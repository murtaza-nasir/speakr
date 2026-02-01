"""
Folder management and assignment.

This blueprint handles folder CRUD operations and recording-folder assignments.
Folders are one-to-many (a recording can only belong to one folder).
"""

import os
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from flask_login import login_required, current_user

from sqlalchemy.exc import IntegrityError

from src.database import db
from src.models import *

# Create blueprint
folders_bp = Blueprint('folders', __name__)

# Configuration from environment
ENABLE_AUTO_DELETION = os.environ.get('ENABLE_AUTO_DELETION', 'false').lower() == 'true'
ENABLE_INTERNAL_SHARING = os.environ.get('ENABLE_INTERNAL_SHARING', 'false').lower() == 'true'

# Global helpers (will be injected from app)
has_recording_access = None
bcrypt = None
csrf = None
limiter = None


def init_folders_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')


# --- Routes ---

@folders_bp.route('/api/folders', methods=['GET'])
@login_required
def get_folders():
    """Get all folders for the current user, including group folders they have access to."""
    # Check if folders feature is enabled - return empty array if not
    folders_enabled = SystemSetting.get_setting('enable_folders', False)
    if not folders_enabled:
        return jsonify([])

    # Get user's personal folders
    user_folders = Folder.query.filter_by(user_id=current_user.id, group_id=None).order_by(Folder.name).all()

    # Get user's team memberships with roles
    memberships = GroupMembership.query.filter_by(user_id=current_user.id).all()
    team_roles = {m.group_id: m.role for m in memberships}
    team_ids = list(team_roles.keys())

    # Get group folders for all teams the user is a member of
    team_folders = []
    if team_ids:
        team_folders = Folder.query.filter(Folder.group_id.in_(team_ids)).order_by(Folder.name).all()

    # Build response with edit permissions
    result = []

    # Personal folders - user can always edit their own
    for folder in user_folders:
        folder_dict = folder.to_dict()
        folder_dict['can_edit'] = True
        folder_dict['user_role'] = None
        result.append(folder_dict)

    # Group folders - only admins can edit
    for folder in team_folders:
        folder_dict = folder.to_dict()
        user_role = team_roles.get(folder.group_id, 'member')
        folder_dict['can_edit'] = (user_role == 'admin')
        folder_dict['user_role'] = user_role
        result.append(folder_dict)

    return jsonify(result)


@folders_bp.route('/api/folders', methods=['POST'])
@login_required
def create_folder():
    """Create a new folder (personal or group folder)."""
    # Check if folders feature is enabled
    folders_enabled = SystemSetting.get_setting('enable_folders', False)
    if not folders_enabled:
        return jsonify({'error': 'Folders feature is not enabled'}), 403

    data = request.get_json()

    if not data or not data.get('name'):
        return jsonify({'error': 'Folder name is required'}), 400

    group_id = data.get('group_id')

    # If creating a group folder, verify user is admin of that group
    if group_id:
        membership = GroupMembership.query.filter_by(
            group_id=group_id,
            user_id=current_user.id
        ).first()

        if not membership or membership.role != 'admin':
            return jsonify({'error': 'Only group admins can create group folders'}), 403

        # Check if group folder with same name already exists for this group
        existing_folder = Folder.query.filter_by(name=data['name'], group_id=group_id).first()
        if existing_folder:
            return jsonify({'error': 'A folder with this name already exists for this group'}), 400
    else:
        # Check if personal folder with same name already exists for this user
        existing_folder = Folder.query.filter_by(name=data['name'], user_id=current_user.id, group_id=None).first()
        if existing_folder:
            return jsonify({'error': 'Folder with this name already exists'}), 400

    # Handle retention_days: -1 means protected from deletion
    retention_days = data.get('retention_days')
    protect_from_deletion = False

    if retention_days == -1:
        # -1 indicates infinite retention (protected from auto-deletion)
        protect_from_deletion = True if ENABLE_AUTO_DELETION else False

    # Validate naming_template_id if provided
    naming_template_id = data.get('naming_template_id')
    if naming_template_id:
        template = NamingTemplate.query.filter_by(id=naming_template_id, user_id=current_user.id).first()
        if not template:
            return jsonify({'error': 'Naming template not found'}), 404

    folder = Folder(
        name=data['name'],
        user_id=current_user.id,
        group_id=group_id,
        color=data.get('color', '#10B981'),
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

    db.session.add(folder)

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.error(f"Folder creation failed due to integrity constraint: {str(e)}")
        return jsonify({'error': 'A folder with this name already exists'}), 400

    return jsonify(folder.to_dict()), 201


@folders_bp.route('/api/folders/<int:folder_id>', methods=['PUT'])
@login_required
def update_folder(folder_id):
    """Update a folder."""
    # Check if folders feature is enabled
    folders_enabled = SystemSetting.get_setting('enable_folders', False)
    if not folders_enabled:
        return jsonify({'error': 'Folders feature is not enabled'}), 403

    folder = db.session.get(Folder, folder_id)
    if not folder:
        return jsonify({'error': 'Folder not found'}), 404

    # Check permissions
    if folder.group_id:
        # Group folder - user must be a team admin
        membership = GroupMembership.query.filter_by(
            group_id=folder.group_id,
            user_id=current_user.id
        ).first()

        if not membership or membership.role != 'admin':
            return jsonify({'error': 'Only group admins can edit group folders'}), 403
    else:
        # Personal folder - must be the owner
        if folder.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to edit this folder'}), 403

    data = request.get_json()

    if 'name' in data:
        # Check if new name conflicts with another folder
        if folder.group_id:
            existing_folder = Folder.query.filter_by(name=data['name'], group_id=folder.group_id).filter(Folder.id != folder_id).first()
        else:
            existing_folder = Folder.query.filter_by(name=data['name'], user_id=current_user.id).filter(Folder.id != folder_id).first()

        if existing_folder:
            return jsonify({'error': 'Another folder with this name already exists'}), 400
        folder.name = data['name']

    # Handle group_id changes (converting between personal and group folders)
    if 'group_id' in data:
        new_group_id = data['group_id'] if data['group_id'] else None

        # If changing to a group folder, verify user is admin of that group
        if new_group_id:
            membership = GroupMembership.query.filter_by(
                group_id=new_group_id,
                user_id=current_user.id
            ).first()

            if not membership or membership.role != 'admin':
                return jsonify({'error': 'Only group admins can assign folders to groups'}), 403

        folder.group_id = new_group_id

    if 'color' in data:
        folder.color = data['color']
    if 'custom_prompt' in data:
        folder.custom_prompt = data['custom_prompt']
    if 'default_language' in data:
        folder.default_language = data['default_language']
    if 'default_min_speakers' in data:
        folder.default_min_speakers = data['default_min_speakers']
    if 'default_max_speakers' in data:
        folder.default_max_speakers = data['default_max_speakers']

    # Handle retention_days: -1 means protected from deletion
    if 'retention_days' in data:
        retention_days = data['retention_days']

        if retention_days == -1:
            # -1 indicates infinite retention (protected from auto-deletion)
            if ENABLE_AUTO_DELETION:
                folder.protect_from_deletion = True
                folder.retention_days = -1
        else:
            # Regular retention period or null (use global)
            folder.protect_from_deletion = False
            folder.retention_days = retention_days if retention_days else None
    if 'auto_share_on_apply' in data:
        # Only applicable to group folders
        if folder.group_id:
            folder.auto_share_on_apply = bool(data['auto_share_on_apply'])
    if 'share_with_group_lead' in data:
        # Only applicable to group folders
        if folder.group_id:
            folder.share_with_group_lead = bool(data['share_with_group_lead'])
    if 'naming_template_id' in data:
        naming_template_id = data['naming_template_id']
        if naming_template_id:
            template = NamingTemplate.query.filter_by(id=naming_template_id, user_id=current_user.id).first()
            if not template:
                return jsonify({'error': 'Naming template not found'}), 404
        folder.naming_template_id = naming_template_id if naming_template_id else None

    folder.updated_at = datetime.utcnow()

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.error(f"Folder update failed due to integrity constraint: {str(e)}")
        return jsonify({'error': 'A folder with this name already exists'}), 400

    return jsonify(folder.to_dict())


@folders_bp.route('/api/folders/<int:folder_id>', methods=['DELETE'])
@login_required
def delete_folder(folder_id):
    """Delete a folder. Recordings in this folder will have folder_id set to NULL."""
    # Check if folders feature is enabled
    folders_enabled = SystemSetting.get_setting('enable_folders', False)
    if not folders_enabled:
        return jsonify({'error': 'Folders feature is not enabled'}), 403

    folder = db.session.get(Folder, folder_id)
    if not folder:
        return jsonify({'error': 'Folder not found'}), 404

    # Check permissions
    if folder.group_id:
        # Group folder - user must be a team admin
        membership = GroupMembership.query.filter_by(
            group_id=folder.group_id,
            user_id=current_user.id
        ).first()

        if not membership or membership.role != 'admin':
            return jsonify({'error': 'Only group admins can delete group folders'}), 403
    else:
        # Personal folder - must belong to the user
        if folder.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to delete this folder'}), 403

    # Recordings in this folder will have folder_id set to NULL via ondelete='SET NULL'
    db.session.delete(folder)
    db.session.commit()
    return jsonify({'success': True})


@folders_bp.route('/api/groups/<int:group_id>/folders', methods=['POST'])
@login_required
def create_group_folder(group_id):
    """Create a group-scoped folder (group admins only)."""
    # Check if folders feature is enabled
    folders_enabled = SystemSetting.get_setting('enable_folders', False)
    if not folders_enabled:
        return jsonify({'error': 'Folders feature is not enabled'}), 403

    if not ENABLE_INTERNAL_SHARING:
        return jsonify({'error': 'Group folders require internal sharing to be enabled. Please set ENABLE_INTERNAL_SHARING=true in your configuration.'}), 403

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
        return jsonify({'error': 'Only group admins can create group folders'}), 403

    data = request.get_json()
    name = data.get('name', '').strip()

    if not name:
        return jsonify({'error': 'Folder name is required'}), 400

    # Check if a group folder with this name already exists for this team
    existing_folder = Folder.query.filter_by(
        name=name,
        group_id=group_id
    ).first()

    if existing_folder:
        return jsonify({'error': 'A group folder with this name already exists'}), 400

    # Create the group folder with all supported parameters
    folder = Folder(
        name=name,
        user_id=current_user.id,  # Creator
        group_id=group_id,
        color=data.get('color', '#10B981'),
        custom_prompt=data.get('custom_prompt'),
        default_language=data.get('default_language'),
        default_min_speakers=data.get('default_min_speakers'),
        default_max_speakers=data.get('default_max_speakers'),
        protect_from_deletion=data.get('protect_from_deletion', False),
        retention_days=data.get('retention_days'),
        auto_share_on_apply=data.get('auto_share_on_apply', True),  # Default to True for group folders
        share_with_group_lead=data.get('share_with_group_lead', True)  # Default to True for group folders
    )

    db.session.add(folder)

    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.error(f"Folder creation failed due to integrity constraint: {str(e)}")
        return jsonify({'error': 'A folder with this name already exists'}), 400

    return jsonify(folder.to_dict()), 201


@folders_bp.route('/api/groups/<int:group_id>/folders', methods=['GET'])
@login_required
def get_group_folders(group_id):
    """Get all folders for a team (team members only)."""
    # Check if folders feature is enabled
    folders_enabled = SystemSetting.get_setting('enable_folders', False)
    if not folders_enabled:
        return jsonify({'error': 'Folders feature is not enabled'}), 403

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
        return jsonify({'error': 'You must be a team member to view group folders'}), 403

    # Get all group folders
    folders = Folder.query.filter_by(group_id=group_id).all()

    return jsonify({'folders': [folder.to_dict() for folder in folders]})


@folders_bp.route('/api/recordings/<int:recording_id>/folder', methods=['PUT'])
@login_required
def assign_recording_folder(recording_id):
    """Assign a recording to a folder (or move to a different folder)."""
    # Check if folders feature is enabled
    folders_enabled = SystemSetting.get_setting('enable_folders', False)
    if not folders_enabled:
        return jsonify({'error': 'Folders feature is not enabled'}), 403

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    # Check access to recording (require edit permission)
    if has_recording_access:
        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify({'error': 'You do not have permission to modify this recording'}), 403
    else:
        # Fallback: only owner can assign folder
        if recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to modify this recording'}), 403

    data = request.get_json()
    folder_id = data.get('folder_id')

    if folder_id:
        # Verify folder exists and user has access
        folder = db.session.get(Folder, folder_id)
        if not folder:
            return jsonify({'error': 'Folder not found'}), 404

        # Check if user can use this folder
        if folder.group_id:
            # Group folder - user must be a member
            membership = GroupMembership.query.filter_by(
                group_id=folder.group_id,
                user_id=current_user.id
            ).first()
            if not membership:
                return jsonify({'error': 'You do not have access to this folder'}), 403
        else:
            # Personal folder - must be owner
            if folder.user_id != current_user.id:
                return jsonify({'error': 'You do not have access to this folder'}), 403

        # Handle auto-sharing for group folders
        old_folder_id = recording.folder_id
        recording.folder_id = folder_id

        # Apply auto-shares if moving to a group folder
        if folder.group_id and (folder.auto_share_on_apply or folder.share_with_group_lead):
            _apply_folder_auto_shares(recording, folder)

        db.session.commit()
        current_app.logger.info(f"Recording {recording_id} moved to folder {folder_id} by user {current_user.id}")
    else:
        # Remove from folder
        recording.folder_id = None
        db.session.commit()
        current_app.logger.info(f"Recording {recording_id} removed from folder by user {current_user.id}")

    return jsonify(recording.to_dict(include_html=False, viewer_user=current_user))


@folders_bp.route('/api/recordings/<int:recording_id>/folder', methods=['DELETE'])
@login_required
def remove_recording_folder(recording_id):
    """Remove a recording from its folder."""
    # Check if folders feature is enabled
    folders_enabled = SystemSetting.get_setting('enable_folders', False)
    if not folders_enabled:
        return jsonify({'error': 'Folders feature is not enabled'}), 403

    recording = db.session.get(Recording, recording_id)
    if not recording:
        return jsonify({'error': 'Recording not found'}), 404

    # Check access to recording (require edit permission)
    if has_recording_access:
        if not has_recording_access(recording, current_user, require_edit=True):
            return jsonify({'error': 'You do not have permission to modify this recording'}), 403
    else:
        # Fallback: only owner can remove folder
        if recording.user_id != current_user.id:
            return jsonify({'error': 'You do not have permission to modify this recording'}), 403

    recording.folder_id = None
    db.session.commit()
    current_app.logger.info(f"Recording {recording_id} removed from folder by user {current_user.id}")

    return jsonify({'success': True})


@folders_bp.route('/api/recordings/bulk/folder', methods=['POST'])
@login_required
def bulk_assign_folder():
    """Assign multiple recordings to a folder."""
    # Check if folders feature is enabled
    folders_enabled = SystemSetting.get_setting('enable_folders', False)
    if not folders_enabled:
        return jsonify({'error': 'Folders feature is not enabled'}), 403

    data = request.get_json()
    recording_ids = data.get('recording_ids', [])
    folder_id = data.get('folder_id')  # Can be None to remove from folder

    if not recording_ids:
        return jsonify({'error': 'No recordings specified'}), 400

    # Verify folder if specified
    folder = None
    if folder_id:
        folder = db.session.get(Folder, folder_id)
        if not folder:
            return jsonify({'error': 'Folder not found'}), 404

        # Check if user can use this folder
        if folder.group_id:
            membership = GroupMembership.query.filter_by(
                group_id=folder.group_id,
                user_id=current_user.id
            ).first()
            if not membership:
                return jsonify({'error': 'You do not have access to this folder'}), 403
        else:
            if folder.user_id != current_user.id:
                return jsonify({'error': 'You do not have access to this folder'}), 403

    updated_count = 0
    for rec_id in recording_ids:
        recording = db.session.get(Recording, rec_id)
        if not recording:
            continue

        # Check access (require edit permission)
        if has_recording_access:
            if not has_recording_access(recording, current_user, require_edit=True):
                continue
        else:
            if recording.user_id != current_user.id:
                continue

        recording.folder_id = folder_id

        # Apply auto-shares if moving to a group folder
        if folder and folder.group_id and (folder.auto_share_on_apply or folder.share_with_group_lead):
            _apply_folder_auto_shares(recording, folder)

        updated_count += 1

    db.session.commit()
    action = f"moved to folder {folder_id}" if folder_id else "removed from folder"
    current_app.logger.info(f"Bulk folder update: {updated_count} recordings {action} by user {current_user.id}")

    return jsonify({'success': True, 'updated_count': updated_count})


def _apply_folder_auto_shares(recording, folder):
    """
    Apply auto-shares for a group folder when a recording is assigned to it.

    Args:
        recording: Recording being assigned to the folder
        folder: Folder with auto-share settings
    """
    if not ENABLE_INTERNAL_SHARING:
        return

    if not folder.group_id:
        return

    # Determine who to share with
    if folder.auto_share_on_apply:
        group_members = GroupMembership.query.filter_by(group_id=folder.group_id).all()
    elif folder.share_with_group_lead:
        group_members = GroupMembership.query.filter_by(group_id=folder.group_id, role='admin').all()
    else:
        return

    shares_created = 0

    for membership in group_members:
        # Skip the recording owner
        if membership.user_id == recording.user_id:
            continue

        # Check if already shared
        existing_share = InternalShare.query.filter_by(
            recording_id=recording.id,
            shared_with_user_id=membership.user_id
        ).first()

        if not existing_share:
            # Create internal share with correct permissions
            share = InternalShare(
                recording_id=recording.id,
                owner_id=recording.user_id,
                shared_with_user_id=membership.user_id,
                can_edit=(membership.role == 'admin'),
                can_reshare=False,
                source_type='group_folder',
                source_tag_id=None  # We don't use this field for folders
            )
            db.session.add(share)

            # Create SharedRecordingState with default values for the recipient
            state = SharedRecordingState(
                recording_id=recording.id,
                user_id=membership.user_id,
                is_inbox=True,
                is_highlighted=False
            )
            db.session.add(state)

            shares_created += 1
            current_app.logger.info(f"Auto-shared recording {recording.id} with user {membership.user_id} via group folder '{folder.name}'")

    if shares_created > 0:
        current_app.logger.info(f"Created {shares_created} auto-shares for recording {recording.id} via folder assignment")
