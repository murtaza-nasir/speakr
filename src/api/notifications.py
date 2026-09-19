"""In-app notification endpoints.

Everything here is scoped to the signed-in user. There is no admin view of
somebody else's notices: instance-level notices are fanned out to each admin
at raise time, so an admin reads their own copy.
"""

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from src.services import notifications as notification_service

notifications_bp = Blueprint('notifications', __name__)


@notifications_bp.route('/api/notifications', methods=['GET'])
@login_required
def list_notifications():
    """The dropdown's contents, plus the badge count in the same response.

    Returned together so opening the panel does not need a second request and
    cannot show a list that disagrees with the badge beside it.
    """
    notices = notification_service.active_for(current_user)
    return jsonify({
        'notifications': [n.to_dict() for n in notices],
        'unread_count': notification_service.unread_count(current_user),
    })


@notifications_bp.route('/api/notifications/count', methods=['GET'])
@login_required
def notification_count():
    """Just the badge. Polled, so it stays as cheap as possible."""
    return jsonify({'unread_count': notification_service.unread_count(current_user)})


@notifications_bp.route('/api/notifications/read', methods=['POST'])
@login_required
def mark_notifications_read():
    """Mark the given ids read, or everything when none are given."""
    data = request.get_json(silent=True) or {}
    ids = data.get('ids')
    if ids is not None and not isinstance(ids, list):
        return jsonify({'error': 'ids must be a list'}), 400

    marked = notification_service.mark_read(current_user, ids)
    return jsonify({
        'marked': marked,
        'unread_count': notification_service.unread_count(current_user),
    })


@notifications_bp.route('/api/notifications/<int:notification_id>', methods=['DELETE'])
@login_required
def dismiss_notification(notification_id):
    """Clear one notice away.

    404 rather than 403 when it belongs to somebody else: whether an id exists
    is not something to confirm to a user who does not own it.
    """
    if not notification_service.dismiss(current_user, notification_id):
        return jsonify({'error': 'Notification not found'}), 404
    return jsonify({
        'dismissed': notification_id,
        'unread_count': notification_service.unread_count(current_user),
    })
