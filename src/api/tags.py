"""
Tag management and assignment.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
import time
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, send_file, Response, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from sqlalchemy.exc import IntegrityError

from src.database import db
from src.models import *
from src.utils import *

# Create blueprint
tags_bp = Blueprint('tags', __name__)

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

def init_tags_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')


# --- Routes ---

@tags_bp.route('/api/tags', methods=['GET'])
@login_required
def get_tags():
    """Get all tags for the current user, including group tags they have access to."""
    # Get user's personal tags
    user_tags = Tag.query.filter_by(user_id=current_user.id, group_id=None).order_by(Tag.name).all()

    # Get user's team memberships with roles
    memberships = GroupMembership.query.filter_by(user_id=current_user.id).all()
    team_roles = {m.group_id: m.role for m in memberships}
    team_ids = list(team_roles.keys())

    # Get group tags for all teams the user is a member of
    team_tags = []
    if team_ids:
        team_tags = Tag.query.filter(Tag.group_id.in_(team_ids)).order_by(Tag.name).all()

    # Build response with edit permissions
    result = []

    # Personal tags - user can always edit their own
    for tag in user_tags:
        tag_dict = tag.to_dict()
        tag_dict['can_edit'] = True
        tag_dict['user_role'] = None
        result.append(tag_dict)

    # Group tags - only admins can edit
    for tag in team_tags:
        tag_dict = tag.to_dict()
        user_role = team_roles.get(tag.group_id, 'member')
        tag_dict['can_edit'] = (user_role == 'admin')
        tag_dict['user_role'] = user_role
        result.append(tag_dict)

    return jsonify(result)



@tags_bp.route('/api/tags', methods=['POST'])
@login_required
def create_tag():
    """Create a new tag (personal or group tag)."""
    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'Tag name is required'}), 400

    group_id = data.get('group_id')

    # If creating a group tag, verify user is admin of that group
    if group_id:
        membership = GroupMembership.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()

        if not membership or membership.role != 'admin':
            return jsonify({'error': 'Only group admins can create group tags'}), 403

        # Check if group tag with same name already exists for this group
        existing_tag = Tag.query.filter_by(name=data['name'], group_id=group_id).first()
        if existing_tag:
            return jsonify({'error': 'A tag with this name already exists for this group'}), 400
    else:
        # Check if personal tag with same name already exists for this user
        existing_tag = Tag.query.filter_by(name=data['name'], user_id=current_user.id, group_id=None).first()
        if existing_tag:
            return jsonify({'error': 'Tag with this name already exists'}), 400

    # Handle retention_days: -1 means protected from deletion
    retention_days = data.get('retention_days')
    protect_from_deletion = False

    if retention_days == -1:
        # -1 indicates infinite retention (protected from auto-deletion)
        protect_from_deletion = True if ENABLE_AUTO_DELETION else False

    # Validate naming_template_id if provided
    naming_template_id = data.get('naming_template_id')
    if naming_template_id:
        from src.models import NamingTemplate
        template = NamingTemplate.query.filter_by(id=naming_template_id, user_id=current_user.id).first()
        if not template:
            return jsonify({'error': 'Naming template not found'}), 404

    tag = Tag(
        name=data['name'],
        user_id=current_user.id,
        group_id=group_id,
        color=data.get('color', '#3B82F6'),
        custom_prompt=data.get('custom_prompt'),
        default_language=data.get('default_language'),
        default_min_speakers=data.get('default_min_speakers'),
        default_max_speakers=data.get('default_max_speakers'),
        protect_from_deletion=protect_from_deletion,
        retention_days=retention_days,
        auto_share_on_apply=data.get('auto_share_on_apply', True) if group_id else True,
        share_with_group_lead=data.get('share_with_group_lead', True) if group_id else True,
        naming_template_id=naming_template_id
    )

    db.session.add(tag)

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.error(f"Tag creation failed due to integrity constraint: {str(e)}")
        return jsonify({'error': 'A tag with this name already exists'}), 400

    return jsonify(tag.to_dict()), 201



@tags_bp.route('/api/tags/<int:tag_id>', methods=['PUT'])
@login_required
def update_tag(tag_id):
    """Update a tag."""
    tag = db.session.get(Tag, tag_id)
    if not tag:
        return jsonify({'error': 'Tag not found'}), 404

    # Check permissions
    if tag.group_id:
        # Group tag - user must be a team admin
        membership = GroupMembership.query.filter_by(
            group_id=tag.group_id,
            user_id=current_user.id
        ).first()

        if not membership or membership.role != 'admin':
            return jsonify({'error': 'Only group admins can edit group tags'}), 403
    else:
        # Personal tag - must be the owner
        if tag.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to edit this tag'}), 403

    data = request.get_json()

    if 'name' in data:
        # Check if new name conflicts with another tag
        if tag.group_id:
            existing_tag = Tag.query.filter_by(name=data['name'], group_id=tag.group_id).filter(Tag.id != tag_id).first()
        else:
            existing_tag = Tag.query.filter_by(name=data['name'], user_id=current_user.id).filter(Tag.id != tag_id).first()

        if existing_tag:
            return jsonify({'error': 'Another tag with this name already exists'}), 400
        tag.name = data['name']

    # Handle group_id changes (converting between personal and group tags)
    if 'group_id' in data:
        new_group_id = data['group_id'] if data['group_id'] else None

        # If changing to a group tag, verify user is admin of that group
        if new_group_id:
            membership = GroupMembership.query.filter_by(
                group_id=new_group_id,
                user_id=current_user.id
            ).first()

            if not membership or membership.role != 'admin':
                return jsonify({'error': 'Only group admins can assign tags to groups'}), 403

        tag.group_id = new_group_id

    if 'color' in data:
        tag.color = data['color']
    if 'custom_prompt' in data:
        tag.custom_prompt = data['custom_prompt']
    if 'default_language' in data:
        tag.default_language = data['default_language']
    if 'default_min_speakers' in data:
        tag.default_min_speakers = data['default_min_speakers']
    if 'default_max_speakers' in data:
        tag.default_max_speakers = data['default_max_speakers']

    # Handle retention_days: -1 means protected from deletion
    if 'retention_days' in data:
        retention_days = data['retention_days']

        if retention_days == -1:
            # -1 indicates infinite retention (protected from auto-deletion)
            if ENABLE_AUTO_DELETION:
                tag.protect_from_deletion = True
                tag.retention_days = -1
        else:
            # Regular retention period or null (use global)
            tag.protect_from_deletion = False
            tag.retention_days = retention_days if retention_days else None
    if 'auto_share_on_apply' in data:
        # Only applicable to group tags
        if tag.group_id:
            tag.auto_share_on_apply = bool(data['auto_share_on_apply'])
    if 'share_with_group_lead' in data:
        # Only applicable to group tags
        if tag.group_id:
            tag.share_with_group_lead = bool(data['share_with_group_lead'])
    if 'naming_template_id' in data:
        naming_template_id = data['naming_template_id']
        if naming_template_id:
            from src.models import NamingTemplate
            template = NamingTemplate.query.filter_by(id=naming_template_id, user_id=current_user.id).first()
            if not template:
                return jsonify({'error': 'Naming template not found'}), 404
        tag.naming_template_id = naming_template_id if naming_template_id else None

    tag.updated_at = datetime.utcnow()

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.error(f"Tag update failed due to integrity constraint: {str(e)}")
        return jsonify({'error': 'A tag with this name already exists'}), 400

    return jsonify(tag.to_dict())



@tags_bp.route('/api/tags/<int:tag_id>', methods=['DELETE'])
@login_required
def delete_tag(tag_id):
    """Delete a tag."""
    tag = db.session.get(Tag, tag_id)
    if not tag:
        return jsonify({'error': 'Tag not found'}), 404

    # Check permissions
    if tag.group_id:
        # Group tag - user must be a team admin
        membership = GroupMembership.query.filter_by(
            group_id=tag.group_id,
            user_id=current_user.id
        ).first()

        if not membership or membership.role != 'admin':
            return jsonify({'error': 'Only group admins can delete group tags'}), 403
    else:
        # Personal tag - must belong to the user
        if tag.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to delete this tag'}), 403

    db.session.delete(tag)
    db.session.commit()
    return jsonify({'success': True})



@tags_bp.route('/api/groups/<int:group_id>/tags', methods=['POST'])
@login_required
def create_group_tag(group_id):
    """Create a group-scoped tag (group admins only)."""
    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Group tags require internal sharing to be enabled. Please set ENABLE_INTERNAL_SHARING=true in your configuration.'}), 403

    # Verify team exists
    team = db.session.get(Group, group_id)
    if not team:
        return jsonify({'error': 'Group not found'}), 404

    # Verify user is a team admin
    membership = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id
    ).first()

    if not membership or membership.role != 'admin':
        return jsonify({'error': 'Only group admins can create group tags'}), 403

    data = request.get_json()
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'error': 'Tag name is required'}), 400

    # Check if a group tag with this name already exists for this team
    existing_tag = Tag.query.filter_by(
        name=name,
        group_id=group_id
    ).first()

    if existing_tag:
        return jsonify({'error': 'A group tag with this name already exists'}), 400

    # Create the group tag with all supported parameters
    tag = Tag(
        name=name,
        user_id=current_user.id,  # Creator
        group_id=group_id,
        color=data.get('color', '#3B82F6'),
        custom_prompt=data.get('custom_prompt'),
        default_language=data.get('default_language'),
        default_min_speakers=data.get('default_min_speakers'),
        default_max_speakers=data.get('default_max_speakers'),
        protect_from_deletion=data.get('protect_from_deletion', False),
        retention_days=data.get('retention_days'),
        auto_share_on_apply=data.get('auto_share_on_apply', True),  # Default to True for group tags
        share_with_group_lead=data.get('share_with_group_lead', True)  # Default to True for group tags
    )

    db.session.add(tag)

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.error(f"Tag creation failed due to integrity constraint: {str(e)}")
        return jsonify({'error': 'A tag with this name already exists'}), 400

    return jsonify(tag.to_dict()), 201



@tags_bp.route('/api/groups/<int:group_id>/tags', methods=['GET'])
@login_required
def get_group_tags(group_id):
    """Get all tags for a team (team members only)."""
    # Verify team exists
    team = db.session.get(Group, group_id)
    if not team:
        return jsonify({'error': 'Group not found'}), 404

    # Verify user is a team member
    membership = GroupMembership.query.filter_by(
        group_id=group_id,
        user_id=current_user.id
    ).first()

    if not membership:
        return jsonify({'error': 'You must be a team member to view group tags'}), 403

    # Get all group tags
    tags = Tag.query.filter_by(group_id=group_id).all()

    return jsonify({'tags': [tag.to_dict() for tag in tags]})



