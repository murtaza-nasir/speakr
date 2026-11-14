"""Segment citations in assistant answers: one grammar for the streaming
web panel and the API v1 batch reply (#605).

With segment markers in the transcript ([S12 12:34 Dana]), the model cites
[S12 "exact words"]. Each distinct (segment index, normalized quote) gets
the next number, and a quote is verified when its normalized text occurs in
its segment's normalized text. A citation of a segment that does not exist
keeps its number, with no segment fields and verified False.

Two entry points share that numbering:

  parse_sources(reply, segments)  the finished answer (API v1): each marker
                                  becomes [n], one source per number.
  SegmentCitations(segments)      the streamed answer (web panel): the text
                                  keeps the model's markers, and each
                                  distinct marker text gets one `source`
                                  event, so two spellings of one citation
                                  both reach the browser with the same n,
                                  each with its own quote text.

The browser's copy of the marker syntax is
static/js/modules/assistant/citations.js.
"""

import re

_CITE = re.compile(r'\[S(\d+)(?:\s+"([^"\]]*)")?\]')
# What the start of an unfinished citation can look like: "[", "[S", "[S12",
# "[S12 ", '[S12 "some wor', '[S12 "some words"'. It follows _CITE's syntax.
_PARTIAL = re.compile(r'\[(?:S(?:\d+(?:\s+(?:"[^"\]]*"?)?)?)?)?\Z')
_MAX_HELD = 400


def _norm(text):
    return re.sub(r'\s+', ' ', (text or '')).strip().lower()


class _Numbering:
    """Numbers citations by (index, normalized quote) and checks each quote."""

    def __init__(self, segments):
        self.by_index = {seg['index']: seg for seg in segments or []}
        self.numbers = {}

    def source(self, match):
        """(is_new, source) for one _CITE match."""
        index, quote = int(match.group(1)), match.group(2)
        key = (index, _norm(quote))
        is_new = key not in self.numbers
        if is_new:
            self.numbers[key] = len(self.numbers) + 1
        seg = self.by_index.get(index)
        return is_new, {
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
    numbering = _Numbering(segments)
    sources = []

    def _replace(match):
        is_new, source = numbering.source(match)
        if is_new:
            sources.append(source)
        return f"[{source['n']}]"

    return _CITE.sub(_replace, reply or ''), sources


class SegmentCitations:
    """Watches streamed answer text for [S12 "quote"] citations.

    feed() returns the payloads to send for a piece of text: a `source` event
    for each new marker, then the text itself as a `delta`. The text keeps
    the model's own markers, so the history the browser sends back is what
    the model wrote; the browser shows each marker as its number. A marker
    split across chunks is held back until it is complete.
    """

    def __init__(self, segments):
        self.numbering = _Numbering(segments)
        self.markers = set()
        self.held = ''

    def _source(self, match):
        _, source = self.numbering.source(match)
        return {'n': source.pop('n'), 'marker': match.group(0), **source}

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
