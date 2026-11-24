"""
Administrative functions and user management.

This blueprint was auto-generated from app.py route extraction.
"""

import os
import json
from datetime import datetime, timedelta

import httpx
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response, current_app
from flask_login import login_required, current_user, login_user

from src.database import db
from src.models import *
from src.utils import *
from src.utils.route_errors import internal_error
from src.utils.localization import translate, api_error
from src.utils.request_body import (DB_INT_MAX, flag_fields, id_args_error, int_args_error, is_int,
                                    json_object_body)
from src.utils.search import contains_pattern, LIKE_ESCAPE
from src.services.retention import (is_recording_exempt_from_deletion, get_retention_days_for_recording,
                                    process_auto_deletion)
from src.services.embeddings import EMBEDDINGS_AVAILABLE, process_recording_chunks
from src.services.token_tracking import token_tracker
from src.services.transcription_tracking import transcription_tracker
from src.services.storage_quota import default_quota_mb
from src.services import audit, llm_config, login_lock
from src.auth.permissions import CREATE_GROUPS, set_user_grant, user_has_grant
from src.config.startup import get_file_monitor_functions
from src.utils.error_text import describe_error

# Create blueprint
admin_bp = Blueprint('admin', __name__)

# Configuration from environment
ENABLE_INQUIRE_MODE = os.environ.get('ENABLE_INQUIRE_MODE', 'false').lower() == 'true'

# Global helpers (will be injected from app)
has_recording_access = None
bcrypt = None
csrf = None
limiter = None

def init_admin_helpers(**kwargs):
    """Initialize helper functions and extensions from app."""
    global has_recording_access, bcrypt, csrf, limiter
    has_recording_access = kwargs.get('has_recording_access')
    bcrypt = kwargs.get('bcrypt')
    csrf = kwargs.get('csrf')
    limiter = kwargs.get('limiter')



# --- Routes ---

@admin_bp.route('/admin', methods=['GET'])
@login_required
def admin():
    # Check if user is admin OR group admin
    is_team_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    from src.api.groups import can_create_groups
    may_create_groups = not current_user.is_admin and can_create_groups(current_user)
    if not current_user.is_admin and not is_team_admin and not may_create_groups:
        flash(translate('server.flash.adminAccessDenied'), 'danger')
        return redirect(url_for('recordings.index'))

    # Redirect group admins, and users who may create groups, to their
    # dedicated management page
    if not current_user.is_admin:
        return redirect(url_for('admin.group_management'))

    # Full admins only get here
    user_language = current_user.ui_language if current_user.is_authenticated and current_user.ui_language else 'en'
    return render_template('admin.html',
                         title='Admin Dashboard',
                         admin_title=translate('nav.adminDashboard', user_language),
                         groups_title=translate('nav.groupManagement', user_language),
                         inquire_mode_enabled=ENABLE_INQUIRE_MODE,
                         global_retention_days=_self.GLOBAL_RETENTION_DAYS,
                         is_group_admin_only=False,
                         user_language=user_language)


@admin_bp.route('/group-management', methods=['GET'])
@login_required
def group_management():
    """Dedicated group management page for group admins (non-full admins)
    and for users who may create groups (#576)."""
    # Check if user is a group admin
    is_team_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    from src.api.groups import can_create_groups
    if not is_team_admin and not current_user.is_admin and not can_create_groups(current_user):
        flash(translate('server.flash.groupAccessDenied'), 'danger')
        return redirect(url_for('recordings.index'))

    # If they're a full admin, redirect to main admin dashboard
    if current_user.is_admin:
        return redirect(url_for('admin.admin'))

    user_language = current_user.ui_language if current_user.is_authenticated and current_user.ui_language else 'en'
    return render_template('group-admin.html',
                         title='Group Management',
                         groups_title=translate('nav.groupManagement', user_language),
                         global_retention_days=_self.GLOBAL_RETENTION_DAYS,
                         user_language=user_language)



def _text_order(column):
    """Order by a text column without regard to case, the same on SQLite and PostgreSQL."""
    def build(descending, _scope):
        lowered = db.func.lower(column)
        return (lowered.desc() if descending else lowered.asc(),), None
    return build


def _flag_order(column):
    """Order by a boolean column; a NULL counts as off, as it is displayed."""
    def build(descending, _scope):
        flag = db.case((column.is_(True), 1), else_=0)
        return (flag.desc() if descending else flag.asc(),), None
    return build


def _id_order(_descending, _scope):
    # The id closes every order, in the requested direction; nothing precedes it here.
    return (), None


def _last_active_order(descending, _scope):
    # A CASE, not NULLS FIRST/LAST: the same order on SQLite and PostgreSQL.
    # Accounts that were never used come first in ascending order (least
    # recently used first) and last in descending order.
    never = db.case((User.last_seen_at.is_(None), 0), else_=1)
    if descending:
        return (never.desc(), User.last_seen_at.desc()), None
    return (never.asc(), User.last_seen_at.asc()), None


def _recording_total_order(aggregate, live_audio_only=False):
    """Order by a total over the recordings of each user.

    The totals are read from one grouped subquery, joined to the user list
    with a LEFT JOIN so that a user without recordings has the total 0. The
    expressions are those of the displayed numbers, so the order and the
    display agree. For a group admin the subquery is limited to the users in
    scope.
    """
    def build(descending, scope):
        totals = db.session.query(
            Recording.user_id.label('user_id'), aggregate.label('total'))
        if live_audio_only:
            totals = totals.filter(Recording.audio_deleted_at.is_(None))
        if scope is not None:
            totals = totals.filter(Recording.user_id.in_(scope))
        totals = totals.group_by(Recording.user_id).subquery('user_recording_totals')
        total = db.func.coalesce(totals.c.total, 0)
        return (total.desc() if descending else total.asc(),), totals
    return build


# The sort keys of GET /admin/users. Each value builds the leading ORDER BY
# expressions and, for the totals, the subquery to join.
USER_LIST_SORTS = {
    'id': _id_order,
    'username': _text_order(User.username),
    'email': _text_order(User.email),
    'is_admin': _flag_order(User.is_admin),
    'can_share_publicly': _flag_order(User.can_share_publicly),
    'recordings_count': _recording_total_order(db.func.count(Recording.id)),
    'storage_used': _recording_total_order(db.func.sum(Recording.file_size), live_audio_only=True),
    'last_active': _last_active_order,
}
# Keys honoured for site admins only, who are the only ones given the value.
USER_LIST_SITE_ADMIN_SORTS = frozenset({'last_active'})


def _user_list_sort(scope=None):
    """The sort of the user list, from ?sort= and ?order=.

    Returns (key, order, order_by, joined): the applied key and direction,
    the ORDER BY expressions and the subquery to LEFT JOIN on its user_id, or
    None. An unknown key, and a key reserved for site admins when requested by
    a group admin, give the order by id. Ties are broken by ascending id, so
    that pages are stable. ``scope`` holds the user ids visible to a group
    admin, None for a site admin.
    """
    descending = (request.args.get('order') or '').lower() == 'desc'
    key = (request.args.get('sort') or '').lower()
    if key not in USER_LIST_SORTS or (
            key in USER_LIST_SITE_ADMIN_SORTS and not current_user.is_admin):
        key = 'id'
    leading, joined = USER_LIST_SORTS[key](descending, scope)
    if key == 'id':
        closing = User.id.desc() if descending else User.id.asc()
    else:
        closing = User.id.asc()
    return key, 'desc' if descending else 'asc', leading + (closing,), joined


@admin_bp.route('/admin/users', methods=['GET'])
@login_required
def admin_get_users():
    # Check if user is admin OR group admin
    is_team_admin = GroupMembership.query.filter_by(
        user_id=current_user.id,
        role='admin'
    ).first() is not None

    if not current_user.is_admin and not is_team_admin:
        return jsonify(api_error('unauthorized')), 403

    member_ids = None
    if current_user.is_admin:
        base_query = User.query
    else:
        # Group admin (not a site admin): scope to members of the groups this
        # user actually administers. Returning User.query.all() here leaked the
        # entire user directory — every user's email, admin flag, and token /
        # storage budgets — to anyone who administers any single group.
        admined_group_ids = [
            gm.group_id for gm in GroupMembership.query.filter_by(
                user_id=current_user.id, role='admin').all()
        ]
        member_ids = {
            row[0] for row in db.session.query(GroupMembership.user_id)
            .filter(GroupMembership.group_id.in_(admined_group_ids)).all()
        }
        member_ids.add(current_user.id)
        base_query = User.query.filter(User.id.in_(member_ids))

    # Parse pagination params (normalize gracefully).
    page_raw = request.args.get('page', 1, type=int)
    try:
        page = int(page_raw) if page_raw is not None else 1
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1
    # Clamped like per_page: a page past the column range is an empty page,
    # not an offset that overflows the query (#664)
    page = min(page, DB_INT_MAX)

    # Cap kept high enough for admin tooling/exports while still bounding
    # response size and query cost. Out-of-range values are clamped to the
    # nearest bound instead of silently falling back to the default.
    MAX_PER_PAGE = 1000
    per_page_raw = request.args.get('per_page', 15, type=int)
    try:
        per_page = int(per_page_raw) if per_page_raw is not None else 15
    except (TypeError, ValueError):
        per_page = 15
    if per_page < 1:
        per_page = 15
    elif per_page > MAX_PER_PAGE:
        per_page = MAX_PER_PAGE

    search = (request.args.get('search') or '').strip()

    if search:
        pattern = contains_pattern(search)
        base_query = base_query.filter(db.or_(
            User.username.ilike(pattern, escape=LIKE_ESCAPE),
            User.email.ilike(pattern, escape=LIKE_ESCAPE),
        ))

    total = base_query.count()

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1
    if total_pages < 1:
        total_pages = 1
    if page > total_pages:
        page = total_pages
    if page < 1:
        page = 1

    # The totals are joined for the order only, and only when a total is the
    # sort key; the count above and the other orders read the user table alone.
    sort_key, sort_order, order_by, joined = _user_list_sort(member_ids)
    page_query = base_query
    if joined is not None:
        page_query = page_query.outerjoin(joined, joined.c.user_id == User.id)
    users = (page_query.order_by(*order_by)
             .offset((page - 1) * per_page).limit(per_page).all())

    user_data = []
    # Only a site admin can unlock an account, so only a site admin sees the lock (#529)
    locked_ids = login_lock.locked_user_ids([u.id for u in users]) if current_user.is_admin else set()

    # Aggregate recordings_count and storage_used for the users on the current
    # page only, to keep the page request cheap.
    page_ids = [u.id for u in users]
    if page_ids:
        counts_by_uid = dict(
            db.session.query(Recording.user_id, db.func.count(Recording.id))
            .filter(Recording.user_id.in_(page_ids))
            .group_by(Recording.user_id)
            .all()
        )
        sizes_by_uid = dict(
            db.session.query(Recording.user_id, db.func.sum(Recording.file_size))
            .filter(Recording.user_id.in_(page_ids))
            .filter(Recording.audio_deleted_at.is_(None))
            .group_by(Recording.user_id)
            .all()
        )
    else:
        counts_by_uid = {}
        sizes_by_uid = {}

    # Individual create_groups grants, for the site admin's edit dialog (#576)
    create_groups_ids = set()
    if page_ids and current_user.is_admin:
        create_groups_ids = {
            row[0] for row in db.session.query(PermissionGrant.user_id).filter(
                PermissionGrant.permission == CREATE_GROUPS,
                PermissionGrant.user_id.in_(page_ids)).all()
        }

    for user in users:
        recordings_count = counts_by_uid.get(user.id, 0)
        storage_used = sizes_by_uid.get(user.id) or 0

        # Get current month token usage
        current_usage = token_tracker.get_monthly_usage(user.id)
        usage_percentage = (current_usage / user.monthly_token_budget * 100) if user.monthly_token_budget else 0

        # Get current month transcription usage
        current_transcription_usage = transcription_tracker.get_monthly_usage(user.id)
        transcription_usage_percentage = (current_transcription_usage / user.monthly_transcription_budget * 100) if user.monthly_transcription_budget else 0

        user_data.append({
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'is_admin': user.is_admin,
            'can_share_publicly': user.can_share_publicly,
            'recordings_count': recordings_count,
            'storage_used': storage_used,
            'storage_quota_mb': user.storage_quota_mb,
            'monthly_token_budget': user.monthly_token_budget,
            'current_token_usage': current_usage,
            'token_usage_percentage': round(usage_percentage, 1),
            'monthly_transcription_budget': user.monthly_transcription_budget,
            'monthly_transcription_budget_minutes': (user.monthly_transcription_budget // 60) if user.monthly_transcription_budget else None,
            'current_transcription_usage': current_transcription_usage,
            'current_transcription_usage_minutes': current_transcription_usage // 60,
            'transcription_usage_percentage': round(transcription_usage_percentage, 1),
            'login_locked': user.id in locked_ids,
            'create_groups_granted': user.id in create_groups_ids,
        })
        if current_user.is_admin:
            # The time of the last use (src/services/last_active.py), null
            # when there was none. For site admins only. Stored as naive UTC
            # like every other timestamp; the designator makes it an
            # unambiguous instant for the <time datetime> attribute.
            user_data[-1]['last_seen_at'] = (
                user.last_seen_at.isoformat() + 'Z' if user.last_seen_at else None)

    has_next = page < total_pages
    has_prev = page > 1

    return jsonify({
        'users': user_data,
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'total_pages': total_pages,
            'has_next': has_next,
            'has_prev': has_prev
        },
        # The sort that was applied, which differs from the requested one for
        # an unknown key and for a key reserved for site admins.
        'sort': sort_key,
        'order': sort_order,
    })



@admin_bp.route('/admin/users', methods=['POST'])
@login_required
def admin_add_user():
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    data, body_error = json_object_body()
    if body_error:
        return body_error
    if not data:
        return jsonify(api_error('no_data_provided')), 400
    
    # Validate required fields
    required_fields = ['username', 'email', 'password']
    for field in required_fields:
        if field not in data:
            return jsonify(api_error('missing_required_field', field=field)), 400
    account_error = _account_fields_error(data)
    if account_error:
        return account_error
    flags, flag_error = flag_fields(data, ('is_admin',))
    if flag_error:
        return flag_error
    
    # The username column holds 20 characters; PostgreSQL refuses a longer
    # value, which gave a 500 instead of this message
    if not 2 <= len(str(data['username']).strip()) <= 20:
        return jsonify(api_error('invalid_username_length')), 400

    # Check if username or email already exists
    if User.query.filter_by(username=data['username']).first():
        return jsonify(api_error('username_exists')), 400
    
    email = User.normalize_email(data['email'])
    if User.find_by_email(email):
        return jsonify(api_error('email_exists')), 400

    try:
        storage_quota = _quota_mb(data['storage_quota_mb']) if 'storage_quota_mb' in data else default_quota_mb()
    except (TypeError, ValueError):
        return jsonify(api_error('invalid_storage_quota')), 400
    budgets, bad_budget = _budget_fields(data)
    if bad_budget:
        return bad_budget

    # Create new user
    hashed_password = bcrypt.generate_password_hash(data['password']).decode('utf-8')
    new_user = User(
        username=data['username'],
        email=email,
        password=hashed_password,
        is_admin=flags.get('is_admin', False),
        monthly_token_budget=budgets.get('monthly_token_budget'),
        monthly_transcription_budget=budgets.get('monthly_transcription_budget'),
        storage_quota_mb=storage_quota,
    )
    from src.services.user_defaults import apply_new_user_defaults
    apply_new_user_defaults(new_user)

    db.session.add(new_user)
    db.session.flush()
    # The username is stored here and at the deletion of the account, and on
    # no other row, so that a row can be attributed after the account is gone.
    audit.record_in_tx('admin.user.created', target_user_id=new_user.id,
                       details={'username': new_user.username, 'is_admin': bool(new_user.is_admin)})
    db.session.commit()

    return jsonify({
        'id': new_user.id,
        'username': new_user.username,
        'email': new_user.email,
        'is_admin': new_user.is_admin,
        'recordings_count': 0,
        'storage_used': 0,
        'storage_quota_mb': new_user.storage_quota_mb,
        'monthly_token_budget': new_user.monthly_token_budget,
        'current_token_usage': 0,
        'token_usage_percentage': 0,
        'monthly_transcription_budget': new_user.monthly_transcription_budget,
        'monthly_transcription_budget_minutes': (new_user.monthly_transcription_budget // 60) if new_user.monthly_transcription_budget else None,
        'current_transcription_usage': 0,
        'current_transcription_usage_minutes': 0,
        'transcription_usage_percentage': 0
    }), 201



@admin_bp.route('/admin/users/<int:user_id>', methods=['PUT'])
@login_required
def admin_update_user(user_id):
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    user = db.session.get(User, user_id)
    if not user:
        return jsonify(api_error('user_not_found')), 404
    
    data, body_error = json_object_body()
    if body_error:
        return body_error
    if not data:
        return jsonify(api_error('no_data_provided')), 400
    # Every field is checked before the first change (#687)
    account_error = _account_fields_error(data)
    if account_error:
        return account_error
    flags, flag_error = flag_fields(data, ('is_admin', 'can_share_publicly', 'create_groups_granted'))
    if flag_error:
        return flag_error
    budgets, bad_budget = _budget_fields(data)
    if bad_budget:
        return bad_budget
    if 'storage_quota_mb' in data:
        try:
            _quota_mb(data['storage_quota_mb'])
        except (TypeError, ValueError):
            return jsonify(api_error('invalid_storage_quota')), 400

    audited_fields = ('username', 'email', 'can_share_publicly', 'monthly_token_budget',
                      'monthly_transcription_budget', 'storage_quota_mb')
    before = {name: getattr(user, name) for name in audited_fields}
    before['create_groups_granted'] = user_has_grant(CREATE_GROUPS, user.id)
    was_admin = bool(user.is_admin)

    # Update user fields
    if 'username' in data and data['username'] != user.username:
        if not 2 <= len(str(data['username']).strip()) <= 20:
            return jsonify(api_error('invalid_username_length')), 400
        # Check if username already exists
        if User.query.filter_by(username=data['username']).first():
            return jsonify(api_error('username_exists')), 400
        user.username = data['username']
    
    if 'email' in data:
        new_email = User.normalize_email(data['email'])
        if new_email != user.email:
            # Case-insensitive existence check against other accounts.
            existing = User.find_by_email(new_email)
            if existing and existing.id != user.id:
                return jsonify(api_error('email_exists')), 400
            user.email = new_email
    
    if 'password' in data and data['password']:
        user.password = bcrypt.generate_password_hash(data['password']).decode('utf-8')
        # Sign the account out of every browser (#437)
        user.end_all_sessions()
        # A new password ends any login lock on the account (#529)
        login_lock.clear_failures(user.id)

    if data.get('unlock_login') is True:
        login_lock.clear_failures(user.id)

    if 'is_admin' in data:
        new_is_admin = flags['is_admin']
        # Guard against demoting the last remaining admin, which would lock
        # everyone out of the admin panel with no in-app recovery.
        if user.is_admin and not new_is_admin:
            other_admins = User.query.filter(User.is_admin.is_(True), User.id != user.id).count()
            if other_admins == 0:
                return jsonify(api_error('last_admin_demotion')), 400
        user.is_admin = new_is_admin

    if 'can_share_publicly' in data:
        user.can_share_publicly = flags['can_share_publicly']

    if 'create_groups_granted' in data:
        set_user_grant(CREATE_GROUPS, user.id, flags['create_groups_granted'])

    # None, '' or 0 is no limit; the transcription budget is in seconds
    budgets, bad_budget = _budget_fields(data)
    if bad_budget:
        db.session.rollback()
        return bad_budget
    for field, value in budgets.items():
        setattr(user, field, value)

    if 'storage_quota_mb' in data:
        try:
            user.storage_quota_mb = _quota_mb(data['storage_quota_mb'])
        except (TypeError, ValueError):
            return jsonify(api_error('invalid_storage_quota')), 400

    # The names of the fields that changed, never their values.
    changed = [name for name in audited_fields if getattr(user, name) != before[name]]
    if 'create_groups_granted' in data and flags['create_groups_granted'] != before['create_groups_granted']:
        changed.append('create_groups_granted')
    if changed:
        audit.record_in_tx('admin.user.changed', target_user_id=user.id, details={'fields': sorted(changed)})
    if 'password' in data and data['password']:
        audit.record_in_tx('admin.user.password_set', target_user_id=user.id,
                           details={'self': user.id == current_user.id})
    if data.get('unlock_login') is True:
        audit.record_in_tx('admin.user.unlocked', target_user_id=user.id)
    if bool(user.is_admin) != was_admin:
        audit.record_in_tx('admin.user.role_changed', target_user_id=user.id,
                           details={'old': was_admin, 'new': bool(user.is_admin)})
    db.session.commit()
    if user.id == current_user.id and data.get('password'):
        # An administrator who sets their own password stays signed in here
        remembered = current_app.config.get('REMEMBER_COOKIE_NAME', 'remember_token') in request.cookies
        login_user(user, remember=remembered)

    # Get recordings count and storage used
    recordings_count = len(user.recordings)
    storage_used = sum(r.file_size for r in user.recordings if r.file_size and not r.audio_deleted_at) or 0

    # Get current month token usage
    current_usage = token_tracker.get_monthly_usage(user.id)
    usage_percentage = (current_usage / user.monthly_token_budget * 100) if user.monthly_token_budget else 0

    # Get current month transcription usage
    current_transcription_usage = transcription_tracker.get_monthly_usage(user.id)
    transcription_usage_percentage = (current_transcription_usage / user.monthly_transcription_budget * 100) if user.monthly_transcription_budget else 0

    return jsonify({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'is_admin': user.is_admin,
        'can_share_publicly': user.can_share_publicly,
        'create_groups_granted': user_has_grant(CREATE_GROUPS, user.id),
        'recordings_count': recordings_count,
        'storage_used': storage_used,
        'storage_quota_mb': user.storage_quota_mb,
        'monthly_token_budget': user.monthly_token_budget,
        'current_token_usage': current_usage,
        'token_usage_percentage': round(usage_percentage, 1),
        'monthly_transcription_budget': user.monthly_transcription_budget,
        'monthly_transcription_budget_minutes': (user.monthly_transcription_budget // 60) if user.monthly_transcription_budget else None,
        'current_transcription_usage': current_transcription_usage,
        'current_transcription_usage_minutes': current_transcription_usage // 60,
        'transcription_usage_percentage': round(transcription_usage_percentage, 1),
        'login_locked': bool(login_lock.locked_user_ids([user.id]))
    })



@admin_bp.route('/admin/users/<int:user_id>', methods=['DELETE'])
@login_required
@audit.emits('admin.user.deleted', 'recording.deleted')
def admin_delete_user(user_id):
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    # Prevent deleting self
    if user_id == current_user.id:
        return jsonify(api_error('cannot_delete_self')), 400
    
    user = db.session.get(User, user_id)
    if not user:
        return jsonify(api_error('user_not_found')), 404
    
    # Delete user's recordings and audio files
    total_chunks = 0
    if ENABLE_INQUIRE_MODE:
        total_chunks = TranscriptChunk.query.filter_by(user_id=user_id).count()
        if total_chunks > 0:
            current_app.logger.info(f"Deleting {total_chunks} transcript chunks with embeddings for user {user_id}")
    # The whole recording goes, as when its owner deletes it: the media, the
    # transcript, summary, notes and embeddings, its shares, tags and jobs.
    # Deleting only the media left the rows with user_id NULL (#572).
    from src.services.storage import get_storage_service
    from src.services.recording_deletion import delete_recording_completely, cleanup_orphaned_speakers_quietly
    storage = get_storage_service()
    recordings = list(user.recordings)  # the helper commits after each one
    for recording in recordings:
        delete_recording_completely(recording, storage=storage, audit_reason='account_deleted')
    if recordings:
        current_app.logger.info(f"Deleted {len(recordings)} recordings of user {user_id}")

    # Explicitly delete related records that might not cascade properly
    ProcessingJob.query.filter_by(user_id=user_id).delete()
    InternalShare.query.filter(
        (InternalShare.owner_id == user_id) | (InternalShare.shared_with_user_id == user_id)
    ).delete()

    # Rows of the former share_audit_log table that name the account as the
    # target of a share. Their foreign key has no ON DELETE, so on PostgreSQL
    # one such row refused the deletion. The rows were copied to the audit
    # log (ledger id 0009), where they remain.
    from src.models import ShareAuditLog
    ShareAuditLog.query.filter_by(target_user_id=user_id).delete(synchronize_session=False)

    # Summaries this user generated on other users' recordings: the stored
    # prompt was theirs alone, and their id may be given to a new account.
    for other_recording in Recording.query.filter_by(summary_prompt_user_id=user_id).all():
        other_recording.clear_summary_prompt_used()

    # Groups the user created keep existing without a creator (#576)
    Group.query.filter_by(created_by=user_id).update({'created_by': None}, synchronize_session=False)

    audit.record_in_tx('admin.user.deleted', target_user_id=user_id,
                       details={'username': user.username, 'recordings_deleted': len(recordings)})
    # Delete user (cascade will handle remaining related data including chunks/embeddings)
    db.session.delete(user)
    db.session.commit()
    if recordings:
        cleanup_orphaned_speakers_quietly()
    
    if ENABLE_INQUIRE_MODE and total_chunks > 0:
        current_app.logger.info(f"Successfully deleted {total_chunks} embeddings and chunks for user {user_id}")
    
    return jsonify({'success': True})



@admin_bp.route('/admin/users/<int:user_id>/toggle-admin', methods=['POST'])
@login_required
def admin_toggle_admin(user_id):
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    # Prevent changing own admin status
    if user_id == current_user.id:
        return jsonify(api_error('cannot_change_own_admin')), 400
    
    user = db.session.get(User, user_id)
    if not user:
        return jsonify(api_error('user_not_found')), 404

    # Guard against demoting the last remaining admin (defence in depth; the
    # self-status check above already prevents the acting admin demoting itself).
    if user.is_admin:
        other_admins = User.query.filter(User.is_admin.is_(True), User.id != user.id).count()
        if other_admins == 0:
            return jsonify(api_error('last_admin_demotion')), 400

    # Toggle admin status
    user.is_admin = not user.is_admin
    audit.record_in_tx('admin.user.role_changed', target_user_id=user.id,
                       details={'old': not user.is_admin, 'new': bool(user.is_admin)})
    db.session.commit()

    return jsonify({'success': True, 'is_admin': user.is_admin})



def _limit_value(value):
    """A limit from the admin form or API: a whole number of 0 or more, or None
    for no limit (None, '', 0 and '0'). Raises ValueError for anything else,
    such as 'abc', '1.5', -1, True or a list (#651)."""
    if value is None or isinstance(value, bool):
        if value is None:
            return None
        raise ValueError('a limit is a whole number')
    if isinstance(value, str):
        value = value.strip()
        if value == '':
            return None
        if not value.isdigit():
            raise ValueError('a limit is a whole number')
        value = int(value)
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    elif not isinstance(value, int):
        raise ValueError('a limit is a whole number')
    if value < 0:
        raise ValueError('a limit is not negative')
    # The budget and quota columns are Integer (#664)
    if value > DB_INT_MAX:
        raise ValueError('a limit fits an Integer column')
    return value or None


def _quota_mb(value):
    """A storage quota from the admin form: a positive number of MB, or no limit."""
    return _limit_value(value)


def _account_fields_error(data):
    """The 400 for a username, email or password of the admin user form
    that is not a string, or an email longer than its column, else None
    (#687). A value of another type reached the database or bcrypt."""
    for field in ('username', 'email', 'password'):
        if field in data and not isinstance(data[field], str):
            if field == 'password' and data[field] is None:
                continue  # no new password
            return jsonify(api_error('field_must_be_string', field=field)), 400
    if len((data.get('email') or '').strip()) > 120:
        return jsonify(api_error('field_too_long', field='email', max=120)), 400
    return None


def _budget_fields(data):
    """The budget fields present in a request, checked: ({field: value}, None),
    or (None, the 400 response) for the first invalid one."""
    values = {}
    for field in ('monthly_token_budget', 'monthly_transcription_budget'):
        if field in data:
            try:
                values[field] = _limit_value(data[field])
            except ValueError:
                code = 'invalid_token_budget' if field == 'monthly_token_budget' else 'invalid_transcription_budget'
                return None, (jsonify(api_error(code)), 400)
    return values, None


# --- Audit log ---------------------------------------------------------------
#
# Read-only. Site admins only: a session or a full API token of an admin.
# A scoped token does not reach these routes (they carry no scope mark).
# Nothing here updates or deletes a row of audit_event.

AUDIT_LOG_NOTICE_SETTING = 'audit_log_notice_dismissed'
# The longest wait for the writer of this process before a read of the table.
AUDIT_PAGE_FLUSH_SECONDS = 0.25
AUDIT_EXPORT_FLUSH_SECONDS = 1.0


def _audit_datetime(value):
    """A date or an ISO 8601 date-time as naive UTC."""
    from src.utils.dates import to_utc_naive
    value = value.strip()
    if len(value) == 10:
        return datetime.strptime(value, '%Y-%m-%d')
    return to_utc_naive(datetime.fromisoformat(value.replace('Z', '+00:00')))


def _audit_filters():
    """(filters, names of the filters in use) from the query string.

    Raises ValueError with an API error code for a value that cannot be read.
    """
    args = request.args
    filters, used = {}, []
    for name, key in (('from', 'date_from'), ('to', 'date_to')):
        raw = (args.get(name) or '').strip()
        if raw:
            try:
                filters[key] = _audit_datetime(raw)
            except (ValueError, TypeError):
                raise ValueError('invalid_filter_date')
            used.append(name)
    user = (args.get('user') or '').strip()
    if user:
        if user.isdigit():
            filters['user_id'] = int(user)
        else:
            row = User.query.filter(db.func.lower(User.username) == user.lower()).first()
            # An unknown name matches nothing.
            filters['user_id'] = row.id if row is not None else -1
        used.append('user')
    recording = (args.get('recording_id') or '').strip()
    if recording:
        if not recording.isdigit():
            raise ValueError('invalid_audit_filter')
        filters['recording_id'] = int(recording)
        used.append('recording_id')
    for name, key, allowed in (('group', 'event_group', audit.EVENT_GROUPS),
                               ('event', 'event', audit.EVENTS),
                               ('outcome', 'outcome', audit.OUTCOMES),
                               ('actor_kind', 'actor_kind', audit.ACTOR_KINDS)):
        raw = (args.get(name) or '').strip()
        if raw:
            if raw not in allowed:
                raise ValueError('invalid_audit_filter')
            filters[key] = raw
            used.append(name)
    return filters, used


def _csv_cell(value):
    """A text cell that a spreadsheet would read as a formula (a user agent
    that starts with =, +, - or @) gets a leading apostrophe."""
    if isinstance(value, str) and value[:1] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + value
    return value


def _audit_usernames(rows):
    """id -> username for the accounts named in the rows that still exist."""
    ids = set()
    for row in rows:
        ids.update(v for v in (row['actor_user_id'], row['target_user_id'], row['recording_owner_id'])
                   if v is not None)
    if not ids:
        return {}
    return {str(user_id): name for user_id, name in
            db.session.query(User.id, User.username).filter(User.id.in_(ids))}


@admin_bp.route('/admin/audit-log', methods=['GET'])
@login_required
def admin_audit_log():
    """One page of the audit log, newest first.

    Query: from, to (a date or an ISO 8601 date-time, UTC), user (id or
    username; actor or target), group, event, recording_id, outcome,
    actor_kind, before_id or after_id, limit (1 to 500, default 50).

    Events queued in this process are written before the table is read.
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    arg_error = id_args_error(('user', 'recording_id', 'before_id', 'after_id'), code='invalid_audit_filter')
    if arg_error:
        return arg_error
    try:
        filters, used = _audit_filters()
        before_id = request.args.get('before_id', type=int)
        after_id = request.args.get('after_id', type=int)
        limit = int(request.args.get('limit', 50))
    except ValueError as error:
        code = str(error) if str(error) in ('invalid_filter_date', 'invalid_audit_filter') else 'invalid_limit'
        return jsonify(api_error(code)), 400
    if not 1 <= limit <= 500:
        return jsonify(api_error('invalid_limit')), 400

    # The wait ends with the timeout when the writer is busy; the page then
    # holds the rows written so far.
    audit.flush(timeout=AUDIT_PAGE_FLUSH_SECONDS)
    rows, has_older, has_newer = audit.page(filters, before_id=before_id, after_id=after_id, limit=limit)
    # Reading the log is an event of the log: once per half hour and admin.
    audit.record('audit.viewed', details={'filters': used}, dedup=audit.DEDUP_VIEW)
    return jsonify({
        'events': rows,
        'users': _audit_usernames(rows),
        'has_older': has_older,
        'has_newer': has_newer,
        'enabled': audit.is_enabled(),
        'retention_days': audit.retention_days(),
        'store_ip': audit.ip_mode(),
        'notice': not SystemSetting.get_setting(AUDIT_LOG_NOTICE_SETTING, False),
    })


@admin_bp.route('/admin/audit-log/export', methods=['GET'])
@login_required
def admin_audit_log_export():
    """The filtered result as a file, oldest first: format=csv or format=jsonl.

    The rows are the rows of the table, unchanged. The response is streamed.
    The file is a snapshot: events queued in this process are written first,
    and rows above the highest matching id of that moment are left out.
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    export_format = (request.args.get('format') or 'csv').strip().lower()
    if export_format not in ('csv', 'jsonl'):
        return jsonify(api_error('unknown_format', format=export_format[:20])), 400
    try:
        filters, used = _audit_filters()
    except ValueError as error:
        return jsonify(api_error(str(error))), 400

    # Before the response exists and in this order: an event written after
    # the bound was taken would lie above it.
    audit.flush(timeout=AUDIT_EXPORT_FLUSH_SECONDS)
    until_id = audit.highest_id(filters) or 0

    import csv
    import io
    from flask import stream_with_context

    def lines():
        count = 0
        if export_format == 'csv':
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(audit.COLUMNS)
            for row in audit.stream(filters, max_id=until_id):
                writer.writerow([
                    '' if row[name] is None else
                    json.dumps(row[name], sort_keys=True) if name == 'details' else _csv_cell(row[name])
                    for name in audit.COLUMNS])
                count += 1
                if buffer.tell() > 32768:
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate(0)
            yield buffer.getvalue()
        else:
            for row in audit.stream(filters, max_id=until_id):
                count += 1
                yield json.dumps(row, sort_keys=True) + '\n'
        audit.record('audit.exported', details={'filters': used, 'count': count, 'format': export_format})

    stamp = datetime.utcnow().strftime('%Y%m%d-%H%M%S')
    response = Response(stream_with_context(lines()),
                        mimetype='text/csv' if export_format == 'csv' else 'application/x-ndjson')
    response.headers['Content-Disposition'] = f'attachment; filename="audit-log-{stamp}.{export_format}"'
    response.headers['Cache-Control'] = 'no-store'
    return response


@admin_bp.route('/admin/stats', methods=['GET'])
@login_required
def admin_get_stats():
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    # Get total users
    total_users = User.query.count()
    
    # Get total recordings
    total_recordings = Recording.query.count()
    
    # Get recordings by status
    completed_recordings = Recording.query.filter_by(status='COMPLETED').count()
    processing_recordings = Recording.query.filter(Recording.status.in_(['PROCESSING', 'SUMMARIZING'])).count()
    transcribing_recordings = Recording.query.filter_by(status='PROCESSING').count()
    summarizing_recordings = Recording.query.filter_by(status='SUMMARIZING').count()
    pending_recordings = Recording.query.filter_by(status='PENDING').count()
    failed_recordings = Recording.query.filter_by(status='FAILED').count()
    audio_removed_recordings = Recording.query.filter(Recording.audio_deleted_at.isnot(None)).count()
    archived_recordings = Recording.query.filter(Recording.is_archived.is_(True)).count()

    # The job queue: what is waiting, running, and what failed this week.
    from datetime import datetime as _dt, timedelta as _td
    from src.models.processing_job import ProcessingJob
    jobs_queued = ProcessingJob.query.filter_by(status='queued').count()
    jobs_running = ProcessingJob.query.filter_by(status='processing').count()
    jobs_failed_7d = ProcessingJob.query.filter(
        ProcessingJob.status == 'failed',
        db.func.coalesce(ProcessingJob.completed_at, ProcessingJob.created_at) >= _dt.utcnow() - _td(days=7),
    ).count()
    
    # Get total storage used (exclude retention-removed audio — file gone but
    # file_size still recorded; see the user-list query above).
    total_storage = db.session.query(db.func.sum(Recording.file_size)) \
        .filter(Recording.audio_deleted_at.is_(None)).scalar() or 0

    # Get top users by storage.
    #
    # Two things here are deliberate and were both wrong before (#393).
    #
    # The audio_deleted_at test belongs in the JOIN condition, not in WHERE.
    # On the right side of an outer join, WHERE ... IS NULL is also true for
    # the all-NULL row produced when a user has no recordings at all, so those
    # users were being selected as if they qualified.
    #
    # And the sum is coalesced before ordering. PostgreSQL sorts NULLs FIRST
    # on DESC, so a user with no recordings (sum NULL) outranked everyone with
    # data and the whole top five came back as "0 bytes (0 recordings)".
    # SQLite sorts them last, which is why this only ever showed up on
    # PostgreSQL deployments.
    storage_used = db.func.coalesce(db.func.sum(Recording.file_size), 0)
    top_users_query = db.session.query(
        User.id,
        User.username,
        db.func.count(Recording.id).label('recordings_count'),
        storage_used.label('storage_used')
    ).join(
        Recording,
        db.and_(User.id == Recording.user_id, Recording.audio_deleted_at.is_(None)),
        isouter=True,
    ) \
     .group_by(User.id, User.username) \
     .order_by(storage_used.desc()) \
     .limit(5)
    
    top_users = []
    for user_id, username, recordings_count, storage_used in top_users_query:
        top_users.append({
            'id': user_id,
            'username': username,
            'recordings_count': recordings_count or 0,
            'storage_used': storage_used or 0
        })
    
    # AI requests this month: every model call except embeddings (chat,
    # summaries, titles, events, speakers, Inquire). It replaces a "total
    # queries" figure that was a hard-coded 0.
    from datetime import date as _date
    from src.models import TokenUsage
    from src.services.token_tracking import token_tracker
    month_start = _date.today().replace(day=1)
    ai_requests_month = sum(
        int(n or 0) for op, n in db.session.query(TokenUsage.operation_type, db.func.sum(TokenUsage.request_count))
        .filter(TokenUsage.date >= month_start).group_by(TokenUsage.operation_type)
        if not token_tracker.is_embedding_op(op))
    
    return jsonify({
        'total_users': total_users,
        'total_recordings': total_recordings,
        'completed_recordings': completed_recordings,
        'processing_recordings': processing_recordings,
        'pending_recordings': pending_recordings,
        'failed_recordings': failed_recordings,
        'total_storage': total_storage,
        'top_users': top_users,
        'ai_requests_month': ai_requests_month,
        'transcribing_recordings': transcribing_recordings,
        'summarizing_recordings': summarizing_recordings,
        'audio_removed_recordings': audio_removed_recordings,
        'archived_recordings': archived_recordings,
        'jobs': {'queued': jobs_queued, 'running': jobs_running, 'failed_7d': jobs_failed_7d},
    })


@admin_bp.route('/admin/stats/overview', methods=['GET'])
@login_required
def admin_get_stats_overview():
    """Everything the System Statistics tab shows, for the last 7, 30 or 90 days."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    from src.services.admin_stats import build_overview
    return jsonify(build_overview(request.args.get('days', 30, type=int)))


# --- Token Usage Stats ---

@admin_bp.route('/admin/token-stats', methods=['GET'])
@login_required
def admin_get_token_stats():
    """Get overall token usage statistics."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        from src.models import TokenUsage
        from sqlalchemy import func, extract
        from datetime import date

        # Get today's usage (already split into llm_/embedding_ buckets)
        today_usage = token_tracker.get_today_usage()

        # Get current month usage for all users (also bucketed by the tracker)
        monthly_stats = token_tracker.get_monthly_stats(months=1)
        current_month = monthly_stats[-1] if monthly_stats else {
            'tokens': 0, 'cost': 0,
            'llm_tokens': 0, 'llm_cost': 0,
            'embedding_tokens': 0, 'embedding_cost': 0,
        }

        # Get per-user stats for current month
        user_stats = token_tracker.get_user_stats()

        # Per-operation breakdown for the current month so the UI can show
        # how the total splits across summarization, chat, embedding, etc.
        today = date.today()
        op_rows = db.session.query(
            TokenUsage.operation_type,
            func.sum(TokenUsage.total_tokens).label('tokens'),
            func.sum(TokenUsage.cost).label('cost'),
            func.sum(TokenUsage.request_count).label('requests'),
            func.sum(TokenUsage.cached_tokens).label('cached_tokens'),
            func.sum(TokenUsage.cache_write_tokens).label('cache_write_tokens'),
        ).filter(
            extract('year', TokenUsage.date) == today.year,
            extract('month', TokenUsage.date) == today.month,
        ).group_by(TokenUsage.operation_type).all()
        by_operation = [
            {
                'operation_type': r.operation_type,
                'tokens': int(r.tokens or 0),
                'cost': float(r.cost or 0.0),
                'requests': int(r.requests or 0),
                'cached_tokens': int(r.cached_tokens or 0),
                'cache_write_tokens': int(r.cache_write_tokens or 0),
                'is_embedding': token_tracker.is_embedding_op(r.operation_type),
            }
            for r in op_rows
        ]

        from src.services.embeddings import embeddings_are_local
        return jsonify({
            'today': today_usage,
            'embeddings_local': embeddings_are_local(),
            'current_month': {
                'tokens': current_month.get('tokens', 0),
                'cost': current_month.get('cost', 0),
                'llm_tokens': current_month.get('llm_tokens', 0),
                'llm_cost': current_month.get('llm_cost', 0),
                'embedding_tokens': current_month.get('embedding_tokens', 0),
                'embedding_cost': current_month.get('embedding_cost', 0),
                'cached_tokens': current_month.get('cached_tokens', 0),
                'cache_write_tokens': current_month.get('cache_write_tokens', 0),
                'by_operation': by_operation,
            },
            'user_count_with_usage': len([u for u in user_stats if u['current_usage'] > 0]),
            'users_over_80_percent': len([u for u in user_stats if u['percentage'] >= 80]),
            'users_at_100_percent': len([u for u in user_stats if u['percentage'] >= 100])
        })

    except Exception as e:
        current_app.logger.error(f"Error getting token stats: {e}")
        return internal_error(e)


@admin_bp.route('/admin/token-stats/daily', methods=['GET'])
@login_required
def admin_get_daily_token_stats():
    """Get daily token usage for charts (last 30 days)."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        arg_error = id_args_error(('user_id',)) or int_args_error({'days': (1, 3650)})
        if arg_error:
            return arg_error
        days = request.args.get('days', 30, type=int)
        user_id = request.args.get('user_id', type=int)

        daily_stats = token_tracker.get_daily_stats(days=days, user_id=user_id)

        return jsonify({
            'stats': daily_stats,
            'days': days
        })

    except Exception as e:
        current_app.logger.error(f"Error getting daily token stats: {e}")
        return internal_error(e)


@admin_bp.route('/admin/token-stats/monthly', methods=['GET'])
@login_required
def admin_get_monthly_token_stats():
    """Get monthly token usage for charts (last 12 months)."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        arg_error = int_args_error({'months': (1, 1200)})
        if arg_error:
            return arg_error
        months = request.args.get('months', 12, type=int)

        monthly_stats = token_tracker.get_monthly_stats(months=months)

        return jsonify({
            'stats': monthly_stats,
            'months': months
        })

    except Exception as e:
        current_app.logger.error(f"Error getting monthly token stats: {e}")
        return internal_error(e)


@admin_bp.route('/admin/token-stats/users', methods=['GET'])
@login_required
def admin_get_user_token_stats():
    """Get per-user token usage for current month."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        user_stats = token_tracker.get_user_stats()

        return jsonify({
            'users': user_stats
        })

    except Exception as e:
        current_app.logger.error(f"Error getting user token stats: {e}")
        return internal_error(e)


# --- Transcription Usage Stats ---


@admin_bp.route('/admin/transcription-stats', methods=['GET'])
@login_required
def admin_get_transcription_stats():
    """Get overall transcription usage statistics."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        # Get today's usage
        today_usage = transcription_tracker.get_today_usage()

        # Get current month usage for all users
        monthly_stats = transcription_tracker.get_monthly_stats(months=1)
        current_month = monthly_stats[-1] if monthly_stats else {'seconds': 0, 'minutes': 0, 'cost': 0}

        # Get per-user stats for current month
        user_stats = transcription_tracker.get_user_stats()

        # Calculate totals
        total_monthly_seconds = current_month.get('seconds', 0)
        total_monthly_cost = current_month.get('cost', 0)

        return jsonify({
            'today': today_usage,
            'current_month': {
                'seconds': total_monthly_seconds,
                'minutes': total_monthly_seconds // 60,
                'cost': total_monthly_cost
            },
            'user_count_with_usage': len([u for u in user_stats if u['current_usage_seconds'] > 0]),
            'users_over_80_percent': len([u for u in user_stats if u['percentage'] >= 80]),
            'users_at_100_percent': len([u for u in user_stats if u['percentage'] >= 100])
        })

    except Exception as e:
        current_app.logger.error(f"Error getting transcription stats: {e}")
        return internal_error(e)


@admin_bp.route('/admin/transcription-stats/daily', methods=['GET'])
@login_required
def admin_get_daily_transcription_stats():
    """Get daily transcription usage for charts (last 30 days)."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        arg_error = id_args_error(('user_id',)) or int_args_error({'days': (1, 3650)})
        if arg_error:
            return arg_error
        days = request.args.get('days', 30, type=int)
        user_id = request.args.get('user_id', type=int)

        daily_stats = transcription_tracker.get_daily_stats(days=days, user_id=user_id)

        return jsonify({
            'stats': daily_stats,
            'days': days
        })

    except Exception as e:
        current_app.logger.error(f"Error getting daily transcription stats: {e}")
        return internal_error(e)


@admin_bp.route('/admin/transcription-stats/monthly', methods=['GET'])
@login_required
def admin_get_monthly_transcription_stats():
    """Get monthly transcription usage for charts (last 12 months)."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        arg_error = int_args_error({'months': (1, 1200)})
        if arg_error:
            return arg_error
        months = request.args.get('months', 12, type=int)

        monthly_stats = transcription_tracker.get_monthly_stats(months=months)

        return jsonify({
            'stats': monthly_stats,
            'months': months
        })

    except Exception as e:
        current_app.logger.error(f"Error getting monthly transcription stats: {e}")
        return internal_error(e)


@admin_bp.route('/admin/transcription-stats/users', methods=['GET'])
@login_required
def admin_get_user_transcription_stats():
    """Get per-user transcription usage for current month."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        user_stats = transcription_tracker.get_user_stats()

        return jsonify({
            'users': user_stats
        })

    except Exception as e:
        current_app.logger.error(f"Error getting user transcription stats: {e}")
        return internal_error(e)


# --- Transcript Template Routes ---


@admin_bp.route('/admin/llm-temperatures', methods=['GET'])
@login_required
def admin_get_llm_temperatures():
    """Effective sampling temperatures, with their source (#411)."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    from src.services.llm_settings import temperatures_status
    return jsonify({'temperatures': temperatures_status()})


@admin_bp.route('/admin/llm-temperatures', methods=['POST'])
@login_required
def admin_set_llm_temperatures():
    """Save or clear admin temperatures. Body: {"values": {"summary": 0.2, "chat": null, ...}}.

    A number between 0 and 2 is saved; null or an empty string clears the admin
    value, so the built-in default applies again.
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    from src.services.llm_settings import TEMPERATURES, parse_temperature, setting_key, temperatures_status
    data, body_error = json_object_body()
    if body_error:
        return body_error
    values = data.get('values')
    if not isinstance(values, dict) or not values:
        return jsonify(api_error('invalid_temperature_values')), 400
    unknown = [k for k in values if k not in TEMPERATURES]
    if unknown:
        return jsonify(api_error('unknown_temperature', names=', '.join(unknown))), 400
    parsed = {}
    for kind, raw in values.items():
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            parsed[kind] = None
            continue
        value = parse_temperature(raw)
        if value is None or isinstance(raw, bool):
            return jsonify({**api_error('invalid_temperature', kind=kind), 'field': kind}), 400
        parsed[kind] = value
    for kind, value in parsed.items():
        key = setting_key(kind)
        audit.setting_changed(key, None, None)
        if value is None:
            SystemSetting.query.filter_by(key=key).delete()
        else:
            SystemSetting.set_setting(key, str(value),
                                      description=f'Sampling temperature for {kind} requests (#411)',
                                      setting_type='string')
    db.session.commit()
    return jsonify({'success': True, 'temperatures': temperatures_status()})


@admin_bp.route('/admin/settings', methods=['GET'])
@login_required
def admin_get_settings():
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    from sqlalchemy import func
    from src.utils.system_settings import ENV_BACKED_SETTINGS, env_backed_display_value
    settings = SystemSetting.query.order_by(func.lower(SystemSetting.key)).all()
    result = []
    for setting in settings:
        if setting.key == llm_config.SETTING_KEY:
            # Managed by the LLM configuration API only.
            continue
        data = setting.to_dict()
        if setting.key in ENV_BACKED_SETTINGS:
            data['env_backed'] = True
            if setting.value is None:
                # No value saved: the value from .env applies.
                data['value'] = env_backed_display_value(setting.key)
                data['source'] = 'env'
                if setting.key == 'user_search_scope':
                    # Unset in .env as well, it follows show_usernames_in_ui.
                    from src.services import user_lookup
                    data['value'] = user_lookup.default_search_scope()
        result.append(data)
    return jsonify(result)



@admin_bp.route('/admin/settings', methods=['POST'])
@login_required
def admin_update_setting():
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    data, body_error = json_object_body()
    if body_error:
        return body_error
    if not data:
        return jsonify(api_error('no_data_provided')), 400
    
    key = data.get('key')
    value = data.get('value')
    description = data.get('description')
    setting_type = data.get('setting_type', 'string')
    
    if not key:
        return jsonify(api_error('setting_key_required')), 400
    # The key, type and description are strings (#687)
    for field, field_value in (('key', key), ('setting_type', setting_type), ('description', description)):
        if field_value is not None and not isinstance(field_value, str):
            return jsonify(api_error('field_must_be_string', field=field)), 400

    if key == llm_config.SETTING_KEY:
        return jsonify(api_error('llm_config_managed_elsewhere')), 400
    
    # An empty value for a setting that falls back to .env clears the saved
    # value, so the .env value applies again (#567).
    from src.utils.system_settings import ENV_BACKED_SETTINGS
    # The stored value before the change, for the audit row.
    existing_row = SystemSetting.query.filter_by(key=key).first()
    old_value = existing_row.value if existing_row is not None else None
    # A default for user settings (#584). With apply_to_existing, the accounts
    # that still have the previous default get the new one in this request;
    # without it (the default), only new accounts do.
    from src.services import user_defaults
    user_default = user_defaults.get(key)
    apply_to_existing = user_default is not None and data.get('apply_to_existing') is True
    old_default = user_default.current() if user_default else None

    def _save_user_default(new_default):
        """Update the accounts on the previous default when asked, and add the
        audit row. Returns the number of accounts updated."""
        updated = (user_defaults.apply_to_existing(user_default, old_default, new_default)
                   if apply_to_existing else 0)
        audit.setting_changed(key, old_default, new_default, users_updated=updated)
        return updated

    if key in ENV_BACKED_SETTINGS and (value is None or (isinstance(value, str) and not value.strip())):
        try:
            if user_default:
                users_updated = _save_user_default(user_default.fallback())
            else:
                audit.setting_changed(key, old_value, None)
            setting = SystemSetting.set_setting(key, None, description, ENV_BACKED_SETTINGS[key][2])
            if user_default:
                return jsonify({**setting.to_dict(), 'users_updated': users_updated})
            return jsonify(setting.to_dict())
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Error clearing setting {key}: {e}")
            return internal_error(e)

    if user_default:
        try:
            value = user_default.clean(value)
        except ValueError:
            return jsonify(api_error(user_default.error_code, **user_default.error_params())), 400
        setting_type = user_default.setting_type

    # Validate setting type
    valid_types = ['string', 'integer', 'boolean', 'float']
    if setting_type not in valid_types:
        return jsonify(api_error('invalid_setting_type', types=', '.join(valid_types))), 400
    
    # Validate value based on type
    if setting_type == 'integer':
        try:
            int(value) if value is not None and value != '' else None
        except (ValueError, TypeError):
            return jsonify(api_error('invalid_integer_value')), 400
    elif setting_type == 'float':
        try:
            float(value) if value is not None and value != '' else None
        except (ValueError, TypeError):
            return jsonify(api_error('invalid_number_value')), 400
    elif setting_type == 'boolean':
        if value not in ['true', 'false', '1', '0', 'yes', 'no', True, False, 1, 0]:
            return jsonify(api_error('invalid_boolean_value')), 400
    
    if key == 'deletion_mode':
        if value not in ('audio_only', 'full_recording'):
            return jsonify(api_error('invalid_deletion_mode_value')), 400

    if key == 'user_search_scope':
        from src.services import user_lookup
        value = str(value).strip().lower()
        if value not in user_lookup.SCOPES:
            return jsonify(api_error('invalid_user_search_scope', scopes=', '.join(user_lookup.SCOPES))), 400
        setting_type = 'string'

    from src.auth.permissions import MODES as PERMISSION_MODES, permission_for_setting
    if permission_for_setting(key):
        value = str(value or '').strip().lower()
        if value not in PERMISSION_MODES:
            return jsonify(api_error('invalid_permission_mode', modes=', '.join(PERMISSION_MODES))), 400
        setting_type = 'string'

    try:
        if user_default:
            users_updated = _save_user_default(value)
        else:
            audit.setting_changed(key, old_value, value)
        setting = SystemSetting.set_setting(key, value, description, setting_type)

        # Recompute the Werkzeug ceiling whenever either upload limit
        # changes. The WSGI ceiling must be the higher of the two so that
        # audio-only video uploads can exceed max_file_size_mb.
        if key in ('max_file_size_mb', 'max_audio_only_video_size_mb') and value:
            try:
                regular_mb = int(SystemSetting.get_setting('max_file_size_mb', 250))
                audio_only_mb = int(
                    SystemSetting.get_setting('max_audio_only_video_size_mb', regular_mb * 4)
                )
                ceiling_mb = max(regular_mb, audio_only_mb)
                current_app.config['MAX_CONTENT_LENGTH'] = ceiling_mb * 1024 * 1024
                current_app.logger.info(
                    f"Updated MAX_CONTENT_LENGTH to {ceiling_mb}MB "
                    f"(regular={regular_mb}, audio_only={audio_only_mb})"
                )
            except (ValueError, TypeError):
                pass

        if user_default:
            return jsonify({**setting.to_dict(), 'users_updated': users_updated})
        return jsonify(setting.to_dict())
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error updating setting {key}: {e}")
        return internal_error(e)


# --- LLM models and role assignments ----------------------------------------


class _LLMBodyError(Exception):
    """A request body that is missing, malformed or not an object."""

    def __init__(self, response):
        super().__init__('unusable request body')
        self.response = response


def _llm_payload(optional=False):
    """The JSON object body of a request (#661).

    Parsed strictly: a malformed body, a content type other than JSON or a
    value that is not an object is a 400 or 415 with an error code, never an
    empty object that would revert a role. With optional=True a request
    without a body reads as {}.
    """
    payload, error = json_object_body(optional=optional)
    if error:
        raise _LLMBodyError(error)
    return payload


def _llm_role_or_error(role):
    """The canonical role name, or a ready 400 response."""
    try:
        return llm_config.normalize_role(role), None
    except llm_config.LLMConfigError as error:
        return None, _llm_error_response(error)


def _llm_error_response(error):
    """The response for a rejected LLM model or assignment change."""
    if isinstance(error, _LLMBodyError):
        return error.response
    body = api_error(error.code, **error.params)
    if isinstance(error, llm_config.LLMModelNotFound):
        return jsonify(body), 404
    if isinstance(error, llm_config.LLMModelInUse):
        return jsonify({**body, 'roles': list(error.roles)}), 409
    return jsonify(body), 400


def _llm_roles_response():
    """The assignments and the effective configuration of every role.

    A stored id that no longer matches a configured model reads as
    unassigned, exactly as it resolves, so the UI never selects an option
    that is not in the list.
    """
    document = llm_config.load_document()
    model_ids = {model['id'] for model in document['models']}
    assignments = {
        role: (model_id if model_id in model_ids else None)
        for role, model_id in document['assignments'].items()
    }
    return {
        'assignments': assignments,
        'roles': {role: llm_config.public_config(role) for role in llm_config.ROLES},
    }


def _ask_provider_for_models(base_url, api_key):
    """The models the provider lists at {base_url}/models."""
    headers = {'Accept': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    response = httpx.get(f'{base_url}/models', headers=headers, timeout=20.0)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise TypeError('the answer is not a JSON object')
    return [
        {'id': model['id'], 'owned_by': model.get('owned_by')}
        for model in (body.get('data') or [])
        if isinstance(model, dict) and model.get('id')
    ]


def _llm_discovery_failure(base_url, error):
    """A 502 that says why the provider could not be reached."""
    if isinstance(error, httpx.HTTPStatusError):
        body = api_error('llm_provider_http_error',
                         status=error.response.status_code, url=f'{base_url}/models')
    elif isinstance(error, httpx.TimeoutException):
        body = api_error('llm_provider_timeout', url=base_url)
    elif isinstance(error, httpx.ConnectError):
        body = api_error('llm_provider_unreachable', url=base_url)
    else:
        body = api_error('llm_provider_unexpected_answer', detail=error)
    return jsonify({**body, 'success': False}), 502


_PROBE_FIELDS = frozenset({'base_url', 'api_key', 'model_id'})


def _probe_target(payload):
    """The provider URL and key a discovery or test request describes.

    A saved model may be given as the base; a masked or empty key then keeps
    its stored secret, so a test does not ask for the key again. The stored
    key goes only to the base URL it was saved with: for another URL, only a
    key entered now is sent.
    """
    unknown = sorted(payload.keys() - _PROBE_FIELDS)
    if unknown:
        raise llm_config.LLMConfigError(
            f"Unknown field(s): {', '.join(unknown)}",
            'llm_unknown_fields', fields=', '.join(unknown))
    model_id = payload.get('model_id')
    if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
        raise llm_config.LLMConfigError('model_id must be the id of a configured model',
                                        'llm_invalid_model_id')
    saved_url, saved_key = llm_config.saved_connection(model_id.strip()) if model_id else ('', '')
    base_url = payload.get('base_url')
    if base_url is None or (isinstance(base_url, str) and not base_url.strip()):
        base_url = saved_url
    base_url = llm_config.normalize_base_url(base_url)
    if base_url != saved_url:
        api_key = llm_config.new_key(payload.get('api_key'), saved_key)
    else:
        api_key = llm_config.merge_api_key(payload.get('api_key'), saved_key)
    return base_url, api_key


def _probe_response(payload):
    """Ask the provider for its models and answer with the list or an error."""
    try:
        base_url, api_key = _probe_target(payload)
    except llm_config.LLMConfigError as error:
        return _llm_error_response(error)

    try:
        models = _ask_provider_for_models(base_url, api_key)
    except httpx.HTTPError as error:
        return _llm_discovery_failure(base_url, error)
    except (TypeError, ValueError) as error:
        return jsonify({**api_error('llm_provider_unexpected_answer', detail=error),
                        'success': False}), 502
    return jsonify({'success': True, 'models': models})


@admin_bp.route('/admin/llm-config', methods=['GET'])
@login_required
def admin_get_llm_config():
    """The configured models, their assignments and each role in effect.

    API keys are write-only: a stored key reads as the mask, in the models
    and in the roles alike (#661).
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    return jsonify({
        'models': llm_config.list_models(),
        'masked_api_key': llm_config.MASKED_API_KEY,
        'model_limit': llm_config.MAX_MODELS,
        **_llm_roles_response(),
    })


@admin_bp.route('/admin/llm-config/models', methods=['POST'])
@login_required
def admin_create_llm_model():
    """Add a configured model."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        result = llm_config.create_model(_llm_payload())
    except (llm_config.LLMConfigError, _LLMBodyError) as error:
        db.session.rollback()
        return _llm_error_response(error)

    audit.llm_model_created(result.model['id'], result.changed_fields)
    db.session.commit()
    return jsonify({'success': True, 'model': result.model}), 201


@admin_bp.route('/admin/llm-config/models/<model_id>', methods=['POST'])
@login_required
def admin_update_llm_model(model_id):
    """Change a configured model. Omitted fields keep their value."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        result = llm_config.update_model(model_id, _llm_payload())
    except (llm_config.LLMConfigError, _LLMBodyError) as error:
        db.session.rollback()
        return _llm_error_response(error)

    if result.changed_fields:
        audit.llm_model_changed(model_id, result.changed_fields)
    db.session.commit()
    return jsonify({'success': True, 'model': result.model})


@admin_bp.route('/admin/llm-config/models/<model_id>', methods=['DELETE'])
@login_required
def admin_delete_llm_model(model_id):
    """Delete a model that no role uses."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        removed = llm_config.delete_model(model_id)
    except llm_config.LLMConfigError as error:
        db.session.rollback()
        return _llm_error_response(error)

    audit.llm_model_deleted(removed['id'])
    db.session.commit()
    return jsonify({'success': True})


@admin_bp.route('/admin/llm-config/assignments/<role>', methods=['POST'])
@login_required
def admin_assign_llm_model(role):
    """Assign a model to a role, or revert it to the environment with null."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    role, error = _llm_role_or_error(role)
    if error:
        return error

    try:
        payload = _llm_payload()
        # model_id is required: null returns the role to the environment, so
        # a body without it must not read as that choice.
        if 'model_id' not in payload:
            raise llm_config.LLMConfigError('model_id is required',
                                            'missing_required_field', field='model_id')
        change = llm_config.assign_model(role, payload['model_id'])
    except (llm_config.LLMConfigError, _LLMBodyError) as error:
        db.session.rollback()
        return _llm_error_response(error)

    if change.old != change.new:
        if change.new is None:
            audit.llm_role_reverted(change.role, change.old)
        else:
            audit.llm_role_assigned(change.role, change.new, change.old)
    db.session.commit()
    return jsonify({'success': True, **_llm_roles_response()})


@admin_bp.route('/admin/llm-config/assignments/<role>', methods=['DELETE'])
@login_required
def admin_revert_llm_assignment(role):
    """Return a role to its environment configuration."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    role, error = _llm_role_or_error(role)
    if error:
        return error

    change = llm_config.clear_assignment(role)
    if change.old is not None:
        audit.llm_role_reverted(role, change.old)
    db.session.commit()
    return jsonify({'success': True, **_llm_roles_response()})


@admin_bp.route('/admin/llm-config/test', methods=['POST'])
@login_required
def admin_probe_llm_configuration():
    """Discover or test a provider with the values of a form, nothing saved."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        payload = _llm_payload(optional=True)
    except _LLMBodyError as error:
        return _llm_error_response(error)
    return _probe_response(payload)


@admin_bp.route('/admin/llm-config/models/<model_id>/test', methods=['POST'])
@login_required
def admin_test_llm_model(model_id):
    """Test a saved model, with optional base_url and api_key overrides."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        payload = _llm_payload(optional=True)
        unsupported_fields = sorted(payload.keys() - {'base_url', 'api_key'})
        if unsupported_fields:
            fields = ', '.join(unsupported_fields)
            raise llm_config.LLMConfigError(
                f'Unsupported model test override field(s): {fields}',
                'llm_unknown_fields', fields=fields)
        llm_config.get_model(model_id)
    except (llm_config.LLMConfigError, _LLMBodyError) as error:
        return _llm_error_response(error)
    payload['model_id'] = model_id
    return _probe_response(payload)


@admin_bp.route('/admin/user-defaults', methods=['GET'])
@login_required
def admin_user_defaults():
    """The defaults for user settings (#584), with the value in effect."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    from src.services import user_defaults
    return jsonify({'defaults': user_defaults.describe()})


@admin_bp.route('/admin/user-defaults/<key>/accounts', methods=['GET'])
@login_required
def admin_user_default_accounts(key):
    """The number of accounts that still have the default in effect. The
    admin page asks before a default is changed (#584)."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    from src.services import user_defaults
    user_default = user_defaults.get(key)
    if user_default is None:
        return jsonify(api_error('unknown_user_default')), 404
    value = user_default.current()
    return jsonify({'key': key, 'value': value, 'count': user_defaults.accounts_on(user_default, value)})

# --- Configuration API ---


@admin_bp.route('/admin/auto-deletion/run', methods=['POST'])
@login_required
@audit.emits('admin.retention.run')
def run_auto_deletion():
    """Admin endpoint to manually trigger auto-deletion process."""
    if not current_user.is_admin:
        return jsonify(api_error('admin_required')), 403

    try:
        stats = process_auto_deletion()
        if isinstance(stats, dict) and 'checked' in stats:
            audit.record_in_tx('admin.retention.run', details={
                name: int(stats.get(name) or 0)
                for name in ('checked', 'deleted_audio_only', 'deleted_full', 'errors')})
            db.session.commit()
        return jsonify(stats)
    except Exception as e:
        current_app.logger.error(f"Error running auto-deletion: {e}")
        return internal_error(e)



@admin_bp.route('/admin/auto-deletion/stats', methods=['GET'])
@login_required
def get_auto_deletion_stats():
    """Get statistics about recordings eligible for auto-deletion."""
    if not current_user.is_admin:
        return jsonify(api_error('admin_required')), 403

    try:
        stats = {
            'enabled': _self.ENABLE_AUTO_DELETION,
            'global_retention_days': _self.GLOBAL_RETENTION_DAYS,
            'deletion_mode': _self.DELETION_MODE,
            'eligible_count': 0,
            'exempted_count': 0,
            'no_retention_count': 0,
            'archived_count': 0
        }

        if _self.ENABLE_AUTO_DELETION:
            # Check ALL completed recordings (per-recording retention)
            all_recordings = Recording.query.filter(
                Recording.status == 'COMPLETED'
            ).all()

            eligible = 0
            exempted = 0
            no_retention = 0
            current_time = datetime.utcnow()

            for recording in all_recordings:
                # Check if exempt from deletion entirely
                if is_recording_exempt_from_deletion(recording):
                    exempted += 1
                    continue

                # Get the effective retention period for this recording
                retention_days = get_retention_days_for_recording(recording)

                if not retention_days:
                    no_retention += 1
                    continue

                # Calculate cutoff for this specific recording
                cutoff_date = current_time - timedelta(days=retention_days)

                # Check if past retention period
                if recording.created_at < cutoff_date:
                    eligible += 1

            stats['eligible_count'] = eligible
            stats['exempted_count'] = exempted
            stats['no_retention_count'] = no_retention

        # Count already archived recordings
        stats['archived_count'] = Recording.query.filter(
            Recording.audio_deleted_at.is_not(None)
        ).count()

        return jsonify(stats)
    except Exception as e:
        current_app.logger.error(f"Error fetching auto-deletion stats: {e}")
        return internal_error(e)



@admin_bp.route('/api/admin/migrate_recordings', methods=['POST'])
@login_required
@audit.emits('admin.inquire.processed')
def migrate_existing_recordings_api():
    """API endpoint to migrate existing recordings for inquire mode (admin only)."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized_admin_required')), 403
    
    try:
        # Count recordings that need processing
        completed_recordings = Recording.query.filter_by(status='COMPLETED').all()
        recordings_needing_processing = []
        
        for recording in completed_recordings:
            if recording.transcription:  # Has transcription
                chunk_count = TranscriptChunk.query.filter_by(recording_id=recording.id).count()
                if chunk_count == 0:  # No chunks yet
                    recordings_needing_processing.append(recording)
        
        if len(recordings_needing_processing) == 0:
            return jsonify({
                'success': True,
                'message': translate('server.apiMessage.inquireAllProcessed'),
                'processed': 0,
                'total': len(completed_recordings)
            })
        
        # Process in small batches to avoid timeout
        batch_size = min(5, len(recordings_needing_processing))  # Process max 5 at a time
        processed = 0
        errors = 0
        
        for i in range(min(batch_size, len(recordings_needing_processing))):
            recording = recordings_needing_processing[i]
            try:
                success = process_recording_chunks(recording.id)
                if success:
                    processed += 1
                else:
                    errors += 1
            except Exception as e:
                current_app.logger.error(f"Error processing recording {recording.id} for migration: {e}")
                errors += 1
        
        remaining = max(0, len(recordings_needing_processing) - batch_size)

        audit.record_in_tx('admin.inquire.processed', details={
            'processed': processed, 'failed': errors,
            'total': len(recordings_needing_processing), 'force': False})
        db.session.commit()

        return jsonify({
            'success': True,
            'message': translate('server.apiMessage.inquireProcessedRemaining',
                                 processed=processed, remaining=remaining),
            'processed': processed,
            'errors': errors,
            'remaining': remaining,
            'total': len(recordings_needing_processing)
        })
        
    except Exception as e:
        current_app.logger.error(f"Error in migration API: {e}")
        return internal_error(e)


# --- Auto-Processing File Monitor Integration ---


@admin_bp.route('/admin/auto-process/status', methods=['GET'])
@login_required
def admin_get_auto_process_status():
    """Get the status of the automated file processing system."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    try:
        _, _, get_file_monitor_status = get_file_monitor_functions(current_app._get_current_object())
        status = get_file_monitor_status()
        
        # Add configuration info
        config = {
            'enabled': os.environ.get('ENABLE_AUTO_PROCESSING', 'false').lower() == 'true',
            'watch_directory': os.environ.get('AUTO_PROCESS_WATCH_DIR', '/data/auto-process'),
            'check_interval': int(os.environ.get('AUTO_PROCESS_CHECK_INTERVAL', '30')),
            'mode': os.environ.get('AUTO_PROCESS_MODE', 'admin_only'),
            'default_username': os.environ.get('AUTO_PROCESS_DEFAULT_USERNAME')
        }
        
        return jsonify({
            'status': status,
            'config': config
        })
        
    except Exception as e:
        current_app.logger.error(f"Error getting auto-process status: {e}")
        return internal_error(e)



@admin_bp.route('/admin/auto-process/start', methods=['POST'])
@login_required
def admin_start_auto_process():
    """Start the automated file processing system."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    try:
        start_file_monitor, _, _ = get_file_monitor_functions(current_app._get_current_object())
        start_file_monitor()
        return jsonify({'success': True, 'message': translate('server.apiMessage.autoProcessingStarted')})
    except Exception as e:
        current_app.logger.error(f"Error starting auto-process: {e}")
        return internal_error(e)



@admin_bp.route('/admin/auto-process/stop', methods=['POST'])
@login_required
def admin_stop_auto_process():
    """Stop the automated file processing system."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    try:
        _, stop_file_monitor, _ = get_file_monitor_functions(current_app._get_current_object())
        stop_file_monitor()
        return jsonify({'success': True, 'message': translate('server.apiMessage.autoProcessingStopped')})
    except Exception as e:
        current_app.logger.error(f"Error stopping auto-process: {e}")
        return internal_error(e)



@admin_bp.route('/admin/auto-process/config', methods=['POST'])
@login_required
def admin_update_auto_process_config():
    """Update auto-processing configuration (requires restart)."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403
    
    try:
        data, body_error = json_object_body()
        if body_error:
            return body_error
        if not data:
            return jsonify(api_error('no_configuration_data')), 400
        
        # This endpoint would typically update environment variables or config files
        # For now, we'll just return the current config and note that restart is required
        return jsonify({
            'success': True, 
            'message': translate('server.apiMessage.configUpdatedRestartRequired'),
            'note': 'Environment variables need to be updated manually and application restarted.'
        })
        
    except Exception as e:
        current_app.logger.error(f"Error updating auto-process config: {e}")
        return internal_error(e)



@admin_bp.route('/admin/auto-process/trigger', methods=['POST'])
@login_required
def admin_trigger_auto_process():
    """Trigger an immediate auto-process scan cycle."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        from src.file_monitor import file_monitor
        if not file_monitor or not file_monitor.running:
            return jsonify(api_error('auto_processing_not_running')), 400

        import threading
        def run_scan():
            try:
                file_monitor._update_user_cache()
                if file_monitor.mode == 'admin_only':
                    file_monitor._scan_admin_directory()
                elif file_monitor.mode == 'user_directories':
                    file_monitor._scan_user_directories()
                elif file_monitor.mode == 'single_user':
                    file_monitor._scan_single_user_directory()
            except Exception as e:
                file_monitor.logger.error(f"Error during triggered scan: {e}", exc_info=True)

        thread = threading.Thread(target=run_scan, daemon=True)
        thread.start()

        return jsonify({'success': True, 'message': translate('server.apiMessage.scanTriggered')})

    except Exception as e:
        current_app.logger.error(f"Error triggering auto-process scan: {e}")
        return internal_error(e)



@admin_bp.route('/admin/transcription/discover-models', methods=['GET'])
@login_required
def admin_discover_transcription_models():
    """Probe the active transcription connector's /v1/models endpoint.

    Used by the admin UI to populate a "models available on this provider"
    list when curating which models users can pick from. Returns a list of
    {id, label, owned_by} dicts. Empty list if the connector does not
    implement list_models() or the upstream is unreachable.
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        from src.services.transcription import get_registry
        registry = get_registry()
        connector = registry.get_active_connector()
        if not connector:
            return jsonify(api_error('no_transcription_connector')), 503

        connector_name = registry.get_active_connector_name()
        try:
            models = connector.list_models()
        except NotImplementedError:
            return jsonify({
                'connector': connector_name,
                'supported': False,
                'models': [],
                'message': translate('server.apiMessage.modelDiscoveryUnsupported', connector=connector_name),
            })

        return jsonify({
            'connector': connector_name,
            'supported': True,
            'models': models or [],
        })

    except Exception as e:
        current_app.logger.error(f"Error discovering transcription models: {e}")
        return internal_error(e)


@admin_bp.route('/admin/transcription/visible-models', methods=['GET'])
@login_required
def admin_get_visible_models():
    """Return the admin-curated list of models exposed in the user dropdown.

    Falls back to the TRANSCRIPTION_MODELS_AVAILABLE env var when no DB
    setting has been saved yet. The shape matches what /api/config returns
    so the admin UI can pre-populate its form.
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        from src.models import SystemSetting
        from src.config.app_config import TRANSCRIPTION_MODEL_OPTIONS
        import json as _json

        raw = SystemSetting.get_setting('transcription_models_visible_json', None)
        if raw:
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    return jsonify({
                        'source': 'database',
                        'options': parsed,
                        'default_model': SystemSetting.get_setting('transcription_default_model', None),
                    })
            except (ValueError, TypeError):
                pass

        return jsonify({
            'source': 'env' if TRANSCRIPTION_MODEL_OPTIONS else 'unset',
            'options': TRANSCRIPTION_MODEL_OPTIONS,
            'default_model': None,
        })

    except Exception as e:
        current_app.logger.error(f"Error reading visible transcription models: {e}")
        return internal_error(e)


@admin_bp.route('/admin/transcription/visible-models', methods=['POST'])
@login_required
def admin_save_visible_models():
    """Save the admin-curated list of user-facing transcription models.

    Body: { "options": [{"value": "...", "label": "..."}, ...],
            "default_model": "..." | null }
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        from src.models import SystemSetting
        import json as _json

        # A body is required, as before ({} for the defaults)
        data, body_error = json_object_body()
        if body_error:
            return body_error
        options = data.get('options', [])
        if not isinstance(options, list):
            return jsonify(api_error('invalid_options_list')), 400

        # Normalise each option into {value, label}.
        normalised = []
        for opt in options:
            if isinstance(opt, str):
                normalised.append({'value': opt, 'label': opt})
            elif isinstance(opt, dict) and opt.get('value'):
                normalised.append({
                    'value': str(opt['value']).strip(),
                    'label': str(opt.get('label') or opt['value']).strip(),
                })

        audit.setting_changed('transcription_models_visible_json', None, None)
        audit.setting_changed('transcription_default_model', None, None)
        SystemSetting.set_setting(
            key='transcription_models_visible_json',
            value=_json.dumps(normalised),
            description='Admin-curated list of transcription models exposed in user dropdowns. Overrides TRANSCRIPTION_MODELS_AVAILABLE env var when set.',
            setting_type='string',
        )

        default_model = (data.get('default_model') or '').strip()
        if default_model:
            SystemSetting.set_setting(
                key='transcription_default_model',
                value=default_model,
                description='Admin-selected default transcription model. Used when no per-upload, tag, or folder override is set.',
                setting_type='string',
            )
        else:
            existing = SystemSetting.query.filter_by(key='transcription_default_model').first()
            if existing:
                db.session.delete(existing)
                db.session.commit()

        return jsonify({
            'success': True,
            'options': normalised,
            'default_model': default_model or None,
        })

    except Exception as e:
        current_app.logger.error(f"Error saving visible transcription models: {e}")
        db.session.rollback()
        return internal_error(e)


@admin_bp.route('/admin/inquire/process-recordings', methods=['POST'])
@login_required
@audit.emits('admin.inquire.processed')
def admin_process_recordings_for_inquire():
    """Process recordings for inquire mode (chunk and embed them).

    Default behaviour processes only recordings that do not yet have chunks.
    Pass ``force=true`` in the JSON body to re-embed every completed
    recording regardless of existing chunks. This is the migration path after
    swapping ``EMBEDDING_MODEL`` or ``EMBEDDING_BASE_URL``: the old vectors
    are deleted and replaced with vectors from the new configuration.

    Body fields:
        force (bool, optional, default false): re-embed all recordings
        batch_size (int, optional, default 10): commit cadence
        max_recordings (int, optional): cap the number processed in one call
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        # A body is required, as before ({} for the defaults)
        data, body_error = json_object_body()
        if body_error:
            return body_error
        # Whole numbers and a flag, checked before any work (#687)
        batch_size = data.get('batch_size', 10)
        if batch_size is None:
            batch_size = 10
        if not is_int(batch_size) or not 1 <= batch_size <= 1000:
            return jsonify(api_error('field_out_of_range', field='batch_size', min=1, max=1000)), 400
        max_recordings = data.get('max_recordings', None)
        if max_recordings is not None and (not is_int(max_recordings) or not 1 <= max_recordings <= DB_INT_MAX):
            return jsonify(api_error('field_out_of_range', field='max_recordings', min=1, max=DB_INT_MAX)), 400
        flags, flag_error = flag_fields(data, ('force',))
        if flag_error:
            return flag_error
        force = flags.get('force', False)

        if force:
            # Re-embed every recording that has a usable transcription, plus
            # any recording that already has chunks stored. The chunks-already-
            # stored case matters because a recording can be in a non-COMPLETED
            # state (PROCESSING, SUMMARIZING, FAILED) while still holding stale
            # vectors from the previous embedding configuration. Filtering on
            # status alone leaves those ghost chunks behind and produces the
            # "X.shape[1] == new while Y.shape[1] == old" warnings on every
            # subsequent search until they are cleaned up.
            recordings_with_chunks_subq = db.session.query(
                TranscriptChunk.recording_id
            ).distinct()
            recordings_needing_processing = Recording.query.filter(
                db.or_(
                    db.and_(
                        Recording.status == 'COMPLETED',
                        Recording.transcription.isnot(None),
                        Recording.transcription != '',
                    ),
                    Recording.id.in_(recordings_with_chunks_subq),
                )
            ).all()
        else:
            # Only process recordings that have no chunks yet.
            recordings_with_chunks = db.session.query(TranscriptChunk.recording_id).distinct()
            recordings_needing_processing = Recording.query.filter(
                Recording.status == 'COMPLETED',
                Recording.transcription.isnot(None),
                Recording.transcription != '',
                ~Recording.id.in_(recordings_with_chunks)
            ).all()

        if max_recordings:
            recordings_needing_processing = recordings_needing_processing[:int(max_recordings)]

        total_to_process = len(recordings_needing_processing)

        if total_to_process == 0:
            return jsonify({
                'success': True,
                'message': translate('server.apiMessage.noRecordingsToReembed' if force
                                     else 'server.apiMessage.inquireAllProcessed'),
                'processed': 0,
                'total': 0,
                'force': force,
            })

        processed = 0
        failed = []

        # Number of times to retry each recording after the first pass. The
        # underlying _api_embed already retries transient errors; this layer
        # catches whole-pipeline failures (DB lock contention, partial
        # provider responses, anything that produced a False return).
        retry_passes = max(0, int(data.get('retry_passes', 2)))

        def _attempt_recording(recording, attempt_label):
            """Run process_recording_chunks for one recording.

            Returns ``(success: bool, error: Optional[str])``. Any exception
            is caught and converted to an error string so a single bad row
            cannot abort the whole sweep.
            """
            try:
                ok = process_recording_chunks(recording.id)
            except Exception as exc:
                current_app.logger.error(
                    f"Admin API: Failed to process recording {recording.id} "
                    f"({attempt_label}): {exc}"
                )
                # The kind of the error only: its text can quote the transcript.
                return False, describe_error(exc)
            if ok:
                current_app.logger.info(
                    f"Admin API: {'Re-embedded' if force else 'Processed chunks for'} "
                    f"recording ({attempt_label}): {recording.id}"
                )
                return True, None
            return False, 'Processing returned false'

        # First pass: try every recording once.
        retry_queue = []
        for recording in recordings_needing_processing:
            ok, err = _attempt_recording(recording, 'pass 1')
            if ok:
                processed += 1
            else:
                retry_queue.append((recording, err))

            if processed % batch_size == 0:
                db.session.commit()

        # Subsequent passes: retry any recording that did not succeed.
        # Backoff between passes lets a temporarily overloaded provider
        # recover before we hammer it again.
        for pass_num in range(2, retry_passes + 2):
            if not retry_queue:
                break
            import time as _t
            _t.sleep(min(15, 2 ** (pass_num - 1)))
            current_app.logger.info(
                f"Admin API: retry pass {pass_num} on {len(retry_queue)} recordings"
            )
            still_failing = []
            for recording, prev_err in retry_queue:
                ok, err = _attempt_recording(recording, f'pass {pass_num}')
                if ok:
                    processed += 1
                else:
                    still_failing.append((recording, err or prev_err))
            retry_queue = still_failing

        # Anything still in retry_queue after the last pass is recorded as failed.
        for recording, err in retry_queue:
            # Ids and the kind of the error. The recordings belong to other
            # users, so no title is returned to the administrator.
            failed.append({
                'id': recording.id,
                'reason': err or 'Processing returned false',
                'attempts': retry_passes + 1,
            })

        audit.record_in_tx('admin.inquire.processed', details={
            'processed': processed, 'failed': len(failed),
            'total': total_to_process, 'force': force})
        db.session.commit()

        # On a successful force re-embed, refresh the stored identifier so the
        # mismatch warning clears.
        if force and processed > 0 and not failed:
            try:
                from src.models import SystemSetting
                from src.services.embeddings import EMBEDDING_IDENTIFIER
                SystemSetting.set_setting(
                    key='embedding_identifier',
                    value=EMBEDDING_IDENTIFIER,
                    description='Identifier of the embedding provider and model that produced the stored chunk vectors. Used to detect dimensionality and semantic-space mismatches at startup.',
                    setting_type='string',
                )
            except Exception as e:
                current_app.logger.warning(f"Failed to refresh embedding_identifier after re-embed: {e}")

        if force:
            message = translate('server.apiMessage.embeddingsReembeddedCount',
                                processed=processed, total=total_to_process)
        else:
            message = translate('server.apiMessage.embeddingsProcessedCount',
                                processed=processed, total=total_to_process)
        return jsonify({
            'success': True,
            'message': message,
            'processed': processed,
            'total': total_to_process,
            'failed': failed,
            'force': force,
        })

    except Exception as e:
        current_app.logger.error(f"Error in admin process recordings endpoint: {e}")
        db.session.rollback()
        return internal_error(e)



@admin_bp.route('/admin/inquire/status', methods=['GET'])
@login_required
def admin_inquire_status():
    """Get the status of recordings for inquire mode."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    try:
        from src.services.embeddings import (
            EMBEDDING_MODEL, EMBEDDING_BASE_URL, EMBEDDING_IDENTIFIER,
            USE_API_EMBEDDINGS, EMBEDDING_DIMENSIONS,
        )
        from src.models import SystemSetting, TokenUsage
        from sqlalchemy import func

        # Count total completed recordings
        total_completed = Recording.query.filter_by(status='COMPLETED').count()

        # Count recordings with transcriptions
        recordings_with_transcriptions = Recording.query.filter(
            Recording.status == 'COMPLETED',
            Recording.transcription.isnot(None),
            Recording.transcription != ''
        ).count()

        # Count recordings that have been processed for inquire mode
        processed_recordings = db.session.query(Recording.id).join(
            TranscriptChunk, Recording.id == TranscriptChunk.recording_id
        ).distinct().count()

        # Count recordings that still need processing (have transcription but no chunks)
        need_processing = db.session.query(func.count(Recording.id)).filter(
            Recording.status == 'COMPLETED',
            Recording.transcription.isnot(None),
            Recording.transcription != '',
            ~Recording.id.in_(db.session.query(TranscriptChunk.recording_id).distinct())
        ).scalar() or 0

        # Total chunks across the system
        total_chunks = TranscriptChunk.query.count()

        # Detect embedded chunks that pre-date the current configuration. The
        # stored identifier is what produced the existing vectors; if it does
        # not match the current EMBEDDING_IDENTIFIER, those chunks are stale
        # and Inquire mode will return wrong results until they are re-embedded.
        stored_identifier = SystemSetting.get_setting('embedding_identifier', None)
        if stored_identifier is None:
            legacy = SystemSetting.get_setting('embedding_model_name', None)
            stored_identifier = f"local::{legacy}" if legacy else None

        identifier_mismatch = bool(
            stored_identifier and stored_identifier != EMBEDDING_IDENTIFIER and total_chunks > 0
        )

        # Embedding-API usage aggregates (lifetime totals across all users).
        usage_rows = TokenUsage.query.filter_by(operation_type='embedding').all()
        embedding_usage = {
            'total_tokens': sum(u.total_tokens for u in usage_rows),
            'total_cost': sum((u.cost or 0.0) for u in usage_rows),
            'request_count': sum(u.request_count for u in usage_rows),
        }

        return jsonify({
            'total_completed_recordings': total_completed,
            'recordings_with_transcriptions': recordings_with_transcriptions,
            'processed_for_inquire': processed_recordings,
            'need_processing': need_processing,
            'total_chunks': total_chunks,
            'embeddings_available': EMBEDDINGS_AVAILABLE,
            # Embedding configuration surfaced to the UI
            'embedding_model': EMBEDDING_MODEL,
            'embedding_provider': 'api' if USE_API_EMBEDDINGS else 'local',
            # Chunking configuration (segment-aware since the speaker/timestamp
            # chunk metadata rework; 'legacy' = plain 500-char windows)
            'chunking_strategy': os.environ.get('CHUNKING_STRATEGY', 'segment').strip().lower(),
            'chunk_target_chars': int(os.environ.get('CHUNK_TARGET_CHARS', '1400')),
            'embedding_base_url': EMBEDDING_BASE_URL or None,
            'embedding_dimensions_override': EMBEDDING_DIMENSIONS,
            'embedding_identifier': EMBEDDING_IDENTIFIER,
            'embedding_identifier_stored': stored_identifier,
            'embedding_identifier_mismatch': identifier_mismatch,
            # Embedding-API usage (only meaningful in API mode; local mode = 0)
            'embedding_usage': embedding_usage,
        })

    except Exception as e:
        current_app.logger.error(f"Error getting inquire status: {e}")
        return internal_error(e)

# --- Group Management API (Admin Only) ---



# --- Voice embedding compatibility (#380) ---------------------------------

@admin_bp.route('/admin/voice-embeddings/status', methods=['GET'])
@login_required
def admin_voice_embedding_status():
    """Report whether the backend still produces compatible voice embeddings.

    Reads the stored verdict only. Probing costs a real transcription, so it
    is never done on a page load.
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    from src.services.voice_embedding_check import get_status
    return jsonify(get_status(current_app))


@admin_bp.route('/admin/voice-embeddings/check', methods=['POST'])
@login_required
def admin_voice_embedding_check():
    """Re-run the check now, regardless of whether the backend looks changed."""
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    from src.services.voice_embedding_check import check_voice_embeddings
    result = check_voice_embeddings(current_app, force=True)
    current_app.config['VOICE_EMBEDDING_STATUS'] = result
    return jsonify(result)


@admin_bp.route('/admin/voice-embeddings/rebaseline', methods=['POST'])
@login_required
def admin_voice_embedding_rebaseline():
    """Accept the current backend as the reference.

    For when the change was intentional and the voice profiles have been
    rebuilt against the new model, or are being abandoned. This discards the
    embedding the existing profiles were built against, so it cannot be undone
    by restoring the old backend.
    """
    if not current_user.is_admin:
        return jsonify(api_error('unauthorized')), 403

    from src.services.voice_embedding_check import rebaseline
    result = rebaseline(current_app)
    current_app.config['VOICE_EMBEDDING_STATUS'] = result
    current_app.logger.info(
        "Voice embedding reference re-baselined by admin user %s", current_user.id)
    return jsonify(result)


# Hot-applied System Settings (high priority) with environment fallback.
from src.utils.system_settings import apply_hot_settings
_SYSTEM_SETTING_MAP = {
    "USERS_CAN_DELETE": ('users_can_delete', 'USERS_CAN_DELETE', True),
    "ENABLE_INTERNAL_SHARING": ('enable_internal_sharing', 'ENABLE_INTERNAL_SHARING', False),
    "ENABLE_AUTO_DELETION": ('enable_auto_deletion', 'ENABLE_AUTO_DELETION', False),
    "GLOBAL_RETENTION_DAYS": ('global_retention_days', 'GLOBAL_RETENTION_DAYS', 0),
    "DELETION_MODE": ('deletion_mode', 'DELETION_MODE', 'full_recording'),
}
_self, __getattr__ = apply_hot_settings(__name__, _SYSTEM_SETTING_MAP)
