"""Cancel of an upload the browser aborted.

An aborted request leaves nothing behind while its body is still arriving:
the multipart parser's temporary file goes with the request, and no staging
file or recording exists yet. Once the body has arrived the server no longer
notices the abort: it converts and stores the file and creates the
recording. For that part the browser sends the upload's client_upload_id to
POST /upload/cancel, and both sides meet in the client_upload table:

- The ingestion stores the recording, then writes the recording's id on the
  row and reads the cancel mark. A marked upload is discarded there: media
  and row are deleted, no job is queued and no webhook is sent.
- The cancel writes the mark, then reads the recording id. A recording the
  ingestion has already stored is deleted like any recording.

Each side writes before it reads, so whichever comes second sees the other.
"""

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError

from src.database import db
from src.models import ClientUpload, Recording

logger = logging.getLogger(__name__)

# How long after its upload a recording can be discarded by a cancel.
CANCEL_WINDOW = timedelta(minutes=15)
ROW_TTL = timedelta(hours=24)

_ID_RE = re.compile(r'^[A-Za-z0-9_-]{8,80}$')


def client_upload_id_from(form):
    """The client_upload_id of an upload form, or None when it has none or an invalid one."""
    try:
        value = form.get('client_upload_id')
    except Exception:
        return None
    if isinstance(value, str) and _ID_RE.match(value):
        return value
    return None


def _write(user_id, client_upload_id, **values):
    """Create or update the row of this upload; returns it. Commits."""
    for _ in range(2):
        row = ClientUpload.query.filter_by(user_id=user_id, client_upload_id=client_upload_id).first()
        if row is None:
            row = ClientUpload(user_id=user_id, client_upload_id=client_upload_id)
            db.session.add(row)
        for key, value in values.items():
            setattr(row, key, value)
        try:
            db.session.commit()
            return row
        except IntegrityError:
            # The other side created the row at the same moment.
            db.session.rollback()
    return ClientUpload.query.filter_by(user_id=user_id, client_upload_id=client_upload_id).first()


def is_canceled(user_id, client_upload_id):
    """Whether this upload was canceled already.

    One read on a connection of its own, so the session of the ingestion
    that asks is left as it is.
    """
    try:
        from sqlalchemy import select
        with db.engine.connect() as conn:
            row = conn.execute(
                select(ClientUpload.canceled_at).where(
                    ClientUpload.user_id == user_id,
                    ClientUpload.client_upload_id == client_upload_id)
            ).first()
        return bool(row and row[0] is not None)
    except Exception as e:
        logger.warning(f"Could not read the cancel mark of an upload of user {user_id}: {type(e).__name__}")
        return False


def register_recording(user_id, client_upload_id, recording):
    """Note the recording this upload stored. True when the upload was canceled."""
    try:
        cutoff = datetime.utcnow() - ROW_TTL
        ClientUpload.query.filter(ClientUpload.created_at < cutoff).delete(synchronize_session=False)
        recording_id, created_at = recording.id, recording.created_at
        row = _write(user_id, client_upload_id, recording_id=recording_id, recording_created_at=created_at)
        return bool(row is not None and row.canceled_at is not None)
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Could not note the recording of an upload of user {user_id}: {type(e).__name__}")
        return False


def discard_unannounced_recording(recording, storage):
    """Delete a recording that was stored a moment ago and never announced.

    No job was queued and no recording.created webhook was sent, so the
    media and the row are all there is.
    """
    recording_id, locator = recording.id, recording.audio_path
    if locator:
        try:
            storage.delete(locator, missing_ok=True)
        except Exception as e:
            logger.warning(f"Could not delete the media of canceled upload {recording_id}: {type(e).__name__}")
    recording = db.session.get(Recording, recording_id)
    if recording is not None:
        db.session.delete(recording)
        db.session.commit()
    logger.info(f"Upload canceled: recording {recording_id} discarded before its first job")


def cancel_upload(user, client_upload_id):
    """Mark an upload as canceled and delete the recording it has stored, if any.

    Returns {'recording_deleted': bool, 'recording_kept': bool}. A recording
    is deleted only when this upload stored it within CANCEL_WINDOW and it
    is not COMPLETED: a recording that finished is kept in the library
    (recording_kept), where its owner decides.
    """
    row = _write(user.id, client_upload_id, canceled_at=datetime.utcnow())
    result = {'recording_deleted': False, 'recording_kept': False}
    if row is None or not row.recording_id:
        return result
    recording = db.session.get(Recording, row.recording_id)
    if (recording is None or recording.user_id != user.id
            or recording.created_at != row.recording_created_at):
        return result
    if recording.status == 'COMPLETED' or datetime.utcnow() - row.created_at > CANCEL_WINDOW:
        result['recording_kept'] = True
        return result
    recording_id = recording.id
    from src.services.recording_deletion import delete_recording_completely
    delete_recording_completely(recording, audit_reason='upload_canceled')
    logger.info(f"Upload canceled by user {user.id}: recording {recording_id} deleted")
    result['recording_deleted'] = True
    return result
