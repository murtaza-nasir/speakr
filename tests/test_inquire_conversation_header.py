"""The conversation header on the Inquire page: label, filter summary, Download and Clear conversation."""
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCALES = ('en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh')


def _template():
    with open(os.path.join(ROOT, 'templates', 'inquire.html'), encoding='utf-8') as f:
        return f.read()


def _header():
    page = _template()
    start = page.index('class="inquire-conversation-header')
    return page[start:page.index('<!-- Chat Messages Area -->', start)]


def _function(name):
    page = _template()
    start = page.index(f'const {name} = () => {{')
    return page[start:page.index('\n                    };', start)]


def test_header_shown_with_the_download_condition():
    page = _template()
    opening = page[page.rindex('<div', 0, page.index('class="inquire-conversation-header')):]
    assert opening.startswith('<div v-if="canDownloadInquire"')


def test_header_has_label_summary_then_download_and_clear_in_order():
    header = _header()
    assert "t('inquire.conversation')" in header
    assert 'v-if="inquireFilterSummary"' in header
    download = header.index('@click="downloadInquireConversation"')
    clear = header.index('@click="clearInquireConversation"')
    assert download < clear
    assert header.index('fas fa-download') < header.index('fas fa-eraser')
    assert "t('inquire.clearConversation')" in header[clear:]
    assert 'style="color: var(--text-muted);"' in header[clear:]


def test_clear_conversation_keeps_the_filters():
    body = _function('clearInquireConversation')
    assert 'stopInquireChat();' in body
    assert 'inquireChatMessages.value = [];' in body
    assert "memoryState.memory = '';" in body and 'contextUsage.value = 0;' in body
    assert 'inquireFilters' not in body
    assert 'confirm(' not in body


def test_clear_all_also_clears_the_conversation():
    body = _function('clearInquireFilters')
    assert 'clearInquireConversation();' in body
    assert 'inquireFilters.selectedTags = [];' in body


def test_header_functions_exposed_to_the_template():
    page = _template()
    returned = page[page.rindex('return {'):]
    assert re.search(r'\binquireFilterSummary,', returned)
    assert re.search(r'\bclearInquireConversation,', returned)


@pytest.mark.parametrize('locale', LOCALES)
def test_header_keys_translated(locale):
    with open(os.path.join(ROOT, 'static', 'locales', f'{locale}.json'), encoding='utf-8') as f:
        inquire = json.load(f)['inquire']
    assert inquire['conversation'].strip()
    assert inquire['clearConversation'].strip()
    if locale != 'en':
        with open(os.path.join(ROOT, 'static', 'locales', 'en.json'), encoding='utf-8') as f:
            en = json.load(f)['inquire']
        assert inquire['clearConversation'] != en['clearConversation']
