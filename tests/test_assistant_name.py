"""The assistant's name (Ambrose by default), saved per user (User.assistant_name).

The name is set in Account -> Preferences or with POST /api/user/preferences,
reaches the scripts as window.speakrAssistantName and fills {{name}} in the
assistant's strings: the recording chat, the Ask page (/inquire, alias /ask)
and the header button.

SHARED-DB: the test users are removed afterwards.
"""

import json
import os
import re
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User
from src.utils.assistant_name import (
    DEFAULT_ASSISTANT_NAME, assistant_initial, assistant_name_of, clean_assistant_name,
)

app.config["WTF_CSRF_ENABLED"] = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALES = ('en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh')
TEMPLATES = os.path.join(ROOT, 'templates')


@pytest.fixture
def user():
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f"ambrose_{s}", email=f"ambrose_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _client(uid):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return client


def _page_name(html):
    m = re.search(r"window\.speakrAssistantName = (\".*?\");", html)
    return json.loads(m.group(1)) if m else None


def _saved(uid):
    with app.app_context():
        return db.session.get(User, uid).assistant_name


# --- The rule -----------------------------------------------------------------

@pytest.mark.parametrize('value, stored', [
    (None, None), ('', None), ('   ', None), ('Ambrose', None), ('  Ambrose ', None),
    ('Jeeves', 'Jeeves'), ('  Mr.  Jeeves ', 'Mr. Jeeves'), ("O'Brien", "O'Brien"),
    ('Zoë-Ann', 'Zoë-Ann'), ('小助手', '小助手'), ('x' * 40, 'x' * 40),
])
def test_names_are_trimmed_and_the_default_is_not_stored(value, stored):
    assert clean_assistant_name(value) == stored


@pytest.mark.parametrize('value', ['x' * 41, '-Ann', '${evil}', '<b>', 'a{{b}}', 'tab\tname!', 7])
def test_names_outside_the_rule_are_refused(value):
    with pytest.raises(ValueError):
        clean_assistant_name(value)


def test_the_name_shown_falls_back_to_the_default():
    class U:
        assistant_name = None
    assert assistant_name_of(None) == DEFAULT_ASSISTANT_NAME == 'Ambrose'
    assert assistant_name_of(U()) == 'Ambrose'
    U.assistant_name = '<script>'
    assert assistant_name_of(U()) == 'Ambrose'
    U.assistant_name = 'jeeves'
    assert assistant_name_of(U()) == 'jeeves'
    assert assistant_initial('jeeves') == 'J'


# --- Saving it ----------------------------------------------------------------

def test_preferences_api_saves_and_clears_the_name(user):
    client = _client(user)
    resp = client.post("/api/user/preferences", json={"assistant_name": "  Jeeves "})
    assert resp.status_code == 200
    assert resp.get_json()["assistant_name"] == "Jeeves"
    assert _saved(user) == "Jeeves"
    resp = client.post("/api/user/preferences", json={"assistant_name": ""})
    assert resp.get_json()["assistant_name"] == "Ambrose"
    assert _saved(user) is None


def test_other_preferences_leave_the_name_alone(user):
    client = _client(user)
    client.post("/api/user/preferences", json={"assistant_name": "Jeeves"})
    assert client.post("/api/user/preferences", json={"color_scheme": "teal"}).status_code == 200
    assert _saved(user) == "Jeeves"


@pytest.mark.parametrize('value', ['x' * 41, '<b>bold</b>', 12])
def test_preferences_api_refuses_a_bad_name(user, value):
    resp = _client(user).post("/api/user/preferences", json={"assistant_name": value})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_assistant_name"
    assert _saved(user) is None


def test_account_preferences_form_saves_the_name(user):
    client = _client(user)
    resp = client.post("/account", data={"preferences_form": "1", "ui_language": "en",
                                         "assistant_name": "Jeeves"},
                       headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code in (200, 302)
    assert _saved(user) == "Jeeves"
    # A preferences submission without the field keeps it.
    client.post("/account", data={"preferences_form": "1", "ui_language": "en"},
                headers={"X-Requested-With": "XMLHttpRequest"})
    assert _saved(user) == "Jeeves"
    page = client.get("/account").get_data(as_text=True)
    assert 'name="assistant_name"' in page and 'value="Jeeves"' in page and 'maxlength="40"' in page


# --- Showing it ---------------------------------------------------------------

def test_pages_carry_the_default_name(user):
    page = _client(user).get("/").get_data(as_text=True)
    assert _page_name(page) == "Ambrose"


def test_pages_carry_the_saved_name(user):
    client = _client(user)
    client.post("/api/user/preferences", json={"assistant_name": "Jeeves"})
    for path in ("/", "/account"):
        page = client.get(path).get_data(as_text=True)
        assert _page_name(page) == "Jeeves", path
    # The monogram of the recording chat (includes/assistant_avatar.html).
    page = client.get("/").get_data(as_text=True)
    assert '<span class="assistant-avatar assistant-avatar--sm" aria-hidden="true" v-pre>J</span>' in page


def test_the_ask_page_is_titled_with_the_name(user, monkeypatch):
    from src.api import inquire as inquire_api
    monkeypatch.setattr(inquire_api, 'ENABLE_INQUIRE_MODE', True)
    client = _client(user)
    client.post("/api/user/preferences", json={"assistant_name": "Jeeves"})
    for path in ("/inquire", "/ask"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        page = resp.get_data(as_text=True)
        assert "<title>Ask Jeeves - Speakr</title>" in page, path


# --- Markup and strings -------------------------------------------------------

def _read(*parts):
    with open(os.path.join(TEMPLATES, *parts), encoding='utf-8') as f:
        return f.read()


@pytest.mark.parametrize('template', [
    ('components', 'detail', 'floating-chat.html'),
    ('components', 'detail', 'mobile-chat-panel.html'),
])
def test_recording_chat_shows_the_assistant(template):
    html = _read(*template)
    assert 'fa-robot' not in html
    assert "includes/assistant_avatar.html" in html
    assert "t('assistant.scopeRecording')" in html
    assert "assistant.recordingIntro" not in html
    assert "t('chat.placeholder', { name: assistantName })" in html
    assert ":title=\"t('assistant.sendHint')\"" in html


def test_chat_button_keeps_the_chat_icon():
    # The launcher shows the chat symbol; the name is in its label and tooltip.
    html = _read('components', 'detail', 'floating-chat.html')
    start = html.index('class="floating-chat-fab"')
    fab = html[start:html.index('</button>', start)]
    assert 'fa-comments' in fab
    assert 'assistant_avatar' not in fab
    assert ":aria-label=\"t('assistant.ask', { name: assistantName })\"" in fab


def test_ask_page_shows_the_assistant():
    html = _read('inquire.html')
    assert "t('inquire.title', { name: assistantName })" in html
    assert "t('inquire.askQuestions', { name: assistantName })" in html
    assert "t('inquire.placeholder', { name: assistantName })" in html
    assert html.count('v-text="inquireScopeLabel"') == 2
    assert "t('assistant.scopeLibrary')" in html
    assert 'inquire.sendHint' not in html
    # Vue's ${ } delimiters end at the first }, so an object literal cannot go inside them.
    assert not re.search(r'\$\{[^}]*\{', html)


def test_header_button_uses_the_name():
    html = _read('components', 'header-controls.html')
    assert html.count("t('inquire.title', { name: assistantName })") == 2
    assert "includes/assistant_avatar.html" in html


def _flat(d, prefix=''):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _flat(v, f'{prefix}{k}.')
        else:
            yield f'{prefix}{k}', v


NAMED_KEYS = ('assistant.ask', 'chat.placeholder',
              'chat.availableAfterTranscription', 'chat.cannotChatTranscriptionFailed',
              'inquire.title', 'inquire.askQuestions', 'inquire.placeholder',
              'inquirePrivacy.title', 'inquirePrivacy.description', 'inquirePrivacy.summariesHelp')


@pytest.mark.parametrize('locale', LOCALES)
def test_assistant_strings_carry_the_name(locale):
    with open(os.path.join(ROOT, 'static', 'locales', f'{locale}.json'), encoding='utf-8') as f:
        flat = dict(_flat(json.load(f)))
    for key in NAMED_KEYS:
        assert '{{name}}' in flat[key], (locale, key)
    for key in NAMED_KEYS + ('assistant.sendHint', 'assistant.scopeRecording', 'assistant.scopeLibrary',
                             'adminDashboard.inquireModeDescription', 'inquire.contextMeter'):
        value = flat[key]
        assert '—' not in value, (locale, key)
        assert 'Inquire' not in value, (locale, key)
    if locale == 'en':
        for key in NAMED_KEYS + ('adminDashboard.inquireModeDescription',):
            assert not re.search(r'\bworth\b|rather than', flat[key], re.I), key
        assert not re.search(r'chunk|embedding', flat['adminDashboard.inquireModeDescription'], re.I)
