"""Trim a recording: keep the audio between two times, and its transcript (#660).

The user picks a start and an end in the timeline of the segment editor. The
audio outside that range is cut away and the transcript is cut with it, so
nothing is transcribed again:

- a segment that ends before the start or begins after the end is dropped;
- a segment across the start or the end is clipped to the range (its text is
  kept: a segment has no times per word to cut it by);
- every time is moved back by the start, so the transcript starts at 0:00.

Two phases, as a merge (src/services/recording_merge.py):

1. ``request_trim`` runs in the request. It checks the range against the
   stored recording, writes the audit event and queues a ``trim`` job.
2. ``run_trim_job`` runs on a worker of the convert queue (ffmpeg). It cuts
   the audio into a new file, stores it, and in one transaction points the
   recording at it with the trimmed transcript and the new duration. The old
   file is deleted only after that commit. A failure before the commit
   leaves the recording as it was; the job runs again only on an error that
   may pass (the storage), and a job that runs again after its commit finds
   the new file in place and does nothing.

What depends on the audio or the transcript follows: the waveform peaks are
removed (they are computed again for the new file), the Inquire chunks are
removed and built again, the speaker snippets of dropped segments are
removed and the others renumbered and moved in time, the voice embeddings of
speakers without a segment left are removed, and a stored translation is cut
the same way. The summary is not written again by itself: the editor offers
to regenerate it with the trim (``regenerate_summary``).

The job is a background job (job_queue.BACKGROUND_JOBS): it leaves the
recording's status alone, so a failed trim never marks a finished
recording FAILED or replaces its transcript with an error.
"""

import json
import os
from datetime import datetime

from flask import current_app

from src.database import db
from src.services.storage.service import get_storage_service

# Shortest audio a trim may keep, in seconds.
MIN_KEPT_SECONDS = 1.0
# Start or end this close to the edges of the audio count as the edges.
EDGE_TOLERANCE = 0.05
# An end this far past the stored length is taken as the end of the audio
# (a player can measure a file a little longer than ffprobe did).
END_OVERSHOOT = 1.0
# A segment clipped shorter than this is dropped.
MIN_SEGMENT_SECONDS = 0.05

# Encoders for the codecs ffprobe names, so that the trimmed file has the
# codec of the original. A codec not listed is encoded with the default
# encoder of the file's container.
_ENCODERS = {
    'aac': 'aac', 'mp3': 'libmp3lame', 'opus': 'libopus', 'vorbis': 'libvorbis', 'flac': 'flac',
    'alac': 'alac', 'pcm_s16le': 'pcm_s16le', 'pcm_s24le': 'pcm_s24le', 'pcm_s32le': 'pcm_s32le',
    'pcm_f32le': 'pcm_f32le', 'pcm_u8': 'pcm_u8', 'pcm_s16be': 'pcm_s16be', 'pcm_mulaw': 'pcm_mulaw',
    'pcm_alaw': 'pcm_alaw', 'amr_nb': 'libopencore_amrnb',
}
_LOSSLESS = ('flac', 'alac')


# The reasons a trim is refused or fails: server.apiError.<code>
ERROR_CODES = (
    'trim_no_audio', 'trim_not_completed', 'trim_needs_segments', 'trim_unknown_duration', 'trim_invalid_range',
    'trim_too_short', 'trim_nothing_removed', 'trim_needs_times', 'trim_busy', 'trim_failed', 'trim_store_failed',
)


class TrimError(Exception):
    """The trim cannot be done. ``code`` names the reason for the API
    (server.apiError.<code>); ``permanent`` stops the job queue's retries."""

    def __init__(self, code, permanent=True):
        super().__init__(code)
        self.code = code
        self.permanent = permanent


# ---- the transcript -----------------------------------------------------------

def _time(segment, *keys):
    for key in keys:
        value = segment.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value), key
    return None, keys[0]


def _round(value):
    return round(value, 3)


def trim_segments(segments, start, end):
    """The segments kept by a trim to [start, end], moved to start at 0.

    Returns (kept, index_map): ``kept`` the new list (copies, with the same
    keys as the stored ones), ``index_map`` the old index of each kept
    segment -> its new index. A segment without times raises TrimError.
    """
    kept = []
    index_map = {}
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        seg_start, start_key = _time(segment, 'start_time', 'start')
        seg_end, end_key = _time(segment, 'end_time', 'end')
        if seg_start is None or seg_end is None:
            raise TrimError('trim_needs_times')
        if seg_end <= start or seg_start >= end:
            continue
        new_start = max(seg_start, start) - start
        new_end = min(seg_end, end) - start
        if new_end - new_start < MIN_SEGMENT_SECONDS and (seg_start < start or seg_end > end):
            continue
        copy = dict(segment)
        copy[start_key] = _round(new_start)
        copy[end_key] = _round(new_end)
        words = segment.get('words')
        if isinstance(words, list):
            copy['words'] = _trim_words(words, start, end)
        index_map[index] = len(kept)
        kept.append(copy)
    return kept, index_map


def _trim_words(words, start, end):
    out = []
    for word in words:
        if not isinstance(word, dict):
            continue
        w_start, s_key = _time(word, 'start', 'start_time')
        w_end, e_key = _time(word, 'end', 'end_time')
        if w_start is None or w_end is None:
            continue
        if w_end <= start or w_start >= end:
            continue
        copy = dict(word)
        copy[s_key] = _round(max(w_start, start) - start)
        copy[e_key] = _round(min(w_end, end) - start)
        out.append(copy)
    return out


def _segment_list(text):
    try:
        data = json.loads(text) if text else None
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _speakers(segments):
    return {seg.get('speaker') for seg in segments if isinstance(seg, dict) and seg.get('speaker')}


# ---- the checks -----------------------------------------------------------------

def check_range(recording, start, end):
    """(start, end, segments) for a trim of this recording, or TrimError."""
    if recording.audio_deleted_at or not recording.audio_path:
        raise TrimError('trim_no_audio')
    if recording.status != 'COMPLETED':
        raise TrimError('trim_not_completed')
    segments = _segment_list(recording.transcription)
    if segments is None:
        raise TrimError('trim_needs_segments')
    duration = recording.audio_duration_seconds
    if not duration or duration <= 0:
        raise TrimError('trim_unknown_duration')
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError):
        raise TrimError('trim_invalid_range')
    if start != start or end != end:          # NaN
        raise TrimError('trim_invalid_range')
    if start < 0 or end > duration + END_OVERSHOOT or end <= start:
        raise TrimError('trim_invalid_range')
    end = min(end, duration)
    if end - start < MIN_KEPT_SECONDS:
        raise TrimError('trim_too_short')
    if start <= EDGE_TOLERANCE and end >= duration - EDGE_TOLERANCE:
        raise TrimError('trim_nothing_removed')
    trim_segments(segments, start, end)        # raises on a segment without times
    return start, end, segments


def _active_job(recording_id):
    from src.models import ProcessingJob
    return ProcessingJob.query.filter(
        ProcessingJob.recording_id == recording_id,
        ProcessingJob.status.in_(['queued', 'processing']),
        ProcessingJob.job_type != 'index_chunks',
    ).first()


def request_trim(recording, user, start, end, regenerate_summary=False):
    """Check the range, record the event and queue the trim. Returns the job id."""
    from src.services import audit
    from src.services.job_queue import job_queue

    start, end, segments = check_range(recording, start, end)
    if _active_job(recording.id):
        raise TrimError('trim_busy')
    kept, _ = trim_segments(segments, start, end)
    audit.record_in_tx('recording.trimmed', recording=recording, details={
        'start': _round(start), 'end': _round(end), 'removed_segments': len(segments) - len(kept)})
    db.session.commit()
    return job_queue.enqueue(
        user_id=user.id,
        recording_id=recording.id,
        job_type='trim',
        params={
            'start': start,
            'end': end,
            'source_audio_path': recording.audio_path,
            'regenerate_summary': bool(regenerate_summary),
            'user_id': user.id,
        },
    )


# ---- the audio ------------------------------------------------------------------

def _cut_command(source, output, start, end, codec_info):
    """ffmpeg arguments that keep [start, end] of the source, re-encoded with
    the source's codec (the cut is exact to the sample, which copying the
    stream is not)."""
    cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', source,
           '-ss', f'{start:.3f}', '-to', f'{end:.3f}', '-map', '0:a:0']
    if codec_info.get('has_video'):
        cmd += ['-map', '0:v:0?']
    cmd += ['-map_metadata', '0']
    codec = (codec_info.get('audio_codec') or '').lower()
    encoder = _ENCODERS.get(codec)
    if encoder:
        cmd += ['-c:a', encoder]
        bit_rate = codec_info.get('bit_rate')
        if not codec.startswith('pcm_') and codec not in _LOSSLESS and bit_rate:
            try:
                bits = int(bit_rate)
            except (TypeError, ValueError):
                bits = 0
            if bits >= 8000:
                cmd += ['-b:a', str(min(bits, 512000))]
    if codec_info.get('sample_rate'):
        cmd += ['-ar', str(codec_info['sample_rate'])]
    cmd.append(output)
    return cmd


def _extension(locator):
    ext = os.path.splitext(str(locator or '').split('?', 1)[0])[1].lower()
    return ext if 1 < len(ext) <= 6 else '.m4a'


def _cut_audio(local_path, staging_path, start, end):
    from src.utils.ffmpeg_utils import FFmpegError, _run_ffmpeg_command
    from src.utils.ffprobe import get_codec_info, get_duration

    try:
        info = get_codec_info(local_path) or {}
    except Exception:
        info = {}
    try:
        _run_ffmpeg_command(_cut_command(local_path, staging_path, start, end, info), 'trim')
    except FFmpegError as error:
        if info.get('audio_codec') and _ENCODERS.get(str(info['audio_codec']).lower()):
            # The codec's encoder is not in this ffmpeg build: the default
            # encoder of the container
            current_app.logger.warning(f"Trim with the original codec failed ({type(error).__name__}); "
                                       f"trying the container's default encoder")
            _run_ffmpeg_command(_cut_command(local_path, staging_path, start, end,
                                             {**info, 'audio_codec': None}), 'trim')
        else:
            raise
    if not os.path.exists(staging_path) or os.path.getsize(staging_path) == 0:
        raise TrimError('trim_failed')
    try:
        duration = get_duration(staging_path)
    except Exception:
        duration = None
    return duration or (end - start)


def _safe_unlink(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---- what follows the audio -------------------------------------------------------

def _trim_snippets(recording_id, index_map, start, end):
    from src.models import SpeakerSnippet
    for snippet in SpeakerSnippet.query.filter_by(recording_id=recording_id).all():
        new_index = index_map.get(snippet.segment_index)
        inside = snippet.timestamp is None or start <= snippet.timestamp <= end
        if new_index is None or not inside:
            db.session.delete(snippet)
            continue
        snippet.segment_index = new_index
        if snippet.timestamp is not None:
            snippet.timestamp = _round(max(0.0, snippet.timestamp - start))


def _trim_embeddings(recording, old_speakers, new_speakers):
    """The voice embeddings of speakers that have no segment left are removed.
    A key is removed only when it (or the name it was assigned) was a speaker
    of the transcript before the trim and is none after it."""
    embeddings = recording.speaker_embeddings
    if not isinstance(embeddings, dict) or not embeddings:
        return
    label_map = recording.speaker_label_map if isinstance(recording.speaker_label_map, dict) else {}
    kept = {}
    for key, vector in embeddings.items():
        names = {key, label_map.get(key)} - {None}
        if names & old_speakers and not names & new_speakers:
            continue
        kept[key] = vector
    if len(kept) != len(embeddings):
        recording.speaker_embeddings = kept or None
        if label_map:
            remaining = {k: v for k, v in label_map.items() if k in kept or v in new_speakers}
            recording.speaker_label_map = remaining or None


def _trim_translation(recording, index_map, old_count, kept):
    """The stored translation, cut like the transcript when it has one
    segment per segment of the transcript; otherwise it is removed."""
    if not recording.transcription_translation:
        return
    translated = _segment_list(recording.transcription_translation)
    if translated is None or len(translated) != old_count:
        recording.transcription_translation = None
        recording.transcription_translation_language = None
        return
    out = []
    for old_index, new_index in sorted(index_map.items(), key=lambda item: item[1]):
        segment = dict(translated[old_index])
        for key in ('start_time', 'end_time', 'start', 'end'):
            if key in kept[new_index]:
                segment[key] = kept[new_index][key]
        out.append(segment)
    recording.transcription_translation = json.dumps(out, ensure_ascii=False)


# ---- the job -----------------------------------------------------------------------

def run_trim_job(recording, params):
    """Cut the audio and the transcript of a queued trim (convert worker)."""
    params = params or {}
    start = params.get('start')
    end = params.get('end')
    source = params.get('source_audio_path')
    if recording.audio_path != source:
        # This job's commit happened before (the job runs again after a
        # crash), or the audio was replaced: nothing is cut twice
        current_app.logger.info(f"Trim of recording {recording.id}: the audio is no longer the one asked for; nothing to do")
        return {'skipped': True}

    start, end, segments = check_range(recording, start, end)
    kept, index_map = trim_segments(segments, start, end)

    storage = get_storage_service()
    if not storage.exists(source):
        raise TrimError('trim_no_audio')

    upload_folder = current_app.config['UPLOAD_FOLDER']
    os.makedirs(upload_folder, exist_ok=True)
    now = datetime.utcnow()
    ext = _extension(source)
    staging_path = os.path.join(upload_folder, f"trim_{now.strftime('%Y%m%d%H%M%S%f')}_{recording.id}{ext}")

    from src.utils.ffmpeg_utils import FFmpegError
    try:
        with storage.materialize(source) as materialized:
            duration = _cut_audio(materialized.local_path, staging_path, start, end)
        size = os.path.getsize(staging_path)
    except FFmpegError as error:
        _safe_unlink(staging_path)
        current_app.logger.error(f"Trim ffmpeg failed for recording {recording.id}: {type(error).__name__}")
        raise TrimError('trim_failed')
    except TrimError:
        _safe_unlink(staging_path)
        raise
    except Exception:
        _safe_unlink(staging_path)
        raise

    stem = os.path.splitext(os.path.basename(recording.original_filename or 'recording'))[0] or 'recording'
    key = storage.build_recording_key(f"{stem}_trimmed{ext}", recording.id, now=now)
    try:
        stored = storage.upload_local_file(staging_path, key, content_type=recording.mime_type or None,
                                           delete_source=True)
    except Exception as error:
        _safe_unlink(staging_path)
        current_app.logger.error(f"Trim storage upload failed for recording {recording.id}: {type(error).__name__}")
        # The storage may be back on the next attempt; the recording is unchanged
        raise TrimError('trim_store_failed', permanent=False)

    old_speakers = _speakers(segments)
    try:
        recording.audio_path = stored.locator
        recording.file_size = size
        recording.audio_duration_seconds = float(duration)
        # The translation first, in the same flush as the transcript, so the
        # flush listener keeps it (recording_changes._reconcile_translation)
        _trim_translation(recording, index_map, len(segments), kept)
        recording.transcription = json.dumps(kept, ensure_ascii=False)
        _trim_snippets(recording.id, index_map, start, end)
        _trim_embeddings(recording, old_speakers, _speakers(kept))
        from src.models import TranscriptChunk
        TranscriptChunk.query.filter_by(recording_id=recording.id).delete(synchronize_session=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        try:
            storage.delete(stored.locator, missing_ok=True)
        except Exception:
            pass
        raise

    # The recording points at the new file: the old one goes
    try:
        storage.delete(source, missing_ok=True)
    except Exception as error:
        current_app.logger.warning(f"Trim of recording {recording.id}: the old audio file was not deleted "
                                   f"({type(error).__name__})")
    from src.services.waveform_peaks import remove_peaks
    remove_peaks(upload_folder, recording.id)

    from src.api import recordings as recordings_api
    recordings_api.reindex_recording_chunks_async(recording.id)

    summary_queued = False
    if params.get('regenerate_summary') and kept:
        from src.models import User
        from src.services.job_queue import job_queue
        from src.services.reprocessing import follow_up_summary_params
        user = db.session.get(User, params.get('user_id') or recording.user_id)
        if user is not None:
            job_queue.enqueue(user_id=user.id, recording_id=recording.id, job_type='summarize',
                              params=follow_up_summary_params(recording, user))
            summary_queued = True
    if not summary_queued:
        try:
            from src.file_exporter import export_recording
            export_recording(recording.id)
        except Exception as error:
            current_app.logger.warning(f"Export after the trim of recording {recording.id} failed: {type(error).__name__}")
    return {'summary_queued': summary_queued}


def trim_state(recording_id):
    """The newest trim job of a recording: {state, error, summary_queued}."""
    from src.models import ProcessingJob
    job = (ProcessingJob.query.filter_by(recording_id=recording_id, job_type='trim')
           .order_by(ProcessingJob.id.desc()).first())
    if job is None:
        return {'state': 'none'}
    params = {}
    try:
        params = json.loads(job.params) if job.params else {}
    except (TypeError, ValueError):
        params = {}
    out = {'state': job.status, 'job_id': job.id}
    if job.status == 'failed':
        out['error'] = job.error_message
    if job.status == 'completed':
        out['summary_queued'] = bool(params.get('regenerate_summary'))
    return out
