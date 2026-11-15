"""The segment-citation grammar both assistant engines use.

The model cites a transcript segment by its marker and a short exact quote,
written as [S12 "exact words"]. The recording engine answers in one call and
the web panel streams that answer, so the same grammar is used two ways:

  parse_sources(reply, segments)
      the batch path: rewrite each citation as [n] and return the numbered
      sources (the API's with_sources reply).
  SegmentCitations(segments)
      the streaming path: send a `source` event before the delta that
      carries its marker, holding a marker split across chunks until it is
      complete.

Both number a citation by its segment index and normalized quote, so a repeat
with different spacing keeps its number, and both check the quote against the
same segment the same way. One owner, so the two paths cannot disagree about
what a citation is or which number it gets.
"""

import re

_CITE = re.compile(r'\[S(\d+)(?:\s+"([^"\]]*)")?\]')
# What the start of an unfinished citation can look like: "[", "[S", "[S12",
# "[S12 ", '[S12 "some wor', '[S12 "some words"'.
_PARTIAL = re.compile(r'\[(?:S(?:\d+(?:\s+(?:"[^"\]]*"?)?)?)?)?\Z')
_MAX_HELD = 400


def _norm(text):
    return re.sub(r'\s+', ' ', (text or '')).strip().lower()


class _CitationNumbers:
    """Numbers citations in the order they first appear and describes each
    one against its segment. A repeated citation keeps its number; an index
    no segment carries still gets one, with the segment fields empty."""

    def __init__(self, segments):
        self.by_index = {seg['index']: seg for seg in segments or []}
        self.numbers = {}

    def cite(self, index, quote):
        """(first_time, source) for one [Sindex "quote"] citation."""
        key = (index, _norm(quote))
        first_time = key not in self.numbers
        if first_time:
            self.numbers[key] = len(self.numbers) + 1
        seg = self.by_index.get(index)
        return first_time, {
            'n': self.numbers[key],
            'segment_index': index if seg else None,
            'start_time': seg['start_time'] if seg else None,
            'end_time': seg['end_time'] if seg else None,
            'speaker': seg['speaker'] if seg else None,
            'quote': quote,
            'verified': bool(seg and quote and _norm(quote) in _norm(seg['text'])),
        }


def parse_sources(reply, segments):
    """Replace [S12 "quote"] citations with [n]; returns (reply, sources)."""
    numbered = _CitationNumbers(segments)
    sources = []

    def _replace(match):
        first_time, source = numbered.cite(int(match.group(1)), match.group(2))
        if first_time:
            sources.append(source)
        return f'[{source["n"]}]'

    return _CITE.sub(_replace, reply or ''), sources


class SegmentCitations:
    """Watches streamed answer text for [S12 "quote"] citations.

    feed() returns the payloads to send for a piece of text: a `source` event
    for each new citation, then the text itself as a `delta`. The text keeps
    the model's own markers, so the history the browser sends back is what
    the model wrote; the browser shows each marker as its number. A marker
    split across chunks is held back until it is complete.

    Numbering and quote checks are the same as parse_sources, which is the
    batch form of this grammar.
    """

    def __init__(self, segments):
        self.citations = _CitationNumbers(segments)
        self.markers = set()
        self.held = ''

    def _source(self, match):
        _first_time, source = self.citations.cite(int(match.group(1)), match.group(2))
        return dict(source, marker=match.group(0))

    def feed(self, text):
        buf = self.held + (text or '')
        self.held = ''
        out, events = [], []
        while buf:
            i = buf.find('[')
            if i == -1:
                out.append(buf)
                break
            out.append(buf[:i])
            rest = buf[i:]
            match = _CITE.match(rest)
            if match:
                if match.group(0) not in self.markers:
                    self.markers.add(match.group(0))
                    if out and ''.join(out):
                        events.append({'delta': ''.join(out)})
                        out = []
                    events.append({'source': self._source(match)})
                out.append(match.group(0))
                buf = rest[match.end():]
                continue
            if _PARTIAL.match(rest) and len(rest) < _MAX_HELD:
                self.held = rest
                break
            out.append('[')
            buf = rest[1:]
        text_out = ''.join(out)
        if text_out:
            events.append({'delta': text_out})
        return events

    def flush(self):
        held, self.held = self.held, ''
        return [{'delta': held}] if held else []
