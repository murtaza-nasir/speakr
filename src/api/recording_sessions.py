"""Recording-session endpoints for server-side chunk streaming.

Backend half of issue #287 (c)(d): instead of accumulating the entire
audio blob in the browser until the user clicks Upload, the in-app
recorder POSTs MediaRecorder chunks to the server as they are produced.

Endpoints
---------

::

    POST   /upload/session                       create
    POST   /upload/session/<sid>/chunks/<n>      append chunk (n=1..)
    GET    /upload/session/<sid>                 status
    POST   /upload/session/<sid>/finalize        request stitch
    DELETE /upload/session/<sid>                 abort + cleanup

All endpoints require login. They write the on-disk layout described in
``src/models/recording_session.py``.

Finalize is async: the endpoint creates a placeholder Recording row in
``STITCHING`` status, enqueues a ``stitch`` job via the existing
``job_queue``, and returns the recording id immediately. The worker
(see :mod:`src.services.recording_stitch`) runs ``ffmpeg`` concat demux,
moves the resulting file into ``UPLOAD_FOLDER``, and either enqueues a
follow-up ``transcribe`` job on success or marks the recording ``FAILED``.

Limits
------

Per-user storage of in-progress sessions is bounded by
``RECORDING_SESSION_MAX_BYTES_PER_USER`` (default 5 GB). Sessions whose
``last_seen_at`` is older than ``RECORDING_SESSION_TTL_HOURS`` (default
24) are reaped by :func:`cleanup_expired_sessions` from an APScheduler
job. To avoid losing audio from users who disappear mid-recording,
cleanup AUTO-FINALIZES abandoned sessions that have chunks (assembling
what was uploaded into the user's library) and only deletes sessions that
have nothing to recover. See that function for the full policy.
"""

import json
import os
import shutil
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user

from src.database import db
from src.models import RecordingSession, Recording, RECORDING_SESSION_STATUSES
from src.services.job_queue import job_queue


recording_sessions_bp = Blueprint('recording_sessions', __name__)


# --- Config knobs (env-overridable, read fresh each call so tests can patch) ---

def _session_root():
    upload_folder = current_app.config.get('UPLOAD_FOLDER') or '/data/uploads'
    return os.path.join(upload_folder, '_sessions')


def _session_dir(session_id):
    return os.path.join(_session_root(), session_id)


def _max_bytes_per_user():
    return int(os.environ.get('RECORDING_SESSION_MAX_BYTES_PER_USER', str(5 * 1024 * 1024 * 1024)))


def _ttl_hours():
    return int(os.environ.get('RECORDING_SESSION_TTL_HOURS', '24'))


def _allowed_mime_types():
    raw = os.environ.get(
        'RECORDING_SESSION_ALLOWED_MIME_TYPES',
        'audio/webm,audio/ogg,audio/mp4,audio/mpeg,audio/wav,audio/x-m4a',
    )
    return {m.strip().lower() for m in raw.split(',') if m.strip()}


def _max_chunk_bytes():
    """Per-chunk upload size cap. A MediaRecorder timeslice of a few seconds
    yields chunks well under 1 MB even at high bitrates, so 16 MB is a wide
    safety margin that still rejects accidental whole-file uploads."""
    return int(os.environ.get('RECORDING_SESSION_MAX_CHUNK_BYTES', str(16 * 1024 * 1024)))


def _commit_batch_size():
    """How many chunks to accumulate before committing session bookkeeping.

    Default 1 = commit every chunk (the previous behaviour). Higher
    values reduce fsync overhead on slow or network-backed storage at
    the cost of stale chunk_count/bytes_received between commits if
    the process is killed. Finalize and abort always force a commit.
    """
    try:
        return max(1, int(os.environ.get('RECORDING_SESSION_COMMIT_BATCH_SIZE', '1')))
    except (TypeError, ValueError):
        return 1


# --- Helpers ----------------------------------------------------------------

def _ensure_owned(session):
    """Return None if ok, otherwise a Flask response tuple."""
    if not session:
        return jsonify({'error': 'Session not found'}), 404
    if session.user_id != current_user.id:
        # 404 instead of 403 so the existence of other users' sessions is
        # not leaked to the caller.
        return jsonify({'error': 'Session not found'}), 404
    return None


def _user_bytes_in_progress(user_id):
    """Sum of bytes_received across this user's non-terminal sessions."""
    total = db.session.query(db.func.coalesce(db.func.sum(RecordingSession.bytes_received), 0)).filter(
        RecordingSession.user_id == user_id,
        RecordingSession.status.in_(('recording', 'finalizing')),
    ).scalar() or 0
    return int(total)


def _write_session_manifest(session):
    """Write a JSON copy of the session row next to the chunks. This is a
    belt-and-braces recovery aid: if the DB is wiped but the disk survives,
    the manifest tells an operator what was being uploaded."""
    dir_path = _session_dir(session.id)
    os.makedirs(dir_path, exist_ok=True)
    manifest_path = os.path.join(dir_path, 'session.json')
    try:
        with open(manifest_path, 'w') as f:
            json.dump(session.to_dict(), f, indent=2)
    except Exception as e:
        current_app.logger.warning(f"Could not write session manifest for {session.id}: {e}")


def _remove_session_dir(session_id, app=None):
    """Best-effort removal of a session's on-disk chunk directory."""
    logger = (app.logger if app is not None else current_app.logger)
    try:
        dir_path = _session_dir(session_id)
        if os.path.isdir(dir_path):
            shutil.rmtree(dir_path, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Could not remove session dir {session_id}: {e}")


def _finalize_session_into_stitch(session, *, user_id, title, notes=None,
                                  resolved_folder_id=None, metadata=None):
    """Create the placeholder Recording, flip the session to ``finalizing``,
    and enqueue the stitch job.

    This is the single code path used by BOTH the user-initiated finalize
    endpoint and the abandoned-session auto-finalize in cleanup, so the two
    behave identically (same Recording shape, same job, same downstream
    transcription kickoff). Returns ``(recording, enqueue_error)`` where
    ``enqueue_error`` is ``None`` on success.
    """
    recording = Recording(
        user_id=user_id,
        title=(title or 'Recording')[:200],
        status='STITCHING',
        notes=notes,
        folder_id=resolved_folder_id,
        processing_source='recording_session',
        mime_type=session.mime_type,
        # Set meeting_date up front (= when recording started) so the
        # sidebar sorts it into the right position the instant it appears,
        # instead of sorting as null until the stitch worker fills it in
        # (which left newly-finalized recordings at the bottom of "Today").
        meeting_date=session.created_at,
    )
    db.session.add(recording)
    db.session.flush()

    session.status = 'finalizing'
    session.finalize_metadata = json.dumps(metadata or {})
    session.finalized_recording_id = recording.id
    session.last_seen_at = datetime.utcnow()
    db.session.commit()

    try:
        job_queue.enqueue(
            user_id=user_id,
            recording_id=recording.id,
            job_type='stitch',
            params={'session_id': session.id},
            is_new_upload=True,
        )
        return recording, None
    except Exception as e:
        current_app.logger.error(f"Could not enqueue stitch job for session {session.id}: {e}")
        return recording, e


# --- Endpoints --------------------------------------------------------------

@recording_sessions_bp.route('/upload/session', methods=['POST'])
@login_required
def create_session():
    """Create a new in-progress recording session.

    Body (JSON, all optional): {"mime_type": "audio/webm"}.

    Returns ``{session_id, expires_at, max_chunk_bytes}`` on success.
    """
    data = request.get_json(silent=True) or {}
    mime_type = (data.get('mime_type') or 'audio/webm').strip().lower()
    if mime_type not in _allowed_mime_types():
        return jsonify({
            'error': f'Unsupported mime_type {mime_type!r}',
            'allowed': sorted(_allowed_mime_types()),
        }), 400

    # Per-user quota check before we let them open another session.
    if _user_bytes_in_progress(current_user.id) >= _max_bytes_per_user():
        return jsonify({
            'error': 'Per-user in-progress recording storage quota exhausted',
            'quota_bytes': _max_bytes_per_user(),
        }), 507  # Insufficient Storage

    session = RecordingSession(
        user_id=current_user.id,
        mime_type=mime_type,
        status='recording',
    )
    db.session.add(session)
    db.session.commit()

    try:
        os.makedirs(_session_dir(session.id), exist_ok=True)
        _write_session_manifest(session)
    except OSError as e:
        # Roll back so we don't leak a half-created session row.
        db.session.delete(session)
        db.session.commit()
        current_app.logger.error(f"Could not create session dir: {e}")
        return jsonify({'error': 'Server could not allocate session storage'}), 500

    expires_at = session.created_at + timedelta(hours=_ttl_hours())
    return jsonify({
        'session_id': session.id,
        'mime_type': session.mime_type,
        'status': session.status,
        'expires_at': expires_at.isoformat(),
        'max_chunk_bytes': _max_chunk_bytes(),
    }), 201


@recording_sessions_bp.route('/upload/session/<string:session_id>', methods=['GET'])
@login_required
def get_session(session_id):
    """Return the current status of an in-progress session."""
    session = db.session.get(RecordingSession, session_id)
    err = _ensure_owned(session)
    if err is not None:
        return err
    return jsonify(session.to_dict())


@recording_sessions_bp.route('/upload/session/<string:session_id>/chunks/<int:chunk_index>', methods=['POST'])
@login_required
def upload_chunk(session_id, chunk_index):
    """Append one chunk to a session.

    The chunk index must equal ``session.chunk_count + 1`` (strict in-order
    uploads). If it does not, the response is 409 with the expected next
    index, so the client can resync without losing the recording.

    The chunk body is read from ``request.data`` (raw bytes) or from a file
    field named ``chunk`` (multipart). The raw-bytes path is preferred by
    the streaming client because it avoids the multipart framing overhead
    on every short request.
    """
    session = db.session.get(RecordingSession, session_id)
    err = _ensure_owned(session)
    if err is not None:
        return err

    if session.status != 'recording':
        return jsonify({
            'error': f'Session is in status {session.status!r}; chunks are no longer accepted',
        }), 409

    expected_index = session.chunk_count + 1
    if chunk_index != expected_index:
        return jsonify({
            'error': 'Out-of-order chunk',
            'expected_chunk_index': expected_index,
            'got': chunk_index,
        }), 409

    # Accept either raw body or multipart upload.
    chunk_bytes = None
    if 'chunk' in request.files:
        f = request.files['chunk']
        chunk_bytes = f.read()
    else:
        chunk_bytes = request.get_data(cache=False)

    if not chunk_bytes:
        return jsonify({'error': 'Empty chunk body'}), 400

    if len(chunk_bytes) > _max_chunk_bytes():
        return jsonify({
            'error': 'Chunk exceeds max_chunk_bytes',
            'max_chunk_bytes': _max_chunk_bytes(),
            'got': len(chunk_bytes),
        }), 413

    # Per-user quota check: the new chunk must not push the user over.
    # NOTE: this is a best-effort (soft) check. Concurrent chunks
    # arriving on different sessions can both pass the projection and
    # together exceed the cap by up to N * max_chunk_bytes where N is
    # the worker count. SQLite doesn't support row-level locking, and
    # cross-process locking would need Redis/Postgres advisory locks.
    # The overrun is small (16 MB per concurrent chunk by default) and
    # bounded by the session count, so we treat the quota as a
    # soft limit and accept the race.
    projected = _user_bytes_in_progress(current_user.id) - session.bytes_received + session.bytes_received + len(chunk_bytes)
    if projected > _max_bytes_per_user():
        return jsonify({
            'error': 'Per-user in-progress recording storage quota would be exceeded',
            'quota_bytes': _max_bytes_per_user(),
        }), 507

    dir_path = _session_dir(session.id)
    os.makedirs(dir_path, exist_ok=True)
    chunk_path = os.path.join(dir_path, f'chunk-{chunk_index:06d}.bin')
    try:
        with open(chunk_path, 'wb') as f:
            f.write(chunk_bytes)
    except OSError as e:
        current_app.logger.error(f"Could not write chunk {chunk_index} for session {session_id}: {e}")
        return jsonify({'error': 'Server could not write chunk'}), 500

    session.chunk_count = chunk_index
    session.bytes_received = (session.bytes_received or 0) + len(chunk_bytes)
    session.last_seen_at = datetime.utcnow()
    # Commit every chunk by default. Opt-in batching is available via
    # RECORDING_SESSION_COMMIT_BATCH_SIZE for deployments on slow or
    # network-backed storage where per-chunk fsync is the bottleneck.
    # The chunk file itself is already written to disk above; what
    # batching skips is the in-row bookkeeping commit. If the process
    # restarts between commits, chunk_count and bytes_received fall
    # back to the last committed value, but the chunk files survive
    # and finalize re-derives the count from disk (see _stitch path).
    batch_size = _commit_batch_size()
    if batch_size <= 1 or (chunk_index % batch_size) == 0:
        db.session.commit()
    else:
        db.session.flush()

    # Manifest is best-effort; missing it doesn't break the flow.
    _write_session_manifest(session)

    return ('', 204)


@recording_sessions_bp.route('/upload/session/<string:session_id>/finalize', methods=['POST'])
@login_required
def finalize_session(session_id):
    """Request asynchronous stitch and transcription kickoff.

    Body (JSON, all optional): metadata to attach to the resulting
    Recording row: ``{title, notes, tags, folder_id, asr_options,
    transcription_model, hotwords, initial_prompt, prompt_variables}``.

    Returns ``{recording_id, status}`` immediately. The actual stitch
    happens off-request via the job queue; clients can poll
    ``GET /api/v1/recordings/{id}/status`` to follow progress.
    """
    session = db.session.get(RecordingSession, session_id)
    err = _ensure_owned(session)
    if err is not None:
        return err

    if session.status not in ('recording', 'finalizing'):
        return jsonify({
            'error': f'Session is in status {session.status!r}; cannot finalize',
        }), 409

    if session.chunk_count <= 0:
        return jsonify({'error': 'No chunks uploaded yet'}), 409

    metadata = request.get_json(silent=True) or {}
    if not isinstance(metadata, dict):
        return jsonify({'error': 'finalize body must be a JSON object'}), 400

    # Create a placeholder Recording row. The stitch worker will fill in
    # audio_path and file_size after concat completes.
    title = (metadata.get('title') or f"Recording {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}").strip() or "Recording"
    notes = metadata.get('notes') or None
    folder_id = metadata.get('folder_id')
    if folder_id in ('', 'none', 'null'):
        folder_id = None

    # Authorize folder access (#287 c/d, mirrors update_recording in
    # src/api/api_v1.py). Without this check, an attacker could finalize
    # a session targeting another user's folder, and the resulting
    # recording would be linked to it. Personal folders require
    # ownership; group folders require membership.
    resolved_folder_id = None
    if isinstance(folder_id, int):
        from src.models.organization import Folder, GroupMembership
        target = db.session.get(Folder, folder_id)
        if not target:
            return jsonify({'error': f'Folder {folder_id} not found'}), 404
        if target.group_id is None:
            if target.user_id != current_user.id:
                return jsonify({'error': 'No access to target folder'}), 403
        else:
            membership = GroupMembership.query.filter_by(
                user_id=current_user.id, group_id=target.group_id
            ).first()
            if not membership:
                return jsonify({'error': 'No access to target folder'}), 403
        resolved_folder_id = target.id

    # Create the placeholder Recording, flip the session to finalizing, and
    # enqueue the async stitch job — shared with the cleanup auto-finalize
    # path so both behave identically. The worker dispatches to _run_stitch
    # which assembles the audio, writes it into UPLOAD_FOLDER, and enqueues a
    # downstream 'transcribe' job.
    recording, enqueue_error = _finalize_session_into_stitch(
        session,
        user_id=current_user.id,
        title=title,
        notes=notes,
        resolved_folder_id=resolved_folder_id,
        metadata=metadata,
    )
    if enqueue_error is not None:
        # Leave the session in `finalizing` so the client can retry; the
        # placeholder recording stays in STITCHING so the user can see it.
        return jsonify({
            'recording_id': recording.id,
            'status': session.status,
            'queue_error': str(enqueue_error),
        }), 500

    return jsonify({
        'recording_id': recording.id,
        'status': session.status,
    }), 202


@recording_sessions_bp.route('/upload/session/<string:session_id>', methods=['DELETE'])
@login_required
def abort_session(session_id):
    """Abort an in-progress session and clean up its on-disk chunks.

    If the session has already finalized, this returns 409; finalized
    sessions are owned by the stitch worker, not the user.
    """
    session = db.session.get(RecordingSession, session_id)
    err = _ensure_owned(session)
    if err is not None:
        return err

    if session.status == 'finalized':
        return jsonify({'error': 'Session already finalized'}), 409

    session.status = 'aborted'
    session.last_seen_at = datetime.utcnow()
    db.session.commit()

    # Best-effort directory cleanup; if the dir is already gone or the FS
    # complains, the periodic cleanup job will catch it.
    _remove_session_dir(session_id)

    return ('', 204)


# --- Cleanup hook -----------------------------------------------------------

def cleanup_expired_sessions(app=None):
    """Reap sessions whose ``last_seen_at`` is older than the TTL.

    Called from APScheduler. The policy prioritises NOT losing a user's
    audio when they disappear mid-recording (crash, closed laptop, dropped
    connection):

    - ``recording`` with chunks on the books → **auto-finalize**: assemble
      what was uploaded and land it in the user's library (same path as a
      manual finalize), rather than discarding it.
    - ``recording`` with no chunks → expire and delete the empty directory.
    - ``finalizing`` (already has a placeholder recording) but never reached
      a terminal stitch state → **re-enqueue** the stitch to unstick it
      (enqueue dedupes an already-active job). Bumping ``last_seen_at`` here
      naturally rate-limits this to once per TTL window.

    Returns the number of sessions acted on.
    """
    if app is None:
        from flask import current_app as _ca
        app = _ca._get_current_object()

    with app.app_context():
        cutoff = datetime.utcnow() - timedelta(hours=_ttl_hours())
        candidates = RecordingSession.query.filter(
            RecordingSession.last_seen_at < cutoff,
            RecordingSession.status.in_(('recording', 'finalizing')),
        ).all()

        finalized = rekicked = expired = 0
        for s in candidates:
            try:
                if s.status == 'recording' and (s.chunk_count or 0) > 0:
                    when = s.created_at.strftime('%Y-%m-%d %H:%M') if s.created_at else ''
                    title = f"Recovered recording {when}".strip()
                    _finalize_session_into_stitch(
                        s, user_id=s.user_id, title=title,
                        metadata={'recovered': True},
                    )
                    finalized += 1
                    app.logger.info(
                        f"Auto-finalized abandoned recording session {s.id} "
                        f"({s.chunk_count} chunks) for user {s.user_id}"
                    )
                elif s.status == 'finalizing' and s.finalized_recording_id:
                    s.last_seen_at = datetime.utcnow()
                    db.session.commit()
                    try:
                        job_queue.enqueue(
                            user_id=s.user_id,
                            recording_id=s.finalized_recording_id,
                            job_type='stitch',
                            params={'session_id': s.id},
                            is_new_upload=True,
                        )
                        rekicked += 1
                        app.logger.info(f"Re-enqueued stitch for stalled session {s.id}")
                    except Exception as e:
                        app.logger.warning(f"Could not re-enqueue stitch for {s.id}: {e}")
                else:
                    # No chunks to save (or finalizing without a recording):
                    # nothing recoverable — expire and remove the directory.
                    s.status = 'expired'
                    db.session.commit()
                    _remove_session_dir(s.id, app)
                    expired += 1
            except Exception as e:
                app.logger.error(f"Cleanup failed for session {s.id}: {e}", exc_info=True)
                db.session.rollback()

        total = finalized + rekicked + expired
        if total:
            app.logger.info(
                f"Recording-session cleanup: {finalized} auto-finalized, "
                f"{rekicked} re-enqueued, {expired} expired"
            )
        return total
