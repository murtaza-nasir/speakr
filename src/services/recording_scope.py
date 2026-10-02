"""Which recordings an API v1 list covers: own, shared with the caller, or
both (mailr spec G4).

Shared means an InternalShare for the caller (manual, group tag and group
folder shares all create one) on a completed recording, as in the web app's
shared-with-me list. With internal sharing off, shared is empty and all
equals own.
"""

import os

from sqlalchemy import and_, case, func, or_

from src.database import db

SCOPES = ('own', 'shared', 'all')


def sharing_enabled():
    from src import app as app_module
    return bool(getattr(app_module, 'ENABLE_INTERNAL_SHARING', False))


def _show_usernames():
    return os.environ.get('SHOW_USERNAMES_IN_UI', 'false').lower() == 'true'


def scope_condition(user_id, scope):
    """SQL condition on Recording for the scope."""
    from src.models import InternalShare, Recording
    own = Recording.user_id == user_id
    if scope == 'own' or not sharing_enabled():
        return own if scope != 'shared' else Recording.id.is_(None)
    shared = and_(
        Recording.id.in_(db.session.query(InternalShare.recording_id)
                         .filter(InternalShare.shared_with_user_id == user_id)),
        Recording.user_id != user_id,
        Recording.status == 'COMPLETED',
    )
    return shared if scope == 'shared' else or_(own, shared)


def personal_flag(user_id, column_name, default):
    """The caller's value of is_inbox / is_highlighted / is_archived: the
    recording's own for the owner, SharedRecordingState for a recipient.
    Use with an outer join on SharedRecordingState for this user."""
    from src.models import Recording, SharedRecordingState
    return case((Recording.user_id == user_id, func.coalesce(getattr(Recording, column_name), default)),
                else_=func.coalesce(getattr(SharedRecordingState, column_name), default))


def join_personal_state(query, user_id):
    from src.models import Recording, SharedRecordingState
    return query.outerjoin(SharedRecordingState, and_(SharedRecordingState.recording_id == Recording.id,
                                                      SharedRecordingState.user_id == user_id))


def share_details(recordings, user):
    """{recording id: dict of is_shared, owner, share, flags, tags} for the
    recordings of one page that are shared with user."""
    from src.models import InternalShare, SharedRecordingState, User
    shared_ids = [r.id for r in recordings if r.user_id != user.id]
    if not shared_ids:
        return {}
    shares = {s.recording_id: s for s in InternalShare.query.filter(
        InternalShare.recording_id.in_(shared_ids), InternalShare.shared_with_user_id == user.id)}
    states = {s.recording_id: s for s in SharedRecordingState.query.filter(
        SharedRecordingState.recording_id.in_(shared_ids), SharedRecordingState.user_id == user.id)}
    owners = {u.id: u for u in User.query.filter(User.id.in_({r.user_id for r in recordings if r.id in shares}))}
    show = _show_usernames()
    details = {}
    for r in recordings:
        share = shares.get(r.id)
        if share is None:
            continue
        owner = owners.get(r.user_id)
        state = states.get(r.id)
        details[r.id] = {
            'is_shared': True,
            'owner': {'id': r.user_id,
                      'username': owner.username if (owner and show) else None,
                      'name': owner.name if (owner and show) else None},
            'share': {'id': share.id, 'can_edit': bool(share.can_edit), 'can_reshare': bool(share.can_reshare),
                      'source': share.source_type or 'manual',
                      'shared_at': share.created_at.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if share.created_at else None},
            'is_inbox': bool(state.is_inbox) if state and state.is_inbox is not None else True,
            'is_highlighted': bool(state.is_highlighted) if state else False,
            'is_archived': bool(state.is_archived) if state and state.is_archived is not None else False,
            'tags': [{'id': t.id, 'name': t.name, 'color': t.color} for t in r.get_visible_tags(user)],
        }
    return details


OWN_ITEM = {'is_shared': False, 'owner': None, 'share': None}
