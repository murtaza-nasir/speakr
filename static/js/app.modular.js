const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue;

// Import composables
import { useRecordings } from './modules/composables/recordings.js';
import { useUpload } from './modules/composables/upload.js';
import { useAudio } from './modules/composables/audio.js';
import { useUI } from './modules/composables/ui.js';
import { useModals } from './modules/composables/modals.js';
import { useSharing } from './modules/composables/sharing.js';
import { useReprocess } from './modules/composables/reprocess.js';
import { useTranscription } from './modules/composables/transcription.js';
import { useSpeakers } from './modules/composables/speakers.js';
import { useChat } from './modules/composables/chat.js';
import { useTags } from './modules/composables/tags.js';
import { usePWA } from './modules/composables/pwa.js';

// Import utilities
import { showToast } from './modules/utils/toast.js';
import { getContrastTextColor } from './modules/utils/colors.js';

// Wait for the DOM to be fully loaded before mounting the Vue app
document.addEventListener('DOMContentLoaded', async () => {
    // Initialize i18n before creating Vue app (if not already initialized)
    try {
        if (window.i18n && !window.i18n.currentLocale) {
            const appElement = document.getElementById('app');
            const userLang = appElement?.dataset.userLanguage || localStorage.getItem('preferredLanguage') || 'en';

            // Add timeout to prevent indefinite waiting
            await Promise.race([
                window.i18n.init(userLang),
                new Promise((resolve) => setTimeout(resolve, 3000))
            ]);

            console.log('i18n initialized with language:', userLang);
        } else if (window.i18n && window.i18n.currentLocale) {
            console.log('i18n already initialized with language:', window.i18n.currentLocale);
        }
    } catch (error) {
        console.error('Error initializing i18n:', error);
        // Continue anyway with fallback translations
    }

    // CSRF Token Integration with Vue.js
    const csrfToken = ref(document.querySelector('meta[name="csrf-token"]')?.getAttribute('content'));

    // Register Service Worker (non-blocking)
    if ('serviceWorker' in navigator) {
        // Delay registration to not block page load
        setTimeout(() => {
            navigator.serviceWorker.register('/static/sw.js')
                .then(registration => {
                    console.log('ServiceWorker registration successful with scope:', registration.scope);
                })
                .catch(error => {
                    console.warn('ServiceWorker registration failed (non-critical):', error);
                });
        }, 1000);
    }

    // Create a safe t function that's always available
    const safeT = (key, params = {}) => {
        if (!window.i18n || !window.i18n.t) {
            return key;
        }
        return window.i18n.t(key, params);
    };

    const app = createApp({
        setup() {
            // =========================================================================
            // STATE DECLARATIONS - All reactive state stays here for proper reactivity
            // =========================================================================

            // --- Core State ---
            const currentView = ref('upload');
            const dragover = ref(false);
            const recordings = ref([]);
            const selectedRecording = ref(null);
            const selectedTab = ref('summary');
            const searchQuery = ref('');
            const isLoadingRecordings = ref(true);
            const globalError = ref(null);

            // Advanced filter state
            const showAdvancedFilters = ref(false);
            const filterTags = ref([]);
            const filterDateRange = ref({ start: '', end: '' });
            const filterDatePreset = ref('');
            const filterTextQuery = ref('');
            const showArchivedRecordings = ref(false);
            const showSharedWithMe = ref(false);

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

            // --- UI State ---
            const browser = ref('unknown');
            const isSidebarCollapsed = ref(false);
            const searchTipsExpanded = ref(false);
            const isUserMenuOpen = ref(false);
            const isDarkMode = ref(false);
            const currentColorScheme = ref('blue');
            const showColorSchemeModal = ref(false);
            const windowWidth = ref(window.innerWidth);
            const mobileTab = ref('transcript');
            const isMetadataExpanded = ref(false);
            const showSortOptions = ref(false);

            // --- i18n State ---
            const currentLanguage = ref('en');
            const currentLanguageName = ref('English');
            const availableLanguages = ref([]);
            const showLanguageMenu = ref(false);

            // --- Upload State ---
            const uploadQueue = ref([]);
            const currentlyProcessingFile = ref(null);
            const processingProgress = ref(0);
            const processingMessage = ref('');
            const isProcessingActive = ref(false);
            const pollInterval = ref(null);
            const progressPopupMinimized = ref(false);
            const progressPopupClosed = ref(false);
            const maxFileSizeMB = ref(250);
            const chunkingEnabled = ref(true);
            const chunkingMode = ref('size');
            const chunkingLimit = ref(20);
            const chunkingLimitDisplay = ref('20MB');
            const recordingDisclaimer = ref('');
            const showRecordingDisclaimerModal = ref(false);
            const pendingRecordingMode = ref(null);

            // --- Audio Recording State ---
            const isRecording = ref(false);
            const mediaRecorder = ref(null);
            const audioChunks = ref([]);
            const audioBlobURL = ref(null);
            const recordingTime = ref(0);
            const recordingInterval = ref(null);
            const canRecordAudio = ref(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
            const canRecordSystemAudio = computed(() => navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
            const systemAudioSupported = ref(false);
            const systemAudioError = ref('');
            const recordingNotes = ref('');
            const showSystemAudioHelp = ref(false);
            const showSystemAudioHelpModal = ref(false);
            const showRecoveryModal = ref(false);
            const recoverableRecording = ref(null);
            const asrLanguage = ref('');
            const asrMinSpeakers = ref('');
            const asrMaxSpeakers = ref('');
            const audioContext = ref(null);
            const analyser = ref(null);
            const micAnalyser = ref(null);
            const systemAnalyser = ref(null);
            const visualizer = ref(null);
            const micVisualizer = ref(null);
            const systemVisualizer = ref(null);
            const animationFrameId = ref(null);
            const recordingMode = ref('microphone');
            const activeStreams = ref([]);

            // --- Wake Lock and Background Recording ---
            const wakeLock = ref(null);
            const recordingNotification = ref(null);
            const isPageVisible = ref(true);

            // --- PWA Features ---
            const deferredInstallPrompt = ref(null);
            const showInstallButton = ref(false);
            const isPWAInstalled = ref(false);
            const notificationPermission = ref('default');
            const pushSubscription = ref(null);
            const appBadgeCount = ref(0);
            const currentMediaMetadata = ref(null);
            const isMediaSessionActive = ref(false);

            // --- Recording Size Monitoring ---
            const estimatedFileSize = ref(0);
            const fileSizeWarningShown = ref(false);
            const recordingQuality = ref('optimized');
            const actualBitrate = ref(0);
            const maxRecordingMB = ref(200);
            const sizeCheckInterval = ref(null);

            // Advanced Options for ASR
            const showAdvancedOptions = ref(false);
            const uploadLanguage = ref('');
            const uploadMinSpeakers = ref('');
            const uploadMaxSpeakers = ref('');

            // Tag Selection
            const availableTags = ref([]);
            const selectedTagIds = ref([]);
            const uploadTagSearchFilter = ref('');

            // --- Modal State ---
            const showEditModal = ref(false);
            const showDeleteModal = ref(false);
            const showEditTagsModal = ref(false);
            const selectedNewTagId = ref('');
            const tagSearchFilter = ref('');
            const showReprocessModal = ref(false);
            const showResetModal = ref(false);
            const showSpeakerModal = ref(false);
            const speakerModalTab = ref('speakers');  // 'speakers' or 'transcript' for mobile view
            const showShareModal = ref(false);
            const showSharesListModal = ref(false);
            const showTextEditorModal = ref(false);
            const showAsrEditorModal = ref(false);
            const editingRecording = ref(null);
            const editingTranscriptionContent = ref('');
            const editingSegments = ref([]);
            const availableSpeakers = ref([]);
            const showEditSpeakersModal = ref(false);
            const editingSpeakersList = ref([]);
            const databaseSpeakers = ref([]);
            const editingSpeakerSuggestions = ref({});
            const showEditParticipantsModal = ref(false);
            const editingParticipantsList = ref([]);
            const editingParticipantSuggestions = ref({});
            const allParticipants = ref([]);
            const recordingToShare = ref(null);
            const shareOptions = reactive({
                share_summary: true,
                share_notes: true,
            });
            const generatedShareLink = ref('');
            const existingShareDetected = ref(false);
            const recordingPublicShares = ref([]); // All public shares for current recording
            const isLoadingPublicShares = ref(false);
            const userShares = ref([]);
            const isLoadingShares = ref(false);
            const copiedShareId = ref(null);
            const shareToDelete = ref(null);
            const showShareDeleteModal = ref(false);
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
            const modalSpeakers = ref([]);
            const speakerDisplayMap = ref({});
            const regenerateSummaryAfterSpeakerUpdate = ref(true);
            const speakerSuggestions = ref({});
            const loadingSuggestions = ref({});
            const activeSpeakerInput = ref(null);
            const voiceSuggestions = ref({});
            const loadingVoiceSuggestions = ref(false);

            // --- Transcript Editing State ---
            const editingSegmentIndex = ref(null);
            const editingSpeakerIndex = ref(null);
            const showEditTextModal = ref(false);
            const editedText = ref('');
            const showAddSpeakerModal = ref(false);
            const newSpeakerName = ref('');
            const newSpeakerIsMe = ref(false);
            const newSpeakerSuggestions = ref([]);
            const loadingNewSpeakerSuggestions = ref(false);
            const showNewSpeakerSuggestions = ref(false);
            const editedTranscriptData = ref(null);

            // --- Inline Editing State ---
            const editingTitle = ref(false);
            const originalTitle = ref('');
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

            // --- Transcription State ---
            const transcriptionViewMode = ref('simple');
            const legendExpanded = ref(false);
            const highlightedSpeaker = ref(null);
            const showDownloadMenu = ref(false);
            const currentPlayingSegmentIndex = ref(null);
            const followPlayerMode = ref(false);
            const processingIndicatorMinimized = ref(false);

            // --- Chat State ---
            const showChat = ref(false);
            const isChatMaximized = ref(false);
            const chatMessages = ref([]);
            const chatInput = ref('');
            const isChatLoading = ref(false);
            const chatMessagesRef = ref(null);

            // --- Audio Player State ---
            const playerVolume = ref(1.0);
            const asrEditorAudio = ref(null);

            // --- Column Resizing State ---
            const leftColumnWidth = ref(60);
            const rightColumnWidth = ref(40);
            const isResizing = ref(false);

            // --- Dropdown Positioning ---
            const dropdownPositions = ref({});

            // --- App Configuration ---
            const useAsrEndpoint = ref(false);
            const currentUserName = ref('');
            const canDeleteRecordings = ref(true);
            const enableInternalSharing = ref(false);
            const enableArchiveToggle = ref(false);
            const showUsernamesInUI = ref(false);

            // --- Internal Sharing State ---
            const showUnifiedShareModal = ref(false);
            const internalShareUserSearch = ref('');
            const internalShareSearchResults = ref([]);
            const internalShareRecording = ref(null);
            const internalSharePermissions = ref({ can_edit: false, can_reshare: false });
            const internalShareMaxPermissions = ref({ can_edit: true, can_reshare: true });  // Permission ceiling for current user
            const recordingInternalShares = ref([]);
            const isLoadingInternalShares = ref(false);
            const isSearchingUsers = ref(false);
            const allUsers = ref([]);
            const isLoadingAllUsers = ref(false);

            // --- Reprocessing Polls ---
            const reprocessingPolls = ref(new Map());

            // --- Speaker Groups State ---
            const currentSpeakerGroupIndex = ref(0);
            const speakerGroups = ref([]);

            // --- Computed properties needed by composables ---
            const isMobileScreen = computed(() => windowWidth.value < 1024);
            const isMobileDevice = computed(() => {
                return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) ||
                       ('ontouchstart' in window) ||
                       (navigator.maxTouchPoints > 0);
            });

            const colorSchemes = {
                light: [
                    { id: 'blue', name: 'Ocean Blue', description: 'Classic blue theme with professional appeal', class: '' },
                    { id: 'emerald', name: 'Forest Emerald', description: 'Fresh green theme for a natural feel', class: 'theme-light-emerald' },
                    { id: 'purple', name: 'Royal Purple', description: 'Elegant purple theme with sophistication', class: 'theme-light-purple' },
                    { id: 'rose', name: 'Sunset Rose', description: 'Warm pink theme with gentle energy', class: 'theme-light-rose' },
                    { id: 'amber', name: 'Golden Amber', description: 'Warm yellow theme for brightness', class: 'theme-light-amber' },
                    { id: 'teal', name: 'Ocean Teal', description: 'Cool teal theme for tranquility', class: 'theme-light-teal' }
                ],
                dark: [
                    { id: 'blue', name: 'Midnight Blue', description: 'Deep blue theme for focused work', class: '' },
                    { id: 'emerald', name: 'Dark Forest', description: 'Rich green theme for comfortable viewing', class: 'theme-dark-emerald' },
                    { id: 'purple', name: 'Deep Purple', description: 'Mysterious purple theme for creativity', class: 'theme-dark-purple' },
                    { id: 'rose', name: 'Dark Rose', description: 'Muted pink theme with subtle warmth', class: 'theme-dark-rose' },
                    { id: 'amber', name: 'Dark Amber', description: 'Warm brown theme for cozy sessions', class: 'theme-dark-amber' },
                    { id: 'teal', name: 'Deep Teal', description: 'Dark teal theme for calm focus', class: 'theme-dark-teal' }
                ]
            };

            // =========================================================================
            // COLLECT ALL STATE INTO SINGLE OBJECT FOR COMPOSABLES
            // =========================================================================
            const state = {
                // Core
                currentView, dragover, recordings, selectedRecording, selectedTab, searchQuery,
                isLoadingRecordings, globalError, csrfToken,

                // Filters
                showAdvancedFilters, filterTags, filterDateRange, filterDatePreset, filterTextQuery,
                showArchivedRecordings, showSharedWithMe, sortBy, selectedTagFilter,

                // Pagination
                currentPage, perPage, totalRecordings, totalPages, hasNextPage, hasPrevPage,
                isLoadingMore, searchDebounceTimer,

                // UI
                browser, isSidebarCollapsed, searchTipsExpanded, isUserMenuOpen, isDarkMode,
                currentColorScheme, showColorSchemeModal, windowWidth, mobileTab, isMetadataExpanded,
                showSortOptions, currentLanguage, currentLanguageName, availableLanguages, showLanguageMenu,
                colorSchemes, isMobileScreen, isMobileDevice,

                // Upload
                uploadQueue, currentlyProcessingFile, processingProgress, processingMessage,
                isProcessingActive, pollInterval, progressPopupMinimized, progressPopupClosed,
                maxFileSizeMB, chunkingEnabled, chunkingMode, chunkingLimit, chunkingLimitDisplay,
                recordingDisclaimer, showRecordingDisclaimerModal, pendingRecordingMode,
                showAdvancedOptions, uploadLanguage, uploadMinSpeakers, uploadMaxSpeakers,
                availableTags, selectedTagIds, uploadTagSearchFilter,

                // Audio Recording
                isRecording, mediaRecorder, audioChunks, audioBlobURL, recordingTime, recordingInterval,
                canRecordAudio, canRecordSystemAudio, systemAudioSupported, systemAudioError,
                recordingNotes, showSystemAudioHelp, showSystemAudioHelpModal, asrLanguage, asrMinSpeakers, asrMaxSpeakers,
                audioContext, analyser, micAnalyser, systemAnalyser, visualizer, micVisualizer,
                systemVisualizer, animationFrameId, recordingMode, activeStreams,
                wakeLock, recordingNotification, isPageVisible,
                estimatedFileSize, fileSizeWarningShown, recordingQuality, actualBitrate,
                maxRecordingMB, sizeCheckInterval,

                // PWA Features
                deferredInstallPrompt, showInstallButton, isPWAInstalled,
                notificationPermission, pushSubscription, appBadgeCount,
                currentMediaMetadata, isMediaSessionActive,

                // Modals
                showEditModal, showDeleteModal, showEditTagsModal, selectedNewTagId, tagSearchFilter,
                showReprocessModal, showResetModal, showSpeakerModal, speakerModalTab, showShareModal, showSharesListModal,
                showTextEditorModal, showAsrEditorModal, editingRecording, editingTranscriptionContent,
                editingSegments, availableSpeakers, showEditSpeakersModal, editingSpeakersList,
                databaseSpeakers, editingSpeakerSuggestions,
                showEditParticipantsModal, editingParticipantsList, editingParticipantSuggestions, allParticipants,
                recordingToShare, shareOptions,
                generatedShareLink, existingShareDetected, recordingPublicShares, isLoadingPublicShares,
                userShares, isLoadingShares, copiedShareId,
                shareToDelete, showShareDeleteModal, recordingToDelete, recordingToReset,
                reprocessType, reprocessRecording, isAutoIdentifying, asrReprocessOptions,
                summaryReprocessPromptSource, summaryReprocessSelectedTagId, summaryReprocessCustomPrompt,
                speakerMap, modalSpeakers, speakerDisplayMap, regenerateSummaryAfterSpeakerUpdate, speakerSuggestions,
                loadingSuggestions, activeSpeakerInput, voiceSuggestions, loadingVoiceSuggestions,

                // Transcript Editing
                editingSegmentIndex, editingSpeakerIndex, showEditTextModal, editedText,
                showAddSpeakerModal, newSpeakerName, newSpeakerIsMe, newSpeakerSuggestions,
                loadingNewSpeakerSuggestions, showNewSpeakerSuggestions, editedTranscriptData,

                // Inline Editing
                editingTitle, originalTitle,
                editingParticipants, editingMeetingDate, editingSummary, editingNotes,
                tempNotesContent, tempSummaryContent, autoSaveTimer, autoSaveDelay,

                // Markdown
                notesMarkdownEditor, markdownEditorInstance, summaryMarkdownEditor,
                summaryMarkdownEditorInstance, recordingNotesEditor, recordingMarkdownEditorInstance,

                // Transcription
                transcriptionViewMode, legendExpanded, highlightedSpeaker, showDownloadMenu,
                currentPlayingSegmentIndex, followPlayerMode, processingIndicatorMinimized,

                // Chat
                showChat, isChatMaximized, chatMessages, chatInput, isChatLoading, chatMessagesRef,

                // Audio Player
                playerVolume, asrEditorAudio,

                // Column Resizing
                leftColumnWidth, rightColumnWidth, isResizing,

                // Dropdown Positioning
                dropdownPositions,

                // App Config
                useAsrEndpoint, currentUserName, canDeleteRecordings, enableInternalSharing, enableArchiveToggle, showUsernamesInUI,

                // Internal Sharing
                showUnifiedShareModal, internalShareUserSearch, internalShareSearchResults,
                internalShareRecording, internalSharePermissions, internalShareMaxPermissions, recordingInternalShares,
                isLoadingInternalShares, isSearchingUsers, allUsers, isLoadingAllUsers,

                // Reprocessing
                reprocessingPolls,

                // Speaker Groups
                currentSpeakerGroupIndex, speakerGroups
            };

            // =========================================================================
            // TRANSLATION FUNCTION
            // =========================================================================
            const t = safeT;
            const tc = (key, count, params = {}) => {
                if (!window.i18n || !window.i18n.tc) {
                    return key;
                }
                return window.i18n.tc(key, count, params);
            };

            // =========================================================================
            // UTILITY FUNCTIONS
            // =========================================================================
            // showToast is now imported from modules/utils/toast.js

            const setGlobalError = (message, duration = 5000) => {
                // Use toast system for all errors instead of the old global error banner
                showToast(message, 'fa-exclamation-circle', duration, 'error');
            };

            // Helper function to calculate global segment index in bubble view
            const getBubbleGlobalIndex = (rowIndex, bubbleIndex) => {
                if (!processedTranscription.value.bubbleRows) return 0;

                let globalIndex = 0;
                for (let i = 0; i < rowIndex; i++) {
                    globalIndex += processedTranscription.value.bubbleRows[i].bubbles.length;
                }
                globalIndex += bubbleIndex;
                return globalIndex;
            };

            const formatFileSize = (bytes) => {
                if (!bytes) return '0 B';
                const k = 1024;
                const sizes = ['B', 'KB', 'MB', 'GB'];
                const i = Math.floor(Math.log(bytes) / Math.log(k));
                return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
            };

            const formatDisplayDate = (dateString) => {
                if (!dateString) return '';
                try {
                    let date = new Date(dateString);
                    if (isNaN(date.getTime())) {
                        if (/^\d{4}-\d{2}-\d{2}$/.test(dateString)) {
                            const [year, month, day] = dateString.split('-').map(Number);
                            date = new Date(year, month - 1, day);
                        } else {
                            return dateString;
                        }
                    }
                    if (isNaN(date.getTime())) {
                        return dateString;
                    }
                    return date.toLocaleDateString(undefined, {
                        year: 'numeric', month: 'short', day: 'numeric',
                        hour: '2-digit', minute: '2-digit'
                    });
                } catch (e) {
                    return dateString;
                }
            };

            const formatShortDate = (dateString) => {
                if (!dateString) return '';
                try {
                    let date = new Date(dateString);
                    if (isNaN(date.getTime())) {
                        if (/^\d{4}-\d{2}-\d{2}$/.test(dateString)) {
                            const [year, month, day] = dateString.split('-').map(Number);
                            date = new Date(year, month - 1, day);
                        }
                    }
                    if (isNaN(date.getTime())) {
                        return dateString;
                    }
                    const now = new Date();
                    const isCurrentYear = date.getFullYear() === now.getFullYear();
                    if (isCurrentYear) {
                        return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
                    }
                    return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
                } catch (e) {
                    return dateString;
                }
            };

            const formatStatus = (status) => {
                const statusMap = {
                    'PENDING': t('status.pending'),
                    'PROCESSING': t('status.processing'),
                    'SUMMARIZING': t('status.summarizing'),
                    'COMPLETED': t('status.completed'),
                    'FAILED': t('status.failed')
                };
                return statusMap[status] || status;
            };

            const getStatusClass = (status) => {
                switch(status) {
                    case 'COMPLETED': return 'status-completed';
                    case 'PROCESSING': return 'status-processing';
                    case 'SUMMARIZING': return 'status-summarizing';
                    case 'PENDING': return 'status-pending';
                    case 'FAILED': return 'status-failed';
                    default: return '';
                }
            };

            const formatTime = (seconds) => {
                const mins = Math.floor(seconds / 60);
                const secs = Math.floor(seconds % 60);
                return `${mins}:${secs.toString().padStart(2, '0')}`;
            };

            const formatDuration = (totalSeconds) => {
                if (!totalSeconds && totalSeconds !== 0) return '';
                totalSeconds = Math.round(totalSeconds);
                if (totalSeconds < 1) {
                    return '< 1s';
                }
                const hours = Math.floor(totalSeconds / 3600);
                const minutes = Math.floor((totalSeconds % 3600) / 60);
                const seconds = totalSeconds % 60;
                if (totalSeconds < 60) {
                    return `${seconds}s`;
                }
                let parts = [];
                if (hours > 0) {
                    parts.push(`${hours}h`);
                }
                if (minutes > 0) {
                    parts.push(`${minutes}m`);
                }
                if (hours === 0 && seconds > 0) {
                    parts.push(`${seconds}s`);
                }
                return parts.join(' ');
            };

            const formatEventDateTime = (dateString, timeOnly = false) => {
                if (!dateString) return '';
                const date = new Date(dateString);
                if (timeOnly) {
                    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                }
                return date.toLocaleString([], {
                    weekday: 'short', month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit'
                });
            };

            // Date helper functions
            const getDateForSorting = (recording) => {
                const dateStr = sortBy.value === 'meeting_date'
                    ? (recording.meeting_date || recording.created_at)
                    : recording.created_at;
                return dateStr ? new Date(dateStr) : null;
            };

            const isToday = (date) => {
                const today = new Date();
                return date.getDate() === today.getDate() &&
                       date.getMonth() === today.getMonth() &&
                       date.getFullYear() === today.getFullYear();
            };

            const isYesterday = (date) => {
                const yesterday = new Date();
                yesterday.setDate(yesterday.getDate() - 1);
                return date.getDate() === yesterday.getDate() &&
                       date.getMonth() === yesterday.getMonth() &&
                       date.getFullYear() === yesterday.getFullYear();
            };

            const isThisWeek = (date) => {
                const now = new Date();
                const startOfWeek = new Date(now);
                startOfWeek.setDate(now.getDate() - now.getDay());
                startOfWeek.setHours(0, 0, 0, 0);
                const endOfWeek = new Date(startOfWeek);
                endOfWeek.setDate(startOfWeek.getDate() + 7);
                return date >= startOfWeek && date < endOfWeek && !isToday(date) && !isYesterday(date);
            };

            const isLastWeek = (date) => {
                const now = new Date();
                const startOfLastWeek = new Date(now);
                startOfLastWeek.setDate(now.getDate() - now.getDay() - 7);
                startOfLastWeek.setHours(0, 0, 0, 0);
                const endOfLastWeek = new Date(startOfLastWeek);
                endOfLastWeek.setDate(startOfLastWeek.getDate() + 7);
                return date >= startOfLastWeek && date < endOfLastWeek;
            };

            const isThisMonth = (date) => {
                const now = new Date();
                return date.getMonth() === now.getMonth() &&
                       date.getFullYear() === now.getFullYear() &&
                       !isToday(date) && !isYesterday(date) && !isThisWeek(date) && !isLastWeek(date);
            };

            const isLastMonth = (date) => {
                const now = new Date();
                const lastMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1);
                return date.getMonth() === lastMonth.getMonth() &&
                       date.getFullYear() === lastMonth.getFullYear();
            };

            const isSameDay = (date1, date2) => {
                return date1.getDate() === date2.getDate() &&
                       date1.getMonth() === date2.getMonth() &&
                       date1.getFullYear() === date2.getFullYear();
            };

            // Bundle utilities for composables
            const utils = {
                t, tc, setGlobalError, showToast, formatFileSize, formatDisplayDate, formatShortDate,
                formatStatus, getStatusClass, formatTime, formatDuration, formatEventDateTime,
                getDateForSorting, isToday, isYesterday, isThisWeek, isLastWeek, isThisMonth, isLastMonth, isSameDay,
                nextTick
            };

            // =========================================================================
            // COMPUTED PROPERTIES (define before composables that need them)
            // =========================================================================
            const processedTranscription = computed(() => {
                if (!selectedRecording.value?.transcription) {
                    return { hasDialogue: false, content: '', speakers: [], simpleSegments: [], bubbleRows: [] };
                }

                const transcription = selectedRecording.value.transcription;
                let transcriptionData;

                try {
                    transcriptionData = JSON.parse(transcription);
                } catch (e) {
                    transcriptionData = null;
                }

                // Handle new simplified JSON format (array of segments)
                if (transcriptionData && Array.isArray(transcriptionData)) {
                    const wasDiarized = transcriptionData.some(segment => segment.speaker);

                    if (!wasDiarized) {
                        const segments = transcriptionData.map(segment => ({
                            sentence: segment.sentence,
                            startTime: segment.start_time,
                        }));
                        return {
                            hasDialogue: false,
                            isJson: true,
                            content: segments.map(s => s.sentence).join('\n'),
                            simpleSegments: segments,
                            speakers: [],
                            bubbleRows: []
                        };
                    }

                    // Extract unique speakers
                    const speakers = [...new Set(transcriptionData.map(segment => segment.speaker).filter(Boolean))];
                    const speakerColors = {};
                    speakers.forEach((speaker, index) => {
                        speakerColors[speaker] = `speaker-color-${(index % 8) + 1}`;
                    });

                    const simpleSegments = transcriptionData.map(segment => ({
                        speakerId: segment.speaker,
                        speaker: speakerMap.value[segment.speaker]?.name || segment.speaker,
                        sentence: segment.sentence,
                        startTime: segment.start_time || segment.startTime,
                        endTime: segment.end_time || segment.endTime,
                        color: speakerColors[segment.speaker] || 'speaker-color-1'
                    }));

                    const processedSimpleSegments = [];
                    let lastSpeakerId = null;
                    simpleSegments.forEach(segment => {
                        processedSimpleSegments.push({
                            ...segment,
                            showSpeaker: segment.speakerId !== lastSpeakerId
                        });
                        lastSpeakerId = segment.speakerId;
                    });

                    const bubbleRows = [];
                    let lastBubbleSpeakerId = null;
                    simpleSegments.forEach(segment => {
                        if (bubbleRows.length === 0 || segment.speakerId !== lastBubbleSpeakerId) {
                            bubbleRows.push({
                                speaker: segment.speaker,
                                color: segment.color,
                                isMe: segment.speaker && (typeof segment.speaker === 'string') && segment.speaker.toLowerCase().includes('me'),
                                bubbles: []
                            });
                            lastBubbleSpeakerId = segment.speakerId;
                        }
                        bubbleRows[bubbleRows.length - 1].bubbles.push({
                            sentence: segment.sentence,
                            startTime: segment.startTime || segment.start_time,
                            color: segment.color
                        });
                    });

                    return {
                        hasDialogue: true,
                        isJson: true,
                        segments: simpleSegments,
                        simpleSegments: processedSimpleSegments,
                        bubbleRows: bubbleRows,
                        speakers: speakers.map(speaker => ({
                            name: speakerMap.value[speaker]?.name || speaker,
                            color: speakerColors[speaker]
                        }))
                    };

                } else {
                    // Fallback for plain text transcription
                    const speakerRegex = /\[([^\]]+)\]:\s*/g;
                    const hasDialogue = speakerRegex.test(transcription);

                    if (!hasDialogue) {
                        return {
                            hasDialogue: false,
                            isJson: false,
                            content: transcription,
                            speakers: [],
                            simpleSegments: [],
                            bubbleRows: []
                        };
                    }

                    speakerRegex.lastIndex = 0;
                    const speakers = new Set();
                    let match;
                    while ((match = speakerRegex.exec(transcription)) !== null) {
                        speakers.add(match[1]);
                    }

                    const speakerList = Array.from(speakers);
                    const speakerColors = {};
                    speakerList.forEach((speaker, index) => {
                        speakerColors[speaker] = `speaker-color-${(index % 8) + 1}`;
                    });

                    const segments = [];
                    const lines = transcription.split('\n');
                    let currentSpeakerId = null;
                    let currentText = '';

                    for (const line of lines) {
                        const speakerMatch = line.match(/^\[([^\]]+)\]:\s*(.*)$/);
                        if (speakerMatch) {
                            if (currentSpeakerId && currentText.trim()) {
                                segments.push({
                                    speakerId: currentSpeakerId,
                                    speaker: speakerMap.value[currentSpeakerId]?.name || currentSpeakerId,
                                    sentence: currentText.trim(),
                                    color: speakerColors[currentSpeakerId] || 'speaker-color-1'
                                });
                            }
                            currentSpeakerId = speakerMatch[1];
                            currentText = speakerMatch[2];
                        } else if (currentSpeakerId && line.trim()) {
                            currentText += ' ' + line.trim();
                        } else if (!currentSpeakerId && line.trim()) {
                            segments.push({
                                speakerId: null,
                                speaker: null,
                                sentence: line.trim(),
                                color: 'speaker-color-1'
                            });
                        }
                    }

                    if (currentSpeakerId && currentText.trim()) {
                        segments.push({
                            speakerId: currentSpeakerId,
                            speaker: speakerMap.value[currentSpeakerId]?.name || currentSpeakerId,
                            sentence: currentText.trim(),
                            color: speakerColors[currentSpeakerId] || 'speaker-color-1'
                        });
                    }

                    const simpleSegments = [];
                    let lastSpeakerId = null;
                    segments.forEach(segment => {
                        simpleSegments.push({
                            ...segment,
                            showSpeaker: segment.speakerId !== lastSpeakerId,
                            sentence: segment.sentence || segment.text
                        });
                        lastSpeakerId = segment.speakerId;
                    });

                    const bubbleRows = [];
                    let currentRow = null;
                    segments.forEach(segment => {
                        if (!currentRow || currentRow.speakerId !== segment.speakerId) {
                            if (currentRow) bubbleRows.push(currentRow);
                            currentRow = {
                                speakerId: segment.speakerId,
                                speaker: segment.speaker,
                                color: segment.color,
                                bubbles: [],
                                isMe: segment.speaker && segment.speaker.toLowerCase().includes('me')
                            };
                        }
                        currentRow.bubbles.push({
                            sentence: segment.sentence,
                            color: segment.color
                        });
                    });
                    if (currentRow) bubbleRows.push(currentRow);

                    return {
                        hasDialogue: true,
                        isJson: false,
                        segments: segments,
                        simpleSegments: simpleSegments,
                        bubbleRows: bubbleRows,
                        speakers: speakerList.map(speaker => ({
                            name: speakerMap.value[speaker]?.name || speaker,
                            color: speakerColors[speaker] || 'speaker-color-1'
                        }))
                    };
                }
            });

            // =========================================================================
            // INITIALIZE COMPOSABLES (after processedTranscription is defined)
            // =========================================================================
            // Create reprocess composable first so it can be passed to recordings
            const reprocessComposable = useReprocess(state, utils);
            const recordingsComposable = useRecordings(state, utils, reprocessComposable);
            const uploadComposable = useUpload(state, utils);

            // Add startUpload to utils for audio composable to use
            utils.startUploadQueue = uploadComposable.startUpload;

            const audioComposable = useAudio(state, utils);
            const uiComposable = useUI(state, utils, processedTranscription);
            const modalsComposable = useModals(state, utils);
            const sharingComposable = useSharing(state, utils);
            const transcriptionComposable = useTranscription(state, utils);
            const chatComposable = useChat(state, utils);
            const pwaComposable = usePWA(state, utils);
            const tagsComposable = useTags({
                recordings,
                availableTags,
                selectedRecording,
                showEditTagsModal,
                editingRecording,
                tagSearchFilter,
                showToast,
                setGlobalError
            });

            // Speakers composable needs processedTranscription, so initialize it after
            const speakersComposable = useSpeakers(state, utils, processedTranscription);

            const groupedRecordings = computed(() => {
                const groups = {};
                recordings.value.forEach(recording => {
                    const date = getDateForSorting(recording);
                    if (!date) return;
                    let group;
                    if (isToday(date)) group = t('sidebar.today');
                    else if (isYesterday(date)) group = t('sidebar.yesterday');
                    else if (isThisWeek(date)) group = t('sidebar.thisWeek');
                    else if (isLastWeek(date)) group = t('sidebar.lastWeek');
                    else if (isThisMonth(date)) group = t('sidebar.thisMonth');
                    else if (isLastMonth(date)) group = t('sidebar.lastMonth');
                    else group = t('sidebar.older');
                    if (!groups[group]) groups[group] = [];
                    groups[group].push(recording);
                });
                return Object.entries(groups).map(([title, items]) => ({ title, items }));
            });

            const filteredAvailableTags = computed(() => {
                return availableTags.value.filter(tag =>
                    !selectedTagIds.value.includes(tag.id) &&
                    (!tagSearchFilter.value || tag.name.toLowerCase().includes(tagSearchFilter.value.toLowerCase()))
                );
            });

            const selectedTags = computed(() => {
                return selectedTagIds.value.map(id =>
                    availableTags.value.find(t => t.id === id)
                ).filter(Boolean);
            });

            const toasts = ref([]);

            // Date preset options for filters
            const datePresetOptions = computed(() => {
                return [
                    { value: 'today', label: t('sidebar.today') },
                    { value: 'yesterday', label: t('sidebar.yesterday') },
                    { value: 'thisweek', label: t('sidebar.thisWeek') },
                    { value: 'lastweek', label: t('sidebar.lastWeek') },
                    { value: 'thismonth', label: t('sidebar.thisMonth') },
                    { value: 'lastmonth', label: t('sidebar.lastMonth') }
                ];
            });

            // Language options for ASR
            const languageOptions = computed(() => {
                return [
                    { value: '', label: t('form.autoDetect') },
                    { value: 'en', label: t('languages.en') },
                    { value: 'es', label: t('languages.es') },
                    { value: 'fr', label: t('languages.fr') },
                    { value: 'de', label: t('languages.de') },
                    { value: 'it', label: t('languages.it') },
                    { value: 'pt', label: t('languages.pt') },
                    { value: 'nl', label: t('languages.nl') },
                    { value: 'ru', label: t('languages.ru') },
                    { value: 'zh', label: t('languages.zh') },
                    { value: 'ja', label: t('languages.ja') },
                    { value: 'ko', label: t('languages.ko') }
                ];
            });

            // Recording metadata for sidebar
            const activeRecordingMetadata = computed(() => {
                if (!selectedRecording.value) return [];

                const recording = selectedRecording.value;
                const metadata = [];

                if (recording.created_at) {
                    // Format duration in human-readable format (e.g., "2m 30s")
                    const formatProcessingDuration = (seconds) => {
                        if (!seconds && seconds !== 0) return null;
                        if (seconds < 60) return `${seconds}s`;
                        const mins = Math.floor(seconds / 60);
                        const secs = seconds % 60;
                        return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
                    };

                    // Build tooltip with processing breakdown
                    let tooltipParts = [`Processed: ${formatDisplayDate(recording.completed_at || recording.created_at)}`];

                    if (recording.transcription_duration_seconds) {
                        tooltipParts.push(`Transcription: ${formatProcessingDuration(recording.transcription_duration_seconds)}`);
                    }
                    if (recording.summarization_duration_seconds) {
                        tooltipParts.push(`Summarization: ${formatProcessingDuration(recording.summarization_duration_seconds)}`);
                    }

                    const tooltipText = tooltipParts.length > 1 ? tooltipParts.join('\n') : null;

                    metadata.push({
                        icon: 'fas fa-history',
                        text: formatDisplayDate(recording.created_at),
                        fullText: tooltipText
                    });
                }

                if (recording.file_size) {
                    metadata.push({
                        icon: 'fas fa-file-audio',
                        text: formatFileSize(recording.file_size)
                    });
                }

                if (recording.duration) {
                    metadata.push({
                        icon: 'fas fa-clock',
                        text: formatDuration(recording.duration)
                    });
                }

                if (recording.original_filename) {
                    const maxLength = 30;
                    const truncated = recording.original_filename.length > maxLength
                        ? recording.original_filename.substring(0, maxLength) + '...'
                        : recording.original_filename;
                    metadata.push({
                        icon: 'fas fa-file',
                        text: truncated,
                        fullText: recording.original_filename
                    });
                }

                return metadata;
            });

            // Upload queue computed properties
            const totalInQueue = computed(() => uploadQueue.value.length);
            const completedInQueue = computed(() => uploadQueue.value.filter(item => item.status === 'completed' || item.status === 'failed').length);
            // Filter out upload completions that already have a backend job (to avoid duplicates)
            const finishedFilesInQueue = computed(() => {
                const backendRecordingIds = new Set(allJobs.value.map(j => j.recording_id));
                return uploadQueue.value.filter(item =>
                    ['completed', 'failed'].includes(item.status) &&
                    !backendRecordingIds.has(item.recordingId)
                );
            });
            const waitingFilesInQueue = computed(() => uploadQueue.value.filter(item => item.status === 'ready'));
            const pendingQueueFiles = computed(() => uploadQueue.value.filter(item => item.status === 'queued'));

            // Backend processing queue - recordings being processed on the server
            const backendProcessingRecordings = computed(() => {
                return recordings.value.filter(r => ['PENDING', 'PROCESSING', 'SUMMARIZING', 'QUEUED'].includes(r.status));
            });

            // All jobs from backend (queued, processing, completed, failed)
            const allJobs = ref([]);
            let jobQueuePollInterval = null;
            let lastJobQueueFetch = 0; // Timestamp of last fetch
            const JOB_QUEUE_POLL_INTERVAL = 5000;  // Poll every 5 seconds when active
            const JOB_QUEUE_FETCH_DEBOUNCE = 2000; // Minimum 2 seconds between fetches

            // Computed properties for different job states
            const activeJobs = computed(() => allJobs.value.filter(j => ['queued', 'processing'].includes(j.job_status)));
            const completedJobs = computed(() => allJobs.value.filter(j => j.job_status === 'completed'));
            const failedJobs = computed(() => allJobs.value.filter(j => j.job_status === 'failed'));

            // Job queue details map (for backward compatibility with progress popup)
            const jobQueueDetails = computed(() => {
                const detailsMap = {};
                for (const job of allJobs.value) {
                    // Use recording_id as key, store the most relevant job (prefer active over completed)
                    if (!detailsMap[job.recording_id] || ['queued', 'processing'].includes(job.job_status)) {
                        detailsMap[job.recording_id] = job;
                    }
                }
                return detailsMap;
            });

            // Fetch job queue status from backend (with debounce protection)
            const fetchJobQueueStatus = async (force = false) => {
                const now = Date.now();
                // Debounce: skip if fetched recently (unless forced)
                if (!force && (now - lastJobQueueFetch) < JOB_QUEUE_FETCH_DEBOUNCE) {
                    return;
                }
                lastJobQueueFetch = now;

                try {
                    const response = await fetch('/api/recordings/job-queue-status');
                    if (response.ok) {
                        const data = await response.json();
                        allJobs.value = data.jobs || [];
                    } else if (response.status === 429) {
                        console.warn('Job queue polling rate limited');
                    }
                } catch (error) {
                    console.error('Error fetching job queue status:', error);
                }
            };

            // Start polling job queue status
            const startJobQueuePolling = () => {
                if (jobQueuePollInterval) return;
                fetchJobQueueStatus(true); // Fetch immediately (forced)
                jobQueuePollInterval = setInterval(() => fetchJobQueueStatus(true), JOB_QUEUE_POLL_INTERVAL);
            };

            const stopJobQueuePolling = () => {
                if (jobQueuePollInterval) {
                    clearInterval(jobQueuePollInterval);
                    jobQueuePollInterval = null;
                }
            };

            // Check if we have active items that need polling
            const hasActiveProcessing = computed(() => {
                const completedStatuses = ['completed', 'failed', 'COMPLETED', 'FAILED'];
                const hasActiveUploads = uploadQueue.value.some(item =>
                    !completedStatuses.includes(item.status)
                );
                const hasActiveJobs = activeJobs.value.length > 0;
                const hasProcessingRecordings = backendProcessingRecordings.value.length > 0;
                return hasActiveUploads || hasActiveJobs || hasProcessingRecordings;
            });

            // Start/stop polling based on whether we have active items
            watch(hasActiveProcessing, (hasActive) => {
                if (hasActive) {
                    startJobQueuePolling();
                } else {
                    // Stop polling after a delay (to catch final status updates)
                    setTimeout(() => {
                        if (!hasActiveProcessing.value) {
                            stopJobQueuePolling();
                        }
                    }, 10000);
                }
            }, { immediate: true });

            // When popup opens, do a one-time fetch to populate it
            watch(() => progressPopupClosed.value, (closed) => {
                if (!closed) {
                    // Popup just opened - fetch current status
                    fetchJobQueueStatus();
                }
            });

            // Get job details for a recording
            const getJobDetails = (recordingId) => {
                return jobQueueDetails.value[recordingId] || null;
            };

            // Retry a failed job
            const retryJob = async (jobId) => {
                try {
                    const response = await fetch(`/api/recordings/jobs/${jobId}/retry`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    if (response.ok) {
                        fetchJobQueueStatus();
                        showToast('Job queued for retry', 'success');
                    } else {
                        const data = await response.json();
                        showToast(data.error || 'Failed to retry job', 'error');
                    }
                } catch (error) {
                    console.error('Error retrying job:', error);
                    showToast('Failed to retry job', 'error');
                }
            };

            // Delete/clear a job
            const deleteJob = async (jobId) => {
                try {
                    const response = await fetch(`/api/recordings/jobs/${jobId}`, {
                        method: 'DELETE'
                    });
                    if (response.ok) {
                        fetchJobQueueStatus();
                    } else {
                        const data = await response.json();
                        showToast(data.error || 'Failed to delete job', 'error');
                    }
                } catch (error) {
                    console.error('Error deleting job:', error);
                }
            };

            // Clear all completed jobs
            const clearCompletedJobs = async () => {
                try {
                    const response = await fetch('/api/recordings/jobs/clear-completed', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    if (response.ok) {
                        // Clear upload queue completed/failed items
                        uploadQueue.value = uploadQueue.value.filter(item =>
                            !['completed', 'failed', 'COMPLETED', 'FAILED'].includes(item.status)
                        );
                        // Force fetch to update the job list (bypass debounce)
                        await fetchJobQueueStatus(true);
                    }
                } catch (error) {
                    console.error('Error clearing completed jobs:', error);
                }
            };

            // Combined clear function for backward compatibility
            const clearAllCompleted = () => {
                clearCompletedJobs();
            };

            // ============================================
            // UNIFIED PROGRESS TRACKING SYSTEM
            // Merges upload queue, backend recordings, and job queue into single list
            // Each recording appears ONCE with its current status
            // ============================================
            const unifiedProgressItems = computed(() => {
                const items = new Map(); // Key by recordingId or clientId

                // Get the currently uploading file's clientId for special handling
                const currentUploadClientId = currentlyProcessingFile.value?.clientId;

                // 1. First, add all backend jobs (these have the most accurate status)
                for (const job of allJobs.value) {
                    const key = `rec_${job.recording_id}`;
                    const existing = items.get(key);

                    // Determine unified status from job
                    let unifiedStatus = 'queued';
                    if (job.job_status === 'processing') {
                        unifiedStatus = job.queue_type === 'summary' ? 'summarizing' : 'transcribing';
                    } else if (job.job_status === 'completed') {
                        unifiedStatus = 'completed';
                    } else if (job.job_status === 'failed') {
                        unifiedStatus = 'failed';
                    }

                    // Prefer active jobs over completed/failed
                    if (!existing || ['queued', 'transcribing', 'summarizing'].includes(unifiedStatus)) {
                        items.set(key, {
                            id: key,
                            recordingId: job.recording_id,
                            jobId: job.id,
                            clientId: null,
                            title: job.recording_title || 'Untitled',
                            status: unifiedStatus,
                            progress: unifiedStatus === 'transcribing' ? 50 : (unifiedStatus === 'summarizing' ? 80 : null),
                            progressMessage: unifiedStatus === 'queued' ? `#${job.position || '?'} in queue` :
                                             unifiedStatus === 'transcribing' ? 'Transcribing audio...' :
                                             unifiedStatus === 'summarizing' ? 'Generating summary...' :
                                             unifiedStatus === 'completed' ? 'Done' : 'Failed',
                            queuePosition: job.position,
                            errorMessage: job.error_message,
                            completedAt: job.completed_at,
                            source: 'job'
                        });
                    }
                }

                // 2. Add upload queue items (client-side tracking)
                for (const upload of uploadQueue.value) {
                    // Check if this is the currently uploading file
                    const isCurrentUpload = upload.clientId === currentUploadClientId;

                    // If we have a recordingId and it's already tracked from jobs, check if we should merge
                    if (upload.recordingId) {
                        const key = `rec_${upload.recordingId}`;
                        const existing = items.get(key);

                        if (existing) {
                            // Merge upload info into existing item
                            existing.clientId = upload.clientId;
                            existing.file = upload.file;
                            // If this is the current upload, use the global progress refs
                            if (isCurrentUpload && ['uploading', 'pending', 'processing', 'summarizing'].includes(upload.status)) {
                                // Map upload status to unified status
                                if (upload.status === 'uploading') {
                                    existing.status = 'uploading';
                                } else if (upload.status === 'summarizing') {
                                    existing.status = 'summarizing';
                                } else if (upload.status === 'processing' || upload.status === 'pending') {
                                    existing.status = 'transcribing';
                                }
                                existing.progress = processingProgress.value;
                                existing.progressMessage = processingMessage.value || 'Processing...';
                                existing.title = upload.displayName || upload.file?.name || existing.title;
                            }
                            continue;
                        }
                    }

                    // Determine unified status from upload status
                    let unifiedStatus = 'ready';
                    let progressVal = 0;
                    let progressMsg = 'Waiting to upload...';

                    // If this is the currently processing file, use global progress refs
                    if (isCurrentUpload) {
                        progressVal = processingProgress.value;
                        progressMsg = processingMessage.value || 'Processing...';

                        if (upload.status === 'uploading') {
                            unifiedStatus = 'uploading';
                        } else if (upload.status === 'pending' || upload.status === 'processing' || upload.status === 'PROCESSING') {
                            unifiedStatus = 'transcribing';
                        } else if (upload.status === 'summarizing' || upload.status === 'SUMMARIZING') {
                            unifiedStatus = 'summarizing';
                        } else if (upload.status === 'completed' || upload.status === 'COMPLETED') {
                            unifiedStatus = 'completed';
                            progressMsg = 'Done';
                        } else if (upload.status === 'failed' || upload.status === 'FAILED') {
                            unifiedStatus = 'failed';
                            progressMsg = upload.error || 'Processing failed';
                        } else if (upload.status === 'ready') {
                            unifiedStatus = 'ready';
                            progressMsg = 'Waiting to upload...';
                        }
                    } else {
                        // Not the current upload - use item's own status
                        if (upload.status === 'uploading') {
                            unifiedStatus = 'uploading';
                            progressMsg = 'Uploading...';
                        } else if (upload.status === 'completed' || upload.status === 'COMPLETED') {
                            unifiedStatus = 'completed';
                            progressMsg = 'Done';
                        } else if (upload.status === 'failed' || upload.status === 'FAILED') {
                            unifiedStatus = 'upload_failed';
                            progressMsg = upload.error || 'Upload failed';
                        } else if (upload.status === 'ready') {
                            unifiedStatus = 'ready';
                            progressMsg = 'Waiting to upload...';
                        } else if (upload.status === 'queued') {
                            unifiedStatus = 'ready';
                            progressMsg = 'Waiting to upload...';
                        }
                    }

                    const key = upload.recordingId ? `rec_${upload.recordingId}` : `client_${upload.clientId}`;

                    // Skip if we already have an entry with the same recordingId (from jobs)
                    if (upload.recordingId && items.has(key)) {
                        continue;
                    }

                    items.set(key, {
                        id: key,
                        recordingId: upload.recordingId,
                        jobId: null,
                        clientId: upload.clientId,
                        title: upload.displayName || upload.file?.name || 'Unknown file',
                        status: unifiedStatus,
                        progress: progressVal,
                        progressMessage: progressMsg,
                        queuePosition: null,
                        errorMessage: upload.status === 'failed' ? upload.error : null,
                        file: upload.file,
                        source: 'upload'
                    });
                }

                // Convert to array and sort: active first, then by status priority
                const statusOrder = {
                    'uploading': 1,
                    'transcribing': 2,
                    'summarizing': 3,
                    'queued': 4,
                    'ready': 5,
                    'completed': 6,
                    'failed': 7,
                    'upload_failed': 8
                };

                return Array.from(items.values()).sort((a, b) => {
                    return (statusOrder[a.status] || 99) - (statusOrder[b.status] || 99);
                });
            });

            // Filtered views of unified items
            const activeProgressItems = computed(() =>
                unifiedProgressItems.value.filter(item =>
                    ['uploading', 'transcribing', 'summarizing', 'queued', 'ready'].includes(item.status)
                )
            );

            const completedProgressItems = computed(() =>
                unifiedProgressItems.value.filter(item => item.status === 'completed')
            );

            const failedProgressItems = computed(() =>
                unifiedProgressItems.value.filter(item =>
                    ['failed', 'upload_failed'].includes(item.status)
                )
            );

            // Helper to get status display info
            const getStatusDisplay = (status) => {
                const displays = {
                    'ready': { label: 'Waiting', color: 'gray', icon: 'fa-clock' },
                    'uploading': { label: 'Uploading', color: 'blue', icon: 'fa-cloud-upload-alt', animate: true },
                    'queued': { label: 'Queued', color: 'yellow', icon: 'fa-clock' },
                    'transcribing': { label: 'Transcribing', color: 'purple', icon: 'fa-microphone-alt', animate: true },
                    'summarizing': { label: 'Summarizing', color: 'green', icon: 'fa-file-alt', animate: true },
                    'completed': { label: 'Done', color: 'green', icon: 'fa-check-circle' },
                    'failed': { label: 'Failed', color: 'red', icon: 'fa-exclamation-circle' },
                    'upload_failed': { label: 'Upload Failed', color: 'red', icon: 'fa-exclamation-circle' }
                };
                return displays[status] || displays['ready'];
            };

            // Cancel/remove an item from the queue
            const removeProgressItem = async (item) => {
                if (item.jobId && ['failed', 'completed'].includes(item.status)) {
                    // Delete backend job
                    await deleteJob(item.jobId);
                } else if (item.clientId && !item.jobId) {
                    // Remove from upload queue
                    uploadQueue.value = uploadQueue.value.filter(u => u.clientId !== item.clientId);
                }
            };

            // Retry a failed item
            const retryProgressItem = async (item) => {
                if (item.jobId) {
                    await retryJob(item.jobId);
                }
            };

            // Track recently completed for backward compat (now using allJobs)
            const recentlyCompletedBackend = computed(() => {
                return completedJobs.value.map(j => ({
                    id: j.recording_id,
                    title: j.recording_title || 'Untitled',
                    status: 'completed',
                    completedAt: j.completed_at
                }));
            });

            // Combined processing queue count
            const totalProcessingCount = computed(() => {
                return activeProgressItems.value.length;
            });

            // Should show the processing popup
            const showProcessingPopup = computed(() => {
                return unifiedProgressItems.value.length > 0;
            });

            // All completed items count
            const allCompletedCount = computed(() => {
                return completedProgressItems.value.length + failedProgressItems.value.length;
            });

            // Speaker computed properties
            const hasSpeakerNames = computed(() => {
                // Check if any speaker has a non-empty name
                return Object.values(speakerMap.value).some(speakerData =>
                    speakerData && speakerData.name && speakerData.name.trim() !== ''
                );
            });

            // Tags with custom prompts for reprocess modal
            const tagsWithCustomPrompts = computed(() => {
                return availableTags.value.filter(tag => tag.custom_prompt && tag.custom_prompt.trim() !== '');
            });

            // Get tag prompt preview
            const getTagPromptPreview = (tagId) => {
                const tag = availableTags.value.find(t => t.id == tagId);
                if (tag && tag.custom_prompt) {
                    // Return first 100 characters of the custom prompt
                    return tag.custom_prompt.length > 100
                        ? tag.custom_prompt.substring(0, 100) + '...'
                        : tag.custom_prompt;
                }
                return '';
            };

            // Tag functions from composable (using composable's implementations)

            // =========================================================================
            // WATCHERS
            // =========================================================================
            // Watch for search query changes
            watch(searchQuery, (newQuery) => {
                recordingsComposable.debouncedSearch(newQuery);
            });

            // Auto-apply filters when they change
            watch(filterTags, () => {
                recordingsComposable.applyAdvancedFilters();
            }, { deep: true });

            watch(filterDatePreset, () => {
                recordingsComposable.applyAdvancedFilters();
            });

            watch(filterDateRange, () => {
                recordingsComposable.applyAdvancedFilters();
            }, { deep: true });

            watch(filterTextQuery, (newValue) => {
                clearTimeout(searchDebounceTimer.value);
                searchDebounceTimer.value = setTimeout(() => {
                    recordingsComposable.applyAdvancedFilters();
                }, 300);
            });

            watch(showArchivedRecordings, () => {
                // Reload recordings when switching between archived/normal view
                if (showArchivedRecordings.value) {
                    showSharedWithMe.value = false;  // Can't show both at once
                }
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(showSharedWithMe, () => {
                // Reload recordings when switching to/from shared view
                if (showSharedWithMe.value) {
                    showArchivedRecordings.value = false;  // Can't show both at once
                }
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            // Watch for view changes to initialize recording notes editor
            watch(currentView, async (newView, oldView) => {
                if (newView === 'recording') {
                    // Initialize recording notes editor when entering recording view
                    await nextTick();
                    uiComposable.initializeRecordingNotesEditor();
                } else if (oldView === 'recording') {
                    // Destroy editor when leaving recording view
                    uiComposable.destroyRecordingNotesEditor();
                }
            });

            // Watch for mobile tab changes to reinitialize editors if still in edit mode
            watch(mobileTab, async (newTab) => {
                // Wait for DOM to update
                await nextTick();

                // If switching to summary tab and still in edit mode, reinitialize editor
                if (newTab === 'summary' && editingSummary.value) {
                    uiComposable.initializeSummaryMarkdownEditor();
                }

                // If switching to notes tab and still in edit mode, reinitialize editor
                if (newTab === 'notes' && editingNotes.value) {
                    uiComposable.initializeMarkdownEditor();
                }
            });

            // Watch for desktop tab changes to reinitialize editors if still in edit mode
            watch(selectedTab, async (newTab) => {
                // Wait for DOM to update
                await nextTick();

                // If switching to summary tab and still in edit mode, reinitialize editor
                if (newTab === 'summary' && editingSummary.value) {
                    uiComposable.initializeSummaryMarkdownEditor();
                }

                // If switching to notes tab and still in edit mode, reinitialize editor
                if (newTab === 'notes' && editingNotes.value) {
                    uiComposable.initializeMarkdownEditor();
                }
            });

            // Watch for selectedRecording changes to reset chat
            watch(selectedRecording, (newRecording, oldRecording) => {
                // Only clear if we're actually switching to a different recording
                if (oldRecording && newRecording && oldRecording.id !== newRecording.id) {
                    chatMessages.value = [];
                    chatInput.value = '';
                }
            });

            // =========================================================================
            // LIFECYCLE
            // =========================================================================
            onMounted(async () => {
                // Get config from data attributes
                const appElement = document.getElementById('app');
                if (appElement) {
                    useAsrEndpoint.value = appElement.dataset.useAsrEndpoint === 'True';
                    currentUserName.value = appElement.dataset.currentUserName || '';
                }

                // Initialize UI
                uiComposable.initializeDarkMode();
                uiComposable.initializeColorScheme();
                uiComposable.initializeSidebar();

                // Check for recoverable recording from IndexedDB
                try {
                    const recoverable = await audioComposable.checkForRecoverableRecording();
                    if (recoverable && recoverable.chunks && recoverable.chunks.length > 0) {
                        recoverableRecording.value = recoverable;
                        showRecoveryModal.value = true;
                        console.log('[App] Found recoverable recording, showing recovery dialog');
                    }
                } catch (error) {
                    console.error('[App] Failed to check for recoverable recording:', error);
                }

                // Load initial data
                await Promise.all([
                    recordingsComposable.loadRecordings(),
                    recordingsComposable.loadTags()
                ]);

                // Load config
                try {
                    const response = await fetch('/api/config');
                    if (response.ok) {
                        const config = await response.json();
                        maxFileSizeMB.value = config.max_file_size_mb || 250;
                        chunkingEnabled.value = config.chunking_enabled !== false;
                        chunkingMode.value = config.chunking_mode || 'size';
                        chunkingLimit.value = config.chunking_limit || 20;
                        recordingDisclaimer.value = config.recording_disclaimer || '';
                        canDeleteRecordings.value = config.can_delete_recordings !== false;
                        enableInternalSharing.value = config.enable_internal_sharing === true;
                        enableArchiveToggle.value = config.enable_archive_toggle === true;
                        showUsernamesInUI.value = config.show_usernames_in_ui === true;
                    }
                } catch (error) {
                    console.error('Failed to load config:', error);
                }

                // Initialize UI settings from localStorage
                uiComposable.initializeUI();

                // Initialize audio capabilities
                await audioComposable.initializeAudio();

                // Initialize PWA features
                pwaComposable.initPWA();

                // Show app - hide loader and show main content
                const loader = document.getElementById('loader');
                const appEl = document.getElementById('app');
                if (loader) {
                    loader.style.opacity = '0';
                    setTimeout(() => {
                        loader.style.display = 'none';
                    }, 500);
                }
                if (appEl) {
                    appEl.style.opacity = '1';
                    appEl.classList.remove('opacity-0');
                }

                // Also hide AppLoader overlay if it exists
                if (window.AppLoader) {
                    window.AppLoader.hide();
                }

                // Window resize handler
                window.addEventListener('resize', () => {
                    windowWidth.value = window.innerWidth;
                });

                // Visibility change handler for wake lock
                document.addEventListener('visibilitychange', audioComposable.handleVisibilityChange);

                // Prevent data loss on tab close/refresh during recording
                window.addEventListener('beforeunload', (e) => {
                    if (audioComposable.hasUnsavedRecording()) {
                        e.preventDefault();
                        e.returnValue = ''; // Chrome requires this
                        return 'You have an unsaved recording. Are you sure you want to leave?';
                    }
                });
            });

            // =========================================================================
            // RECORDING RECOVERY FUNCTIONS
            // =========================================================================

            const recoverRecording = async () => {
                try {
                    showRecoveryModal.value = false;

                    const recovered = await audioComposable.recoverRecordingFromDB();
                    if (recovered) {
                        currentView.value = 'recording';
                        showToast('Recording recovered successfully', 'success');
                    } else {
                        showToast('Failed to recover recording', 'error');
                    }

                    recoverableRecording.value = null;
                } catch (error) {
                    console.error('[App] Failed to recover recording:', error);
                    showToast('Error recovering recording', 'error');
                }
            };

            const cancelRecovery = async () => {
                try {
                    showRecoveryModal.value = false;

                    // Clear the recording from IndexedDB
                    await audioComposable.clearRecordingSession();

                    showToast('Recording discarded', 'info');
                    recoverableRecording.value = null;
                } catch (error) {
                    console.error('[App] Failed to discard recording:', error);
                }
            };

            const formatRecordingMode = (mode) => {
                const modes = {
                    'microphone': t('recording.modeMicrophone'),
                    'system': t('recording.modeSystem'),
                    'both': t('recording.modeBoth')
                };
                return modes[mode] || mode;
            };

            // =========================================================================
            // WATCHERS
            // =========================================================================

            // Update badge count when recordings change
            watch(recordings, (newRecordings) => {
                if (newRecordings && Array.isArray(newRecordings)) {
                    pwaComposable.updateBadgeCount(newRecordings);
                }
            });

            // =========================================================================
            // RETURN ALL STATE AND METHODS
            // =========================================================================
            return {
                // Translation
                t, tc,

                // State
                ...state,

                // Computed
                isMobileScreen,
                isMobileDevice,
                processedTranscription,
                groupedRecordings,
                filteredAvailableTags,
                selectedTags,
                colorSchemes,
                dropdownPositions,
                toasts,
                datePresetOptions,
                languageOptions,
                activeRecordingMetadata,
                totalInQueue,
                completedInQueue,
                finishedFilesInQueue,
                waitingFilesInQueue,
                pendingQueueFiles,
                backendProcessingRecordings,
                totalProcessingCount,
                showProcessingPopup,
                jobQueueDetails,
                getJobDetails,
                allJobs,
                activeJobs,
                completedJobs,
                failedJobs,
                retryJob,
                deleteJob,
                clearCompletedJobs,
                recentlyCompletedBackend,
                clearAllCompleted,
                allCompletedCount,
                // Unified progress tracking
                unifiedProgressItems,
                activeProgressItems,
                completedProgressItems,
                failedProgressItems,
                getStatusDisplay,
                removeProgressItem,
                retryProgressItem,
                hasSpeakerNames,
                tagsWithCustomPrompts,
                getTagPromptPreview,

                // Utilities
                formatFileSize,
                formatDisplayDate,
                formatShortDate,
                formatStatus,
                getStatusClass,
                formatTime,
                formatDuration,
                formatEventDateTime,
                formatDateTime: formatEventDateTime, // Alias for recovery modal
                setGlobalError,
                showToast,
                getContrastTextColor,
                getBubbleGlobalIndex,
                formatRecordingMode,

                // Recording recovery
                showRecoveryModal,
                recoverableRecording,
                recoverRecording,
                cancelRecovery,

                // Composable methods
                ...recordingsComposable,
                ...uploadComposable,
                ...audioComposable,
                ...uiComposable,
                ...modalsComposable,
                ...sharingComposable,
                ...reprocessComposable,
                ...transcriptionComposable,
                ...speakersComposable,
                ...chatComposable,
                ...tagsComposable,
                ...pwaComposable
            };
        },
        delimiters: ['${', '}']
    });

    app.config.globalProperties.t = safeT;
    app.config.globalProperties.tc = (key, count, params = {}) => {
        if (!window.i18n || !window.i18n.tc) {
            return key;
        }
        return window.i18n.tc(key, count, params);
    };

    app.provide('t', safeT);
    app.provide('tc', (key, count, params = {}) => {
        if (!window.i18n || !window.i18n.tc) {
            return key;
        }
        return window.i18n.tc(key, count, params);
    });

    app.mount('#app');
});
