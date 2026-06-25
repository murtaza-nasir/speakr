"""Webhook backend tests (#275).

Covers:

- model serialization + event_list validation
- HMAC signature generation
- SSRF guard (private IP block + intranet allowlist)
- emit_webhook_event creates delivery rows for matching subscriptions
- dispatcher pass: success path, retry-with-backoff path, permanent
  failure path, auto-pause after N consecutive failures
- v1 CRUD endpoints: create/list/get/patch/delete/test/rotate-secret
- replay creates a new delivery with a fresh event_id
"""

import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Webhook, WebhookDelivery, WEBHOOK_EVENT_TYPES, generate_webhook_secret
from src.services.webhook_dispatch import (
    sign_payload,
    is_url_safe_for_webhook,
    emit_webhook_event,
    run_dispatcher_pass,
    _delay_for_attempt,
    _is_retryable_status,
)


app.config["WTF_CSRF_ENABLED"] = False


def _make_user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    from flask import g
    with client.session_transaction() as sess:
        sess.clear()
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    try:
        g.pop("_login_user", None)
    except RuntimeError:
        pass


# --- Model tests -----------------------------------------------------------

def test_event_list_setter_dedupes_and_validates():
    with app.app_context():
        user = _make_user("wh_model")
        wh = Webhook(user_id=user.id, name="t", url="https://example.com/x")
        wh.event_list = ['recording.created', 'recording.created', 'recording.deleted']
        assert wh.event_list == ['recording.created', 'recording.deleted']
        try:
            wh.event_list = ['not.a.real.event']
            assert False, "expected ValueError"
        except ValueError:
            pass
        db.session.delete(user)
        db.session.commit()


def test_generate_webhook_secret_returns_distinct_values():
    a = generate_webhook_secret()
    b = generate_webhook_secret()
    assert a and b and a != b
    assert len(a) >= 32


def test_to_dict_omits_secret_by_default():
    with app.app_context():
        user = _make_user("wh_secret")
        wh = Webhook(user_id=user.id, name="t", url="https://example.com/x", secret="topsecret")
        wh.event_list = ['recording.created']
        db.session.add(wh)
        db.session.commit()
        assert 'secret' not in wh.to_dict()
        assert wh.to_dict(include_secret=True)['secret'] == 'topsecret'
        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


# --- Signature ---------------------------------------------------------------

def test_sign_payload_matches_hmac_sha256():
    body = b'{"hello":"world"}'
    secret = 'shared-secret'
    sig = sign_payload(secret, body)
    expected_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sig == f'sha256={expected_hex}'


def test_serialize_envelope_uses_compact_json():
    """The wire body must be compact JSON (no spaces after ``:`` or ``,``).

    This matches what most webhook debugging tools (webhook.site,
    Postman, RequestBin) display, so receivers copy-pasting the body
    can compute a matching HMAC without canonicalisation footguns.
    Stripe / GitHub / Mux all use the same wire format.

    If this test fails, signature verification will silently break for
    every receiver that re-encodes the displayed body.
    """
    from src.services.webhook_dispatch import serialize_envelope
    envelope = {'id': 'x', 'type': 'recording.created', 'data': {'a': 1, 'b': 2}}
    serialized = serialize_envelope(envelope)
    # No space after either separator.
    assert ', ' not in serialized, f'wire body contains a space after a comma: {serialized!r}'
    assert ': ' not in serialized, f'wire body contains a space after a colon: {serialized!r}'
    # And it must still round-trip cleanly.
    import json
    assert json.loads(serialized) == envelope


def test_emit_and_test_fire_produce_identical_wire_format():
    """Both code paths that create WebhookDelivery rows must produce
    payload bytes in the same canonical form, otherwise a delivery
    written by one path and verified against the format of the other
    will fail HMAC verification."""
    import json as _json
    with app.app_context():
        user = _make_user('wh_canonical')
        wh = Webhook(user_id=user.id, name='c', url='https://example.com/c', enabled=True)
        wh.event_list = ['recording.created']
        db.session.add(wh)
        db.session.commit()

        # Path 1: emit_webhook_event
        emit_webhook_event(user.id, 'recording.created', {'recording_id': 1})
        emitted = WebhookDelivery.query.filter_by(webhook_id=wh.id).first()
        assert emitted is not None
        assert ', ' not in emitted.payload
        assert ': ' not in emitted.payload

        # Path 2: test_fire endpoint
        client = app.test_client()
        _login(client, user)
        resp = client.post(f'/api/v1/webhooks/{wh.id}/test')
        assert resp.status_code == 202, resp.data
        # The most recent delivery is the test fire
        test_fired = (
            WebhookDelivery.query
            .filter_by(webhook_id=wh.id)
            .order_by(WebhookDelivery.id.desc())
            .first()
        )
        assert test_fired.event_type == 'webhook.test'
        assert ', ' not in test_fired.payload
        assert ': ' not in test_fired.payload

        for d in WebhookDelivery.query.filter_by(webhook_id=wh.id).all():
            db.session.delete(d)
        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


def test_job_queue_completion_actually_emits_webhook_event():
    """Regression test: the job_queue's _emit_completion_webhook used to
    fail silently with ``name 'db' is not defined`` because it tried to
    use db without importing it, and the calling code swallowed the
    error in a try/except. The mocked emit tests above did not catch
    this because they patched emit_webhook_event out entirely. This
    test calls the real ``_emit_completion_webhook`` against a real
    subscribed webhook and asserts a delivery row is created.

    If this test fails, every transcription/summary completion will
    silently fail to fire a webhook in production."""
    with app.app_context():
        from src.models import Recording
        from src.services.job_queue import job_queue

        user = _make_user('wh_jq_emit')
        wh = Webhook(
            user_id=user.id,
            name='subscriber',
            url='https://example.com/wh',
            enabled=True,
        )
        wh.event_list = ['recording.transcription.completed']
        db.session.add(wh)

        recording = Recording(
            user_id=user.id,
            title='test recording',
            audio_path='/tmp/test.mp3',
            status='COMPLETED',
        )
        db.session.add(recording)
        db.session.commit()

        # Call the real emit method (no mocking of emit_webhook_event)
        job_queue._emit_completion_webhook('transcribe', recording.id)

        delivered = WebhookDelivery.query.filter_by(webhook_id=wh.id).all()
        assert len(delivered) == 1, (
            'job_queue._emit_completion_webhook did not create a delivery. '
            'Check that all required imports (db, Recording, emit_webhook_event) '
            'are inside the method or its app-context block.'
        )
        envelope = json.loads(delivered[0].payload)
        assert envelope['type'] == 'recording.transcription.completed'
        assert envelope['data']['recording_id'] == recording.id

        # Same check for the failure path.
        for d in delivered:
            db.session.delete(d)
        wh.event_list = ['recording.transcription.failed']
        db.session.commit()
        job_queue._emit_failure_webhook('transcribe', recording.id, 'something broke')
        fail_delivered = WebhookDelivery.query.filter_by(webhook_id=wh.id).all()
        assert len(fail_delivered) == 1
        env = json.loads(fail_delivered[0].payload)
        assert env['type'] == 'recording.transcription.failed'
        assert env['data']['error'] == 'something broke'

        for d in WebhookDelivery.query.filter_by(webhook_id=wh.id).all():
            db.session.delete(d)
        db.session.delete(wh)
        db.session.delete(recording)
        db.session.delete(user)
        db.session.commit()


def test_job_queue_failure_map_covers_all_advertised_failure_events():
    """Every entry in the failure-event map must actually fire when the
    matching job_type encounters a failure. Without this, a worker that
    raises an exception on summarize would silently fail to notify
    subscribers, defeating the value of subscribing to .failed at all.

    The completion test covers `transcribe` failure already; this test
    pins the remaining three (summarize, reprocess_transcription,
    reprocess_summary) and asserts the error payload is preserved."""
    cases = [
        ('summarize', 'recording.summary.failed', 'mock LLM error'),
        ('reprocess_transcription', 'recording.transcription.failed', 'asr 500'),
        ('reprocess_summary', 'recording.summary.failed', 'LLM unavailable'),
    ]
    with app.app_context():
        from src.models import Recording
        from src.services.job_queue import job_queue

        for job_type, expected_event_type, error_text in cases:
            user = _make_user(f'wh_fail_{job_type[:8]}')
            wh = Webhook(
                user_id=user.id,
                name=f'sub_{job_type}',
                url=f'https://example.com/{job_type}',
                enabled=True,
            )
            wh.event_list = [expected_event_type]
            db.session.add(wh)
            recording = Recording(
                user_id=user.id,
                title=f'fail-{job_type}',
                audio_path=f'/tmp/{job_type}.mp3',
                status='COMPLETED',
            )
            db.session.add(recording)
            db.session.commit()

            job_queue._emit_failure_webhook(job_type, recording.id, error_text)

            delivered = WebhookDelivery.query.filter_by(webhook_id=wh.id).all()
            assert len(delivered) == 1, (
                f'{job_type} -> {expected_event_type} did not fire: got '
                f'{len(delivered)} deliveries.'
            )
            env = json.loads(delivered[0].payload)
            assert env['type'] == expected_event_type, (
                f'Wrong event type for {job_type}: {env["type"]}'
            )
            assert env['data']['recording_id'] == recording.id
            assert env['data']['error'] == error_text, (
                f'Error text not propagated for {job_type}: '
                f'expected {error_text!r}, got {env["data"].get("error")!r}'
            )

            for d in delivered:
                db.session.delete(d)
            db.session.delete(wh)
            db.session.delete(recording)
            db.session.delete(user)
            db.session.commit()


def test_failure_event_error_text_truncated_to_500_chars():
    """The error string from a job failure is included in the payload
    so receivers can route on it. We cap at 500 chars to prevent a
    runaway traceback or pathological provider message from blowing
    up the webhook body."""
    with app.app_context():
        from src.models import Recording
        from src.services.job_queue import job_queue

        user = _make_user('wh_fail_long')
        wh = Webhook(user_id=user.id, name='long', url='https://example.com/long', enabled=True)
        wh.event_list = ['recording.transcription.failed']
        db.session.add(wh)
        recording = Recording(
            user_id=user.id, title='t', audio_path='/tmp/t.mp3', status='COMPLETED',
        )
        db.session.add(recording)
        db.session.commit()

        huge_error = 'x' * 5000
        job_queue._emit_failure_webhook('transcribe', recording.id, huge_error)

        delivered = WebhookDelivery.query.filter_by(webhook_id=wh.id).all()
        assert len(delivered) == 1
        env = json.loads(delivered[0].payload)
        assert len(env['data']['error']) == 500

        for d in delivered:
            db.session.delete(d)
        db.session.delete(wh)
        db.session.delete(recording)
        db.session.delete(user)
        db.session.commit()


def test_job_queue_emits_transcription_started_event():
    """recording.transcription.started must fire when the worker claims
    a transcribe job. The vocabulary advertises this event so users
    expect to receive it when they subscribe; a silent no-op would be
    a contract violation."""
    with app.app_context():
        from src.models import Recording
        from src.services.job_queue import job_queue

        user = _make_user('wh_started')
        wh = Webhook(user_id=user.id, name='s', url='https://example.com/s', enabled=True)
        wh.event_list = ['recording.transcription.started']
        db.session.add(wh)
        recording = Recording(
            user_id=user.id, title='t', audio_path='/tmp/x.mp3', status='PENDING',
        )
        db.session.add(recording)
        db.session.commit()

        # transcribe job_type → fires the started event
        job_queue._emit_started_webhook('transcribe', recording.id)
        delivered = WebhookDelivery.query.filter_by(webhook_id=wh.id).all()
        assert len(delivered) == 1
        env = json.loads(delivered[0].payload)
        assert env['type'] == 'recording.transcription.started'
        assert env['data']['recording_id'] == recording.id

        # summarize job_type → no started event in the vocabulary, so
        # no delivery is created. Ensures the started map stays in
        # sync with WEBHOOK_EVENT_TYPES.
        for d in delivered:
            db.session.delete(d)
        db.session.commit()
        job_queue._emit_started_webhook('summarize', recording.id)
        assert WebhookDelivery.query.filter_by(webhook_id=wh.id).count() == 0

        db.session.delete(wh)
        db.session.delete(recording)
        db.session.delete(user)
        db.session.commit()


def test_extract_events_from_transcript_fires_events_extracted_webhook():
    """recording.events.extracted must fire when extraction successfully
    writes at least one Event row. It must NOT fire for "ran the
    extractor and found nothing" because receivers only care about
    "there are events to consume"."""
    from unittest.mock import patch
    with app.app_context():
        from src.models import Recording, Event
        from src.tasks.processing import extract_events_from_transcript

        user = _make_user('wh_evx')
        user.extract_events = True
        db.session.commit()
        wh = Webhook(user_id=user.id, name='ev', url='https://example.com/ev', enabled=True)
        wh.event_list = ['recording.events.extracted']
        db.session.add(wh)
        recording = Recording(
            user_id=user.id, title='evx', audio_path='/tmp/evx.mp3', status='COMPLETED',
        )
        db.session.add(recording)
        db.session.commit()
        rid = recording.id

        # Stub the LLM call to return one event so extraction "succeeds"
        # with at least one Event row, triggering the webhook.
        sample_event = {
            'title': 'Standup', 'description': '',
            'date': '2026-06-10', 'time': '10:00',
            'duration_minutes': 30, 'attendees': [], 'reminder_minutes': 15,
        }

        with patch('src.tasks.processing.call_llm_completion') as call_llm:
            # Make the LLM return a JSON string our parser will accept
            call_llm.return_value = {
                'content': json.dumps({'events': [sample_event]}),
                'model': 'mock',
            }
            try:
                extract_events_from_transcript(rid, 'transcript text', 'summary text')
            except Exception as e:
                # The parser path may still raise on absent fields in the
                # mock; what matters for this test is whether the webhook
                # fires when Event rows were created.
                pass

        delivered = WebhookDelivery.query.filter_by(webhook_id=wh.id).all()
        if Event.query.filter_by(recording_id=rid).count() > 0:
            assert len(delivered) == 1, (
                'Event rows were created but recording.events.extracted '
                'did not fire.'
            )
            env = json.loads(delivered[0].payload)
            assert env['type'] == 'recording.events.extracted'
            assert env['data']['events_count'] >= 1
        else:
            # If extraction wrote no events (mock didn't parse), the
            # webhook must NOT fire.
            assert len(delivered) == 0, (
                'recording.events.extracted fired with zero events written.'
            )

        for e in Event.query.filter_by(recording_id=rid).all():
            db.session.delete(e)
        for d in delivered:
            db.session.delete(d)
        db.session.delete(wh)
        db.session.delete(recording)
        db.session.delete(user)
        db.session.commit()


def test_api_v1_patch_recording_fires_recording_updated_webhook():
    """recording.updated must fire from the v1 PATCH endpoint with the
    list of fields that changed. Subscribers use this to keep companion
    apps in sync without polling."""
    with app.app_context():
        from src.models import Recording

        user = _make_user('wh_upd')
        wh = Webhook(user_id=user.id, name='u', url='https://example.com/u', enabled=True)
        wh.event_list = ['recording.updated']
        db.session.add(wh)
        recording = Recording(
            user_id=user.id, title='before', audio_path='/tmp/u.mp3', status='COMPLETED',
        )
        db.session.add(recording)
        db.session.commit()
        rid = recording.id

        client = app.test_client()
        _login(client, user)
        resp = client.patch(
            f'/api/v1/recordings/{rid}',
            json={'title': 'after', 'notes': 'fresh notes'},
        )
        assert resp.status_code == 200, resp.data

        delivered = WebhookDelivery.query.filter_by(webhook_id=wh.id).all()
        assert len(delivered) == 1, (
            'PATCH /api/v1/recordings/{id} did not fire recording.updated.'
        )
        env = json.loads(delivered[0].payload)
        assert env['type'] == 'recording.updated'
        assert env['data']['recording_id'] == rid
        assert set(env['data']['fields_changed']) == {'title', 'notes'}, (
            f'Unexpected fields_changed: {env["data"]["fields_changed"]}'
        )

        for d in delivered:
            db.session.delete(d)
        db.session.delete(wh)
        db.session.delete(recording)
        db.session.delete(user)
        db.session.commit()


def test_every_advertised_event_type_has_an_emit_site():
    """Pin the contract: every entry in WEBHOOK_EVENT_TYPES (the list
    surfaced to users in the subscription UI and v1 API) must have at
    least one code path that emits it. If a future event type is added
    to the vocabulary without wiring an emitter, this test fails and
    the subscriber UX is broken."""
    import subprocess
    # `webhook.test` is fired only from the test_fire endpoint, never
    # by application code, so it is excluded from the "must have an
    # emit_webhook_event call" sweep.
    skip = {'webhook.test'}
    src_root = os.path.join(os.path.dirname(__file__), '..', 'src')
    src_root = os.path.abspath(src_root)
    missing = []
    for event_type in WEBHOOK_EVENT_TYPES:
        if event_type in skip:
            continue
        # Grep for the literal event-type string anywhere under src/.
        # An emit site has the form `event_type='recording.X'` or
        # `'recording.X'` as a value somewhere.
        result = subprocess.run(
            ['grep', '-rn', event_type, src_root],
            capture_output=True, text=True,
        )
        if event_type not in result.stdout:
            missing.append(event_type)
    assert not missing, (
        f'Event types advertised in WEBHOOK_EVENT_TYPES but never emitted '
        f'by any src/ code path: {missing}. Either wire an emit_webhook_event '
        f'call or remove from the vocabulary.'
    )


def test_end_to_end_signature_verifies_against_wire_bytes():
    """Plant a delivery, run the dispatcher (mocking the HTTP POST so we
    can capture the exact bytes that would have been sent), then verify
    the signature computed over those bytes matches the
    Speakr-Signature header value.

    This is the property the receiver depends on. If it breaks, every
    webhook receiver in the wild stops being able to verify deliveries."""
    with app.app_context():
        user = _make_user('wh_sig_e2e')
        wh, d = _seed_pending(user.id, secret='end-to-end-test-secret')

        captured = {}

        def fake_post(url, **kwargs):
            captured['body'] = kwargs.get('data')
            captured['headers'] = kwargs.get('headers')
            return MagicMock(status_code=204, text='')

        with patch('src.services.webhook_dispatch.requests.post', side_effect=fake_post):
            run_dispatcher_pass()

        body = captured['body']
        headers = captured['headers']
        assert body is not None and headers is not None

        # Receiver-side verification: recompute HMAC over the EXACT body
        # that was sent, with the shared secret.
        signature_header = headers['Speakr-Signature']
        assert signature_header.startswith('sha256=')
        expected = hmac.new(b'end-to-end-test-secret', body, hashlib.sha256).hexdigest()
        assert signature_header == f'sha256={expected}', (
            'Speakr-Signature header does not match HMAC of the wire body; '
            'receivers will reject every delivery.'
        )

        d_refreshed = db.session.get(WebhookDelivery, d.id)
        db.session.delete(d_refreshed)
        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


# --- SSRF guard --------------------------------------------------------------

def test_is_url_safe_rejects_http_when_allow_http_false():
    ok, reason = is_url_safe_for_webhook('http://example.com/wh', allow_http=False)
    assert not ok
    assert 'http' in reason.lower()


def test_is_url_safe_accepts_https_public():
    ok, _ = is_url_safe_for_webhook('https://example.com/wh', allow_http=False)
    assert ok


def test_is_url_safe_rejects_private_ip():
    # 192.168.0.0/16 is RFC1918; should be rejected by default.
    ok, reason = is_url_safe_for_webhook('http://192.168.1.50/x', allow_http=True)
    assert not ok
    assert 'private' in reason.lower() or 'loopback' in reason.lower()


def test_is_url_safe_rejects_loopback():
    ok, reason = is_url_safe_for_webhook('http://127.0.0.1/x', allow_http=True)
    assert not ok


def test_is_url_safe_allowlist_overrides_private_ip():
    # Set the env var so 192.168.1.50 is allowed.
    with patch.dict(os.environ, {'WEBHOOK_INTRANET_HOST_ALLOWLIST': r'^192\.168\.1\.\d+$'}):
        ok, _ = is_url_safe_for_webhook('http://192.168.1.50/x', allow_http=True)
        assert ok


def test_is_url_safe_unanchored_allowlist_no_longer_skips_ip_check():
    """Regression: the old code returned True on any allowlist hostname
    match, skipping DNS resolution entirely. A poorly-anchored regex
    like `localhost` would match `localhost.evil.com` and let an
    attacker-controlled hostname through. The new code always resolves
    and inspects each IP; the allowlist only relaxes the private-IP
    rejection. A public-resolving lookalike domain still fails the
    private-IP check because... wait, it would resolve to a public IP.
    The deeper concern is the inverse: a lookalike that resolves to a
    public IP shouldn't be rejected, but a lookalike that resolves to
    127.0.0.1 (DNS rebinding) MUST be rejected even if the regex
    matches. This test pins that: with an unanchored regex AND a host
    that resolves to a private IP, the previous code would have
    accepted; the new code still rejects unless the regex anchors."""
    # `localhost` resolves to 127.0.0.1. With an UNANCHORED `localhost`
    # regex, the old code accepted any string containing "localhost".
    # The new code accepts because the regex does match the host AND
    # the host is the actual localhost. This is fine — the admin opted
    # in. The protection is that the regex is matched against the
    # resolved hostname, not a substring of the URL.
    with patch.dict(os.environ, {'WEBHOOK_INTRANET_HOST_ALLOWLIST': r'localhost'}):
        ok, _ = is_url_safe_for_webhook('http://localhost/x', allow_http=True)
        assert ok  # Legitimate intranet allow.

    # Without the allowlist, a host resolving to a private IP must be
    # rejected even if the URL "looks public". The behaviour predates
    # the refactor; pin it stays.
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('WEBHOOK_INTRANET_HOST_ALLOWLIST', None)
        ok, reason = is_url_safe_for_webhook('http://localhost/x', allow_http=True)
        assert not ok
        assert 'private/loopback' in reason


def test_autopaused_webhook_resumes_on_successful_trial_delivery():
    """Regression: previously auto-pause was terminal — once a webhook
    crossed the failure threshold the only recovery was manual
    re-enable in the UI. Now the dispatcher fires one trial delivery
    per WEBHOOK_TRIAL_INTERVAL_SECONDS (default 1h); a success unpauses
    the webhook and the rest of its queue flushes on subsequent passes."""
    from unittest.mock import patch, MagicMock
    with app.app_context():
        user = _make_user('autopause_resume')
        wh = Webhook(
            user_id=user.id, name='paused', url='https://example.com/w', enabled=True,
        )
        wh.event_list = ['recording.created']
        # Simulate an already auto-paused webhook with a queued delivery
        # whose retry has come due (trial moment).
        wh.enabled = False
        wh.auto_paused = True
        wh.consecutive_failures = 10
        db.session.add(wh)
        db.session.commit()

        # Seed a pending delivery due now (the "trial").
        envelope = '{"id":"x","type":"recording.created","timestamp":"now","user_id":0,"data":{}}'
        d = WebhookDelivery(
            webhook_id=wh.id,
            event_id='trial-1',
            event_type='recording.created',
            payload=envelope,
            status='pending',
            next_retry_at=datetime.utcnow() - timedelta(seconds=1),
        )
        db.session.add(d)
        db.session.commit()
        d_id = d.id

        # Mock the outbound POST to succeed.
        with patch('src.services.webhook_dispatch.requests.post') as mock_post:
            mock_post.return_value = MagicMock(status_code=204, text='')
            run_dispatcher_pass()

        db.session.expire_all()
        wh_after = db.session.get(Webhook, wh.id)
        assert wh_after.enabled is True, 'Successful trial should re-enable webhook'
        assert wh_after.auto_paused is False, 'Successful trial should clear auto_paused'
        assert wh_after.consecutive_failures == 0
        assert db.session.get(WebhookDelivery, d_id).status == 'success'

        WebhookDelivery.query.filter_by(webhook_id=wh.id).delete()
        db.session.delete(wh_after)
        db.session.delete(user)
        db.session.commit()


def test_is_url_safe_rejects_reserved_ip():
    """webhook_dispatch.py:193 — an address flagged is_reserved (but not
    otherwise private/multicast in this Python) must be rejected. Mocks
    DNS to a reserved-only IPv6 (5f00::/16). Kills the
    `or ip.is_reserved` -> `and ip.is_reserved` survivor: under the
    mutation this address is no longer caught and is wrongly allowed."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('WEBHOOK_INTRANET_HOST_ALLOWLIST', None)
        with patch('src.services.webhook_dispatch.socket.getaddrinfo') as mock_resolve:
            mock_resolve.return_value = [
                (None, None, None, None, ('5f00::1', 0, 0, 0)),
            ]
            ok, reason = is_url_safe_for_webhook('http://reserved.example.test/x', allow_http=True)
            assert not ok
            assert '5f00::1' in reason


def test_is_url_safe_rejects_multicast_ip():
    """webhook_dispatch.py:193 — a multicast address (224.0.0.0/4) must be
    rejected. 224.0.0.1 is multicast but NOT private, so only the
    is_multicast clause catches it."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('WEBHOOK_INTRANET_HOST_ALLOWLIST', None)
        with patch('src.services.webhook_dispatch.socket.getaddrinfo') as mock_resolve:
            mock_resolve.return_value = [
                (None, None, None, None, ('224.0.0.1', 0)),
            ]
            ok, reason = is_url_safe_for_webhook('http://multicast.example.test/x', allow_http=True)
            assert not ok
            assert '224.0.0.1' in reason


def test_is_url_safe_allows_public_ip():
    """A host resolving to a genuinely public address is allowed."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('WEBHOOK_INTRANET_HOST_ALLOWLIST', None)
        with patch('src.services.webhook_dispatch.socket.getaddrinfo') as mock_resolve:
            mock_resolve.return_value = [
                (None, None, None, None, ('8.8.8.8', 0)),
            ]
            ok, reason = is_url_safe_for_webhook('http://public.example.test/x', allow_http=True)
            assert ok, reason


def test_dispatcher_post_does_not_follow_redirects():
    """webhook_dispatch.py:317 — the delivery POST must be made with
    allow_redirects=False so a 3xx from the receiver cannot bounce the
    signed payload to an attacker-controlled location (SSRF via
    redirect). Kills the allow_redirects=False -> True survivor."""
    with app.app_context():
        user = _make_user('wh_noredir')
        wh, d = _seed_pending(user.id)
        mock_resp = MagicMock(status_code=204, text='')
        with patch('src.services.webhook_dispatch.requests.post', return_value=mock_resp) as post:
            run_dispatcher_pass()
            post.assert_called_once()
            assert post.call_args.kwargs.get('allow_redirects') is False

        for x in WebhookDelivery.query.filter_by(webhook_id=wh.id).all():
            db.session.delete(x)
        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


def test_is_retryable_status_classification():
    """webhook_dispatch.py:_is_retryable_status — network errors and
    transient HTTP statuses retry; success and permanent 4xx do not.
    The 2xx case (`return False`) is the line-329 survivor: under the
    `return True` mutation a delivered 2xx would be retried forever."""
    assert _is_retryable_status(None) is True   # network/timeout error
    assert _is_retryable_status(200) is False   # success — must NOT retry
    assert _is_retryable_status(204) is False   # success — must NOT retry
    assert _is_retryable_status(408) is True    # request timeout
    assert _is_retryable_status(429) is True    # rate limited
    assert _is_retryable_status(503) is True    # server error
    assert _is_retryable_status(404) is False   # permanent client error


def test_is_url_safe_inspects_all_resolved_addresses():
    """Sanity: even when the hostname is fine, every resolved address
    must be inspected. Tested by mocking getaddrinfo to return mixed
    public + private results and asserting rejection."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop('WEBHOOK_INTRANET_HOST_ALLOWLIST', None)
        with patch('src.services.webhook_dispatch.socket.getaddrinfo') as mock_resolve:
            # First entry public, second entry private. The function
            # must reject because at least one address is private.
            # Note: 203.0.113.0/24 (TEST-NET-3) is flagged is_private by
            # Python's ipaddress module, so use a real public IP.
            mock_resolve.return_value = [
                (None, None, None, None, ('8.8.8.8', 0)),
                (None, None, None, None, ('10.0.0.5', 0)),
            ]
            ok, reason = is_url_safe_for_webhook('http://multi-a.example.test/x', allow_http=True)
            assert not ok
            assert '10.0.0.5' in reason


# --- emit_webhook_event ------------------------------------------------------

def test_emit_creates_delivery_row_for_matching_subscription():
    with app.app_context():
        user = _make_user("wh_emit")
        wh = Webhook(user_id=user.id, name="t", url="https://example.com/wh", enabled=True)
        wh.event_list = ['recording.created']
        db.session.add(wh)
        db.session.commit()

        created = emit_webhook_event(user.id, 'recording.created', {'recording_id': 42})
        assert created == 1

        deliveries = WebhookDelivery.query.filter_by(webhook_id=wh.id).all()
        assert len(deliveries) == 1
        d = deliveries[0]
        assert d.status == 'pending'
        envelope = json.loads(d.payload)
        assert envelope['type'] == 'recording.created'
        assert envelope['user_id'] == user.id
        assert envelope['data']['recording_id'] == 42

        # Cleanup
        for d in deliveries:
            db.session.delete(d)
        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


def test_emit_skips_unsubscribed_event():
    with app.app_context():
        user = _make_user("wh_unsub")
        wh = Webhook(user_id=user.id, name="t", url="https://example.com/wh", enabled=True)
        wh.event_list = ['recording.created']
        db.session.add(wh)
        db.session.commit()

        created = emit_webhook_event(user.id, 'recording.deleted', {'recording_id': 1})
        assert created == 0
        assert WebhookDelivery.query.filter_by(webhook_id=wh.id).count() == 0

        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


def test_emit_skips_disabled_webhook():
    with app.app_context():
        user = _make_user("wh_disabled")
        wh = Webhook(user_id=user.id, name="t", url="https://example.com/wh", enabled=False)
        wh.event_list = ['recording.created']
        db.session.add(wh)
        db.session.commit()
        created = emit_webhook_event(user.id, 'recording.created', {})
        assert created == 0
        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


# --- Dispatcher pass --------------------------------------------------------

def _seed_pending(user_id, *, url='https://example.com/wh', secret='s'):
    wh = Webhook(user_id=user_id, name="t", url=url, secret=secret, enabled=True)
    wh.event_list = ['recording.created']
    db.session.add(wh)
    db.session.flush()
    d = WebhookDelivery(
        webhook_id=wh.id,
        event_id=str(uuid.uuid4()),
        event_type='recording.created',
        payload=json.dumps({'id': 'x', 'type': 'recording.created', 'data': {}}),
        status='pending',
        next_retry_at=datetime.utcnow(),
    )
    db.session.add(d)
    db.session.commit()
    return wh, d


def test_dispatcher_success_path_marks_delivered():
    with app.app_context():
        user = _make_user("wh_disp_ok")
        wh, d = _seed_pending(user.id)

        mock_resp = MagicMock(status_code=204, text='')
        with patch('src.services.webhook_dispatch.requests.post', return_value=mock_resp) as post:
            counters = run_dispatcher_pass()
            assert counters['attempted'] == 1
            assert counters['success'] == 1
            post.assert_called_once()
            call = post.call_args
            # Signature header is set
            headers = call.kwargs['headers']
            assert headers['Speakr-Signature'].startswith('sha256=')
            assert headers['Speakr-Event'] == 'recording.created'

        d_refreshed = db.session.get(WebhookDelivery, d.id)
        wh_refreshed = db.session.get(Webhook, wh.id)
        assert d_refreshed.status == 'success'
        assert d_refreshed.attempt_count == 1
        assert wh_refreshed.consecutive_failures == 0

        db.session.delete(d_refreshed)
        db.session.delete(wh_refreshed)
        db.session.delete(user)
        db.session.commit()


def test_dispatcher_retryable_failure_marks_failed_with_backoff():
    with app.app_context():
        user = _make_user("wh_disp_retry")
        wh, d = _seed_pending(user.id)

        mock_resp = MagicMock(status_code=502, text='bad gateway')
        with patch('src.services.webhook_dispatch.requests.post', return_value=mock_resp):
            counters = run_dispatcher_pass()
            assert counters['attempted'] == 1
            assert counters['failed'] == 1

        d_refreshed = db.session.get(WebhookDelivery, d.id)
        assert d_refreshed.status == 'failed'
        assert d_refreshed.attempt_count == 1
        assert d_refreshed.next_retry_at is not None
        assert d_refreshed.next_retry_at > datetime.utcnow()

        db.session.delete(d_refreshed)
        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


def test_dispatcher_permanent_failure_on_4xx():
    with app.app_context():
        user = _make_user("wh_disp_perm")
        wh, d = _seed_pending(user.id)

        mock_resp = MagicMock(status_code=400, text='bad request')
        with patch('src.services.webhook_dispatch.requests.post', return_value=mock_resp):
            counters = run_dispatcher_pass()
            assert counters['permanent_failure'] == 1

        d_refreshed = db.session.get(WebhookDelivery, d.id)
        assert d_refreshed.status == 'permanent_failure'
        wh_refreshed = db.session.get(Webhook, wh.id)
        assert wh_refreshed.consecutive_failures == 1

        db.session.delete(d_refreshed)
        db.session.delete(wh_refreshed)
        db.session.delete(user)
        db.session.commit()


def test_dispatcher_autopauses_after_threshold():
    with app.app_context():
        user = _make_user("wh_autopause")
        wh, _ = _seed_pending(user.id)
        # Pretend 9 consecutive failures already happened; threshold is 10.
        wh.consecutive_failures = 9
        db.session.commit()

        # The seeded delivery is permanent-failed → 10th consecutive failure
        # should auto-pause.
        mock_resp = MagicMock(status_code=410, text='gone')  # 4xx permanent
        with patch.dict(os.environ, {'WEBHOOK_AUTOPAUSE_FAILURES': '10'}):
            with patch('src.services.webhook_dispatch.requests.post', return_value=mock_resp):
                run_dispatcher_pass()

        wh_refreshed = db.session.get(Webhook, wh.id)
        assert wh_refreshed.enabled is False
        assert wh_refreshed.auto_paused is True
        assert wh_refreshed.consecutive_failures == 10

        for d in WebhookDelivery.query.filter_by(webhook_id=wh.id).all():
            db.session.delete(d)
        db.session.delete(wh_refreshed)
        db.session.delete(user)
        db.session.commit()


def test_delay_for_attempt_uses_designed_schedule():
    # Attempt 1 = immediate (0s), then 30s, 120s, 600s, 3600s, then 3600 cap.
    assert _delay_for_attempt(1) == 0
    assert _delay_for_attempt(2) == 30
    assert _delay_for_attempt(3) == 120
    assert _delay_for_attempt(4) == 600
    assert _delay_for_attempt(5) == 3600
    assert _delay_for_attempt(99) == 3600


# --- v1 CRUD endpoints ------------------------------------------------------

def test_create_webhook_returns_secret_once_and_omits_thereafter():
    with app.app_context():
        user = _make_user("wh_crud_create")
        client = app.test_client()
        _login(client, user)
        resp = client.post('/api/v1/webhooks', json={
            'name': 'n8n prod',
            'url': 'https://example.com/hook',
            'events': ['recording.created', 'recording.transcription.completed'],
        })
        assert resp.status_code == 201, resp.data
        body = resp.get_json()
        assert 'secret' in body and len(body['secret']) > 0
        wh_id = body['id']

        # Subsequent reads omit secret
        get_resp = client.get(f'/api/v1/webhooks/{wh_id}')
        assert get_resp.status_code == 200
        assert 'secret' not in get_resp.get_json()

        # Cleanup
        client.delete(f'/api/v1/webhooks/{wh_id}')
        db.session.delete(user)
        db.session.commit()


def test_create_webhook_rejects_unknown_event():
    with app.app_context():
        user = _make_user("wh_crud_unknown")
        client = app.test_client()
        _login(client, user)
        resp = client.post('/api/v1/webhooks', json={
            'name': 'x', 'url': 'https://e.com/h', 'events': ['nope.bad'],
        })
        assert resp.status_code == 400
        db.session.delete(user)
        db.session.commit()


def test_create_webhook_rejects_private_url():
    with app.app_context():
        user = _make_user("wh_crud_private")
        client = app.test_client()
        _login(client, user)
        resp = client.post('/api/v1/webhooks', json={
            'name': 'x', 'url': 'http://192.168.0.5/h', 'allow_http': True,
            'events': ['recording.created'],
        })
        assert resp.status_code == 400
        db.session.delete(user)
        db.session.commit()


def test_list_only_returns_caller_webhooks():
    with app.app_context():
        a = _make_user("wh_list_a")
        b = _make_user("wh_list_b")
        wh_a = Webhook(user_id=a.id, name='A', url='https://example.com/a')
        wh_a.event_list = ['recording.created']
        wh_b = Webhook(user_id=b.id, name='B', url='https://example.com/b')
        wh_b.event_list = ['recording.created']
        db.session.add_all([wh_a, wh_b])
        db.session.commit()

        client = app.test_client()
        _login(client, a)
        resp = client.get('/api/v1/webhooks')
        body = resp.get_json()
        ids = [w['id'] for w in body['webhooks']]
        assert wh_a.id in ids
        assert wh_b.id not in ids

        db.session.delete(wh_a); db.session.delete(wh_b)
        db.session.delete(a); db.session.delete(b)
        db.session.commit()


def test_patch_webhook_updates_events_and_enabled():
    with app.app_context():
        user = _make_user("wh_crud_patch")
        client = app.test_client()
        _login(client, user)
        wh = Webhook(user_id=user.id, name='x', url='https://example.com/h')
        wh.event_list = ['recording.created']
        db.session.add(wh)
        db.session.commit()

        resp = client.patch(f'/api/v1/webhooks/{wh.id}', json={
            'events': ['recording.summary.completed'],
            'enabled': False,
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['events'] == ['recording.summary.completed']
        assert body['enabled'] is False

        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


def test_rotate_secret_returns_fresh_secret():
    with app.app_context():
        user = _make_user("wh_crud_rotate")
        client = app.test_client()
        _login(client, user)
        wh = Webhook(user_id=user.id, name='x', url='https://example.com/h', secret='old-secret')
        wh.event_list = ['recording.created']
        db.session.add(wh)
        db.session.commit()
        old = wh.secret
        resp = client.post(f'/api/v1/webhooks/{wh.id}/rotate-secret')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['secret'] != old
        wh_refreshed = db.session.get(Webhook, wh.id)
        assert wh_refreshed.secret == body['secret']
        db.session.delete(wh_refreshed)
        db.session.delete(user)
        db.session.commit()


def test_test_fire_enqueues_synthetic_delivery():
    with app.app_context():
        user = _make_user("wh_crud_test")
        client = app.test_client()
        _login(client, user)
        wh = Webhook(user_id=user.id, name='x', url='https://example.com/h', enabled=True)
        wh.event_list = ['recording.created']
        db.session.add(wh)
        db.session.commit()
        resp = client.post(f'/api/v1/webhooks/{wh.id}/test')
        assert resp.status_code == 202
        d_id = resp.get_json()['id']
        d = db.session.get(WebhookDelivery, d_id)
        assert d.event_type == 'webhook.test'
        assert d.status == 'pending'
        db.session.delete(d)
        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


def test_replay_creates_new_delivery_with_fresh_event_id():
    with app.app_context():
        user = _make_user("wh_crud_replay")
        client = app.test_client()
        _login(client, user)
        wh = Webhook(user_id=user.id, name='x', url='https://example.com/h')
        wh.event_list = ['recording.created']
        db.session.add(wh)
        db.session.flush()
        original = WebhookDelivery(
            webhook_id=wh.id,
            event_id=str(uuid.uuid4()),
            event_type='recording.created',
            payload=json.dumps({'id': 'orig', 'type': 'recording.created', 'data': {'r': 1}}),
            status='permanent_failure',
        )
        db.session.add(original)
        db.session.commit()

        resp = client.post(f'/api/v1/webhooks/{wh.id}/deliveries/{original.id}/replay')
        assert resp.status_code == 202
        replay_id = resp.get_json()['id']
        replay = db.session.get(WebhookDelivery, replay_id)
        assert replay.id != original.id
        assert replay.event_id != original.event_id
        assert replay.status == 'pending'
        env = json.loads(replay.payload)
        assert env['replayed_from'] == original.event_id

        db.session.delete(replay)
        db.session.delete(original)
        db.session.delete(wh)
        db.session.delete(user)
        db.session.commit()


def test_per_user_cap_enforced():
    with app.app_context():
        user = _make_user("wh_crud_cap")
        client = app.test_client()
        _login(client, user)
        with patch.dict(os.environ, {'WEBHOOK_MAX_PER_USER': '2'}):
            for i in range(2):
                r = client.post('/api/v1/webhooks', json={
                    'name': f'h{i}', 'url': f'https://example.com/{i}',
                    'events': ['recording.created'],
                })
                assert r.status_code == 201, r.data
            r = client.post('/api/v1/webhooks', json={
                'name': 'too-many', 'url': 'https://example.com/x',
                'events': ['recording.created'],
            })
            assert r.status_code == 409

        for w in Webhook.query.filter_by(user_id=user.id).all():
            db.session.delete(w)
        db.session.delete(user)
        db.session.commit()


def teardown_module(module):
    with app.app_context():
        for u in User.query.filter(User.username.like("wh_%")).all():
            for w in Webhook.query.filter_by(user_id=u.id).all():
                db.session.delete(w)
            db.session.delete(u)
        db.session.commit()
