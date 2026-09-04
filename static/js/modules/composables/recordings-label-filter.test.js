/**
 * The /label/<name> deep link, on the composable side.
 *
 * Two behaviours are load-bearing for the feature and easy to break:
 *   1. filterByLabelName resolves a name to every tag id that carries it and
 *      applies them as the tag filter, reporting whether it found anything.
 *   2. loadRecordings must NOT auto-select the last viewed recording on the
 *      load that follows a label deep link — doing so opens the detail view
 *      and rewrites the address bar to /recordings/<id>, losing the link.
 *
 * useRecordings only ever touches `.value`, so plain `{ value }` objects
 * stand in for refs and no Vue runtime is needed.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useRecordings } from './recordings.js';

const ref = (value) => ({ value });

const tag = (id, name) => ({ id, name });

function makeState(overrides = {}) {
    return {
        recordings: ref([]),
        selectedRecording: ref(null),
        isLoadingRecordings: ref(false),
        isLoadingMore: ref(false),
        currentPage: ref(1),
        perPage: ref(25),
        totalRecordings: ref(0),
        totalPages: ref(1),
        hasNextPage: ref(false),
        hasPrevPage: ref(false),
        showSharedWithMe: ref(false),
        showArchivedRecordings: ref(false),
        searchQuery: ref(''),
        searchDebounceTimer: ref(null),
        filterTags: ref([]),
        filterSpeakers: ref([]),
        filterDatePreset: ref(''),
        filterDateRange: ref({ start: '', end: '' }),
        filterTextQuery: ref(''),
        filterStarred: ref(false),
        filterInbox: ref(false),
        filterNeedsTranscription: ref(false),
        filterNeedsSummary: ref(false),
        filterNeedsSpeakers: ref(false),
        filterFolder: ref(''),
        sortBy: ref('created_at'),
        availableTags: ref([]),
        availableSpeakers: ref([]),
        availableFolders: ref([]),
        selectedTagIds: ref([]),
        uploadLanguage: ref(''),
        uploadMinSpeakers: ref(null),
        uploadMaxSpeakers: ref(null),
        uploadHotwords: ref(''),
        uploadInitialPrompt: ref(''),
        useAsrEndpoint: ref(false),
        connectorSupportsDiarization: ref(false),
        globalError: ref(null),
        uploadQueue: ref([]),
        isProcessingActive: ref(false),
        currentView: ref('recording'),
        showUploadModal: ref(false),
        uploadDeepLinkPending: ref(false),
        labelDeepLinkPending: ref(false),
        isMobileScreen: ref(false),
        isSidebarCollapsed: ref(false),
        isRecording: ref(false),
        audioBlobURL: ref(null),
        speakerColorMap: ref({}),
        incognitoRecording: ref(null),
        ...overrides
    };
}

function setup(overrides) {
    const state = makeState(overrides);
    const utils = { setGlobalError: vi.fn(), showToast: vi.fn() };
    return { state, utils, composable: useRecordings(state, utils, {}) };
}

/** One page of results, so loadRecordings takes its normal path. */
function stubFetchReturning(recordings) {
    const fetch = vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
            recordings,
            pagination: {
                page: 1, per_page: 25, total: recordings.length,
                total_pages: 1, has_next: false, has_prev: false
            }
        })
    });
    globalThis.fetch = fetch;
    return fetch;
}

let getItem;

beforeEach(() => {
    getItem = vi.fn().mockReturnValue(null);
    globalThis.localStorage = { getItem, setItem: vi.fn(), removeItem: vi.fn() };
});

afterEach(() => {
    vi.restoreAllMocks();
    delete globalThis.fetch;
    delete globalThis.localStorage;
});

describe('filterByLabelName', () => {
    it('applies every tag that carries the name, so the link is their union', () => {
        const { state, composable } = setup({
            availableTags: ref([tag(1, 'foo'), tag(2, 'foobar'), tag(3, 'FOO')])
        });

        expect(composable.filterByLabelName('foo')).toBe(true);
        expect(state.filterTags.value).toEqual([1, 3]);
    });

    it('matches case-insensitively and drives the tag: search token', () => {
        const { state, composable } = setup({
            availableTags: ref([tag(3, 'Team Sync')])
        });

        expect(composable.filterByLabelName('team sync')).toBe(true);
        expect(state.filterTags.value).toEqual([3]);
        // Spaces travel as underscores in the search syntax; the server maps
        // them back and matches the name exactly.
        expect(state.searchQuery.value).toBe('tag:Team_Sync');
    });

    it('reports an unknown label instead of silently showing everything', () => {
        const { state, composable } = setup({
            availableTags: ref([tag(1, 'foo')])
        });

        expect(composable.filterByLabelName('nope')).toBe(false);
        expect(state.filterTags.value).toEqual([]);
        expect(state.searchQuery.value).toBe('');
    });

    it('treats a blank name as no label', () => {
        const { composable } = setup({ availableTags: ref([tag(1, 'foo')]) });
        expect(composable.filterByLabelName('   ')).toBe(false);
    });
});

describe('loadRecordings auto-selection after a label deep link', () => {
    it('skips the auto-select so the label list is what the user lands on', async () => {
        const { state, composable } = setup({ labelDeepLinkPending: ref(true) });
        stubFetchReturning([{ id: 7, title: 'tagged' }]);

        await composable.loadRecordings(1, false, 'tag:foo');

        expect(getItem).not.toHaveBeenCalledWith('lastSelectedRecordingId');
        expect(state.selectedRecording.value).toBeNull();
        expect(state.recordings.value).toHaveLength(1);
    });

    it('consumes the flag, so the next load behaves normally', async () => {
        const { state, composable } = setup({ labelDeepLinkPending: ref(true) });
        stubFetchReturning([{ id: 7, title: 'tagged' }]);

        await composable.loadRecordings(1, false, 'tag:foo');
        expect(state.labelDeepLinkPending.value).toBe(false);

        await composable.loadRecordings(1, false, 'tag:foo');
        expect(getItem).toHaveBeenCalledWith('lastSelectedRecordingId');
    });

    it('still auto-selects on an ordinary load', async () => {
        const { composable } = setup();
        stubFetchReturning([{ id: 7, title: 'tagged' }]);

        await composable.loadRecordings();

        expect(getItem).toHaveBeenCalledWith('lastSelectedRecordingId');
    });

    it('leaves an appending load (infinite scroll) untouched', async () => {
        const { state, composable } = setup({ labelDeepLinkPending: ref(true) });
        stubFetchReturning([{ id: 8, title: 'page two' }]);
        state.recordings.value = [{ id: 7, title: 'page one' }];

        await composable.loadRecordings(2, true, 'tag:foo');

        // append never auto-selects, and must not burn the one-shot flag.
        expect(state.recordings.value).toHaveLength(2);
        expect(state.labelDeepLinkPending.value).toBe(true);
    });
});
