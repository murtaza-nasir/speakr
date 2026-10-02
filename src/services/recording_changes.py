"""Change tracking for recordings: updated_at, tombstones, the changes feed
and ETags (mailr spec G2 and G11).

updated_at is set in one before_flush listener, so no write path can forget
it: a change to a field a client sees, or to a row that belongs to the
recording (tags, events, shares, per-user state), moves it. Fields that only
processing uses (embeddings, timings, resolved settings, export file name) do
not.

Deleting a recording writes a tombstone for the owner and for every user it
was shared with; removing an internal share writes an access_revoked
tombstone for that user. The changes feed reads updated_at and the
tombstones in one order, (changed_at, kind, id), and pages with an opaque
cursor.

Bulk Query.update() bypasses ORM events; tests/test_recording_changes.py fails
if one is added for the recording table.
"""

import base64
import json
import os
from datetime import datetime, timedelta

from sqlalchemy import event, or_, and_, func
from sqlalchemy.orm import Session

from src.database import db

# Fields whose change a client should see.
TRACKED_FIELDS = (
    'title', 'participants', 'notes', 'summary', 'transcription', 'status', 'meeting_date',
    'folder_id', 'is_inbox', 'is_highlighted', 'is_archived', 'audio_deleted_at', 'completed_at',
    'error_message', 'speaker_label_map', 'deletion_exempt', 'prompt_variables',
)

SETTLE_SECONDS = 2
TOMBSTONE_DAYS = int(os.environ.get('RECORDING_TOMBSTONE_DAYS', '90') or 90)
FEED_SINCE_KEY = 'changes_feed_since'
PRUNED_BEFORE_KEY = 'recording_tombstones_pruned_before'

_UPSERT, _DELETE = 0, 1
_MAX_ID = 2 ** 62

_registered = False


def iso_z(value):
    """ISO 8601 with microseconds and a Z suffix, for new fields."""
    if value is None:
        return None
    return value.strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def effective_updated_at(recording):
    return recording.updated_at or recording.created_at


# ---------------------------------------------------------------- listener

def _child_recording_ids(session):
    """Recording ids touched through a row that belongs to a recording."""
    from src.models import Event, InternalShare, RecordingTag, SharedRecordingState
    child_types = (RecordingTag, Event, InternalShare, SharedRecordingState)
    try:
        from src.models import RecordingExternalRef  # G8
        child_types = child_types + (RecordingExternalRef,)
    except ImportError:
        pass
    ids = set()
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(obj, child_types):
            rid = getattr(obj, 'recording_id', None)
            if rid is not None:
                ids.add(rid)
    return ids


def _changed_fields(session, recording):
    from sqlalchemy import inspect as sa_inspect
    state = sa_inspect(recording)
    keys = state.attrs.keys()
    return [name for name in TRACKED_FIELDS if name in keys and state.attrs[name].history.has_changes()]


def _before_flush(session, flush_context, instances):
    from src.models import InternalShare, Recording
    now = datetime.utcnow()
    changes = session.info.setdefault('recording_changes', {})

    for obj in session.new:
        if isinstance(obj, Recording):
            if obj.created_at is None:
                obj.created_at = now
            if obj.updated_at is None:
                obj.updated_at = obj.created_at

    for obj in session.dirty:
        if isinstance(obj, Recording) and session.is_modified(obj, include_collections=False):
            fields = _changed_fields(session, obj)
            if fields:
                obj.updated_at = now
                changes.setdefault(obj.id, set()).update(fields)

    deleted_recordings = {obj.id for obj in session.deleted if isinstance(obj, Recording)}
    with session.no_autoflush:
        for rid in _child_recording_ids(session) - deleted_recordings:
            rec = session.get(Recording, rid)
            if rec is not None and rec not in session.new:
                rec.updated_at = now
                changes.setdefault(rid, set()).add('related')

        for obj in session.deleted:
            if isinstance(obj, Recording):
                _tombstones_for_deleted_recording(session, obj, now)
            elif isinstance(obj, InternalShare) and obj.recording_id not in deleted_recordings:
                _add_tombstone(session, obj.recording_id, obj.shared_with_user_id, now, 'access_revoked')


def _tombstones_for_deleted_recording(session, recording, now):
    from src.models import InternalShare
    reason = session.info.get('tombstone_reason', 'deleted')
    users = set()
    if recording.user_id is not None:
        users.add(recording.user_id)
    for (uid,) in session.query(InternalShare.shared_with_user_id).filter(
            InternalShare.recording_id == recording.id).all():
        users.add(uid)
    for uid in users:
        _add_tombstone(session, recording.id, uid, now, reason)


def _add_tombstone(session, recording_id, user_id, now, reason):
    from src.models import RecordingTombstone
    session.add(RecordingTombstone(recording_id=recording_id, user_id=user_id, deleted_at=now, reason=reason))


def register_change_tracking():
    """Register the listener once, for every session."""
    global _registered
    if _registered:
        return
    event.listen(Session, 'before_flush', _before_flush)
    _registered = True


# ---------------------------------------------------------------- pruning

def prune_tombstones(now=None):
    """Remove tombstones older than RECORDING_TOMBSTONE_DAYS. Returns the count."""
    from src.models import RecordingTombstone, SystemSetting
    now = now or datetime.utcnow()
    horizon = now - timedelta(days=TOMBSTONE_DAYS)
    removed = RecordingTombstone.query.filter(RecordingTombstone.deleted_at < horizon).delete(
        synchronize_session=False)
    db.session.commit()
    if removed:
        SystemSetting.set_setting(PRUNED_BEFORE_KEY, iso_z(horizon),
                                  'Tombstones before this time are pruned (changes feed 410)')
    return removed


# ---------------------------------------------------------------- feed

class CursorError(ValueError):
    pass


def encode_cursor(changed_at, kind, rid, full_since=None):
    data = {'u': iso_z(changed_at), 'k': kind, 'i': rid}
    if full_since is not None:
        data['s'] = iso_z(full_since)
    raw = json.dumps(data, separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _parse_z(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')


def decode_cursor(cursor):
    try:
        raw = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4))
        data = json.loads(raw)
        changed_at = _parse_z(data['u'])
        kind, rid = int(data['k']), int(data['i'])
        full_since = _parse_z(data['s']) if data.get('s') else None
    except Exception as e:
        raise CursorError('cursor could not be decoded') from e
    if kind not in (_UPSERT, _DELETE):
        raise CursorError('cursor could not be decoded')
    return changed_at, kind, rid, full_since


def _after(column, id_column, kind, cursor_key):
    """SQL for (column, kind, id) > cursor_key, for one constant kind."""
    at, ck, ci = cursor_key
    same_time = column == at
    if kind > ck:
        tie = same_time
    elif kind == ck:
        tie = and_(same_time, id_column > ci)
    else:
        tie = False
    return or_(column > at, tie) if tie is not False else column > at


def cursor_expired(user_id, changed_at, full_since):
    """True when deletions the cursor still needs may have been pruned."""
    from src.models import SystemSetting
    needed_from = full_since or changed_at
    pruned = SystemSetting.get_setting(PRUNED_BEFORE_KEY)
    if pruned:
        try:
            if needed_from < _parse_z(pruned):
                return True
        except ValueError:
            pass
    since = SystemSetting.get_setting(FEED_SINCE_KEY)
    if since and full_since is None:
        try:
            # A first answer can put the cursor up to the settle window before
            # the upgrade time; no deletion can be missing from that gap.
            if changed_at < _parse_z(since) - timedelta(seconds=SETTLE_SECONDS + 1):
                return True
        except ValueError:
            pass
    return False


def read_changes(user_id, cursor=None, limit=100, now=None):
    """One page of the feed. Returns (items, next_cursor, has_more); each item
    is ('upsert', Recording) or ('delete', RecordingTombstone).

    Raises CursorError for an undecodable cursor. The caller checks
    cursor_expired first.
    """
    from src.models import Recording, RecordingTombstone
    now = now or datetime.utcnow()
    settle = now - timedelta(seconds=SETTLE_SECONDS)

    if cursor:
        at, kind, rid, full_since = decode_cursor(cursor)
        key = (at, kind, rid)
    else:
        key, full_since = None, now   # a full pass: tombstones only from its start

    changed = func.coalesce(Recording.updated_at, Recording.created_at)
    rq = Recording.query.filter(Recording.user_id == user_id, changed <= settle)
    if key:
        rq = rq.filter(_after(changed, Recording.id, _UPSERT, key))
    recs = rq.order_by(changed.asc(), Recording.id.asc()).limit(limit + 1).all()

    tq = RecordingTombstone.query.filter(RecordingTombstone.user_id == user_id,
                                         RecordingTombstone.deleted_at <= settle)
    if full_since is not None:
        tq = tq.filter(RecordingTombstone.deleted_at >= full_since)
    if key:
        tq = tq.filter(_after(RecordingTombstone.deleted_at, RecordingTombstone.id, _DELETE, key))
    stones = tq.order_by(RecordingTombstone.deleted_at.asc(), RecordingTombstone.id.asc()).limit(limit + 1).all()

    merged = [((effective_updated_at(r), _UPSERT, r.id), 'upsert', r) for r in recs]
    merged += [((t.deleted_at, _DELETE, t.id), 'delete', t) for t in stones]
    merged.sort(key=lambda item: item[0])
    page, has_more = merged[:limit], len(merged) > limit

    if has_more:
        last = page[-1][0]
        next_cursor = encode_cursor(*last, full_since=full_since)
    else:
        # Everything up to the settle point has been returned.
        next_cursor = encode_cursor(settle, _DELETE, _MAX_ID)
    return [(kind, obj) for _, kind, obj in page], next_cursor, has_more


# ---------------------------------------------------------------- ETags

def updated_at_us(recording):
    value = effective_updated_at(recording)
    if value is None:
        return 0
    return int((value - datetime(1970, 1, 1)).total_seconds() * 1_000_000)


def etag_matches(request, etag):
    header = request.headers.get('If-None-Match')
    if not header:
        return False
    if header.strip() == '*':
        return True
    bare = etag[2:] if etag.startswith('W/') else etag
    for candidate in header.split(','):
        candidate = candidate.strip()
        if candidate.startswith('W/'):
            candidate = candidate[2:]
        if candidate == bare:
            return True
    return False


def conditional(request, etag, build):
    """304 when If-None-Match matches etag, else build() with the ETag set.
    Call only after the access check."""
    from flask import make_response
    if etag_matches(request, etag):
        response = make_response('', 304)
    else:
        response = make_response(build())
        if response.status_code != 200:
            return response
    response.headers['ETag'] = etag
    response.headers['Cache-Control'] = 'private, no-cache'
    return response
