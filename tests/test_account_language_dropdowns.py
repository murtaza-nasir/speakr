"""Account language dropdowns (issue #613).

The Transcription language select is filled in the browser from the shared
list in static/js/asr-languages.js, and OUTPUT_LANGUAGES holds the same
languages. A stored value outside either list is rendered as a selected
option and survives a save of the preferences.

SHARED-DB: the users are removed afterwards.
"""

import contextlib
import os
import re
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import User  # noqa: E402
from src.utils.language import (OUTPUT_LANGUAGES, SUPPORTED_CODES, canonical_translation_language,  # noqa: E402
                                output_language_name_for_code)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The names and codes offered before the list was extended. A user's stored
# output language is one of these names, so none may change.
ORIGINAL_OUTPUT_LANGUAGES = [
    ("English", "en"), ("Spanish", "es"), ("French", "fr"), ("German", "de"), ("Italian", "it"),
    ("Portuguese", "pt"), ("Dutch", "nl"), ("Russian", "ru"), ("Chinese", "zh"), ("Japanese", "ja"),
    ("Korean", "ko"), ("Arabic", "ar"), ("Hindi", "hi"), ("Polish", "pl"), ("Ukrainian", "uk"),
    ("Vietnamese", "vi"), ("Thai", "th"), ("Turkish", "tr"), ("Indonesian", "id"), ("Swedish", "sv"),
    ("Portuguese (Brazilian)", "pt-BR"),
]


def _shared_js_codes():
    with open(os.path.join(ROOT, 'static', 'js', 'asr-languages.js'), encoding='utf-8') as f:
        source = f.read()
    block = re.search(r"const CODES = \[(.*?)\];", source, re.S).group(1)
    return re.findall(r"'([a-z]+)'", block)


@pytest.fixture(autouse=True)
def _disable_csrf():
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    yield
    app.config['WTF_CSRF_ENABLED'] = prev


@pytest.fixture
def make_user():
    ids = []
    with app.app_context():
        def _make(**extra):
            s = uuid.uuid4().hex[:10]
            user = User(username=f"lang_{s}", email=f"lang_{s}@local.test", password="x", **extra)
            db.session.add(user)
            db.session.commit()
            ids.append(user.id)
            return user.id
        yield _make
        db.session.rollback()
        User.query.filter(User.id.in_(ids or [0])).delete(synchronize_session=False)
        db.session.commit()


@contextlib.contextmanager
def _client(user_id):
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True
    yield client


def _select(html, select_id):
    return re.search(r'<select[^>]*id="%s".*?</select>' % select_id, html, re.S).group(0)


def _selected_values(select_html):
    return re.findall(r'<option value="([^"]*)"[^>]*\sselected', select_html)


PREFERENCES = {'preferences_form': '1', 'ui_language': 'en'}


# --- the lists ---------------------------------------------------------------

def test_shared_js_list_matches_the_server_codes():
    codes = _shared_js_codes()
    assert len(codes) == len(set(codes)) == 100
    assert set(codes) == set(SUPPORTED_CODES)


def test_output_languages_hold_the_shared_list():
    names = [name for name, _ in OUTPUT_LANGUAGES]
    codes = [code for _, code in OUTPUT_LANGUAGES]
    assert len(set(names)) == len(names) and len(set(codes)) == len(codes)
    assert set(codes) == set(SUPPORTED_CODES) | {"pt-BR"}
    assert names == sorted(names)


def test_original_output_languages_keep_their_spelling():
    for entry in ORIGINAL_OUTPUT_LANGUAGES:
        assert entry in OUTPUT_LANGUAGES


def test_new_languages_resolve_for_translation():
    assert canonical_translation_language("Czech") == "Czech"
    assert canonical_translation_language("cs") == "Czech"
    assert output_language_name_for_code("hu") == "Hungarian"
    assert output_language_name_for_code("pt-BR") == "Portuguese (Brazilian)"
    assert canonical_translation_language("Klingon") is None


# --- the account page --------------------------------------------------------

def test_transcription_select_is_filled_from_the_shared_list(make_user):
    uid = make_user(transcription_language='cs')
    with _client(uid) as client:
        html = client.get('/account').get_data(as_text=True)
    select = _select(html, 'transcription_language')
    # Auto-detect and the stored value only: no hardcoded list, no pt-BR entry.
    assert re.findall(r'<option value="([^"]*)"', select) == ['', 'cs']
    assert _selected_values(select) == ['cs']
    assert 'js/asr-languages.js' in html
    assert 'fillLanguageDropdowns' in html


def test_transcription_select_without_a_stored_value_selects_auto_detect(make_user):
    uid = make_user()
    with _client(uid) as client:
        html = client.get('/account').get_data(as_text=True)
    select = _select(html, 'transcription_language')
    assert re.findall(r'<option value="([^"]*)"', select) == ['']
    assert _selected_values(select) == ['']


def test_output_select_offers_the_full_list(make_user):
    uid = make_user(output_language='Czech')
    with _client(uid) as client:
        html = client.get('/account').get_data(as_text=True)
    select = _select(html, 'output_language')
    values = re.findall(r'<option value="([^"]*)"', select)
    assert values == [''] + [name for name, _ in OUTPUT_LANGUAGES]
    assert _selected_values(select) == ['Czech']
    assert 'data-lang-code="cs"' in select


def test_stored_values_outside_the_lists_are_rendered_selected(make_user):
    uid = make_user(transcription_language='xx-legacy', output_language='Klingon')
    with _client(uid) as client:
        html = client.get('/account').get_data(as_text=True)
    assert _selected_values(_select(html, 'transcription_language')) == ['xx-legacy']
    output = _select(html, 'output_language')
    assert _selected_values(output) == ['Klingon']
    assert output.count('value="Klingon"') == 1


def test_save_keeps_stored_values_outside_the_lists(make_user):
    uid = make_user(transcription_language='xx-legacy', output_language='Klingon')
    with _client(uid) as client:
        # What the page posts: both selects send the option that is selected.
        resp = client.post('/account', data=dict(PREFERENCES, ui_language='de',
                                                 transcription_language='xx-legacy', output_language='Klingon'))
        assert resp.status_code == 302
    with app.app_context():
        user = db.session.get(User, uid)
        assert user.ui_language == 'de'
        assert user.transcription_language == 'xx-legacy'
        assert user.output_language == 'Klingon'


def test_save_still_normalizes_and_clears_the_transcription_language(make_user):
    uid = make_user(transcription_language='xx-legacy', output_language='Klingon')
    with _client(uid) as client:
        client.post('/account', data=dict(PREFERENCES, transcription_language='cs', output_language='Czech'))
    with app.app_context():
        user = db.session.get(User, uid)
        assert user.transcription_language == 'cs' and user.output_language == 'Czech'
    with _client(uid) as client:
        # An unknown value that is not the stored one is not accepted.
        client.post('/account', data=dict(PREFERENCES, transcription_language='not-a-language',
                                          output_language=''))
    with app.app_context():
        user = db.session.get(User, uid)
        assert user.transcription_language is None and user.output_language is None


def test_portuguese_brazil_is_stored_as_portuguese(make_user):
    uid = make_user()
    with _client(uid) as client:
        client.post('/account', data=dict(PREFERENCES, transcription_language='pt-BR'))
    with app.app_context():
        assert db.session.get(User, uid).transcription_language == 'pt'


def test_translation_menu_is_built_from_the_list(make_user):
    for name in ('desktop-transcription-panel.html', 'mobile-transcript-panel.html'):
        with open(os.path.join(ROOT, 'templates', 'components', 'detail', name), encoding='utf-8') as f:
            source = f.read()
        assert 'v-for="option in translationLanguageOptions"' in source
    uid = make_user()
    with _client(uid) as client:
        html = client.get('/').get_data(as_text=True)
    assert '[&#34;Czech&#34;, &#34;cs&#34;]' in html or '["Czech", "cs"]' in html
