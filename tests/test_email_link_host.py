"""Links in password-reset and verification mail (#436).

The host of the link came from the request, including X-Forwarded-Host, so a
person asking for somebody else's reset link could point it at their own site
and receive the token when the link was opened. With APP_BASE_URL set, the
links are built from that address whatever the request carries.
"""

import os
from unittest.mock import patch

import pytest

from src.app import app
from src.database import db
from src.models import User
from src.services import email as email_service

FORGED = {'Host': 'attacker.example', 'X-Forwarded-Host': 'attacker.example'}


@pytest.fixture
def user():
    with app.app_context():
        u = User(username=f'linkhost_{os.getpid()}', email=f'linkhost_{os.getpid()}@example.com', password='x')
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        u = db.session.get(User, uid)
        if u:
            db.session.delete(u)
            db.session.commit()


def _send(sender, uid, base):
    sent = {}

    def capture(to, subject, html_body, text_body=None):
        sent['html'], sent['text'] = html_body, text_body
        return True

    env = {'APP_BASE_URL': base} if base else {}
    with patch.dict(os.environ, env), \
            patch.object(email_service, 'is_smtp_configured', return_value=True), \
            patch.object(email_service, 'is_email_verification_enabled', return_value=True), \
            patch.object(email_service, '_send_email', side_effect=capture), \
            app.test_request_context('/forgot-password', headers=FORGED):
        if not base:
            os.environ.pop('APP_BASE_URL', None)
        assert sender(db.session.get(User, uid))
    return sent


@pytest.mark.parametrize('sender', [email_service.send_password_reset_email,
                                    email_service.send_verification_email])
def test_links_use_app_base_url_not_request_host(user, sender):
    sent = _send(sender, user, 'https://speakr.example.com/')
    for body in (sent['html'], sent['text']):
        assert 'https://speakr.example.com/' in body
        assert 'attacker.example' not in body


def test_without_app_base_url_a_warning_is_logged(user, caplog):
    email_service._warned_host_links = False
    sent = _send(email_service.send_password_reset_email, user, None)
    assert '/reset-password/' in sent['text']
    assert 'APP_BASE_URL is not set' in caplog.text
