"""An incognito request logs nothing but metadata, at any log level.

The three incognito routes (upload, chat, summary) run here with the log
captured from DEBUG upward. The transcription service and the LLM are fakes
behind httpx.MockTransport, so the real connector, the real LLM service and
the real openai and httpx clients write their usual records. Every string a
user or a provider supplies is a marker, and no marker may appear in any log
record: not in the message, the arguments, the traceback or any attribute.
The same calls outside an incognito request still log their content at DEBUG.

An incognito request is counted against the monthly budgets, so it writes the
same usage rows as an ordinary request (token_usage, transcription_usage).
Those rows hold numbers, the operation, the connector and the model name. No
other row of any table is added or changed, and no file is left in the
temporary folder. A user over a budget is refused before anything is sent.

SHARED-DB: the user is removed afterwards.
"""

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import uuid
import wave
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import TokenUsage, TranscriptionUsage, User
import src.api.recordings as rec_api
import src.services.llm as llm
import src.tasks.processing as proc
from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
from src.services.transcription.exceptions import ProviderError
from src.utils import incognito_logging as il

app.config["WTF_CSRF_ENABLED"] = False

ASR_BASE = "http://asr.test:9000"
LLM_BASE = "http://llm.test/v1"

TRANSCRIPT = "quokka-transcript-marker the patient reported mild vertigo"
SPEAKER = "SPEAKER_00"
TITLE = "Narwhal-title-marker"
SUMMARY = "## Wombat-summary-marker\nFollow-up in two weeks."
CHAT_REPLY = "axolotl-reply-marker"
CHAT_MESSAGE = "what did the okapi-message-marker say?"
HISTORY = "earlier pangolin-history-marker answer"
NOTES = "tapir-notes-marker"
PARTICIPANTS = "Dr. Ibex-participant-marker"
FILENAME = "capybara-filename-marker session 3.wav"
HOTWORDS = "manatee-hotword-marker"
INITIAL_PROMPT = "lemur-initial-prompt-marker"
USER_PROMPT = "Summarize with the echidna-user-prompt-marker layout."
ASR_ERROR = "kakapo-asr-error-marker: could not decode 'the patient reported'"
LLM_ERROR = "numbat-llm-error-marker: prompt quoted back, the patient reported"

MARKERS = [
    "quokka-transcript-marker", "patient reported", "Narwhal-title-marker", "Wombat-summary-marker",
    "axolotl-reply-marker", "okapi-message-marker", "pangolin-history-marker", "tapir-notes-marker",
    "Ibex-participant-marker", "capybara-filename-marker", "manatee-hotword-marker",
    "lemur-initial-prompt-marker", "echidna-user-prompt-marker", "kakapo-asr-error-marker",
    "numbat-llm-error-marker",
]


# --- fakes -----------------------------------------------------------------

class FakeASR:
    """A transcription service: synchronous /asr, or the WhisperX jobs API."""

    def __init__(self, jobs=False, fail=False):
        self.jobs, self.fail = jobs, fail
        self.requests = []
        self.deleted = []

    def result(self):
        return {"language": "en", "segments": [
            {"start": 0.0, "end": 2.0, "text": TRANSCRIPT, "speaker": SPEAKER}]}

    def handle(self, request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if path in ("/health", "/"):
            body = {"status": "ok"}
            if self.jobs:
                body["capabilities"] = {"async_jobs": True, "jobs_api_version": 1, "idempotency_key": True}
            return httpx.Response(200, json=body)
        if path == "/asr":
            if self.fail:
                return httpx.Response(500, json={"detail": ASR_ERROR})
            return httpx.Response(200, json=self.result())
        if path == "/jobs" and request.method == "POST":
            return httpx.Response(202, json={"job_id": "job0001", "status": "queued"})
        if path == "/jobs/job0001" and request.method == "GET":
            if self.fail:
                return httpx.Response(200, json={"job_id": "job0001", "status": "error",
                                                 "error": {"code": "decode", "message": ASR_ERROR}})
            return httpx.Response(200, json={"job_id": "job0001", "status": "done", "poll_after_seconds": 0})
        if path == "/jobs/job0001/result":
            return httpx.Response(200, json=self.result())
        if path == "/jobs/job0001" and request.method == "DELETE":
            self.deleted.append("job0001")
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"detail": "not found"})

    def connector(self):
        connector = ASREndpointConnector({"base_url": ASR_BASE, "diarize": True})
        real = httpx.Client
        connector._http_client = lambda **kw: real(transport=httpx.MockTransport(self.handle), **kw)
        return connector


class FakeLLM:
    """An OpenAI-compatible chat completions endpoint."""

    def __init__(self, replies=(), fail=False, refusal=None):
        self.replies = list(replies)
        self.fail = fail
        self.refusal = refusal
        self.bodies = []

    def handle(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        if self.fail:
            return httpx.Response(400, json={"error": {"message": LLM_ERROR, "type": "invalid_request_error",
                                                       "code": "context_length_exceeded"}})
        text = self.replies.pop(0)
        if body.get("stream"):
            chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "fake",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                  "finish_reason": None}]}
            payload = f"data: {json.dumps(chunk)}\n\n"
            if (body.get("stream_options") or {}).get("include_usage"):
                usage = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "fake",
                         "choices": [],
                         "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}}
                payload += f"data: {json.dumps(usage)}\n\n"
            payload += "data: [DONE]\n\n"
            return httpx.Response(200, content=payload.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 1, "model": "fake",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text, "refusal": self.refusal},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def client(self):
        return OpenAI(api_key="test-key", base_url=LLM_BASE, max_retries=0,
                      http_client=httpx.Client(transport=httpx.MockTransport(self.handle)))


# --- fixtures and helpers ---------------------------------------------------

@pytest.fixture
def user_id():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"inclog_{s}", email=f"inclog_{s}@local.test", password="x")
        user.summary_prompt = USER_PROMPT
        db.session.add(user)
        db.session.commit()
        uid = user.id
    yield uid
    with app.app_context():
        db.session.rollback()
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


@pytest.fixture
def client(user_id):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
    return c


@pytest.fixture
def log(caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=app.logger.name)
    for name in ("openai", "httpx", "httpcore"):
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


@pytest.fixture
def temp_folder(tmp_path, monkeypatch):
    """The temporary folder of the request, so that leftovers can be counted.

    Incognito files go to a folder of their own (INCOGNITO_TEMP_DIR); nothing
    of the request may be written to the temporary folder of the system, and
    the incognito folder must be empty again after the request.
    """
    folder = tmp_path / "tmp"
    folder.mkdir()
    incognito = tmp_path / "incognito"
    monkeypatch.setenv("INCOGNITO_TEMP_DIR", str(incognito))
    with patch.object(tempfile, "tempdir", str(folder)):
        yield folder
    assert not incognito.exists() or list(incognito.iterdir()) == []


def _wav():
    data = io.BytesIO()
    with wave.open(data, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 16000 * 2)
    data.seek(0)
    return data


def _everything(log):
    """Every text a handler or formatter could reach in the captured records."""
    formatter = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    parts = []
    for record in log.records:
        parts.append(formatter.format(record))
        parts.append(repr(record.__dict__))
    return "\n".join(parts)


def _assert_no_content(log, *more):
    text = _everything(log)
    for marker in list(MARKERS) + list(more):
        assert marker not in text, f"{marker!r} reached the log"
    # The check has teeth only when the shared code did try to log.
    assert any("log record withheld" in r.getMessage() for r in log.records)
    assert not il.incognito_active()


def _database():
    """Every row of every table, so that a changed row counts as much as a new one.

    One column is left out: user.last_seen_at, the time of the last use of the
    account. An incognito request updates it like any other request, and it
    says nothing about the request (tests/test_admin_last_active.py checks
    that the time is the only value stored).
    """
    from sqlalchemy import select
    with app.app_context():
        db.session.rollback()
        # Audit rows are written by a thread; the snapshot waits for them.
        from src.services import audit
        audit.flush()
        snapshot = {}
        for table in db.metadata.tables.values():
            columns = [c for c in table.columns if (table.name, c.name) != ('user', 'last_seen_at')]
            snapshot[table.name] = sorted(
                repr(tuple(row)) for row in db.session.execute(select(*columns)))
        return snapshot


USAGE_TABLES = ("token_usage", "transcription_usage")


def _without_usage(database):
    # audit_event is compared separately (_assert_audit_rows).
    return {name: rows for name, rows in database.items()
            if name not in USAGE_TABLES and name != 'audit_event'}


def _assert_audit_rows(before, after, user_id, more=()):
    """An incognito request adds audit rows of one kind only: incognito.used,
    with the account, the time and the outcome and nothing else."""
    from src.models import AuditEvent
    known = set(before['audit_event'])
    assert known <= set(after['audit_event'])
    with app.app_context():
        new = [row for row in AuditEvent.query.order_by(AuditEvent.id)
               if not any(entry.startswith(f'({row.id},') for entry in known)]
        for row in new:
            assert row.event == 'incognito.used', row.event
            assert row.actor_user_id is not None and user_id in (None, row.actor_user_id)
            assert row.outcome in ('success', 'denied', 'failed')
            assert (row.recording_id, row.recording_owner_id, row.target_user_id, row.group_id,
                    row.details, row.ip, row.user_agent, row.actor_token_id) == (None,) * 8
    text = "\n".join(sorted(set(after['audit_event']) - known))
    for marker in list(MARKERS) + list(more):
        assert marker not in text, f"{marker!r} reached audit_event"
    return new


def _assert_only_the_audit_row(before, outcome):
    """The database is as before, except for one incognito.used row."""
    after = _database()
    assert {k: v for k, v in after.items() if k != 'audit_event'} == \
        {k: v for k, v in before.items() if k != 'audit_event'}
    new = _assert_audit_rows(before, after, None)
    assert [row.outcome for row in new] == [outcome]


def _usage(user_id):
    """The usage rows of the user, as plain values."""
    with app.app_context():
        db.session.rollback()
        tokens = {u.operation_type: (u.prompt_tokens, u.completion_tokens, u.total_tokens, u.request_count,
                                     u.model_name)
                  for u in TokenUsage.query.filter_by(user_id=user_id)}
        audio = {u.connector_type: (u.audio_duration_seconds, u.request_count, u.model_name)
                 for u in TranscriptionUsage.query.filter_by(user_id=user_id)}
    return tokens, audio


def _assert_only_usage_rows_changed(before, user_id, more=(), audit_rows=0):
    """Nothing but usage rows of this user changed, and those hold no content.

    audit_rows: 1 for an upload (its incognito.used row), 0 for chat and summary."""
    after = _database()
    assert _without_usage(after) == _without_usage(before)
    assert len(_assert_audit_rows(before, after, user_id, more)) == audit_rows
    with app.app_context():
        columns = {name: [c.name for c in db.metadata.tables[name].columns] for name in USAGE_TABLES}
        for name in USAGE_TABLES:
            table = db.metadata.tables[name]
            new = set(after[name]) - set(before[name])
            for row in db.session.execute(table.select().where(table.c.user_id != user_id)):
                assert repr(tuple(row)) in before[name], f"a {name} row of another user changed"
            text = "\n".join(sorted(new))
            for marker in list(MARKERS) + list(more):
                assert marker not in text, f"{marker!r} reached {name}"
    # The usage tables have no column for a title, a filename, content or a recording.
    for name, names in columns.items():
        for column in names:
            assert not any(word in column for word in ("title", "filename", "text", "content", "recording")), \
                f"{name}.{column}"


def _set_budget(user_id, tokens=None, seconds=None):
    with app.app_context():
        user = db.session.get(User, user_id)
        user.monthly_token_budget = tokens
        user.monthly_transcription_budget = seconds
        db.session.commit()


def _llm(fake):
    c = fake.client()
    return [patch.object(llm, "client", c), patch.object(llm, "chat_client", c),
            patch.object(llm, "TEXT_MODEL_NAME", "fake-model"),
            # The chat route records the resolved chat model, which inherits
            # the text model when the chat role has no configuration.
            patch.object(llm, "CHAT_MODEL_NAME", "fake-model"),
            patch.object(proc.llm_config, "is_configured", return_value=True),
            ]


class _All:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


def _world(asr, fake_llm, chunked=False):
    connector = asr.connector()
    registry = type("Registry", (), {"get_active_connector": lambda self: connector})()
    patches = _llm(fake_llm) + [
        patch.object(rec_api, "ENABLE_INCOGNITO_MODE", True),
        patch("src.services.transcription.get_registry", return_value=registry),
        patch("src.services.transcription.connectors.asr_endpoint._sleep", lambda s: None),
    ]
    if chunked:
        def create_chunks(path, temp_dir, specs=None):
            chunks = []
            for i in range(2):
                target = os.path.join(temp_dir, f"chunk_{i}.mp3")
                shutil.copy(path, target)
                chunks.append({"index": i, "start_time": i * 1.0, "end_time": i * 1.0 + 1.0, "duration": 1.0,
                               "size_mb": 0.1, "filename": f"chunk_{i}.mp3", "path": target})
            return chunks
        patches += [patch.object(proc.chunking_service, "needs_chunking", return_value=True),
                    patch.object(proc.chunking_service, "create_chunks", side_effect=create_chunks),
                    patch("src.audio_chunking.extract_speaker_samples", return_value=[])]
    else:
        patches += [patch.object(proc.chunking_service, "needs_chunking", return_value=False)]
    return _All(patches)


def _upload(client, auto_summarize=True):
    return client.post("/api/recordings/incognito", data={
        "file": (_wav(), FILENAME), "language": "en", "hotwords": HOTWORDS, "initial_prompt": INITIAL_PROMPT,
        "auto_summarize": "true" if auto_summarize else "false",
    }, content_type="multipart/form-data")


# --- the upload route -------------------------------------------------------

@pytest.mark.parametrize("jobs", [False, True], ids=["synchronous", "whisperx-jobs"])
def test_upload_logs_metadata_only_and_stores_nothing(client, user_id, log, temp_folder, jobs):
    asr, fake_llm = FakeASR(jobs=jobs), FakeLLM([TITLE, SUMMARY])
    before = _database()
    with _world(asr, fake_llm):
        r = _upload(client)
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    # The fakes were reached and the content came back to the browser.
    assert "quokka-transcript-marker" in body["transcription"]
    assert body["title"] == TITLE and body["summary"] == SUMMARY
    assert any("quokka-transcript-marker" in json.dumps(b) for b in fake_llm.bodies)
    assert any("echidna-user-prompt-marker" in json.dumps(b) for b in fake_llm.bodies)
    if jobs:
        assert ("POST", "/jobs") in asr.requests and asr.deleted == ["job0001"]
    else:
        assert ("POST", "/asr") in asr.requests

    _assert_no_content(log)
    messages = [r.getMessage() for r in log.records]
    assert any(m.startswith(f"[Incognito] Processing request from user {user_id}: ext=.wav") for m in messages)
    assert any(m.startswith("[Incognito] Summary generated:") for m in messages)
    # Records of the shared code and of the libraries were created and replaced.
    withheld = {r.name for r in log.records if "log record withheld" in r.getMessage()}
    assert "src.services.transcription.connectors.asr_endpoint" in withheld
    assert "src.services.llm" in withheld
    assert any(name.startswith(("openai", "httpx", "httpcore")) for name in withheld)

    _assert_only_usage_rows_changed(before, user_id, more=(FILENAME,), audit_rows=1)
    tokens, audio = _usage(user_id)
    # One title call and one summary call of the fake (10 + 5 tokens each).
    assert tokens == {"title_generation": (10, 5, 15, 1, "fake-model"),
                      "summarization": (10, 5, 15, 1, "fake-model")}
    # Two seconds of audio, counted for the connector an ordinary upload names.
    assert list(audio) == ["asr_endpoint"]
    assert audio["asr_endpoint"][0] == 2 and audio["asr_endpoint"][1] == 1
    assert list(temp_folder.iterdir()) == []


def test_chunked_upload_logs_metadata_only(client, user_id, log, temp_folder):
    asr, fake_llm = FakeASR(), FakeLLM([TITLE])
    before = _database()
    with _world(asr, fake_llm, chunked=True):
        r = _upload(client, auto_summarize=False)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert asr.requests.count(("POST", "/asr")) == 2
    assert "quokka-transcript-marker" in r.get_json()["transcription"]
    _assert_no_content(log)
    _assert_only_usage_rows_changed(before, user_id, audit_rows=1)
    tokens, audio = _usage(user_id)
    assert set(tokens) == {"title_generation"} and audio["asr_endpoint"][1] == 1
    assert list(temp_folder.iterdir()) == []


@pytest.mark.parametrize("jobs", [False, True], ids=["synchronous", "whisperx-jobs"])
def test_failing_transcription_service_is_logged_as_class_and_status(client, log, temp_folder, jobs):
    asr = FakeASR(jobs=jobs, fail=True)
    before = _database()
    with _world(asr, FakeLLM()):
        r = _upload(client)
    assert r.status_code == 500
    answer = r.get_data(as_text=True)
    for marker in MARKERS:
        assert marker not in answer
    _assert_no_content(log)
    failed = [m for m in (r.getMessage() for r in log.records) if m.startswith("[Incognito] Transcription failed:")]
    assert failed and "length=" in failed[0]
    if not jobs:
        assert "ProviderError provider=asr_endpoint status=500" in failed[0]
    else:
        assert asr.deleted == ["job0001"]
    _assert_only_the_audit_row(before, 'failed')
    assert list(temp_folder.iterdir()) == []


def test_failing_llm_during_upload_logs_no_error_body(client, log, temp_folder):
    with _world(FakeASR(), FakeLLM(fail=True)):
        r = _upload(client)
    assert r.status_code == 200
    body = r.get_json()
    assert body["title"] == "Incognito Recording" and body["summary"] is None
    _assert_no_content(log)
    messages = [r.getMessage() for r in log.records]
    assert any(m.startswith("[Incognito] Title generation failed: BadRequestError status=400 "
                            "code=context_length_exceeded length=") for m in messages)
    assert list(temp_folder.iterdir()) == []


def test_unexpected_error_in_the_view_is_answered_without_its_text(client, log, temp_folder):
    with _world(FakeASR(), FakeLLM()), \
         patch("src.tasks.processing.transcribe_incognito", side_effect=RuntimeError(ASR_ERROR)):
        r = _upload(client)
    assert r.status_code == 500
    assert "kakapo" not in r.get_data(as_text=True)
    text = _everything(log)
    for marker in MARKERS:
        assert marker not in text
    assert any(r.getMessage().startswith("[Incognito] Error during processing: RuntimeError length=")
               for r in log.records)
    assert list(temp_folder.iterdir()) == []


# --- chat and summary -------------------------------------------------------

CHAT_BODY = {"transcription": json.dumps([{"speaker": SPEAKER, "sentence": TRANSCRIPT}]),
             "message": CHAT_MESSAGE, "participants": PARTICIPANTS, "notes": NOTES,
             "message_history": [{"role": "user", "content": HISTORY}, {"role": "assistant", "content": HISTORY}]}


def test_chat_logs_metadata_only_while_the_answer_streams(client, user_id, log):
    fake_llm = FakeLLM([CHAT_REPLY])
    before = _database()
    with _world(FakeASR(), fake_llm):
        r = client.post("/api/recordings/incognito/chat", json=CHAT_BODY)
        answer = r.get_data(as_text=True)
    assert r.status_code == 200
    assert CHAT_REPLY in answer and "data: [DONE]" in answer
    sent = json.dumps(fake_llm.bodies[0])
    assert "okapi-message-marker" in sent and "tapir-notes-marker" in sent
    _assert_no_content(log)
    assert any(r.getMessage().startswith(f"[Incognito Chat] User {user_id} sending message (")
               for r in log.records)
    # The library records of the streamed answer are created after the view returned.
    assert any("log record withheld" in r.getMessage() and r.name.startswith(("openai", "httpx", "httpcore"))
               for r in log.records)
    _assert_only_usage_rows_changed(before, user_id)
    tokens, audio = _usage(user_id)
    assert tokens == {"chat": (20, 7, 27, 1, "fake-model")} and audio == {}


def test_chat_with_a_failing_llm_answers_a_fixed_message(client, log):
    with _world(FakeASR(), FakeLLM(fail=True)):
        r = client.post("/api/recordings/incognito/chat", json=CHAT_BODY)
        answer = r.get_data(as_text=True)
    assert r.status_code == 200
    assert '"error"' in answer
    for marker in MARKERS:
        assert marker not in answer
    _assert_no_content(log)
    assert any(r.getMessage().startswith("[Incognito Chat] Error during streaming: BadRequestError status=400")
               for r in log.records)


def test_summary_logs_metadata_only(client, user_id, log):
    fake_llm = FakeLLM([SUMMARY])
    before = _database()
    with _world(FakeASR(), fake_llm):
        r = client.post("/api/recordings/incognito/summary", json={"transcription": TRANSCRIPT})
    assert r.status_code == 200
    assert r.get_json()["summary"] == SUMMARY
    assert "echidna-user-prompt-marker" in json.dumps(fake_llm.bodies[0])
    _assert_no_content(log)
    assert any(r.getMessage().startswith(f"[Incognito Summary] User {user_id} requesting summary generation")
               for r in log.records)
    _assert_only_usage_rows_changed(before, user_id)
    tokens, audio = _usage(user_id)
    assert tokens == {"summarization": (10, 5, 15, 1, "fake-model")} and audio == {}


def test_summary_with_a_failing_llm_answers_without_the_error_body(client, log):
    with _world(FakeASR(), FakeLLM(fail=True)):
        r = client.post("/api/recordings/incognito/summary", json={"transcription": TRANSCRIPT})
    assert r.status_code == 503
    answer = r.get_data(as_text=True)
    for marker in MARKERS:
        assert marker not in answer
    _assert_no_content(log)


def test_summary_error_answer_has_no_exception_text(client, log):
    with _world(FakeASR(), FakeLLM()), \
         patch("src.tasks.processing.generate_incognito_summary", side_effect=RuntimeError(LLM_ERROR)):
        r = client.post("/api/recordings/incognito/summary", json={"transcription": TRANSCRIPT})
    assert r.status_code == 500
    assert "numbat" not in r.get_data(as_text=True)
    assert "numbat" not in _everything(log)


# --- budgets -----------------------------------------------------------------

def _use_tokens(user_id, total):
    with app.app_context():
        from src.services.token_tracking import token_tracker
        token_tracker.record_usage(user_id, "summarization", total, 0, total, model_name="earlier")


def test_upload_is_refused_over_the_transcription_budget_before_anything_is_sent(client, user_id, log, temp_folder):
    _set_budget(user_id, seconds=60)
    with app.app_context():
        from src.services.transcription_tracking import transcription_tracker
        transcription_tracker.record_usage(user_id, "asr_endpoint", 61, model_name="earlier")
    asr, fake_llm = FakeASR(), FakeLLM([TITLE, SUMMARY])
    before = _database()
    with _world(asr, fake_llm):
        r = _upload(client)
    assert r.status_code == 403
    body = r.get_json()
    assert body["budget_exceeded"] is True and body["incognito"] is True and body["error"]
    assert ("POST", "/asr") not in asr.requests and fake_llm.bodies == []
    _assert_only_the_audit_row(before, 'denied')
    assert list(temp_folder.iterdir()) == []
    text = _everything(log)
    for marker in MARKERS:
        assert marker not in text


def test_upload_over_the_token_budget_answers_the_transcript_without_title_or_summary(client, user_id, log, temp_folder):
    _set_budget(user_id, tokens=100)
    _use_tokens(user_id, 100)
    asr, fake_llm = FakeASR(), FakeLLM([TITLE, SUMMARY])
    with _world(asr, fake_llm):
        r = _upload(client)
    assert r.status_code == 200
    body = r.get_json()
    assert "quokka-transcript-marker" in body["transcription"]
    assert body["title"] == "Incognito Recording" and body["summary"] is None
    assert body["summary_budget_exceeded"] is True and body["summary_error"]
    assert fake_llm.bodies == [], "no text was sent to the LLM"
    _assert_no_content(log)
    assert list(temp_folder.iterdir()) == []


def test_chat_is_refused_over_the_token_budget(client, user_id, log):
    _set_budget(user_id, tokens=100)
    _use_tokens(user_id, 100)
    fake_llm = FakeLLM([CHAT_REPLY])
    before = _database()
    with _world(FakeASR(), fake_llm):
        r = client.post("/api/recordings/incognito/chat", json=CHAT_BODY)
        answer = r.get_data(as_text=True)
    assert '"budget_exceeded": true' in answer and CHAT_REPLY not in answer
    assert fake_llm.bodies == []
    assert _database() == before
    text = _everything(log)
    for marker in MARKERS:
        assert marker not in text


def test_summary_is_refused_over_the_token_budget(client, user_id, log):
    _set_budget(user_id, tokens=100)
    _use_tokens(user_id, 100)
    fake_llm = FakeLLM([SUMMARY])
    before = _database()
    with _world(FakeASR(), fake_llm):
        r = client.post("/api/recordings/incognito/summary", json={"transcription": TRANSCRIPT})
    assert r.status_code == 403 and r.get_json()["budget_exceeded"] is True
    assert fake_llm.bodies == []
    assert _database() == before


def test_incognito_usage_counts_toward_the_budget_of_later_requests(client, user_id, log):
    """Two summaries of 15 tokens against a budget of 20: the second is refused."""
    _set_budget(user_id, tokens=20)
    fake_llm = FakeLLM([SUMMARY, SUMMARY, SUMMARY])
    with _world(FakeASR(), fake_llm):
        first = client.post("/api/recordings/incognito/summary", json={"transcription": TRANSCRIPT})
        second = client.post("/api/recordings/incognito/summary", json={"transcription": TRANSCRIPT})
        third = client.post("/api/recordings/incognito/summary", json={"transcription": TRANSCRIPT})
    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 403)
    assert len(fake_llm.bodies) == 2


# --- ordinary requests keep their DEBUG log ---------------------------------

def test_the_same_calls_outside_incognito_still_log_content(log):
    refusal = "I cannot summarize the echidna-user-prompt-marker text."
    fake_llm = FakeLLM([""], refusal=refusal)
    with _All(_llm(fake_llm)):
        llm.call_llm_completion([{"role": "user", "content": USER_PROMPT}])
    connector = FakeASR(fail=True).connector()
    with app.app_context(), pytest.raises(ProviderError):
        connector.transcribe(TranscriptionRequest(audio_file=_wav(), filename="visit.wav", language="en"))
    debug = "\n".join(r.getMessage() for r in log.records if r.levelno == logging.DEBUG)
    assert f"Refusal: {refusal}" in debug                 # model output, at DEBUG as before
    assert "kakapo-asr-error-marker" in _everything(log)  # the connector's record of the error body
    assert not any("log record withheld" in r.getMessage() for r in log.records)


# --- the mechanism ----------------------------------------------------------

def test_every_incognito_route_is_marked():
    rules = [rule for rule in app.url_map.iter_rules() if "incognito" in rule.rule]
    assert len(rules) >= 3
    for rule in rules:
        assert getattr(app.view_functions[rule.endpoint], "incognito_route", False), rule.rule


def test_record_factory_replaces_message_arguments_and_traceback(log):
    plain = logging.getLogger("src.some.shared.module")
    with il.incognito_context():
        try:
            raise ProviderError(ASR_ERROR, provider="asr_endpoint", status_code=502)
        except ProviderError:
            plain.exception("failed for %s: %s", FILENAME, TRANSCRIPT, stack_info=True)
        il.incognito_log.info("[Incognito] metadata line")
    plain.debug("ordinary %s", TRANSCRIPT)
    first, second, third = log.records[-3:]
    assert first.getMessage().startswith("[Incognito] log record withheld (")
    assert first.getMessage().endswith("[ProviderError provider=asr_endpoint status=502 length=%d]"
                                       % len(str(ProviderError(ASR_ERROR, provider="asr_endpoint", status_code=502))))
    assert first.exc_info is None and first.stack_info is None and not first.args
    assert first.levelno == logging.ERROR and first.name == "src.some.shared.module"
    assert second.getMessage() == "[Incognito] metadata line"
    assert TRANSCRIPT in third.getMessage()


def test_describe_error_never_holds_the_message():
    class Odd(Exception):
        code = "has spaces and 'quotes'"
    assert il.describe_error(Odd(TRANSCRIPT)) == f"Odd length={len(TRANSCRIPT)}"
    assert il.describe_error(ValueError()) == "ValueError length=0"
    assert il.short_code("en") == "en" and il.short_code(TRANSCRIPT) == "other" and il.short_code(None) == ""


def test_context_is_carried_into_a_thread_only_when_asked(log):
    seen = {}

    def work(key):
        seen[key] = il.incognito_active()

    with il.incognito_context():
        plain = threading.Thread(target=work, args=("plain",))
        carried = threading.Thread(target=il.carry_context(work), args=("carried",))
        plain.start(), carried.start()
        plain.join(), carried.join()
    assert seen == {"plain": False, "carried": True}
    assert not il.incognito_active()
