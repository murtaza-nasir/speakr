"""The phone layout of the recording page and the recording list (below 768px).

One structure serves every UI theme (templates/components/phone/, the phone
parts of templates/components/detail/, static/css/phone.css). These tests
read the templates and stylesheets; the behaviour in a browser is covered by
tests/e2e/test_phone_layout_ui.py.

What is guarded:
  - every action of a recording on the desktop header and panels is also in
    the phone layout, so nothing is lost on a phone;
  - the structure is shared: no theme folder carries phone markup;
  - an incognito recording has no download control, on a phone or on the
    desktop (the download routes need a stored recording);
  - the texts of the phone layout exist and are all used;
  - no :has() in the phone stylesheet (a :has() on <body> made the browser
    look at the whole page again on every change).
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, 'templates')
PHONE_DIR = os.path.join(TEMPLATES, 'components', 'phone')
DETAIL_DIR = os.path.join(TEMPLATES, 'components', 'detail')
PHONE_DETAIL_PARTS = (
    'mobile-summary-panel.html', 'mobile-transcript-panel.html', 'mobile-notes-panel.html',
    'mobile-chat-panel.html', 'mobile-events-panel.html', 'mobile-stats-panel.html',
    'audio-player.html', 'tab-navigation.html',
)


def _read(*parts):
    with open(os.path.join(*parts), encoding='utf-8') as f:
        return f.read()


def _phone_markup():
    texts = [_read(PHONE_DIR, name) for name in sorted(os.listdir(PHONE_DIR)) if name.endswith('.html')]
    texts += [_read(DETAIL_DIR, name) for name in PHONE_DETAIL_PARTS]
    return '\n'.join(texts)


def _calls(markup):
    """Names of the functions called in the event handlers of a template."""
    names = set()
    for handler in re.findall(r'@(?:click|change)(?:\.[a-z.]+)?="([^"]*)"', markup):
        names.update(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\(', handler))
        if re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', handler.strip()):
            names.add(handler.strip())  # a handler given by its name alone
    return names


# The actions of a recording on the desktop: the header (themes/default/
# header-actions.html), the metadata strip, the transcription panel and the
# right panel. Left out: what only arranges the desktop page.
DESKTOP_ONLY = {
    'toggleHintsPopover',        # hints are rows of the Info sheet on a phone
    'downloadWithDefaultTemplate', 'showTemplateSelector',  # the phone asks for the template in downloadTranscript
    'closest', 'removeAttribute',  # DOM calls of the overflow menu
    'toggleEditTitle',           # Rename calls it through phoneRename (checked below)
    'toggleSidebar',             # the list is a screen on a phone; the back button opens it
}


def test_every_desktop_action_of_a_recording_is_on_the_phone():
    desktop = ''
    desktop += _read(TEMPLATES, 'themes', 'default', 'header-actions.html')
    desktop += _read(TEMPLATES, 'themes', 'default', 'detail-meta.html')
    desktop += _read(DETAIL_DIR, 'desktop-transcription-panel.html')
    desktop += _read(DETAIL_DIR, 'desktop-right-panel.html')
    desktop += _read(DETAIL_DIR, 'desktop-events-tab.html')
    desktop += _read(DETAIL_DIR, 'desktop-summary-tab.html')
    desktop += _read(DETAIL_DIR, 'desktop-notes-tab.html')
    desktop += _read(TEMPLATES, 'components', 'header.html')
    wanted = _calls(desktop) - DESKTOP_ONLY
    # the handlers this test must at least see, so that a changed regex cannot empty it
    assert {'toggleInbox', 'toggleHighlight', 'toggleArchive', 'assignFolderToRecording', 'editRecordingTags',
            'confirmReprocess', 'openRegenerateSummaryModal', 'openTranslationModal', 'confirmReset',
            'openSpeakerModal', 'openUnifiedShareModal', 'confirmDeleteAudio', 'confirmDelete',
            'openParticipantsModal', 'openMeetingDatePicker', 'openDuplicatesModal', 'regenerateTitle',
            'toggleFollowPlayerMode', 'toggleTranscriptionViewMode', 'toggleTranscriptFind', 'copyTranscription',
            'openTranscriptionEditor', 'generateTranslation', 'copySummary', 'downloadSummary', 'toggleEditSummary',
            'copyNotes', 'downloadNotes', 'toggleEditNotes', 'downloadEventICS', 'deleteEvent',
            'generateSummary'} <= wanted
    phone = _calls(_phone_markup())
    assert not (wanted - phone), sorted(wanted - phone)
    assert 'phoneRename' in phone
    assert 'toggleEditTitle()' in _read(ROOT, 'static', 'js', 'modules', 'composables', 'phone-layout.js')


def test_phone_only_actions_are_kept():
    """What the phone header offered before the shared layout and the desktop does not."""
    phone = _calls(_phone_markup())
    for name in ('downloadTranscript', 'downloadICS', 'clearChat', 'downloadChat',
                 'clearIncognitoRecordingWithConfirm', 'phoneFilterByTag', 'skipAudio', 'setPlaybackRate',
                 'toggleAudioPlayback', 'enterVideoFullscreen'):
        assert name in phone, name
    markup = _phone_markup()
    assert "'/audio/' + selectedRecording.id + '?download=true'" in markup


def test_the_list_keeps_its_filters_and_its_menu():
    markup = _phone_markup() + _read(TEMPLATES, 'components', 'sidebar.html')
    for model in ('filterTextQuery', 'filterInbox', 'filterStarred', 'showArchivedRecordings', 'showSharedWithMe',
                  'filterAudioRemoved', 'filterNeedsTranscription', 'filterNeedsSummary', 'filterNeedsSpeakers',
                  'filterTags', 'filterSpeakers', 'filterDatePreset', 'filterDateRange.start', 'filterDateRange.end',
                  'filterFolder', 'includeChildFolders', 'sortBy'):
        assert model in markup, model
    for call in ('enterSelectionMode', 'exitSelectionMode', 'clearAllFilters', 'switchToUploadView',
                 'toggleNotifications', 'openSharesList', 'openColorSchemeModal', 'toggleDarkMode', 'promptInstall'):
        assert call in markup, call
    for link in ('href="/account"', 'href="/admin"', 'href="/group-management"', 'href="/inquire"', 'action="/logout"'):
        assert link in markup, link


def test_the_structure_is_shared_by_the_themes():
    themes = os.path.join(TEMPLATES, 'themes')
    for theme in sorted(os.listdir(themes)):
        folder = os.path.join(themes, theme)
        if not os.path.isdir(folder):
            continue
        assert not os.path.exists(os.path.join(folder, 'mobile-detail-header.html')), theme
        for name in os.listdir(folder):
            source = _read(folder, name)
            assert 'data-mobile-bottom-nav' not in source and 'ph-bar' not in source, (theme, name)
    detail = _read(TEMPLATES, 'components', 'detail-view.html')
    assert detail.count('<div v-if="isMobile" class="ph-detail"') == 1
    assert "'mobile-detail-header'" not in detail
    # every theme styles the one structure in its stylesheet
    css_dir = os.path.join(ROOT, 'static', 'css', 'themes')
    for name in sorted(os.listdir(css_dir)):
        if name.endswith('.css'):
            css = _read(css_dir, name)
            assert '--ph-' in css, name
            assert '.mh-row' not in css and '.mhdr-' not in css, name


def test_four_fixed_tabs():
    nav = _read(DETAIL_DIR, 'tab-navigation.html')
    assert re.findall(r'data-phone-tab="([a-z]+)"', nav) == ['summary', 'transcript', 'notes', 'chat']
    assert 'mobileMoreOpen' not in nav and 'hasMobileMoreOverflow' not in nav
    app = _read(ROOT, 'static', 'js', 'app.modular.js')
    assert 'mobileMoreOpen' not in app and 'hasMobileMoreOverflow' not in app


def test_the_switch_is_in_the_pinned_bar():
    panel = _read(DETAIL_DIR, 'mobile-transcript-panel.html')
    bar = panel[panel.index('class="ph-tbar"'):panel.index('class="ph-pane__body')]
    for part in ('data-phone-switch', 'viewingTranslation = false', 'viewingTranslation = true',
                 'toggleTranscriptFind', 'copyTranscription', 'toggleFollowPlayerMode', '_transcript-find.html'):
        assert part in bar, part
    css = _read(ROOT, 'static', 'css', 'phone.css')
    rule = css[css.index('.ph-tbar {'):]
    rule = rule[:rule.index('}')]
    assert 'position: sticky' in rule and 'top: 0' in rule
    # the pane has no scrolling area of its own: the bar pins in the recording's one
    assert 'overflow-y-auto' not in panel[:panel.index('modal-overlay')]


def test_no_volume_control_in_the_phone_player():
    player = _read(DETAIL_DIR, 'audio-player.html')
    strip = player[player.index('data-phone-player-strip'):]
    assert 'setAudioVolume' not in strip and 'toggleAudioMute' not in strip and 'volume' not in strip.lower()


INCOGNITO_GATE = '!selectedRecording.incognito'


def _element_with(source, needle):
    """The opening tag that holds `needle`."""
    at = source.index(needle)
    start = source.rindex('<', 0, at)
    end = source.index('>', at)
    return source[start:end + 1]


def test_an_incognito_recording_has_no_download_control():
    right = _read(DETAIL_DIR, 'desktop-right-panel.html')
    # the summary's download menu (its wrapper carries the condition) and the overflow menu's two formats
    wrapper = right[:right.index('data-summary-download-toggle')]
    assert INCOGNITO_GATE in wrapper[wrapper.rindex('<div'):]
    for call in ('@click="downloadSummary(\'docx\')"', '@click="downloadSummary(\'md\')"'):
        assert INCOGNITO_GATE in _element_with(right, call), call
    assert right.count('@click="downloadNotes"') == 2
    for part in right.split('@click="downloadNotes"')[:-1]:
        assert INCOGNITO_GATE in part[part.rindex('<button'):]

    transcript = _read(DETAIL_DIR, 'desktop-transcription-panel.html')
    wrapper = transcript[:transcript.index('data-download-toggle')]
    assert INCOGNITO_GATE in wrapper[wrapper.rindex('<div'):]

    chat = _read(DETAIL_DIR, 'floating-chat.html')
    assert INCOGNITO_GATE in chat[:chat.index('@click="downloadChat"')][-120:]
    assert "download_if='!selectedRecording.incognito'" in chat
    assert "download_if='!selectedRecording.incognito'" in _read(DETAIL_DIR, 'mobile-chat-panel.html')
    macro = _read(TEMPLATES, 'components', 'assistant', 'chat.html')
    assert '{% if download_if %} v-if="{{ download_if }}"{% endif %} type="button" @click="{{ download }}(index)"' in macro

    phone = _phone_markup()
    for needle in ('data-phone-download="summary"', 'data-phone-download="notes"', 'data-phone-download="chat"',
                   'data-phone-action="download-transcript"'):
        assert INCOGNITO_GATE in _element_with(phone, needle), needle
    sheets = _read(PHONE_DIR, 'recording-sheets.html')
    audio = sheets[:sheets.index('data-phone-action="download-audio"')]
    assert '<template v-if="!selectedRecording.incognito">' in audio[audio.rindex('<div v-if="(!selectedRecording.is_shared'):] \
        or INCOGNITO_GATE in audio[audio.rindex('<template'):]
    # copy stays
    for call in ('copySummary', 'copyNotes', 'copyTranscription'):
        assert INCOGNITO_GATE not in _element_with(phone, f'@click="{call}"'), call


def test_phone_texts_exist_and_are_used():
    with open(os.path.join(ROOT, 'static', 'locales', 'en.json'), encoding='utf-8') as f:
        keys = set(json.load(f)['phone'])
    used = set(re.findall(r"'phone\.([a-zA-Z0-9]+)'", _phone_markup()))
    assert used == keys, (sorted(used - keys), sorted(keys - used))


def test_the_stylesheet_is_linked_and_has_no_has_selector():
    index = _read(TEMPLATES, 'index.html')
    assert "filename='css/phone.css', v=app_version" in index
    assert index.index('css/primitives.css') < index.index('css/phone.css') < index.index('css/tailwind.css')
    css = re.sub(r'/\*.*?\*/', '', _read(ROOT, 'static', 'css', 'phone.css'), flags=re.S)
    assert ':has(' not in css
    for name in ('cards', 'classic', 'desk', 'focus', 'workspace'):
        theme = _read(ROOT, 'static', 'css', 'themes', f'{name}.css')
        section = re.sub(r'/\*.*?\*/', '', theme[theme.index('Phone layout (below 768px)'):], flags=re.S)
        assert ':has(' not in section, name
