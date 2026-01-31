/**
 * Filters composable
 * Handles search, filtering, and sorting functionality
 */

import { ref, computed } from 'vue';
import { parseDateRange } from '../utils/dateUtils.js';

export function useFilters() {
    // State
    const searchQuery = ref('');
    const showAdvancedFilters = ref(false);
    const filterTags = ref([]);
    const filterFolder = ref('');  // '' = all, 'none' = no folder, or folder id
    const filterDateRange = ref({ start: '', end: '' });
    const filterDatePreset = ref('');
    const filterTextQuery = ref('');
    const showArchivedRecordings = ref(false);
    const showSharedWithMe = ref(false);
    const sortBy = ref('created_at');
    const selectedTagFilter = ref(null);
    const searchDebounceTimer = ref(null);

    // Methods
    const toggleAdvancedFilters = () => {
        showAdvancedFilters.value = !showAdvancedFilters.value;
    };

    const setDatePreset = (preset) => {
        filterDatePreset.value = preset;
        const range = parseDateRange(preset);
        filterDateRange.value = {
            start: range.start ? range.start.toISOString().split('T')[0] : '',
            end: range.end ? range.end.toISOString().split('T')[0] : ''
        };
    };

    const clearDateFilter = () => {
        filterDatePreset.value = '';
        filterDateRange.value = { start: '', end: '' };
    };

    const toggleTagFilter = (tagId) => {
        const index = filterTags.value.indexOf(tagId);
        if (index > -1) {
            filterTags.value.splice(index, 1);
        } else {
            filterTags.value.push(tagId);
        }
    };

    const clearTagFilters = () => {
        filterTags.value = [];
        selectedTagFilter.value = null;
    };

    const clearFolderFilter = () => {
        filterFolder.value = '';
    };

    const clearAllFilters = () => {
        filterTags.value = [];
        filterFolder.value = '';
        filterDateRange.value = { start: '', end: '' };
        filterDatePreset.value = '';
        filterTextQuery.value = '';
        selectedTagFilter.value = null;
        searchQuery.value = '';
    };

    const toggleArchivedView = () => {
        showArchivedRecordings.value = !showArchivedRecordings.value;
        if (showArchivedRecordings.value) {
            showSharedWithMe.value = false;
        }
    };

    const toggleSharedView = () => {
        showSharedWithMe.value = !showSharedWithMe.value;
        if (showSharedWithMe.value) {
            showArchivedRecordings.value = false;
        }
    };

    const setSortBy = (field) => {
        sortBy.value = field;
    };

    const hasActiveFilters = computed(() => {
        return filterTags.value.length > 0 ||
               filterFolder.value ||
               filterDateRange.value.start ||
               filterDateRange.value.end ||
               filterTextQuery.value ||
               searchQuery.value;
    });

    return {
        // State
        searchQuery,
        showAdvancedFilters,
        filterTags,
        filterFolder,
        filterDateRange,
        filterDatePreset,
        filterTextQuery,
        showArchivedRecordings,
        showSharedWithMe,
        sortBy,
        selectedTagFilter,
        searchDebounceTimer,

        // Computed
        hasActiveFilters,

        // Methods
        toggleAdvancedFilters,
        setDatePreset,
        clearDateFilter,
        toggleTagFilter,
        clearTagFilters,
        clearFolderFilter,
        clearAllFilters,
        toggleArchivedView,
        toggleSharedView,
        setSortBy
    };
}
