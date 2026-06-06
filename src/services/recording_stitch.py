"""Stitch worker for server-side recording chunks (#287 c/d).

The :func:`stitch_recording_session` function is called from the
``stitch`` job_queue dispatch. It

1. Reads the session row + on-disk chunks.
2. Builds an ffmpeg concat-demux input file listing the chunks in order.
3. Runs ``ffmpeg -f concat -safe 0 -i list.txt -c copy <out>`` to assemble
   the final audio file. Stream-copy keeps the codec parameters intact
   and avoids re-encoding; the concat demuxer correctly handles
   pause/resume across MediaRecorder restarts (where each restart
   produces a fresh container header, which a naive ``cat`` would
   corrupt).
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
    }
    return mapping.get((mime_type or '').lower(), 'webm')


def _run_ffmpeg_concat(chunk_paths: list, output_path: str, mime_type: str) -> None:
    """Stream-copy concat via ffmpeg's concat demuxer.

    The concat demuxer reads a text manifest and assembles container
    elements correctly across restart boundaries. Stream-copy (``-c copy``)
    avoids re-encoding so this is fast even on long recordings.
    """
    if not chunk_paths:
        raise StitchError('no chunks to stitch')

    # Write the manifest next to the output file.
    manifest_path = output_path + '.concat.txt'
    try:
        with open(manifest_path, 'w') as f:
            for p in chunk_paths:
                # The concat demuxer requires the directive prefix; we
                # quote single quotes inside paths defensively.
                safe = p.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        cmd = [
            'ffmpeg',
            '-hide_banner', '-loglevel', 'error',
            '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', manifest_path,
            '-c', 'copy',
            output_path,
        ]
        logger.info(f"Running ffmpeg concat with {len(chunk_paths)} chunks → {output_path}")
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            stderr = (result.stderr.decode('utf-8', errors='replace') or '').strip()
            raise StitchError(f'ffmpeg concat failed (exit {result.returncode}): {stderr[:500]}')
    except FileNotFoundError:
        raise StitchError('ffmpeg binary not found on server PATH')
    except subprocess.TimeoutExpired:
        raise StitchError('ffmpeg concat timed out after 10 minutes')
    finally:
        # Clean up the manifest regardless of outcome.
        try:
            if os.path.exists(manifest_path):
                os.remove(manifest_path)
        except OSError:
            pass


def stitch_recording_session(session_id: str) -> Tuple[int, str]:
    """Stitch a session's chunks into a final audio file.

    Returns ``(recording_id, audio_path)`` on success. Raises
    :class:`StitchError` on any failure; the caller (job_queue worker)
    is responsible for updating the Recording row's status and
    surfacing the error to the user.
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

    _run_ffmpeg_concat(chunk_paths, final_path, session.mime_type)

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

    # Update the recording row in place. We don't change the title - the
    # finalize endpoint set it.
    recording.audio_path = final_path
    recording.original_filename = final_filename
    recording.file_size = file_size
    recording.status = 'PENDING'
    if not recording.meeting_date:
        recording.meeting_date = session.created_at

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

    # Parse the user's finalize metadata so the downstream transcribe job
    # picks up tags, ASR options, hotwords, etc. as if they had been on a
    # regular upload form.
    metadata = {}
    if session.finalize_metadata:
        try:
            metadata = json.loads(session.finalize_metadata) or {}
        except json.JSONDecodeError:
            metadata = {}

    return recording.id, final_path, metadata


def kickoff_transcription_for_stitched(
    recording_id: int,
    user_id: int,
    metadata: dict,
) -> None:
    """Enqueue the downstream transcribe job using the same precedence
    chain as ``upload_file`` for the fields that apply to a recording
    that already exists. Idempotent (the job_queue rejects duplicate
    active jobs of the same type for the same recording)."""
    from src.services.job_queue import job_queue
    job_queue.enqueue(
        user_id=user_id,
        recording_id=recording_id,
        job_type='transcribe',
        params={
            'language': metadata.get('language') or metadata.get('asr_language'),
            'min_speakers': metadata.get('min_speakers'),
            'max_speakers': metadata.get('max_speakers'),
            'hotwords': metadata.get('hotwords'),
            'initial_prompt': metadata.get('initial_prompt'),
            'transcription_model': metadata.get('transcription_model'),
        },
        is_new_upload=True,
    )
