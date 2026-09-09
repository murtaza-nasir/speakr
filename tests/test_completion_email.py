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
