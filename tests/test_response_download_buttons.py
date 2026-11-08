"""Per-response Copy and Download buttons on the Inquire page and in the recording chat."""
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALES = ('en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh')
CHAT_VIEWS = ('floating-chat.html', 'mobile-chat-panel.html')


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def _inquire_row():
    page = _read('templates', 'inquire.html')
    start = page.index('class="inquire-answer-actions')
    opening = page[page.rindex('<div', 0, start):start]
    return page, opening, page[start:page.index('</div>', start)]


def _chat_actions(view):
    page = _read('templates', 'components', 'detail', view)
    start = page.index('@click="copyMessage(message.content, $event)"')
    begin = page.rindex('<div', 0, start)
    return page, page[begin:page.index('</div>', start)]


def test_inquire_row_sits_below_the_answer_and_its_sources():
    page, opening, _ = _inquire_row()
    # The Sources list is part of message.html, so the row follows that div.
    assert page.index('v-html="message.html"') < page.index('class="inquire-answer-actions')
    assert opening.startswith('<div v-if="showAnswerActions(message, index)"')
    assert page.index('class="inquire-answer-actions') < page.index('<!-- Loading indicator')


def test_inquire_row_has_copy_then_download():
    _, _, row = _inquire_row()
    assert 'justify-end' in row
    copy = row.index('@click="copyInquireAnswer(message, $event)"')
    download = row.index('@click="downloadInquireResponse(index)"')
    assert copy < download
    assert row.count('class="btn btn--icon btn--sm"') == 2
    assert row.index('fas fa-copy') < row.index('fas fa-download')
    for key in ('buttons.copyMessage', 'buttons.downloadResponse'):
        assert f":title=\"t('{key}')\"" in row
        assert f":aria-label=\"t('{key}')\"" in row


def test_inquire_row_waits_for_the_answer_to_finish():
    page = _read('templates', 'inquire.html')
    start = page.index('const showAnswerActions = ')
    body = page[start:page.index(';\n', start)]
    assert "message.role === 'assistant'" in body
    assert 'hasTurnContent(message)' in body
    assert 'isInquireChatLoading.value && index === inquireChatMessages.value.length - 1' in body


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


def test_inquire_copy_matches_the_chat_copy_feedback():
    page = _read('templates', 'inquire.html')
    body = page[page.index('const copyInquireAnswer = '):]
    body = body[:body.index('\n                    };')]
    assert 'navigator.clipboard.writeText(text)' in body
    assert "showToast(t('messages.copiedSuccessfully'))" in body
    assert 'flashCopied(button)' in body and 'fallbackCopy(text, button)' in body
    assert "await import('/static/js/modules/utils/toast.js')" in page


def test_inquire_functions_exposed_to_the_template():
    page = _read('templates', 'inquire.html')
    returned = page[page.rindex('return {'):]
    for name in ('downloadInquireResponse', 'showAnswerActions', 'copyInquireAnswer'):
        assert re.search(rf'\b{name},', returned), name


@pytest.mark.parametrize('view', CHAT_VIEWS)
def test_chat_views_have_copy_then_download(view):
    page, actions = _chat_actions(view)
    assert actions.startswith(
        '<div v-if="message.role === \'assistant\' && message.content'
        ' && !(isChatLoading && index === chatMessages.length - 1)"')
    assert 'absolute' not in actions
    assert actions.index('fas fa-copy') < actions.index('fas fa-download')
    assert '@click="downloadChatResponse(index)"' in actions
    assert actions.count('class="btn btn--icon btn--sm"') == 2
    for key in ('buttons.copyMessage', 'buttons.downloadResponse'):
        assert f":title=\"t('{key}')\"" in actions
        assert f":aria-label=\"t('{key}')\"" in actions


@pytest.mark.parametrize('view', CHAT_VIEWS)
def test_chat_view_buttons_sit_below_the_response(view):
    page, _ = _chat_actions(view)
    # Below the text, so the buttons never cover it; no room is reserved.
    assert page.index('${message.content}</div>') < page.index('@click="copyMessage(message.content, $event)"')
    assert 'pr-20' not in page and 'pr-10' not in page


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
