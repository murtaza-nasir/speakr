import { describe, expect, it } from 'vitest';

import { useTranscription } from './transcription.js';

const ref = (value) => ({ value });

// #425: a segment assigned to SPEAKER_02 offered only SPEAKER_02, because the
// list was filtered by the name already in the field when it opened.
function editor() {
    const state = {
        editingSegments: ref([
            { speaker: 'SPEAKER_02', text: 'Yellow' },
            { speaker: 'SPEAKER_01', text: 'Purple' },
        ]),
        availableSpeakers: ref(['SPEAKER_01', 'SPEAKER_02']),
        openAsrDropdownIndex: ref(null),
        dropdownPositions: ref({}),
    };
    const api = useTranscription(state, { showToast() {}, setGlobalError() {}, nextTick() {} });
    return { state, api };
}

describe('speaker list in the transcript editor', () => {
    it('lists every speaker when it is opened', () => {
        const { state, api } = editor();
        api.openSpeakerSuggestions(0);
        expect(state.openAsrDropdownIndex.value).toBe(0);
        expect(state.editingSegments.value[0].filteredSpeakers).toEqual(['SPEAKER_01', 'SPEAKER_02']);
    });

    it('filters the list while a name is typed', () => {
        const { state, api } = editor();
        state.editingSegments.value[0].speaker = '01';
        api.openSpeakerSuggestions(0, { filter: true });
        expect(state.editingSegments.value[0].filteredSpeakers).toEqual(['SPEAKER_01']);
    });

    it('assigns the chosen speaker to the segment', () => {
        const { state, api } = editor();
        api.openSpeakerSuggestions(0);
        api.selectSegmentSpeaker(0, 'SPEAKER_01');
        expect(state.editingSegments.value[0].speaker).toBe('SPEAKER_01');
        expect(state.openAsrDropdownIndex.value).toBe(null);
    });
});
