"""System Statistics overview (/admin/stats/overview).

The range and the range before it, recordings per day by source, failure
rate, language-model tokens by task with embeddings as a series of their own,
cost per model, twelve calendar months of cost and one row per user.

The database is shared, so each test reads the overview before and after
adding its own rows and checks the difference.

SHARED-DB: users, usage rows and recordings are removed afterwards.
"""

import os
import secrets
import sys
from datetime import date, datetime, timedelta

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, TokenUsage, TranscriptionUsage, User
from src.services.admin_stats import build_overview


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        admin = User(username=f"ov_{s}", email=f"ov_{s}@local.test", password="x", is_admin=True,
                     monthly_token_budget=1000, monthly_transcription_budget=600)
        plain = User(username=f"ovp_{s}", email=f"ovp_{s}@local.test", password="x")
        db.session.add_all([admin, plain])
        db.session.commit()
        ids = {"admin": admin.id, "plain": plain.id}
    yield ids
    with app.app_context():
        db.session.rollback()
        for uid in ids.values():
            TokenUsage.query.filter_by(user_id=uid).delete()
            TranscriptionUsage.query.filter_by(user_id=uid).delete()
            Recording.query.filter_by(user_id=uid).delete()
            User.query.filter_by(id=uid).delete()
        db.session.commit()


def _client(uid):
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return c


def _rec(uid, when, source="upload", status="COMPLETED", seconds=None, title="r"):
    db.session.add(Recording(user_id=uid, title=title, status=status, created_at=when, processing_source=source,
                             processing_time_seconds=seconds, file_size=1000, error_message="boom" if status == "FAILED" else None))


def _day(o, iso, part="activity"):
    return next(d for d in o[part]["daily"] if d["date"] == iso)


def test_only_admins_read_it(world):
    assert _client(world["plain"]).get("/admin/stats/overview").status_code == 403
    resp = _client(world["admin"]).get("/admin/stats/overview?days=7")
    assert resp.status_code == 200 and resp.get_json()["days"] == 7


def test_an_unknown_range_falls_back_to_30_days(world):
    body = _client(world["admin"]).get("/admin/stats/overview?days=12").get_json()
    assert body["days"] == 30 and len(body["activity"]["daily"]) == 30 and len(body["usage"]["daily"]) == 30


def test_recordings_by_day_source_and_failure(world):
    today = date.today()
    noon = datetime.combine(today, datetime.min.time()) + timedelta(hours=12)
    with app.app_context():
        before = build_overview(7, today)
        _rec(world["admin"], noon, "upload", seconds=30)
        _rec(world["admin"], noon, "recording_session", seconds=90)
        _rec(world["admin"], noon, "merge", status="FAILED", title="Broken one")
        _rec(world["admin"], noon - timedelta(days=10), "upload")          # the range before
        db.session.commit()
        after = build_overview(7, today)
    d0, d1 = _day(before, today.isoformat()), _day(after, today.isoformat())
    assert (d1["upload"] - d0["upload"], d1["recording"] - d0["recording"], d1["merge"] - d0["merge"]) == (1, 1, 1)
    assert d1["failed"] - d0["failed"] == 1
    assert after["activity"]["created"] - before["activity"]["created"] == 3
    assert after["overview"]["recordings"]["previous"] - before["overview"]["recordings"]["previous"] == 1
    assert after["activity"]["failure_rate"] is not None
    assert after["failed_recent"][0]["title"] == "Broken one" and after["failed_recent"][0]["error"] == "boom"


def test_tokens_split_by_task_with_embeddings_apart(world):
    today = date.today()
    with app.app_context():
        before = build_overview(7, today)
        db.session.add_all([
            TokenUsage(user_id=world["admin"], date=today, operation_type="chat", total_tokens=500, cost=0.5,
                       request_count=2, model_name="m-chat"),
            TokenUsage(user_id=world["admin"], date=today, operation_type="embedding", total_tokens=9000, cost=0.1,
                       request_count=3, model_name="m-emb"),
            TranscriptionUsage(user_id=world["admin"], date=today, connector_type="asr_endpoint",
                               audio_duration_seconds=1200, estimated_cost=0.25, request_count=1, model_name="m-asr"),
        ])
        db.session.commit()
        after = build_overview(7, today)
    u0, u1 = _day(before, today.isoformat(), "usage"), _day(after, today.isoformat(), "usage")
    assert u1["operations"].get("chat", 0) - u0["operations"].get("chat", 0) == 500
    assert "embedding" not in u1["operations"]
    assert u1["embedding_tokens"] - u0["embedding_tokens"] == 9000
    assert u1["minutes"] - u0["minutes"] == 20
    models = {(m["kind"], m["model"]): m for m in after["usage"]["by_model"]}
    assert models[("llm", "m-chat")]["requests"] == 2 and models[("embedding", "m-emb")]["tokens"] == 9000
    assert models[("transcription", "m-asr")]["minutes"] == 20
    cur = after["overview"]
    assert round(cur["ai_cost"]["current"] - before["overview"]["ai_cost"]["current"], 6) == 0.85
    assert cur["ai_requests"]["current"] - before["overview"]["ai_requests"]["current"] == 2   # embeddings excluded
    month = after["usage"]["monthly"][-1]
    assert (month["year"], month["month"]) == (today.year, today.month) and len(after["usage"]["monthly"]) == 12
    row = next(u for u in after["users"] if u["id"] == world["admin"])
    assert row["tokens_month"] == 9500 and row["token_pct"] == 950.0
    assert row["minutes_month"] == 20 and row["minutes_budget"] == 10 and row["minutes_pct"] == 200.0
    assert row["cost_month"] == 0.85


def test_storage_leaves_out_removed_audio(world):
    with app.app_context():
        _rec(world["plain"], datetime.utcnow())
        r = Recording(user_id=world["plain"], title="gone", status="COMPLETED", file_size=5000,
                      audio_deleted_at=datetime.utcnow())
        db.session.add(r)
        db.session.commit()
        row = next(u for u in build_overview(30)["users"] if u["id"] == world["plain"])
    assert row["recordings"] == 2 and row["storage"] == 1000 and row["token_pct"] is None
