/**
 * The library filters Ambrose may read on the Ask page (phase 2b): the state
 * of its filter column. The filters are a ceiling: the server intersects them
 * with the user's access, and the tools narrow within them, never beyond
 * (resolve_allowed_recording_ids). Default: all recordings. The recording
 * panel has no filters.
 */

/** "2 tags · Date Range", in the wording of the Ask page's active-filter pills; '' with none. */
export function filterSummary(filters, t) {
    const parts = [];
    if (filters.selectedTags.length) parts.push(`${filters.selectedTags.length} ${t('inquire.tagsCount')}`);
    if (filters.selectedFolders.length) parts.push(`${filters.selectedFolders.length} ${t('inquire.foldersCount')}`);
    if (filters.selectedSpeakers.length) parts.push(`${filters.selectedSpeakers.length} ${t('inquire.speakersCount')}`);
    if (filters.dateFrom || filters.dateTo) parts.push(t('inquire.dateRange'));
    return parts.join(' · ');
}

/** The request fields the Ask page's chat route reads (assistant_events.filters_from_request). */
export function filterFields(filters) {
    return {
        filter_tags: [...filters.selectedTags],
        filter_folders: [...filters.selectedFolders],
        filter_speakers: [...filters.selectedSpeakers],
        filter_recording_ids: [...filters.selectedRecordings],
        filter_date_from: filters.dateFrom || null,
        filter_date_to: filters.dateTo || null,
    };
}

export function useSearchIn({ t }) {
    const { ref, reactive, computed } = window.Vue;
    const filters = reactive({
        selectedTags: [], selectedFolders: [], selectedSpeakers: [], selectedRecordings: [],
        dateFrom: null, dateTo: null,
    });
    const available = reactive({ tags: [], folders: [], speakers: [], recordings: [] });
    const tagQuery = ref('');
    const folderQuery = ref('');
    const speakerQuery = ref('');

    const load = async () => {
        try {
            const response = await fetch('/api/inquire/available_filters');
            if (!response.ok) return;
            const data = await response.json();
            available.tags = data.tags || [];
            available.folders = data.folders || [];
            available.speakers = data.speakers || [];
            available.recordings = data.recordings || [];
        } catch (error) {
            console.error('Error loading available filters:', error);
        }
    };

    const matching = (items, query, nameOf) => computed(() => {
        const q = (query.value || '').toLowerCase();
        return q ? items().filter((item) => nameOf(item).toLowerCase().includes(q)) : items();
    });

    const active = computed(() => !!(filters.selectedTags.length || filters.selectedFolders.length ||
        filters.selectedSpeakers.length || filters.selectedRecordings.length || filters.dateFrom || filters.dateTo));
    const summary = computed(() => filterSummary(filters, t));
    const label = computed(() => summary.value || t('assistant.scopeLibrary'));

    const clear = () => {
        filters.selectedTags = [];
        filters.selectedFolders = [];
        filters.selectedSpeakers = [];
        filters.selectedRecordings = [];
        filters.dateFrom = null;
        filters.dateTo = null;
    };

    return reactive({
        filters, available, tagQuery, folderQuery, speakerQuery,
        filteredTags: matching(() => available.tags, tagQuery, (tag) => tag.name || ''),
        filteredFolders: matching(() => available.folders, folderQuery, (folder) => folder.name || ''),
        filteredSpeakers: matching(() => available.speakers, speakerQuery, (speaker) => String(speaker)),
        active, summary, label, load, clear,
        fields: () => filterFields(filters),
    });
}
