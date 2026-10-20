"""No raw summary, notes or message text is assigned to v-html (#439).

The templates used ``summary_html || summary``: when the sanitizer removed a
payload entirely, the empty string fell through to the raw stored text, which
was then rendered as HTML.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_FIELD = re.compile(r'\.(summary|notes|content|transcription)\b(?!_html)')


def _v_html_expressions():
    for folder, _, files in os.walk(os.path.join(ROOT, 'templates')):
        for name in files:
            if not name.endswith('.html'):
                continue
            path = os.path.join(folder, name)
            with open(path, encoding='utf-8') as f:
                for expr in re.findall(r'v-html="([^"]*)"', f.read()):
                    yield os.path.relpath(path, ROOT), expr


def test_no_raw_field_reaches_v_html():
    offenders = [(p, e) for p, e in _v_html_expressions() if RAW_FIELD.search(e)]
    assert not offenders
