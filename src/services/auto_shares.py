"""Shares that a group tag or a group folder created, kept in step with their reason.

A group tag or group folder shares a recording with the group's members
(source_type 'group_tag' or 'group_folder'). Such a share is valid only while
its reason holds: the recipient is a member of a group that has a tag on the
recording, or of the group whose folder holds the recording, and the share
settings of that tag or folder include the recipient (auto-share on apply:
every member; share with group leads only: the group's admins). When the
reason goes away (the member leaves, the recording leaves the folder, the
tag is removed or deleted, the group is deleted, the share settings narrow),
the share goes with it (#570, #582). A share the owner created by hand
('manual') is never touched here.

A change of a tag's or folder's group or share settings goes through
sync_source_shares, from the web routes and from API v1 alike.

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


def _settings_include(source, role):
    """Whether the share settings of a group tag or folder include a member with this role."""
    return bool(source.auto_share_on_apply) or (bool(source.share_with_group_lead) and role == 'admin')


def _reasons(recording, user_id):
    """[(source_type, source id, is_admin)] that still justify an auto-share."""
    from sqlalchemy import and_, or_
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
        or_(Tag.auto_share_on_apply.is_(True),
            and_(Tag.share_with_group_lead.is_(True), GroupMembership.role == 'admin')),
    ).order_by(Tag.id).all()
    reasons.extend(('group_tag', tag_id, role == 'admin') for tag_id, role in rows)
    if recording.folder_id:
        folder = db.session.get(Folder, recording.folder_id)
        if folder is not None and folder.group_id:
            membership = GroupMembership.query.filter_by(group_id=folder.group_id, user_id=user_id).first()
            if membership is not None and _settings_include(folder, membership.role):
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


def remove_unfounded_auto_shares(logger=None):
    """Delete every auto-share without a reason; commits. Returns the count.

    One-shot cleanup (migration 0007) of the shares left by member removal,
    folder unlinks, folder and tag deletion and group deletion before #570
    and #582. Only deletes: shares that still have a reason keep their source
    and edit access. Running it again finds nothing to delete.
    """
    from src.models import InternalShare, Recording
    removed = 0
    for share in InternalShare.query.filter(InternalShare.source_type.in_(AUTO_SOURCES)).all():
        recording = db.session.get(Recording, share.recording_id)
        if recording is None or not _reasons(recording, share.shared_with_user_id):
            db.session.delete(share)
            removed += 1
    db.session.commit()
    if removed and logger is not None:
        logger.info(f"Removed {removed} group tag or folder share(s) whose group, tag or folder no longer gives them")
    return removed


# --- A change of a tag's or folder's group or share settings ---------------

def sharing_state(source):
    """What decides whom a tag or folder shares with: (group id, auto-share, leads only).

    Taken before a change and passed to sync_source_shares. The group id is
    compared as text, so a group id sent as "3" matches 3.
    """
    group_id = source.group_id or None
    return (str(group_id) if group_id is not None else None,
            bool(source.auto_share_on_apply), bool(source.share_with_group_lead))


def regroup_error(source, new_group_id, user, has_recording_access):
    """Why user may not move a tag or folder into group new_group_id, as an error key, or None.

    Only an admin of the new group may make a tag or folder part of it. A tag
    already on recordings puts a group tag on each, so each must allow that
    by the rule for applying a group tag (#578, #579). A folder shares the
    recordings in it, so another user's recording in it needs the caller's
    edit access, as a move into a group folder does (#515). The check comes
    before the change: after it, the group tag or folder itself would grant
    edit access. Making a tag or folder personal (new_group_id empty) needs
    no check here.
    """
    from src.models import GroupMembership, Tag
    if not new_group_id:
        return None
    is_tag = isinstance(source, Tag)
    membership = GroupMembership.query.filter_by(group_id=new_group_id, user_id=user.id).first()
    if membership is None or membership.role != 'admin':
        return 'tag_group_assign_admin_only' if is_tag else 'folder_group_assign_admin_only'
    if is_tag:
        from src.services.tag_sharing import group_tag_apply_error
        for association in source.recording_associations:
            error_key = group_tag_apply_error(association.recording, new_group_id, user, has_recording_access)
            if error_key:
                return error_key
        return None
    for recording in source.recordings:
        if recording.user_id == user.id:
            continue
        if has_recording_access is None or not has_recording_access(recording, user, require_edit=True):
            return 'recording_modify_denied'
    return None


def apply_folder_shares(recording, folder, sharing_enabled=None):
    """Create the shares for a recording in a group folder (no commit). Returns the count.

    The folder's settings decide: auto-share on apply shares with every
    member of the group, share with group leads only with its admins. Admins
    get edit access, other members read access. A folder shares a recording
    whatever its processing status. A user who already has a share of the
    recording gets no second one.
    sharing_enabled: the caller's ENABLE_INTERNAL_SHARING (None reads src.app's).
    """
    from flask import current_app
    from src.models import GroupMembership, InternalShare
    from src.services.tag_sharing import _sharing_enabled
    if folder is None or not folder.group_id or not _sharing_enabled(sharing_enabled):
        return 0
    if folder.auto_share_on_apply:
        members = GroupMembership.query.filter_by(group_id=folder.group_id).all()
    elif folder.share_with_group_lead:
        members = GroupMembership.query.filter_by(group_id=folder.group_id, role='admin').all()
    else:
        return 0
    already = {uid for (uid,) in db.session.query(InternalShare.shared_with_user_id)
               .filter(InternalShare.recording_id == recording.id)}
    created = 0
    for membership in members:
        if membership.user_id == recording.user_id or membership.user_id in already:
            continue
        db.session.add(InternalShare(
            recording_id=recording.id,
            owner_id=recording.user_id,
            shared_with_user_id=membership.user_id,
            can_edit=(membership.role == 'admin'),
            can_reshare=False,
            source_type='group_folder',
            source_tag_id=None,
            source_folder_id=folder.id,  # share-reason folder, surfaced to the recipient (#314)
        ))
        ensure_recipient_state(recording.id, membership.user_id)
        already.add(membership.user_id)
        created += 1
        current_app.logger.info(f"Auto-shared recording {recording.id} with user {membership.user_id} "
                                f"via group folder '{folder.name}'")
    if created:
        db.session.flush()
    return created


def sync_source_shares(source, previous_state, sharing_enabled=None):
    """Bring the shares of a tag or folder in line with its group and share settings.

    The one path for a change of a tag's or folder's group or share settings,
    used by the web routes and API v1. previous_state is sharing_state(source)
    taken before the change; an unchanged state changes nothing. Shares
    without a reason end: the previous group's, and those of members the
    narrower settings leave out. The recordings that carry the tag, or sit in
    the folder, are then shared as the current settings say: a tag shares
    completed recordings only (the others at the end of processing), a
    folder every recording in it. A subfolder has its own group and share
    settings, so only the folder's own recordings count. Manual shares, and
    shares another group tag or folder still gives, stay.
    sharing_enabled: the caller's ENABLE_INTERNAL_SHARING (None reads
    src.app's). No commit. Returns (removed, created).
    """
    from src.models import Tag
    if sharing_state(source) == previous_state:
        return 0, 0
    if isinstance(source, Tag):
        from src.services.tag_sharing import apply_tag_shares
        recordings = [association.recording for association in source.recording_associations]
        apply = apply_tag_shares
    else:
        recordings = list(source.recordings)
        apply = apply_folder_shares
    removed = drop_stale_auto_shares(recording_ids=[r.id for r in recordings])
    created = 0
    for recording in recordings:
        created += apply(recording, source, sharing_enabled=sharing_enabled)
    return removed, created


def share_scope_refusal(created):
    """The 403 for an API token without the share scope when a change created shares, else None.

    Applying a tag through API v1 needs the share scope when it shares the
    recording; a change of a tag's or folder's group or share settings that
    creates shares needs it too. The caller rolls back on a refusal. Session
    requests and full-access tokens always pass.
    """
    if not created:
        return None
    from src.utils.token_auth import current_api_token, scope_error_response
    token = current_api_token()
    if token is None or token.scope_set is None or 'share' in token.scope_set:
        return None
    return scope_error_response({'share'}, token)
