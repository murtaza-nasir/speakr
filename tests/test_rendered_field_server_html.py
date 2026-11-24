"""Server-provided summary_html and notes_html are sanitized at the v-html
sink (#695).

The server cleans the HTML it renders from markdown (bleach, md_to_html), but
the page cannot verify that the bytes it received came from that code path. So
every v-html value is sanitized in the browser at the point of use: markdown
through window.renderMarkdownSafe, ready-made HTML through window.sanitizeHtml
(both DOMPurify, both failing closed to escaped text), or built by a helper
that escapes every value it inserts.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def _function_body(source, name):
    start = source.index(f'const {name} = (')
    end = source.index('};', start)
    return source[start:end]


def test_rendered_field_does_not_return_server_html_verbatim():
    body = _function_body(_read('static', 'js', 'app.modular.js'), 'renderedField')
    assert "return rec[field + '_html']" not in body


def test_rendered_field_sanitizes_server_html():
    body = _function_body(_read('static', 'js', 'app.modular.js'), 'renderedField')
    assert 'window.sanitizeHtml(serverHtml)' in body
    assert 'window.renderMarkdownSafe(rec[field])' in body


def test_sanitize_html_uses_dompurify_and_fails_closed():
    source = _read('static', 'js', 'markdown-safe.js')
    sanitize = source[source.index('function sanitizeHtml'):source.index('function renderMarkdownSafe')]
    assert 'window.DOMPurify.sanitize(' in sanitize
    assert 'return escapeText(html);' in sanitize
    render = source[source.index('function renderMarkdownSafe'):]
    assert 'return sanitizeHtml(html);' in render
    assert 'window.sanitizeHtml = sanitizeHtml;' in source


def test_detail_vhtml_sinks_still_use_rendered_fields():
    detail = os.path.join('templates', 'components', 'detail')
    assert 'v-html="summaryHtml"' in _read(detail, 'desktop-summary-tab.html')
    assert 'v-html="notesHtml"' in _read(detail, 'desktop-notes-tab.html')
    assert 'v-html="summaryHtml"' in _read(detail, 'mobile-summary-panel.html')
    assert 'v-html="notesHtml"' in _read(detail, 'mobile-notes-panel.html')


# Every v-html expression in the templates, and where its value is sanitized.
# A new sink must be added here with the place it is made safe.
SANITIZED_SINKS = {
    'summaryHtml': 'renderedField: window.sanitizeHtml / window.renderMarkdownSafe',
    'notesHtml': 'renderedField: window.sanitizeHtml / window.renderMarkdownSafe',
    'customBannerHtml': 'window.renderMarkdownSafe',
    'uploadDisclaimerHtml': 'window.renderMarkdownSafe',
    'recordingDisclaimerHtml': 'window.renderMarkdownSafe',
    'message.html': 'assistant/render.js renderAnswer: window.renderMarkdownSafe, then DOM nodes',
}
# Helpers that escape the text they are given and add fixed markup only
ESCAPING_HELPERS = re.compile(r"^(formatPromptPreview\(|t\('systemAudioHelp\.)")


def test_every_vhtml_sink_is_sanitized():
    unknown = []
    for folder, _, files in os.walk(os.path.join(ROOT, 'templates')):
        for name in files:
            if not name.endswith('.html'):
                continue
            path = os.path.join(folder, name)
            for expr in re.findall(r'v-html="([^"]*)"', _read(path)):
                if expr in SANITIZED_SINKS or ESCAPING_HELPERS.match(expr):
                    continue
                unknown.append((os.path.relpath(path, ROOT), expr))
    assert not unknown, f'v-html with no known sanitizer: {unknown}'


def test_sanitized_sink_computeds_go_through_the_sanitizer():
    source = _read('static', 'js', 'app.modular.js')
    for name in ('customBannerHtml', 'uploadDisclaimerHtml', 'recordingDisclaimerHtml'):
        start = source.index(f'const {name} = computed(')
        body = source[start:source.index('});', start)]
        assert 'window.renderMarkdownSafe(' in body, name
    assert "const summaryHtml = computed(() => renderedField('summary'));" in source
    assert "const notesHtml = computed(() => renderedField('notes'));" in source
