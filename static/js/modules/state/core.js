/**
 * Core application state
 */

export function createCoreState(ref, computed) {
    // --- Core State ---
    const currentView = ref('upload');
    const dragover = ref(false);
    const recordings = ref([]);
    const selectedRecording = ref(null);
    const selectedTab = ref('summary');
    const searchQuery = ref('');
    const isLoadingRecordings = ref(true);
    const globalError = ref(null);

    // --- Pagination State ---
    const currentPage = ref(1);
    const perPage = ref(25);
    const totalRecordings = ref(0);
    const totalPages = ref(0);
    const hasNextPage = ref(false);
    const hasPrevPage = ref(false);
    const isLoadingMore = ref(false);
    const searchDebounceTimer = ref(null);

    // --- Enhanced Search & Organization State ---
    const sortBy = ref('created_at');
    const selectedTagFilter = ref(null);

    // Advanced filter state
    const showAdvancedFilters = ref(false);
    const filterTags = ref([]);
    const filterDateRange = ref({ start: '', end: '' });
    const filterDatePreset = ref('');
    const filterTextQuery = ref('');
    const showArchivedRecordings = ref(false);
    const showSharedWithMe = ref(false);

    // --- App Configuration ---
    const useAsrEndpoint = ref(false);
    const currentUserName = ref('');
    const canDeleteRecordings = ref(true);
    const enableInternalSharing = ref(false);
    const enableArchiveToggle = ref(false);
    const showUsernamesInUI = ref(false);

    // Tag Selection
    const availableTags = ref([]);
    const selectedTagIds = ref([]);
    const uploadTagSearchFilter = ref('');

    const selectedTags = computed(() => {
        return selectedTagIds.value.map(tagId =>
            availableTags.value.find(tag => tag.id == tagId)
        ).filter(Boolean);
    });

    const filteredAvailableTagsForUpload = computed(() => {
        const availableForSelection = availableTags.value.filter(tag => !selectedTagIds.value.includes(tag.id));
        if (!uploadTagSearchFilter.value) return availableForSelection;

        const filter = uploadTagSearchFilter.value.toLowerCase();
        return availableForSelection.filter(tag =>
            tag.name.toLowerCase().includes(filter)
        );
    });

    const filteredRecordings = computed(() => {
        return recordings.value;
    });

    const setGlobalError = (message, duration = 7000) => {
        globalError.value = message;
        if (duration > 0) {
            setTimeout(() => { if (globalError.value === message) globalError.value = null; }, duration);
        }
    };

    return {
        // Core
        currentView,
        dragover,
        recordings,
        selectedRecording,
        selectedTab,
        searchQuery,
        isLoadingRecordings,
        globalError,
        setGlobalError,

        // Pagination
        currentPage,
        perPage,
        totalRecordings,
        totalPages,
        hasNextPage,
        hasPrevPage,
        isLoadingMore,
        searchDebounceTimer,

        // Search & Organization
        sortBy,
        selectedTagFilter,
        showAdvancedFilters,
        filterTags,
        filterDateRange,
        filterDatePreset,
        filterTextQuery,
        showArchivedRecordings,
        showSharedWithMe,

        // App Configuration
        useAsrEndpoint,
        currentUserName,
        canDeleteRecordings,
        enableInternalSharing,
        enableArchiveToggle,
        showUsernamesInUI,

        // Tags
        availableTags,
        selectedTagIds,
        uploadTagSearchFilter,
        selectedTags,
        filteredAvailableTagsForUpload,
        filteredRecordings
    };
}
