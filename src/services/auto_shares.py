"""Shares that a group tag or a group folder created, kept in step with their reason.

A group tag or group folder shares a recording with the group's members
(source_type 'group_tag' or 'group_folder'). Such a share is valid only while
its reason holds: the recipient is a member of a group that has a tag on the
recording, or of the group whose folder holds the recording. When the reason
goes away (the member leaves, the recording leaves the folder, the tag is
removed or deleted, the group is deleted), the share goes with it (#570,
#582). A share the owner created by hand ('manual') is never touched here.

The recipient's SharedRecordingState is kept, as a manual revoke keeps it,
so personal notes survive; a later share reuses the row (#571).
"""

from src.database import db

AUTO_SOURCES = ('group_tag', 'group_folder')


def ensure_recipient_state(recording_id, user_id):
    """The recipient's SharedRecordingState for a new share, reused if it exists.

    A revoked share leaves the row behind (it holds personal notes), and the
    table allows one row per recording and user, so inserting a second one
    made the commit fail (#571). A reused row returns to the inbox, as a new
    manual share does.
    """
    from src.models import SharedRecordingState
    state = SharedRecordingState.query.filter_by(recording_id=recording_id, user_id=user_id).first()
    if state is None:
        state = SharedRecordingState(recording_id=recording_id, user_id=user_id,
                                     is_inbox=True, is_highlighted=False)
        db.session.add(state)
    else:
        state.is_inbox = True
    return state


def _reasons(recording, user_id):
    """[(source_type, source id, is_admin)] that still justify an auto-share."""
    from src.models import Folder, GroupMembership, RecordingTag, Tag
    reasons = []
    rows = db.session.query(Tag.id, GroupMembership.role).join(
        RecordingTag, RecordingTag.tag_id == Tag.id
    ).join(
        GroupMembership, GroupMembership.group_id == Tag.group_id
    ).filter(
        RecordingTag.recording_id == recording.id,
        GroupMembership.user_id == user_id,
        Tag.group_id.isnot(None),
    ).order_by(Tag.id).all()
    reasons.extend(('group_tag', tag_id, role == 'admin') for tag_id, role in rows)
    if recording.folder_id:
        folder = db.session.get(Folder, recording.folder_id)
        if folder is not None and folder.group_id:
            membership = GroupMembership.query.filter_by(group_id=folder.group_id, user_id=user_id).first()
            if membership is not None:
                reasons.append(('group_folder', folder.id, membership.role == 'admin'))
    return reasons


def drop_stale_auto_shares(recording_ids=None, user_ids=None):
    """Remove the auto-shares whose reason is gone; no commit. Returns the count removed.

    Call after the change that may remove a reason (flushes first). Limit the
    check with recording_ids and/or user_ids; pass at least one. A share that
    still has a reason keeps it, with its source moved to a reason that holds
    (the share-reason label) and edit access as the reasons give it: a group
    admin edits, a member reads, the same rule that created it.
    """
    from src.models import InternalShare, Recording
    if recording_ids is None and user_ids is None:
        raise ValueError('drop_stale_auto_shares needs recording_ids or user_ids')
    db.session.flush()
    query = InternalShare.query.filter(InternalShare.source_type.in_(AUTO_SOURCES))
    if recording_ids is not None:
        recording_ids = [rid for rid in set(recording_ids) if rid is not None]
        if not recording_ids:
            return 0
        query = query.filter(InternalShare.recording_id.in_(recording_ids))
    if user_ids is not None:
        user_ids = [uid for uid in set(user_ids) if uid is not None]
        if not user_ids:
            return 0
        query = query.filter(InternalShare.shared_with_user_id.in_(user_ids))

    removed = 0
    for share in query.all():
        recording = db.session.get(Recording, share.recording_id)
        reasons = _reasons(recording, share.shared_with_user_id) if recording is not None else []
        if not reasons:
            db.session.delete(share)  # ORM delete: writes the access_revoked tombstone
            removed += 1
            continue
        current = ('group_tag', share.source_tag_id) if share.source_type == 'group_tag' \
            else ('group_folder', share.source_folder_id)
        if current not in {(kind, source_id) for kind, source_id, _ in reasons}:
            kind, source_id, _ = reasons[0]
            share.source_type = kind
            share.source_tag_id = source_id if kind == 'group_tag' else None
            share.source_folder_id = source_id if kind == 'group_folder' else None
        can_edit = any(is_admin for _, _, is_admin in reasons)
        if bool(share.can_edit) != can_edit:
            share.can_edit = can_edit
    if removed:
        db.session.flush()
    return removed
