"""The admin template keeps every section at its own level.

A missing </div> after the Language models panel put the Transcription models
panel inside #llm-models and every later tab (the Audit log among them) inside
the System settings tab, where it stayed hidden.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

ADMIN = Path(__file__).resolve().parent.parent / 'templates' / 'admin.html'
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
        'source', 'track', 'wbr'}


class _Nesting(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.ancestors = {}
        self.implicit = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        attrs = dict(attrs)
        key = attrs.get('id') or ('data-audit-log' if 'data-audit-log' in attrs else None)
        if key:
            self.ancestors.setdefault(key, [name for _, name in self.stack if name])
        self.stack.append((tag, attrs.get('id')))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if len(self.stack) - index > 1:
                    self.implicit.append((tag, self.getpos()[0]))
                del self.stack[index:]
                return


def _parse():
    source = ADMIN.read_text(encoding='utf-8')
    source = re.sub(r'<script\b.*?</script>', lambda m: '\n' * m.group(0).count('\n'),
                    source, flags=re.S)
    parser = _Nesting()
    parser.feed(source)
    return parser


def test_the_transcription_models_panel_is_not_inside_the_language_models_panel():
    parser = _parse()
    assert 'asr-models' in parser.ancestors
    assert 'llm-models' not in parser.ancestors['asr-models']


def test_no_element_is_closed_by_an_outer_closing_tag():
    assert _parse().implicit == []


def test_the_audit_log_is_not_inside_another_panel():
    parser = _parse()
    assert 'llm-models' not in parser.ancestors['data-audit-log']
    assert 'asr-models' not in parser.ancestors['data-audit-log']
