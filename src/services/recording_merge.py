"""
Merge multiple recordings into a single new recording.

Use case (issue #323): a call drops or a meeting freezes, leaving the user with
two (or more) separate recordings of what was really one session. Merging
concatenates the source audio, in the order the user chooses, into one new
recording that is then run through the standard processing pipeline
(transcription + diarization + summarization). The result is a single coherent
transcript and summary instead of several partial ones.

Design notes:
- We merge at the AUDIO level and re-process, rather than stitching transcripts.
  Re-transcribing costs time/API, but it is the only way to get continuous
  speaker diarization and one unified summary. Transcript-level stitching cannot
  reconcile speaker identities across independently-diarized files.
- Two phases, mirroring the recording-session ``stitch`` flow:
    1. ``create_merge_recording`` runs IN the request. It validates cheaply
       (ownership, audio present, stable status, count), creates a placeholder
       recording in PROCESSING, and enqueues a ``merge`` job. It does NO ffmpeg,
       so the request returns immediately even for hours-long sources.
    2. ``run_merge_job`` runs in a background worker. It does the heavy ffmpeg
       concat, uploads the result via the storage facade, sets ``audio_path``,
       optionally deletes the originals, then enqueues the ``transcribe`` job.
  This keeps a long re-encode off the request path (a synchronous concat would
  exceed reverse-proxy timeouts on multi-hour meetings).
- The concat uses ffmpeg's concat FILTER (not the demuxer), decoding and
  re-encoding each input. Every input is first normalized to a common sample
  rate / channel layout via ``aformat`` so the filter accepts heterogeneous
  sources (different codecs, sample rates, mono vs stereo). Only the first audio
  stream of each input is used, so video streams are ignored and the merged
  output is audio-only (which is what the transcription pipeline consumes).
"""

import os
from contextlib import ExitStack
from datetime import datetime

from flask import current_app

from src.database import db
from src.models import Recording, RecordingTag
from src.services.job_queue import job_queue
from src.services.storage.service import get_storage_service
from src.utils.ffmpeg_utils import _run_ffmpeg_command, FFmpegError

# Normalization target for the concat filter. 44.1 kHz stereo is a safe,
# widely-supported baseline; the ASR downsamples as needed downstream.
_TARGET_SAMPLE_RATE = '44100'
_TARGET_CHANNEL_LAYOUT = 'stereo'
_MERGED_BITRATE = '192k'

# Statuses whose audio file is guaranteed stable (not being written/converted by
# the pipeline). A source mid-transcription may have its audio_path swapped when
# video is extracted to audio, so PENDING/PROCESSING sources are rejected.
_STABLE_STATUSES = {'COMPLETED', 'SUMMARIZING', 'FAILED'}

# Distinguishes "caller did not choose a notes source" (default to the first
# source, for API back-compat) from "explicitly keep no notes" (None).
_UNSET = object()


class MergeError(Exception):
    """Raised when a merge cannot be performed. The message is user-safe."""


def _union_participants(recordings):
    """Union the comma-separated participants of all sources, de-duplicated
    case-insensitively and order-preserving. Participants are additive across a
    merge, so none should be lost. Returns a comma-separated string or None."""
    seen = set()
    out = []
    for rec in recordings:
        if not rec.participants:
            continue
        for name in (p.strip() for p in rec.participants.split(',')):
            key = name.lower()
            if name and key not in seen:
                seen.add(key)
                out.append(name)
    return ', '.join(out) if out else None


def _build_concat_filter(num_inputs: int) -> str:
    """Build an ffmpeg -filter_complex string that normalizes then concatenates
    the first audio stream of each input."""
    normalize = []
    labels = []
    for i in range(num_inputs):
        normalize.append(
            f"[{i}:a:0]aformat=sample_rates={_TARGET_SAMPLE_RATE}:"
            f"channel_layouts={_TARGET_CHANNEL_LAYOUT}[a{i}]"
        )
        labels.append(f"[a{i}]")
    return (
        ";".join(normalize)
        + ";"
        + "".join(labels)
        + f"concat=n={num_inputs}:v=0:a=1[out]"
    )


def _concat_audio(input_paths: list, output_path: str) -> None:
    """Concatenate local audio files into output_path (AAC/.m4a)."""
    cmd = ['ffmpeg', '-y']
    for path in input_paths:
        cmd += ['-i', path]
    cmd += [
        '-filter_complex', _build_concat_filter(len(input_paths)),
        '-map', '[out]',
        '-c:a', 'aac',
        '-b:a', _MERGED_BITRATE,
        # Put the moov atom first. Without this, MP4 output cannot be decoded
        # from a non-seekable pipe — the WhisperX ASR webservice pipes uploads
        # to ffmpeg stdin and silently decodes 0 samples, which surfaces as a
        # pyannote "(channel, time) torch Tensor" 500 (#372).
        '-movflags', '+faststart',
        output_path,
    ]
    _run_ffmpeg_command(cmd, f"merge concat of {len(input_paths)} recordings")


def _validate_sources(user, recording_ids, storage, require_settled=True):
    """Validate the requested sources and return them in caller order.

    ``require_settled`` gates the "must be a settled/COMPLETED recording" check.
    User-initiated merges keep it on (a source mid-processing could still have
    its audio_path mutated by video->audio extraction). Trusted internal callers
    that just produced the audio themselves — the recording-session stitch that
    routes straight into a merge — pass False, since the audio is final the
    moment the stitch finishes even though the recording is not COMPLETED. The
    ownership and audio-existence checks always apply.

    Raises MergeError on any problem. Returns the list of Recording rows.
    """
    if not recording_ids or len(recording_ids) < 2:
        raise MergeError("Select at least two recordings to merge.")

    # Preserve caller order while rejecting duplicates.
    seen = set()
    ordered_ids = []
    for rid in recording_ids:
        if rid in seen:
            raise MergeError("A recording cannot be merged with itself.")
        seen.add(rid)
        ordered_ids.append(rid)

    recordings = []
    for rid in ordered_ids:
        rec = db.session.get(Recording, rid)
        if not rec or rec.user_id != user.id:
            raise MergeError("One or more recordings were not found.")
        if require_settled and rec.status not in _STABLE_STATUSES:
            raise MergeError(
                f"\"{rec.title or 'A recording'}\" is still processing. "
                "Wait for it to finish before merging."
            )
        if not rec.audio_path or not storage.exists(rec.audio_path):
            raise MergeError(
                f"Audio for \"{rec.title or 'a recording'}\" is unavailable "
                "(it may have been auto-deleted)."
            )
        recordings.append(rec)
    return recordings


def create_merge_recording(user, recording_ids, title=None, delete_originals=False,
                           require_settled=True, notes_source_id=_UNSET):
    """Validate sources and create a placeholder recording queued for merging.

    Runs in the request (or, with ``require_settled=False``, from the stitch
    worker for the merge-from-recording flow). Does NO ffmpeg — the heavy concat
    happens later in ``run_merge_job``. Returns the new (QUEUED) Recording.

    Raises:
        MergeError: on validation failure.
    """
    storage = get_storage_service()
    recordings = _validate_sources(user, recording_ids, storage, require_settled=require_settled)
    ordered_ids = [r.id for r in recordings]

    first = recordings[0]
    merged_title = (title or '').strip() or f"{first.title or 'Recording'} (merged)"

    now = datetime.utcnow()

    # The "reference" source (chosen via notes_source_id) donates both its notes
    # AND its per-recording prompt variables to the merged recording, so the same
    # summarization context carries into the merged recording's pipeline:
    #   - notes_source_id unset  -> the first source (API back-compat default)
    #   - notes_source_id is None -> no reference (keep no notes / prompt vars)
    #   - notes_source_id == <id> -> that source (its notes + prompt vars)
    if notes_source_id is _UNSET:
        reference = first
    elif notes_source_id is None:
        reference = None
    else:
        reference = next((r for r in recordings if r.id == notes_source_id), first)

    merged_notes = reference.notes if reference else None

    # Prompt variables travel with the unioned tags — their names usually come
    # from tag prompts — so union them across ALL sources (kept regardless of the
    # notes choice). On a key collision the reference source wins, e.g. two
    # recordings on the same default prompt with the same variable set to
    # different values keep the reference recording's value.
    merged_prompt_variables = {}
    for rec in recordings:
        if rec.prompt_variables:
            merged_prompt_variables.update(rec.prompt_variables)
    if reference and reference.prompt_variables:
        merged_prompt_variables.update(reference.prompt_variables)
    merged_prompt_variables = merged_prompt_variables or None

    # Participants are additive across a merge — union them so none are lost.
    merged_participants = _union_participants(recordings)

    # Folder: a recording lives in exactly one folder; keep the first source's.
    # meeting_date: earliest source date so the merged session sorts where the
    # conversation began.
    meeting_dates = [r.meeting_date for r in recordings if r.meeting_date]
    merged_meeting_date = min(meeting_dates) if meeting_dates else now

    recording = Recording(
        audio_path=None,
        original_filename=f"{merged_title}.m4a",
        title=merged_title,
        # 'QUEUED' matches what job_queue.enqueue() sets for a transcription-side
        # job; set it here too so the row is consistent even before enqueue.
        status='QUEUED',
        meeting_date=merged_meeting_date,
        user_id=user.id,
        mime_type='audio/mp4',
        notes=merged_notes,
        participants=merged_participants,
        prompt_variables=merged_prompt_variables,
        folder_id=first.folder_id,
        processing_source='merge',
    )
    db.session.add(recording)
    db.session.flush()  # assign recording.id

    # Tags are additive too: union across all sources so none are lost. The first
    # source's tags come first (in order) so the transcription-param resolver
    # still keys off the first source's primary tag; other sources' tags follow.
    order = 1
    seen_tag_ids = set()
    for rec in recordings:
        for tag in rec.tags:
            if tag.id in seen_tag_ids:
                continue
            seen_tag_ids.add(tag.id)
            db.session.add(RecordingTag(
                recording_id=recording.id,
                tag_id=tag.id,
                order=order,
                added_at=datetime.utcnow(),
            ))
            order += 1

    db.session.commit()

    try:
        job_queue.enqueue(
            user_id=user.id,
            recording_id=recording.id,
            job_type='merge',
            params={
                'source_ids': ordered_ids,
                'delete_originals': bool(delete_originals),
            },
            is_new_upload=True,
        )
    except Exception as e:
        current_app.logger.error(f"Merge enqueue failed for recording {recording.id}: {e}")
        recording.status = 'FAILED'
        recording.error_message = f"Processing failed: {e}"
        db.session.commit()
        raise MergeError("Could not queue the merge for processing.")

    return recording


def run_merge_job(recording, params):
    """Perform the audio concat for a queued merge, then queue transcription.

    Runs on a background worker (see JobQueue._run_merge). On success the merged
    recording's audio_path/file_size are set and a ``transcribe`` job is queued.
    Raises MergeError on failure so the worker can flip the recording to FAILED.
    """
    source_ids = (params or {}).get('source_ids') or []
    delete_originals = bool((params or {}).get('delete_originals', False))

    if len(source_ids) < 2:
        raise MergeError("Merge job is missing its source recordings.")

    storage = get_storage_service()

    # Re-fetch and re-validate sources at run time (they may have changed since
    # the request was made).
    sources = []
    for rid in source_ids:
        rec = db.session.get(Recording, rid)
        if not rec or rec.user_id != recording.user_id:
            raise MergeError("A source recording is no longer available.")
        if not rec.audio_path or not storage.exists(rec.audio_path):
            raise MergeError("Audio for a source recording is unavailable.")
        sources.append(rec)

    now = datetime.utcnow()
    upload_folder = current_app.config['UPLOAD_FOLDER']
    os.makedirs(upload_folder, exist_ok=True)
    staging_path = os.path.join(
        upload_folder, f"merged_{now.strftime('%Y%m%d%H%M%S')}_{recording.id}.m4a"
    )

    try:
        with ExitStack() as stack:
            local_paths = []
            for rec in sources:
                materialized = stack.enter_context(storage.materialize(rec.audio_path))
                local_paths.append(materialized.local_path)
            _concat_audio(local_paths, staging_path)

        # Guard against a silent success that produced nothing usable. Capture
        # the size now, before upload — upload_local_file(delete_source=True)
        # removes the staging file, so getsize would fail afterwards.
        if not os.path.exists(staging_path):
            raise MergeError("The merged audio came out empty.")
        merged_size = os.path.getsize(staging_path)
        if merged_size == 0:
            raise MergeError("The merged audio came out empty.")
    except FFmpegError as e:
        current_app.logger.error(f"Merge ffmpeg failed for recording {recording.id}: {e}")
        _safe_unlink(staging_path)
        raise MergeError("Failed to combine the audio files.")
    except MergeError:
        _safe_unlink(staging_path)
        raise
    except Exception as e:
        current_app.logger.error(f"Merge concat failed for recording {recording.id}: {e}")
        _safe_unlink(staging_path)
        raise MergeError("Failed to combine the audio files.")

    try:
        storage_key = storage.build_recording_key(recording.original_filename, recording.id, now=now)
        stored_object = storage.upload_local_file(
            staging_path,
            storage_key,
            content_type='audio/mp4',
            delete_source=True,
        )
        recording.audio_path = stored_object.locator
        recording.file_size = merged_size
        db.session.commit()
    except Exception as e:
        current_app.logger.error(f"Merge storage upload failed for recording {recording.id}: {e}")
        db.session.rollback()
        _safe_unlink(staging_path)
        raise MergeError("Failed to store the merged recording.")

    # Delete originals only after the merged audio is safely stored.
    if delete_originals:
        for rec in sources:
            try:
                _delete_recording(rec, storage)
            except Exception as e:
                current_app.logger.warning(f"Failed to delete source recording {rec.id} after merge: {e}")
        db.session.commit()

    # Queue transcription with the SAME resolved params a normal upload gets, so
    # the merged file honors all of the owner's tag/folder/account preferences
    # (speaker-count hints, hotwords, initial prompt, transcription model) and
    # therefore diarization + auto speaker labelling behave identically (#323).
    from src.services.transcription_defaults import resolve_transcription_params
    job_params = resolve_transcription_params(recording)
    job_queue.enqueue(
        user_id=recording.user_id,
        recording_id=recording.id,
        job_type='transcribe',
        params=job_params,
        is_new_upload=True,
    )


def _delete_recording(recording, storage):
    """Delete a source recording's audio and row. Best-effort on the file."""
    if recording.audio_path:
        try:
            storage.delete(recording.audio_path, missing_ok=True)
        except Exception as e:
            current_app.logger.warning(f"Could not delete audio for recording {recording.id}: {e}")
    db.session.delete(recording)


def _safe_unlink(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
