"""Scoped API tokens (mailr spec G1).

A token with scopes NULL is a full token and behaves exactly as before scopes
existed. A scoped token is accepted only in a header, only on routes marked
with require_scope, and only with every scope the route needs; a refused
request has no side effect.

SHARED-DB: users, tokens and recordings are removed afterwards.
"""

import io
import json
import os
import secrets
import sys
from unittest.mock import patch

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, User, Webhook, WebhookDelivery
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def world():
    made = {"tokens": [], "recs": []}
    with app.app_context():
        n = f"scope_{secrets.token_hex(4)}"
        u = User(username=n, email=f"{n}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="Original title", status="COMPLETED", original_filename="s.wav",
                      transcription="Speaker 1: hello there, this is a test transcript.")
        db.session.add(r)
        db.session.commit()
        made.update(user=u.id, rec=r.id)

    def token(scopes):
        plain = f"tok-{secrets.token_urlsafe(16)}"
        with app.app_context():
            t = APIToken(user_id=made["user"], token_hash=hash_token(plain), name="t",
                         scopes=None if scopes is None else json.dumps(sorted(scopes)))
            db.session.add(t)
            db.session.commit()
            made["tokens"].append(t.id)
        return plain
    made["token"] = token
    yield made
    with app.app_context():
        db.session.rollback()
        WebhookDelivery.query.filter(WebhookDelivery.webhook_id.in_(
            [w.id for w in Webhook.query.filter_by(user_id=made["user"]).all()])).delete(synchronize_session=False)
        Webhook.query.filter_by(user_id=made["user"]).delete()
        for tid in made["tokens"]:
            obj = db.session.get(APIToken, tid)
            if obj is not None:
                db.session.delete(obj)
        APIToken.query.filter_by(user_id=made["user"]).delete()
        for rid in [made["rec"], *made["recs"]]:
            obj = db.session.get(Recording, rid)
            if obj is not None:
                db.session.delete(obj)
        db.session.delete(db.session.get(User, made["user"]))
        db.session.commit()


def _h(plain):
    return {"Authorization": f"Bearer {plain}"}


def _title(rid):
    with app.app_context():
        return db.session.get(Recording, rid).title


# ------------------------------------------------------------ full tokens

def test_a_full_token_works_everywhere_as_before(world):
    plain = world["token"](None)
    with app.test_client() as c:
        assert c.get("/api/v1/recordings", headers=_h(plain)).status_code == 200
        assert c.patch(f"/api/v1/recordings/{world['rec']}", json={"title": "Renamed"}, headers=_h(plain)).status_code == 200
        assert c.get("/api/recordings", headers=_h(plain)).status_code == 200          # web-UI route
        assert c.get(f"/api/v1/recordings?token={plain}").status_code == 200          # query token
    assert _title(world["rec"]) == "Renamed"


# ------------------------------------------------------------ read token

def test_a_read_token_reads(world):
    plain = world["token"](["read"])
    with app.test_client() as c:
        for path in ("/api/v1/users/me", "/api/v1/stats", "/api/v1/recordings", f"/api/v1/recordings/{world['rec']}",
                     f"/api/v1/recordings/{world['rec']}/transcript", f"/api/v1/recordings/{world['rec']}/summary",
                     f"/api/v1/recordings/{world['rec']}/notes", f"/api/v1/recordings/{world['rec']}/speakers",
                     f"/api/v1/recordings/{world['rec']}/events", "/api/v1/tags", "/api/v1/folders"):
            resp = c.get(path, headers=_h(plain))
            assert resp.status_code != 403, (path, resp.get_json())


@pytest.mark.parametrize("method,path,body,needed", [
    ("patch", "/api/v1/recordings/{rec}", {"title": "Hacked"}, "write"),
    ("put", "/api/v1/recordings/{rec}/notes", {"notes": "x"}, "write"),
    ("delete", "/api/v1/recordings/{rec}", None, "delete"),
    ("post", "/api/v1/recordings/{rec}/chat", {"message": "hi"}, "process"),
    ("post", "/api/v1/recordings/{rec}/transcribe", {}, "process"),
    ("get", "/api/v1/webhooks", None, "webhooks"),
    ("put", "/api/v1/settings/auto-summarization", {"enabled": False}, "account"),
])
def test_a_read_token_is_refused_with_no_side_effect(world, method, path, body, needed):
    plain = world["token"](["read"])
    with app.test_client() as c:
        kwargs = {"headers": _h(plain)}
        if body is not None:
            kwargs["json"] = body
        resp = getattr(c, method)(path.format(rec=world["rec"]), **kwargs)
    assert resp.status_code == 403
    data = resp.get_json()
    assert data["code"] == "insufficient_scope"
    assert data["required_scopes"] == [needed] and data["token_scopes"] == ["read"]
    assert resp.headers["WWW-Authenticate"] == f'Bearer error="insufficient_scope", scope="{needed}"'
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        assert r is not None and r.title == "Original title" and r.notes is None


def test_a_refused_request_fires_no_webhook(world):
    with app.app_context():
        wh = Webhook(user_id=world["user"], name="mailr", url="https://example.com/hook", secret="s" * 32,
                     events=json.dumps(["recording.updated"]))
        db.session.add(wh)
        db.session.commit()
        before = WebhookDelivery.query.filter_by(webhook_id=wh.id).count()
    plain = world["token"](["read"])
    with app.test_client() as c:
        assert c.patch(f"/api/v1/recordings/{world['rec']}", json={"title": "x"}, headers=_h(plain)).status_code == 403
    with app.app_context():
        assert WebhookDelivery.query.filter_by(webhook_id=wh.id).count() == before


def test_a_scoped_token_is_refused_outside_v1(world):
    plain = world["token"](["read", "write", "share", "process"])
    with app.test_client() as c:
        assert c.post("/save", json={"id": world["rec"], "title": "x"}, headers=_h(plain)).status_code == 403
        assert c.get("/api/recordings", headers=_h(plain)).status_code == 403
        assert c.post("/api/inquire/search", json={"query": "x"}, headers=_h(plain)).status_code == 403
        assert c.get("/api/tokens", headers=_h(plain)).status_code == 403       # never manages tokens
    assert _title(world["rec"]) == "Original title"


def test_a_scoped_token_in_the_query_string_is_refused(world):
    plain = world["token"](["read"])
    with app.test_client() as c:
        resp = c.get(f"/api/v1/recordings?token={plain}")
    assert resp.status_code == 401


def test_asr_recorder_secret_needs_upload(world):
    up, ro = world["token"](["upload"]), world["token"](["read"])
    with app.test_client() as c:
        ok = c.post("/api/v1/integrations/asr-voice-recorder/upload", data={"secret": up},
                    content_type="multipart/form-data")
        bad = c.post("/api/v1/integrations/asr-voice-recorder/upload", data={"secret": ro},
                     content_type="multipart/form-data")
    assert ok.status_code == 200 and ok.get_json().get("connection_test") is True
    assert bad.status_code == 401


# ------------------------------------------------------------ management

def _session(world):
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(world["user"])
    return c


@pytest.mark.parametrize("scopes", [["bogus"], [], ["full", "read"], "read"])
def test_creating_a_token_with_bad_scopes_is_refused(world, scopes):
    resp = _session(world).post("/api/tokens", json={"name": "x", "scopes": scopes})
    assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_parameter"


def test_creating_tokens_with_and_without_scopes(world):
    c = _session(world)
    scoped = c.post("/api/tokens", json={"name": "mailr", "scopes": ["write", "read", "upload"]})
    full = c.post("/api/tokens", json={"name": "script"})
    assert scoped.status_code == 201 and scoped.get_json()["scopes"] == ["read", "upload", "write"]
    assert full.status_code == 201 and full.get_json()["scopes"] == ["full"]
    listed = {t["name"]: t["scopes"] for t in c.get("/api/tokens").get_json()["tokens"]}
    assert listed["mailr"] == ["read", "upload", "write"] and listed["script"] == ["full"]


def test_scopes_can_only_be_narrowed(world):
    c = _session(world)
    tid = c.post("/api/tokens", json={"name": "n", "scopes": ["read", "write"]}).get_json()["id"]
    assert c.patch(f"/api/tokens/{tid}", json={"scopes": ["read", "write", "delete"]}).status_code == 400
    assert c.patch(f"/api/tokens/{tid}", json={"scopes": ["full"]}).status_code == 400
    ok = c.patch(f"/api/tokens/{tid}", json={"scopes": ["read"]})
    assert ok.status_code == 200 and ok.get_json()["scopes"] == ["read"]
    fid = c.post("/api/tokens", json={"name": "f"}).get_json()["id"]
    assert c.patch(f"/api/tokens/{fid}", json={"scopes": ["read"]}).status_code == 200


# ------------------------------------------------------------ introspection

def test_tokens_current_and_capabilities(world):
    plain = world["token"](["read", "upload"])
    with app.test_client() as c:
        cur = c.get("/api/v1/tokens/current", headers=_h(plain))
        caps = c.get("/api/v1/capabilities", headers=_h(plain))
    assert cur.status_code == 200
    body = cur.get_json()
    assert body["scopes"] == ["read", "upload"] and body["via"] == "header"
    assert body["created_at"].endswith("Z")
    assert caps.status_code == 200 and caps.get_json()["features"]["token_scopes"] is True
    assert caps.get_json()["models_local"] in (True, False)
    assert _session(world).get("/api/v1/tokens/current").status_code == 404


# ------------------------------------------------------------ rate limits

def test_rate_limits_are_per_token():
    from src.utils import token_auth as ta
    for tid in (41, 42):
        with app.test_request_context("/api/v1/recordings"):
            from flask import request
            request.environ["_speakr_api_token"] = (type("T", (), {"id": tid})(), False)
            assert ta.token_rate_key() == f"token:{tid}"
    assert ta._RATE_LIMITS == {"read": "120 per minute", "write": "30 per minute", "process": "10 per minute"} \
        or os.environ.get("API_TOKEN_RATE_LIMIT_READ")
    assert ta._rate_category({"read"}) == "read"
    assert ta._rate_category({"process"}) == "process"
    assert ta._rate_category({"write"}) == "write" and ta._rate_category({"delete"}) == "write"


def test_a_token_never_carries_over_to_the_next_request(world):
    """g outlives a request when a caller holds an app context; the token must not."""
    plain = world["token"](None)
    with app.app_context():
        with _Client(app, app.response_class) as c:   # drops Flask-Login's own g cache, as a new request does
            assert c.get("/api/v1/recordings", headers=_h(plain)).status_code == 200
            assert c.get("/api/v1/recordings").status_code == 401
