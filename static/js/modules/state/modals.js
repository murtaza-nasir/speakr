/**
 * Modal state management
 */

export function createModalState(ref, reactive) {
    // --- Modal Visibility State ---
    const showEditModal = ref(false);
    const showDeleteModal = ref(false);
    const showEditTagsModal = ref(false);
    const showReprocessModal = ref(false);
    const showResetModal = ref(false);
    const showSpeakerModal = ref(false);
    const showShareModal = ref(false);
    const showSharesListModal = ref(false);
    const showTextEditorModal = ref(false);
    const showAsrEditorModal = ref(false);
    const showEditSpeakersModal = ref(false);
    const showEditTextModal = ref(false);
    const showAddSpeakerModal = ref(false);
    const showShareDeleteModal = ref(false);
    const showUnifiedShareModal = ref(false);

    // --- Modal Data State ---
    const selectedNewTagId = ref('');
    const tagSearchFilter = ref('');
    const editingRecording = ref(null);
    const editingTranscriptionContent = ref('');
    const editingSegments = ref([]);
    const availableSpeakers = ref([]);
    const editingSpeakersList = ref([]);
    const databaseSpeakers = ref([]);
    const editingSpeakerSuggestions = ref({});
    const recordingToDelete = ref(null);
    const recordingToReset = ref(null);
    const reprocessType = ref(null);
    const reprocessRecording = ref(null);
    const isAutoIdentifying = ref(false);

    const asrReprocessOptions = reactive({
        language: '',
        min_speakers: null,
        max_speakers: null
    });

    const summaryReprocessPromptSource = ref('default');
    const summaryReprocessSelectedTagId = ref('');
    const summaryReprocessCustomPrompt = ref('');
    const speakerMap = ref({});
    const regenerateSummaryAfterSpeakerUpdate = ref(true);
    const speakerSuggestions = ref({});
    const loadingSuggestions = ref({});
    const activeSpeakerInput = ref(null);
    const voiceSuggestions = ref({});
    const loadingVoiceSuggestions = ref(false);

    // --- Transcript Editing State ---
    const editingSegmentIndex = ref(null);
    const editingSpeakerIndex = ref(null);
    const editedText = ref('');
    const newSpeakerName = ref('');
    const newSpeakerIsMe = ref(false);
    const editedTranscriptData = ref(null);

    // --- Inline Editing State ---
    const editingParticipants = ref(false);
    const editingMeetingDate = ref(false);
    const editingSummary = ref(false);
    const editingNotes = ref(false);
    const tempNotesContent = ref('');
    const tempSummaryContent = ref('');
    const autoSaveTimer = ref(null);
    const autoSaveDelay = 2000;

    // --- Markdown Editor State ---
    const notesMarkdownEditor = ref(null);
    const markdownEditorInstance = ref(null);
    const summaryMarkdownEditor = ref(null);
    const summaryMarkdownEditorInstance = ref(null);
    const recordingNotesEditor = ref(null);
    const recordingMarkdownEditorInstance = ref(null);

    // --- Dropdown Positions ---
    const dropdownPositions = ref({});
    const editSpeakerDropdownPositions = ref({});

    return {
        // Modal visibility
        showEditModal,
        showDeleteModal,
        showEditTagsModal,
        showReprocessModal,
        showResetModal,
        showSpeakerModal,
        showShareModal,
        showSharesListModal,
        showTextEditorModal,
        showAsrEditorModal,
        showEditSpeakersModal,
        showEditTextModal,
        showAddSpeakerModal,
        showShareDeleteModal,
        showUnifiedShareModal,

        // Modal data
        selectedNewTagId,
        tagSearchFilter,
        editingRecording,
        editingTranscriptionContent,
        editingSegments,
        availableSpeakers,
        editingSpeakersList,
        databaseSpeakers,
        editingSpeakerSuggestions,
        recordingToDelete,
        recordingToReset,
        reprocessType,
        reprocessRecording,
        isAutoIdentifying,
        asrReprocessOptions,
        summaryReprocessPromptSource,
        summaryReprocessSelectedTagId,
        summaryReprocessCustomPrompt,
        speakerMap,
        regenerateSummaryAfterSpeakerUpdate,
        speakerSuggestions,
        loadingSuggestions,
        activeSpeakerInput,
        voiceSuggestions,
        loadingVoiceSuggestions,

        // Transcript editing
        editingSegmentIndex,
        editingSpeakerIndex,
        editedText,
        newSpeakerName,
        newSpeakerIsMe,
        editedTranscriptData,

        // Inline editing
        editingParticipants,
        editingMeetingDate,
        editingSummary,
        editingNotes,
        tempNotesContent,
        tempSummaryContent,
        autoSaveTimer,
        autoSaveDelay,

        // Markdown editors
        notesMarkdownEditor,
        markdownEditorInstance,
        summaryMarkdownEditor,
        summaryMarkdownEditorInstance,
        recordingNotesEditor,
        recordingMarkdownEditorInstance,

        // Dropdown positions
        dropdownPositions,
        editSpeakerDropdownPositions
    };
}
