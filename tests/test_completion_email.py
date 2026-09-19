"""Email notifications when a transcription finishes (#386).

The sender has three gates that must all pass: the user asked for it, Speakr
can actually deliver to them, and the job that finished was a transcription.
Each is tested for both directions, because the failure that matters here is
mailing somebody who did not ask rather than failing to mail somebody who did.
"""

import os
from unittest.mock import patch

import pytest

from src.app import app
from src.database import db
from src.models import User, Recording
from src.services import email as email_service


@pytest.fixture
def smtp_configured():
    """Pretend SMTP is set up without touching a real server."""
    with patch.object(email_service, 'is_smtp_configured', return_value=True):
        yield


@pytest.fixture
def user_factory():
    created = []

    def _make(**kwargs):
        with app.app_context():
            suffix = len(created)
            user = User(
                username=kwargs.pop('username', f'notify_user_{suffix}_{os.getpid()}'),
                email=kwargs.pop('email', f'notify{suffix}_{os.getpid()}@example.com'),
                password='x',
                **kwargs,
            )
            db.session.add(user)
            db.session.commit()
            created.append(user.id)
            return user

    yield _make

    with app.app_context():
        for uid in created:
            u = db.session.get(User, uid)
            if u:
                db.session.delete(u)
        db.session.commit()


def _recording(user_id, **kwargs):
    return Recording(
        user_id=user_id,
        title=kwargs.pop('title', 'Quarterly planning call'),
        audio_path='/tmp/does-not-matter.wav',
        **kwargs,
    )


# --- the "can we reach them" gate ---------------------------------------

def test_no_smtp_means_no_mail(user_factory):
    user = user_factory(notify_email_on_completion=True)
    with app.app_context(), patch.object(email_service, 'is_smtp_configured', return_value=False):
        assert email_service.can_email_user(user) is False
        assert email_service.wants_completion_email(user) is False


def test_sso_placeholder_address_is_never_mailed(user_factory, smtp_configured):
    """An SSO login whose provider withheld an address gets a synthetic one.

    Mailing it would hard-bounce on every finished recording, which is how a
    sending domain gets blocked. See issue #377 for where these come from.
    """
    user = user_factory(email='abc123@placeholder.local', notify_email_on_completion=True)
    with app.app_context():
        assert email_service.can_email_user(user) is False
        assert email_service.wants_completion_email(user) is False


def test_deliverable_address_with_opt_in_is_mailable(user_factory, smtp_configured):
    user = user_factory(notify_email_on_completion=True)
    with app.app_context():
        assert email_service.wants_completion_email(user) is True


# --- the "did they ask for it" gate -------------------------------------

def test_opt_out_is_the_default(user_factory, smtp_configured):
    """The column defaults off, so an upgrade mails nobody."""
    user = user_factory()
    with app.app_context():
        assert bool(user.notify_email_on_completion) is False
        assert email_service.wants_completion_email(user) is False


def test_completion_mail_is_not_sent_to_a_user_who_opted_out(user_factory, smtp_configured):
    user = user_factory(notify_email_on_completion=False)
    with app.app_context(), patch.object(email_service, '_send_email') as send:
        sent = email_service.send_transcription_complete_email(user, _recording(user.id))
        assert sent is False
        send.assert_not_called()


# --- content -------------------------------------------------------------

def test_completion_mail_addresses_the_user_and_names_the_recording(user_factory, smtp_configured):
    user = user_factory(notify_email_on_completion=True)
    with app.app_context(), patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id, title='Quarterly planning call')
        rec.id = 4242
        assert email_service.send_transcription_complete_email(user, rec) is True

        to_email, subject, html, text = send.call_args[0]
        assert to_email == user.email
        assert 'Quarterly planning call' in subject
        assert 'Quarterly planning call' in html
        assert 'Quarterly planning call' in text
        assert user.username in html and user.username in text
        # Both alternatives must carry the opt-out route, or the mail is spam.
        assert 'account settings' in html and 'account settings' in text


def test_link_is_omitted_rather_than_broken_without_a_base_url(user_factory, smtp_configured):
    """No APP_BASE_URL means no deep link. A wrong link is worse than none."""
    user = user_factory(notify_email_on_completion=True)
    with app.app_context(), patch.dict(os.environ, {'APP_BASE_URL': ''}, clear=False), \
            patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id)
        rec.id = 7
        email_service.send_transcription_complete_email(user, rec)
        _, _, html, text = send.call_args[0]
        assert '/recordings/7' not in html
        assert 'Open recording' not in html
        assert 'Open it here' not in text


def test_base_url_produces_a_deep_link_to_the_recording(user_factory, smtp_configured):
    user = user_factory(notify_email_on_completion=True)
    with app.app_context(), \
            patch.dict(os.environ, {'APP_BASE_URL': 'https://speakr.example.com/'}, clear=False), \
            patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id)
        rec.id = 7
        email_service.send_transcription_complete_email(user, rec)
        _, _, html, text = send.call_args[0]
        # Trailing slash on the configured base must not double up.
        assert 'https://speakr.example.com/recordings/7' in html
        assert 'https://speakr.example.com/recordings/7' in text
        assert '//recordings' not in html


def test_failure_mail_summarises_the_error_without_forwarding_it_whole(user_factory, smtp_configured):
    """Worker errors can carry an upstream URL or key, so only the first line
    goes out, truncated."""
    user = user_factory(notify_email_on_completion=True)
    with app.app_context(), patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id)
        rec.id = 9
        error = 'Connection refused to http://internal:9000\nTraceback...\nsecret=abcdef'
        assert email_service.send_transcription_failed_email(user, rec, error) is True
        _, subject, html, text = send.call_args[0]
        assert 'failed' in subject.lower()
        assert 'Connection refused' in html
        assert 'secret=abcdef' not in html
        assert 'Traceback' not in html
        assert 'secret=abcdef' not in text


def test_a_very_long_error_is_truncated(user_factory, smtp_configured):
    user = user_factory(notify_email_on_completion=True)
    with app.app_context(), patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id)
        rec.id = 9
        email_service.send_transcription_failed_email(user, rec, 'x' * 5000)
        _, _, html, _ = send.call_args[0]
        assert 'x' * 201 not in html


def test_duration_is_rendered_for_humans():
    assert email_service._format_duration(45) == '45s'
    assert email_service._format_duration(125) == '2m 5s'
    assert email_service._format_duration(3725) == '1h 2m'
    # Absent or nonsensical values drop the row rather than printing "None".
    assert email_service._format_duration(None) is None
    assert email_service._format_duration(0) is None
    assert email_service._format_duration('not a number') is None


def test_a_recording_without_a_title_still_produces_a_sane_subject(user_factory, smtp_configured):
    user = user_factory(notify_email_on_completion=True)
    with app.app_context(), patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id, title=None)
        rec.id = 11
        email_service.send_transcription_complete_email(user, rec)
        _, subject, _, _ = send.call_args[0]
        assert 'None' not in subject
        assert 'Untitled recording' in subject


# --- the "which job" gate ------------------------------------------------

@pytest.mark.parametrize('job_type,should_mail', [
    ('transcribe', True),
    ('reprocess_transcription', True),
    ('summarize', False),
    ('reprocess_summary', False),
    ('stitch', False),
])
def test_only_transcription_jobs_reach_the_inbox(job_type, should_mail):
    from src.services.job_queue import job_queue
    with app.app_context(), patch('src.services.email.send_transcription_complete_email') as send:
        job_queue.init_app(app)
        with patch.object(job_queue, '_load_recording_and_owner', return_value=(None, None)):
            job_queue._emit_completion_email(job_type, 1)
        # A non-transcription job must return before it even looks the
        # recording up, so the send is unreachable either way; what this
        # pins is that summaries never acquire a mail path by accident.
        assert send.call_count == 0
    assert (job_type in job_queue._EMAIL_JOB_TYPES) is should_mail


# --- HTML injection into the message body --------------------------------

def test_a_recording_title_cannot_inject_html_into_the_email(user_factory, smtp_configured):
    """Titles are set from the API and generated by the LLM from transcript
    content, which can come from somebody other than the recipient. Mail
    clients do not run scripts, so the risk is not XSS but content injection
    into a genuine, correctly-addressed Speakr message, which is exactly what
    makes an injected link believable.
    """
    user = user_factory(notify_email_on_completion=True)
    payload = '<a href="https://evil.example/reset">Reset your password</a>'
    with app.app_context(), patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id, title=payload)
        rec.id = 1
        email_service.send_transcription_complete_email(user, rec)
        _, _, html, text = send.call_args[0]

        assert '<a href="https://evil.example/reset">' not in html
        assert '&lt;a href=&quot;https://evil.example/reset&quot;&gt;' in html
        # Exactly one anchor: the legitimate "Open recording" button, or none
        # when no base URL is set. Never one the title smuggled in.
        assert html.count('<a ') <= 1
        # The plain-text alternative is not escaped, and must not be.
        assert payload in text
        assert '&lt;' not in text


def test_a_username_cannot_inject_html_into_the_email(user_factory, smtp_configured):
    """Usernames carry only a length validator, no character filter."""
    user = user_factory(username='<img src=x>', notify_email_on_completion=True)
    with app.app_context(), patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id)
        rec.id = 2
        email_service.send_transcription_complete_email(user, rec)
        _, _, html, _ = send.call_args[0]
        assert '<img src=x>' not in html
        assert '&lt;img src=x&gt;' in html


def test_an_error_message_cannot_inject_html_into_the_email(user_factory, smtp_configured):
    user = user_factory(notify_email_on_completion=True)
    with app.app_context(), patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id)
        rec.id = 3
        email_service.send_transcription_failed_email(user, rec, '<script>alert(1)</script>')
        _, _, html, _ = send.call_args[0]
        assert '<script>' not in html
        assert '&lt;script&gt;' in html


def test_the_shared_verification_template_also_escapes_the_username(user_factory, smtp_configured):
    """Same defect, older code: verification and reset mail interpolate the
    username too, and a password-reset message is where an injected link
    would be worth the most.
    """
    user = user_factory(username='<b>bold</b>')
    # This sender builds an _external URL, which needs a request to resolve
    # against; it is only ever called from one in production.
    with app.test_request_context('/'), \
            patch.object(email_service, 'is_email_verification_enabled', return_value=True), \
            patch.object(email_service, '_send_email', return_value=True) as send:
        email_service.send_verification_email(user)
        _, _, html, _ = send.call_args[0]
        assert '<b>bold</b>' not in html
        assert '&lt;b&gt;bold&lt;/b&gt;' in html


# --- the header logo -----------------------------------------------------

_FAKE_SMTP_CONFIG = {
    'smtp_host': 'smtp.example.com', 'smtp_port': 587,
    'smtp_username': 'u', 'smtp_password': 'p',
    'smtp_use_tls': False, 'smtp_use_ssl': False,
    'from_address': 'noreply@example.com', 'from_name': 'Speakr',
}


def test_the_logo_travels_with_the_message_rather_than_being_linked():
    """A worker has no request context, so url_for(_external=True) raised and
    the template fell back to src="", which rendered as a broken image. Even
    with a URL, most clients block remote images until the reader allows them.
    """
    from email import message_from_string

    with app.app_context():
        html_body, text_body = email_service._get_email_template('<p>hi</p>', 'hi', 'Subject')
        assert f'cid:{email_service.LOGO_CID}' in html_body
        assert 'src=""' not in html_body

        with patch.object(email_service, 'is_smtp_configured', return_value=True), \
                patch.object(email_service, 'get_email_config', return_value=_FAKE_SMTP_CONFIG), \
                patch('smtplib.SMTP') as smtp:
            assert email_service._send_email('someone@example.com', 'Subject', html_body, text_body) is True
            raw = smtp.return_value.sendmail.call_args[0][2]

    msg = message_from_string(raw)
    assert msg.get_content_type() == 'multipart/related'
    parts = {p.get_content_type() for p in msg.walk()}
    assert 'image/png' in parts
    assert 'text/plain' in parts and 'text/html' in parts

    image = next(p for p in msg.walk() if p.get_content_type() == 'image/png')
    # The angle brackets are what the cid: reference resolves against.
    assert image.get('Content-ID') == f'<{email_service.LOGO_CID}>'
    assert image.get_payload(decode=True)[:8] == b'\x89PNG\r\n\x1a\n'


def test_an_unreadable_logo_still_sends_a_valid_message():
    """The logo is a decoration. Losing it must not lose the email."""
    from email import message_from_string

    with app.app_context():
        html_body, text_body = email_service._get_email_template('<p>hi</p>', 'hi', 'Subject')
        with patch.object(email_service, '_load_logo_bytes', return_value=None), \
                patch.object(email_service, 'is_smtp_configured', return_value=True), \
                patch.object(email_service, 'get_email_config', return_value=_FAKE_SMTP_CONFIG), \
                patch('smtplib.SMTP') as smtp:
            assert email_service._send_email('someone@example.com', 'Subject', html_body, text_body) is True
            raw = smtp.return_value.sendmail.call_args[0][2]

    msg = message_from_string(raw)
    assert msg.get_content_type() == 'multipart/alternative'
    parts = {p.get_content_type() for p in msg.walk()}
    assert 'text/plain' in parts and 'text/html' in parts
    assert 'image/png' not in parts


def test_a_whitespace_only_error_still_sends_the_failure_email(user_factory, smtp_configured):
    """Found in review: `(error or '').strip().splitlines()[0]` raised
    IndexError on an error like "\\n" (truthy, strips to nothing), and the
    worker's except swallowed it, so exactly the failure email was lost."""
    user = user_factory(notify_email_on_completion=True)
    with app.app_context(), patch.object(email_service, '_send_email', return_value=True) as send:
        rec = _recording(user.id)
        rec.id = 12
        assert email_service.send_transcription_failed_email(user, rec, '\n  \n') is True
        send.assert_called_once()
