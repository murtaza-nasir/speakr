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
# RAG generator internals: dedup, sort, speaker post-filter (mutation-targeted)
#
# These drive the streaming chat generator and inspect either the system prompt
# handed to call_chat_completion (which embeds the retrieved context) or the
# sequence of queries handed to semantic_search_chunks. Each test below is
# pinned to a specific surviving mutant in src/api/inquire.py.
# --------------------------------------------------------------------------- #

def _rag_router_then(enrichment_json):
    """call_llm_completion side_effect: route to RAG, then return the given JSON
    string as the query-enrichment response."""
    def _se(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            return _llm_msg("RAG")
        return _llm_msg(enrichment_json)
    return _se


def _capturing_chat_completion(captured, texts=("answer",)):
    """call_chat_completion side_effect that records the system prompt + kwargs of
    the (first) answer call, then streams `texts` back."""
    def _se(messages, **kwargs):
        if 'system_prompt' not in captured:
            captured['system_prompt'] = messages[0]['content']
            captured['kwargs'] = kwargs
        return _stream_chunks(list(texts))
    return _se


def test_chat_rag_dedups_repeated_chunks(client):
    """inquire.py:398 — a chunk returned twice by search must appear once in context.

    MUTATION-VERIFIED: 398 `not in`->`in` makes the dedup guard reject every chunk
    (the seen-set starts empty), so the context ends up empty and the marker count
    drops to 0 -> this test FAILS.
    """
    user_id = _make_user()
    rec_id = _make_recording(user_id, title="Dedup Rec", participants=None)
    marker = "DEDUPUNIQUECONTENT_" + _suffix()
    chunk_id = _make_chunk(user_id, rec_id, content=marker, chunk_index=0)

    def search_se(uid, query, filters, top_k):
        # Same chunk id returned twice within a single search result.
        return [_get_chunk_pair(chunk_id, 0.8), _get_chunk_pair(chunk_id, 0.8)]

    captured = {}
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=_rag_router_then('[]')), \
         patch('src.api.inquire.semantic_search_chunks', side_effect=search_se), \
         patch('src.api.inquire.call_chat_completion',
               side_effect=_capturing_chat_completion(captured)):
        resp = client.post('/api/inquire/chat', json={'message': 'tell me about the topic'})
        assert resp.status_code == 200
        _sse_events(resp)
    assert captured.get('system_prompt', '').count(marker) == 1


def test_chat_rag_sorts_by_similarity_desc(client):
    """inquire.py:403 — combined results are sorted by similarity DESCENDING before
    the top-N truncation, so the highest-scored chunk survives a top-1 cut.

    MUTATION-VERIFIED: 403 `reverse=True`->`reverse=False` sorts ascending, so the
    top-1 cut keeps the LOW-scored chunk instead -> this test FAILS.
    """
    user_id = _make_user()
    rec_id = _make_recording(user_id, title="Sort Rec", participants=None)
    high = "HIGHSCORECONTENT_" + _suffix()
    low = "LOWSCORECONTENT_" + _suffix()
    high_id = _make_chunk(user_id, rec_id, content=high, chunk_index=0)
    low_id = _make_chunk(user_id, rec_id, content=low, chunk_index=1)

    def search_se(uid, query, filters, top_k):
        # Returned low-first so input order differs from the sorted order.
        return [_get_chunk_pair(low_id, 0.1), _get_chunk_pair(high_id, 0.9)]

    captured = {}
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=_rag_router_then('[]')), \
         patch('src.api.inquire.semantic_search_chunks', side_effect=search_se), \
         patch('src.api.inquire.call_chat_completion',
               side_effect=_capturing_chat_completion(captured)):
        resp = client.post('/api/inquire/chat',
                           json={'message': 'budget', 'context_chunks': 1})
        assert resp.status_code == 200
        _sse_events(resp)
    sp = captured.get('system_prompt', '')
    assert high in sp
    assert low not in sp


def test_chat_rag_speaker_postfilter(client):
    """inquire.py:427-436/458 — when a mentioned speaker is absent from the initial
    results, a speaker-filtered re-search runs; non-matching chunk is dropped and
    the matching speaker's chunks are kept (and deduped).

    MUTATION-VERIFIED:
      * 436 `if not speaker_in_results`->`if speaker_in_results` suppresses the
        auto speaker filter, so the non-Alice chunk is kept -> this test FAILS.
      * 458 `not in`->`in` makes the re-search dedup reject every chunk, so the
        auto-filtered set is empty and the code keeps the original chunk -> FAILS.
    """
    user_id = _make_user()
    # alice_rec's participants make 'Alice' an available speaker for this user.
    alice_rec = _make_recording(user_id, title="Alice Rec", participants="Alice")
    other_rec = _make_recording(user_id, title="Other Rec", participants=None)

    other_content = "OTHERSPEAKERCONTENT_" + _suffix()
    alice1_content = "ALICEONECONTENT_" + _suffix()
    alice2_content = "ALICETWOCONTENT_" + _suffix()
    other_id = _make_chunk(user_id, other_rec, content=other_content,
                           speaker_name="Bob", chunk_index=0)
    alice1_id = _make_chunk(user_id, alice_rec, content=alice1_content,
                            speaker_name="Alice", chunk_index=0)
    alice2_id = _make_chunk(user_id, alice_rec, content=alice2_content,
                            speaker_name="Alice", chunk_index=1)

    def search_se(uid, query, filters, top_k):
        if filters.get('speaker_names'):
            # Speaker-filtered re-search: Alice chunks, with alice1 duplicated to
            # also exercise the 458 dedup guard. Two distinct ids -> len >= 2 so the
            # downstream "<2 results" broader re-search is skipped.
            return [_get_chunk_pair(alice1_id, 0.95),
                    _get_chunk_pair(alice1_id, 0.95),
                    _get_chunk_pair(alice2_id, 0.85)]
        return [_get_chunk_pair(other_id, 0.5)]

    captured = {}
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=_rag_router_then('[]')), \
         patch('src.api.inquire.semantic_search_chunks', side_effect=search_se), \
         patch('src.api.inquire.call_chat_completion',
               side_effect=_capturing_chat_completion(captured)):
        resp = client.post('/api/inquire/chat', json={'message': 'what did Alice say?'})
        assert resp.status_code == 200
        _sse_events(resp)
    sp = captured.get('system_prompt', '')
    assert alice1_content in sp          # matching speaker kept
    assert alice2_content in sp
    assert other_content not in sp       # non-matching speaker dropped
    assert sp.count(alice1_content) == 1  # 458: re-search dedup


def test_chat_rag_uses_enriched_terms(client):
    """inquire.py:360 — a non-empty enrichment response is parsed and its terms are
    actually searched (not discarded as if empty).

    MUTATION-VERIFIED: 360 `not raw_content`->`raw_content` treats the (truthy)
    enrichment JSON as empty, raises, and falls back to the original query only, so
    the enriched term is never searched -> this test FAILS.
    """
    user_id = _make_user()
    rec_id = _make_recording(user_id, participants=None)
    chunk_id = _make_chunk(user_id, rec_id, content="enr content " + _suffix())

    queries_seen = []

    def search_se(uid, query, filters, top_k):
        queries_seen.append(query)
        return [_get_chunk_pair(chunk_id, 0.7)]

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion',
               side_effect=_rag_router_then('["alphaterm"]')), \
         patch('src.api.inquire.semantic_search_chunks', side_effect=search_se), \
         patch('src.api.inquire.call_chat_completion',
               return_value=_stream_chunks(["answer"])):
        resp = client.post('/api/inquire/chat', json={'message': 'orig query'})
        assert resp.status_code == 200
        _sse_events(resp)
    assert 'orig query' in queries_seen   # original query always searched
    assert 'alphaterm' in queries_seen    # enriched term also searched


def test_chat_direct_path_passes_stream_true(client):
    """inquire.py:284 — the DIRECT-path completion is invoked with stream=True.

    MUTATION-VERIFIED: 284 `stream=True`->`stream=False` changes the kwarg passed
    to call_llm_completion for the 'chat' operation -> this test FAILS.
    """
    user_id = _make_user()
    router = MagicMock(return_value=_llm_msg("DIRECT"))
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.call_llm_completion', router), \
         patch('src.api.inquire.process_streaming_with_thinking',
               return_value=iter(["data: " + json.dumps({'end_of_stream': True}) + "\n\n"])), \
         patch('src.api.inquire.semantic_search_chunks') as search:
        resp = client.post('/api/inquire/chat', json={'message': 'format this'})
        assert resp.status_code == 200
        _sse_events(resp)
        search.assert_not_called()
    chat_calls = [c for c in router.call_args_list
                  if c.kwargs.get('operation_type') == 'chat']
    assert len(chat_calls) == 1
    assert chat_calls[0].kwargs.get('stream') is True


def test_chat_empty_router_falls_back_to_rag(client):
    """inquire.py:257 — an empty router response is treated as a failure: the code
    must NOT take the DIRECT path and instead falls through to RAG (search + the
    streaming answer via call_chat_completion).
    """
    user_id = _make_user()
    rec_id = _make_recording(user_id, participants=None)
    chunk_id = _make_chunk(user_id, rec_id, content="rag content " + _suffix())

    def llm_se(messages, **kwargs):
        if kwargs.get('operation_type') == 'query_routing':
            return _llm_msg("")   # empty router content
        return _llm_msg('["t"]')

    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=llm_se), \
         patch('src.api.inquire.semantic_search_chunks',
               side_effect=lambda *a, **k: [_get_chunk_pair(chunk_id, 0.7)]), \
         patch('src.api.inquire.process_streaming_with_thinking') as pst, \
         patch('src.api.inquire.call_chat_completion',
               return_value=_stream_chunks(["rag answer"])):
        resp = client.post('/api/inquire/chat', json={'message': 'who said what?'})
        assert resp.status_code == 200
        events = _sse_events(resp)
        deltas = "".join(e['delta'] for e in events if 'delta' in e)
        # RAG answer streamed (DIRECT path, which uses process_streaming_with_thinking,
        # was not taken).
        assert 'rag answer' in deltas
        pst.assert_not_called()


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


def test_chat_rag_present_speaker_not_auto_filtered(client):
    """inquire.py:433 — a mentioned speaker who is ALREADY present in the initial
    chunk results must NOT trigger the auto speaker-filter re-search.

    The user message mentions 'Alice', who is an available speaker (a recording
    lists her as a participant) AND whose chunks are already in the results. So
    `speaker_in_results` becomes True, Alice is NOT appended to
    `mentioned_speakers`, and no speaker-filtered re-search is run.

    MUTATION-VERIFIED: line 433 `speaker_in_results = True`->`False` makes the
    present speaker look missing, so Alice is appended and the auto speaker
    filter fires — semantic_search_chunks gets called with
    filters['speaker_names'] == ['Alice'] and a 'filtering' status event is
    emitted -> this test FAILS.
    """
    user_id = _make_user()
    # alice_rec's participants make 'Alice' an available speaker for this user.
    alice_rec = _make_recording(user_id, title="Alice Rec", participants="Alice")

    a1 = "ALICEPRESENTONE_" + _suffix()
    a2 = "ALICEPRESENTTWO_" + _suffix()
    a1_id = _make_chunk(user_id, alice_rec, content=a1,
                        speaker_name="Alice", chunk_index=0)
    a2_id = _make_chunk(user_id, alice_rec, content=a2,
                        speaker_name="Alice", chunk_index=1)

    search_filters = []

    def search_se(uid, query, filters, top_k):
        # Record the filters of every search so we can assert no speaker re-search.
        search_filters.append(dict(filters) if filters else {})
        # Alice is already present in the initial results (two distinct chunks so
        # len >= 2 skips the downstream "<2 results" broader re-search).
        return [_get_chunk_pair(a1_id, 0.9), _get_chunk_pair(a2_id, 0.8)]

    captured = {}
    with _login(client, user_id), \
         patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.client', MagicMock()), \
         patch('src.api.inquire.EMBEDDINGS_AVAILABLE', True), \
         patch('src.api.inquire.call_llm_completion', side_effect=_rag_router_then('[]')), \
         patch('src.api.inquire.semantic_search_chunks', side_effect=search_se), \
         patch('src.api.inquire.call_chat_completion',
               side_effect=_capturing_chat_completion(captured)):
        resp = client.post('/api/inquire/chat', json={'message': 'what did Alice say?'})
        assert resp.status_code == 200
        events = _sse_events(resp)

    # No search call may carry a speaker filter — the auto re-search never runs.
    assert all(not f.get('speaker_names') for f in search_filters), \
        f"unexpected speaker-filtered re-search: {search_filters}"
    # And no 'filtering' status event should be emitted for a present speaker.
    statuses = [e.get('status') for e in events]
    assert 'filtering' not in statuses, f"unexpected filtering event: {events}"
