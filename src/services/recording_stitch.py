"""Stitch worker for server-side recording chunks (#287 c/d).

The :func:`stitch_recording_session` function is called from the
``stitch`` job_queue dispatch. It

1. Reads the session row + on-disk chunks.
2. Partitions the chunks into *segments*. A segment is the output of one
   continuous ``MediaRecorder`` instance: its first chunk carries the
   container initialization header (WebM/Matroska EBML, or fMP4 ftyp) and
   every later chunk is a headerless continuation. A normal recording is a
   single segment; a recording that was resumed after a page reload (a new
   MediaRecorder appended to the same session) has one segment per
   MediaRecorder. Segment boundaries are detected from the bytes
   themselves (header magic at a chunk's start), so no client-side
   bookkeeping or schema change is required.
3. Assembles the audio:
   - WITHIN a segment, the chunks are **byte-joined** (raw concatenation in
     index order). This is the correct reassembly for MediaRecorder
     timeslice fragments — only the first fragment has a header, so the
     joined stream is a valid container. (Feeding the fragments to ffmpeg's
     concat *demuxer* instead would decode only the first headered fragment
     and silently drop the rest — the bug this design replaces.)
   - ACROSS segments (resume case only), the per-segment byte-joined files
     are each independently valid, so they are combined with ffmpeg's
     concat demuxer ``-c copy`` — the right tool at this granularity.
   - A single-segment recording is finished with a stream-copy remux pass
     so the output gets a seekable container with proper duration metadata
     (raw MediaRecorder output is "live": no SeekHead/Cues, unknown
     duration, which trips players, the duration probe, and some ASR).
4. Moves the stitched file into UPLOAD_FOLDER with a deterministic name.
5. Updates the placeholder Recording row with the resulting path, size,
   and a status transition to PENDING (so the downstream transcribe job
   picks it up).
6. Removes the session directory and marks the session ``finalized``.
7. Enqueues a ``transcribe`` job for the recording.

Any failure flips the recording status to FAILED with a descriptive
``transcription`` payload (mirrors the existing failure-surface format
upload_file uses) and the session to ``failed`` so the user can see what
happened.
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Tuple

from src.database import db
from src.models import RecordingSession, Recording
from src.utils.ffmpeg_utils import decode_ffmpeg_output


logger = logging.getLogger(__name__)


class StitchError(Exception):
    """Raised when concat / move / cleanup fails. The message is surfaced
    on the Recording's ``transcription`` field so the user can see what
    went wrong from the UI."""


def _session_dir(upload_folder: str, session_id: str) -> str:
    return os.path.join(upload_folder, '_sessions', session_id)


def _chunk_paths(session_dir: str) -> list:
    """Return chunk file paths in monotonic order. ``chunk-NNNNNN.bin``
    naming sorts lexicographically by index because of the zero pad."""
    if not os.path.isdir(session_dir):
        return []
    entries = sorted(
        e for e in os.listdir(session_dir)
        if e.startswith('chunk-') and e.endswith('.bin')
    )
    return [os.path.join(session_dir, e) for e in entries]


def _mime_to_extension(mime_type: str) -> str:
    """Pick a sensible output extension for the stitched container."""
    mapping = {
        'audio/webm': 'webm',
        'audio/ogg': 'ogg',
        'audio/mp4': 'm4a',
        'audio/x-m4a': 'm4a',
        'audio/mpeg': 'mp3',
        'audio/wav': 'wav',
        'video/webm': 'webm',
        'video/mp4': 'mp4',
    }
    return mapping.get((mime_type or '').lower(), 'webm')


# Container initialization-segment signatures. A chunk that STARTS a fresh
# MediaRecorder stream begins with one of these; a timeslice continuation
# chunk does not. Ogg/WAV are intentionally absent: Ogg pages all start with
# 'OggS' (so it can't delimit segments) but Ogg is self-framing and chains
# losslessly under a plain byte-join, and WAV is single-shot — both are
# handled correctly as one byte-joined segment.
_WEBM_EBML_MAGIC = b'\x1a\x45\xdf\xa3'        # Matroska/WebM EBML header, offset 0
_MP4_FTYP_MAGIC = b'ftyp'                      # ISO-BMFF ftyp box type, offset 4


def _chunk_starts_segment(chunk_path: str) -> bool:
    """True if this chunk begins a new container (a fresh MediaRecorder)."""
    try:
        with open(chunk_path, 'rb') as f:
            head = f.read(12)
    except OSError:
        return False
    return head[0:4] == _WEBM_EBML_MAGIC or head[4:8] == _MP4_FTYP_MAGIC


def _partition_into_segments(chunk_paths: list) -> list:
    """Group ordered chunk paths into segments by header detection.

    Returns a list of lists; each inner list is the chunks of one segment in
    order. The first chunk always opens a segment; later chunks open a new
    segment only when they carry a container header (i.e. a resumed
    recording started a new MediaRecorder)."""
    segments = []
    for i, p in enumerate(chunk_paths):
        if i == 0 or _chunk_starts_segment(p):
            segments.append([p])
        else:
            segments[-1].append(p)
    return segments


def _byte_join(chunk_paths: list, output_path: str) -> None:
    """Raw byte concatenation in order, streamed for flat memory use."""
    with open(output_path, 'wb') as out:
        for p in chunk_paths:
            with open(p, 'rb') as src:
                shutil.copyfileobj(src, out, length=1024 * 1024)


def _remux_copy(src_path: str, output_path: str) -> None:
    """Stream-copy remux ``src`` → ``output`` for a seekable container with
    duration metadata. On any ffmpeg problem, fall back to using the raw
    byte-joined stream directly (it is already valid audio) rather than
    losing the recording — logged loudly so it can be investigated."""
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-i', src_path, '-c', 'copy', output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
    except FileNotFoundError:
        logger.warning("ffmpeg not found; using raw byte-joined stream without remux")
        os.replace(src_path, output_path)
        return
    except subprocess.TimeoutExpired:
        raise StitchError('ffmpeg remux timed out after 10 minutes')
    if result.returncode != 0:
        stderr = decode_ffmpeg_output(result.stderr).strip()
        logger.warning(
            f"Remux failed (exit {result.returncode}): {stderr[:300]}; "
            "falling back to raw byte-joined stream"
        )
        os.replace(src_path, output_path)


def _concat_demux(segment_files: list, output_path: str, work_dir: str) -> None:
    """Combine multiple independently-valid segment files via ffmpeg's
    concat demuxer (the correct tool once each input is a complete
    container). Used only for resumed recordings (>1 segment)."""
    manifest_path = os.path.join(work_dir, 'segments.concat.txt')
    with open(manifest_path, 'w') as f:
        for p in segment_files:
            safe = p.replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'concat', '-safe', '0', '-i', manifest_path,
        '-c', 'copy', output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
    except FileNotFoundError:
        raise StitchError('ffmpeg binary not found on server PATH')
    except subprocess.TimeoutExpired:
        raise StitchError('ffmpeg segment concat timed out after 10 minutes')
    if result.returncode != 0:
        stderr = decode_ffmpeg_output(result.stderr).strip()
        raise StitchError(f'ffmpeg segment concat failed (exit {result.returncode}): {stderr[:500]}')


def _assemble_session_audio(chunk_paths: list, output_path: str, mime_type: str) -> None:
    """Assemble MediaRecorder chunks into one playable file.

    Byte-joins within each detected segment, then either remuxes the single
    segment or concat-demuxes multiple segments (resume case). See the module
    docstring for the rationale.
    """
    if not chunk_paths:
        raise StitchError('no chunks to stitch')

    segments = _partition_into_segments(chunk_paths)
    work_dir = output_path + '.parts'
    os.makedirs(work_dir, exist_ok=True)
    try:
        segment_files = []
        for i, seg_chunks in enumerate(segments):
            seg_path = os.path.join(work_dir, f'segment-{i:04d}.bin')
            _byte_join(seg_chunks, seg_path)
            segment_files.append(seg_path)

        if len(segment_files) == 1:
            logger.info(f"Assembling {len(chunk_paths)} chunks (1 segment) → byte-join + remux → {output_path}")
            _remux_copy(segment_files[0], output_path)
        else:
            logger.info(
                f"Assembling {len(chunk_paths)} chunks across {len(segment_files)} segments "
                f"(resume detected) → byte-join per segment + concat → {output_path}"
            )
            _concat_demux(segment_files, output_path, work_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _apply_finalize_metadata(recording, metadata):
    """Attach the tags picked in the recorder's Upload Settings panel and
    resolve tag/folder ASR-option defaults, mirroring ``upload_file``.

    The finalize metadata carries ``tags`` (the tag objects selected in the
    review pane), but the stitch path historically dropped them — tags chosen
    before clicking Upload were silently lost. This creates the RecordingTag
    associations (ownership-checked, order preserved) and fills any ASR
    options the user left blank from the first tag's defaults, falling back
    to the folder's defaults when no tags are selected — the same precedence
    chain as a drag-drop upload. Mutates and returns ``metadata`` so the
    transcribe kickoff sees the resolved options.
    """
    from src.models import Tag, RecordingTag
    from src.models.organization import Folder, GroupMembership

    raw_tags = metadata.get('tags') or []
    tag_ids = []
    for entry in raw_tags:
        tid = entry.get('id') if isinstance(entry, dict) else entry
        try:
            tag_ids.append(int(tid))
        except (TypeError, ValueError):
            continue

    selected_tags = []
    for order, tid in enumerate(tag_ids, 1):
        tag = db.session.get(Tag, tid)
        if not tag:
            continue
        allowed = tag.user_id == recording.user_id or (
            tag.group_id
            and GroupMembership.query.filter_by(
                group_id=tag.group_id, user_id=recording.user_id).first()
        )
        if not allowed:
            logger.warning(
                f"Skipping tag {tid} on recording {recording.id}: "
                f"user {recording.user_id} has no access")
            continue
        if RecordingTag.query.filter_by(recording_id=recording.id, tag_id=tag.id).first():
            selected_tags.append(tag)
            continue
        db.session.add(RecordingTag(
            recording_id=recording.id,
            tag_id=tag.id,
            order=order,
            added_at=datetime.utcnow(),
        ))
        selected_tags.append(tag)

    first_tag = selected_tags[0] if selected_tags else None
    folder = db.session.get(Folder, recording.folder_id) if recording.folder_id else None

    def fill(key, attr):
        if metadata.get(key):
            return
        if first_tag and getattr(first_tag, attr, None):
            metadata[key] = getattr(first_tag, attr)
        elif not selected_tags and folder and getattr(folder, attr, None):
            metadata[key] = getattr(folder, attr)

    fill('language', 'default_language')
    fill('min_speakers', 'default_min_speakers')
    fill('max_speakers', 'default_max_speakers')
    fill('hotwords', 'default_hotwords')
    fill('initial_prompt', 'default_initial_prompt')
    fill('transcription_model', 'default_transcription_model')

    if first_tag is not None:
        metadata['tag_id'] = first_tag.id

    return metadata


def stitch_recording_session(session_id: str) -> Tuple[int, str, dict]:
    """Stitch a session's chunks into a final media file.

    Returns ``(recording_id, media_path, metadata)`` on success, where
    ``metadata`` is the parsed finalize metadata used to kick off the
    downstream transcribe job. Raises :class:`StitchError` on any failure;
    the caller (job_queue worker) is responsible for updating the Recording
    row's status and surfacing the error to the user.
    """
    session = db.session.get(RecordingSession, session_id)
    if not session:
        raise StitchError(f'session {session_id} not found')
    if not session.finalized_recording_id:
        raise StitchError(f'session {session_id} has no finalized_recording_id')

    recording = db.session.get(Recording, session.finalized_recording_id)
    if not recording:
        raise StitchError(f'recording {session.finalized_recording_id} not found')

    from flask import current_app
    upload_folder = current_app.config.get('UPLOAD_FOLDER') or '/data/uploads'
    sess_dir = _session_dir(upload_folder, session_id)

    chunk_paths = _chunk_paths(sess_dir)
    if not chunk_paths:
        raise StitchError(f'session {session_id} has no chunks on disk')

    extension = _mime_to_extension(session.mime_type)
    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    final_filename = f'{timestamp}_recording-{session_id[:8]}.{extension}'
    final_path = os.path.join(upload_folder, final_filename)

    _assemble_session_audio(chunk_paths, final_path, session.mime_type)

    file_size = os.path.getsize(final_path)

    # Validate the stitched output before claiming success. ffmpeg can
    # exit 0 while writing a truncated file (disk full, OOM kill mid-
    # write); re-probe so we surface a clean failure here instead of a
    # confusing "audio unreadable" error during downstream transcription.
    try:
        from src.utils.ffprobe import get_codec_info
        probe = get_codec_info(final_path, timeout=10)
        probed_duration = probe.get('duration') if probe else None
    except Exception as e:
        logger.warning(f"Post-stitch probe failed for {final_path}: {e}")
        probe = None
        probed_duration = None
    if file_size <= 0 or (probe is not None and probed_duration is not None and probed_duration <= 0.5):
        # Try to clean up the bad output so a retry has a clean slate.
        try:
            if os.path.exists(final_path):
                os.remove(final_path)
        except OSError:
            pass
        raise StitchError(
            f'stitched output for session {session_id} is invalid '
            f'(size={file_size}, duration={probed_duration}); ffmpeg may have '
            'been killed mid-write or run out of disk space'
        )

    # Parse the user's finalize metadata up front: it drives both the title
    # and the downstream transcribe-job options (tags, ASR settings, etc.).
    metadata = {}
    if session.finalize_metadata:
        try:
            metadata = json.loads(session.finalize_metadata) or {}
        except json.JSONDecodeError:
            metadata = {}

    # Update the recording row in place. Resolve the title through the SAME
    # shared helper as drag-drop upload and the PWA share target
    # (src/utils/titles.py): a real user-supplied title is kept, otherwise we
    # set a recognised placeholder so generate_title_task gives it an AI
    # title — instead of a fabricated default being mistaken for a user
    # choice and AI titling getting skipped (the same class of bug fixed for
    # the share target in 482614c).
    from src.utils.titles import resolve_upload_title
    user_title = (metadata.get('title') or '').strip()
    recording.title = resolve_upload_title(user_title, final_filename)
    recording.audio_path = final_path
    recording.original_filename = final_filename
    recording.file_size = file_size
    recording.status = 'PENDING'
    # The session's mime_type is a client claim; the stitched bytes are
    # authoritative. Re-derive the MIME from the validation probe above so a
    # video capture is recognized as video (and vice versa) regardless of
    # what the client declared — same ffprobe-first policy as the upload
    # pipeline. Keep the claimed value when the probe was unavailable.
    if probe is not None:
        try:
            from src.utils.mime import resolve_media_mime
            recording.mime_type = resolve_media_mime(final_path, codec_info=probe)
        except Exception as e:
            logger.warning(f"Could not resolve stitched mime for {final_path}: {e}")
    if not recording.meeting_date:
        recording.meeting_date = session.created_at

    # Attach the tags picked in the review pane and resolve tag/folder
    # ASR-option defaults into the metadata (same precedence as upload_file).
    metadata = _apply_finalize_metadata(recording, metadata)

    session.status = 'finalized'
    session.finalized_at = datetime.utcnow()
    session.last_seen_at = datetime.utcnow()
    db.session.commit()

    # Remove the session directory now that we have the stitched output.
    try:
        if os.path.isdir(sess_dir):
            shutil.rmtree(sess_dir, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Could not remove session dir for {session_id}: {e}")

    # metadata (parsed above) carries tags, ASR options, hotwords, etc. for
    # the downstream transcribe job, as if they had been on a normal upload
    # form.
    return recording.id, final_path, metadata


def _try_kickoff_merge(recording, user_id, metadata) -> bool:
    """If the finalize metadata carries a ``merge_intent``, create the merged
    recording (this clip + the chosen existing recordings, in the chosen order)
    and enqueue the merge job, skipping this clip's standalone transcription.

    Returns True when a merge was enqueued, False to fall through to the normal
    transcribe. Any failure returns False so the clip is transcribed normally
    rather than left in limbo.
    """
    intent = (metadata or {}).get('merge_intent')
    if not isinstance(intent, dict):
        return False

    order = intent.get('order')
    if not isinstance(order, list) or '__self__' not in order:
        return False

    from flask import current_app
    from src.database import db
    from src.models import User
    from src.services.recording_merge import create_merge_recording, MergeError, _UNSET

    try:
        # Substitute this clip's real id for the "__self__" placeholder, coercing
        # the existing ids to ints and dropping anything unusable.
        source_ids = []
        for entry in order:
            if entry == '__self__':
                source_ids.append(recording.id)
            else:
                try:
                    source_ids.append(int(entry))
                except (TypeError, ValueError):
                    continue

        if len(source_ids) < 2 or recording.id not in source_ids:
            return False

        user = db.session.get(User, user_id)
        if not user:
            return False

        # Resolve which source's notes to keep. '__self__' -> the clip; an id ->
        # that source; null -> none; absent -> first source (default).
        if 'notes_source' in intent:
            ns = intent.get('notes_source')
            if ns == '__self__':
                notes_source_id = recording.id
            elif ns is None:
                notes_source_id = None
            else:
                try:
                    notes_source_id = int(ns)
                except (TypeError, ValueError):
                    notes_source_id = _UNSET
        else:
            notes_source_id = _UNSET

        # Trusted path: the clip's audio was just produced by the stitch, so it
        # is final even though the recording is not COMPLETED (require_settled
        # off). Ownership + audio-existence are still enforced.
        create_merge_recording(
            user,
            source_ids,
            title=(intent.get('title') or None),
            delete_originals=bool(intent.get('delete_originals', False)),
            require_settled=False,
            notes_source_id=notes_source_id,
        )
        return True
    except MergeError as e:
        current_app.logger.warning(
            f"Merge-from-recording for {recording.id} failed ({e}); transcribing normally instead."
        )
        return False
    except Exception as e:
        current_app.logger.error(
            f"Unexpected error routing recording {recording.id} into a merge: {e}; transcribing normally."
        )
        return False


def kickoff_transcription_for_stitched(
    recording_id: int,
    user_id: int,
    metadata: dict,
) -> None:
    """Enqueue the downstream transcribe job through the shared param resolver
    so a stitched recording-session file honors the owner's tag/folder/account
    defaults just like a normal upload. The session's own selections (captured in
    ``metadata``) act as the per-request overrides. Idempotent (the job_queue
    rejects duplicate active jobs of the same type for the same recording)."""
    from src.services.job_queue import job_queue
    from src.services.transcription_defaults import resolve_transcription_params
    from src.database import db
    from src.models import Recording

    recording = db.session.get(Recording, recording_id)

    # Merge-from-recording (issue #323): if the user chose to merge this clip
    # into existing recording(s) BEFORE finishing, route the freshly-stitched
    # audio straight into a merge instead of transcribing it on its own. The clip
    # is therefore never transcribed standalone — only the combined recording is,
    # once. Falls back to a normal transcribe on any problem so the clip is never
    # left orphaned.
    if _try_kickoff_merge(recording, user_id, metadata):
        return

    overrides = {
        'min_speakers': metadata.get('min_speakers'),
        'max_speakers': metadata.get('max_speakers'),
        'hotwords': metadata.get('hotwords'),
        'initial_prompt': metadata.get('initial_prompt'),
        'transcription_model': metadata.get('transcription_model'),
    }
    session_language = metadata.get('language') or metadata.get('asr_language')
    if session_language:
        overrides['language'] = session_language
    if metadata.get('tag_id') is not None:
        overrides['tag_id'] = metadata.get('tag_id')

    job_params = resolve_transcription_params(recording, overrides)
    job_queue.enqueue(
        user_id=user_id,
        recording_id=recording_id,
        job_type='transcribe',
        params=job_params,
        is_new_upload=True,
    )
