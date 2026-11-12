"""Ambrose phase 2b: library tools on an open recording.

With Inquire and its agent on, the recording panel keeps the whole
transcript in the prompt (the 2a builder, unchanged) and also offers the
library tools over every recording the user can access. The panel has no
filters (they belong to the Ask page): filter fields in its request are
ignored, and access is the only limit.
Tools per surface: incognito none, the open recording the library tools, the
Ask page all. With the agent off, or when the endpoint rejects tool calls,
the answer is the transcript-only one (a rejected call is retried once
without tools). The model is mocked: no request leaves the test.

SHARED-DB: users, tags, shares and recordings are removed afterwards.
"""

import json
import os
import secrets
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import Recording, RecordingTag, SystemSetting, Tag, User  # noqa: E402
from src.services import inquire_agent as ia  # noqa: E402
from src.services.assistant_events import LIBRARY_TOOLS, panel_history, parse_sse  # noqa: E402
from src.services.recording_chat import build_chat_messages, transcript_cut  # noqa: E402

app.config["WTF_CSRF_ENABLED"] = False

SEGMENTS = [{"speaker": "Dana", "sentence": "Welcome back.", "start_time": 0.0, "end_time": 3.0},
            {"speaker": "Omar", "sentence": "So the budget freeze applies to travel only.", "start_time": 754.2,
             "end_time": 761.0}]
QUESTION = "Did we discuss the freeze anywhere else?"


def _chunk(content=None, calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=calls),
                                                    finish_reason=None)], usage=None)


def _text(reply):
    return [_chunk(reply[i:i + 6]) for i in range(0, len(reply), 6)]


def _call(name, args, call_id='c1'):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args))
    return [_chunk(calls=[SimpleNamespace(index=0, id=call_id, function=fn)])]


class FakeLLM:
    """Stands in for call_chat_completion: each script item is a list of
    stream chunks, or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def __call__(self, messages=None, tools=None, **kwargs):
        self.calls.append(dict(kwargs, messages=messages, tools=tools))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return iter(item)


@pytest.fixture
def agent_on(monkeypatch):
    monkeypatch.setenv("ENABLE_INQUIRE_AGENT", "true")
    monkeypatch.setenv("INQUIRE_AGENT_TOOL_MODE", "auto")
    monkeypatch.delenv("INQUIRE_DEFAULT_ALLOW_NOTES", raising=False)
    monkeypatch.delenv("INQUIRE_DEFAULT_ALLOW_SUMMARIES", raising=False)
    monkeypatch.setattr("src.api.recordings.ENABLE_INQUIRE_MODE", True)
    ia._NATIVE_TOOLS_SUPPORTED.clear()
    yield
    ia._NATIVE_TOOLS_SUPPORTED.clear()


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"b2_{s}", email=f"b2_{s}@local.test", password="x", name="Jordan Avery",
                     job_title="Product Manager", company="Fernhill Labs", assistant_name="Iris")
        stranger = User(username=f"b2x_{s}", email=f"b2x_{s}@local.test", password="x")
        db.session.add_all([owner, stranger])
        db.session.commit()
        tag = Tag(name=f"budget_{s}", user_id=owner.id)
        db.session.add(tag)
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Weekly sync", status="COMPLETED", original_filename="a.wav",
                        participants="Dana, Omar", transcription=json.dumps(SEGMENTS))
        tagged = Recording(user_id=owner.id, title="Budget review", status="COMPLETED", original_filename="b.wav",
                           transcription=json.dumps(SEGMENTS), summary="The freeze, again.")
        untagged = Recording(user_id=owner.id, title="Hiring plan", status="COMPLETED", original_filename="c.wav",
                             transcription=json.dumps(SEGMENTS), summary="Hiring.")
        foreign = Recording(user_id=stranger.id, title="Not yours", status="COMPLETED", original_filename="d.wav",
                            transcription=json.dumps(SEGMENTS), summary="SECRET SUMMARY")
        db.session.add_all([rec, tagged, untagged, foreign])
        db.session.commit()
        db.session.add(RecordingTag(recording_id=tagged.id, tag_id=tag.id))
        db.session.commit()
        ids = dict(owner=owner.id, stranger=stranger.id, tag=tag.id, rec=rec.id, tagged=tagged.id,
                   untagged=untagged.id, foreign=foreign.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        recs = [ids[k] for k in ("rec", "tagged", "untagged", "foreign")]
        RecordingTag.query.filter(RecordingTag.recording_id.in_(recs)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(recs)).delete(synchronize_session=False)
        Tag.query.filter_by(id=ids["tag"]).delete()
        User.query.filter(User.id.in_([ids["owner"], ids["stranger"]])).delete(synchronize_session=False)
        db.session.commit()


def _post(uid, url, body, fake):
    with patch("src.api.recordings.chat_client", MagicMock()), \
            patch("src.api.recordings.call_chat_completion", side_effect=fake):
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["_user_id"] = str(uid)
        resp = c.post(url, json=body)
        events = parse_sse(resp.get_data(as_text=True))
    return resp, events


def _panel(world, fake, **extra):
    body = {"recording_id": world["rec"], "message": QUESTION, "message_history": []}
    body.update(extra)
    return _post(world["owner"], "/chat", body, fake)


def _answer(events):
    return ''.join(e['delta'] for e in events if isinstance(e, dict) and 'delta' in e)


def _builder_messages(world, history=None):
    with app.app_context():
        messages, _ = build_chat_messages(db.session.get(Recording, world["rec"]),
                                          db.session.get(User, world["owner"]), QUESTION,
                                          history or [], with_sources=True)
    return messages


def _tool_names(call):
    return [t['function']['name'] for t in call['tools'] or []]


# ---------------------------------------------------------------------------
# Tools per surface
# ---------------------------------------------------------------------------

def test_the_open_recording_offers_the_library_tools_and_keeps_its_prompt(world, agent_on):
    fake = FakeLLM(_text('Only here [S1 "budget freeze applies to travel"]. ' * 6))
    resp, events = _panel(world, fake)
    assert resp.status_code == 200
    call = fake.calls[0]
    # Notes are off by default (INQUIRE_DEFAULT_ALLOW_NOTES), so get_notes is not offered.
    assert _tool_names(call) == [t for t in LIBRARY_TOOLS if t != 'get_notes']
    builder = _builder_messages(world)
    # The builder's prompt, history and question unchanged; one tools note after the prompt.
    assert call['messages'][0] == builder[0]
    assert call['messages'][2:] == builder[1:]
    note = call['messages'][1]
    assert note['role'] == 'system'
    assert note['content'].startswith("You have tools to search transcripts, list recordings, read transcripts, "
                                      "read stored summaries, and read metadata in Jordan Avery's recording library.")
    assert "own notes" not in note['content']
    assert '[S12 "exact words"]' in note['content'] and '/recordings/<recording_id>' in note['content']
    assert call['operation_type'] == 'chat'
    assert [e['source']['n'] for e in events if 'source' in e] == [1]
    assert events[-1].get('end_of_stream') is True


def test_with_the_agent_off_the_panel_has_no_tools(world, monkeypatch):
    monkeypatch.delenv("ENABLE_INQUIRE_AGENT", raising=False)
    monkeypatch.setattr("src.api.recordings.ENABLE_INQUIRE_MODE", True)
    fake = FakeLLM(_text('Plain answer.'))
    _, events = _panel(world, fake)
    assert fake.calls[0]['tools'] is None and 'tools' not in {k for k, v in fake.calls[0].items() if v}
    assert fake.calls[0]['messages'] == _builder_messages(world)
    assert _answer(events) == 'Plain answer.'


def test_incognito_gets_no_tools(world, agent_on):
    fake = FakeLLM(_text('Incognito answer.'))
    with patch("src.api.recordings.ENABLE_INCOGNITO_MODE", True):
        resp, events = _post(world["owner"], "/api/recordings/incognito/chat",
                             {"transcription": json.dumps(SEGMENTS), "message": QUESTION}, fake)
    assert resp.status_code == 200
    assert fake.calls[0]['tools'] is None
    assert [m['role'] for m in fake.calls[0]['messages']] == ['system', 'user']
    assert 'You have tools' not in fake.calls[0]['messages'][0]['content']
    assert _answer(events) == 'Incognito answer.'


def test_the_ask_page_offers_every_tool(world, agent_on):
    from src.services.assistant_events import tools_for_surface
    assert tools_for_surface('ask') is None
    every = {'summaries': True, 'notes': True}
    assert len(ia.build_toolbox(every, tools_for_surface('ask'))[0]) == 6
    assert [s['function']['name'] for s in ia.build_toolbox(every, tools_for_surface('recording'))[0]] == \
        list(LIBRARY_TOOLS)
    assert ia.build_toolbox(every, tools_for_surface('incognito'))[0] == []


# ---------------------------------------------------------------------------
# No filters on the panel: access is the only limit
# ---------------------------------------------------------------------------

def _tool_result(call):
    return json.loads(next(m for m in call['messages'] if m.get('role') == 'tool')['content'])


def test_a_tool_outside_access_gets_nothing(world, agent_on):
    fake = FakeLLM(_call('get_summary', {'recording_id': world['foreign']}),
                   _text('Nothing found elsewhere, as far as I can see in your recordings.' * 3))
    _, events = _panel(world, fake)
    result = _tool_result(fake.calls[1])
    assert 'not available' in result['error']
    assert 'SECRET SUMMARY' not in json.dumps(fake.calls[1]['messages'])
    step = [e['agent_step'] for e in events if 'agent_step' in e][-1]
    assert step['status'] == 'error'


@pytest.mark.parametrize('target, summary', [('tagged', 'The freeze, again.'), ('untagged', 'Hiring.')])
def test_a_tool_reads_any_recording_the_user_can_access(world, agent_on, target, summary):
    title = {'tagged': 'Budget review', 'untagged': 'Hiring plan'}[target]
    fake = FakeLLM(_call('get_summary', {'recording_id': world[target]}),
                   _text('Yes, in [%s](/recordings/%d) as well. ' % (title, world[target]) * 4))
    _, events = _panel(world, fake)
    assert _tool_result(fake.calls[1])['summary'] == summary
    steps = [e['agent_step'] for e in events if 'agent_step' in e]
    assert steps[0]['label'] == 'Reading the summary of %s' % title and steps[-1]['status'] == 'done'
    assert any('agent_summary' in e for e in events)
    assert '](/recordings/%d)' % world[target] in _answer(events)


def test_filter_fields_sent_to_the_panel_are_ignored(world, agent_on):
    """A request that still carries the Ask page's filter keys (a tag that
    leaves "Hiring plan" out, a date that does not parse) is answered over
    the whole accessible library."""
    fake = FakeLLM(_call('get_summary', {'recording_id': world['untagged']}),
                   _text('Yes, in [Hiring plan](/recordings/%d) too. ' % world['untagged'] * 4))
    resp, events = _panel(world, fake, filter_tags=[world["tag"]], filter_date_from='not a date')
    assert resp.status_code == 200
    assert _tool_result(fake.calls[1])['summary'] == 'Hiring.'
    assert events[-1].get('end_of_stream') is True


def test_the_ask_page_refuses_a_bad_filter_date(world, agent_on):
    with patch("src.api.inquire.ENABLE_INQUIRE_MODE", True), patch("src.api.inquire.client", MagicMock()):
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["_user_id"] = str(world["owner"])
        resp = c.post('/api/inquire/chat', json={'message': QUESTION, 'filter_date_from': 'not a date'})
    assert resp.status_code == 400 and resp.get_json()['code'] == 'invalid_filter_date'


# ---------------------------------------------------------------------------
# Fallback: a rejected tool call is retried once without tools
# ---------------------------------------------------------------------------

def test_a_rejected_tool_call_is_retried_once_without_tools(world, agent_on):
    fake = FakeLLM(RuntimeError('400: tools are not supported by this model'), _text('From the transcript.'))
    _, events = _panel(world, fake)
    assert len(fake.calls) == 2
    assert fake.calls[0]['tools'] and fake.calls[1]['tools'] is None
    assert fake.calls[1]['messages'] == _builder_messages(world)
    assert _answer(events) == 'From the transcript.'
    # The endpoint is remembered: the next question goes straight to the transcript-only answer.
    fake2 = FakeLLM(_text('Again from the transcript.'))
    _, events = _panel(world, fake2)
    assert len(fake2.calls) == 1 and fake2.calls[0]['tools'] is None
    assert _answer(events) == 'Again from the transcript.'


def test_any_agent_failure_before_an_answer_falls_back_once(world, agent_on):
    fake = FakeLLM(_call('search_transcripts', {'query': 'freeze'}), ValueError('boom'),
                   _text('Transcript-only answer.'))
    with patch('src.services.inquire_agent.semantic_search_chunks', return_value=[]), \
            patch('src.services.inquire_agent.basic_text_search_chunks', return_value=[]):
        _, events = _panel(world, fake)
    assert len(fake.calls) == 3 and fake.calls[2]['tools'] is None
    assert _answer(events) == 'Transcript-only answer.'


# ---------------------------------------------------------------------------
# Long recordings: page the rest with get_transcript
# ---------------------------------------------------------------------------

def test_a_long_transcript_is_paged_with_markers(world, agent_on):
    long_segments = [{"speaker": "Dana", "sentence": f"Line {i} about the plan.", "start_time": i * 5.0,
                      "end_time": i * 5.0 + 4} for i in range(60)]
    with app.app_context():
        rec = db.session.get(Recording, world["rec"])
        rec.transcription = json.dumps(long_segments)
        previous = SystemSetting.get_setting('transcript_length_limit', 30000)
        SystemSetting.set_setting('transcript_length_limit', 600, setting_type='integer')
        db.session.commit()
    try:
        fake = FakeLLM(_call('get_transcript', {'recording_id': world['rec'], 'offset': 20, 'limit': 2}),
                       _text('Later on [S21 "Line 21 about the plan"]. ' * 6))
        _, events = _panel(world, fake, filter_tags=[world["tag"]])
        system = fake.calls[0]['messages'][0]['content']
        assert len(system.split('<<start transcript>>')[1].split('<<end transcript>>')[0]) <= 602
        with app.app_context():
            cut = transcript_cut(None, [dict(s, index=i, text=s['sentence']) for i, s in enumerate(long_segments)],
                                 600)
        note = fake.calls[0]['messages'][1]['content']
        assert f"offset {cut['offset']}" in note and f"recording_id {world['rec']}" in note
        page = _tool_result(fake.calls[1])
        # The open recording is readable past the cut, even outside the filter, with markers.
        assert page['transcript'].splitlines()[0] == '[S20 01:40 Dana] Line 20 about the plan.'
        sources = [e['source'] for e in events if 'source' in e]
        assert sources[0]['segment_index'] == 21 and sources[0]['verified'] is True
    finally:
        with app.app_context():
            SystemSetting.set_setting('transcript_length_limit', previous, setting_type='integer')
            db.session.commit()


def test_without_the_agent_a_long_transcript_is_still_truncated(world, monkeypatch):
    monkeypatch.delenv("ENABLE_INQUIRE_AGENT", raising=False)
    with app.app_context():
        previous = SystemSetting.get_setting('transcript_length_limit', 30000)
        SystemSetting.set_setting('transcript_length_limit', 40, setting_type='integer')
        db.session.commit()
    try:
        fake = FakeLLM(_text('ok'))
        _panel(world, fake)
        system = fake.calls[0]['messages'][0]['content']
        assert len(system.split('<<start transcript>>\n')[1].split('\n<<end transcript>>')[0]) == 40
        assert len(fake.calls[0]['messages']) == 2
    finally:
        with app.app_context():
            SystemSetting.set_setting('transcript_length_limit', previous, setting_type='integer')
            db.session.commit()


# ---------------------------------------------------------------------------
# Citation numbering continues through a chat
# ---------------------------------------------------------------------------

def test_the_displayed_numbering_reaches_the_model_as_a_note():
    history = [
        {'role': 'user', 'content': 'What about travel?', 'sources': [{'n': 9}]},
        {'role': 'assistant', 'content': 'Travel only [S1 "budget freeze"].', 'html': '<p>x</p>',
         'sources': [{'n': 3, 'segment_index': 1, 'start_time': 754.2, 'speaker': 'Omar', 'quote': 'budget freeze'},
                     {'n': 4, 'recording_id': 12, 'title': 'Budget review'}]},
        {'role': 'assistant', 'content': 'No sources here.'},
        {'role': 'system', 'content': 'dropped'},
    ]
    out = panel_history(history)
    assert out[0] == {'role': 'user', 'content': 'What about travel?'}
    assert out[1] == {'role': 'assistant', 'content': 'Travel only [S1 "budget freeze"].'}
    assert out[2]['role'] == 'system'
    assert '3 = segment S1 of this recording at 12:34 (Omar) "budget freeze"' in out[2]['content']
    assert '4 = Budget review (recording 12)' in out[2]['content']
    assert out[3] == {'role': 'assistant', 'content': 'No sources here.'} and len(out) == 4
    # Plain history is passed as it is, so the 2a prompt is unchanged.
    plain = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}]
    assert panel_history(plain) == plain


def test_the_ask_page_note_keeps_its_wording():
    out = ia.sanitize_history([{'role': 'assistant', 'content': 'a',
                                'sources': [{'n': 5, 'recording_id': 7, 'title': 'Weekly'}]}])
    assert out[0]['note'].endswith('citations were displayed to the user numbered: 5 = Weekly (recording 7)')


def test_an_ask_download_keeps_the_numbers_the_page_showed():
    from src.api.inquire import _inquire_download_turns
    turns = _inquire_download_turns([{
        'role': 'assistant', 'content': 'Again [Weekly @ 1:02](/recordings/7?t=62), new [Other](/recordings/9).',
        'sources': [{'n': 2, 'title': 'Weekly', 'url': '/recordings/7'},
                    {'n': 5, 'title': 'Other', 'url': '/recordings/9'},
                    {'title': 'No number', 'url': '/recordings/11'}]}])
    role, content, sources = turns[0]
    assert sources == [(2, 'Weekly', '/recordings/7'), (5, 'Other', '/recordings/9'), (3, 'No number', '/recordings/11')]
    assert content == 'Again [2 @ 1:02](/recordings/7?t=62), new [5](/recordings/9).'
