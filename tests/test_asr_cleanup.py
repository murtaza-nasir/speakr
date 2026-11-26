"""The dead paths of the old transcription-model curation, gone for good.

The merged panel writes only ``asr_config_json``, so the connector-wide
discovery endpoint, the visible-model list endpoint and their readers must
not come back. These tests pin that:

- the three removed admin routes answer 404, even for an admin;
- ``asr_config_json`` is a known setting for the audit log, is hidden from
  ``GET /admin/settings`` and cannot be written through ``POST``;
- the module-level aliases the refactor left behind are gone;
- the public API description names the profile document, not the old env list.
"""

import uuid
from pathlib import Path

import pytest

from src.app import app, db
from src.models import SystemSetting, User
from src.services import asr_config as ac
from src.services import audit

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

ROOT = Path(__file__).resolve().parent.parent


def _mk_user(is_admin):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"c_{suffix}", email=f"{suffix}@local.test",
                password="x", is_admin=is_admin)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def ctx():
    with app.app_context():
        SystemSetting.query.filter(
            SystemSetting.key.in_((ac.SETTING_KEY, "transcription_models_visible_json",
                                   "transcription_default_model"))).delete(
            synchronize_session=False)
        db.session.commit()
        yield
        SystemSetting.query.filter(
            SystemSetting.key.in_((ac.SETTING_KEY, "transcription_models_visible_json",
                                   "transcription_default_model"))).delete(
            synchronize_session=False)
        db.session.commit()


@pytest.fixture
def admin_user(ctx):
    user = _mk_user(True)
    yield user
    db.session.delete(user)
    db.session.commit()


@pytest.fixture
def admin_client(admin_user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin_user.id)
        session['_fresh'] = True
    return client


# ------------------------------------------------------------ removed routes


@pytest.mark.parametrize("method,path,body", [
    ("get", "/admin/transcription/discover-models", None),
    ("get", "/admin/transcription/visible-models", None),
    ("post", "/admin/transcription/visible-models", {"options": []}),
])
def test_old_routes_are_gone(admin_client, method, path, body):
    response = getattr(admin_client, method)(path, json=body)
    assert response.status_code == 404, (path, response.status_code, response.get_data(as_text=True)[:200])


def test_eight_asr_endpoints_are_the_only_ones(admin_client):
    rules = {rule.rule for rule in app.url_map.iter_rules()
             if rule.rule.startswith('/admin/asr-config')}
    assert rules == {
        '/admin/asr-config',
        '/admin/asr-config/models',
        '/admin/asr-config/models/<profile_id>',
        '/admin/asr-config/default',
        '/admin/asr-config/test',
        '/admin/asr-config/discover',
        '/admin/asr-config/import',
    }


# ---------------------------------------------------------------- the setting


def test_asr_config_json_is_a_known_setting():
    assert 'asr_config_json' in audit._DEFINED_SETTING_KEYS
    assert audit.known_setting_key('asr_config_json')


def test_settings_get_hides_the_document(admin_client):
    SystemSetting.set_setting(ac.SETTING_KEY, '{"models": []}', None, None)
    response = admin_client.get('/admin/settings')
    assert response.status_code == 200
    keys = [item['key'] for item in response.get_json()]
    assert 'asr_config_json' not in keys


def test_settings_post_refuses_the_document(admin_client):
    response = admin_client.post('/admin/settings',
                                 json={'key': 'asr_config_json', 'value': '{"models": []}'})
    assert response.status_code == 400
    body = response.get_json()
    assert body['code'] == 'asr_config_managed_elsewhere'
    # The refusal leaves the document alone.
    assert SystemSetting.get_setting(ac.SETTING_KEY) is None


def test_admin_page_hides_the_document_from_the_settings_list():
    text = (ROOT / 'templates' / 'admin.html').read_text(encoding='utf-8')
    marker = 'const HIDDEN_SETTING_KEYS = new Set(['
    start = text.index(marker)
    block = text[start:text.index(']);', start)]
    assert "'asr_config_json'" in block


# -------------------------------------------------------------------- aliases


def test_removed_helpers_do_not_come_back():
    assert not hasattr(ac, 'environment_profiles')
    from src.services.transcription import registry
    assert not hasattr(registry, 'get_connector_for')


# --------------------------------------------------------------- public copy


def test_api_description_names_the_profile_document():
    text = (ROOT / 'src' / 'api' / 'api_v1.py').read_text(encoding='utf-8')
    assert 'profiles in asr_config_json' in text
    assert 'TRANSCRIPTION_MODELS_AVAILABLE entries' not in text
    assert 'visible-models list' not in text


def test_no_document_offers_the_env_model_list_as_the_way_to_add_models():
    """TRANSCRIPTION_MODELS_AVAILABLE is read once by the migration; the user
    guides and the example env files point at the Transcription models
    section instead, and the dropdown shows with one visible model."""
    for name in ('recording.md', 'settings.md', 'folders.md'):
        text = (ROOT / 'docs' / 'user-guide' / name).read_text(encoding='utf-8')
        assert 'TRANSCRIPTION_MODELS_AVAILABLE' not in text, name
        assert 'Transcription models' in text, name
    recording = (ROOT / 'docs' / 'user-guide' / 'recording.md').read_text(encoding='utf-8')
    assert 'hidden if only one option' not in recording
    for name in ('env.asr.example', 'env.transcription.example', 'env.whisper.example'):
        text = (ROOT / 'config' / name).read_text(encoding='utf-8')
        assert '# TRANSCRIPTION_MODELS_AVAILABLE=' not in text, name
        assert '# TRANSCRIPTION_MODEL_LABELS=' not in text, name
        assert 'Transcription models' in text, name
