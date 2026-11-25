"""Split streamed model output into visible and thinking parts.

The model may wrap its reasoning in ``<think>`` / ``<thinking>`` tags. The
filter keeps partial tags buffered across chunks, so a tag split between two
stream events is not leaked as visible text, and returns the pieces in the
order they arrived.
"""

import re


class ThinkingFilter:
    """Splits streamed content into visible/thinking parts (<think> tags)."""

    _OPEN = re.compile(r'<think(?:ing)?>', re.IGNORECASE)
    _CLOSE = re.compile(r'</think(?:ing)?>', re.IGNORECASE)

    def __init__(self):
        self.buffer = ''
        self.in_thinking = False
        self.thinking = ''

    def feed(self, fragment):
        """Returns (visible_text, thinking_completed_or_None)."""
        events = self.feed_events(fragment)
        visible = ''.join(text for kind, text in events if kind == 'delta')
        thinking = None
        for kind, text in events:
            if kind == 'thinking':
                thinking = text
        return visible, thinking

    def feed_events(self, fragment):
        """Returns ordered ``(kind, text)`` pairs for one fragment.

        ``kind`` is ``'delta'`` for visible text or ``'thinking'`` for a
        completed thinking block. Keeping the order lets a caller stream the
        events without moving post-thinking text before the thinking block.
        """
        self.buffer += fragment
        events = []
        while True:
            if not self.in_thinking:
                m = self._OPEN.search(self.buffer)
                if m:
                    if m.start():
                        events.append(('delta', self.buffer[:m.start()]))
                    self.buffer = self.buffer[m.end():]
                    self.in_thinking = True
                    self.thinking = ''
                else:
                    # Hold back a partial "<think" prefix at the tail.
                    safe_len = len(self.buffer)
                    tail = self.buffer[-9:]
                    lt = tail.rfind('<')
                    if lt != -1 and self._OPEN.match(tail[lt:]) is None and '<think'.startswith(tail[lt:lt + 6].lower()):
                        safe_len = len(self.buffer) - (len(tail) - lt)
                    if safe_len:
                        events.append(('delta', self.buffer[:safe_len]))
                    self.buffer = self.buffer[safe_len:]
                    break
            else:
                m = self._CLOSE.search(self.buffer)
                if m:
                    self.thinking += self.buffer[:m.start()]
                    self.buffer = self.buffer[m.end():]
                    self.in_thinking = False
                    if self.thinking.strip():
                        events.append(('thinking', self.thinking.strip()))
                    self.thinking = ''
                else:
                    hold = 10
                    if len(self.buffer) > hold:
                        self.thinking += self.buffer[:-hold]
                        self.buffer = self.buffer[-hold:]
                    break
        return events

    def flush(self):
        """Returns (visible_text, thinking_leftover_or_None)."""
        events = self.flush_events()
        visible = ''.join(text for kind, text in events if kind == 'delta')
        thinking = None
        for kind, text in events:
            if kind == 'thinking':
                thinking = text
        return visible, thinking

    def flush_events(self):
        """Returns ordered ``(kind, text)`` pairs for whatever remains."""
        events = []
        if self.in_thinking:
            leftover = (self.thinking + self.buffer).strip()
            self.buffer = ''
            self.thinking = ''
            if leftover:
                events.append(('thinking', leftover))
        else:
            if self.buffer:
                events.append(('delta', self.buffer))
            self.buffer = ''
        return events


# Back-compat alias for callers that refer to the filter by its original name.
_ThinkingFilter = ThinkingFilter
