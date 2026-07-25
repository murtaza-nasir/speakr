#!/usr/bin/env python3
"""
Regression tests for the security-audit fixes.

Covers:
  - bulk_toggle IDOR: a user cannot toggle inbox/highlight on another user's
    recording, and no SharedRecordingState row is created for it.
  - rate_limit decorator actually applies a Flask-Limiter limit (it was a
    silent no-op, leaving auth endpoints without per-endpoint limits).
  - Webhook delivery re-validates the URL at SEND time, blocking a target that
    resolves to a private/loopback address (DNS-rebinding SSRF).

Standalone style (no pytest fixtures), matching the neighbouring suites.
"""

import secrets
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording
from src.models.sharing import SharedRecordingState

# The bulk-toggle endpoint is a session-cookie POST guarded by CSRF; disable
# CSRF for this suite so the test client can exercise it directly (matches the
# other endpoint suites).
app.config["WTF_CSRF_ENABLED"] = False


def _make_user(suffix):
    username = f"secfix_{suffix}_{secrets.token_hex(3)}"
    user = User(username=username, email=f"{username}@local.test")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


# ---------------------------------------------------------------------------
# bulk_toggle IDOR
# ---------------------------------------------------------------------------


def test_bulk_toggle_ignores_other_users_recording():
    with app.app_context():
        owner = _make_user("owner")
        attacker = _make_user("attacker")
        victim_rec = Recording(user_id=owner.id, title="victim", status="COMPLETED")
        db.session.add(victim_rec)
        db.session.commit()
        rec_id = victim_rec.id

        client = app.test_client()
        _login(client, attacker)
        try:
            resp = client.post("/api/recordings/bulk-toggle",
                               json={"recording_ids": [rec_id], "field": "highlight", "value": True})
            assert resp.status_code == 200, resp.data
            body = resp.get_json()
            # The victim's recording must NOT be reported as affected...
            assert rec_id not in body.get("affected_ids", [])
            # ...and no per-user status row may have been created for the attacker.
            leaked = SharedRecordingState.query.filter_by(
                user_id=attacker.id, recording_id=rec_id).first()
            assert leaked is None, "bulk_toggle created a status row for an inaccessible recording"
        finally:
            SharedRecordingState.query.filter_by(recording_id=rec_id).delete()
            db.session.delete(db.session.get(Recording, rec_id))
            db.session.delete(db.session.get(User, attacker.id))
            db.session.delete(db.session.get(User, owner.id))
            db.session.commit()


def test_bulk_toggle_still_works_for_own_recording():
    with app.app_context():
        owner = _make_user("selfowner")
        rec = Recording(user_id=owner.id, title="mine", status="COMPLETED")
        db.session.add(rec)
        db.session.commit()
        rec_id = rec.id

        client = app.test_client()
        _login(client, owner)
        try:
            resp = client.post("/api/recordings/bulk-toggle",
                               json={"recording_ids": [rec_id], "field": "highlight", "value": True})
            assert resp.status_code == 200, resp.data
            assert rec_id in resp.get_json().get("affected_ids", [])
        finally:
            SharedRecordingState.query.filter_by(recording_id=rec_id).delete()
            db.session.delete(db.session.get(Recording, rec_id))
            db.session.delete(db.session.get(User, owner.id))
            db.session.commit()


# ---------------------------------------------------------------------------
# rate_limit decorator actually applies a limit
# ---------------------------------------------------------------------------


def test_rate_limit_decorator_applies_limiter_limit():
    """The auth rate_limit decorator must call limiter.limit() (previously it
    was a no-op that only stashed the string on the wrapper)."""
    import src.api.auth as auth_mod

    calls = {"limit_arg": None, "applied": False}

    class FakeLimiter:
        enabled = True

        def limit(self, limit_string):
            calls["limit_arg"] = limit_string

            def deco(f):
                def wrapped(*a, **k):
                    calls["applied"] = True
                    return f(*a, **k)
                return wrapped
            return deco

    original = auth_mod.limiter
    auth_mod.limiter = FakeLimiter()
    try:
        @auth_mod.rate_limit("7 per minute")
        def view():
            return "ok"

        assert view() == "ok"
        assert calls["applied"] is True, "limiter.limit() was never applied — decorator is a no-op"
        assert calls["limit_arg"] == "7 per minute"
        assert getattr(view, "_rate_limit", None) == "7 per minute"
    finally:
        auth_mod.limiter = original


def test_rate_limit_decorator_passthrough_when_disabled():
    """When the limiter is disabled, the decorator must not block the view."""
    import src.api.auth as auth_mod

    class DisabledLimiter:
        enabled = False

        def limit(self, _):  # pragma: no cover - must not be called
            raise AssertionError("limiter.limit called while disabled")

    original = auth_mod.limiter
    auth_mod.limiter = DisabledLimiter()
    try:
        @auth_mod.rate_limit("1 per minute")
        def view():
            return "ok"

        for _ in range(5):
            assert view() == "ok"
    finally:
        auth_mod.limiter = original


# ---------------------------------------------------------------------------
# Webhook delivery-time SSRF re-validation
# ---------------------------------------------------------------------------


def test_webhook_delivery_blocks_private_ip_at_send_time():
    """_post_delivery must re-validate the URL and refuse to POST to a target
    that resolves to a private/loopback address, even if it passed at create
    time (DNS rebinding)."""
    from src.services import webhook_dispatch

    wh = MagicMock()
    wh.url = "http://127.0.0.1:8080/internal"
    wh.allow_http = True
    wh.secret = "s"
    delivery = MagicMock()
    delivery.payload = '{"x":1}'
    delivery.event_id = "evt_1"
    delivery.event_type = "recording.completed"

    with app.app_context():
        with patch("src.services.webhook_dispatch._http_post") as mock_post:
            status, preview, error = webhook_dispatch._post_delivery(delivery, wh)
            assert mock_post.call_count == 0, "POST was sent to a private-IP target"
            assert status is None and preview is None
            assert error and "blocked at delivery" in error


def test_webhook_delivery_allows_public_host():
    """A public host must still be delivered to (POST is called)."""
    from src.services import webhook_dispatch

    wh = MagicMock()
    wh.url = "https://example.com/hook"
    wh.allow_http = False
    wh.secret = "s"
    delivery = MagicMock()
    delivery.payload = '{"x":1}'
    delivery.event_id = "evt_2"
    delivery.event_type = "recording.completed"

    with app.app_context():
        with patch("src.services.webhook_dispatch._http_post",
                   return_value=MagicMock(status_code=204, text="")) as mock_post:
            status, preview, error = webhook_dispatch._post_delivery(delivery, wh)
            # example.com resolves publicly; if DNS is unavailable the guard
            # falls through (transient failure is not a block) and the POST is
            # still attempted — either way the POST must be reached.
            assert mock_post.call_count == 1
            assert error is None


# ---------------------------------------------------------------------------
# Long recordings must not be throttled by the global IP rate limit
# ---------------------------------------------------------------------------


def test_recording_session_chunk_uploads_are_rate_limit_exempt():
    """The server-side recording-session endpoints (the recommended path for
    multi-hour recordings) must be exempt from the global IP rate limit, or a
    long session's ~720 chunks/hour would start getting 429'd partway through."""
    from src.app import exempt_status_endpoints

    chunk_path = "/upload/session/2b6a-uuid/chunks/842"
    finalize_path = "/upload/session/2b6a-uuid/finalize"
    with app.test_request_context(chunk_path, method="POST"):
        assert exempt_status_endpoints() is True
    with app.test_request_context(finalize_path, method="POST"):
        assert exempt_status_endpoints() is True
    # A regular (non-session) endpoint must NOT be blanket-exempted.
    with app.test_request_context("/api/recordings", method="GET"):
        assert exempt_status_endpoints() is not True


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
