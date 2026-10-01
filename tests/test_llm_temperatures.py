"""Configurable sampling temperatures (#411).

Each kind of request (summary, title, chat, event) is sent with the admin value
from the Default Prompts tab, else the built-in default. An invalid saved value
is ignored and the default is used.
"""

import ast
import os
import sys
import uuid
from unittest.mock import patch, MagicMock

import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, SystemSetting
from src.services import llm_settings as ls

app.config["WTF_CSRF_ENABLED"] = False
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def ctx():
    with app.app_context():
        yield
        db.session.rollback()
        for kind in ls.TEMPERATURES:
            SystemSetting.query.filter_by(key=ls.setting_key(kind)).delete()
        db.session.commit()


def _admin_value(kind, value):
    SystemSetting.set_setting(ls.setting_key(kind), value, setting_type='string')
    db.session.commit()


# ---------------------------------------------------------------- resolution

def test_defaults_are_unchanged_from_earlier_releases(ctx):
    assert {k: ls.get_temperature(k) for k in ls.TEMPERATURES} == {'summary': 0.5, 'title': 0.7, 'chat': 0.7, 'event': 0.2}
    assert ls.resolve_temperature('summary') == (0.5, 'default')


def test_admin_value_overrides_the_default(ctx):
    _admin_value('chat', '0.3')
    assert ls.resolve_temperature('chat') == (0.3, 'admin')
    assert ls.resolve_temperature('title') == (0.7, 'default')


def test_environment_variables_are_not_read(ctx, monkeypatch):
    monkeypatch.setenv('SUMMARY_TEMPERATURE', '0')
    assert ls.resolve_temperature('summary') == (0.5, 'default')


@pytest.mark.parametrize('raw', ['abc', '-0.1', '2.5', 'nan', ' ', ''])
def test_invalid_admin_values_fall_back_to_the_default(ctx, raw):
    _admin_value('event', raw)
    assert ls.resolve_temperature('event') == (0.2, 'default')


@pytest.mark.parametrize('raw,expected', [('0', 0.0), ('2', 2.0), ('1.25', 1.25), (0.3, 0.3), (' 0.5 ', 0.5)])
def test_bounds_are_inclusive(raw, expected):
    assert ls.parse_temperature(raw) == expected


def test_an_admin_change_applies_to_the_next_request(ctx):
    assert ls.get_temperature('summary') == 0.5
    _admin_value('summary', '0.1')
    assert ls.get_temperature('summary') == 0.1


# ---------------------------------------------------------------- call sites

CALL_SITES = {
    os.path.join('src', 'tasks', 'processing.py'): {
        '_generate_ai_title': 'title',
        'generate_summary_only_task': 'summary',
        'extract_events_from_transcript': 'event',
        '_generate_incognito_title': 'title',
        'generate_incognito_summary': 'summary',
    },
    os.path.join('src', 'api', 'recordings.py'): {
        'chat_incognito': 'chat',
        'chat_with_transcription': 'chat',
    },
}


def _temperature_kinds(func):
    kinds = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and getattr(node.func, 'id', None) == 'get_temperature':
            kinds.append(node.args[0].value)
    return kinds


@pytest.mark.parametrize('path', list(CALL_SITES))
def test_every_call_site_sends_its_configured_temperature(path):
    tree = ast.parse(open(os.path.join(ROOT, path), encoding='utf-8').read())
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name, kind in CALL_SITES[path].items():
        assert _temperature_kinds(funcs[name]) == [kind], name
        passed = [kw.value for n in ast.walk(funcs[name]) if isinstance(n, ast.Call)
                  for kw in n.keywords if kw.arg == 'temperature']
        assert passed, f"{name} sends no temperature"
        for value in passed:
            # either get_temperature(kind) inline, or a variable assigned from it
            ok = (isinstance(value, ast.Call) and getattr(value.func, 'id', None) == 'get_temperature') or (
                isinstance(value, ast.Name) and any(
                    isinstance(a, ast.Assign) and any(getattr(t, 'id', None) == value.id for t in a.targets)
                    and isinstance(a.value, ast.Call) and getattr(a.value.func, 'id', None) == 'get_temperature'
                    for a in ast.walk(funcs[name])))
            assert ok, f"{name} sends temperature={ast.unparse(value)}"
    literal = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.keyword) and n.arg == 'temperature' and isinstance(n.value, ast.Constant)]
    assert literal == [], f"hard-coded temperature in {path} at lines {literal}"


def test_incognito_title_and_summary_use_the_resolved_values(ctx):
    from src.tasks import processing
    _admin_value('title', '0.15')
    _admin_value('summary', '0')
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content='A title'))]
    seen = []

    def fake(*args, **kwargs):
        seen.append(kwargs.get('temperature'))
        return completion

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = fake
    with patch.object(processing, 'client', fake_client):
        processing._generate_incognito_title('Some transcript text about budgets.')
        processing.generate_incognito_summary('Some transcript text about budgets.')
    assert seen == [0.15, 0.0]


# ---------------------------------------------------------------- admin API

def _client(user):
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


@pytest.fixture
def users(ctx):
    s = uuid.uuid4().hex[:8]
    admin = User(username=f"tA_{s}", email=f"ta_{s}@local.test", password="x", is_admin=True)
    plain = User(username=f"tU_{s}", email=f"tu_{s}@local.test", password="x")
    db.session.add_all([admin, plain])
    db.session.commit()
    yield admin, plain
    db.session.delete(db.session.get(User, admin.id))
    db.session.delete(db.session.get(User, plain.id))
    db.session.commit()


def test_api_requires_an_admin(users):
    _, plain = users
    c = _client(plain)
    assert c.get('/admin/llm-temperatures').status_code == 403
    assert c.post('/admin/llm-temperatures', json={'values': {'summary': 0}}).status_code == 403


def test_api_reports_values_and_sources(users):
    admin, _ = users
    _admin_value('chat', '0.9')
    data = _client(admin).get('/admin/llm-temperatures').get_json()['temperatures']
    assert data['chat'] == {'value': 0.9, 'source': 'admin', 'default': 0.7}
    assert data['summary'] == {'value': 0.5, 'source': 'default', 'default': 0.5}


def test_api_saves_and_clears(users):
    admin, _ = users
    c = _client(admin)
    r = c.post('/admin/llm-temperatures', json={'values': {'summary': 0, 'event': '0.1'}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert ls.resolve_temperature('summary') == (0.0, 'admin')
    assert ls.resolve_temperature('event') == (0.1, 'admin')
    r = c.post('/admin/llm-temperatures', json={'values': {'summary': None}})
    assert r.status_code == 200
    assert ls.resolve_temperature('summary') == (0.5, 'default')
    assert ls.resolve_temperature('event') == (0.1, 'admin')


@pytest.mark.parametrize('values', [{'summary': 3}, {'summary': 'hot'}, {'summary': True}, {'nope': 0.1}, {}])
def test_api_rejects_invalid_input_and_saves_nothing(users, values):
    admin, _ = users
    r = _client(admin).post('/admin/llm-temperatures', json={'values': values})
    assert r.status_code == 400
    assert SystemSetting.query.filter(SystemSetting.key.like('llm_temperature_%')).count() == 0
