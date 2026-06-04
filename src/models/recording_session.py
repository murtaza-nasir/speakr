"""RecordingSession model for server-side in-progress recording chunks.

Implements the backend half of issue #287 (c)(d): instead of accumulating
the entire audio blob in browser RAM until the user clicks Upload, the
in-app recorder POSTs MediaRecorder chunks to the server as they are
produced. This model tracks the open session: a session id, the user who
owns it, the chunks received so far, and the status as it transitions
from `recording` → `finalizing` → `finalized` (with `aborted` and
`expired` as terminal failure states).

The on-disk layout for a session is:

    {UPLOAD_FOLDER}/_sessions/<session_id>/
        session.json           ← redundant copy of the columns for crash recovery
        chunk-000001.webm
        chunk-000002.webm
        ...

On finalize, the chunks are stitched into a single file via ffmpeg concat
demux (handles pause/resume across MediaRecorder restarts, which a raw
cat would corrupt). The resulting file becomes the new Recording's
audio_path and the session directory is removed.
"""

import uuid
from datetime import datetime

from src.database import db


# Status constants kept as module-level strings so callers can reference
# them by name. SQLAlchemy doesn't enforce the enum, so all reads / writes
# go through these constants.
RECORDING_SESSION_STATUSES = (
    'recording',     # client is actively POSTing chunks
    'finalizing',    # finalize requested; stitch job is queued or running
    'finalized',     # stitch completed; session dir may already be removed
    'aborted',       # user (or admin) cancelled before finalize
    'expired',       # TTL elapsed without finalize; cleanup ran
    'failed',        # stitch job hit a permanent error
)


class RecordingSession(db.Model):
    """An in-progress browser-recorder session with chunks on the server."""

    __tablename__ = 'recording_session'

    # Use a UUID4 string as the primary key. Predictable integer ids would
    # let an attacker who guesses an id POST chunks into another user's
    # session before the ownership check fires; UUID4 makes that infeasible.
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    user_id = db.Column(
        db.Integer,
        db.ForeignKey('user.id', ondelete='CASCADE'),
        nullable=False,
        index=True,
    )

    # The MIME type the client declared when creating the session. Stored so
    # we can validate per-chunk uploads (reject anything inconsistent with
    # the declared type) and pick the right ffmpeg input args at stitch time.
    mime_type = db.Column(db.String(100), nullable=False, default='audio/webm')

    # Status transitions are guarded by the API; see RECORDING_SESSION_STATUSES.
    status = db.Column(db.String(20), nullable=False, default='recording', index=True)

    # Monotonic counter advanced on every successful chunk POST. The next
    # expected client-supplied chunk index equals `chunk_count + 1`. Out-of-
    # order POSTs are rejected with a 409 telling the client the expected
    # next index so it can resync.
    chunk_count = db.Column(db.Integer, nullable=False, default=0)

    # Cumulative bytes received across all chunks. Used for the per-user
    # storage quota check and to populate Recording.file_size on finalize.
    bytes_received = db.Column(db.BigInteger, nullable=False, default=0)

    # Set when finalize succeeds; lets the recording detail view link back
    # to the session that produced it for audit purposes.
    finalized_recording_id = db.Column(
        db.Integer,
        db.ForeignKey('recording.id', ondelete='SET NULL'),
        nullable=True,
        index=True,
    )

    # Optional user-supplied finalize metadata (notes, tags, folder, ASR
    # options, prompt variables). Stored as JSON text so the stitch worker
    # can read it without an additional table.
    finalize_metadata = db.Column(db.Text, nullable=True)

    # Optional error message when status == 'failed'.
    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_seen_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    finalized_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship(
        'User',
        backref=db.backref('recording_sessions', lazy='dynamic', cascade='all, delete-orphan'),
    )
    finalized_recording = db.relationship('Recording', foreign_keys=[finalized_recording_id])

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'<RecordingSession {self.id} user={self.user_id} status={self.status} chunks={self.chunk_count}>'

    def to_dict(self):
        """Public view of the session for API responses."""
        return {
            'session_id': self.id,
            'status': self.status,
            'mime_type': self.mime_type,
            'chunk_count': self.chunk_count,
            'bytes_received': self.bytes_received,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'last_seen_at': self.last_seen_at.isoformat() if self.last_seen_at else None,
            'finalized_at': self.finalized_at.isoformat() if self.finalized_at else None,
            'finalized_recording_id': self.finalized_recording_id,
            'error_message': self.error_message,
        }
