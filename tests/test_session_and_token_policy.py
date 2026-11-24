"""Session idle timeout, the ?token= switch and the startup warnings
(privacy review A4 and A5).

* session_idle_timeout_minutes (SESSION_IDLE_TIMEOUT_MINUTES, default 0: off):
  a browser session with no request for that long must sign in again. API
  tokens are not affected, the requests a page sends by itself do not count
  as activity, and the remember-me cookie is not accepted while it is set.
* api_query_tokens_enabled (API_QUERY_TOKENS_ENABLED, default false, #688):
  off refuses a token in ?token= on every method; on accepts it on GET and
  HEAD.
* A warning when Speakr is reached over HTTPS, or APP_BASE_URL is https,
  while SESSION_COOKIE_SECURE is false.
* A warning per service whose address is plain http to another machine.

SHARED-DB: the user, the token and the settings are restored.
"""

import logging
import os
import secrets
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, SystemSetting, User
from src.services import session_policy
from src.config import outbound_warnings as ow
from src.utils.system_settings import ENV_BACKED_SETTINGS
from src.utils.token_auth import hash_token

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

KEYS = ('session_idle_timeout_minutes', 'api_query_tokens_enabled')
ACTIVE = '/api/shares'                      # needs a signed-in user, answers JSON
PASSIVE = '/api/notifications/count'        # polled by the page every minute


@pytest.fixture
def world(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(ENV_BACKED_SETTINGS[key][0], raising=False)
    with app.app_context():
        saved = {}
        for key in KEYS:
            row = SystemSetting.query.filter_by(key=key).first()
            saved[key] = (row.value, row.setting_type) if row else None
            SystemSetting.set_setting(key, None, ENV_BACKED_SETTINGS[key][3], ENV_BACKED_SETTINGS[key][2])
        s = secrets.token_hex(4)
        user = User(username=f"sp_{s}", email=f"sp_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        plain = f"tok-{secrets.token_urlsafe(16)}"
        db.session.add(APIToken(user_id=user.id, token_hash=hash_token(plain), name="t"))
        db.session.commit()
        ids = dict(user=user.id, session_id=user.get_id(), plain=plain)
    yield ids
    with app.app_context():
        db.session.rollback()
        APIToken.query.filter_by(user_id=ids["user"]).delete()
        User.query.filter_by(id=ids["user"]).delete()
        db.session.commit()
        for key, value in saved.items():
            if value is None:
                row = SystemSetting.query.filter_by(key=key).first()
                if row:
                    db.session.delete(row)
                    db.session.commit()
            else:
                SystemSetting.set_setting(key, value[0], None, value[1])


def _signed_in(world, last_activity=None):
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = world["session_id"]
        s['_fresh'] = True
        if last_activity is not None:
            s[session_policy.LAST_ACTIVITY_KEY] = last_activity
    return client


def _session(client):
    with client.session_transaction() as s:
        return dict(s)


def _timeout(minutes):
    with app.app_context():
        SystemSetting.set_setting('session_idle_timeout_minutes', str(minutes), setting_type='integer')


# --- idle timeout -------------------------------------------------------------

def test_defaults():
    assert ENV_BACKED_SETTINGS['session_idle_timeout_minutes'][:3] == ('SESSION_IDLE_TIMEOUT_MINUTES', 0, 'integer')
    assert ENV_BACKED_SETTINGS['api_query_tokens_enabled'][:3] == ('API_QUERY_TOKENS_ENABLED', False, 'boolean')


def test_with_the_timeout_off_nothing_is_checked_or_written(world):
    client = _signed_in(world, last_activity=int(time.time()) - 10 * 24 * 3600)
    assert client.get(ACTIVE).status_code == 200
    fresh = _signed_in(world)
    assert fresh.get(ACTIVE).status_code == 200
    assert session_policy.LAST_ACTIVITY_KEY not in _session(fresh)


def test_an_idle_session_must_sign_in_again(world):
    _timeout(15)
    client = _signed_in(world)
    assert client.get(ACTIVE).status_code == 200
    stamped = _session(client)[session_policy.LAST_ACTIVITY_KEY]
    assert abs(stamped - time.time()) < 5
    with client.session_transaction() as s:
        s[session_policy.LAST_ACTIVITY_KEY] = int(time.time()) - 16 * 60
    resp = client.get(ACTIVE)
    assert resp.status_code == 401 and resp.get_json()['code'] == 'authentication_required'
    after = _session(client)
    assert '_user_id' not in after and session_policy.LAST_ACTIVITY_KEY not in after
    # A page request is sent to the sign-in page.
    page = client.get('/')
    assert page.status_code in (301, 302) and '/login' in page.headers['Location']


def test_a_session_within_the_timeout_stays_signed_in_and_is_renewed(world):
    _timeout(15)
    earlier = int(time.time()) - 14 * 60
    client = _signed_in(world, last_activity=earlier)
    assert client.get(ACTIVE).status_code == 200
    assert _session(client)[session_policy.LAST_ACTIVITY_KEY] > earlier


def test_env_value_applies_until_a_setting_is_saved(world, monkeypatch):
    monkeypatch.setenv('SESSION_IDLE_TIMEOUT_MINUTES', '5')
    client = _signed_in(world, last_activity=int(time.time()) - 6 * 60)
    assert client.get(ACTIVE).status_code == 401
    _timeout(0)
    client = _signed_in(world, last_activity=int(time.time()) - 6 * 60)
    assert client.get(ACTIVE).status_code == 200


def test_requests_the_page_sends_by_itself_do_not_count_as_activity(world):
    _timeout(15)
    earlier = int(time.time()) - 10 * 60
    client = _signed_in(world, last_activity=earlier)
    for path in sorted(session_policy.PASSIVE_PATHS):
        assert client.get(path).status_code == 200, path
        assert _session(client)[session_policy.LAST_ACTIVITY_KEY] == earlier, path
    # They are refused once the session has timed out.
    with client.session_transaction() as s:
        s[session_policy.LAST_ACTIVITY_KEY] = int(time.time()) - 16 * 60
    assert client.get(PASSIVE).status_code == 401


def test_passive_paths_are_real_routes():
    with app.app_context():
        rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert session_policy.PASSIVE_PATHS <= rules


def test_api_tokens_are_not_affected_by_the_timeout(world):
    _timeout(1)
    headers = {'Authorization': f"Bearer {world['plain']}"}
    with app.test_client() as c:
        assert c.get('/api/v1/recordings', headers=headers).status_code == 200
        assert c.get('/api/v1/recordings', headers=headers).status_code == 200
        with c.session_transaction() as s:
            assert session_policy.LAST_ACTIVITY_KEY not in s


def _remembered(world):
    from flask_login.utils import encode_cookie
    client = app.test_client()
    with app.app_context():
        client.set_cookie('remember_token', encode_cookie(world["session_id"]))
    return client


def test_remember_me_cookie_is_accepted_without_a_timeout(world):
    assert _remembered(world).get(ACTIVE).status_code == 200


def test_remember_me_cookie_is_not_accepted_with_a_timeout(world):
    _timeout(15)
    client = _remembered(world)
    resp = client.get(ACTIVE)
    assert resp.status_code == 401
    # The cookie is removed by the answer.
    cleared = [h for h in resp.headers.getlist('Set-Cookie') if h.startswith('remember_token=')]
    assert cleared and ('Max-Age=0' in cleared[0] or 'Expires=Thu, 01 Jan 1970' in cleared[0])


# --- ?token= ------------------------------------------------------------------

def _query_tokens(value):
    with app.app_context():
        SystemSetting.set_setting('api_query_tokens_enabled', value, setting_type='boolean')


def test_query_token_disabled_by_default(world):
    """#688, #691: with no value saved and nothing in .env, a token in
    ?token= authenticates nothing, on any method."""
    with app.test_client() as c:
        resp = c.get(f"/api/v1/recordings?token={world['plain']}")
        assert resp.status_code == 401 and resp.get_json()['code'] == 'query_token_disabled'
        assert c.head(f"/api/v1/recordings?token={world['plain']}").status_code == 401
        resp = c.post(f"/api/v1/tags?token={world['plain']}", json={'name': 'via-query'})
        assert resp.status_code == 401 and resp.get_json()['code'] == 'query_token_disabled'


@pytest.mark.parametrize("how", ["setting", "env"])
def test_query_token_still_works_when_opted_in(world, monkeypatch, how):
    if how == "env":
        monkeypatch.setenv('API_QUERY_TOKENS_ENABLED', 'true')
    else:
        _query_tokens('true')
    with app.test_client() as c:
        for method in ('get', 'head'):
            resp = getattr(c, method)(f"/api/v1/recordings?token={world['plain']}")
            assert resp.status_code == 200, method
            assert resp.headers.get('Deprecation') == 'true', method


@pytest.mark.parametrize("header", ["Authorization", "X-API-Token", "API-Token"])
def test_header_token_still_authenticates_by_default(world, header):
    value = f"Bearer {world['plain']}" if header == "Authorization" else world['plain']
    with app.test_client() as c:
        resp = c.get('/api/v1/recordings', headers={header: value})
    assert resp.status_code == 200 and 'Deprecation' not in resp.headers


@pytest.mark.parametrize("how", ["setting", "env"])
def test_query_token_is_refused_on_every_method_when_turned_off(world, monkeypatch, how):
    if how == "env":
        monkeypatch.setenv('API_QUERY_TOKENS_ENABLED', 'false')
    else:
        _query_tokens('false')
    with app.test_client() as c:
        for method, path in (('get', '/api/v1/recordings'), ('head', '/api/v1/recordings'),
                             ('post', '/api/v1/tags'), ('delete', '/api/v1/tags/999999')):
            resp = getattr(c, method)(f"{path}?token={world['plain']}")
            assert resp.status_code == 401, method
            assert resp.headers.get('WWW-Authenticate', '').startswith('Bearer')
            if method != 'head':
                assert resp.get_json()['code'] == 'query_token_disabled'
        # The audio route a media element would call with ?token=.
        assert c.get(f"/api/v1/recordings/1/audio?token={world['plain']}").status_code == 401
        # A header still works, and an unknown token is answered as before.
        assert c.get('/api/v1/recordings', headers={'Authorization': f"Bearer {world['plain']}"}).status_code == 200
        unknown = c.get('/api/v1/recordings?token=not-a-token')
        assert unknown.status_code == 401 and unknown.get_json()['code'] == 'authentication_required'


def test_a_browser_session_is_not_affected_by_the_token_switch(world):
    _query_tokens('false')
    client = _signed_in(world)
    assert client.get(f"{ACTIVE}?token={world['plain']}").status_code == 200


# --- session cookie without Secure --------------------------------------------

def test_startup_warning_for_an_https_base_url_without_a_secure_cookie():
    warn = session_policy.secure_cookie_startup_warning
    assert warn({'SESSION_COOKIE_SECURE': False}, 'https://speakr.example.com') and \
        'SESSION_COOKIE_SECURE=true' in warn({'SESSION_COOKIE_SECURE': False}, 'https://speakr.example.com')
    assert warn({'SESSION_COOKIE_SECURE': True}, 'https://speakr.example.com') is None
    assert warn({'SESSION_COOKIE_SECURE': False}, 'http://speakr.lan') is None
    assert warn({'SESSION_COOKIE_SECURE': False}, None) is None


def test_first_https_request_without_a_secure_cookie_is_warned_once(world, caplog, monkeypatch):
    monkeypatch.setattr(session_policy, '_https_cookie_warned', False)
    monkeypatch.setitem(app.config, 'SESSION_COOKIE_SECURE', False)
    caplog.set_level(logging.WARNING)
    with app.test_client() as c:
        c.get('/login', base_url='http://localhost')
        assert not [r for r in caplog.records if 'SESSION_COOKIE_SECURE' in r.getMessage()]
        c.get('/login', base_url='https://localhost')
        c.get('/login', base_url='https://localhost')
    assert len([r for r in caplog.records if 'SESSION_COOKIE_SECURE' in r.getMessage()]) == 1


def test_no_https_warning_with_a_secure_cookie(world, caplog, monkeypatch):
    monkeypatch.setattr(session_policy, '_https_cookie_warned', False)
    monkeypatch.setitem(app.config, 'SESSION_COOKIE_SECURE', True)
    caplog.set_level(logging.WARNING)
    with app.test_client() as c:
        c.get('/login', base_url='https://localhost')
    assert not [r for r in caplog.records if 'SESSION_COOKIE_SECURE' in r.getMessage()]


# --- plain http to another machine ---------------------------------------------

@pytest.mark.parametrize("url,host", [
    ('http://192.168.1.20:9000', '192.168.1.20'),       # a private range still crosses a network
    ('http://10.0.0.5/v1', '10.0.0.5'),
    ('http://asr.internal.example:9000/asr', 'asr.internal.example'),
    ('http://user:secret-password@llm.example.com/v1?api_key=sk-secret', 'llm.example.com'),
    ('http://[2001:db8::1]:9000', '2001:db8::1'),
    ('HTTP://GPU-BOX.lan:8000', 'gpu-box.lan'),
])
def test_plain_http_to_another_machine_warns(url, host):
    assert ow.plain_http_host(url) == host


@pytest.mark.parametrize("url", [
    'https://api.openai.com/v1', 'http://localhost:9000', 'http://127.0.0.1:9000', 'http://127.8.8.8',
    'http://[::1]:9000', 'http://whisperx:9000', 'http://whisperx-asr:9000/asr', 'http://app.localhost:8080',
    'http://host.docker.internal:11434/v1', '', None, 'not a url', 'http://',
])
def test_local_and_https_addresses_do_not_warn(url):
    assert ow.plain_http_host(url) is None


def test_one_line_per_service_with_the_host_only():
    environ = {
        'ASR_BASE_URL': 'http://192.168.1.20:9000  # the GPU box',
        'TEXT_MODEL_BASE_URL': 'http://user:secret-password@llm.example.com/v1/private-path?api_key=sk-secret',
        'CHAT_MODEL_BASE_URL': 'https://openrouter.ai/api/v1',
        'EMBEDDING_BASE_URL': 'http://embeddings:8080',
        'TRANSLATION_MODEL_BASE_URL': '',
    }
    lines = ow.plain_http_warnings(environ)
    assert len(lines) == 2
    assert 'transcription service (ASR_BASE_URL)' in lines[0] and 'host 192.168.1.20:' in lines[0]
    assert 'text model (TEXT_MODEL_BASE_URL)' in lines[1] and 'host llm.example.com:' in lines[1]
    text = "\n".join(lines)
    for secret in ('secret-password', 'sk-secret', 'private-path', 'api_key', 'user:', '9000'):
        assert secret not in text
    assert all('\n' not in line for line in lines)


def test_the_warnings_are_logged_at_startup(caplog):
    caplog.set_level(logging.WARNING)
    lines = ow.log_plain_http_warnings(app, {'EMBEDDING_BASE_URL': 'http://172.16.4.4:8080/v1'})
    assert len(lines) == 1
    assert any('embedding service (EMBEDDING_BASE_URL)' in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)
    assert ow.log_plain_http_warnings(app, {}) == []
