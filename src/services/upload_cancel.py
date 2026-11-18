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

A retry the browser sends while the first request is still at work has the
same client_upload_id (#626). Before anything is staged, the request claims
the row (claim_upload): an INSERT that the unique constraint lets through
once, or else an UPDATE that only succeeds while no other request holds a
fresh claim. Both are single statements, so only one request holds the
claim, on SQLite and PostgreSQL and across workers. The other request waits
a few seconds for the recording of the first and is answered with it, or
with 409 upload_in_progress. The claim goes when the recording is
registered or the request ends without one; a claim left by a request that
died expires after CLAIM_STALE.
"""

import logging
import re
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, insert, or_, update
from sqlalchemy.exc import IntegrityError

from src.database import db
from src.models import ClientUpload, Recording

logger = logging.getLogger(__name__)

# How long after its upload a recording can be discarded by a cancel.
CANCEL_WINDOW = timedelta(minutes=15)
ROW_TTL = timedelta(hours=24)
# A claim without a recording this old was left by a request that died.
CLAIM_STALE = timedelta(minutes=15)
# How long a second request waits for the recording of the first, and how
# often it looks.
CLAIM_WAIT_SECONDS = 4.0
CLAIM_POLL_SECONDS = 0.25

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


def _stored_recording(row, user_id):
    """The recording the row points to, while it still exists and is this upload's."""
    if row is None or not row.recording_id:
        return None
    recording = db.session.get(Recording, row.recording_id)
    if (recording is None or recording.user_id != user_id
            or recording.created_at != row.recording_created_at):
        return None
    return recording


def already_stored_upload(user_id, client_upload_id):
    """The recording an earlier request of this upload stored, or None.

    The browser sends the same client_upload_id again when it retries an
    upload whose answer was lost (#626). The retry is answered with that
    recording, within ROW_TTL like the idempotency_key of API v1, unless the
    upload was canceled or the recording has been deleted since.
    """
    cutoff = datetime.utcnow() - ROW_TTL
    row = ClientUpload.query.filter_by(user_id=user_id, client_upload_id=client_upload_id).first()
    if row is None or row.canceled_at is not None or row.created_at < cutoff:
        return None
    return _stored_recording(row, user_id)


def _take_claim(user_id, client_upload_id):
    """Claim the row of this upload for the calling request. Commits.

    Returns the claim's timestamp, which identifies it on release, or None
    when another request holds a claim that has not gone stale.
    """
    table = ClientUpload.__table__
    now = datetime.utcnow()
    mine = (table.c.user_id == user_id) & (table.c.client_upload_id == client_upload_id)
    # A row past ROW_TTL is forgotten, as register_recording does for all.
    db.session.execute(delete(table).where(mine, table.c.created_at < now - ROW_TTL))
    db.session.commit()
    try:
        db.session.execute(insert(table).values(
            user_id=user_id, client_upload_id=client_upload_id, created_at=now, claimed_at=now))
        db.session.commit()
        return now
    except IntegrityError:
        # The row exists: written by a cancel, an earlier request or the
        # other request at this moment.
        db.session.rollback()
    result = db.session.execute(
        update(table)
        .where(mine, or_(table.c.claimed_at.is_(None), table.c.claimed_at < now - CLAIM_STALE))
        .values(claimed_at=now))
    db.session.commit()
    return now if result.rowcount == 1 else None


def release_claim(user_id, client_upload_id, claim):
    """Give up a claim of claim_upload, unless another request took it over since."""
    if claim is None:
        return
    table = ClientUpload.__table__
    try:
        db.session.execute(
            update(table)
            .where(table.c.user_id == user_id, table.c.client_upload_id == client_upload_id,
                   table.c.claimed_at == claim)
            .values(claimed_at=None))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning(f"Could not release the claim of an upload of user {user_id}: {type(e).__name__}")


def claim_upload(user_id, client_upload_id):
    """Reserve this upload for the calling request before anything is staged.

    Returns one of:
    - ('claimed', claim): this request stores the upload; it passes claim
      to release_claim when it ends.
    - ('replay', recording): a request of this upload stored this recording.
    - ('canceled', None): the upload was canceled while another request
      held it.
    - ('in_progress', None): another request still works on it after
      CLAIM_WAIT_SECONDS.
    """
    deadline = time.monotonic() + CLAIM_WAIT_SECONDS
    while True:
        recording = already_stored_upload(user_id, client_upload_id)
        if recording is not None:
            return 'replay', recording
        claim = _take_claim(user_id, client_upload_id)
        if claim is not None:
            # The other request may have registered its recording and let
            # the claim go between the look above and the claim.
            recording = already_stored_upload(user_id, client_upload_id)
            if recording is not None:
                release_claim(user_id, client_upload_id, claim)
                return 'replay', recording
            return 'claimed', claim
        if is_canceled(user_id, client_upload_id):
            return 'canceled', None
        if time.monotonic() >= deadline:
            return 'in_progress', None
        time.sleep(CLAIM_POLL_SECONDS)
        db.session.expire_all()


def register_recording(user_id, client_upload_id, recording):
    """Note the recording this upload stored. True when the upload was canceled.

    A row that already points to a recording of this upload keeps it, so a
    second copy cannot take the cancel away from the first. The claim of
    claim_upload goes with the same write.
    """
    try:
        cutoff = datetime.utcnow() - ROW_TTL
        ClientUpload.query.filter(ClientUpload.created_at < cutoff).delete(synchronize_session=False)
        row = ClientUpload.query.filter_by(user_id=user_id, client_upload_id=client_upload_id).first()
        if _stored_recording(row, user_id) is not None:
            return row.canceled_at is not None
        recording_id, created_at = recording.id, recording.created_at
        row = _write(user_id, client_upload_id, recording_id=recording_id,
                     recording_created_at=created_at, claimed_at=None)
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
        from src.services.recording_deletion import forget_recording_sessions
        forget_recording_sessions(recording_id)
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
    recording = _stored_recording(row, user.id)
    if recording is None:
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
