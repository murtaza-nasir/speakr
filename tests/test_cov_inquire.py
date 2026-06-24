#!/usr/bin/env python3
"""
Coverage-focused tests for src/api/inquire.py (semantic search + chat-over-library).

These complement tests/test_inquire_mode.py (which covers models / imports /
reindex) by exercising the HTTP routes and the streaming RAG generator end to
end. Everything external is mocked at the inquire.py import site:
  - semantic_search_chunks  (vector similarity search)
  - call_llm_completion     (router + query enrichment LLM calls)
  - call_chat_completion    (the final answer LLM call)
  - process_streaming_with_thinking (DIRECT-path streaming)
  - client                  (OpenRouter client availability gate)

so the suite is fully offline and deterministic.

SHARED-DB NOTE: the pytest DB is shared across files. Every test creates its
own user/recording/chunk rows and scopes all assertions to those IDs; nothing
asserts on global counts or lists.
"""
import os
import sys
import json
import uuid
import contextlib
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from src.app import app, db
from src.models import User, Recording, TranscriptChunk, InquireSession, Tag

app.config['WTF_CSRF_ENABLED'] = False


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

def _suffix():
    return uuid.uuid4().hex[:8]


def _drain_app_contexts():
    """Pop any Flask app contexts that leaked onto the stack.

    The inquire chat endpoint streams from a generator that does
    `ctx = app.app_context(); ctx.push()` and only pops it in a `finally`. When
    a test reads the SSE Response without exhausting/closing the generator, that
    `finally` never runs and the pushed context lingers — corrupting db.session
    for *later* tests (stale identity-map hits, wrong-owner lookups). We pop them
    defensively so every test starts with a clean context stack and session.
    """
    from flask import has_app_context
    popped = 0
    while has_app_context() and popped < 50:
        try:
            app_ctx = app.app_context()
            # Pop whatever is currently on top of the stack.
            from flask.globals import app_ctx as _current  # type: ignore
            _current._get_current_object().pop()
        except Exception:
            break
        popped += 1


@pytest.fixture
def client():
    _drain_app_contexts()
    with app.app_context():
        db.session.remove()
    yield app.test_client()
    _drain_app_contexts()
    with app.app_context():
        db.session.remove()


@contextlib.contextmanager
def _login(client, user_id):
    with client.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True
    yield


def _make_user(**overrides):
    """Create and persist a user; returns the (detached-safe) id + a fresh getter."""
    suffix = _suffix()
    with app.app_context():
        user = User(
            username=overrides.get('username', f"inqcov_{suffix}"),
            email=overrides.get('email', f"inqcov_{suffix}@example.com"),
            name=overrides.get('name', "Test User"),
            job_title=overrides.get('job_title'),
            company=overrides.get('company'),
            output_language=overrides.get('output_language'),
        )
        db.session.add(user)
        db.session.commit()
        return user.id


def _make_recording(user_id, **overrides):
    with app.app_context():
        rec = Recording(
            user_id=user_id,
            title=overrides.get('title', f"Rec {_suffix()}"),
            status=overrides.get('status', 'COMPLETED'),
            transcription=overrides.get('transcription', "Full transcript text here."),
            participants=overrides.get('participants'),
            meeting_date=overrides.get('meeting_date'),
        )
        db.session.add(rec)
        db.session.commit()
        return rec.id


def _make_chunk(user_id, recording_id, **overrides):
    with app.app_context():
        chunk = TranscriptChunk(
            recording_id=recording_id,
            user_id=user_id,
            chunk_index=overrides.get('chunk_index', 0),
            content=overrides.get('content', "A relevant chunk of conversation."),
            start_time=overrides.get('start_time', 0.0),
            end_time=overrides.get('end_time', 5.0),
            speaker_name=overrides.get('speaker_name'),
        )
        db.session.add(chunk)
        db.session.commit()
        return chunk.id


def _get_chunk_pair(chunk_id, similarity=0.9):
    """Return a real, session-attached (chunk, similarity) tuple usable inside an
    app context, mirroring what semantic_search_chunks yields.

    Must be called from within an active app context. We eager-load the parent
    Recording (joinedload) so chunk.recording stays accessible even after the
    chat generator's nested app-context exits and detaches the chunk — mirroring
    real semantic_search_chunks results, which the caller consumes lazily.
    """
    from sqlalchemy.orm import joinedload
    chunk = (db.session.query(TranscriptChunk)
             .options(joinedload(TranscriptChunk.recording))
             .filter_by(id=chunk_id)
             .first())
    return (chunk, similarity)


def _search_returning(chunk_id, similarity=0.8):
    """Build a semantic_search_chunks side_effect that re-fetches the chunk inside
    whatever app context is active when the search runs, keeping it session-bound."""
    def _side_effect(*args, **kwargs):
        return [_get_chunk_pair(chunk_id, similarity)]
    return _side_effect


def _sse_events(resp):
    """Parse an SSE Response into a list of decoded JSON payload dicts."""
    body = resp.get_data(as_text=True)
    # Close the streaming response so the generator's finally (ctx.pop()) runs and
    # no app context leaks into the next test.
    try:
        resp.close()
    except Exception:
        pass
    events = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


# --------------------------------------------------------------------------- #
# Auth: all routes require login
# --------------------------------------------------------------------------- #

def test_inquire_page_requires_login(client):
    resp = client.get('/inquire')
    assert resp.status_code in (302, 401)


def test_search_requires_login(client):
    resp = client.post('/api/inquire/search', json={'query': 'x'})
    assert resp.status_code in (302, 401)


def test_chat_requires_login(client):
    resp = client.post('/api/inquire/chat', json={'message': 'x'})
    assert resp.status_code in (302, 401)


def test_sessions_get_requires_login(client):
    resp = client.get('/api/inquire/sessions')
    assert resp.status_code in (302, 401)


def test_available_filters_requires_login(client):
    resp = client.get('/api/inquire/available_filters')
    assert resp.status_code in (302, 401)


# --------------------------------------------------------------------------- #
# /inquire page
# --------------------------------------------------------------------------- #

def test_inquire_page_enabled_renders(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.get('/inquire')
        assert resp.status_code == 200


def test_inquire_page_disabled_redirects(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', False):
        resp = client.get('/inquire')
        assert resp.status_code == 302


# --------------------------------------------------------------------------- #
# Inquire-mode disabled => 403 on the JSON APIs
# --------------------------------------------------------------------------- #

def test_sessions_get_disabled_403(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', False):
        resp = client.get('/api/inquire/sessions')
        assert resp.status_code == 403


def test_sessions_post_disabled_403(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', False):
        resp = client.post('/api/inquire/sessions', json={'session_name': 'x'})
        assert resp.status_code == 403


def test_search_disabled_403(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', False):
        resp = client.post('/api/inquire/search', json={'query': 'x'})
        assert resp.status_code == 403


def test_chat_disabled_403(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', False):
        resp = client.post('/api/inquire/chat', json={'message': 'x'})
        assert resp.status_code == 403


def test_available_filters_disabled_403(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', False):
        resp = client.get('/api/inquire/available_filters')
        assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Sessions: create + list
# --------------------------------------------------------------------------- #

def test_create_session_no_data_400(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        # Empty JSON body -> data is falsy -> 400
        resp = client.post('/api/inquire/sessions', json={})
        assert resp.status_code == 400


def test_create_session_success(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.post('/api/inquire/sessions', json={
            'session_name': 'My Session',
            'filter_tags': [1, 2],
            'filter_speakers': ['Alice'],
            'filter_date_from': '2024-01-01',
            'filter_date_to': '2024-12-31',
            'filter_recording_ids': [42],
        })
        assert resp.status_code == 201
        data = resp.get_json()
        assert data['session_name'] == 'My Session'
        assert data['filter_speakers'] == ['Alice']
        assert data['filter_recording_ids'] == [42]
        assert data['filter_date_from'] == '2024-01-01'


def test_get_sessions_owner_scoped(client):
    """A session created by user A must not appear for user B."""
    user_a = _make_user()
    user_b = _make_user()
    with app.app_context():
        sess = InquireSession(user_id=user_a, session_name='A-only', filter_tags='[]')
        db.session.add(sess)
        db.session.commit()
        sess_id = sess.id

    with _login(client, user_a), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.get('/api/inquire/sessions')
        assert resp.status_code == 200
        ids = [s['id'] for s in resp.get_json()]
        assert sess_id in ids

    with _login(client, user_b), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.get('/api/inquire/sessions')
        assert resp.status_code == 200
        ids = [s['id'] for s in resp.get_json()]
        assert sess_id not in ids


def test_create_session_bad_date_500(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.post('/api/inquire/sessions', json={
            'session_name': 'bad', 'filter_date_from': 'not-a-date'
        })
        assert resp.status_code == 500
        assert 'error' in resp.get_json()


# --------------------------------------------------------------------------- #
# Search endpoint
# --------------------------------------------------------------------------- #

def test_search_no_data_400(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.post('/api/inquire/search', json={})
        assert resp.status_code == 400


def test_search_no_query_400(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.post('/api/inquire/search', json={'top_k': 5})
        assert resp.status_code == 400


def test_search_returns_ranked_results(client):
    user_id = _make_user()
    rec_id = _make_recording(user_id, title="Budget Meeting")
    chunk_id = _make_chunk(user_id, rec_id, content="We discussed the budget.",
                           speaker_name="Alice")

    def fake_search(uid, query, filters, top_k):
        assert uid == user_id
        return [_get_chunk_pair(chunk_id, 0.87)]

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.semantic_search_chunks', side_effect=fake_search):
        resp = client.post('/api/inquire/search', json={'query': 'budget'})
        assert resp.status_code == 200
        results = resp.get_json()['results']
        assert len(results) == 1
        r = results[0]
        assert r['similarity'] == 0.87
        assert r['recording_title'] == "Budget Meeting"
        assert r['content'] == "We discussed the budget."


def test_search_empty_results(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.semantic_search_chunks', return_value=[]):
        resp = client.post('/api/inquire/search', json={'query': 'nothing matches'})
        assert resp.status_code == 200
        assert resp.get_json()['results'] == []


def test_search_passes_filters(client):
    """Date/tag/speaker/recording filters are parsed and forwarded to the search."""
    user_id = _make_user()
    captured = {}

    def fake_search(uid, query, filters, top_k):
        captured.update(filters)
        captured['top_k'] = top_k
        return []

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.semantic_search_chunks', side_effect=fake_search):
        resp = client.post('/api/inquire/search', json={
            'query': 'q',
            'filter_tags': [3],
            'filter_speakers': ['Bob'],
            'filter_recording_ids': [7],
            'filter_date_from': '2024-02-01',
            'filter_date_to': '2024-03-01',
            'top_k': 9,
        })
        assert resp.status_code == 200
    assert captured['tag_ids'] == [3]
    assert captured['speaker_names'] == ['Bob']
    assert captured['recording_ids'] == [7]
    assert str(captured['date_from']) == '2024-02-01'
    assert str(captured['date_to']) == '2024-03-01'
    assert captured['top_k'] == 9


def test_search_with_meeting_date_in_result(client):
    from datetime import datetime
    user_id = _make_user()
    rec_id = _make_recording(user_id, meeting_date=datetime(2024, 5, 1))
    chunk_id = _make_chunk(user_id, rec_id)

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.semantic_search_chunks',
               return_value=None) as m:
        # Pass a callable that yields a real pair within the request context.
        m.side_effect = lambda *a, **k: [_get_chunk_pair(chunk_id, 0.5)]
        resp = client.post('/api/inquire/search', json={'query': 'x'})
        assert resp.status_code == 200
        r = resp.get_json()['results'][0]
        assert r['recording_meeting_date'] is not None


def test_search_bad_date_500(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.post('/api/inquire/search', json={
            'query': 'q', 'filter_date_from': 'garbage'
        })
        assert resp.status_code == 500
        assert 'error' in resp.get_json()


def test_search_internal_error_500(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.semantic_search_chunks',
               side_effect=RuntimeError("boom")):
        resp = client.post('/api/inquire/search', json={'query': 'q'})
        assert resp.status_code == 500
        assert 'boom' in resp.get_json()['error']


# --------------------------------------------------------------------------- #
# Chat endpoint
# --------------------------------------------------------------------------- #

def test_chat_no_data_400(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.post('/api/inquire/chat', json={})
        assert resp.status_code == 400


def test_chat_no_message_400(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
        resp = client.post('/api/inquire/chat', json={'message_history': []})
        assert resp.status_code == 400


def test_chat_client_unavailable_503(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', None):
        resp = client.post('/api/inquire/chat', json={'message': 'hi'})
        assert resp.status_code == 503


def _llm_msg(content):
    """Build a fake OpenAI-style completion response object."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _stream_chunks(texts):
    """Build a fake streaming iterator yielding delta chunks of `texts`."""
    out = []
    for t in texts:
        ch = MagicMock()
        ch.choices = [MagicMock()]
        ch.choices[0].delta.content = t
        out.append(ch)
    return iter(out)


def test_chat_direct_path(client):
    """Router returns DIRECT -> uses process_streaming_with_thinking, no search."""
    user_id = _make_user()

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.call_llm_completion',
               return_value=_llm_msg("DIRECT")) as router, \
         patch('src.api.inquire.process_streaming_with_thinking',
               return_value=iter([
                   "data: " + json.dumps({'delta': 'Here is a formatted answer.'}) + "\n\n",
                   "data: " + json.dumps({'end_of_stream': True}) + "\n\n",
               ])) as pst, \
         patch('src.api.inquire.semantic_search_chunks') as search:
        resp = client.post('/api/inquire/chat', json={'message': 'format this'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        deltas = [e.get('delta') for e in events if 'delta' in e]
        assert 'Here is a formatted answer.' in deltas
        # DIRECT path must NOT run semantic search.
        search.assert_not_called()
        pst.assert_called()


def test_chat_rag_path_with_results(client):
    """Router returns RAG -> enrichment -> search -> chat_completion answer."""
    user_id = _make_user()
    rec_id = _make_recording(user_id, title="Planning Call",
                             participants="Alice, Bob")
    chunk_id = _make_chunk(user_id, rec_id, content="Alice proposed a new timeline.",
                           speaker_name="Alice")

    # call_llm_completion is used for BOTH the router and the enrichment step.
    def llm_side_effect(messages, **kwargs):
        op = kwargs.get('operation_type')
        if op == 'query_routing':
            return _llm_msg("RAG")
        # query_enrichment -> JSON array
        return _llm_msg('["timeline", "planning"]')

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_side_effect), \
         patch('src.api.inquire.semantic_search_chunks',
               side_effect=lambda *a, **k: [_get_chunk_pair(chunk_id, 0.8)]), \
         patch('src.api.inquire.call_chat_completion',
               return_value=_stream_chunks(["Alice ", "proposed ", "a timeline."])):
        resp = client.post('/api/inquire/chat', json={'message': 'what did Alice say?'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        deltas = "".join(e['delta'] for e in events if 'delta' in e)
        assert "Alice proposed a timeline." in deltas
        assert any(e.get('end_of_stream') for e in events)


def test_chat_rag_no_results(client):
    """RAG path with zero search results still streams an answer (no context)."""
    user_id = _make_user()

    def llm_side_effect(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            return _llm_msg("RAG")
        return _llm_msg('["foo"]')

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_side_effect), \
         patch('src.api.inquire.semantic_search_chunks', return_value=[]), \
         patch('src.api.inquire.call_chat_completion',
               return_value=_stream_chunks(["No relevant info found."])):
        resp = client.post('/api/inquire/chat', json={'message': 'anything?'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        deltas = "".join(e['delta'] for e in events if 'delta' in e)
        assert "No relevant info found." in deltas


def test_chat_router_failure_falls_back_to_rag(client):
    """If the router LLM raises, code falls back to RAG enrichment + search."""
    user_id = _make_user()
    rec_id = _make_recording(user_id)
    chunk_id = _make_chunk(user_id, rec_id, content="fallback content")

    def llm_side_effect(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            raise RuntimeError("router down")
        return _llm_msg('["term"]')

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_side_effect), \
         patch('src.api.inquire.semantic_search_chunks',
               side_effect=lambda *a, **k: [_get_chunk_pair(chunk_id, 0.7)]), \
         patch('src.api.inquire.call_chat_completion',
               return_value=_stream_chunks(["answer"])):
        resp = client.post('/api/inquire/chat', json={'message': 'q'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        assert any(e.get('delta') == 'answer' for e in events)


def test_chat_enrichment_failure_uses_original_query(client):
    """If enrichment returns non-JSON, search proceeds with the original query."""
    user_id = _make_user()
    rec_id = _make_recording(user_id)
    chunk_id = _make_chunk(user_id, rec_id)

    def llm_side_effect(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            return _llm_msg("RAG")
        return _llm_msg("not json at all")

    search_calls = {'n': 0}

    def search_side_effect(*a, **k):
        search_calls['n'] += 1
        return [_get_chunk_pair(chunk_id, 0.6)]

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_side_effect), \
         patch('src.api.inquire.semantic_search_chunks', side_effect=search_side_effect), \
         patch('src.api.inquire.call_chat_completion',
               return_value=_stream_chunks(["ok"])):
        resp = client.post('/api/inquire/chat', json={'message': 'q'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        # Enrichment failed (non-JSON) -> the code falls back to the original
        # query and still searches and answers. Assert the fallback ran (search
        # was invoked at least once) and produced the streamed answer.
        assert search_calls['n'] >= 1
        deltas = "".join(e['delta'] for e in events if 'delta' in e)
        assert 'ok' in deltas


def test_chat_thinking_tags_split_out(client):
    """<think> content is emitted as 'thinking' events, the rest as 'delta'."""
    user_id = _make_user()
    rec_id = _make_recording(user_id)
    chunk_id = _make_chunk(user_id, rec_id)

    def llm_side_effect(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            return _llm_msg("RAG")
        return _llm_msg('["t"]')

    streamed = ["Before ", "<think>", "secret reasoning", "</think>", "After"]

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_side_effect), \
         patch('src.api.inquire.semantic_search_chunks',
               side_effect=lambda *a, **k: [_get_chunk_pair(chunk_id, 0.7)]), \
         patch('src.api.inquire.call_chat_completion',
               return_value=_stream_chunks(streamed)):
        resp = client.post('/api/inquire/chat', json={'message': 'q'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        thinking = [e['thinking'] for e in events if 'thinking' in e]
        deltas = "".join(e['delta'] for e in events if 'delta' in e)
        assert any('secret reasoning' in t for t in thinking)
        assert 'secret reasoning' not in deltas
        assert 'Before' in deltas and 'After' in deltas


def test_chat_full_transcript_request(client):
    """If the model emits REQUEST_FULL_TRANSCRIPT, the full transcript is fetched
    and a second completion is run via process_streaming_with_thinking."""
    user_id = _make_user()
    rec_id = _make_recording(user_id, transcription="THE FULL TRANSCRIPT BODY")
    chunk_id = _make_chunk(user_id, rec_id)

    def llm_side_effect(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            return _llm_msg("RAG")
        return _llm_msg('["t"]')

    first_stream = _stream_chunks([f"REQUEST_FULL_TRANSCRIPT:{rec_id}\n"])

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_side_effect), \
         patch('src.api.inquire.semantic_search_chunks',
               side_effect=lambda *a, **k: [_get_chunk_pair(chunk_id, 0.7)]), \
         patch('src.api.inquire.call_chat_completion', return_value=first_stream), \
         patch('src.api.inquire.process_streaming_with_thinking',
               return_value=iter([
                   "data: " + json.dumps({'delta': 'full-transcript answer'}) + "\n\n",
               ])) as pst:
        resp = client.post('/api/inquire/chat', json={'message': 'give me everything'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        deltas = "".join(e.get('delta', '') for e in events)
        assert 'full-transcript answer' in deltas
        pst.assert_called()


def test_chat_full_transcript_request_wrong_owner(client):
    """REQUEST_FULL_TRANSCRIPT for a recording the user doesn't own -> error event."""
    owner = _make_user()
    other = _make_user()
    rec_id = _make_recording(owner, transcription="OWNED")  # belongs to `owner`
    my_rec = _make_recording(other)
    my_chunk = _make_chunk(other, my_rec)

    def llm_side_effect(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            return _llm_msg("RAG")
        return _llm_msg('["t"]')

    first_stream = _stream_chunks([f"REQUEST_FULL_TRANSCRIPT:{rec_id}\n"])

    with _login(client, other), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_side_effect), \
         patch('src.api.inquire.semantic_search_chunks',
               side_effect=lambda *a, **k: [_get_chunk_pair(my_chunk, 0.7)]), \
         patch('src.api.inquire.call_chat_completion', return_value=first_stream):
        resp = client.post('/api/inquire/chat', json={'message': 'show it'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        # Should surface an access error, not the owner's transcript.
        assert any('Unable to access full transcript' in str(e.get('delta', ''))
                   for e in events)


def test_chat_generation_error_emits_error_event(client):
    """An exception inside the generator is reported as an SSE error event."""
    user_id = _make_user()

    def llm_side_effect(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            return _llm_msg("RAG")
        return _llm_msg('["t"]')

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_side_effect), \
         patch('src.api.inquire.semantic_search_chunks', return_value=[]), \
         patch('src.api.inquire.call_chat_completion',
               side_effect=RuntimeError("kaboom")):
        resp = client.post('/api/inquire/chat', json={'message': 'q'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        assert any('kaboom' in str(e.get('error', '')) for e in events)


def test_chat_token_budget_exceeded(client):
    """TokenBudgetExceeded inside the generator is reported with budget_exceeded."""
    from src.api.inquire import TokenBudgetExceeded
    user_id = _make_user()

    def llm_side_effect(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            return _llm_msg("RAG")
        return _llm_msg('["t"]')

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_side_effect), \
         patch('src.api.inquire.semantic_search_chunks', return_value=[]), \
         patch('src.api.inquire.call_chat_completion',
               side_effect=TokenBudgetExceeded("over budget")):
        resp = client.post('/api/inquire/chat', json={'message': 'q'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        assert any(e.get('budget_exceeded') for e in events)


# --------------------------------------------------------------------------- #
# available_filters endpoint
# --------------------------------------------------------------------------- #

def test_available_filters_returns_owner_data(client):
    user_id = _make_user()
    rec_id = _make_recording(user_id, title="Standup", status='COMPLETED',
                             participants="Carol, Dave")
    with app.app_context():
        tag = Tag(name=f"tag_{_suffix()}", user_id=user_id, group_id=None)
        db.session.add(tag)
        db.session.commit()
        tag_id = tag.id

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.get_accessible_recording_ids',
               return_value=[rec_id]):
        resp = client.get('/api/inquire/available_filters')
        assert resp.status_code == 200
        data = resp.get_json()
        assert tag_id in [t['id'] for t in data['tags']]
        assert 'Carol' in data['speakers'] and 'Dave' in data['speakers']
        assert rec_id in [r['id'] for r in data['recordings']]


def test_available_filters_error_500(client):
    user_id = _make_user()
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.get_accessible_recording_ids',
               side_effect=RuntimeError("db gone")):
        resp = client.get('/api/inquire/available_filters')
        assert resp.status_code == 500
        assert 'error' in resp.get_json()
