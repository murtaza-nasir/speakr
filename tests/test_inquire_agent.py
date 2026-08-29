#!/usr/bin/env python3
"""Tests for src/services/inquire_agent.py — the agentic Inquire tool loop.

Covers: toolbox availability gating, filter scoping, notes ownership,
transcript pagination, result truncation, history sanitization/compaction,
and the agent loop itself (native tool-calling and prompted-JSON modes,
duplicate-call guard, forced finals, budget errors, legacy fallback) using a
scripted fake LLM. No network, no real LLM.

Run: docker exec speakr-dev python -m pytest tests/test_inquire_agent.py -q
"""

import json
import os
import sys
import uuid
import contextlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import User, Recording, Tag, RecordingTag  # noqa: E402
from src.services import inquire_agent as ia  # noqa: E402
from src.services.llm import TokenBudgetExceeded  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def text_chunk(s):
    return NS(choices=[NS(delta=NS(content=s, tool_calls=None))])


def tool_chunk(idx, call_id=None, name=None, args=None):
    fn = NS(name=name, arguments=args)
    return NS(choices=[NS(delta=NS(content=None, tool_calls=[NS(index=idx, id=call_id, function=fn)]))])


def nonstream_response(text):
    return NS(choices=[NS(message=NS(content=text))])


class FakeLLM:
    """Scripted stand-in for call_chat_completion.

    Each entry in `script` is either a list of stream chunks (returned as an
    iterator when stream=True), a nonstream response object, or an Exception
    to raise.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, messages=None, tools=None, tool_choice=None, temperature=None,
                 stream=False, max_tokens=None, user_id=None, operation_type=None,
                 response_format=None):
        self.calls.append({'messages': messages, 'tools': tools, 'stream': stream,
                           'operation_type': operation_type})
        if not self.script:
            raise AssertionError('FakeLLM script exhausted')
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, list):
            return iter(item)
        return item


def parse_events(sse_strings):
    out = []
    for s in sse_strings:
        assert s.startswith('data: ')
        out.append(json.loads(s[len('data: '):].strip()))
    return out


def event_types(events):
    types = []
    for e in events:
        types.append(next(iter(
            k for k in ('agent_step', 'delta', 'thinking', 'agent_summary', 'end_of_stream',
                        'context_usage', 'context_compacted', 'conversation_memory',
                        'status', 'error') if k in e), 'other'))
    return types


USER_CTX = {'name': 'Testy', 'title': 'analyst', 'company': 'ACME', 'output_language': None}

SEGMENTS = [{'speaker': f'S{i % 2}', 'sentence': f'sentence number {i}',
             'start_time': i * 2.0, 'end_time': i * 2.0 + 1.5}
            for i in range(300)]


@pytest.fixture
def env_agent():
    saved = {}
    keys = ['ENABLE_INQUIRE_AGENT', 'INQUIRE_AGENT_MAX_STEPS', 'INQUIRE_AGENT_TOOL_MODE',
            'INQUIRE_AGENT_TOOL_RESULT_TOKENS', 'INQUIRE_AGENT_TIMEOUT_SECONDS',
            'INQUIRE_CONTEXT_BUDGET_TOKENS', 'INQUIRE_DEFAULT_ALLOW_SUMMARIES',
            'INQUIRE_DEFAULT_ALLOW_NOTES']
    for k in keys:
        saved[k] = os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def data(env_agent):
    """Two users; owner has three recordings (one tagged, one older, one from
    the other user injected only for ownership tests)."""
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        owner = User(username=f'agent_o_{suffix}', email=f'ao_{suffix}@example.com', password='x')
        other = User(username=f'agent_x_{suffix}', email=f'ax_{suffix}@example.com', password='x')
        db.session.add_all([owner, other])
        db.session.flush()

        tag = Tag(name=f'agtag_{suffix}', user_id=owner.id)
        db.session.add(tag)
        db.session.flush()

        from datetime import datetime
        r1 = Recording(user_id=owner.id, title='Weekly Sync', status='COMPLETED',
                       transcription=json.dumps(SEGMENTS), summary='sum of weekly sync',
                       notes='owner notes r1', participants='Alice, Bob',
                       meeting_date=datetime(2026, 8, 20, 10, 0))
        r2 = Recording(user_id=owner.id, title='Old Planning', status='COMPLETED',
                       transcription='plain text transcript ' * 50,
                       meeting_date=datetime(2026, 1, 5, 9, 0))
        r3 = Recording(user_id=other.id, title='Foreign Rec', status='COMPLETED',
                       transcription=json.dumps(SEGMENTS[:5]), notes='not yours',
                       meeting_date=datetime(2026, 8, 21, 9, 0))
        db.session.add_all([r1, r2, r3])
        db.session.flush()
        db.session.add(RecordingTag(recording_id=r1.id, tag_id=tag.id))
        db.session.commit()

        ids = {'owner': owner.id, 'other': other.id, 'tag': tag.id,
               'r1': r1.id, 'r2': r2.id, 'r3': r3.id}
        yield ids

        RecordingTag.query.filter_by(recording_id=ids['r1']).delete()
        Recording.query.filter(Recording.id.in_([ids['r1'], ids['r2'], ids['r3']])).delete(synchronize_session=False)
        Tag.query.filter_by(id=ids['tag']).delete()
        User.query.filter(User.id.in_([ids['owner'], ids['other']])).delete(synchronize_session=False)
        db.session.commit()


# ---------------------------------------------------------------------------
# Toolbox availability
# ---------------------------------------------------------------------------

def test_toolbox_gates_notes_and_summaries():
    schemas, execs = ia.build_toolbox({'summaries': True, 'notes': True})
    assert {'get_summary', 'get_notes'} <= set(execs)
    schemas, execs = ia.build_toolbox({'summaries': False, 'notes': False})
    assert 'get_summary' not in execs and 'get_notes' not in execs
    names = [s['function']['name'] for s in schemas]
    assert 'get_notes' not in names and 'get_summary' not in names
    assert {'search_transcripts', 'list_recordings', 'get_transcript', 'get_recording_metadata'} <= set(execs)


def test_availability_env_defaults_and_user_override(env_agent, data):
    with app.app_context():
        user = db.session.get(User, data['owner'])
        # NULL columns -> env defaults (summaries on, notes off)
        assert ia.get_availability(user) == {'summaries': True, 'notes': False}
        os.environ['INQUIRE_DEFAULT_ALLOW_SUMMARIES'] = 'false'
        assert ia.get_availability(user)['summaries'] is False
        user.inquire_allow_summaries = True
        user.inquire_allow_notes = True
        assert ia.get_availability(user) == {'summaries': True, 'notes': True}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

def test_scope_respects_filters(data):
    with app.app_context():
        uid = data['owner']
        all_ids = ia.resolve_allowed_recording_ids(uid, {})
        assert {data['r1'], data['r2']} <= all_ids
        assert data['r3'] not in all_ids  # other user's recording

        tag_ids = ia.resolve_allowed_recording_ids(uid, {'tag_ids': [data['tag']]})
        assert tag_ids & {data['r1'], data['r2']} == {data['r1']}

        from datetime import date
        recent = ia.resolve_allowed_recording_ids(uid, {'date_from': date(2026, 6, 1)})
        assert data['r1'] in recent and data['r2'] not in recent

        subset = ia.resolve_allowed_recording_ids(uid, {'recording_ids': [data['r2']]})
        assert subset & {data['r1'], data['r2']} == {data['r2']}


def test_tools_refuse_out_of_scope(data):
    with app.app_context():
        ctx = ia.ToolContext(data['owner'], {}, {data['r1']}, {'summaries': True, 'notes': True})
        res = ia.tool_get_transcript(ctx, {'recording_id': data['r2']})
        assert 'error' in res and 'not available' in res['error']


def test_notes_never_on_foreign_recordings(data):
    with app.app_context():
        # Even if a foreign recording somehow lands in scope (e.g. shared),
        # notes remain owner-only.
        ctx = ia.ToolContext(data['owner'], {}, {data['r3']}, {'summaries': True, 'notes': True})
        res = ia.tool_get_notes(ctx, {'recording_id': data['r3']})
        assert 'error' in res and 'own recordings' in res['error']


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def test_get_transcript_pagination(data):
    with app.app_context():
        ctx = ia.ToolContext(data['owner'], {}, {data['r1']}, {'summaries': True, 'notes': False})
        page1 = ia.tool_get_transcript(ctx, {'recording_id': data['r1'], 'limit': 100})
        assert page1['total_segments'] == 300
        assert page1['returned_segments'] == 100
        assert page1['next_offset'] == 100
        assert 'sentence number 0' in page1['transcript']
        page3 = ia.tool_get_transcript(ctx, {'recording_id': data['r1'], 'offset': 200, 'limit': 100})
        assert page3['next_offset'] is None
        assert 'sentence number 299' in page3['transcript']


def test_get_transcript_plain_text(data):
    with app.app_context():
        ctx = ia.ToolContext(data['owner'], {}, {data['r2']}, {'summaries': True, 'notes': False})
        res = ia.tool_get_transcript(ctx, {'recording_id': data['r2']})
        assert 'total_chars' in res and res['transcript'].startswith('plain text')


def test_list_recordings_and_metadata(data):
    with app.app_context():
        allowed = {data['r1'], data['r2']}
        ctx = ia.ToolContext(data['owner'], {}, allowed, {'summaries': True, 'notes': False})
        res = ia.tool_list_recordings(ctx, {})
        assert res['total_in_scope'] == 2
        titles = [r['title'] for r in res['recordings']]
        assert titles == ['Weekly Sync', 'Old Planning']  # newest first
        res = ia.tool_list_recordings(ctx, {'participant': 'Alice'})
        assert res['total_in_scope'] == 1
        meta = ia.tool_get_recording_metadata(ctx, {'recording_id': data['r1']})
        assert meta['has_summary'] is True and meta['has_notes'] is True


def test_search_tool_scopes_and_shapes(data, monkeypatch):
    with app.app_context():
        r1 = db.session.get(Recording, data['r1'])
        chunk = NS(recording_id=r1.id, recording=r1, speaker_name='Alice',
                   start_time=63.0, chunk_index=4, content='budget talk')
        foreign = db.session.get(Recording, data['r3'])
        stray = NS(recording_id=foreign.id, recording=foreign, speaker_name=None,
                   start_time=None, chunk_index=0, content='should not appear')
        monkeypatch.setattr(ia, 'semantic_search_chunks',
                            lambda uid, q, f, k: [(chunk, 0.9), (stray, 0.8)])
        ctx = ia.ToolContext(data['owner'], {}, {data['r1'], data['r2']},
                             {'summaries': True, 'notes': False})
        res = ia.tool_search_transcripts(ctx, {'query': 'budget'})
        assert res['result_count'] == 1
        hit = res['results'][0]
        assert hit['recording_id'] == data['r1'] and hit['time'] == '1:03'


def test_search_tool_modes_and_keyword_fallback(data, monkeypatch):
    with app.app_context():
        r1 = db.session.get(Recording, data['r1'])
        kw_chunk = NS(id=990001, recording_id=r1.id, recording=r1, speaker_name='Bob',
                      start_time=10.0, chunk_index=1, content='the MR rate improved')
        sem_calls, kw_calls = [], []
        monkeypatch.setattr(ia, 'semantic_search_chunks',
                            lambda uid, q, f, k: sem_calls.append(q) or [])
        monkeypatch.setattr(ia, 'basic_text_search_chunks',
                            lambda uid, q, f, k: kw_calls.append(q) or [(kw_chunk, 0.9)])
        ctx = ia.ToolContext(data['owner'], {}, {data['r1']}, {'summaries': True, 'notes': False})

        # auto: semantic empty -> keyword fallback kicks in
        res = ia.tool_search_transcripts(ctx, {'query': 'MR rate'})
        assert res['search_type'] == 'semantic+keyword'
        assert res['result_count'] == 1 and sem_calls and kw_calls

        # keyword mode: semantic never called
        sem_calls.clear(); kw_calls.clear()
        res = ia.tool_search_transcripts(ctx, {'query': 'MR rate', 'mode': 'keyword'})
        assert res['search_type'] == 'keyword'
        assert not sem_calls and kw_calls

        # semantic mode: no fallback even when empty
        sem_calls.clear(); kw_calls.clear()
        res = ia.tool_search_transcripts(ctx, {'query': 'MR rate', 'mode': 'semantic'})
        assert res['search_type'] == 'semantic' and res['result_count'] == 0
        assert sem_calls and not kw_calls


def test_basic_text_search_ranks_before_limiting(data):
    """The pre-fix code applied LIMIT before ranking, so a rare phrase in a
    high-id chunk lost to arbitrary low-id chunks matching one common word."""
    from src.services.embeddings import basic_text_search_chunks
    from src.models import TranscriptChunk
    with app.app_context():
        made = []
        # 60 decoys that only match the common word "rate", created FIRST so
        # they occupy the low-id region the old code's LIMIT would grab.
        for i in range(60):
            c = TranscriptChunk(recording_id=data['r2'], user_id=data['owner'],
                                chunk_index=1000 + i,
                                content=f'the interest rate discussion item {i}')
            db.session.add(c)
            made.append(c)
        target = TranscriptChunk(recording_id=data['r1'], user_id=data['owner'],
                                 chunk_index=2000,
                                 content='we reviewed the MR rate for the SEC team')
        db.session.add(target)
        made.append(target)
        db.session.commit()
        try:
            results = basic_text_search_chunks(data['owner'], 'MR rate', None, 8)
            assert results, 'expected matches'
            top_chunk, top_score = results[0]
            assert top_chunk.id == target.id, 'phrase match must outrank single-word decoys'
            assert top_score > results[-1][1] or len(results) == 1
        finally:
            for c in made:
                db.session.delete(c)
            db.session.commit()


def test_api_embed_cooldown(monkeypatch):
    from src.services import embeddings as emb
    with app.app_context():
        calls = []
        class BoomClient:
            class embeddings:
                @staticmethod
                def create(**kw):
                    calls.append(1)
                    raise RuntimeError('auth failure')  # non-transient: fails fast
        monkeypatch.setattr(emb, 'get_embedding_api_client', lambda: BoomClient)
        emb._api_embed_down_until = 0
        try:
            assert emb._api_embed(['x']) == []
            assert len(calls) == 1
            assert emb._api_embed_down_until > 0
            # During cooldown the client is not touched at all
            assert emb._api_embed(['y']) == []
            assert len(calls) == 1
        finally:
            emb._api_embed_down_until = 0


def test_fmt_time_shows_hours_past_sixty_minutes():
    assert ia._fmt_time(63) == '1:03'
    assert ia._fmt_time(3599) == '59:59'
    assert ia._fmt_time(3600) == '1:00:00'
    assert ia._fmt_time(5463) == '1:31:03'
    assert ia._fmt_time(None) is None


def test_truncation_marker():
    text = 'x' * 100000
    out = ia._truncate_to_tokens(text, 100, 'More available.')
    assert len(out) < 1000
    assert 'TRUNCATED' in out and 'More available.' in out


# ---------------------------------------------------------------------------
# Segment-aware chunking
# ---------------------------------------------------------------------------

def _seg(speaker, text, start, end):
    return {'speaker': speaker, 'sentence': text, 'start_time': start, 'end_time': end}


def test_chunking_quickfire_exchange_stays_together():
    """Many short turns pack into ONE chunk with every speaker labeled inline;
    chunk boundaries are size-based, never per-turn."""
    from src.services.embeddings import chunk_transcript_segments
    segs = []
    t = 0.0
    for i in range(20):
        who = 'Alice' if i % 2 == 0 else 'Bob'
        segs.append(_seg(who, f'quick reply {i}', t, t + 1.5))
        t += 2.0
    chunks = chunk_transcript_segments(json.dumps(segs), max_chunk_chars=1400)
    assert len(chunks) == 1
    c = chunks[0]
    assert c['text'].count('Alice:') == 10 and c['text'].count('Bob:') == 10
    assert c['start_time'] == 0.0 and c['end_time'] == pytest.approx(39.5)
    assert c['speaker_name'] in ('Alice', 'Bob')


def test_chunking_dominant_speaker_and_times():
    from src.services.embeddings import chunk_transcript_segments
    segs = [
        _seg('Alice', 'point one', 10.0, 12.0),
        _seg('Alice', 'point two', 12.0, 14.0),
        _seg('Bob', 'brief interjection', 14.0, 15.0),
    ]
    chunks = chunk_transcript_segments(json.dumps(segs), max_chunk_chars=1400)
    assert len(chunks) == 1
    assert chunks[0]['speaker_name'] == 'Alice'  # dominant
    assert chunks[0]['start_time'] == 10.0 and chunks[0]['end_time'] == 15.0
    assert 'Bob: brief interjection' in chunks[0]['text']


def test_chunking_splits_on_size_with_segment_overlap():
    from src.services.embeddings import chunk_transcript_segments
    segs = [_seg('S1' if i % 2 == 0 else 'S2', f'sentence {i} ' + 'word ' * 20, i * 10.0, i * 10.0 + 8)
            for i in range(12)]
    chunks = chunk_transcript_segments(json.dumps(segs), max_chunk_chars=500)
    assert len(chunks) > 1
    # No chunk exceeds the cap; whole turns are never split across chunks
    for c in chunks:
        assert len(c['text']) <= 500 + 50
        for line in c['text'].split('\n'):
            assert line.startswith(('S1: ', 'S2: '))
    # One-segment overlap: last line of chunk N reappears as first line of N+1
    for a, b in zip(chunks, chunks[1:]):
        assert a['text'].split('\n')[-1] == b['text'].split('\n')[0]
    # Times track the segments actually in the chunk
    assert chunks[0]['start_time'] == 0.0
    assert chunks[-1]['end_time'] == segs[-1]['end_time']


def test_chunking_oversized_single_turn_is_sentence_split():
    from src.services.embeddings import chunk_transcript_segments
    monologue = ('This is a long sentence about the quarterly results. ' * 30).strip()
    segs = [_seg('Speaker', monologue, 5.0, 300.0)]
    chunks = chunk_transcript_segments(json.dumps(segs), max_chunk_chars=400)
    assert len(chunks) > 1
    for c in chunks:
        assert c['speaker_name'] == 'Speaker'
        assert c['start_time'] == 5.0 and c['end_time'] == 300.0


def test_chunking_plain_text_fallback_has_null_metadata():
    from src.services.embeddings import chunk_transcript_segments
    chunks = chunk_transcript_segments('just a plain text transcript. ' * 100, max_chunk_chars=500)
    assert len(chunks) > 1
    assert all(c['speaker_name'] is None and c['start_time'] is None for c in chunks)


def test_process_recording_chunks_populates_metadata(data, monkeypatch):
    import numpy as np
    from src.services import embeddings as emb
    with app.app_context():
        monkeypatch.setattr(emb, 'generate_embeddings',
                            lambda texts, user_id=None: [np.zeros(8, dtype=np.float32) for _ in texts])
        assert emb.process_recording_chunks(data['r1']) is True
        from src.models import TranscriptChunk
        chunks = TranscriptChunk.query.filter_by(recording_id=data['r1']).order_by(TranscriptChunk.chunk_index).all()
        assert chunks
        assert chunks[0].speaker_name in ('S0', 'S1')
        assert chunks[0].start_time == 0.0
        assert chunks[0].end_time is not None
        assert 'S0: sentence number 0' in chunks[0].content
        TranscriptChunk.query.filter_by(recording_id=data['r1']).delete()
        db.session.commit()


# ---------------------------------------------------------------------------
# History and compaction
# ---------------------------------------------------------------------------

def test_sanitize_history_folds_activity_and_drops_junk():
    history = [
        {'role': 'user', 'content': 'q1'},
        {'role': 'assistant', 'content': 'a1', 'html': '<p>a1</p>', 'activity': '2 searches'},
        {'role': 'system', 'content': 'nope'},
        {'role': 'assistant', 'content': ''},
        'garbage',
    ]
    out = ia.sanitize_history(history)
    assert len(out) == 2
    # Provenance goes into the out-of-band note, NEVER into the assistant's
    # own content (models imitate their previous answers' tails).
    assert out[1]['content'] == 'a1'
    assert 'research performed' in out[1]['note']


def test_sanitize_history_carries_displayed_citation_numbering_as_note():
    """The UI numbers citations at render time; the mapping must reach the
    model (as a system-side note) so 'tell me more about 5' resolves to what
    the user saw — without polluting the assistant content it would mimic."""
    history = [
        {'role': 'assistant', 'content': 'answer text',
         'sources': [{'n': 1, 'recording_id': 58, 'title': 'Integrating AI'},
                     {'n': 2, 'recording_id': 73, 'title': 'Impact of AI'},
                     {'n': 'junk', 'recording_id': None, 'title': 'dropped'}]},
        {'role': 'user', 'content': 'tell me more about 2'},
    ]
    out = ia.sanitize_history(history)
    assert out[0]['content'] == 'answer text'
    note = out[0]['note']
    assert 'displayed to the user numbered' in note
    assert '1 = Integrating AI (recording 58)' in note
    assert '2 = Impact of AI (recording 73)' in note
    assert 'dropped' not in note


def test_history_notes_become_system_messages_in_the_loop(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'prompt'
    history = [
        {'role': 'user', 'content': 'earlier question'},
        {'role': 'assistant', 'content': 'earlier answer', 'activity': '2 searches',
         'sources': [{'n': 1, 'recording_id': data['r1'], 'title': 'Weekly Sync'}]},
    ]
    fake = FakeLLM([nonstream_response('follow-up answer')])
    events = _run(data, fake, history=history)
    msgs = fake.calls[0]['messages']
    assistant_msgs = [m for m in msgs if m['role'] == 'assistant']
    assert assistant_msgs[0]['content'] == 'earlier answer'
    notes = [m for m in msgs if m['role'] == 'system' and 'displayed to the user numbered' in m['content']]
    assert len(notes) == 1
    # The note follows its assistant message
    idx = msgs.index(assistant_msgs[0])
    assert msgs[idx + 1] == notes[0]
    assert any(e.get('delta') == 'follow-up answer' for e in events)


def test_compact_history_uses_llm_and_keeps_tail(data, monkeypatch):
    with app.app_context():
        history = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'm{i}'}
                   for i in range(12)]
        monkeypatch.setattr(ia, 'call_chat_completion',
                            lambda **kw: nonstream_response('MEMORY BLOCK'))
        memory, kept, folded = ia.compact_history(data['owner'], None, history, keep_last=8)
        assert memory == 'MEMORY BLOCK'
        assert folded == 4 and len(kept) == 8
        assert kept[0]['content'] == 'm4'


def test_compact_history_degrades_to_truncation(data, monkeypatch):
    with app.app_context():
        history = [{'role': 'user', 'content': f'm{i}'} for i in range(12)]
        def boom(**kw):
            raise RuntimeError('llm down')
        monkeypatch.setattr(ia, 'call_chat_completion', boom)
        memory, kept, folded = ia.compact_history(data['owner'], 'old mem', history, keep_last=8)
        assert 'truncated' in memory.lower() and 'old mem' in memory
        assert len(kept) == 8 and folded == 4


# ---------------------------------------------------------------------------
# The loop (native mode)
# ---------------------------------------------------------------------------

def _run(data, fake, history=None, memory=None, filters=None, fallback=None):
    gen = ia.run_inquire_agent(app, data['owner'], USER_CTX, 'what happened?',
                               history or [], memory, filters or {},
                               legacy_fallback=fallback, llm_call=fake)
    return parse_events(list(gen))


def test_loop_tool_call_then_answer(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'native'
    args = json.dumps({'recording_id': data['r1'], 'limit': 5})
    fake = FakeLLM([
        [tool_chunk(0, 'c1', 'get_transcript', args)],
        [text_chunk('The answer is ' * 30)],  # >200 chars so it live-streams
    ])
    events = _run(data, fake)
    types = event_types(events)
    steps = [e['agent_step'] for e in events if 'agent_step' in e]
    assert steps[0]['status'] == 'running' and steps[1]['status'] == 'done'
    assert 'delta' in types and types[-1] == 'end_of_stream'
    summary = next(e['agent_summary'] for e in events if 'agent_summary' in e)
    assert summary['steps'] == 1 and 'transcript read' in summary['line']
    # The tool result was appended as a tool message on the second call
    second_call = fake.calls[1]['messages']
    assert any(m.get('role') == 'tool' for m in second_call)


def test_loop_duplicate_call_served_from_cache(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'native'
    args = json.dumps({'recording_id': data['r1'], 'limit': 5})
    fake = FakeLLM([
        [tool_chunk(0, 'c1', 'get_transcript', args)],
        [tool_chunk(0, 'c2', 'get_transcript', args)],
        [text_chunk('done ' * 50)],
    ])
    events = _run(data, fake)
    third_call = fake.calls[2]['messages']
    tool_msgs = [m for m in third_call if m.get('role') == 'tool']
    assert len(tool_msgs) == 2
    assert 'duplicate call' in tool_msgs[1]['content']
    summary = next(e['agent_summary'] for e in events if 'agent_summary' in e)
    assert summary['steps'] == 1  # cached call doesn't count as a new step


def test_loop_unknown_tool_and_bad_args_recover(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'native'
    fake = FakeLLM([
        [tool_chunk(0, 'c1', 'no_such_tool', '{}')],
        [tool_chunk(0, 'c2', 'get_transcript', 'NOT JSON')],
        [text_chunk('recovered fine, here is the answer. ' * 10)],
    ])
    events = _run(data, fake)
    steps = [e['agent_step'] for e in events if 'agent_step' in e and e['agent_step'].get('status') not in ('running',)]
    assert all(s['status'] == 'error' for s in steps)
    assert event_types(events)[-1] == 'end_of_stream'
    assert any('delta' in e for e in events)


def test_loop_forced_final_at_max_steps(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'native'
    os.environ['INQUIRE_AGENT_MAX_STEPS'] = '1'
    args = json.dumps({'recording_id': data['r1'], 'limit': 5})
    fake = FakeLLM([
        [tool_chunk(0, 'c1', 'get_transcript', args)],
        [text_chunk('forced final answer with evidence so far. ' * 10)],
    ])
    events = _run(data, fake)
    # The forcing call must NOT offer tools
    assert fake.calls[0]['tools'] is not None
    assert fake.calls[1]['tools'] is None
    forcing_msgs = [m for m in fake.calls[1]['messages'] if m.get('role') == 'system']
    assert any('Step budget exhausted' in m['content'] for m in forcing_msgs)
    assert event_types(events)[-1] == 'end_of_stream'


def test_loop_budget_exceeded_event(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'native'
    fake = FakeLLM([TokenBudgetExceeded('over budget', 101)])
    events = _run(data, fake)
    assert events[-1].get('budget_exceeded') is True


def test_loop_fallback_to_legacy(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'native'
    fake = FakeLLM([RuntimeError('connection refused')])
    def legacy():
        yield 'data: {"delta": "legacy says hi"}\n\n'
        yield 'data: {"end_of_stream": true}\n\n'
    events = _run(data, fake, fallback=legacy)
    assert any(e.get('delta') == 'legacy says hi' for e in events)


def test_loop_auto_switches_to_prompt_mode(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'auto'
    ia._NATIVE_TOOLS_SUPPORTED.clear()
    tool_call = json.dumps({'tool': 'get_transcript',
                            'arguments': {'recording_id': data['r1'], 'limit': 5}})
    fake = FakeLLM([
        RuntimeError("400: 'tools' is not supported by this endpoint"),
        nonstream_response(tool_call),
        nonstream_response('final answer from prompt mode'),
    ])
    events = _run(data, fake)
    assert any(e.get('delta') == 'final answer from prompt mode' for e in events)
    steps = [e['agent_step'] for e in events if 'agent_step' in e]
    assert steps and steps[-1]['status'] == 'done'
    # remembered for the session
    assert False in ia._NATIVE_TOOLS_SUPPORTED.values()
    ia._NATIVE_TOOLS_SUPPORTED.clear()


def test_prompt_mode_fenced_json_and_retry(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'prompt'
    good = '```json\n' + json.dumps({'tool': 'list_recordings', 'arguments': {}}) + '\n```'
    fake = FakeLLM([
        nonstream_response('{ this is not valid json'),
        nonstream_response(good),
        nonstream_response('the final answer'),
    ])
    events = _run(data, fake)
    # retry instruction was injected after the parse failure
    retry_msgs = [m for m in fake.calls[1]['messages'] if m.get('role') == 'system']
    assert any('not valid JSON' in m['content'] for m in retry_msgs)
    assert any(e.get('delta') == 'the final answer' for e in events)


def test_prompt_mode_strips_think_tags(data):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'prompt'
    fake = FakeLLM([nonstream_response('<think>secret reasoning</think>clean answer')])
    events = _run(data, fake)
    delta = next(e['delta'] for e in events if 'delta' in e)
    assert delta == 'clean answer'


def test_loop_compaction_triggers_and_reports(data, monkeypatch):
    os.environ['INQUIRE_AGENT_TOOL_MODE'] = 'prompt'
    os.environ['INQUIRE_CONTEXT_BUDGET_TOKENS'] = '4000'  # floor value, easy to exceed
    history = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'long ' * 400}
               for i in range(12)]
    monkeypatch.setattr(ia, 'compact_history',
                        lambda uid, mem, hist, keep_last=8: ('COMPACTED', hist[-8:], len(hist) - 8))
    fake = FakeLLM([nonstream_response('answer after compaction')])
    events = _run(data, fake, history=history)
    compacted = next(e['context_compacted'] for e in events if 'context_compacted' in e)
    assert compacted['memory'] == 'COMPACTED' and compacted['keep_last'] == 8
    assert any(e.get('conversation_memory') == 'COMPACTED' for e in events)
    usage = next(e['context_usage'] for e in events if 'context_usage' in e)
    assert usage > 50


def test_thinking_filter_streaming():
    f = ia._ThinkingFilter()
    vis1, th1 = f.feed('Hello <thi')
    vis2, th2 = f.feed('nk>hidden</think> world')
    vis3, th3 = f.flush()
    assert (vis1 + vis2 + vis3) == 'Hello  world'
    assert th2 == 'hidden' or th1 == 'hidden' or th3 == 'hidden'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
