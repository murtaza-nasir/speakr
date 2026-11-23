"""
Group management and collaboration.

This blueprint was auto-generated from app.py route extraction.
"""

import json
import re
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from src.database import db
from src.models import *
from src.utils import *
from src.utils.localization import api_error, translate
from src.utils.request_body import flag_fields, is_db_id, json_object_body

# Group.name is String(100); PostgreSQL refuses a longer one at commit.
GROUP_NAME_MAX_LENGTH = 100


def _group_fields_error(data):
    """The 400 for a name or description that is not a string (null is
    no value) or a name past the column length, or None (#687)."""
    for field in ('name', 'description'):
        if data.get(field) is not None and not isinstance(data[field], str):
            return jsonify(api_error('field_must_be_string', field=field)), 400
    if len((data.get('name') or '').strip()) > GROUP_NAME_MAX_LENGTH:
        return jsonify(api_error('field_too_long', field='name', max=GROUP_NAME_MAX_LENGTH)), 400
    return None
from src.services import audit
from src.services.auto_shares import drop_stale_auto_shares, ensure_recipient_state
from src.auth.permissions import CREATE_GROUPS, group_has_grant, set_group_grant, user_can
from sqlalchemy.exc import IntegrityError

# Create blueprint
groups_bp = Blueprint('groups', __name__)

# Global helpers (will be injected from app)
has_recording_access = None
bcrypt = None
csrf = None
limiter = None

def init_groups_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')


def can_create_groups(user):
    """Whether the user may create a group now (#576).

    Groups need internal sharing, so nobody can create one while it is off.
    """
    return bool(_self.ENABLE_INTERNAL_SHARING) and user_can(user, CREATE_GROUPS)


def _is_admin_of_group(user, group_id):
    return GroupMembership.query.filter_by(
        group_id=group_id, user_id=user.id, role='admin'
    ).first() is not None


def can_delete_group(user, group):
    """Site admins, and the group's creator while still a group admin of it (#576)."""
    if user.is_admin:
        return True
    return group.created_by is not None and group.created_by == user.id \
        and _is_admin_of_group(user, group.id)


def _group_payload(group):
    """A group as the management pages show it to the current user."""
    data = group.to_dict()
    data['can_delete'] = can_delete_group(current_user, group)
    if current_user.is_admin:
        # Only a site admin grants permissions, so only a site admin sees them
        data['members_create_groups_granted'] = group_has_grant(CREATE_GROUPS, group.id)
    return data


def _group_name_taken(name, exclude_id=None):
    """Group names are unique instance-wide, ignoring case (#576).

    Checked when a group is created or renamed to a different name
    (update_team), never for a group keeping its own name.
    """
    query = Group.query.filter(db.func.lower(Group.name) == name.lower())
    if exclude_id is not None:
        query = query.filter(Group.id != exclude_id)
    return query.first() is not None


def _find_user_exactly(identifier):
    """The account with exactly this username, or else this email address.

    The email comparison ignores case. No partial match, so a group admin who
    is not a site admin can add a person whose username or email they already
    know (#576), at every level of the user_search_scope setting. The lookup
    is the one the share window uses (src/services/user_lookup.py).
    """
    from src.services import user_lookup
    return user_lookup.find_user_exactly(identifier)


# --- Routes ---

@groups_bp.route('/api/groups/<int:group_id>/sync-shares', methods=['POST'])
@login_required
def sync_team_tag_shares(group_id):
    """Retroactively share recordings with group members based on group tags with auto-sharing enabled."""
    # Verify group exists
    group = db.session.get(Group, group_id)
    if not group:
        return jsonify(api_error('group_not_found')), 404

    # Verify user is a group admin
    membership = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first()

    if not membership:
        return jsonify(api_error('share_sync_admin_only')), 403

    if not _self.ENABLE_INTERNAL_SHARING:
        return jsonify(api_error('internal_sharing_disabled')), 403

    # Get all group tags with auto-sharing enabled
    group_tags = Tag.query.filter(
        Tag.group_id == group_id,
        db.or_(
            Tag.auto_share_on_apply == True,
            Tag.share_with_group_lead == True
        )
    ).all()

    shares_created = 0
    recordings_processed = 0

    for tag in group_tags:
        # Get all completed recordings with this tag
        recordings = db.session.query(Recording).join(RecordingTag).filter(
            RecordingTag.tag_id == tag.id,
            Recording.status == 'COMPLETED'
        ).all()

        for recording in recordings:
            recordings_processed += 1

            # Determine who to share with
            if tag.auto_share_on_apply:
                group_members = GroupMembership.query.filter_by(group_id=group_id).all()
            elif tag.share_with_group_lead:
                group_members = GroupMembership.query.filter_by(group_id=group_id, role='admin').all()
            else:
                continue

            for membership_to_share in group_members:
                # Skip the recording owner
                if membership_to_share.user_id == recording.user_id:
                    continue

                # Check if already shared
                existing_share = InternalShare.query.filter_by(
                    recording_id=recording.id,
                    shared_with_user_id=membership_to_share.user_id
                ).first()

                if not existing_share:
                    # Create internal share with correct permissions
                    # Group admins get edit permission, regular members get read-only
                    share = InternalShare(
                        recording_id=recording.id,
                        owner_id=recording.user_id,
                        shared_with_user_id=membership_to_share.user_id,
                        can_edit=(membership_to_share.role == 'admin'),
                        can_reshare=False,
                        source_type='group_tag',
                        source_tag_id=tag.id
                    )
                    db.session.add(share)

                    # The recipient's state row, reused when an earlier share left one (#571)
                    ensure_recipient_state(recording.id, membership_to_share.user_id)

                    shares_created += 1
                    current_app.logger.info(f"Synced share: Recording {recording.id} with user {membership_to_share.user_id} (role={membership_to_share.role}) via group tag {tag.id}")

    db.session.commit()

    return jsonify({
        'success': True,
        'shares_created': shares_created,
        'recordings_processed': recordings_processed,
        'message': translate('server.apiMessage.sharesSynced', sharesCreated=shares_created,
                             recordingsProcessed=recordings_processed)
    })



@groups_bp.route('/api/admin/groups', methods=['GET'])
@login_required
def get_teams():
    """Get all groups (admin) or groups user is admin of (group admin)."""
    # Check if user is admin OR group admin
    is_group_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    may_create = can_create_groups(current_user)
    if not current_user.is_admin and not is_group_admin and not may_create:
        return jsonify(api_error('admin_required')), 403

    # If full admin, return all groups; if group admin, return only their groups
    if current_user.is_admin:
        groups = Group.query.all()
    else:
        # Get groups where user is an admin
        group_memberships = GroupMembership.query.filter_by(
            user_id=current_user.id,
            role='admin'
        ).all()
        groups = [m.group for m in group_memberships]

    return jsonify({'groups': [_group_payload(group) for group in groups],
                    'can_create_groups': may_create})



@groups_bp.route('/api/admin/groups', methods=['POST'])
@login_required
def create_team():
    """Create a new group: site admins and users with create_groups (#576).

    A creator who is not a site admin becomes the group's admin.
    """
    if not user_can(current_user, CREATE_GROUPS):
        return jsonify(api_error('group_create_not_permitted')), 403

    if not _self.ENABLE_INTERNAL_SHARING:
        return jsonify(api_error('groups_require_internal_sharing')), 403

    data, body_error = json_object_body(optional=True)
    if body_error:
        return body_error
    field_error = _group_fields_error(data)
    if field_error:
        return field_error
    name = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()

    if not name:
        return jsonify(api_error('group_name_required')), 400

    if _group_name_taken(name):
        return jsonify(api_error('group_name_exists')), 400

    group = Group(name=name, description=description, created_by=current_user.id)
    db.session.add(group)
    if not current_user.is_admin:
        group.memberships.append(GroupMembership(user_id=current_user.id, role='admin'))
    try:
        db.session.flush()
        if current_user.is_admin and data.get('members_create_groups_granted'):
            set_group_grant(CREATE_GROUPS, group.id, True)
        # The id of the group; never its name.
        audit.record_in_tx('group.created', group_id=group.id)
        db.session.commit()
    except IntegrityError:
        # Another request created the same name first
        db.session.rollback()
        return jsonify(api_error('group_name_exists')), 400

    current_app.logger.info(f"User {current_user.id} created group {group.id}")
    current_app.logger.debug(f"User {current_user.username} created group: {name}")
    return jsonify(_group_payload(group)), 201



@groups_bp.route('/api/admin/groups/<int:group_id>', methods=['GET'])
@login_required
def get_team(group_id):
    """Get group details (admin or group admin)."""
    group = db.session.get(Group, group_id)
    if not group:
        return jsonify(api_error('group_not_found')), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify(api_error('admin_required')), 403

    group_dict = _group_payload(group)
    group_dict['members'] = [m.to_dict() for m in group.memberships]
    return jsonify(group_dict)



@groups_bp.route('/api/admin/groups/<int:group_id>', methods=['PUT'])
@login_required
def update_team(group_id):
    """Update group (admin or group admin)."""
    group = db.session.get(Group, group_id)
    if not group:
        return jsonify(api_error('group_not_found')), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify(api_error('admin_required')), 403

    data, body_error = json_object_body(optional=True)
    if body_error:
        return body_error
    field_error = _group_fields_error(data)
    if field_error:
        return field_error
    flags, flag_error = flag_fields(data, ('members_create_groups_granted',))
    if flag_error:
        return flag_error
    name = (data.get('name') or '').strip()
    description = (data.get('description') or '').strip()

    if 'members_create_groups_granted' in data:
        # A permission grant, which only a site admin may change (#576)
        if not current_user.is_admin:
            return jsonify(api_error('admin_required')), 403
        set_group_grant(CREATE_GROUPS, group.id, flags['members_create_groups_granted'])

    if name:
        # Only a rename to a different name is checked against the other
        # groups. An install from before names were compared without case
        # can hold "Sales" and "sales"; an edit that sends such a group's
        # own name back (a new description, another case) must still pass.
        renamed = name.lower() != (group.name or '').strip().lower()
        if renamed and _group_name_taken(name, exclude_id=group_id):
            return jsonify(api_error('group_name_exists')), 400
        group.name = name

    if 'description' in data:
        group.description = description
    # The names of the fields that were sent, never the group name.
    audit.record_in_tx('group.changed', group_id=group_id, details={'fields': [
        field for field, sent in (('name', bool(name)), ('description', 'description' in data),
                                  ('members_create_groups_granted', 'members_create_groups_granted' in data))
        if sent]})
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify(api_error('group_name_exists')), 400

    current_app.logger.info(f"User {current_user.id} updated group {group.id}")
    current_app.logger.debug(f"Admin {current_user.username} updated group: {group.name}")
    return jsonify(_group_payload(group))



@groups_bp.route('/api/admin/groups/<int:group_id>', methods=['DELETE'])
@login_required
def delete_team(group_id):
    """Delete a group: site admins, or its creator while a group admin of it (#576)."""
    group = db.session.get(Group, group_id)
    if not group:
        if not current_user.is_admin:
            return jsonify(api_error('admin_required')), 403
        return jsonify(api_error('group_not_found')), 404

    if not can_delete_group(current_user, group):
        return jsonify(api_error('group_delete_not_permitted')), 403

    group_name = group.name
    member_ids = [m.user_id for m in group.memberships]
    # The group's folders become personal folders of their creators. A
    # parent created by someone else would then be in another user's
    # scope, so that child becomes top level (#585).
    group_folders = {folder.id: folder for folder in group.folders}
    for folder in group_folders.values():
        parent = group_folders.get(folder.parent_id)
        if folder.parent_id is not None and (parent is None or parent.user_id != folder.user_id):
            folder.parent_id = None
    db.session.delete(group)
    # The group's tags and folders no longer share anything (#570)
    drop_stale_auto_shares(user_ids=member_ids)
    audit.record_in_tx('group.deleted', group_id=group_id)
    db.session.commit()

    current_app.logger.info(f"User {current_user.id} deleted group {group_id}")
    current_app.logger.debug(f"User {current_user.username} deleted group: {group_name}")
    return jsonify({'success': True})



@groups_bp.route('/api/admin/groups/<int:group_id>/members', methods=['POST'])
@login_required
def add_team_member(group_id):
    """Add a member to a group (admin or group admin)."""
    if not _self.ENABLE_INTERNAL_SHARING:
        return jsonify(api_error('groups_require_internal_sharing')), 403

    group = db.session.get(Group, group_id)
    if not group:
        return jsonify(api_error('group_not_found')), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify(api_error('admin_required')), 403

    data, body_error = json_object_body(optional=True)
    if body_error:
        return body_error
    role = data.get('role', 'member')
    identifier = data.get('identifier')
    identifier = identifier.strip() if isinstance(identifier, str) else ''

    if role not in ['admin', 'member']:
        return jsonify(api_error('invalid_group_role')), 400
    if data.get('user_id') and not is_db_id(data['user_id']):
        return jsonify(api_error('invalid_integer_value')), 400

    if current_user.is_admin and data.get('user_id'):
        # Site admins pick from the full user list (/admin/users)
        user = db.session.get(User, data.get('user_id'))
    elif identifier:
        user = _find_user_exactly(identifier)
    elif current_user.is_admin:
        return jsonify(api_error('user_id_required')), 400
    else:
        # A group admin who is not a site admin names the person exactly:
        # no user ids, no listing and no partial search (#576)
        return jsonify(api_error('member_identifier_required')), 400

    if not user:
        return jsonify(api_error('user_not_found')), 404
    user_id = user.id

    # Check if already a member
    existing = GroupMembership.query.filter_by(group_id=group_id, user_id=user_id).first()
    if existing:
        return jsonify(api_error('already_group_member')), 400

    membership = GroupMembership(group_id=group_id, user_id=user_id, role=role)
    db.session.add(membership)
    audit.record_in_tx('group.member.added', group_id=group_id, target_user_id=user_id,
                       details={'role': audit.code(role, ('admin', 'member'), 'other')})
    db.session.commit()

    current_app.logger.info(f"User {current_user.id} added user {user_id} to group {group_id} as {role}")
    current_app.logger.debug(f"Admin {current_user.username} added {user.username} to group {group.name} as {role}")
    return jsonify(membership.to_dict()), 201



@groups_bp.route('/api/admin/groups/<int:group_id>/members/<int:user_id>', methods=['PUT'])
@login_required
def update_team_member(group_id, user_id):
    """Update group member role (admin or group admin)."""
    membership = GroupMembership.query.filter_by(group_id=group_id, user_id=user_id).first()
    if not membership:
        return jsonify(api_error('membership_not_found')), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify(api_error('admin_required')), 403

    data, body_error = json_object_body()
    if body_error:
        return body_error
    role = data.get('role')

    if role not in ['admin', 'member']:
        return jsonify(api_error('invalid_group_role')), 400

    old_role = membership.role
    membership.role = role
    # Group-made shares give edit to admins only, the rule that created them
    drop_stale_auto_shares(user_ids=[user_id])
    if role != old_role:
        audit.record_in_tx('group.member.role_changed', group_id=group_id, target_user_id=user_id,
                           details={'role': role, 'old_role': audit.code(old_role, ('admin', 'member'), 'other')})
    db.session.commit()

    current_app.logger.info(f"User {current_user.id} updated the role of user {user_id} to {role} in group {group_id}")
    current_app.logger.debug(f"Admin {current_user.username} updated {membership.user.username} role to {role} in group {membership.group.name}")
    return jsonify(membership.to_dict())



@groups_bp.route('/api/admin/groups/<int:group_id>/members/<int:user_id>', methods=['DELETE'])
@login_required
def remove_team_member(group_id, user_id):
    """Remove a member from a group (admin or group admin)."""
    membership = GroupMembership.query.filter_by(group_id=group_id, user_id=user_id).first()
    if not membership:
        return jsonify(api_error('membership_not_found')), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify(api_error('admin_required')), 403

    username = membership.user.username
    group_name = membership.group.name
    db.session.delete(membership)
    # Shares the group's tags and folders made for this member end with the
    # membership; a share the owner made by hand stays (#570)
    drop_stale_auto_shares(user_ids=[user_id])
    audit.record_in_tx('group.member.removed', group_id=group_id, target_user_id=user_id)
    db.session.commit()

    current_app.logger.info(f"User {current_user.id} removed user {user_id} from group {group_id}")
    current_app.logger.debug(f"Admin {current_user.username} removed {username} from group {group_name}")
    return jsonify({'success': True})


# Hot-applied System Settings (high priority) with environment fallback.
from src.utils.system_settings import apply_hot_settings
_SYSTEM_SETTING_MAP = {
    "USERS_CAN_DELETE": ('users_can_delete', 'USERS_CAN_DELETE', True),
    "ENABLE_INTERNAL_SHARING": ('enable_internal_sharing', 'ENABLE_INTERNAL_SHARING', False),
    "ENABLE_AUTO_DELETION": ('enable_auto_deletion', 'ENABLE_AUTO_DELETION', False),
}
_self, __getattr__ = apply_hot_settings(__name__, _SYSTEM_SETTING_MAP)
