"""Public share links: one set of rules for the web app and API v1 (mailr spec G10).

The newest link of a recording is reused unless a new one is asked for. The
web app updates the reused link's flags to what it sent; API v1 changes them
only with update_existing and otherwise reports that they differ, so flags
never change silently. Creating and revoking a link sends
recording.share.created and recording.share.revoked (spec W5).
"""

from datetime import datetime, timedelta

from src.database import db


def public_sharing_enabled():
    """The enable_public_sharing system setting (ENABLE_PUBLIC_SHARING in .env)."""
    from src.models import SystemSetting
    from src.utils.system_settings import ENV_BACKED_SETTINGS, env_fallback
    env_var, default = ENV_BACKED_SETTINGS['enable_public_sharing'][:2]
    return bool(SystemSetting.get_setting('enable_public_sharing', env_fallback(env_var, default)))


def user_can_share_publicly(user, sharing_enabled=None):
    """Whether the user may create a public link now: public sharing is on and
    the user has the permission (#685). The web app, its share dialog and API
    v1 read it here, so they agree."""
    if sharing_enabled is None:
        sharing_enabled = public_sharing_enabled()
    return bool(sharing_enabled and user is not None and getattr(user, 'can_share_publicly', True))


def link_is_active(share, sharing_enabled=None):
    """Whether a public link is answered now.

    A link is answered only while a new one could be created by its owner:
    public sharing is on and the owner still has the permission to share
    publicly. The link itself is kept, so it is answered again when the
    switch or the permission returns. An expired link is not active.
    """
    if sharing_enabled is None:
        sharing_enabled = public_sharing_enabled()
    if not sharing_enabled:
        return False
    if share.user is None or not share.user.can_share_publicly:
        return False
    if share.expires_at is not None and share.expires_at <= datetime.utcnow():
        return False
    return True


def active_share_or_404(public_id):
    """The share behind a public link; an inactive or expired one answers 404.

    The same answer as for an unknown link, so a visitor cannot tell a
    switched-off link from one that never existed.
    """
    from flask import abort
    from src.models import Share
    share = Share.query.filter_by(public_id=public_id).first_or_404()
    if not link_is_active(share):
        abort(404)
    return share


def _audit(event, share):
    """The audit row of a public link, in the transaction of the change. The
    id and the flags; never the public_id, which is the access secret."""
    from src.services import audit
    details = {'share_id': share.id}
    if event != 'share.public.revoked':
        details.update(share_summary=bool(share.share_summary), share_notes=bool(share.share_notes),
                       expires=share.expires_at is not None)
    audit.record_in_tx(event, recording_id=share.recording_id, recording_owner_id=share.user_id,
                       details=details)


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
            _audit('share.public.changed', existing)
            db.session.commit()
            differ = False
        return existing, False, differ

    share = Share(recording_id=recording.id, user_id=user.id, share_summary=share_summary,
                  share_notes=share_notes)
    if expires_in_days:
        share.expires_at = datetime.utcnow() + timedelta(days=expires_in_days)
    db.session.add(share)
    db.session.flush()
    _audit('share.public.created', share)
    db.session.commit()
    _emit('recording.share.created', share)
    return share, True, False


def revoke(share):
    data = _data(share)
    user_id = share.user_id
    _audit('share.public.revoked', share)
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


def public_url(public_id):
    """Absolute link to a public share (#498).

    Built from APP_BASE_URL when it is set, so X-Forwarded-Host cannot choose
    the host of the link. Otherwise the host comes from the request, as before.
    """
    from flask import url_for
    from src.services.email import get_app_base_url
    base = get_app_base_url()
    if base:
        return base + url_for('shares.view_shared_recording', public_id=public_id)
    return url_for('shares.view_shared_recording', public_id=public_id, _external=True)


def link_is_https():
    """Whether a new public link would be an https link (#498).

    With APP_BASE_URL set, the link uses that address, so its scheme decides,
    and an X-Forwarded-Proto header sent by the client does not. Otherwise
    the request's scheme decides, as before.
    """
    from urllib.parse import urlparse
    from flask import request
    from src.services.email import get_app_base_url
    base = get_app_base_url()
    if base:
        return urlparse(base).scheme.lower() == 'https'
    return request.is_secure


def share_dict(share):
    z = lambda v: v.strftime('%Y-%m-%dT%H:%M:%S.%fZ') if v else None
    return {'id': share.id, 'share_summary': bool(share.share_summary), 'share_notes': bool(share.share_notes),
            'created_at': z(share.created_at), 'expires_at': z(share.expires_at),
            'active': link_is_active(share)}
