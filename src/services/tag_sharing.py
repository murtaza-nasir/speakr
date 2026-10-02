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


def apply_tag_shares(recording, tag, require_completed=True, sharing_enabled=None):
    """Create the shares for one applied tag (no commit). Returns the count."""
    from src.models import InternalShare, SharedRecordingState
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
        if not SharedRecordingState.query.filter_by(recording_id=recording.id, user_id=membership.user_id).first():
            db.session.add(SharedRecordingState(recording_id=recording.id, user_id=membership.user_id,
                                                is_inbox=True, is_highlighted=False))
        created += 1
        current_app.logger.info(f"Auto-shared recording {recording.id} with user {membership.user_id} "
                                f"(role={membership.role}) via group tag '{tag.name}'")
    db.session.flush()
    return created
