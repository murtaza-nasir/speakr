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


def test_the_account_budget_defaults_to_twice_the_token_budget():
    from src.utils import token_auth as ta
    assert ta._doubled("120 per minute") == "240 per minute"
    assert ta._doubled("10/second; 100 per 5 minutes") == "20 per second; 200 per 5 minute"
    assert ta._doubled("not a limit") is None
    if not any(os.environ.get(f"API_USER_RATE_LIMIT_{k}") for k in ("READ", "WRITE", "PROCESS")):
        assert ta._USER_RATE_LIMITS == {k: ta._doubled(v) for k, v in ta._RATE_LIMITS.items()}


@pytest.fixture
def live_limiter():
    """An enabled limiter with small budgets in place of the suite's disabled one.

    RATELIMIT_ENABLED is false under test, so the app's own limiter was never
    set up; this one is initialised on a throwaway app and counts in memory.
    Token budget 3, account budget 5 (read); 2 and 4 (write).
    """
    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    import src.app as app_module
    from src.api import api_v1
    from src.utils import token_auth as ta
    live = Limiter(get_remote_address, storage_uri="memory://", enabled=True)
    live.init_app(Flask("limiter-host"))
    # The route limits in api_v1 use the same limiter, as in the app.
    with patch.object(app_module, "limiter", live), patch.object(api_v1, "limiter", live), \
         patch.dict(ta._RATE_LIMITS, {"read": "3 per minute", "write": "2 per minute"}), \
         patch.dict(ta._USER_RATE_LIMITS, {"read": "5 per minute", "write": "4 per minute"}):
        yield live


def _read(c, plain, path="/api/v1/recordings"):
    return c.get(path, headers=_h(plain))


def test_tokens_of_one_account_share_the_account_budget(world, live_limiter):
    first, second = world["token"](None), world["token"](["read"])
    with app.app_context():
        c = _Client(app, app.response_class)
        assert [_read(c, first).status_code for _ in range(3)] == [200, 200, 200]
        over_token = _read(c, first)
        assert [_read(c, second).status_code for _ in range(2)] == [200, 200]
        over_account = _read(c, second)
    # The first token runs out of its own budget; that refusal is not counted
    # against the account, which the second token then exhausts at 5.
    assert over_token.status_code == 429 and over_token.headers.get("Retry-After")
    assert over_token.get_json()["code"] == "token_rate_limit_exceeded"
    assert "this token" in over_token.get_json()["error"]
    assert over_account.status_code == 429 and over_account.headers.get("Retry-After")
    body = over_account.get_json()
    assert body["code"] == "account_rate_limit_exceeded"
    assert "your account" in body["error"] and body["limit"] == "5 per 1 minute"


def test_a_single_token_meets_its_own_budget_first(world, live_limiter):
    plain = world["token"](None)
    with app.app_context():
        c = _Client(app, app.response_class)
        codes = [_read(c, plain).status_code for _ in range(4)]
        refused = _read(c, plain)
    assert codes == [200, 200, 200, 429]
    assert refused.get_json()["code"] == "token_rate_limit_exceeded"


def test_the_account_budget_spans_routes_and_kinds_stay_apart(world, live_limiter):
    plain = world["token"](None)
    with app.app_context():
        c = _Client(app, app.response_class)
        # The token budget is counted per route, the account budget across them.
        codes = [_read(c, plain).status_code for _ in range(3)]
        codes += [_read(c, plain, "/api/v1/tags").status_code for _ in range(3)]
        write = c.patch(f"/api/v1/recordings/{world['rec']}", json={"title": "Still allowed"},
                        headers=_h(plain))
    assert codes == [200, 200, 200, 200, 200, 429]
    assert write.status_code == 200


def test_session_traffic_is_not_held_by_token_budgets(world, live_limiter):
    c = _session(world)
    assert {c.get("/api/v1/recordings").status_code for _ in range(8)} == {200}


def _codes(send, n):
    return [send().status_code for _ in range(n)]


def test_the_asr_upload_limit_counts_each_request_once(live_limiter):
    """520 units a minute at 52 per request: ten uploads, not five."""
    c = app.test_client()

    def send():
        # A wrong secret is refused after the limit has counted the request.
        return c.post("/api/v1/integrations/asr-voice-recorder/upload", data={"secret": "wrong"},
                      content_type="multipart/form-data", environ_base={"REMOTE_ADDR": "203.0.113.48"})
    assert _codes(send, 11) == [401] * 10 + [429]


def test_the_search_limit_counts_each_request_once(world, live_limiter):
    from src.utils import token_auth as ta
    plain = world["token"](None)
    with patch.dict(ta._RATE_LIMITS, {"read": "100 per minute"}), \
         patch.dict(ta._USER_RATE_LIMITS, {"read": "100 per minute"}):
        with app.app_context():
            c = _Client(app, app.response_class)
            codes = _codes(lambda: c.get("/api/v1/search?q=x", headers=_h(plain)), 31)
    assert codes == [400] * 30 + [429]   # API_SEARCH_RATE_LIMIT: 30 per minute


@pytest.mark.parametrize("token_limit,account_limit,code", [
    ("4 per minute", "100 per minute", "token_rate_limit_exceeded"),
    ("100 per minute", "4 per minute", "account_rate_limit_exceeded"),
])
def test_token_and_account_budgets_count_once_on_a_route_with_its_own_limit(world, live_limiter, token_limit,
                                                                            account_limit, code):
    from src.utils import token_auth as ta
    plain = world["token"](None)
    with patch.dict(ta._RATE_LIMITS, {"read": token_limit}), \
         patch.dict(ta._USER_RATE_LIMITS, {"read": account_limit}):
        with app.app_context():
            c = _Client(app, app.response_class)
            codes = _codes(lambda: c.get("/api/v1/search?q=x", headers=_h(plain)), 4)
            refused = c.get("/api/v1/search?q=x", headers=_h(plain))
    assert codes == [400] * 4
    assert refused.status_code == 429 and refused.get_json()["code"] == code


def test_registering_the_limits_again_still_counts_each_request_once(world, live_limiter):
    """A second registration on the same limiter (two threads that both saw the
    route unregistered) must not add a second copy of each limit."""
    plain = world["token"](None)
    view = app.view_functions["api_v1.list_recordings"]
    with app.app_context():
        c = _Client(app, app.response_class)
        first = _read(c, plain)
        view._limit_state["limiter"] = None     # as the thread that lost the race sees it
        rest = [_read(c, plain).status_code for _ in range(3)]
    assert [first.status_code] + rest == [200, 200, 200, 429]   # token budget 3


def test_concurrent_first_requests_register_the_limits_once(world, live_limiter):
    import threading
    plain = world["token"](None)
    barrier = threading.Barrier(6)
    codes = []

    def first_request():
        with app.app_context():
            c = _Client(app, app.response_class)
            barrier.wait()
            codes.append(_read(c, plain, "/api/v1/tags").status_code)

    threads = [threading.Thread(target=first_request) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Token budget 3 on this route: three pass, three are refused, whatever
    # the interleaving; a doubled registration would let only one through.
    assert sorted(codes) == [200, 200, 200, 429, 429, 429]


# ------------------------------------------------------------ token cap

def test_the_token_cap_refuses_another_active_token_until_one_is_revoked(world):
    from datetime import datetime, timedelta
    from src.api import tokens as tokens_api
    c = _session(world)
    with app.app_context():
        # A revoked or an expired token does not take a slot.
        for kwargs in ({"revoked": True}, {"expires_at": datetime.utcnow() - timedelta(days=1)}):
            t = APIToken(user_id=world["user"], token_hash=hash_token(secrets.token_urlsafe(16)), name="old",
                         **kwargs)
            db.session.add(t)
            db.session.commit()
            world["tokens"].append(t.id)
    with patch.object(tokens_api, "API_MAX_TOKENS_PER_USER", 2):
        made = [c.post("/api/tokens", json={"name": f"t{i}"}) for i in range(2)]
        refused = c.post("/api/tokens", json={"name": "t3"})
        assert c.delete(f"/api/tokens/{made[0].get_json()['id']}").status_code == 200
        after_revoke = c.post("/api/tokens", json={"name": "t3"})
    assert [r.status_code for r in made] == [201, 201]
    assert refused.status_code == 409
    body = refused.get_json()
    assert body["code"] == "token_limit_reached" and body["max_tokens"] == 2 and body["active_tokens"] == 2
    assert "Revoke" in body["error"]
    assert after_revoke.status_code == 201


def test_tokens_above_a_lowered_cap_keep_working(world):
    from src.api import tokens as tokens_api
    plains = [world["token"](None) for _ in range(3)]
    with patch.object(tokens_api, "API_MAX_TOKENS_PER_USER", 1):
        assert _session(world).post("/api/tokens", json={"name": "x"}).status_code == 409
        with app.app_context():
            c = _Client(app, app.response_class)
            assert {_read(c, p).status_code for p in plains} == {200}


def test_a_token_cap_of_zero_means_no_limit(world):
    from src.api import tokens as tokens_api
    c = _session(world)
    with patch.object(tokens_api, "API_MAX_TOKENS_PER_USER", 0):
        assert {c.post("/api/tokens", json={"name": f"t{i}"}).status_code for i in range(4)} == {201}


def test_a_token_never_carries_over_to_the_next_request(world):
    """g outlives a request when a caller holds an app context; the token must not."""
    plain = world["token"](None)
    with app.app_context():
        with _Client(app, app.response_class) as c:   # drops Flask-Login's own g cache, as a new request does
            assert c.get("/api/v1/recordings", headers=_h(plain)).status_code == 200
            assert c.get("/api/v1/recordings").status_code == 401
