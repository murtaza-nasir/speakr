/**
 * Sharing state management
 */

export function createSharingState(ref, reactive) {
    // --- Public Sharing State ---
    const recordingToShare = ref(null);
    const shareOptions = reactive({
        share_summary: true,
        share_notes: true,
    });
    const generatedShareLink = ref('');
    const existingShareDetected = ref(false);
    const userShares = ref([]);
    const isLoadingShares = ref(false);
    const shareToDelete = ref(null);

    // --- Internal Sharing State ---
    const internalShareUserSearch = ref('');
    const internalShareSearchResults = ref([]);
    const internalShareRecording = ref(null);
    const internalSharePermissions = ref({ can_edit: false, can_reshare: false });
    const recordingInternalShares = ref([]);
    const isLoadingInternalShares = ref(false);
    const isSearchingUsers = ref(false);
    const allUsers = ref([]);
    const isLoadingAllUsers = ref(false);

    // --- Audio Player State ---
    const playerVolume = ref(1.0);

    return {
        // Public sharing
        recordingToShare,
        shareOptions,
        generatedShareLink,
        existingShareDetected,
        userShares,
        isLoadingShares,
        shareToDelete,

        // Internal sharing
        internalShareUserSearch,
        internalShareSearchResults,
        internalShareRecording,
        internalSharePermissions,
        recordingInternalShares,
        isLoadingInternalShares,
        isSearchingUsers,
        allUsers,
        isLoadingAllUsers,

        // Audio player
        playerVolume
    };
}
