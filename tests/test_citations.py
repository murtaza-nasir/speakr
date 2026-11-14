"""Segment citations: one grammar for the streamed web panel and the API v1
batch reply (#605). No app or database is needed."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services import citations  # noqa: E402
from src.services.citations import SegmentCitations, _norm, parse_sources  # noqa: E402

CANON = [{"index": 0, "speaker": "Dana", "text": "Welcome back.", "start_time": 0.0, "end_time": 3.0},
         {"index": 1, "speaker": "Omar", "text": "So the budget freeze applies to travel only.",
          "start_time": 754.2, "end_time": 761.0}]

REPLIES = [
    'Travel [S1 "budget freeze applies to travel"], hiring [S1 "freeze on hiring"] [S9 "x"], '
    'again [S1  "budget freeze applies to travel"].',
    'A [S0] b [S0 "WELCOME   back"] c [S0 "welcome back"] [S1 ""] [S1] [S1 "x"] [S1 "x"] end',
    'At [00:07:35] and [1], a [link](https://example.test) [S3 "unfinished',
    'Same words twice [S0 "Welcome back."] and [S0 "Welcome back."].',
    '',
]


def _stream(reply, step):
    cites = SegmentCitations(CANON)
    events = []
    for i in range(0, len(reply), step):
        events += cites.feed(reply[i:i + step])
    return events + cites.flush()


def test_shared_parse_sources_matches_streaming_numbering():
    reply = REPLIES[0]
    events = _stream(reply, len(reply))
    streamed = [(e['source']['n'], e['source']['verified']) for e in events if 'source' in e]
    batch = [(s['n'], s['verified']) for s in parse_sources(reply, CANON)[1]]
    assert streamed == [(1, True), (2, False), (3, False), (1, True)]
    assert batch == [(1, True), (2, False), (3, False)]
    assert streamed[:3] == batch


@pytest.mark.parametrize('reply', REPLIES)
@pytest.mark.parametrize('step', [1, 4, 1000])
def test_stream_and_batch_agree_on_the_whole_reply(reply, step):
    events = _stream(reply, step)
    streamed = [e['source'] for e in events if 'source' in e]
    rewritten, batch = parse_sources(reply, CANON)
    # The stream keeps the model's text; replacing each marker with its
    # number gives the batch reply.
    text = ''.join(e['delta'] for e in events if 'delta' in e)
    assert text == reply
    numbers = {s['marker']: s['n'] for s in streamed}
    assert citations._CITE.sub(lambda m: f"[{numbers[m.group(0)]}]", text) == rewritten
    # Each streamed source is the batch source with the same number, plus
    # its marker. The quote is the marker's own spelling in the stream and
    # the first spelling in the batch, so the two agree once normalized.
    # Every batch source is streamed at least once.
    by_n = {s['n']: s for s in batch}
    assert [s['n'] for s in batch] == list(range(1, len(batch) + 1))
    for source in streamed:
        same = by_n[source['n']]
        assert {k: v for k, v in source.items() if k not in ('marker', 'quote')} == \
            {k: v for k, v in same.items() if k != 'quote'}
        assert _norm(source['quote']) == _norm(same['quote'])
    assert {s['n'] for s in streamed} == set(by_n)
    # The stream sends one event per distinct marker text.
    assert len(streamed) == len({s['marker'] for s in streamed})


def test_respaced_and_recased_quotes_share_a_number():
    _, batch = parse_sources(REPLIES[1], CANON)
    assert [(s['segment_index'], s['quote'], s['verified']) for s in batch] == [
        (0, None, False), (0, 'WELCOME   back', True), (1, '', False), (1, 'x', False)]


def test_a_missing_segment_keeps_its_number_without_segment_fields():
    rewritten, batch = parse_sources('See [S9 "x"].', CANON)
    assert rewritten == 'See [1].'
    assert batch == [{'n': 1, 'segment_index': None, 'start_time': None, 'end_time': None,
                      'speaker': None, 'quote': 'x', 'verified': False}]


def test_without_segments_every_citation_is_unverified():
    rewritten, batch = parse_sources('A [S0 "Welcome back"] b [S1].', None)
    assert rewritten == 'A [1] b [2].'
    assert [(s['segment_index'], s['verified']) for s in batch] == [(None, False), (None, False)]
    assert parse_sources(None, CANON) == ('', [])


def test_norm_collapses_whitespace_and_case():
    assert _norm('  A   B\nC  ') == 'a b c'
    assert _norm(None) == ''


def test_both_paths_use_the_shared_module():
    from src.services import assistant_events, recording_chat
    assert assistant_events.SegmentCitations is citations.SegmentCitations
    assert not hasattr(recording_chat, 'parse_sources')
    assert not hasattr(recording_chat, '_CITE') and not hasattr(assistant_events, '_CITE')
