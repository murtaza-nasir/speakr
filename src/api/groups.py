"""
Group management and collaboration.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
import re
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from src.database import db
from src.models import *
from src.utils import *

# Create blueprint
groups_bp = Blueprint('groups', __name__)

# Configuration from environment
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'
ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
USERS_CAN_DELETE = os.environ.get('USERS_CAN_DELETE', 'true').lower() == 'true'
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'
USE_ASR_ENDPOINT = os.environ.get('USE_ASR_ENDPOINT', 'false').lower() == 'true'

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


# --- Routes ---

@groups_bp.route('/api/groups/<int:group_id>/sync-shares', methods=['POST'])
@login_required
def sync_team_tag_shares(group_id):
    """Retroactively share recordings with group members based on group tags with auto-sharing enabled."""
    # Verify group exists
    group = db.session.get(Group, group_id)
    if not group:
        return jsonify({'error': 'Group not found'}), 404

    # Verify user is a group admin
    membership = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first()

    if not membership:
        return jsonify({'error': 'Only group admins can sync shares'}), 403

    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Internal sharing is not enabled'}), 403

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

                    # Create SharedRecordingState with default values for the recipient
                    state = SharedRecordingState(
                        recording_id=recording.id,
                        user_id=membership_to_share.user_id,
                        is_inbox=True,  # New shares appear in inbox by default
                        is_highlighted=False  # Not favorited by default
                    )
                    db.session.add(state)

                    shares_created += 1
                    current_app.logger.info(f"Synced share: Recording {recording.id} with user {membership_to_share.user_id} (role={membership_to_share.role}) via group tag '{tag.name}'")

    db.session.commit()

    return jsonify({
        'success': True,
        'shares_created': shares_created,
        'recordings_processed': recordings_processed,
        'message': f'Created {shares_created} new shares across {recordings_processed} recordings'
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

    if not current_user.is_admin and not is_group_admin:
        return jsonify({'error': 'Admin access required'}), 403

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

    return jsonify({'groups': [group.to_dict() for group in groups]})



@groups_bp.route('/api/admin/groups', methods=['POST'])
@login_required
def create_team():
    """Create a new group (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403

    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Groups require internal sharing to be enabled. Please set ENABLE_INTERNAL_SHARING=true in your configuration.'}), 403

    data = request.get_json()
    name = data.get('name', '').strip()
    description = data.get('description', '').strip()

    if not name:
        return jsonify({'error': 'Group name is required'}), 400

    # Check if group name already exists
    existing = Group.query.filter_by(name=name).first()
    if existing:
        return jsonify({'error': 'A group with this name already exists'}), 400

    group = Group(name=name, description=description)
    db.session.add(group)
    db.session.commit()

    current_app.logger.info(f"Admin {current_user.username} created group: {name}")
    return jsonify(group.to_dict()), 201



@groups_bp.route('/api/admin/groups/<int:group_id>', methods=['GET'])
@login_required
def get_team(group_id):
    """Get group details (admin or group admin)."""
    group = db.session.get(Group, group_id)
    if not group:
        return jsonify({'error': 'Group not found'}), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify({'error': 'Admin access required'}), 403

    group_dict = group.to_dict()
    group_dict['members'] = [m.to_dict() for m in group.memberships]
    return jsonify(group_dict)



@groups_bp.route('/api/admin/groups/<int:group_id>', methods=['PUT'])
@login_required
def update_team(group_id):
    """Update group (admin or group admin)."""
    group = db.session.get(Group, group_id)
    if not group:
        return jsonify({'error': 'Group not found'}), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json()
    name = data.get('name', '').strip()
    description = data.get('description', '').strip()

    if name:
        # Check if new name conflicts with another group
        existing = Group.query.filter(Group.name == name, Group.id != group_id).first()
        if existing:
            return jsonify({'error': 'A group with this name already exists'}), 400
        group.name = name

    group.description = description
    db.session.commit()

    current_app.logger.info(f"Admin {current_user.username} updated group: {group.name}")
    return jsonify(group.to_dict())



@groups_bp.route('/api/admin/groups/<int:group_id>', methods=['DELETE'])
@login_required
def delete_team(group_id):
    """Delete group (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403

    group = db.session.get(Group, group_id)
    if not group:
        return jsonify({'error': 'Group not found'}), 404

    group_name = group.name
    db.session.delete(group)
    db.session.commit()

    current_app.logger.info(f"Admin {current_user.username} deleted group: {group_name}")
    return jsonify({'success': True})



@groups_bp.route('/api/admin/groups/<int:group_id>/members', methods=['POST'])
@login_required
def add_team_member(group_id):
    """Add a member to a group (admin or group admin)."""
    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Groups require internal sharing to be enabled. Please set ENABLE_INTERNAL_SHARING=true in your configuration.'}), 403

    group = db.session.get(Group, group_id)
    if not group:
        return jsonify({'error': 'Group not found'}), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json()
    user_id = data.get('user_id')
    role = data.get('role', 'member')

    if not user_id:
        return jsonify({'error': 'User ID is required'}), 400

    if role not in ['admin', 'member']:
        return jsonify({'error': 'Role must be "admin" or "member"'}), 400

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    # Check if already a member
    existing = GroupMembership.query.filter_by(group_id=group_id, user_id=user_id).first()
    if existing:
        return jsonify({'error': 'User is already a member of this group'}), 400

    membership = GroupMembership(group_id=group_id, user_id=user_id, role=role)
    db.session.add(membership)
    db.session.commit()

    current_app.logger.info(f"Admin {current_user.username} added {user.username} to group {group.name} as {role}")
    return jsonify(membership.to_dict()), 201



@groups_bp.route('/api/admin/groups/<int:group_id>/members/<int:user_id>', methods=['PUT'])
@login_required
def update_team_member(group_id, user_id):
    """Update group member role (admin or group admin)."""
    membership = GroupMembership.query.filter_by(group_id=group_id, user_id=user_id).first()
    if not membership:
        return jsonify({'error': 'Membership not found'}), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify({'error': 'Admin access required'}), 403

    data = request.get_json()
    role = data.get('role')

    if role not in ['admin', 'member']:
        return jsonify({'error': 'Role must be "admin" or "member"'}), 400

    membership.role = role
    db.session.commit()

    current_app.logger.info(f"Admin {current_user.username} updated {membership.user.username} role to {role} in group {membership.group.name}")
    return jsonify(membership.to_dict())



@groups_bp.route('/api/admin/groups/<int:group_id>/members/<int:user_id>', methods=['DELETE'])
@login_required
def remove_team_member(group_id, user_id):
    """Remove a member from a group (admin or group admin)."""
    membership = GroupMembership.query.filter_by(group_id=group_id, user_id=user_id).first()
    if not membership:
        return jsonify({'error': 'Membership not found'}), 404

    # Check if user is admin OR admin of this specific group
    is_group_admin = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_group_admin:
        return jsonify({'error': 'Admin access required'}), 403

    username = membership.user.username
    group_name = membership.group.name
    db.session.delete(membership)
    db.session.commit()

    current_app.logger.info(f"Admin {current_user.username} removed {username} from group {group_name}")
    return jsonify({'success': True})



