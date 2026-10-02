"""Public share links: one set of rules for the web app and API v1 (mailr spec G10).

The newest link of a recording is reused unless a new one is asked for. The
web app updates the reused link's flags to what it sent; API v1 changes them
only with update_existing and otherwise reports that they differ, so flags
never change silently. Creating and revoking a link sends
recording.share.created and recording.share.revoked (spec W5).
"""

from datetime import datetime, timedelta

from src.database import db


def active_share_or_404(public_id):
    """The share behind a public link; an expired one is gone (404)."""
    from flask import abort
    from src.models import Share
    share = Share.query.filter_by(public_id=public_id).first_or_404()
    if share.expires_at is not None and share.expires_at <= datetime.utcnow():
        abort(404)
    return share


def get_or_create(recording, user, share_summary, share_notes, force_new=False,
                  update_existing=False, expires_in_days=None):
    """Returns (share, created, flags_differ). Commits."""
    from src.models import Share
    existing = None
    if not force_new:
        existing = (Share.query.filter_by(recording_id=recording.id, user_id=user.id)
                    .order_by(Share.created_at.desc(), Share.id.desc()).first())
    if existing is not None:
        differ = existing.share_summary != share_summary or existing.share_notes != share_notes
        if differ and update_existing:
            existing.share_summary = share_summary
            existing.share_notes = share_notes
            db.session.commit()
            differ = False
        return existing, False, differ

    share = Share(recording_id=recording.id, user_id=user.id, share_summary=share_summary,
                  share_notes=share_notes)
    if expires_in_days:
        share.expires_at = datetime.utcnow() + timedelta(days=expires_in_days)
    db.session.add(share)
    db.session.commit()
    _emit('recording.share.created', share)
    return share, True, False


def revoke(share):
    data = _data(share)
    user_id = share.user_id
    db.session.delete(share)
    db.session.commit()
    try:
        from src.services.webhook_dispatch import emit_webhook_event
        emit_webhook_event(user_id=user_id, event_type='recording.share.revoked', data=data)
    except Exception:
        pass


def _data(share):
    return {'recording_id': share.recording_id, 'share_id': share.id,
            'share_summary': bool(share.share_summary), 'share_notes': bool(share.share_notes)}


def _emit(event_type, share):
    try:
        from src.services.webhook_dispatch import emit_webhook_event
        emit_webhook_event(user_id=share.user_id, event_type=event_type, data=_data(share))
    except Exception:
        pass


def share_dict(share):
    z = lambda v: v.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if v else None
    return {'id': share.id, 'share_summary': bool(share.share_summary), 'share_notes': bool(share.share_notes),
            'created_at': z(share.created_at), 'expires_at': z(share.expires_at)}
