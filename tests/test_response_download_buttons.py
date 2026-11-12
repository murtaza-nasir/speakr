"""Per-response Copy and Download buttons on the Inquire page and in the recording chat."""
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALES = ('en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh')


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


CHAT_PARTIAL = ('templates', 'components', 'assistant', 'chat.html')


def _answer_actions():
    """The Copy and Download row of the shared chat (Ambrose 2b): the panel,
    the mobile tab and the Ask page all render it."""
    page = _read(*CHAT_PARTIAL)
    start = page.index('class="assistant-answer-actions')
    opening = page[page.rindex('<div', 0, start):start]
    return page, opening, page[start:page.index('</div>', start)]


def test_answer_row_sits_below_the_answer_and_its_sources():
    page, opening, _ = _answer_actions()
    # The Sources list is part of message.html, so the row follows that div.
    assert page.index('v-html="message.html"') < page.index('class="assistant-answer-actions')
    assert opening.startswith('<div v-if="assistant.showActions(message, index)"')
    assert page.index('class="assistant-answer-actions') < page.index('<!-- The thinking row')
    assert 'absolute' not in opening and 'pr-20' not in page and 'pr-10' not in page


def test_answer_row_has_copy_then_download():
    _, _, row = _answer_actions()
    assert 'justify-end' in row
    copy = row.index('@click="assistant.copy(message, $event)"')
    download = row.index('@click="{{ download }}(index)"')
    assert copy < download
    assert row.count('class="btn btn--icon btn--sm"') == 2
    assert row.index('fas fa-copy') < row.index('fas fa-download')
    for key in ('buttons.copyMessage', 'buttons.downloadResponse'):
        assert f":title=\"t('{key}')\"" in row
        assert f":aria-label=\"t('{key}')\"" in row


def test_answer_row_waits_for_the_answer_to_finish():
    js = _read('static', 'js', 'modules', 'assistant', 'chat.js')
    start = js.index('const showActions = ')
    body = js[start:js.index(';\n', start)]
    assert "message.role === 'assistant'" in body and 'hasText(message)' in body
    assert "!(current().loading && isLast(index))" in body


@pytest.mark.parametrize('view, download', [
    (('components', 'detail', 'floating-chat.html'), None),
    (('components', 'detail', 'mobile-chat-panel.html'), None),
    (('inquire.html',), 'downloadInquireResponse'),
])
def test_each_chat_uses_the_shared_partial(view, download):
    page = _read('templates', *view)
    assert "{% from 'components/assistant/chat.html' import assistant_chat with context %}" in page
    assert 'assistant_chat(' in page
    if download:
        assert f"download='{download}'" in page
    else:
        # The macro's default is the recording chat's download.
        assert 'download=' not in page[page.index('assistant_chat('):]
    assert "download='downloadChatResponse'" in _read(*CHAT_PARTIAL)


def test_inquire_response_download_sends_scope_response():
    page = _read('templates', 'inquire.html')
    start = page.index('const downloadInquireResponse = ')
    body = page[start:page.index(';\n', start)]
    assert 'responseWithQuestion(inquireChatMessages.value, index).map(downloadTurn)' in body
    assert "'response'" in body
    post = page[page.index('const postInquireDownload = '):]
    assert "JSON.stringify({ messages, filters: inquireDownloadFilters(), scope })" in post
    assert "scope === 'response' ? 'inquire-response.docx' : 'inquire.docx'" in post
    assert "await import('/static/js/modules/utils/chat-turn.js')" in page


def test_copy_gives_the_chat_copy_feedback():
    js = _read('static', 'js', 'modules', 'assistant', 'chat.js')
    body = js[js.index('const copy = '):]
    body = body[:body.index('\n    };')]
    assert 'navigator.clipboard.writeText(text)' in body
    assert "opts.toast(t('messages.copiedSuccessfully'))" in body
    assert 'flash()' in body and 'fallback' in body
    page = _read('templates', 'inquire.html')
    assert "await import('/static/js/modules/utils/toast.js')" in page
    assert 'toast: showToast' in page


def test_inquire_functions_exposed_to_the_template():
    page = _read('templates', 'inquire.html')
    returned = page[page.rindex('return {'):]
    for name in ('downloadInquireResponse', 'showAnswerActions', 'copyInquireAnswer', 'assistant'):
        assert re.search(rf'\b{name},', returned), name


def test_chat_composable_exposes_the_response_download():
    js = _read('static', 'js', 'modules', 'composables', 'chat.js')
    returned = js[js.rindex('return {'):]
    assert re.search(r'\bdownloadChatResponse,', returned)


@pytest.mark.parametrize('locale', LOCALES)
def test_response_download_label_translated(locale):
    buttons = json.loads(_read('static', 'locales', f'{locale}.json'))['buttons']
    en = json.loads(_read('static', 'locales', 'en.json'))['buttons']
    assert buttons['downloadResponse'].strip()
    assert buttons['copyMessage'].strip()
    if locale == 'en':
        assert buttons['downloadResponse'] == 'Download response as Word document'
    else:
        assert buttons['downloadResponse'] != en['downloadResponse']
