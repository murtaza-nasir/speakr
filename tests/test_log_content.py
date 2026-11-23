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
import re
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
        with patch.object(proc.llm_config, "is_configured", return_value=True), \
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
        with patch.object(proc.llm_config, "is_configured", return_value=True), \
             patch.object(proc, "call_llm_completion") as call, \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False):
            proc.generate_title_task(app.app_context(), world.rec, will_auto_summarize=False)
        call.assert_not_called()
    _assert_only_at_debug(log, TITLE)


def test_summary_generation_logs_no_summary_at_info(world, log):
    with app.app_context():
        with patch.object(proc.llm_config, "is_configured", return_value=True), \
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
             patch("src.services.llm_config.is_configured", return_value=True), \
             patch("src.api.inquire.EMBEDDINGS_AVAILABLE", True), \
             patch("src.api.inquire.semantic_search_chunks", side_effect=search), \
             patch("src.api.inquire.call_chat_completion",
                   side_effect=lambda messages, **kwargs: (
                       llm(messages, **kwargs)
                       if kwargs.get("operation_type") in ("query_routing", "query_enrichment")
                       else _stream([ANSWER]))):
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


# --- transcription request parameters ---------------------------------------

HOTWORDS = "Ottoline, xylophone-hotword"
INITIAL_PROMPT = "Minutes of the heliotrope-prompt committee."


def _asr_connector(jobs):
    import httpx
    from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector

    def handle(request):
        path = request.url.path
        if path in ("/health", "/"):
            capabilities = {"async_jobs": True, "jobs_api_version": 1} if jobs else None
            return httpx.Response(200, json={"status": "ok", "capabilities": capabilities})
        if path == "/jobs" and request.method == "POST":
            return httpx.Response(202, json={"job_id": "job0001", "status": "queued"})
        if path == "/jobs/job0001" and request.method == "GET":
            return httpx.Response(200, json={"job_id": "job0001", "status": "done", "poll_after_seconds": 0})
        return httpx.Response(200, json={"language": "en", "segments": [
            {"start": 0.0, "end": 1.0, "text": "hello", "speaker": "SPEAKER_00"}]})

    connector = ASREndpointConnector({"base_url": "http://asr.test:9000"})
    real = httpx.Client
    connector._http_client = lambda **kw: real(transport=httpx.MockTransport(handle), **kw)
    return connector


@pytest.mark.parametrize("jobs", [False, True], ids=["synchronous", "jobs"])
def test_initial_prompt_and_hotwords_are_not_logged_at_info(log, jobs):
    import io
    from src.services.transcription.base import TranscriptionRequest
    log.set_level(logging.DEBUG, logger="httpx")
    connector = _asr_connector(jobs)
    with app.app_context(), patch("src.services.transcription.connectors.asr_endpoint._sleep", lambda s: None):
        connector.transcribe(TranscriptionRequest(audio_file=io.BytesIO(b"audio"), filename="a.mp3", language="en",
                                                  prompt=INITIAL_PROMPT, hotwords=HOTWORDS))
    _assert_only_at_debug(log, "xylophone-hotword", "heliotrope-prompt")
    default_log = _messages(log, at_least=logging.INFO)
    assert "xylophone" not in default_log and "heliotrope" not in default_log
    assert f"'hotwords': '<{len(HOTWORDS)} characters>'" in default_log
    assert f"'initial_prompt': '<{len(INITIAL_PROMPT)} characters>'" in default_log
    # httpx names each request at INFO: the URL is there, its query string is not.
    requests_logged = [r.getMessage() for r in log.records if r.name == "httpx"]
    assert any("http://asr.test:9000/" in m for m in requests_logged)
    assert not any("?" in m for m in requests_logged)


def test_loggable_params_keeps_everything_but_user_text():
    from src.utils.log_filters import loggable_params
    params = {"language": "de", "min_speakers": 2, "hotwords": HOTWORDS, "initial_prompt": None}
    assert loggable_params(params) == {"language": "de", "min_speakers": 2,
                                       "hotwords": f"<{len(HOTWORDS)} characters>", "initial_prompt": None}
    assert params["hotwords"] == HOTWORDS
    assert loggable_params(None) is None


# --- provider error text (privacy review B1) ----------------------------------
#
# The error body of a transcription service or an LLM provider can quote what
# was sent to it. At INFO and above the log has the class, the status and the
# length; the text is at DEBUG. The text stored on the job and the recording,
# and sent by webhook and email, has the provider's short message without
# what it repeats of the request.

ECHO_PROMPT = "Minutes of the heliotrope-prompt committee."
ECHO_FILENAME = "ocelot-filename-marker intake.mp3"
ECHO_TRANSCRIPT = "the ptarmigan-transcript-marker patient reported mild vertigo after lunch"
PROVIDER_MESSAGE = "Invalid model size"
ASR_BODY = {"detail": f"{PROVIDER_MESSAGE} for '{ECHO_FILENAME}' with prompt '{ECHO_PROMPT}'",
            "request": {"initial_prompt": ECHO_PROMPT, "file": ECHO_FILENAME}}
LLM_MESSAGE = (f"This model's maximum context length is 8192 tokens. Your input started with: "
               f"'{ECHO_TRANSCRIPT}'")
B1_MARKERS = ["heliotrope-prompt", "ocelot-filename-marker", "ptarmigan-transcript-marker", "patient reported"]


def _failing_asr_connector():
    import httpx
    from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector

    def handle(request):
        if request.url.path in ("/health", "/"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(400, json=ASR_BODY)

    connector = ASREndpointConnector({"base_url": "http://asr.test:9000"})
    real = httpx.Client
    connector._http_client = lambda **kw: real(transport=httpx.MockTransport(handle), **kw)
    return connector


def _failing_llm_client():
    import httpx
    from openai import OpenAI

    def handle(request):
        return httpx.Response(400, json={"error": {"message": LLM_MESSAGE, "type": "invalid_request_error",
                                                   "code": "context_length_exceeded"}})
    return OpenAI(api_key="test-key", base_url="http://llm.test/v1", max_retries=0,
                  http_client=httpx.Client(transport=httpx.MockTransport(handle)))


def _assert_markers_only_at_debug(log, markers=B1_MARKERS):
    default_log = "\n".join(
        logging.Formatter("%(message)s").format(r) for r in log.records if r.levelno >= logging.INFO)
    debug_log = _messages(log, below=logging.INFO)
    for marker in markers:
        assert marker not in default_log, f"{marker!r} is in the log at INFO or above"
    assert any(marker in debug_log for marker in markers), "the full error text is logged at DEBUG"
    return default_log


def test_transcription_service_error_body_is_not_logged_at_info(log):
    import io
    from src.services.transcription.base import TranscriptionRequest
    from src.services.transcription.exceptions import ProviderError
    from src.utils.error_text import user_error_text
    connector = _failing_asr_connector()
    with app.app_context(), pytest.raises(ProviderError) as raised:
        connector.transcribe(TranscriptionRequest(audio_file=io.BytesIO(b"audio"), filename=ECHO_FILENAME,
                                                  language="en", prompt=ECHO_PROMPT))
    default_log = _assert_markers_only_at_debug(log)
    assert "ASR request failed (file extension .mp3): status=400 body=" in default_log
    # The exception keeps the summarized body for DEBUG and for the job queue,
    # where it is shortened to the provider's message.
    recording = SimpleNamespace(original_filename=ECHO_FILENAME, resolved_initial_prompt=ECHO_PROMPT)
    shown = user_error_text(raised.value, recording)
    assert "status 400" in shown and PROVIDER_MESSAGE in shown
    for marker in B1_MARKERS:
        assert marker not in shown


def test_llm_error_body_is_not_logged_at_info(log):
    import src.services.llm as llm
    from openai import BadRequestError
    with app.app_context(), patch.object(llm, "client", _failing_llm_client()), \
         patch.object(llm, "TEXT_MODEL_NAME", "fake-model"), pytest.raises(BadRequestError):
        llm.call_llm_completion([{"role": "user", "content": ECHO_TRANSCRIPT}], operation_type="summarization")
    default_log = _assert_markers_only_at_debug(log)
    assert ("LLM API call failed (operation=summarization): BadRequestError status=400 "
            "code=context_length_exceeded length=") in default_log


def test_failed_summary_stores_the_provider_message_without_the_transcript(world, log):
    import src.services.llm as llm
    from src.services.error_classification import SummaryStepFailed
    with app.app_context():
        rec = db.session.get(Recording, world.rec)
        rec.transcription = json.dumps([{"speaker": "SPEAKER_00", "sentence": ECHO_TRANSCRIPT}])
        db.session.commit()
        c = _failing_llm_client()
        with patch.object(llm, "client", c), \
             patch.object(proc.llm_config, "is_configured", return_value=True), \
             patch.object(llm, "TEXT_MODEL_NAME", "fake-model"), \
             patch.object(proc, "ENABLE_INQUIRE_MODE", False), \
             patch("src.services.webhook_dispatch.emit_webhook_event"), \
             pytest.raises(SummaryStepFailed):
            proc.generate_summary_only_task(app.app_context(), world.rec)
        db.session.expire_all()
        rec = db.session.get(Recording, world.rec)
        stored = f"{rec.summary}\n{rec.error_message}"
    assert rec.status == "FAILED"
    # A known kind of failure keeps its plain explanation.
    assert "too long for AI processing" in stored
    for marker in B1_MARKERS:
        assert marker not in stored
    default_log = _assert_markers_only_at_debug(log)
    assert f"Error generating summary for recording {world.rec}: SummaryStepFailed" not in default_log
    assert f"Error generating summary for recording {world.rec}: BadRequestError status=400" in default_log


def test_failed_job_stores_and_sends_the_short_provider_message(world, log):
    """processing_job.error_message, the recording, the webhook and the email."""
    from src.services.job_queue import job_queue
    from src.services.transcription.exceptions import ProviderError
    job_queue.init_app(app)
    error = ProviderError(
        f"ASR request failed with status 400: {json.dumps(ASR_BODY)}", provider="asr_endpoint", status_code=400)
    error.permanent = True
    sent = {}
    with app.app_context():
        rec = db.session.get(Recording, world.rec)
        rec.original_filename = ECHO_FILENAME
        rec.resolved_initial_prompt = ECHO_PROMPT
        db.session.commit()
        job = ProcessingJob(user_id=world.user, recording_id=world.rec, job_type="transcribe",
                            status="processing", params=json.dumps({"initial_prompt": ECHO_PROMPT}))
        db.session.add(job)
        db.session.commit()
        job_id = job.id

        def webhook(user_id, event_type, data):
            sent["webhook"] = (event_type, dict(data))

        def email(owner, recording, error_text):
            sent["email"] = error_text
            return True

        with patch.object(job_queue, "_run_audio_job", side_effect=error), \
             patch("src.services.webhook_dispatch.emit_webhook_event", side_effect=webhook), \
             patch("src.services.email.send_transcription_failed_email", side_effect=email):
            job_queue._process_job(job)
        db.session.expire_all()
        job = db.session.get(ProcessingJob, job_id)
        rec = db.session.get(Recording, world.rec)
        texts = {"job": job.error_message, "recording": rec.error_message, "transcript": rec.transcription,
                 "webhook": sent["webhook"][1]["error"], "email": sent["email"]}
        assert job.status == "failed" and rec.status == "FAILED"
    assert sent["webhook"][0] == "recording.transcription.failed"
    for name, text in texts.items():
        assert "status 400" in text and PROVIDER_MESSAGE in text, name
        for marker in B1_MARKERS:
            assert marker not in text, f"{marker!r} is in the {name} text"
    default_log = _assert_markers_only_at_debug(log)
    assert (f"Job {job_id} (transcribe) for recording {world.rec} failed: "
            "ProviderError provider=asr_endpoint status=400 length=") in default_log


def test_user_error_text_keeps_the_message_and_removes_echoes():
    from src.utils.error_text import REMOVED, provider_message, scrub_request_echo, user_error_text
    # The provider's message field, its code and type; the other fields are dropped.
    assert provider_message('X failed with status 422: {"detail": "bad language", "input": "secret"}') == \
        "X failed with status 422: bad language"
    openai_style = ("Error code: 400 - {'error': {'message': 'Unsupported value', 'type': "
                    "'invalid_request_error', 'param': 'secret', 'code': 'unsupported_value'}}")
    assert provider_message(openai_style) == \
        "Error code: 400 - Unsupported value (code unsupported_value, type invalid_request_error)"
    assert provider_message('X failed: {"detail": [{"loc": ["body"], "msg": "field required", "type": "missing"}]}') \
        == "X failed: field required (type missing)"
    # Text without a structured body is kept.
    assert provider_message("RuntimeError: CUDA out of memory") == "RuntimeError: CUDA out of memory"
    # A long run, a quoted string and a short whole source are removed.
    sources = [json.dumps([{"speaker": "A", "sentence": ECHO_TRANSCRIPT}]), "Kestrel title", "okapi"]
    text = scrub_request_echo(
        f"could not parse {ECHO_TRANSCRIPT[:40]} near 'vertigo' in Kestrel title (hotword okapi)", sources)
    assert "ptarmigan" not in text and "vertigo" not in text and "Kestrel" not in text and "okapi" not in text
    assert text.count(REMOVED) >= 3 and text.startswith("could not parse")
    # No sources: nothing is removed, and the text is bounded.
    assert user_error_text("Connection refused") == "Connection refused"
    assert len(user_error_text("x" * 5000)) <= 500
    # The end of a traceback is kept.
    assert user_error_text("Traceback " + "frame " * 400 + "RuntimeError: out of memory").endswith("out of memory")


# --- names of people and files (privacy review B2) ----------------------------
#
# At INFO and above: user, group, tag, speaker and recording ids, and the
# extension of a file. Usernames, group names, speaker names, the original
# filename and the email recipient are logged at DEBUG.

GROUP_NAME = "Gyrfalcon-group-marker"
GROUP_NEW_NAME = "Gyrfalcon-renamed-marker"
SPEAKER_OLD = "Dr. Pipistrelle-speaker-marker"
SPEAKER_NEW = "Dr. Noctule-speaker-marker"
ORIGINAL_FILENAME = "jane-doe-wolverine-filename-marker.wav"


def _client_for(user_id):
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(user_id)
        s["_fresh"] = True
    return client


def _default_log(log):
    return "\n".join(logging.Formatter("%(message)s").format(r) for r in log.records if r.levelno >= logging.INFO)


def test_group_changes_log_ids_and_no_names_at_info(world, log):
    from src.models import Group, GroupMembership
    import src.api.groups as groups_api
    prev = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    s = uuid.uuid4().hex[:8]
    with app.app_context():
        admin = User(username=f"ospreyadm-{s}", email=f"adm_{s}@local.test", password="x", is_admin=True)
        member = User(username=f"ospreymem-{s}", email=f"mem_{s}@local.test", password="x")
        db.session.add_all([admin, member])
        db.session.commit()
        admin_id, member_id = admin.id, member.id
    group_id = None
    try:
        client = _client_for(admin_id)
        with patch.object(groups_api, "ENABLE_INTERNAL_SHARING", True):
            created = client.post("/api/admin/groups", json={"name": f"{GROUP_NAME} {s}"})
            assert created.status_code == 201, created.get_data(as_text=True)
            group_id = created.get_json()["id"]
            assert client.put(f"/api/admin/groups/{group_id}", json={"name": f"{GROUP_NEW_NAME} {s}"}).status_code == 200
            assert client.post(f"/api/admin/groups/{group_id}/members",
                               json={"user_id": member_id, "role": "member"}).status_code == 201
            assert client.put(f"/api/admin/groups/{group_id}/members/{member_id}",
                              json={"role": "admin"}).status_code == 200
            assert client.delete(f"/api/admin/groups/{group_id}/members/{member_id}").status_code == 200
            assert client.delete(f"/api/admin/groups/{group_id}").status_code == 200
    finally:
        app.config["WTF_CSRF_ENABLED"] = prev
        with app.app_context():
            db.session.rollback()
            if group_id:
                GroupMembership.query.filter_by(group_id=group_id).delete()
                Group.query.filter_by(id=group_id).delete()
            User.query.filter(User.id.in_([admin_id, member_id])).delete(synchronize_session=False)
            db.session.commit()
    _assert_only_at_debug(log, "Gyrfalcon-group-marker", "ospreyadm-", "ospreymem-")
    default_log = _default_log(log)
    assert "Gyrfalcon" not in default_log and "osprey" not in default_log
    # Every line still says what happened to which id.
    for line in (f"User {admin_id} created group {group_id}",
                 f"User {admin_id} updated group {group_id}",
                 f"User {admin_id} added user {member_id} to group {group_id} as member",
                 f"User {admin_id} updated the role of user {member_id} to admin in group {group_id}",
                 f"User {admin_id} removed user {member_id} from group {group_id}",
                 f"User {admin_id} deleted group {group_id}"):
        assert line in default_log, line


def test_speaker_rename_logs_the_id_and_no_name_at_info(world, log):
    from src.models import Speaker
    prev = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():
        speaker = Speaker(name=SPEAKER_OLD, user_id=world.user)
        db.session.add(speaker)
        db.session.commit()
        speaker_id = speaker.id
    try:
        resp = _client_for(world.user).put(f"/speakers/{speaker_id}", json={"name": SPEAKER_NEW})
        assert resp.status_code == 200, resp.get_data(as_text=True)
    finally:
        app.config["WTF_CSRF_ENABLED"] = prev
        with app.app_context():
            db.session.rollback()
            Speaker.query.filter_by(user_id=world.user).delete()
            db.session.commit()
    _assert_only_at_debug(log, "Pipistrelle-speaker-marker", "Noctule-speaker-marker")
    assert f"Renamed speaker {speaker_id} of user {world.user}:" in _default_log(log)


def test_email_recipient_is_not_logged_at_info(log):
    import src.services.email as email
    address = "condor-recipient-marker@example.org"
    config = {"from_name": "Speakr", "from_address": "speakr@example.org", "smtp_host": "smtp.test",
              "smtp_port": 587, "smtp_use_ssl": False, "smtp_use_tls": False, "smtp_username": "u",
              "smtp_password": "p"}
    with app.app_context(), patch.object(email, "get_email_config", return_value=config), \
         patch.object(email, "is_smtp_configured", return_value=True), \
         patch.object(email, "_load_logo_bytes", return_value=None), \
         patch.object(email.smtplib, "SMTP", MagicMock()):
        assert email._send_email(address, "Subject", "<p>body</p>", "body") is True
    _assert_only_at_debug(log, "condor-recipient-marker")
    assert "Email sent successfully" in _default_log(log)


def test_conversion_and_probe_log_the_extension_and_no_filename_at_info(log, tmp_path):
    """A stored upload is named <timestamp>_<uuid>_<original filename>."""
    from src.utils.audio_conversion import convert_if_needed
    from src.utils.ffprobe import is_video_file
    stored = tmp_path / f"20261115101500_{uuid.uuid4().hex}_{ORIGINAL_FILENAME}"
    stored.write_bytes(b"this is not audio")
    with app.app_context():
        assert is_video_file(str(stored)) is False       # the probe fails and is logged
        try:
            convert_if_needed(str(stored), original_filename=ORIGINAL_FILENAME)
        except Exception:
            pass                                          # FFmpeg rejects the file; the log is what counts
    _assert_only_at_debug(log, "wolverine-filename-marker")
    default_log = _default_log(log)
    assert "jane-doe" not in default_log
    # The stored file is still identified: its timestamp and the start of its uuid.
    assert f"<stored file 20261115101500_{stored.name.split('_')[1][:12]}.wav>" in default_log


def test_loggable_path_and_error():
    from src.utils.log_filters import loggable_error, loggable_path
    stored = "/data/uploads/20261115101500_0123456789abcdef0123456789abcdef_Jane_Doe_intake.mp3"
    assert loggable_path(stored) == "<stored file 20261115101500_0123456789ab.mp3>"
    # A file of no known scheme: a keyed digest of its name, never the extension alone (#620).
    assert re.fullmatch(r"<file [0-9a-f]{10}\.m4a>", loggable_path("/watch/user7/Jane Doe intake.M4A"))
    assert re.fullmatch(r"<file [0-9a-f]{10}>", loggable_path("no-extension"))
    assert loggable_path(None) == "<no file>"
    text = loggable_error(RuntimeError(f"{stored}: Invalid data found when processing input"), stored)
    assert "Jane" not in text and "Invalid data found" in text and "<stored file 20261115101500_" in text
    # A stored upload named in the text is replaced even when it was not passed in.
    assert "Jane" not in loggable_error(RuntimeError(f"cannot open {stored}"))
    assert "Jane" not in loggable_error(OSError("[Errno 2] No such file: '/watch/Jane Doe.mp3'"), "/watch/Jane Doe.mp3")
