"""Server-generated text in the user's interface language (#477).

translate() resolves the same static/locales/*.json catalogs the browser uses.
Flash messages, the verification and password-reset emails, budget messages
and API error and info messages go through it. API errors also carry a code
that is the same in every language.
"""

import os
import uuid
from email import message_from_string
from email.header import decode_header, make_header
from unittest import mock

import pytest

from src.app import app, db
from src.models import User
from src.services import email as email_service
from src.utils.localization import (
    api_error, format_file_size, format_number, format_percent, match_language, request_language, translate,
)


# ---------------------------------------------------------------------------
# translate()
# ---------------------------------------------------------------------------

def test_translate_returns_the_requested_language():
    assert translate('server.flash.userNotFound', 'de') == 'Benutzer nicht gefunden.'
    assert translate('server.flash.userNotFound', 'en') == 'User not found.'


def test_translate_interpolates_double_brace_placeholders():
    assert translate('server.flash.verificationCooldown', 'en', seconds=42) == (
        'Please wait 42 seconds before requesting another verification email.')
    assert '42' in translate('server.flash.verificationCooldown', 'zh', seconds=42)


def test_translate_leaves_unfilled_placeholders_as_is():
    assert '{{seconds}}' in translate('server.flash.verificationCooldown', 'en')


def test_translate_missing_key_returns_the_key():
    assert translate('server.flash.noSuchKey', 'de') == 'server.flash.noSuchKey'


def test_translate_falls_back_to_english_for_a_key_missing_in_the_language():
    catalogs = {'de': {'server': {}}, 'en': {'server': {'only': 'English only'}}}
    # Patched through translate's own globals: other tests re-import src
    # modules, so the module path may no longer name this function's module.
    with mock.patch.dict(translate.__globals__, load_locale=lambda lang: catalogs[lang]):
        assert translate('server.only', 'de') == 'English only'


def test_translate_unknown_language_falls_back_to_english():
    assert translate('server.flash.userNotFound', 'xx') == 'User not found.'
    assert translate('server.flash.userNotFound', '../../etc/passwd') == 'User not found.'


@pytest.mark.parametrize('code,expected', [
    ('de', 'de'), ('DE', 'de'), ('de-AT', 'de'), ('de_CH', 'de'),
    ('pt-BR', 'pt-BR'), ('pt-br', 'pt-BR'), ('pt_BR', 'pt-BR'), ('pt', 'pt-BR'), ('pt-PT', 'pt-BR'),
    ('zh', 'zh'), ('zh-CN', 'zh'), ('zh-Hans', 'zh'),
    ('xx', None), ('', None), (None, None),
])
def test_match_language_maps_region_codes(code, expected):
    assert match_language(code) == expected


# ---------------------------------------------------------------------------
# format_number() / format_file_size(): the browser's i18n.formatNumber and
# i18n.formatFileSize on the server (#481), same cases as static/js/i18n.test.js
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('lang,expected', [
    ('en', '1.4 MB'), ('zh', '1.4 MB'), ('de', '1,4 MB'), ('es', '1,4 MB'),
    ('fr', '1,4 MB'), ('pt-BR', '1,4 MB'), ('ru', '1,4 МБ'),
])
def test_file_size_uses_the_languages_decimal_separator(lang, expected):
    assert format_file_size(1464812, lang) == expected


@pytest.mark.parametrize('size,expected', [
    (0, '0 B'), (None, '0 B'), (-5, '0 B'), (1023, '1023 B'), (1024, '1 KB'),
    (1536000, '1,5 MB'), (9 * 1024 ** 2, '9 MB'), (1024 ** 2 - 1, '1 MB'),
    (1024 ** 3 - 1024, '1 GB'), (1024 ** 3, '1 GB'), (int(2.5 * 1024 ** 4), '2,5 TB'),
    (1024 ** 5, '1024 TB'),
])
def test_file_size_units_and_rounding(size, expected):
    assert format_file_size(size, 'de') == expected


def test_format_number_groups_and_rounds_in_the_language():
    assert format_number(1234567, 'en') == '1,234,567'
    assert format_number(1234567, 'de') == '1.234.567'
    assert format_number(1234.25, 'de', max_fraction_digits=1) == '1.234,3'  # half up
    assert format_number(9.0, 'en', max_fraction_digits=1) == '9'
    assert format_number(1234, 'xx') == '1,234'  # unknown language: English


def test_format_number_uses_the_request_language_by_default():
    with app.test_request_context('/', headers={'Accept-Language': 'de'}):
        assert format_number(1234.5, max_fraction_digits=1) == '1.234,5'
        assert format_file_size(1536000) == '1,5 MB'


def test_outside_a_request_the_language_is_english():
    assert request_language() == 'en'
    assert translate('server.flash.userNotFound') == 'User not found.'


@pytest.mark.parametrize('header,expected', [
    ('de-DE,de;q=0.9,en;q=0.8', 'de'),
    ('fr-CA', 'fr'),
    ('pt', 'pt-BR'),
    ('zh-CN,zh;q=0.9', 'zh'),
    ('xx-YY', 'en'),
])
def test_anonymous_requests_use_accept_language(header, expected):
    with app.test_request_context('/', headers={'Accept-Language': header}):
        assert request_language() == expected


def test_anonymous_request_without_accept_language_is_english():
    with app.test_request_context('/'):
        assert translate('server.flash.userNotFound') == 'User not found.'


# ---------------------------------------------------------------------------
# Flash messages and emails
# ---------------------------------------------------------------------------

@pytest.fixture
def make_user():
    created = []

    def make(ui_language=None):
        with app.app_context():
            u = User(username=f'i18n_{uuid.uuid4().hex[:12]}', email=f'{uuid.uuid4().hex[:10]}@example.com',
                     password='x', ui_language=ui_language)
            db.session.add(u)
            db.session.commit()
            created.append(u.id)
            return u.id

    yield make
    with app.app_context():
        for uid in created:
            u = db.session.get(User, uid)
            if u:
                db.session.delete(u)
        db.session.commit()


def _flashes(client):
    with client.session_transaction() as s:
        return [message for _, message in s.get('_flashes', [])]


def test_flash_follows_the_users_interface_language(make_user):
    uid = make_user('de')
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    try:
        client = app.test_client()
        with client.session_transaction() as s:
            s['_user_id'] = str(uid)
            s['_fresh'] = True
        # Accept-Language disagrees on purpose: the saved setting wins.
        resp = client.post('/change_password', data={'new_password': 'x', 'confirm_password': 'x'},
                           headers={'Accept-Language': 'fr'})
        assert resp.status_code == 302
        assert _flashes(client) == ['Das aktuelle Passwort ist erforderlich.']
    finally:
        app.config['WTF_CSRF_ENABLED'] = prev


def test_flash_for_anonymous_visitor_uses_accept_language():
    client = app.test_client()
    client.get('/account', headers={'Accept-Language': 'es-MX,es;q=0.9'})
    assert _flashes(client) == ['Inicie sesión para acceder a esta página.']


def _send(sender, uid, accept_language=None):
    """Run sender for the user and return (subject, html, text) as delivered over SMTP."""
    smtp = mock.MagicMock()
    headers = {'Accept-Language': accept_language} if accept_language else {}
    with mock.patch.dict(os.environ, {'APP_BASE_URL': 'https://speakr.example.com/',
                                      'SMTP_HOST': 'smtp.example.com', 'SMTP_USE_TLS': 'false'}), \
            mock.patch.object(email_service, 'is_smtp_configured', return_value=True), \
            mock.patch.object(email_service, 'is_email_verification_enabled', return_value=True), \
            mock.patch.object(email_service.smtplib, 'SMTP', return_value=smtp), \
            app.test_request_context('/forgot-password', headers=headers):
        assert sender(db.session.get(User, uid))
    raw = smtp.sendmail.call_args[0][2]
    msg = message_from_string(raw)
    parts = {p.get_content_type(): p.get_payload(decode=True).decode(p.get_content_charset())
             for p in msg.walk() if p.get_content_type() in ('text/plain', 'text/html')}
    return str(make_header(decode_header(msg['Subject']))), parts['text/html'], parts['text/plain']


@pytest.mark.parametrize('sender,subject,heading', [
    (email_service.send_verification_email, 'Bestätigen Sie Ihre E-Mail-Adresse - Speakr',
     'Bestätigen Sie Ihre E-Mail-Adresse'),
    (email_service.send_password_reset_email, 'Passwort zurücksetzen - Speakr', 'Passwort zurücksetzen'),
])
def test_account_emails_use_the_recipients_language(make_user, sender, subject, heading):
    uid = make_user('de')
    # The request comes from an English browser; the recipient's setting decides.
    got_subject, html_body, text_body = _send(sender, uid, accept_language='en-US')
    assert got_subject == subject
    assert heading in html_body
    assert 'Hallo i18n_' in html_body and 'Hallo i18n_' in text_body
    assert 'KI-gestützte Audiotranskription' in html_body
    assert 'Diese E-Mail wurde von Speakr gesendet' in text_body
    assert 'Hi ' not in text_body


@pytest.mark.parametrize('sender,subject', [
    (email_service.send_verification_email, 'Verify your email address - Speakr'),
    (email_service.send_password_reset_email, 'Reset your password - Speakr'),
])
def test_account_emails_default_to_english(make_user, sender, subject):
    uid = make_user(None)
    got_subject, html_body, text_body = _send(sender, uid, accept_language='de')
    assert got_subject == subject
    assert 'Hi i18n_' in text_body
    assert 'AI-Powered Audio Transcription' in html_body


def test_translated_email_text_is_html_escaped(make_user):
    uid = make_user('fr')
    with app.app_context():
        u = db.session.get(User, uid)
        u.username = '<b>x</b>'
        db.session.commit()
    _, html_body, text_body = _send(email_service.send_verification_email, uid)
    assert '<b>x</b>' not in html_body
    assert 'Bonjour &lt;b&gt;x&lt;/b&gt;,' in html_body
    # French elision apostrophes are escaped in HTML and left alone in text.
    assert 'n&#x27;avez pas' in html_body
    assert "n'avez pas" in text_body


# ---------------------------------------------------------------------------
# Budget messages (#477): shown as the error of a blocked request or a failed
# recording, so they follow the budget owner's interface language.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('lang,percent', [
    ('en', '85.3%'), ('zh', '85.3%'), ('pt-BR', '85,3%'),
    ('de', '85,3\xa0%'), ('fr', '85,3\xa0%'), ('ru', '85,3\xa0%'),
])
def test_format_percent_follows_the_language(lang, percent):
    assert format_percent(85.25, lang) == percent
    assert format_percent(100, lang) == percent.replace('85,3', '100').replace('85.3', '100')


def _budget(tracker, uid, used, budget_field, budget):
    with app.app_context():
        u = db.session.get(User, uid)
        setattr(u, budget_field, budget)
        db.session.commit()
        with mock.patch.object(tracker, 'get_monthly_usage', return_value=used):
            return tracker.check_budget(uid)


def test_token_budget_messages_use_the_users_language(make_user):
    from src.services.token_tracking import token_tracker
    de, en = make_user('de'), make_user(None)
    assert _budget(token_tracker, de, 1200, 'monthly_token_budget', 1000) == (
        False, 120.0, 'Monatliches Token-Budget überschritten (120\xa0%). '
        'Wenden Sie sich für weitere Tokens an den Administrator.')
    assert _budget(token_tracker, en, 853, 'monthly_token_budget', 1000) == (
        True, 85.3, 'Warning: 85.3% of monthly token budget used.')
    assert _budget(token_tracker, en, 100, 'monthly_token_budget', 1000) == (True, 10.0, None)


def test_transcription_budget_messages_use_the_users_language(make_user):
    from src.services.transcription_tracking import transcription_tracker
    ru, en = make_user('ru'), make_user('en')
    can, _, msg = _budget(transcription_tracker, ru, 90 * 60, 'monthly_transcription_budget', 60 * 60)
    assert not can and msg.startswith('Месячный лимит транскрипции превышен (90/60 мин).')
    assert _budget(transcription_tracker, en, 54 * 60, 'monthly_transcription_budget', 60 * 60)[2] == (
        'Warning: 90% of monthly transcription budget used.')


# ---------------------------------------------------------------------------
# API errors (#477): `error` in the interface language, `code` the same in
# every language. A response that already had a code keeps it.
# ---------------------------------------------------------------------------

def _session_client(uid):
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True
    return client


@pytest.mark.parametrize('lang,text', [
    ('de', 'Aufnahme nicht gefunden'), ('fr', 'Enregistrement introuvable'),
    (None, 'Recording not found'),
    # An unknown saved language falls back to Accept-Language (ru here).
    ('xx', 'Запись не найдена'),
])
def test_api_error_follows_the_users_language_with_a_stable_code(make_user, lang, text):
    uid = make_user(lang)
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    try:
        resp = _session_client(uid).post('/recording/999999999/update_speakers', json={},
                                         headers={'Accept-Language': 'ru'} if lang else {})
    finally:
        app.config['WTF_CSRF_ENABLED'] = prev
    assert resp.status_code == 404
    assert resp.get_json() == {'error': text, 'code': 'recording_not_found'}


def test_api_v1_token_error_keeps_its_existing_code(make_user):
    import secrets
    from src.models import APIToken
    from src.utils.token_auth import hash_token
    uid = make_user('de')
    plain = f'tok-{secrets.token_urlsafe(16)}'
    with app.app_context():
        db.session.add(APIToken(user_id=uid, token_hash=hash_token(plain), name='i18n'))
        db.session.commit()
    resp = app.test_client().get('/api/v1/recordings/999999999/external-refs',
                                 headers={'Authorization': f'Bearer {plain}'})
    assert resp.status_code == 404
    assert resp.get_json() == {'error': 'Aufnahme nicht gefunden', 'code': 'not_found'}
    with app.app_context():
        APIToken.query.filter_by(user_id=uid).delete()
        db.session.commit()


def test_api_error_for_anonymous_request_uses_accept_language():
    with app.test_request_context('/', headers={'Accept-Language': 'es-MX,es;q=0.9'}):
        assert api_error('recording_not_found') == {
            'error': translate('server.apiError.recording_not_found', 'es'), 'code': 'recording_not_found'}
        assert api_error('recording_not_found')['error'] != 'Recording not found'


def test_every_api_error_code_is_a_snake_case_key():
    import json
    import re
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'static', 'locales', 'en.json'), encoding='utf-8') as f:
        codes = json.load(f)['server']['apiError']
    assert codes and all(re.fullmatch(r'[a-z][a-z0-9_]*', c) for c in codes)


# ---------------------------------------------------------------------------
# Form validation messages (#477): fixed when the form class is defined,
# shown in the language of the request that renders them.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('lang,required,mismatch', [
    ('de', 'Dieses Feld ist erforderlich.', 'Die Passwörter müssen übereinstimmen.'),
    ('en', 'This field is required.', 'Passwords must match.'),
])
def test_registration_form_errors_follow_accept_language(lang, required, mismatch):
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    try:
        with mock.patch.dict(os.environ, {'ALLOW_REGISTRATION': 'true'}):
            resp = app.test_client().post('/register', data={
                'username': '', 'email': 'nobody@example.com',
                'password': 'Aa1!aaaa', 'confirm_password': 'Aa1!aaab'},
                headers={'Accept-Language': lang})
    finally:
        app.config['WTF_CSRF_ENABLED'] = prev
    html = resp.get_data(as_text=True)
    assert required in html
    assert mismatch in html


def test_lazy_translation_resolves_when_used():
    from src.utils.localization import LazyTranslation
    text = LazyTranslation('server.form.usernameLength', min=2, max=20)
    with app.test_request_context('/', headers={'Accept-Language': 'fr'}):
        assert str(text) == "Le nom d'utilisateur doit contenir entre 2 et 20 caractères."
        # WTForms applies % with its own values; the catalog text is complete.
        assert text % {'min': 2, 'max': 20, 'length': 1} == str(text)
    with app.test_request_context('/'):
        assert str(text) == 'Username must be between 2 and 20 characters long.'
