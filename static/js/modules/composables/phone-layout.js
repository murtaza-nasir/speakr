/**
 * The phone layout (templates/components/phone/): one top bar, a details
 * block, four tabs, a player strip and sheets on a recording, and the
 * recording list as a screen of its own.
 *
 * Below the phone width (isMobile, composables/ui.js) the sidebar is not a
 * drawer but the list screen: it is shown when it was opened and whenever no
 * recording is open. The back button of a recording opens it; selecting a
 * recording closes it (composables/recordings.js).
 *
 * One sheet is open at a time (`phoneSheet` holds its name, or null). The
 * sheets are in the templates as v-if blocks, so a closed sheet costs
 * nothing. The pure logic is in utils/phone-layout.js.
 */

import {
    advancedFilterCount, composerHeight, detailDates, phoneListShown, skipTarget, speakerAt,
} from '../utils/phone-layout.js';

export function usePhoneLayout({
    isMobile, isSidebarCollapsed, currentView, selectedRecording, mobileTab,
    processedTranscription, currentPlayingSegmentIndex, audioCurrentTime, audioDuration, seekAudioTo,
    editingSummary, editingNotes, editingTitle, toggleEditTitle, filterByTag,
    filterTags, filterSpeakers, filterDatePreset, filterDateRange, filterAudioRemoved,
    filterNeedsTranscription, filterNeedsSummary, filterNeedsSpeakers,
    parseServerInstant, composerText = () => '', locale = () => undefined,
}) {
    const { ref, computed, watch, nextTick } = Vue;

    // ---- sheets ------------------------------------------------------------

    const phoneSheet = ref(null);
    let opener = null;

    const openPhoneSheet = (name) => {
        if (!phoneSheet.value) opener = document.activeElement;
        phoneSheet.value = name;
        nextTick(() => {
            const sheet = document.querySelector('[data-phone-sheet] .ph-sheet');
            if (sheet) sheet.focus({ preventScroll: true });
        });
    };
    const closePhoneSheet = () => {
        if (!phoneSheet.value) return;
        phoneSheet.value = null;
        const back = opener;
        opener = null;
        if (back && back.isConnected && back.focus) nextTick(() => back.focus({ preventScroll: true }));
    };

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && phoneSheet.value) {
            event.stopPropagation();
            closePhoneSheet();
        }
    });
    // A sheet belongs to the phone layout and to the recording it was opened on
    watch(isMobile, (phone) => { if (!phone) phoneSheet.value = null; });
    watch(() => (selectedRecording.value ? selectedRecording.value.id : null), () => { phoneSheet.value = null; });

    // ---- the list as a screen ---------------------------------------------

    const phoneListOpen = computed(() => phoneListShown({
        isPhone: isMobile.value,
        sidebarCollapsed: isSidebarCollapsed.value,
        view: currentView.value,
        hasRecording: !!selectedRecording.value,
    }));
    const showPhoneList = () => {
        phoneSheet.value = null;
        isSidebarCollapsed.value = false;
    };
    const phoneFilterByTag = (tag) => {
        filterByTag(tag);
        showPhoneList();
    };
    const phoneFilterCount = computed(() => advancedFilterCount({
        tags: filterTags.value,
        speakers: filterSpeakers.value,
        datePreset: filterDatePreset.value,
        dateRange: filterDateRange.value,
        audioRemoved: filterAudioRemoved.value,
        needsTranscription: filterNeedsTranscription.value,
        needsSummary: filterNeedsSummary.value,
        needsSpeakers: filterNeedsSpeakers.value,
    }));

    // ---- the recording -----------------------------------------------------

    const phoneDetailsOpen = ref(true);

    const phoneDates = computed(() => {
        const recording = selectedRecording.value;
        if (!recording) return { meeting: '', uploaded: '', only: '' };
        const date = (value) => (value ? parseServerInstant(value) : null);
        return detailDates(date(recording.meeting_date), date(recording.created_at), { locale: locale() });
    });

    // The summary and notes editors and the chat fill the screen; the other
    // tabs scroll with the details block above them.
    const phoneEditing = computed(() =>
        (mobileTab.value === 'summary' && editingSummary.value) || (mobileTab.value === 'notes' && editingNotes.value));
    const phoneFill = computed(() => phoneEditing.value || mobileTab.value === 'chat');

    // Each tab starts at its top, as when each tab had its own scrolling area
    watch(mobileTab, () => nextTick(() => {
        const scroller = document.querySelector('[data-phone-scroll]');
        if (scroller) scroller.scrollTop = 0;
    }));

    // The chip row of the list scrolls sideways. When the row is shown, a chip
    // that is set and out of view is brought into view, so that the reason for
    // a filtered list can be seen. Used as the row's template ref.
    const revealPhoneFilter = (row) => {
        if (!row) return;
        nextTick(() => {
            const chip = row.querySelector('.ph-fc[aria-pressed="true"]');
            if (!chip) return;
            const frame = row.getBoundingClientRect();
            const box = chip.getBoundingClientRect();
            if (box.left < frame.left + 12) row.scrollLeft -= frame.left + 12 - box.left;
            else if (box.right > frame.right - 12) row.scrollLeft += box.right - (frame.right - 12);
        });
    };

    const phoneRename = () => {
        closePhoneSheet();
        toggleEditTitle();
        nextTick(() => nextTick(() => {
            const input = document.querySelector('[data-phone-title-input]');
            if (input && editingTitle.value) {
                input.focus();
                input.select();
            }
        }));
    };

    // ---- the player --------------------------------------------------------

    const phoneSpeaker = computed(() => {
        const transcript = processedTranscription.value || {};
        return transcript.hasDialogue ? speakerAt(transcript.simpleSegments, currentPlayingSegmentIndex.value) : '';
    });
    const skipAudio = (delta) => seekAudioTo(skipTarget(audioCurrentTime.value, delta, audioDuration.value));

    // ---- the composer ------------------------------------------------------

    // One line high; grows with its text to about five lines, then scrolls.
    const growPhoneComposer = () => {
        const field = document.querySelector('[data-phone-composer] textarea');
        if (!field) return;
        field.style.height = 'auto';
        field.style.height = composerHeight(field.scrollHeight + 2) + 'px';
    };
    watch([composerText, mobileTab, isMobile], () => nextTick(growPhoneComposer), { flush: 'post' });

    return {
        phoneSheet, openPhoneSheet, closePhoneSheet,
        phoneListOpen, showPhoneList, phoneFilterByTag, phoneFilterCount,
        phoneDetailsOpen, phoneDates, phoneEditing, phoneFill, phoneRename, revealPhoneFilter,
        phoneSpeaker, skipAudio, growPhoneComposer,
    };
}
