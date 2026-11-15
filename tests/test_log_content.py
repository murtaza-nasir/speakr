"""No user content in the default log (discussion #603, first phase).

At INFO and above, the log holds ids, lengths and counts. Titles, summaries,
raw model replies and Inquire queries are logged at DEBUG only. Each test runs
one of the three flows with the log captured from DEBUG upward and checks
that the content strings appear in DEBUG records and in no record of INFO or
higher.

SHARED-DB: users, recordings and chunks are removed afterwards.
"""

import json
import logging
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import ProcessingJob, Recording, TranscriptChunk, User
import src.tasks.processing as proc

TITLE = "Zanzibar Lighthouse Budget"
SUMMARY = "## Minutes\nThe heliotrope committee approved the marmalade invoice."
QUERY = "what did Ottoline say about the xylophone contract?"
ENRICHED = ["xylophone contract", "Ottoline"]
ANSWER = "Ottoline postponed the xylophone contract."


@pytest.fixture
def world():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"logc_{s}", email=f"logc_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title="Recording - a.mp3", status="PENDING", transcription="word " * 50,
                        original_filename="a.mp3", audio_path="local://a.mp3")
        db.session.add(rec)
        db.session.commit()
        chunk = TranscriptChunk(recording_id=rec.id, user_id=user.id, chunk_index=0,
                                content="Ottoline postponed the contract.", start_time=0.0, end_time=5.0)
        db.session.add(chunk)
        db.session.commit()
        ids = SimpleNamespace(user=user.id, rec=rec.id, chunk=chunk.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        TranscriptChunk.query.filter_by(user_id=ids.user).delete()
        ProcessingJob.query.filter_by(recording_id=ids.rec).delete()
        Recording.query.filter_by(user_id=ids.user).delete()
        User.query.filter_by(id=ids.user).delete()
        db.session.commit()


@pytest.fixture
def log(caplog):
    """The app log from DEBUG upward, whatever LOG_LEVEL the app started with."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=app.logger.name)
    return caplog


def _completion(content):
    message = SimpleNamespace(content=content, reasoning=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)


def _messages(log, at_least=None, below=None):
    return "\n".join(r.getMessage() for r in log.records
                     if (at_least is None or r.levelno >= at_least) and (below is None or r.levelno < below))


def _assert_only_at_debug(log, *contents):
    default_log = _messages(log, at_least=logging.INFO)
    debug_log = _messages(log, below=logging.INFO)
    for content in contents:
        assert content not in default_log
        # The check has teeth only when the content was logged at all.
        assert content in debug_log


def test_title_generation_logs_no_title_at_info(world, log):
    with app.app_context():
        with patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "call_llm_completion", return_value=_completion(TITLE)), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_title_task(app.app_context(), world.rec, will_auto_summarize=False)
        db.session.expire_all()
        assert db.session.get(Recording, world.rec).title == TITLE
    _assert_only_at_debug(log, TITLE)
    default_log = _messages(log, at_least=logging.INFO)
    assert f"AI title generated for recording {world.rec} ({len(TITLE)} characters)" in default_log
    assert f"Title set for recording {world.rec} ({len(TITLE)} characters)" in default_log


def test_user_provided_title_is_not_logged_at_info(world, log):
    with app.app_context():
        rec = db.session.get(Recording, world.rec)
        rec.title = TITLE
        db.session.commit()
        with patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "call_llm_completion") as call, \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_title_task(app.app_context(), world.rec, will_auto_summarize=False)
        call.assert_not_called()
    _assert_only_at_debug(log, TITLE)


def test_summary_generation_logs_no_summary_at_info(world, log):
    with app.app_context():
        with patch.object(proc, "client", MagicMock()), \
             patch.object(proc, "call_llm_completion", return_value=_completion(SUMMARY)), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
             patch("src.services.webhook_dispatch.emit_webhook_event"):
            proc.generate_summary_only_task(app.app_context(), world.rec)
        db.session.expire_all()
        assert db.session.get(Recording, world.rec).summary == SUMMARY
    _assert_only_at_debug(log, "heliotrope committee", "marmalade invoice")
    default_log = _messages(log, at_least=logging.INFO)
    assert f"Raw LLM response for recording {world.rec}: {len(SUMMARY)} characters" in default_log
    # One record per line: no multi-line reply in the default log.
    assert all("\n" not in r.getMessage() for r in log.records if r.levelno >= logging.INFO)


def _llm_reply(content):
    reply = MagicMock()
    reply.choices = [MagicMock()]
    reply.choices[0].message.content = content
    return reply


def _stream(texts):
    chunks = []
    for text in texts:
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = text
        chunks.append(chunk)
    return iter(chunks)


def test_inquire_query_is_not_logged_at_info(world, log):
    from sqlalchemy.orm import joinedload

    def search(*args, **kwargs):
        chunk = (db.session.query(TranscriptChunk).options(joinedload(TranscriptChunk.recording))
                 .filter_by(id=world.chunk).first())
        return [(chunk, 0.8)]

    def llm(messages, **kwargs):
        if kwargs.get("operation_type") == "query_routing":
            return _llm_reply("RAG")
        return _llm_reply(json.dumps(ENRICHED))

    prev = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        client = app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = str(world.user)
            s["_fresh"] = True
        with patch("src.api.inquire.ENABLE_INQUIRE_MODE", True), \
             patch("src.api.inquire.client", MagicMock()), \
             patch("src.api.inquire.EMBEDDINGS_AVAILABLE", True), \
             patch("src.api.inquire.call_llm_completion", side_effect=llm), \
             patch("src.api.inquire.semantic_search_chunks", side_effect=search), \
             patch("src.api.inquire.call_chat_completion", return_value=_stream([ANSWER])):
            resp = client.post("/api/inquire/chat", json={"message": QUERY})
            assert resp.status_code == 200
            body = resp.get_data(as_text=True)
            resp.close()
    finally:
        app.config["WTF_CSRF_ENABLED"] = prev
    assert ANSWER in body
    # The query, the search terms derived from it and the answer.
    _assert_only_at_debug(log, QUERY, "xylophone contract")
    default_log = _messages(log, at_least=logging.INFO)
    assert "xylophone" not in default_log and "Ottoline" not in default_log
    assert f"Query: {len(QUERY)} characters" in default_log
    assert "Router decision: RAG" in default_log
