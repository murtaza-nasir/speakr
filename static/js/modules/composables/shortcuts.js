/**
 * The keyboard shortcuts of the main app: the recording list, the open
 * recording, the player and the panels.
 *
 * Every key stands for a button of the page. Its `run` is the function that
 * button calls, and its `when` is the condition under which the button is
 * shown and enabled, so a key does nothing where the button is absent
 * (a shared recording without the right, an incognito recording, a
 * recording without audio).
 *
 * The registry, its guards and the help are in utils/keyboard-shortcuts.js
 * and utils/shortcut-help.js. Keys that other parts of the app handle
 * themselves (the segment editor, full-screen video, Ctrl+F, selection
 * mode) are listed here as reference rows, without `run`, for the help.
 */

import { createPageShortcuts, globalBindings, openShortcutHelp } from '../utils/shortcut-help.js';

/** The id after `current` in `ids`, `step` rows on (the first row from nowhere); the ends do not wrap. */
export function neighborId(ids, current, step) {
    if (!ids || !ids.length) return null;
    const at = ids.indexOf(current);
    if (at === -1) return ids[0];
    return ids[Math.min(ids.length - 1, Math.max(0, at + step))];
}

/** The speed after `rate` in `speeds`, one step faster or slower; the ends stay. */
export function steppedSpeed(speeds, rate, step) {
    let at = speeds.indexOf(rate);
    if (at === -1) at = speeds.reduce((best, speed, index) => (Math.abs(speed - rate) < Math.abs(speeds[best] - rate) ? index : best), 0);
    return speeds[Math.min(speeds.length - 1, Math.max(0, at + step))];
}

/**
 * True when Space is free for play and pause: no button, link, field or
 * other control has the focus, and the focus is not in a region that Space
 * would scroll. Only the page itself (or nothing) having the focus counts.
 */
export function spaceIsFree(doc, event) {
    const active = doc ? doc.activeElement : null;
    if (active && active !== doc.body) return false;
    const target = event ? event.target : null;
    return !target || target === doc.body || target === doc.documentElement || target === doc;
}

export function useAppShortcuts(ctx) {
    const { ref, watch, nextTick } = Vue;
    const {
        t, currentView, selectedRecording, recordings, visibleRecordingIds, selectedTab, isSidebarCollapsed,
        showAdvancedFilters, selectionMode, chatPanelState, queuePopupShown, queuePanelOpen, videoFullscreen,
        hasRecordingStats, processedTranscription, viewingTranslation, editingSummary, editingNotes, transcriptFind,
        playbackRate, playbackSpeeds, canDeleteRecordings, menus,
        selectRecording, toggleHighlight, toggleInbox, toggleArchive, confirmDelete, switchToUploadView,
        toggleSidebar, toggleAudioPlayback, skipAudio, setPlaybackRate, copyTranscription, copySummary, copyNotes,
        toggleEditSummary, toggleEditNotes, openTranscriptionEditor, toggleTranscriptionViewMode, openChatPanel,
        collapseChatPanel, toggleQueuePanel, closeTranscriptFind,
    } = ctx;

    // The row the keys act on. It follows the open recording and is moved with j and k.
    const keyboardCursorId = ref(null);

    const open = () => (currentView.value === 'detail' && selectedRecording.value ? selectedRecording.value : null);
    const stored = (recording) => !!recording && !recording.incognito && recording.id !== 'incognito';
    const listShown = () => !isSidebarCollapsed.value && !selectionMode.value;

    /** The row under the cursor, when that is not the open recording. */
    const cursorRow = () => {
        const id = keyboardCursorId.value;
        const current = open();
        if (id == null || (current && current.id === id)) return null;
        return recordings.value.find((recording) => recording.id === id) || null;
    };
    /** The recording the list keys act on: the row under the cursor, else the open recording. */
    const target = () => {
        const row = listShown() ? cursorRow() : null;
        if (row) return row;
        const current = open();
        return stored(current) ? current : null;
    };

    watch(() => (selectedRecording.value ? selectedRecording.value.id : null), (id) => {
        keyboardCursorId.value = id != null && id !== 'incognito' ? id : null;
    });
    // A row that left the list (a filter, an archived or deleted recording) takes the cursor with it
    watch(visibleRecordingIds, (ids) => {
        if (keyboardCursorId.value != null && !ids.includes(keyboardCursorId.value)) keyboardCursorId.value = null;
    });

    const moveCursor = (step) => {
        const ids = visibleRecordingIds.value;
        const id = neighborId(ids, keyboardCursorId.value, step);
        if (id == null) return;
        keyboardCursorId.value = id;
        nextTick(() => {
            const row = [...document.querySelectorAll('.recording-row[data-recording-id]')]
                .find((item) => item.getAttribute('data-recording-id') === String(id) && item.getClientRects().length);
            if (row && row.scrollIntoView) row.scrollIntoView({ block: 'nearest' });
        });
    };
    const showList = () => {
        if (isSidebarCollapsed.value) toggleSidebar();
        nextTick(() => {
            const list = document.querySelector('.sidebar .sidebar-list');
            if (list) list.focus({ preventScroll: true });
        });
    };
    const focusSearch = () => {
        if (isSidebarCollapsed.value) toggleSidebar();
        showAdvancedFilters.value = true;
        nextTick(() => {
            const field = [...document.querySelectorAll('[data-shortcut-search]')].find((item) => item.getClientRects().length);
            if (field) { field.focus(); field.select(); }
        });
    };

    // ---- the open recording --------------------------------------------------

    const hasPlayer = () => { const r = open(); return !!r && !r.audio_deleted_at && !r.incognito; };
    const canPlay = () => hasPlayer() && open().audio_ready !== false;
    const transcriptShown = () => {
        const r = open();
        return !!r && !!(viewingTranslation.value ? r.transcription_translation : r.transcription);
    };
    const copyTab = () => {
        const r = open();
        if (!r) return null;
        if (selectedTab.value === 'summary' && r.summary && !editingSummary.value) return copySummary;
        if (selectedTab.value === 'notes' && r.notes && !editingNotes.value) return copyNotes;
        return null;
    };
    const editTab = () => {
        const r = open();
        if (!r) return null;
        if (selectedTab.value === 'summary' && r.summary && !editingSummary.value) return toggleEditSummary;
        if (selectedTab.value === 'notes' && !editingNotes.value) return toggleEditNotes;
        return null;
    };
    const chatField = () => document.querySelector('.floating-chat-root textarea');
    const openChat = () => {
        if (chatPanelState.value === 'collapsed') { openChatPanel(); return; }
        const field = chatField();
        if (field) field.focus();
    };
    const tab = (name, shown = () => true) => ({
        keys: [String(['summary', 'notes', 'stats', 'events'].indexOf(name) + 1)], scope: 'recording', group: 'recording',
        label: `shortcuts.tab.${name}`, when: () => !!open() && shown(), run: () => { selectedTab.value = name; },
    });

    // ---- Escape, from the top down -------------------------------------------

    const openMenus = () => (menus || []).filter((menu) => menu && menu.value);
    const escape = (when, run, extra = {}) => ({
        keys: ['Escape'], scope: 'global', group: 'general', label: 'shortcuts.close', help: false, when, run, ...extra,
    });
    const inChat = (event) => !!(event && event.target && event.target.closest && event.target.closest('.floating-chat-root'));

    const registry = createPageShortcuts({
        scopes: () => {
            // Full-screen video and the recorder have the keys to themselves
            if (videoFullscreen.value) return [];
            if (currentView.value === 'recording') return ['global'];
            const list = ['global'];
            if (listShown()) list.push('list');
            if (open()) list.push('recording');
            return list;
        },
    });
    const openShortcutsHelp = () => openShortcutHelp(registry, { t });

    registry.add(globalBindings({ openHelp: openShortcutsHelp, home: showList }));
    registry.add([
        { keys: ['/'], scope: 'global', group: 'general', label: 'shortcuts.search', run: focusSearch },
        { keys: ['n'], scope: 'global', group: 'general', label: 'shortcuts.newRecording', run: () => switchToUploadView() },
        { keys: ['q'], scope: 'global', group: 'general', label: 'shortcuts.toggleQueue',
            when: () => !!queuePopupShown.value, run: () => toggleQueuePanel() },

        escape(() => openMenus().length > 0, () => { openMenus().forEach((menu) => { menu.value = false; }); }, { inInput: true }),
        escape(() => !!transcriptFind.open, () => closeTranscriptFind()),
        escape((event) => chatPanelState.value !== 'collapsed' && !!open() && inChat(event), () => collapseChatPanel(), { inInput: true }),
        escape(() => chatPanelState.value !== 'collapsed' && !!open(), () => collapseChatPanel()),
        escape(() => !!queuePopupShown.value && !!queuePanelOpen.value, () => toggleQueuePanel()),
        escape(() => !!cursorRow(), () => { const r = open(); keyboardCursorId.value = stored(r) ? r.id : null; }),

        { keys: ['j'], scope: 'list', group: 'list', label: 'shortcuts.moveInList', repeat: true, run: () => moveCursor(1) },
        { keys: ['k'], scope: 'list', group: 'list', label: 'shortcuts.moveInList', repeat: true, run: () => moveCursor(-1) },
        { keys: ['Enter', 'o'], scope: 'list', group: 'list', label: 'shortcuts.openRecording',
            when: () => !!cursorRow(), run: () => selectRecording(cursorRow()) },
        { keys: ['s'], scope: ['list', 'recording'], group: 'list', label: 'shortcuts.star',
            when: () => !!target(), run: () => toggleHighlight(target()) },
        { keys: ['i'], scope: ['list', 'recording'], group: 'list', label: 'shortcuts.inbox',
            when: () => !!target(), run: () => toggleInbox(target()) },
        { keys: ['a'], scope: ['list', 'recording'], group: 'list', label: 'shortcuts.archive',
            when: () => !!target(), run: () => toggleArchive(target()) },
        { keys: ['#'], scope: ['list', 'recording'], group: 'list', label: 'shortcuts.delete',
            when: () => !!target() && canDeleteRecordings.value && target().can_delete !== false, run: () => confirmDelete(target()) },
        { keys: ['Ctrl+A'], scope: 'list', group: 'list', label: 'shortcuts.selectAll' },

        tab('summary'),
        tab('notes'),
        tab('stats', () => !!hasRecordingStats.value),
        tab('events', () => { const r = open(); return !!(r.events && r.events.length > 0); }),
        { keys: ['v'], scope: 'recording', group: 'recording', label: 'shortcuts.toggleView',
            when: () => !!processedTranscription.value.hasDialogue, run: () => toggleTranscriptionViewMode() },
        { keys: ['c'], scope: 'recording', group: 'recording', label: 'shortcuts.copyTranscript', run: () => copyTranscription() },
        { keys: ['C'], scope: 'recording', group: 'recording', label: 'shortcuts.copyTab', when: () => !!copyTab(), run: () => copyTab()() },
        { keys: ['e'], scope: 'recording', group: 'recording', label: 'shortcuts.editTranscript',
            when: transcriptShown, run: () => openTranscriptionEditor() },
        { keys: ['E'], scope: 'recording', group: 'recording', label: 'shortcuts.editTab', when: () => !!editTab(), run: () => editTab()() },
        { keys: ['m'], scope: 'recording', group: 'recording', label: 'shortcuts.openChat',
            params: { name: window.speakrAssistantName || 'Ambrose' }, run: openChat },
        { keys: ['Ctrl+F'], scope: 'recording', group: 'recording', label: 'shortcuts.findInTranscript' },

        { keys: ['p'], scope: 'recording', group: 'player', label: 'shortcuts.playPause', when: canPlay, run: () => toggleAudioPlayback() },
        // Space only while nothing has the focus: on a control it presses the control, in a list it scrolls
        { keys: ['Space'], scope: 'recording', group: 'player', label: 'shortcuts.playPause',
            when: (event) => canPlay() && spaceIsFree(document, event), run: () => toggleAudioPlayback() },
        { keys: ['h'], scope: 'recording', group: 'player', label: 'shortcuts.skip5', repeat: true, when: hasPlayer, run: () => skipAudio(-5) },
        { keys: ['l'], scope: 'recording', group: 'player', label: 'shortcuts.skip5', repeat: true, when: hasPlayer, run: () => skipAudio(5) },
        { keys: ['H'], scope: 'recording', group: 'player', label: 'shortcuts.skip15', repeat: true, when: hasPlayer, run: () => skipAudio(-15) },
        { keys: ['L'], scope: 'recording', group: 'player', label: 'shortcuts.skip15', repeat: true, when: hasPlayer, run: () => skipAudio(15) },
        { keys: ['<'], scope: 'recording', group: 'player', label: 'shortcuts.speed', when: hasPlayer,
            run: () => setPlaybackRate(steppedSpeed(playbackSpeeds, playbackRate.value, -1)) },
        { keys: ['>'], scope: 'recording', group: 'player', label: 'shortcuts.speed', when: hasPlayer,
            run: () => setPlaybackRate(steppedSpeed(playbackSpeeds, playbackRate.value, 1)) },

        // Handled by the segment editor (asr-editor/pane.js, composables/asr-editor.js, find-replace.js)
        { keys: ['?'], scope: 'editor', group: 'editor', label: 'shortcuts.editorHelp' },
        { keys: ['Ctrl+S'], scope: 'editor', group: 'editor', label: 'shortcuts.editorSave' },
        { keys: ['Ctrl+F'], scope: 'editor', group: 'editor', label: 'shortcuts.editorFind' },
        // Handled by the full-screen video player (composables/ui.js)
        { keys: ['Space', 'k'], scope: 'video', group: 'video', label: 'shortcuts.playPause' },
        { keys: ['ArrowLeft', 'ArrowRight'], scope: 'video', group: 'video', label: 'shortcuts.videoSkip' },
        { keys: ['ArrowUp', 'ArrowDown'], scope: 'video', group: 'video', label: 'shortcuts.videoVolume' },
        { keys: ['f', 'Escape'], scope: 'video', group: 'video', label: 'shortcuts.videoLeave' },
    ]);
    registry.install(document);

    return { keyboardCursorId, openShortcutsHelp };
}
