"""Helpers shared by the audit log tests (tests/test_audit_log_*.py)."""

import os
import sys
import uuid

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, bcrypt  # noqa: E402
from src.models import APIToken, AuditEvent, Recording, SystemSetting, User  # noqa: E402
from src.services import audit  # noqa: E402
from src.utils.system_settings import ENV_BACKED_SETTINGS  # noqa: E402
from src.utils.token_auth import hash_token  # noqa: E402

PASSWORD = 'Passw0rd!x'
AUDIT_KEYS = ('audit_log_enabled', 'audit_log_retention_days', 'audit_log_store_ip', 'audit_log_record_views')


def reset_audit_settings():
    for key in AUDIT_KEYS:
        SystemSetting.set_setting(key, None, ENV_BACKED_SETTINGS[key][3], ENV_BACKED_SETTINGS[key][2])


def set_setting(key, value):
    SystemSetting.set_setting(key, value, None, ENV_BACKED_SETTINGS[key][2])


def max_id():
    db.session.rollback()
    return db.session.execute(text('SELECT COALESCE(MAX(id), 0) FROM audit_event')).scalar()


def rows_after(start, event=None, **equals):
    """The rows written after `start`, oldest first, once the queue is written."""
    assert audit.flush(timeout=15)
    db.session.rollback()
    query = AuditEvent.query.filter(AuditEvent.id > start)
    if event:
        query = query.filter_by(event=event)
    if equals:
        query = query.filter_by(**equals)
    return query.order_by(AuditEvent.id).all()


def names(rows):
    return [row.event for row in rows]


def make_user(created, prefix='aud', password=PASSWORD, **extra):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f'{prefix[:9]}_{suffix}', email=f'{prefix}_{suffix}@local.test',
                password=bcrypt.generate_password_hash(password).decode() if password else None, **extra)
    db.session.add(user)
    db.session.commit()
    created.append(user.id)
    return user


def make_recording(owner, **extra):
    values = dict(user_id=owner.id, title='A recording', status='COMPLETED',
                  transcription='[{"speaker": "SPEAKER_00", "sentence": "Hello there.", "start_time": 0, "end_time": 1}]',
                  summary='A summary.', notes='Some notes.')
    values.update(extra)
    recording = Recording(**values)
    db.session.add(recording)
    db.session.commit()
    return recording


def login(client, user):
    from flask import g
    with client.session_transaction() as sess:
        sess.clear()
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    try:
        g.pop('_login_user', None)
    except RuntimeError:
        pass


def client_for(user):
    client = app.test_client()
    login(client, user)
    return client


def anonymous_client():
    from flask import g
    try:
        g.pop('_login_user', None)
    except RuntimeError:
        pass
    return app.test_client()


def api_token(user, scopes=None, name='audit test'):
    """(plaintext, id) of a new token of the user."""
    plain = uuid.uuid4().hex + uuid.uuid4().hex
    row = APIToken(user_id=user.id, name=name, token_hash=hash_token(plain), scopes=scopes)
    db.session.add(row)
    db.session.commit()
    return plain, row.id


def bearer(plain):
    return {'Authorization': f'Bearer {plain}'}


def remove_users(created):
    """Delete the accounts a test created, with what refers to them."""
    from src.models import GroupMembership, InternalShare, Share, SharedRecordingState
    db.session.rollback()
    for user_id in created:
        user = db.session.get(User, user_id)
        if user is None:
            continue
        for recording in Recording.query.filter_by(user_id=user_id).all():
            Share.query.filter_by(recording_id=recording.id).delete()
            InternalShare.query.filter_by(recording_id=recording.id).delete()
            SharedRecordingState.query.filter_by(recording_id=recording.id).delete()
            db.session.delete(recording)
        InternalShare.query.filter(
            (InternalShare.owner_id == user_id) | (InternalShare.shared_with_user_id == user_id)).delete()
        SharedRecordingState.query.filter_by(user_id=user_id).delete()
        APIToken.query.filter_by(user_id=user_id).delete()
        GroupMembership.query.filter_by(user_id=user_id).delete()
        db.session.delete(user)
        db.session.commit()


def unfreeze_hot_settings():
    """Remove module attributes that shadow a hot-applied setting.

    ENABLE_INTERNAL_SHARING, ENABLE_PUBLIC_SHARING and the like are read
    through a module-level __getattr__ (apply_hot_settings), so a saved
    setting applies at once. A test that sets such a name on the module and
    restores it with setattr leaves an ordinary attribute behind, and the
    saved setting is then ignored for the rest of the process. A test that
    relies on the saved setting calls this first.
    """
    import sys
    for module in list(sys.modules.values()):
        names = getattr(module, "__dict__", {}).get("_SYSTEM_SETTING_MAP")
        if isinstance(names, dict) and "__getattr__" in module.__dict__:
            for name in names:
                module.__dict__.pop(name, None)
