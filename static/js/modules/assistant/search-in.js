/**
 * The library filters Ambrose may read (phase 2b): the Ask page's filter
 * column and the recording panel's "Search in" popover are two views of this
 * one state. The filters are a ceiling: the server intersects them with the
 * user's access, and the tools narrow within them, never beyond
 * (resolve_allowed_recording_ids). Default: all recordings.
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

/** The request fields both chat routes read (assistant_events.filters_from_request). */
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
    const open = ref(false);
    let loaded = false;

    const load = async () => {
        try {
            const response = await fetch('/api/inquire/available_filters');
            if (!response.ok) return;
            const data = await response.json();
            available.tags = data.tags || [];
            available.folders = data.folders || [];
            available.speakers = data.speakers || [];
            available.recordings = data.recordings || [];
            loaded = true;
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

    // A click outside the chip and its popover, or Escape, closes it.
    const onOutside = (event) => {
        if (event.type === 'keydown' && event.key !== 'Escape') return;
        if (event.type === 'click' && event.target.closest && event.target.closest('.assistant-search-in')) return;
        close();
    };
    const close = () => {
        open.value = false;
        document.removeEventListener('click', onOutside, true);
        document.removeEventListener('keydown', onOutside, true);
    };
    const toggle = () => {
        if (open.value) { close(); return; }
        open.value = true;
        if (!loaded) load();
        document.addEventListener('click', onOutside, true);
        document.addEventListener('keydown', onOutside, true);
    };

    return reactive({
        filters, available, tagQuery, folderQuery, speakerQuery, open,
        filteredTags: matching(() => available.tags, tagQuery, (tag) => tag.name || ''),
        filteredFolders: matching(() => available.folders, folderQuery, (folder) => folder.name || ''),
        filteredSpeakers: matching(() => available.speakers, speakerQuery, (speaker) => String(speaker)),
        active, summary, label, load, clear, toggle,
        close,
        fields: () => filterFields(filters),
    });
}
