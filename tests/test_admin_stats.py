"""System Statistics numbers.

"This month" is the calendar month, also when it has no usage yet (it showed
the last month WITH usage), months without usage appear as zeros, "AI
requests this month" counts model calls (it was a hard-coded 0), and the
recording states and job queue are reported in full.

SHARED-DB: users, usage rows, jobs and recordings are removed afterwards.
"""

import os
import secrets
import sys
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, TokenUsage, TranscriptionUsage, User
from src.models.processing_job import ProcessingJob


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


def _last_month(today):
    first = today.replace(day=1)
    return (first - timedelta(days=1)).replace(day=15)


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        admin = User(username=f"st_{s}", email=f"st_{s}@local.test", password="x", is_admin=True)
        db.session.add(admin)
        db.session.commit()
        ids = {"admin": admin.id}
    yield ids
    with app.app_context():
        db.session.rollback()
        TokenUsage.query.filter_by(user_id=ids["admin"]).delete()
        TranscriptionUsage.query.filter_by(user_id=ids["admin"]).delete()
        ProcessingJob.query.filter_by(user_id=ids["admin"]).delete()
        Recording.query.filter_by(user_id=ids["admin"]).delete()
        User.query.filter_by(id=ids["admin"]).delete()
        db.session.commit()


def _client(uid):
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return c


def test_this_month_is_the_calendar_month(world):
    from src.services.token_tracking import token_tracker
    from src.services.transcription_tracking import transcription_tracker
    today = date.today()
    with app.app_context():
        before_tokens = token_tracker.get_monthly_stats(months=1)[-1]["llm_tokens"]
        before_seconds = transcription_tracker.get_monthly_stats(months=1)[-1]["seconds"]
        db.session.add(TokenUsage(user_id=world["admin"], date=_last_month(today), operation_type="summarization",
                                  total_tokens=5000, request_count=1, cost=0.5))
        db.session.add(TranscriptionUsage(user_id=world["admin"], date=_last_month(today), connector_type="asr_endpoint",
                                          audio_duration_seconds=6000, estimated_cost=0.1))
        db.session.commit()
        tokens = token_tracker.get_monthly_stats(months=1)
        seconds = transcription_tracker.get_monthly_stats(months=1)
    assert (tokens[-1]["year"], tokens[-1]["month"]) == (today.year, today.month)
    assert tokens[-1]["llm_tokens"] == before_tokens                 # last month's 5000 not counted
    assert (seconds[-1]["year"], seconds[-1]["month"]) == (today.year, today.month)
    assert seconds[-1]["seconds"] == before_seconds


def test_months_without_usage_are_zeros(world):
    from src.services.token_tracking import token_tracker
    from src.services.transcription_tracking import transcription_tracker
    with app.app_context():
        months = token_tracker.get_monthly_stats(months=12)
        tmonths = transcription_tracker.get_monthly_stats(months=12)
    assert len(months) == 12 and len(tmonths) == 12
    keys = [(m["year"], m["month"]) for m in months]
    assert keys == sorted(keys) and len(set(keys)) == 12
    assert keys[-1] == (date.today().year, date.today().month)
    for (y1, m1), (y2, m2) in zip(keys, keys[1:]):
        assert (y2 * 12 + m2) - (y1 * 12 + m1) == 1


def test_stats_report_requests_states_and_queue(world):
    with app.app_context():
        before = _client(world["admin"]).get("/admin/stats").get_json()
        db.session.add_all([
            TokenUsage(user_id=world["admin"], date=date.today(), operation_type="chat", total_tokens=10, request_count=3),
            TokenUsage(user_id=world["admin"], date=date.today(), operation_type="embedding", total_tokens=10, request_count=50),
        ])
        recs = [Recording(user_id=world["admin"], title=t, status=st, original_filename="a.wav")
                for t, st in (("a", "PROCESSING"), ("b", "SUMMARIZING"), ("c", "COMPLETED"))]
        recs[2].audio_deleted_at = datetime.utcnow()
        recs[2].is_archived = True
        db.session.add_all(recs)
        db.session.commit()
        db.session.add_all([
            ProcessingJob(user_id=world["admin"], recording_id=recs[0].id, job_type="transcribe", status="queued"),
            ProcessingJob(user_id=world["admin"], recording_id=recs[1].id, job_type="summarize", status="processing"),
            ProcessingJob(user_id=world["admin"], recording_id=recs[2].id, job_type="transcribe", status="failed",
                          completed_at=datetime.utcnow()),
        ])
        db.session.commit()
        after = _client(world["admin"]).get("/admin/stats").get_json()
    assert "total_queries" not in after
    assert after["ai_requests_month"] - before["ai_requests_month"] == 3          # embeddings not counted
    assert after["transcribing_recordings"] - before["transcribing_recordings"] == 1
    assert after["summarizing_recordings"] - before["summarizing_recordings"] == 1
    assert after["audio_removed_recordings"] - before["audio_removed_recordings"] == 1
    assert after["archived_recordings"] - before["archived_recordings"] == 1
    assert after["jobs"]["queued"] - before["jobs"]["queued"] == 1
    assert after["jobs"]["running"] - before["jobs"]["running"] == 1
    assert after["jobs"]["failed_7d"] - before["jobs"]["failed_7d"] == 1


@pytest.mark.parametrize("url,local", [
    ("", True), ("http://192.168.68.85:5010/v1", True), ("http://localhost:8080/v1", True),
    ("http://embed.lan/v1", True), ("https://openrouter.ai/api/v1", False), ("https://api.openai.com/v1", False),
])
def test_embeddings_are_local(url, local):
    from src.services import embeddings as emb
    with patch.object(emb, "USE_API_EMBEDDINGS", bool(url)), patch.object(emb, "EMBEDDING_BASE_URL", url):
        assert emb.embeddings_are_local() is local
