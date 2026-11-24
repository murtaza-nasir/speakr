"""VAPID key generation, storage and the push API that depends on it (#644)."""

import base64
import importlib
import json
import stat
import sys
import uuid

import pytest

from tests.audit_log_support import anonymous_client, client_for, make_user, remove_users


def _b64decode(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


@pytest.fixture
def vapid(tmp_path, monkeypatch):
    """The key module reloaded against an empty CONFIG_DIR, and a fresh push config."""
    import src.api.push_notifications as push
    from src.app import app
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', False)
    monkeypatch.setenv('CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(push, '_vapid_config', None)
    import src.utils.vapid_keys as module
    module = importlib.reload(module)
    yield module
    push._vapid_config = None


def test_generate_vapid_keys_returns_a_usable_pair(vapid):
    keys = vapid.generate_vapid_keys()

    assert isinstance(keys, dict)
    public = _b64decode(keys['public_key'])
    assert len(public) == 65 and public[0] == 0x04
    assert len(_b64decode(keys['private_key'])) == 32


def test_the_private_key_is_accepted_by_pywebpush_and_matches_the_public_key(vapid):
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid

    keys = vapid.generate_vapid_keys()
    signer = Vapid.from_string(keys['private_key'])
    derived = signer.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)

    assert derived == _b64decode(keys['public_key'])
    headers = signer.sign({'sub': 'mailto:admin@example.com', 'aud': 'https://push.example'})
    assert 'Authorization' in headers


def test_load_vapid_keys_persists_one_pair(vapid, tmp_path):
    first = vapid.VAPID_KEYS
    keys_file = tmp_path / 'vapid_keys.json'

    assert vapid.VAPID_ENABLED is True
    assert keys_file.exists()
    assert stat.S_IMODE(keys_file.stat().st_mode) == 0o600
    assert json.loads(keys_file.read_text()) == first

    restarted = importlib.reload(vapid)
    assert restarted.VAPID_KEYS == first
    assert restarted.VAPID_PUBLIC_KEY == first['public_key']


@pytest.mark.parametrize('content', ['{not json', '[]', '{"public_key": "only half"}'])
def test_an_unusable_key_file_is_replaced_by_a_new_pair(vapid, tmp_path, content):
    keys_file = tmp_path / 'vapid_keys.json'
    keys_file.write_text(content)

    reloaded = importlib.reload(vapid)

    assert reloaded.VAPID_ENABLED is True
    assert len(_b64decode(reloaded.VAPID_PUBLIC_KEY)) == 65
    assert json.loads(keys_file.read_text()) == reloaded.VAPID_KEYS


def test_push_config_reports_enabled_and_accepts_a_subscription(vapid):
    from src.app import app, db
    from src.models.push_subscription import PushSubscription

    created = []
    with app.app_context():
        user = make_user(created, prefix='vapid')
        endpoint = f'https://push.example/{uuid.uuid4().hex}'
        try:
            config = anonymous_client().get('/api/push/config')
            assert config.status_code == 200
            assert config.get_json() == {'enabled': True, 'public_key': vapid.VAPID_PUBLIC_KEY}

            response = client_for(user).post('/api/push/subscribe', json={
                'endpoint': endpoint, 'keys': {'p256dh': 'abc', 'auth': 'def'}})
            assert response.status_code == 200
            assert response.get_json()['success'] is True
            rows = PushSubscription.query.filter_by(user_id=user.id).all()
            assert [(row.endpoint, row.p256dh_key, row.auth_key) for row in rows] == [(endpoint, 'abc', 'def')]
        finally:
            PushSubscription.query.filter_by(endpoint=endpoint).delete()
            db.session.commit()
            remove_users(created)


def test_push_config_stays_disabled_without_the_provider(vapid, monkeypatch, tmp_path, capsys):
    from src.app import app

    (tmp_path / 'vapid_keys.json').unlink()
    monkeypatch.setitem(sys.modules, 'py_vapid', None)
    disabled = importlib.reload(vapid)

    assert disabled.VAPID_ENABLED is False
    assert not (tmp_path / 'vapid_keys.json').exists()
    assert 'py_vapid is not installed' in capsys.readouterr().out

    created = []
    with app.app_context():
        user = make_user(created, prefix='vapid')
        try:
            assert anonymous_client().get('/api/push/config').get_json() == {'enabled': False, 'public_key': None}
            response = client_for(user).post('/api/push/subscribe', json={
                'endpoint': f'https://push.example/{uuid.uuid4().hex}', 'keys': {'p256dh': 'abc', 'auth': 'def'}})
            assert response.status_code == 503
        finally:
            remove_users(created)


def test_key_pair_is_kept_in_the_instance_directory_by_default(monkeypatch):
    """Without CONFIG_DIR, the key pair sits next to the secret key, on the volume of the example compose file."""
    from pathlib import Path
    from src.utils import vapid_keys

    monkeypatch.delenv('CONFIG_DIR', raising=False)
    monkeypatch.setattr(Path, 'mkdir', lambda self, *a, **k: None)
    assert vapid_keys.get_vapid_keys_file() == Path('/data/instance/vapid_keys.json')

    monkeypatch.setenv('CONFIG_DIR', '/srv/speakr-config')
    assert vapid_keys.get_vapid_keys_file() == Path('/srv/speakr-config/vapid_keys.json')
