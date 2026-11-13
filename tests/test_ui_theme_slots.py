"""Guard for the UI theme slots (docs/advanced/ui-themes.md).

A theme replaces the markup of a slot with its own partial,
templates/themes/<id>/<slot>.html. The default partial,
templates/themes/default/<slot>.html, is the reference: an override may add
behaviour and change the look, but it must keep every behaviour the default
has. For each override this test reads both partials and compares:

* event handlers (@event / v-on:event), by their handler expression;
* v-model targets;
* refs (ref / :ref);
* ids (id / :id), which scripts and tests look elements up by;
* data-* attribute names (static or bound), which scripts and CSS select on;
  for data-i18n* attributes the translation key (the value) counts as well;
* the translation keys passed to t('...') in accessibility labels and hints:
  aria-label, title, alt and placeholder, static or bound.

A difference that a theme needs for a good reason goes into ALLOWED, with the
reason in a comment beside it. When a theme moves markup from one slot into
another (for example the recording actions from the header into a toolbar
above the recording), MOVED names the slots whose overrides together must
keep what the default slot had.
"""

import json
import os
import re
from html.parser import HTMLParser

import pytest

from src.config import ui_themes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEMES_DIR = os.path.join(ROOT, 'templates', 'themes')
LOCALES = ('en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh')

# The default header's More menu repeats these actions below 1024px.
_MORE_MENU_COPIES = {
    'showHeaderMoreMenu = false; toggleInbox(selectedRecording)',
    'showHeaderMoreMenu = false; toggleHighlight(selectedRecording)',
    'showHeaderMoreMenu = false; toggleArchive(selectedRecording)',
    'showHeaderMoreMenu = false; assignFolderToRecording(selectedRecording.id, null)',
    'showHeaderMoreMenu = false; assignFolderToRecording(selectedRecording.id, folder.id)',
    'showHeaderMoreMenu = false; editRecordingTags(selectedRecording)',
    'showHeaderMoreMenu = false; openSpeakerModal()',
}

# (theme, slot) -> {kind: {item, ...}}: behaviours the override may leave out.
# Every entry needs a comment with the reason. Kinds are the keys of
# behaviours() below.
ALLOWED = {
    ('workspace', 'sidebar-nav'): {
        # The folder dropdown became the Folders section of the navigation rail:
        # its rows set filterFolder on click, so the select and its v-model are gone.
        'models': {'filterFolder'},
        # New Recording is the filled button of the header (header-controls.html),
        # which every theme shows; the rail has no second one.
        'handlers': {'switchToUploadView'},
        'label_keys': {'nav.newRecording'},
    },
    # Below 1024px the default header keeps Reprocess, Share and More, and the
    # More menu repeats the organize and place actions and Speakers. Focus keeps
    # every action in its overflow panel at every width, and Workspace shows them
    # in the toolbar above the recording, so neither needs the repeated items.
    ('focus', 'header-actions'): {
        'handlers': _MORE_MENU_COPIES,
        'data': {'data-header-more-folders'},
    },
    ('workspace', 'header-actions'): {
        'handlers': _MORE_MENU_COPIES,
        'data': {'data-header-more-folders'},
    },
}

# (theme, slot) -> (other slot, ...): the override of `slot` may have moved
# behaviour into the theme's overrides of these slots.
MOVED = {
    # The recording's actions moved from the header into the toolbar above the recording.
    ('workspace', 'header-actions'): ('detail-meta',),
}

KINDS = ('handlers', 'models', 'refs', 'ids', 'data', 'label_keys')
LABEL_ATTRS = ('aria-label', 'title', 'alt', 'placeholder')
T_KEY = re.compile(r"""\bt\(\s*['"]([^'"]+)['"]""")


def _strip_jinja(text):
    text = re.sub(r'\{#.*?#\}', '', text, flags=re.S)
    text = re.sub(r'\{%.*?%\}', '', text, flags=re.S)
    return re.sub(r'\{\{.*?\}\}', 'X', text, flags=re.S)


def _norm(expr):
    return ' '.join((expr or '').split())


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = {kind: set() for kind in KINDS}

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            value = value or ''
            if name.startswith('@') or name.startswith('v-on:'):
                self.found['handlers'].add(_norm(value))
                continue
            if name == 'v-model' or name.startswith('v-model.') or name.startswith('v-model:'):
                self.found['models'].add(_norm(value))
                continue
            bound = name.startswith(':') or name.startswith('v-bind:')
            base = name.split(':', 1)[1] if bound else name
            if base == 'ref':
                self.found['refs'].add(_norm(value))
            elif base == 'id':
                self.found['ids'].add(_norm(value))
            elif base.startswith('data-'):
                self.found['data'].add(base)
                if base.startswith('data-i18n') and not bound:
                    self.found['data'].add(f'{base}={value}')
            if base in LABEL_ATTRS:
                self.found['label_keys'].update(T_KEY.findall(value))

    handle_startendtag = handle_starttag


def behaviours(html):
    parser = _Collector()
    parser.feed(_strip_jinja(html))
    parser.close()
    return parser.found


def missing_behaviours(default_html, override_htmls, allowed=None):
    """What the default partial has and none of the override partials has,
    minus the allowed differences: {kind: sorted list}, empty when all kept."""
    required = behaviours(default_html)
    provided = {kind: set() for kind in KINDS}
    for html in override_htmls:
        for kind, items in behaviours(html).items():
            provided[kind] |= items
    allowed = allowed or {}
    missing = {}
    for kind in KINDS:
        lost = required[kind] - provided[kind] - set(allowed.get(kind, ()))
        if lost:
            missing[kind] = sorted(lost)
    return missing


def _read(*parts):
    with open(os.path.join(*parts), encoding='utf-8') as f:
        return f.read()


def _theme_folders():
    if not os.path.isdir(THEMES_DIR):
        return []
    return sorted(d for d in os.listdir(THEMES_DIR) if os.path.isdir(os.path.join(THEMES_DIR, d)))


def _overrides():
    pairs = []
    for theme in _theme_folders():
        if theme == ui_themes.DEFAULT_THEME:
            continue
        for name in sorted(os.listdir(os.path.join(THEMES_DIR, theme))):
            if name.endswith('.html'):
                pairs.append((theme, name[:-5]))
    return pairs


# ---------------------------------------------------------------- the slots


def test_every_slot_has_a_default_partial():
    for slot in ui_themes.SLOTS:
        assert os.path.isfile(os.path.join(THEMES_DIR, 'default', f'{slot}.html')), slot


def test_every_slot_is_placed_once_and_only_registered_slots_are_used():
    used = {}
    for dirpath, _, files in os.walk(os.path.join(ROOT, 'templates')):
        for name in files:
            if name.endswith('.html'):
                if os.path.join(dirpath, name) == os.path.join(THEMES_DIR, 'slot.html'):
                    continue  # its comment shows the pattern
                text = _read(dirpath, name)
                for slot in re.findall(r"ui_slot\s*=\s*'([^']+)'", text):
                    used.setdefault(slot, []).append(os.path.relpath(os.path.join(dirpath, name), ROOT))
    assert set(used) == set(ui_themes.SLOTS)
    for slot, places in used.items():
        assert len(places) == 1, (slot, places)


def test_theme_folders_hold_only_registered_themes_and_slots():
    registered = {theme.id for theme in ui_themes.all_themes()}
    for theme in _theme_folders():
        assert theme in registered, f'templates/themes/{theme}/ is not in src/config/ui_themes.py'
    for theme, slot in _overrides():
        assert slot in ui_themes.SLOTS, f'templates/themes/{theme}/{slot}.html is not a registered slot'
    for (theme, slot) in list(ALLOWED) + list(MOVED):
        assert (theme, slot) in _overrides(), f'ALLOWED/MOVED entry without an override: {theme}/{slot}'


@pytest.mark.parametrize('theme,slot', _overrides() or [pytest.param(None, None, marks=pytest.mark.skip(
    reason='no theme overrides a slot yet'))])
def test_an_override_keeps_every_behaviour_of_the_default(theme, slot):
    default_html = _read(THEMES_DIR, 'default', f'{slot}.html')
    htmls = [_read(THEMES_DIR, theme, f'{slot}.html')]
    for other in MOVED.get((theme, slot), ()):
        path = os.path.join(THEMES_DIR, theme, f'{other}.html')
        assert os.path.isfile(path), f'MOVED names {theme}/{other}.html, which does not exist'
        htmls.append(_read(path))
    missing = missing_behaviours(default_html, htmls, ALLOWED.get((theme, slot)))
    assert not missing, f'templates/themes/{theme}/{slot}.html drops: {missing}'


def test_default_partials_have_behaviour_to_guard():
    """Each default partial has at least one guarded behaviour, so an
    override can be checked against it."""
    for slot in ui_themes.SLOTS:
        found = behaviours(_read(THEMES_DIR, 'default', f'{slot}.html'))
        assert any(found.values()), slot


# ---------------------------------------------------------------- the comparison itself


DEFAULT = """
<div v-for="r in rows" :key="r.id" @click="select(r)" data-row-id ref="row"
     :title="t('tooltips.open')" data-i18n="rows.title">
  <input v-model.trim="filterText" id="rowFilter" :aria-label="t('sidebar.search')">
  <button @click.stop="toggleStar(r)" :data-start-time="r.start">*</button>
  {% if current_user.is_admin %}<a href="{{ url_for('x') }}">x</a>{% endif %}
</div>
"""


def test_an_identical_override_passes():
    assert missing_behaviours(DEFAULT, [DEFAULT]) == {}


def test_an_override_may_restyle_and_add():
    override = """
<li v-for="r in rows" :key="r.id" class="new-row" v-on:click="select(r)" data-row-id ref="row"
    :title="t('tooltips.open')" data-i18n="rows.title" data-extra>
  <span><input v-model="filterText" :id="'rowFilter'" aria-label="x" :aria-label="t('sidebar.search')"></span>
  <button type="button" @click.prevent="toggleStar(r)" :data-start-time="r.start" @dblclick="extra()">*</button>
</li>
"""
    # :id="'rowFilter'" is a different expression from id="rowFilter"
    assert missing_behaviours(DEFAULT, [override]) == {'ids': ['rowFilter']}


def test_dropped_behaviours_are_reported():
    override = '<div v-for="r in rows" :key="r.id" @click="select(r)">x</div>'
    missing = missing_behaviours(DEFAULT, [override])
    assert missing['handlers'] == ['toggleStar(r)']
    assert missing['models'] == ['filterText']
    assert missing['refs'] == ['row']
    assert missing['ids'] == ['rowFilter']
    assert missing['data'] == ['data-i18n', 'data-i18n=rows.title', 'data-row-id', 'data-start-time']
    assert missing['label_keys'] == ['sidebar.search', 'tooltips.open']


def test_allowed_differences_and_moved_markup_are_accepted():
    part_a = '<div v-for="r in rows" @click="select(r)" data-row-id ref="row" data-i18n="rows.title" :title="t(\'tooltips.open\')"></div>'
    part_b = '<input v-model="filterText" id="rowFilter" :aria-label="t(\'sidebar.search\')"><button @click="toggleStar(r)"></button>'
    assert missing_behaviours(DEFAULT, [part_a, part_b], {'data': ['data-start-time']}) == {}


def test_the_default_partials_are_parsed():
    found = behaviours(_read(THEMES_DIR, 'default', 'sidebar-row.html'))
    assert 'handleRecordingClick(recording, $event, visibleRecordingIds) || selectRecording(recording)' in found['handlers']
    assert 'toggleHighlight(recording)' in found['handlers']
    assert 'sidebar.removeFromHighlighted' in found['label_keys']
    found = behaviours(_read(THEMES_DIR, 'default', 'transcript-turn.html'))
    assert {'data-start-time', 'data-segment-index'} <= found['data']
    found = behaviours(_read(THEMES_DIR, 'default', 'header-actions.html'))
    assert {'data-header-folder-toggle', 'data-header-more-menu', 'data-header-reprocess-menu'} <= found['data']
    assert 'data-i18n=headerActions.deleteRecording' in found['data']
    found = behaviours(_read(THEMES_DIR, 'default', 'inquire-filters.html'))
    assert 'inquireFilters.selectedTags' in found['models']


# ---------------------------------------------------------------- registry, locales, previews


def test_every_registered_theme_has_a_name_and_description_in_every_locale():
    for lang in LOCALES:
        catalog = json.loads(_read(ROOT, 'static', 'locales', f'{lang}.json'))
        section = catalog['uiTheme']
        for key in ('title', 'help', 'appearance', 'colorSchemeLink'):
            assert isinstance(section.get(key), str) and section[key].strip(), (lang, key)
        for theme in ui_themes.all_themes():
            entry = section['themes'].get(theme.id) or {}
            for key in ('name', 'description'):
                assert isinstance(entry.get(key), str) and entry[key].strip(), (lang, theme.id, key)
        assert catalog['server']['apiError']['invalid_ui_theme'].strip(), lang


def test_every_available_theme_has_a_preview_image():
    for theme in ui_themes.available_themes():
        path = os.path.join(ROOT, 'static', theme.preview)
        assert os.path.isfile(path), theme.preview
        with open(path, 'rb') as f:
            head = f.read(12)
        assert head[:4] == b'RIFF' and head[8:12] == b'WEBP', theme.preview


def _selector_list(sel):
    """The selectors of a rule: its top-level commas, not those inside :is() or :has()."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(sel):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == ',' and depth == 0:
            parts.append(sel[start:i])
            start = i + 1
    parts.append(sel[start:])
    return parts


def test_theme_stylesheets_are_scoped_to_their_theme():
    """Every rule of static/css/themes/<id>.css applies only under
    html[data-ui-theme="<id>"], so a theme never changes another one."""
    folder = os.path.join(ROOT, 'static', 'css', 'themes')
    if not os.path.isdir(folder):
        return
    for name in sorted(os.listdir(folder)):
        if not name.endswith('.css'):
            continue
        theme = name[:-4]
        assert ui_themes.get_theme(theme), f'static/css/themes/{name} is not a registered theme'
        css = re.sub(r'/\*.*?\*/', '', _read(folder, name), flags=re.S)
        scope = f'html[data-ui-theme="{theme}"]'
        depth = 0
        selector = ''
        for ch in css:
            if ch == '{':
                sel = selector.strip()
                if depth == 0 and not sel.startswith('@'):
                    for part in _selector_list(sel):
                        assert part.strip().startswith(scope), f'{name}: {part.strip()!r} is not under {scope}'
                elif depth == 1 and sel and not sel.startswith('@') and not re.match(r'^(from|to|\d+%)', sel):
                    # rules inside @media / @supports
                    for part in _selector_list(sel):
                        assert part.strip().startswith(scope), f'{name}: {part.strip()!r} is not under {scope}'
                depth += 1
                selector = ''
            elif ch == '}':
                depth -= 1
                selector = ''
            elif ch == ';' and depth == 0:
                selector = ''
            else:
                selector += ch
