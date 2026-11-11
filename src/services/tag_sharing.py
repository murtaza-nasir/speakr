"""Shares created by applying a group tag (one rule for every path).

A group tag with auto_share_on_apply shares the recording with every member
of the group; with share_with_group_lead, with the group's admins. Members
who are admins get edit access, others read access. Only completed
recordings are shared this way; recordings still processing get their
shares when processing completes (apply_team_tag_auto_shares).

The web route, API v1 (POST and PUT tags) and the end of processing call
these functions, so a tag shares the same way wherever it is applied.
"""

from flask import current_app

from src.database import db


def _sharing_enabled(enabled):
    if enabled is not None:
        return bool(enabled)
    from src import app as app_module
    return bool(getattr(app_module, 'ENABLE_INTERNAL_SHARING', False))


def share_targets(recording, tag, require_completed=True, sharing_enabled=None):
    """Group memberships that applying tag would share the recording with.

    sharing_enabled: the caller's ENABLE_INTERNAL_SHARING (None reads src.app's).
    """
    from src.models import GroupMembership, InternalShare
    if not tag.group_id or not _sharing_enabled(sharing_enabled):
        return []
    if require_completed and recording.status != 'COMPLETED':
        return []
    if tag.auto_share_on_apply:
        members = GroupMembership.query.filter_by(group_id=tag.group_id).all()
    elif tag.share_with_group_lead:
        members = GroupMembership.query.filter_by(group_id=tag.group_id, role='admin').all()
    else:
        return []
    already = {uid for (uid,) in db.session.query(InternalShare.shared_with_user_id)
               .filter(InternalShare.recording_id == recording.id)}
    return [m for m in members if m.user_id != recording.user_id and m.user_id not in already]


def group_tag_apply_error(recording, group_id, user, has_recording_access):
    """Why user may not put a tag of group group_id on recording, as an error key, or None.

    The rule of the single-tag route: a member of the group, the recording's
    owner or an admin of the group, with edit access to the recording. A
    group tag shares the recording with the group and gives its admins edit
    access, so every path that puts one on a recording asks the same,
    including turning a tag already on it into a group tag (#578, #579).
    """
    from src.models import GroupMembership
    membership = GroupMembership.query.filter_by(group_id=group_id, user_id=user.id).first()
    if not membership:
        return 'tag_access_denied'
    if recording.user_id != user.id and membership.role != 'admin':
        return 'group_tag_apply_restricted'
    if not has_recording_access(recording, user, require_edit=True):
        return 'group_tag_apply_denied'
    return None


def apply_tag_shares(recording, tag, require_completed=True, sharing_enabled=None):
    """Create the shares for one applied tag (no commit). Returns the count."""
    from src.models import InternalShare
    from src.services.auto_shares import ensure_recipient_state
    created = 0
    for membership in share_targets(recording, tag, require_completed=require_completed,
                                    sharing_enabled=sharing_enabled):
        db.session.add(InternalShare(
            recording_id=recording.id,
            owner_id=recording.user_id,
            shared_with_user_id=membership.user_id,
            can_edit=(membership.role == 'admin'),
            can_reshare=False,
            source_type='group_tag',
            source_tag_id=tag.id,
        ))
        ensure_recipient_state(recording.id, membership.user_id)
        created += 1
        current_app.logger.info(f"Auto-shared recording {recording.id} with user {membership.user_id} "
                                f"(role={membership.role}) via group tag '{tag.name}'")
    db.session.flush()
    return created
