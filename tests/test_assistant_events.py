"""Ambrose phase 2a: one prompt builder for the recording chat, one event
stream for both engines, segment citations in the web panel, and step labels
built and translated on the server.

The model is mocked: no request leaves the test.

SHARED-DB: users, shares, states and recordings are removed afterwards.
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
from src.models import InternalShare, Recording, SharedRecordingState, User  # noqa: E402
from src.services import inquire_agent as ia  # noqa: E402
from src.services.assistant_events import (LIBRARY_TOOLS, SegmentCitations, library_events, parse_sse,  # noqa: E402
                                           recording_events, sse, tools_for_surface)
from src.services.assistant_present import step_label, step_result, summary_label  # noqa: E402

app.config["WTF_CSRF_ENABLED"] = False

SEGMENTS = [{"speaker": "Dana", "sentence": "Welcome back.", "start_time": 0.0, "end_time": 3.0},
            {"speaker": "Omar", "sentence": "So the budget freeze applies to travel only.", "start_time": 754.2,
             "end_time": 761.0}]
CANON = [{"index": 0, "speaker": "Dana", "text": "Welcome back.", "start_time": 0.0, "end_time": 3.0},
         {"index": 1, "speaker": "Omar", "text": "So the budget freeze applies to travel only.",
          "start_time": 754.2, "end_time": 761.0}]


def _events(chunks):
    out = []
    for chunk in chunks:
        out.extend(parse_sse(chunk))
    return out


def _text(events):
    return ''.join(e['delta'] for e in events if isinstance(e, dict) and 'delta' in e)


# ---------------------------------------------------------------------------
# Segment citations in a stream
# ---------------------------------------------------------------------------

def test_a_marker_split_across_chunks_arrives_whole_after_its_source():
    reply = 'The freeze covers travel [S1 "budget freeze applies to travel"] only.'
    raw = [sse({'delta': reply[i:i + 5]}) for i in range(0, len(reply), 5)] + [sse({'end_of_stream': True})]
    events = _events(recording_events(raw, CANON))
    assert _text(events) == reply                       # the model's own text, markers kept
    kinds = [next(iter(e)) for e in events]
    source_at = kinds.index('source')
    marker_at = next(i for i, e in enumerate(events) if 'delta' in e and '[S1' in e['delta'])
    assert source_at < marker_at
    assert '[S1 "budget' in events[marker_at]['delta'] and 'travel"]' in events[marker_at]['delta']
    source = events[source_at]['source']
    assert source == {'n': 1, 'marker': '[S1 "budget freeze applies to travel"]', 'segment_index': 1,
                      'start_time': 754.2, 'end_time': 761.0, 'speaker': 'Omar',
                      'quote': 'budget freeze applies to travel', 'verified': True}
    assert kinds[-1] == 'end_of_stream'


def test_numbering_and_quote_checks_follow_parse_sources():
    from src.services.recording_chat import parse_sources
    reply = 'Travel [S1 "budget freeze applies to travel"], hiring [S1 "freeze on hiring"] [S9 "x"], again [S1  "budget freeze applies to travel"].'
    cites = SegmentCitations(CANON)
    events = cites.feed(reply) + cites.flush()
    sources = [e['source'] for e in events if 'source' in e]
    assert [s['n'] for s in sources] == [1, 2, 3, 1]          # the respaced repeat keeps number 1
    assert [s['verified'] for s in sources] == [True, False, False, True]
    assert sources[2]['segment_index'] is None
    _, parsed = parse_sources(reply, CANON)
    assert [(p['n'], p['verified']) for p in parsed] == [(s['n'], s['verified']) for s in sources[:3]]


@pytest.mark.parametrize('reply', [
    'At [00:07:35] they agreed, and at [12:34].',          # the timestamp fallback
    'See [1], a [link](https://example.test) and [Some text].',
    'An unfinished [S3 "quote at the very end',
    '[S',
])
def test_text_without_segment_markers_passes_unchanged(reply):
    cites = SegmentCitations(CANON)
    events = []
    for i in range(0, len(reply), 3):
        events += cites.feed(reply[i:i + 3])
    events += cites.flush()
    assert _text(events) == reply
    assert not [e for e in events if 'source' in e]


def test_without_segments_the_stream_is_untouched():
    raw = [sse({'delta': 'A [S1 "x"]'}), sse({'thinking': 'hmm'}), sse({'end_of_stream': True})]
    assert list(recording_events(raw, None)) == raw


def test_held_text_is_flushed_before_an_error():
    raw = [sse({'delta': 'Cut off [S1 "budg'}), sse({'error': 'boom'})]
    events = _events(recording_events(raw, CANON))
    assert events == [{'delta': 'Cut off '}, {'delta': '[S1 "budg'}, {'error': 'boom'}]


# ---------------------------------------------------------------------------
# Library engine: retrieval stages as steps
# ---------------------------------------------------------------------------

def test_retrieval_stages_become_translated_steps():
    raw = [sse({'status': 'processing', 'message': 'Analyzing your query...'}),
           sse({'status': 'searching', 'message': 'Searching transcriptions...'}),
           sse({'delta': 'Answer'}), sse({'end_of_stream': True})]
    events = _events(library_events(raw, 'de'))
    steps = [e['agent_step'] for e in events if 'agent_step' in e]
    assert [(s['id'], s['status']) for s in steps] == [
        ('stage-1', 'running'), ('stage-1', 'done'), ('stage-2', 'running'), ('stage-2', 'done')]
    assert steps[0]['label'] == 'Frage wird gelesen' and steps[2]['label'] == 'Ihre Aufnahmen werden durchsucht'
    statuses = [e for e in events if 'status' in e]
    assert statuses[0] == {'status': 'processing', 'message': 'Frage wird gelesen'}
    assert _text(events) == 'Answer' and events[-1] == {'end_of_stream': True}


def test_agent_events_pass_through_unchanged():
    raw = [sse({'context_usage': 3}), sse({'agent_step': {'id': 1, 'status': 'running'}}),
           sse({'agent_summary': {'steps': 1, 'line': 'x', 'label': 'y'}}), sse({'end_of_stream': True})]
    assert list(library_events(raw, 'en')) == raw


# ---------------------------------------------------------------------------
# Tools per surface and the prompt that describes them
# ---------------------------------------------------------------------------

def test_tools_follow_the_surface():
    assert tools_for_surface('incognito') == []
    assert tools_for_surface('recording') == list(LIBRARY_TOOLS)
    assert tools_for_surface('ask') is None
    with pytest.raises(ValueError):
        tools_for_surface('elsewhere')


def test_the_toolbox_and_prompt_offer_only_the_surface_tools():
    every = {'summaries': True, 'notes': True}
    schemas, executors = ia.build_toolbox(every, ['search_transcripts', 'get_transcript'])
    assert [s['function']['name'] for s in schemas] == ['search_transcripts', 'get_transcript']
    assert set(executors) == {'search_transcripts', 'get_transcript'}
    assert len(ia.build_toolbox(every)[0]) == 9  # find_people is the ninth
    assert ia.build_toolbox(every, [])[0] == []
    ctx = {'name': 'A', 'title': 'x', 'company': 'y', 'assistant_name': 'Iris'}
    prompt = ia._system_prompt(ctx, '', every, '', ['search_transcripts', 'get_transcript'])
    assert prompt.startswith("You are Iris, the AI assistant built into Speakr")
    assert 'You have tools to search transcripts, and read transcripts.' in prompt
    assert "summaries" not in prompt.split("You have tools")[1].split("Work iteratively")[0]
    full = ia._system_prompt(ctx, '', {'summaries': True, 'notes': False}, '')
    assert 'find similar recordings, look up people, and read transcripts, stored summaries, and metadata' in full


# ---------------------------------------------------------------------------
# Step labels and the steps line
# ---------------------------------------------------------------------------

def test_step_labels_name_the_recording_not_its_id():
    assert step_label('get_transcript', {'recording_id': 7}, 'en', 'Weekly Sync') == \
        'Reading the transcript of Weekly Sync'
    assert step_label('get_transcript', {'recording_id': 7, 'offset': 200}, 'en', 'Weekly Sync') == \
        'Reading the transcript of Weekly Sync from segment 200'
    assert step_label('get_summary', {'recording_id': 7}, 'en') == 'Reading a summary'
    assert '7' not in step_label('get_recording_metadata', {'recording_id': 7}, 'en')
    assert step_label('search_transcripts', {'query': 'budget'}, 'en') == 'Searching for “budget”'
    assert step_label('search_transcripts', {'query': 'budget'}, 'fr') == 'Recherche de « budget »'


def test_step_results_and_the_steps_line_are_translated():
    result = {'result_count': 3, 'results': [{'recording_id': 1}, {'recording_id': 2}, {'recording_id': 2}],
              'search_type': 'semantic+keyword'}
    assert step_result('search_transcripts', result, 'en') == \
        ('done', '3 matches in 2 recordings · with keyword search')
    assert step_result('search_transcripts', {'result_count': 0}, 'es') == ('empty', 'Sin coincidencias')
    assert step_result('get_summary', {'error': 'Recording 9 is not available'}, 'en') == \
        ('error', "Couldn't complete this step")
    counts = {'search_transcripts': 3, 'get_transcript': 1}
    assert summary_label(4, counts, 'en') == '4 steps · 3 searches · 1 transcript read'
    assert summary_label(1, {'list_recordings': 1}, 'en') == '1 step · 1 listing'
    assert summary_label(4, counts, 'zh') == '4 个步骤 · 3 次搜索 · 阅读 1 份转录'


# ---------------------------------------------------------------------------
# The web panel's /chat on the one builder
# ---------------------------------------------------------------------------

@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"a2_{s}", email=f"a2_{s}@local.test", password="x", name="Jordan Avery",
                     job_title="Product Manager", company="Fernhill Labs", output_language="German",
                     assistant_name="Iris")
        guest = User(username=f"a2g_{s}", email=f"a2g_{s}@local.test", password="x")
        db.session.add_all([owner, guest])
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Weekly sync", status="COMPLETED", original_filename="a.wav",
                        participants="Dana, Omar", transcription=json.dumps(SEGMENTS), notes="OWNER SECRET NOTE")
        plain = Recording(user_id=owner.id, title="Plain", status="COMPLETED", original_filename="b.wav",
                          transcription="Dana: we froze the travel budget for the quarter.")
        db.session.add_all([rec, plain])
        db.session.commit()
        db.session.add(InternalShare(recording_id=rec.id, owner_id=owner.id, shared_with_user_id=guest.id))
        db.session.add(SharedRecordingState(recording_id=rec.id, user_id=guest.id, personal_notes="GUEST NOTE"))
        db.session.commit()
        ids = dict(owner=owner.id, guest=guest.id, rec=rec.id, plain=plain.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        SharedRecordingState.query.filter_by(recording_id=ids["rec"]).delete()
        InternalShare.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter(Recording.id.in_([ids["rec"], ids["plain"]])).delete(synchronize_session=False)
        User.query.filter(User.id.in_([ids["owner"], ids["guest"]])).delete(synchronize_session=False)
        db.session.commit()


def _stream(text):
    for i in range(0, len(text), 6):
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text[i:i + 6]),
                                                       finish_reason=None)], usage=None)


def _web_chat(uid, rec_id, reply, history=None):
    seen = {}

    def fake(*args, **kwargs):
        seen.update(kwargs)
        return _stream(reply)

    with patch("src.api.recordings.chat_client", MagicMock()), \
            patch("src.api.recordings.call_chat_completion", side_effect=fake):
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["_user_id"] = str(uid)
        resp = c.post("/chat", json={"recording_id": rec_id, "message": "What does the freeze cover?",
                                     "message_history": history or []})
        body = resp.get_data(as_text=True)
    return resp, seen, _events([body])


def test_the_web_panel_sends_the_builders_prompt(world):
    from src.services.recording_chat import build_chat_messages
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    resp, seen, _ = _web_chat(world["owner"], world["rec"], "ok", history)
    assert resp.status_code == 200
    with app.app_context():
        expected, _segments = build_chat_messages(db.session.get(Recording, world["rec"]),
                                                  db.session.get(User, world["owner"]),
                                                  "What does the freeze cover?", history, with_sources=True)
    assert seen["messages"] == expected
    system = seen["messages"][0]["content"]
    assert system.startswith("You are Iris, the AI assistant built into Speakr, ")
    assert "You are talking with Jordan Avery (Product Manager, Fernhill Labs)." in system
    assert "analyst" not in system and "a(n)" not in system
    assert "Please provide all your responses in German." in system
    assert "[S1 12:34 Omar] So the budget freeze applies to travel only." in system
    assert "Dana, Omar" in system and "OWNER SECRET NOTE" in system
    assert seen["messages"][1:3] == history
    assert seen["operation_type"] == "chat" and seen["stream"] is True and seen["user_id"] == world["owner"]


def test_a_recipient_gets_their_own_notes_and_the_default_name(world):
    with patch("src.api.recordings.has_recording_access", lambda recording, user: True):
        _, seen, _ = _web_chat(world["guest"], world["rec"], "ok")
    system = seen["messages"][0]["content"]
    assert "GUEST NOTE" in system and "OWNER SECRET NOTE" not in system
    assert system.startswith("You are Ambrose, ")


def test_the_web_panel_streams_sources_before_their_markers(world):
    reply = 'Travel only [S1 "budget freeze applies to travel"].'
    _, _, events = _web_chat(world["owner"], world["rec"], reply)
    sources = [e["source"] for e in events if "source" in e]
    assert [(s["n"], s["start_time"], s["verified"]) for s in sources] == [(1, 754.2, True)]
    assert _text(events) == reply
    assert events[-1].get("end_of_stream") is True


def test_timestamp_citations_still_reach_the_panel(world):
    reply = 'The freeze came up at [12:34].'
    _, _, events = _web_chat(world["owner"], world["rec"], reply)
    assert _text(events) == reply and not [e for e in events if "source" in e]


def test_a_plain_text_transcript_has_no_markers(world):
    _, seen, events = _web_chat(world["owner"], world["plain"], 'Yes [S1 "x"].')
    system = seen["messages"][0]["content"]
    assert "we froze the travel budget" in system and "[S0 " not in system and '[S12 "exact words"]' not in system
    assert _text(events) == 'Yes [S1 "x"].' and not [e for e in events if "source" in e]
