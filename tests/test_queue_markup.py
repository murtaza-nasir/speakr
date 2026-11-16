"""Markup guard for the processing queue and the toasts.

The queue has one structure for every UI theme: the indicator, the panel and
the rows are macros of templates/components/queue/_queue.html, used by the
sidebar, the header, the phone layout and the status-area slot. The browser
side is tests/e2e/test_toast_queue_ui.py; this file needs no browser.
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEMES = ('cards', 'classic', 'desk', 'focus', 'workspace')
LOCALES = ('en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh')


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as f:
        return f.read()


def _css(*parts):
    """A stylesheet without its comments."""
    return re.sub(r'/\*.*?\*/', '', _read(*parts), flags=re.S)


QUEUE = _read('templates', 'components', 'queue', '_queue.html')


def test_every_place_uses_the_shared_macros():
    places = {
        ('templates', 'components', 'sidebar.html'): ("queue_indicator('strip')", "queue_indicator('foot')", "queue_panel('dock')"),
        ('templates', 'components', 'header.html'): ("queue_indicator('pill')",),
        ('templates', 'components', 'detail-view.html'): ("queue_indicator('strip')",),
        ('templates', 'themes', 'default', 'status-area.html'): ("queue_panel('float')", 'queue_rows()', "phone_sheet('queue'"),
        ('templates', 'themes', 'desk', 'status-area.html'): ("queue_indicator('status')", "queue_panel('float')", 'queue_rows()',
                                                              "phone_sheet('queue'"),
    }
    for path, calls in places.items():
        text = _read(*path)
        assert "components/queue/_queue.html" in text, path
        for call in calls:
            assert call in text, f'{os.path.join(*path)} does not use {call}'
    # No template outside the macro file writes a row or a panel of its own
    for folder, _, names in os.walk(os.path.join(ROOT, 'templates')):
        for name in names:
            path = os.path.join(folder, name)
            if path.endswith(os.path.join('queue', '_queue.html')):
                continue
            text = open(path, encoding='utf-8').read()
            assert 'class="queue-row' not in text and 'class="queue-panel' not in text and 'progress-popup' not in text, path


def test_the_groups_come_from_one_list_and_rows_offer_their_actions():
    assert QUEUE.count('v-for="group in queueGroupList"') == 1
    assert QUEUE.count('v-for="item in group.items"') == 1
    for marker in ('data-queue-cancel', 'data-queue-retry', 'data-queue-delete', 'data-queue-remove', 'data-queue-open',
                   'data-queue-clear-failed', 'data-queue-clear-finished', 'data-queue-empty', 'data-queue-duplicate'):
        assert QUEUE.count(marker) == 1, marker
    # Cancel only where the row allows it; the indicator has no close button
    assert re.search(r'<button v-if="item\.view\.canCancel"[^>]*data-queue-cancel', QUEUE)
    indicator = QUEUE[QUEUE.index('{% macro queue_indicator'):QUEUE.index('{%- endmacro %}')]
    assert indicator.count('<button') == 1 and 'buttons.close' not in indicator
    app = _read('static', 'js', 'app.modular.js')
    order = re.search(r"const QUEUE_GROUP_TITLES = \{(.*?)\};", app, re.S).group(1)
    assert re.findall(r"^\s*(\w+):", order, re.M) == ['active', 'waiting', 'failed', 'done']


def test_a_moving_bar_carries_no_number_and_the_elapsed_time_is_not_rendered_by_vue():
    moving = re.search(r'<div v-else-if="item\.view\.progress\.kind === \'moving\'"[^>]*>', QUEUE).group(0)
    assert 'role="progressbar"' in moving and 'aria-valuenow' not in moving
    # The clock writes into these elements itself (app.modular.js tickQueueClock)
    for marker in (':data-queue-since="item.view.since"', ':data-queue-finished="item.view.finished"'):
        tag = re.search(r'<span[^>]*' + re.escape(marker) + r'[^>]*>(.*?)</span>', QUEUE, re.S)
        assert tag and tag.group(1).strip() == '' and 'v-text' not in tag.group(0), marker
    app = _read('static', 'js', 'app.modular.js')
    assert app.count('setInterval(tickQueueClock, 1000)') == 1
    assert "document.visibilityState === 'visible'" in app


def test_no_estimate_in_the_words_of_the_queue():
    for lang in LOCALES:
        words = json.loads(_read('static', 'locales', f'{lang}.json'))['progressQueue']
        text = json.dumps(words, ensure_ascii=False).lower()
        assert '%' not in text, lang
        if lang == 'en':
            for word in ('about', 'remaining', 'left', 'estimate', 'estimated', 'eta'):
                assert not re.search(rf'\b{word}\b', text), word
            assert words['canceling'] == 'Canceling…' and words['partsOf'] == '{{done}} of {{total}}'
            assert [words['groups'][k] for k in ('active', 'waiting', 'failed', 'done')] == ['Active', 'Waiting', 'Failed', 'Recently finished']


def test_cancel_is_confirmed_in_the_apps_own_dialog():
    app = _read('static', 'js', 'app.modular.js')
    cancel = app[app.index('const cancelQueueItem'):app.index('// The state of the queue and of the sidebar')]
    assert 'cancelNeedsConfirm(item)' in cancel and 'window.confirmDialog(' in cancel
    assert not re.search(r'(?<![\w.])confirm\(', cancel)


def test_the_page_state_is_read_from_body_classes_not_with_has():
    for parts in (('static', 'css', 'queue.css'), ('static', 'css', 'styles.css'), ('static', 'css', 'phone.css'),
                  *[('static', 'css', 'themes', f'{t}.css') for t in THEMES]):
        css = _css(*parts)
        # (a :has() on an element inside the page is not meant: `html[...] .chip:has(i)`)
        assert not re.search(r'(^|[\s,{}])(html|body)[^\s,{}]*:has\(', css), f'{parts[-1]}: :has() anchored on html or body'
        assert '.progress-popup' not in css, f'{parts[-1]}: rules of the former queue popup are left'
    app = _read('static', 'js', 'app.modular.js')
    for cls in ('queue-open', 'queue-minimized', 'phone-sheet-open'):
        assert f".toggle('{cls}'" in app, cls


def test_the_type_colors_are_fixed_and_no_theme_changes_the_raised_layer():
    styles = _css('static', 'css', 'styles.css')
    root = styles[styles.index('--pop-bg:'):]
    for token, value in (('--type-info', '#2563eb'), ('--type-success', '#15803d'), ('--type-warning', '#f59e0b'), ('--type-error', '#dc2626')):
        assert re.search(re.escape(token) + r':\s*' + value + ';', root), token
    # Info is no longer the accent of the color scheme, and a toast is no theme surface
    toast = styles[styles.index('.toast-message {'):styles.index('.toast-message.toast-show')]
    assert '--text-accent' not in toast and 'var(--toast-type)' in toast and 'var(--pop-bg)' in toast
    assert 'surface-raised' not in _read('static', 'js', 'modules', 'utils', 'toast.js').split('*/', 1)[1]
    for theme in THEMES:
        css = _css('static', 'css', 'themes', f'{theme}.css')
        assert not re.search(r'--(pop|type)-[\w-]+\s*:', css), f'{theme}.css sets a token of the raised layer'
        assert '.toast' not in css and '#toastContainer' not in css, f'{theme}.css styles the toasts'


def test_toasts_keep_their_rules():
    toast = _read('static', 'js', 'modules', 'utils', 'toast.js')
    assert 'export const DEFAULT_DURATION = 3000;' in toast and 'export const WARNING_MIN_DURATION = 8000;' in toast
    for piece in ("addEventListener('mouseenter', hold)", "addEventListener('focusin', hold)", "event.key === 'Escape'",
                  "toastRole(kind)", "'toast__close'", "'toast__countdown'", "state.timer.restart()"):
        assert piece in toast, piece
    styles = _css('static', 'css', 'styles.css')
    assert '@media (prefers-reduced-motion: reduce)' in styles[styles.index('#toastContainer {'):styles.index('#toastContainer {') + 9000]
    assert re.search(r'\.toast-message\.toast-paused \.toast__countdown > i \{ animation-play-state: paused; \}', styles)
    assert 'body.phone-sheet-open #toastContainer' in styles
    queue = _css('static', 'css', 'queue.css')
    assert '@media (prefers-reduced-motion: reduce)' in queue


def test_the_queue_stylesheet_is_linked_with_the_app_version():
    index = _read('templates', 'index.html')
    assert "filename='css/queue.css', v=app_version" in index
