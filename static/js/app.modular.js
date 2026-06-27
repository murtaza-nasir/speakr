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
import { useVirtualScroll, getVirtualItemKey } from './modules/composables/virtualScroll.js';
import { useBulkSelection } from './modules/composables/bulk-selection.js';
import { useBulkOperations } from './modules/composables/bulk-operations.js';
import { useFolders } from './modules/composables/folders.js';

// Import utilities
import { showToast } from './modules/utils/toast.js';
import { getContrastTextColor } from './modules/utils/colors.js';
import { buildVariableList } from './modules/utils/prompt-variables.js';
import { detectPlatform, getAudioCapabilities, enumerateVirtualAudioDevices } from './modules/utils/platform.js';

// Number of speaker colors available in CSS (must match styles.css)
const SPEAKER_COLOR_COUNT = 16;

// Parse transcription text to detect if it's an error message
const parseTranscriptionError = (text) => {
    if (!text) return null;

    // Check for JSON-formatted error from backend
    if (text.startsWith('ERROR_JSON:')) {
        try {
            const jsonStr = text.substring(11);
            const data = JSON.parse(jsonStr);
            const _t = (key, fb) => (window.i18n && window.i18n.t) ? window.i18n.t(key) : fb;
            return {
                title: data.t || _t('errors.fallbackTitle', 'Error'),
                message: data.m || _t('errors.fallbackMessage', 'An error occurred'),
                guidance: data.g || '',
                icon: data.i || 'fa-exclamation-circle',
                type: data.y || 'unknown',
                isKnown: data.k || false,
                technical: data.d || ''
            };
        } catch (e) {
            console.error('Failed to parse error JSON:', e);
        }
    }

    // Check for legacy error format
    const errorPrefixes = [
        'Transcription failed:',
        'Processing failed:',
        'ASR processing failed:',
        'Audio extraction failed:'
    ];

    for (const prefix of errorPrefixes) {
        if (text.startsWith(prefix)) {
            return parseUnformattedError(text);
        }
    }

    return null;
};

// Parse unformatted error messages and make them user-friendly
const parseUnformattedError = (text) => {
    const _t = (key, fb) => (window.i18n && window.i18n.t) ? window.i18n.t(key) : fb;
    const lowerText = text.toLowerCase();

    // Known error patterns
    const patterns = [
        {
            patterns: ['maximum content size limit', 'file too large', '413', 'payload too large', 'exceeded'],
            title: _t('errors.fileTooLargeTitle', 'File Too Large'),
            message: _t('errors.fileTooLargeMessage', 'The audio file exceeds the maximum size allowed by the transcription service.'),
            guidance: _t('errors.fileTooLargeGuidance', 'Try enabling audio chunking in your settings, or compress the audio file before uploading.'),
            icon: 'fa-file-audio',
            type: 'size_limit'
        },
        {
            patterns: ['timed out', 'timeout', 'deadline exceeded'],
            title: _t('errors.processingTimeout', 'Processing Timeout'),
            message: _t('errors.processingTimeoutMessage', 'The transcription took too long to complete.'),
            guidance: _t('errors.processingTimeoutGuidance', 'This can happen with very long recordings. Try splitting the audio into smaller parts.'),
            icon: 'fa-clock',
            type: 'timeout'
        },
        {
            patterns: ['401', 'unauthorized', 'invalid api key', 'authentication failed', 'incorrect api key'],
            title: _t('errors.authenticationError', 'Authentication Error'),
            message: _t('errors.authenticationErrorMessage', 'The transcription service rejected the API credentials.'),
            guidance: _t('errors.authenticationErrorGuidance', 'Please check that the API key is correct and has not expired.'),
            icon: 'fa-key',
            type: 'auth'
        },
        {
            patterns: ['rate limit', 'too many requests', '429', 'quota exceeded'],
            title: _t('errors.rateLimitExceeded', 'Rate Limit Exceeded'),
            message: _t('errors.rateLimitExceededMessage', 'Too many requests were sent to the transcription service.'),
            guidance: _t('errors.rateLimitExceededGuidance', 'Please wait a few minutes and try reprocessing.'),
            icon: 'fa-hourglass-half',
            type: 'rate_limit'
        },
        {
            patterns: ['connection refused', 'connection reset', 'could not connect', 'network unreachable'],
            title: _t('errors.connectionError', 'Connection Error'),
            message: _t('errors.connectionErrorMessage', 'Could not connect to the transcription service.'),
            guidance: _t('errors.connectionErrorGuidance', 'Check your internet connection and ensure the service is available.'),
            icon: 'fa-wifi',
            type: 'connection'
        },
        {
            patterns: ['503', '502', '500', 'service unavailable', 'server error', 'internal server error'],
            title: _t('errors.serviceUnavailable', 'Service Unavailable'),
            message: _t('errors.serviceUnavailableMessage', 'The transcription service is temporarily unavailable.'),
            guidance: _t('errors.serviceUnavailableGuidance', 'This is usually temporary. Please try again in a few minutes.'),
            icon: 'fa-server',
            type: 'service_error'
        },
        {
            patterns: ['invalid file format', 'unsupported format', 'could not decode', 'corrupt', 'not valid audio'],
            title: _t('errors.invalidAudioFormat', 'Invalid Audio Format'),
            message: _t('errors.invalidAudioFormatMessage', 'The audio file format is not supported or the file may be corrupted.'),
            guidance: _t('errors.invalidAudioFormatGuidance', 'Try converting the audio to MP3 or WAV format before uploading.'),
            icon: 'fa-file-audio',
            type: 'format'
        },
        {
            patterns: ['audio extraction failed', 'ffmpeg failed', 'no audio stream'],
            title: _t('errors.audioExtractionFailed', 'Audio Extraction Failed'),
            message: _t('errors.audioExtractionFailedMessage', 'Could not extract audio from the uploaded file.'),
            guidance: _t('errors.audioExtractionFailedGuidance', 'Try converting the file to a standard audio format (MP3, WAV) before uploading.'),
            icon: 'fa-file-video',
            type: 'extraction'
        }
    ];

    // Check patterns
    for (const pattern of patterns) {
        for (const p of pattern.patterns) {
            if (lowerText.includes(p)) {
                return {
                    title: pattern.title,
                    message: pattern.message,
                    guidance: pattern.guidance,
                    icon: pattern.icon,
                    type: pattern.type,
                    isKnown: true,
                    technical: text
                };
            }
        }
    }

    // Unknown error - clean it up
    let cleanMessage = text;
    for (const prefix of ['Transcription failed:', 'Processing failed:', 'Error:', 'ASR processing failed:']) {
        if (cleanMessage.startsWith(prefix)) {
            cleanMessage = cleanMessage.substring(prefix.length).trim();
        }
    }

    // Truncate if too long
    if (cleanMessage.length > 200) {
        cleanMessage = cleanMessage.substring(0, 200) + '...';
    }

    return {
        title: _t('errors.processingError', 'Processing Error'),
        message: cleanMessage,
        guidance: _t('errors.processingErrorGuidance', 'If this error persists, try reprocessing the recording.'),
        icon: 'fa-exclamation-circle',
        type: 'unknown',
        isKnown: false,
        technical: text
    };
};

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
            // Initial view is null (the recordings list / empty state).
            // Was 'upload' which made the old full-screen upload view the
            // landing surface; now that upload is a modal, defaulting to
            // null lets the user land on their recordings list and open
            // the modal explicitly via the New Recording button.
            const currentView = ref(null);
            // Upload visibility is a modal flag, not a currentView value,
            // so the underlying list / detail stays mounted behind the
            // overlay. Set true on switchToUploadView / opened paths;
            // cleared by closeUploadView and the upload-success transition
            // to the detail view. Defaults to false so the user lands on
            // the recordings list / empty state, not on an auto-open modal.
            const showUploadModal = ref(false);
            // Set true on mount when arriving via the ?upload=1 deep-link
            // (inquire mode's "+ New Recording"). The auto-select of the last
            // recording during loadRecordings calls selectRecording(), which
            // resets showUploadModal=false AFTER an awaited fetch — beating any
            // setTimeout race. This flag tells that first selectRecording to
            // leave the modal alone (and consumes itself) so the deep-linked
            // upload modal stays open behind the freshly-loaded detail view.
            const uploadDeepLinkPending = ref(false);
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
            const filterSpeakers = ref([]);
            const filterTagSearch = ref('');
            const filterSpeakerSearch = ref('');
            const filterDateRange = ref({ start: '', end: '' });
            const filterDatePreset = ref('');
            const filterTextQuery = ref('');
            const filterStarred = ref(false);
            const filterInbox = ref(false);
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
            // Restore the sort choice from a previous session so users do not
            // have to switch to Meeting date every time they open the app
            // (discussion #263). Validate against the known set so a stale or
            // tampered localStorage value cannot put us in an unknown state.
            const _savedSortBy = localStorage.getItem('recordingsSortBy');
            const sortBy = ref((_savedSortBy === 'meeting_date' || _savedSortBy === 'created_at')
                ? _savedSortBy
                : 'created_at');
            const selectedTagFilter = ref(null);

            // --- UI State ---
            const browser = ref('unknown');
            const isSidebarCollapsed = ref(false);
            const searchTipsExpanded = ref(false);
            const isUserMenuOpen = ref(false);
            // Detail-header "assign folder" dropdown (replaces a raw <select>
            // whose OS-rendered option list ignored the app theme).
            const showHeaderFolderMenu = ref(false);
            const tokenBudget = ref({
                has_budget: false,
                budget: null,
                usage: 0,
                percentage: 0
            });
            const isDarkMode = ref(false);
            const currentColorScheme = ref('blue');
            const showColorSchemeModal = ref(false);
            const windowWidth = ref(window.innerWidth);
            const mobileTab = ref('summary');  // default landing tab post-transcription
            const mobileMoreOpen = ref(false);  // bottom-nav More overflow sheet
            const isMetadataExpanded = ref(false);
            const expandedSection = ref('settings');  // 'notes' or 'settings' for recording view accordion
            const showSortOptions = ref(false);

            // --- i18n State ---
            const currentLanguage = ref('en');
            const currentLanguageName = ref('English');
            const availableLanguages = ref([]);
            const showLanguageMenu = ref(false);

            // --- Upload State ---
            const uploadQueue = ref([]);
            const allJobs = ref([]); // Backend job queue (queued, processing, completed, failed)
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
            const maxConcurrentUploads = ref(3);
            const recordingDisclaimer = ref('');
            const showRecordingDisclaimerModal = ref(false);
            const pendingRecordingMode = ref(null);
            const uploadDisclaimer = ref('');
            const showUploadDisclaimerModal = ref(false);
            const customBanner = ref('');
            const showBanner = ref(true);

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
            // When true the microphone getUserMedia call is made WITHOUT
            // echoCancellation / noiseSuppression / autoGainControl —
            // appropriate when the user is routing system audio in via a
            // PulseAudio monitor source or a virtual audio device (BlackHole,
            // VB-Cable). The default-on suppression kills sustained speech
            // audio from monitor sources because the algorithm classifies it
            // as "noise". Persisted across sessions in localStorage.
            const disableAudioProcessing = ref(
                (typeof localStorage !== 'undefined' && localStorage.getItem('disableAudioProcessing') === 'true')
            );
            watch(disableAudioProcessing, (v) => {
                try { localStorage.setItem('disableAudioProcessing', v ? 'true' : 'false'); } catch (_) {}
            });

            // Per-input-device state for the multi-source recording flow.
            // The user can pick a PRIMARY input (their mic) and an OPTIONAL
            // SECONDARY input (e.g. a Pulse monitor source / BlackHole / VB-
            // Cable) — both are captured via getUserMedia and mixed in Web
            // Audio so the recording captures the user AND whatever's
            // playing through their speakers in one stream. Device IDs are
            // persisted in localStorage so the user only configures once.
            // Labels populate only AFTER mic permission is granted; the
            // first grant triggers refreshInputAudioDevices to repopulate
            // the dropdowns with real names.
            const inputAudioDevices = ref([]);
            const selectedMicDeviceId = ref(
                (typeof localStorage !== 'undefined' && localStorage.getItem('selectedMicDeviceId')) || ''
            );
            const selectedSecondaryDeviceId = ref(
                (typeof localStorage !== 'undefined' && localStorage.getItem('selectedSecondaryDeviceId')) || ''
            );
            watch(selectedMicDeviceId, (v) => {
                try { localStorage.setItem('selectedMicDeviceId', v || ''); } catch (_) {}
            });
            watch(selectedSecondaryDeviceId, (v) => {
                try { localStorage.setItem('selectedSecondaryDeviceId', v || ''); } catch (_) {}
            });
            const refreshInputAudioDevices = async () => {
                if (typeof navigator === 'undefined' || !navigator.mediaDevices) return;
                try {
                    const devs = await navigator.mediaDevices.enumerateDevices();
                    inputAudioDevices.value = devs
                        .filter(d => d.kind === 'audioinput')
                        .map(d => ({
                            deviceId: d.deviceId,
                            label: d.label || '',
                            // Tag virtual/monitor devices so the UI can badge them
                            isVirtual: /\bmonitor of |blackhole|loopback|soundflower|vb[- ]?(audio|cable)|voicemeeter|stereo mix|what u hear|pulse.*monitor|pipewire.*monitor|monitor source/i.test(d.label || '')
                        }));
                } catch (_) {
                    inputAudioDevices.value = [];
                }
            };
            refreshInputAudioDevices();
            // Platform + capability state for the system-audio flow.
            // Detected once on init; the help modal uses these to
            // pre-select the right OS tab and to gate which paragraphs
            // it shows. Virtual audio devices are scanned after the
            // user grants a microphone permission since labels are
            // empty before then.
            const platformInfo = ref(detectPlatform());
            const audioCaps = ref(getAudioCapabilities());
            const helpModalOsTab = ref(platformInfo.value.os === 'iOS' || platformInfo.value.os === 'Android'
                ? 'macOS' // mobile users get the desktop guide as a default; the modal still shows a mobile note
                : (platformInfo.value.os === 'ChromeOS' ? 'Windows' : (platformInfo.value.os || 'Windows')));
            const virtualAudioDevices = ref([]);
            const refreshVirtualAudioDevices = async () => {
                try {
                    virtualAudioDevices.value = await enumerateVirtualAudioDevices();
                } catch (_) {
                    virtualAudioDevices.value = [];
                }
            };
            // Initial probe — labels are usually empty pre-permission
            // so the result is often []; refreshed again after the
            // first getUserMedia / getDisplayMedia grant.
            refreshVirtualAudioDevices();
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

            // --- Incognito Mode State ---
            const enableIncognitoMode = ref(false);  // Server config
            const incognitoMode = ref(false);
            const incognitoRecording = ref(null);
            const incognitoProcessing = ref(false);

            // --- Video / Audio-Only Upload State ---
            // Server config: whether VIDEO_RETENTION is enabled. Controls
            // toggle visibility (when on, the toggle is shown for video
            // files; when off, the system always extracts audio and the
            // toggle stays hidden — the larger size limit applies
            // implicitly to video files).
            const videoRetentionEnabled = ref(false);
            // Per-upload override (toggle in the upload form). Sent as
            // form field `keep_audio_only=true` when on, recorded on the
            // Recording row, consumed by the processing pipeline.
            const keepAudioOnly = ref(false);
            // Server config: cap for audio-only-mode video uploads, in MB.
            const maxAudioOnlyVideoSizeMB = ref(1000);

            // --- Bulk Selection State ---
            const selectionMode = ref(false);
            const selectedRecordingIds = ref(new Set());
            const bulkActionInProgress = ref(false);

            // --- Recording Size Monitoring ---
            const estimatedFileSize = ref(0);
            const fileSizeWarningShown = ref(false);
            const recordingQuality = ref('optimized');
            const actualBitrate = ref(0);
            const maxRecordingMB = ref(200);
            const sizeCheckInterval = ref(null);

            // Advanced Options for ASR
            const showAdvancedOptions = ref(false);
            const userTranscriptionLanguage = ref('');  // User's default from account settings
            // Personal and site-default summary prompts. Used by the upload
            // form to surface `{{variable}}` placeholders that would otherwise
            // substitute to empty strings.
            const userSummaryPrompt = ref('');
            const adminDefaultSummaryPrompt = ref('');
            const uploadLanguage = ref('');
            const uploadMinSpeakers = ref('');
            const uploadMaxSpeakers = ref('');
            const uploadHotwords = ref('');
            const uploadInitialPrompt = ref('');
            // Saved transcription initial-prompt templates (#309), shown as a
            // picker above the initial-prompt field in the upload modal.
            const initialPromptTemplates = ref([]);
            const applyInitialPromptTemplate = (id) => {
                const tpl = initialPromptTemplates.value.find(t => String(t.id) === String(id));
                if (!tpl) return;
                // A transcription template applies BOTH its prompt and hotwords.
                uploadInitialPrompt.value = tpl.template || '';
                uploadHotwords.value = tpl.hotwords || '';
            };
            // Detail-view popover showing which hotwords/initial-prompt a
            // recording actually used (#309).
            const showHintsPopover = ref(false);
            const uploadTranscriptionModel = ref('');
            const uploadPromptVariables = reactive({});  // {variableName: value}
            const showPromptVariablesPanel = ref(true);  // expander state
            const transcriptionModelOptions = ref([]);

            // Tag Selection
            const availableTags = ref([]);
            const selectedTagIds = ref([]);
            const uploadTagSearchFilter = ref('');

            // Folder Selection
            const availableFolders = ref([]);
            const selectedFolderId = ref(null);
            const foldersEnabled = ref(false);
            const filterFolder = ref('');

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
            // Whether the video player in the speaker modal is collapsed.
            // For video recordings the full <video> element by default eats
            // half the available vertical height in the right pane, leaving
            // very little room for the transcript-with-context that the
            // navigation buttons rely on. When collapsed the <video> hides
            // and only the audio-style transport controls remain, so the
            // transcript gets back ~250 px of room. Preference is persisted
            // to localStorage so the user only has to set it once.
            const speakerModalVideoCollapsed = ref(
                localStorage.getItem('speakerModalVideoCollapsed') === 'true'
            );
            const toggleSpeakerModalVideo = () => {
                speakerModalVideoCollapsed.value = !speakerModalVideoCollapsed.value;
                localStorage.setItem('speakerModalVideoCollapsed', String(speakerModalVideoCollapsed.value));
            };
            const showShareModal = ref(false);
            const showSharesListModal = ref(false);
            const showTextEditorModal = ref(false);
            const showAsrEditorModal = ref(false);
            // Placeholder refs — the actual hydration Sets are wired up
            // later (after speakerModalTranscriptRef and asrEditorRef are
            // declared). Declared here so the rest of this state block
            // can reference them without temporal-dead-zone errors.
            const asrEditorHydratedRows = ref(new Set());
            const speakerModalHydratedRows = ref(new Set());
            const showCustomizeSummaryModal = ref(false);
            const customizeSummaryPrompt = ref('');
            const customizeSummaryMode = ref('append');  // 'append' | 'replace'
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
            // Working copy of prompt-template variable values for the reprocess
            // summary modal. Hydrated from `selectedRecording.prompt_variables`
            // when the modal opens; the user can edit them and the values are
            // persisted on the recording when reprocess is submitted.
            const reprocessPromptVariables = reactive({});
            const isAutoIdentifying = ref(false);
            const asrReprocessOptions = reactive({
                language: '',
                min_speakers: null,
                max_speakers: null
            });
            const summaryReprocessPromptSource = ref('default');
            const summaryReprocessSelectedTagId = ref('');
            const summaryReprocessCustomPrompt = ref('');
            const summaryReprocessPromptMode = ref('append');  // 'append' | 'replace'
            const speakerMap = ref({});
            const speakerColorMap = ref({}); // Stable mapping of speaker ID → color class
            const modalSpeakers = ref([]);
            const speakerDisplayMap = ref({});
            const regenerateSummaryAfterSpeakerUpdate = ref(true);
            const speakerSuggestions = ref({});
            const loadingSuggestions = ref({});
            const activeSpeakerInput = ref(null);
            const voiceSuggestions = ref({});
            const loadingVoiceSuggestions = ref(false);

            // --- DateTime Picker State ---
            const showDateTimePicker = ref(false);
            const pickerMonth = ref(new Date().getMonth());
            const pickerYear = ref(new Date().getFullYear());
            const pickerHour = ref(12);
            const pickerMinute = ref(0);
            const pickerAmPm = ref('PM');
            const pickerSelectedDate = ref(null);
            const dateTimePickerTarget = ref(null);
            const dateTimePickerCallback = ref(null);

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
            const regeneratingTitle = ref(false);
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
            const chatInputRef = ref(null);

            // --- Floating Chat Panel State ---
            // 'collapsed'    – FAB only (bottom-right of viewport)
            // 'floating'     – free-positioned panel (user x,y; persisted)
            // 'dock-left'    – overlays the transcript column
            // 'dock-right'   – overlays the right rail
            // 'dock-full'    – overlays both columns (never under the sidebar
            //                  or bottom audio player)
            const chatPanelState = ref('collapsed');
            const chatPanelX = ref(null);
            const chatPanelY = ref(null);
            const chatPanelW = ref(480);  // user-resizable default
            const chatPanelH = ref(640);
            const chatLayoutTick = ref(0);  // bumped on resize/sidebar-toggle
            const chatDragActive = ref(false);
            const chatResizeActive = ref(false);
            // Last user-chosen dock target — remembered across sessions
            // so the single dock button can re-dock to wherever the
            // user prefers without re-picking from the dropdown.
            const chatLastDock = ref(
                (typeof localStorage !== 'undefined' && localStorage.getItem('chat_last_dock')) || 'right'
            );
            const chatDockMenuOpen = ref(false);
            const setChatLastDock = (target) => {
                chatLastDock.value = target;
                try { localStorage.setItem('chat_last_dock', target); } catch (e) { /* ignore */ }
            };
            let chatDragStartX = 0;
            let chatDragStartY = 0;
            let chatDragInitialX = 0;
            let chatDragInitialY = 0;

            // Pull the bounding rect of a target element. Returns null
            // if the element isn't in the DOM (panel hasn't mounted yet).
            const _rect = (sel) => {
                const el = document.querySelector(sel);
                return el ? el.getBoundingClientRect() : null;
            };

            // Compute the panel's inline style for the active state.
            // Position of the collapsed FAB derives from the same
            // #mainContentColumns rect as the floating/docked panel —
            // single source of truth — so the FAB sits just inside the
            // bottom-right corner of the content area regardless of
            // whether the audio player is docked at the top or bottom.
            // Falls back to viewport-bottom-right with a small inset
            // when the rect isn't available yet (initial render).
            const floatingChatFabStyle = computed(() => {
                void chatLayoutTick.value;
                const r = _rect('#mainContentColumns');
                if (r && r.width > 0) {
                    return {
                        position: 'fixed',
                        top: 'auto',
                        bottom: (window.innerHeight - r.bottom + 24) + 'px',
                        left: 'auto',
                        right: (window.innerWidth - r.right + 24) + 'px',
                    };
                }
                return {};  // falls back to CSS bottom: 84px right: 24px
            });

            // Reactive on chatLayoutTick so resize / sidebar toggle
            // trigger recompute.
            const floatingChatPanelStyle = computed(() => {
                // Touch the tick to register reactivity for resize updates.
                void chatLayoutTick.value;

                if (chatPanelState.value === 'floating' && chatPanelX.value != null) {
                    return {
                        top: chatPanelY.value + 'px',
                        left: chatPanelX.value + 'px',
                        width: chatPanelW.value + 'px',
                        height: chatPanelH.value + 'px',
                        right: 'auto',
                        bottom: 'auto'
                    };
                }
                if (chatPanelState.value === 'dock-left') {
                    const r = _rect('#leftMainColumn');
                    if (r && r.width > 0) {
                        return {
                            left: r.left + 'px',
                            top: r.top + 'px',
                            width: r.width + 'px',
                            height: r.height + 'px',
                            right: 'auto',
                            bottom: 'auto',
                            borderRadius: '0',
                        };
                    }
                }
                if (chatPanelState.value === 'dock-right') {
                    const r = _rect('#rightMainColumn');
                    if (r && r.width > 0) {
                        return {
                            left: r.left + 'px',
                            top: r.top + 'px',
                            width: r.width + 'px',
                            height: r.height + 'px',
                            right: 'auto',
                            bottom: 'auto',
                            borderRadius: '0',
                        };
                    }
                }
                if (chatPanelState.value === 'dock-full') {
                    // Span both content columns. Use the parent
                    // #mainContentColumns rect so the panel naturally
                    // stays within the content area (right of sidebar,
                    // above bottom audio player).
                    const r = _rect('#mainContentColumns');
                    if (r && r.width > 0) {
                        return {
                            left: r.left + 'px',
                            top: r.top + 'px',
                            width: r.width + 'px',
                            height: r.height + 'px',
                            right: 'auto',
                            bottom: 'auto',
                            borderRadius: '0',
                        };
                    }
                }
                return {};
            });

            // Persistence per recording (best-effort; ignores errors).
            const _chatPanelStorageKey = () => {
                const rec = selectedRecording.value;
                return rec ? `chat_panel_pos_${rec.id}` : null;
            };
            const saveChatPanelPosition = () => {
                const key = _chatPanelStorageKey();
                if (!key) return;
                try {
                    localStorage.setItem(key, JSON.stringify({
                        state: chatPanelState.value,
                        x: chatPanelX.value,
                        y: chatPanelY.value,
                        w: chatPanelW.value,
                        h: chatPanelH.value,
                    }));
                } catch (e) { /* ignore */ }
            };
            const restoreChatPanelPosition = () => {
                const key = _chatPanelStorageKey();
                if (!key) return;
                try {
                    const raw = localStorage.getItem(key);
                    if (!raw) { chatPanelState.value = 'collapsed'; return; }
                    const data = JSON.parse(raw);
                    // Migrate legacy state values from the previous
                    // panel design (docked-ne/nw/se/sw, maximized) to
                    // the new state vocabulary.
                    const validStates = ['collapsed', 'floating', 'dock-left', 'dock-right', 'dock-full'];
                    let s = data.state || 'collapsed';
                    if (!validStates.includes(s)) {
                        // Any legacy value → collapse so the user opens fresh.
                        s = 'collapsed';
                    }
                    chatPanelState.value = s;
                    chatPanelX.value = data.x;
                    chatPanelY.value = data.y;
                    if (data.w) chatPanelW.value = data.w;
                    if (data.h) chatPanelH.value = data.h;
                } catch (e) {
                    chatPanelState.value = 'collapsed';
                }
            };

            const openChatPanel = () => {
                if (chatPanelState.value === 'collapsed') {
                    // Use #mainContentColumns as the single bounds
                    // reference — same as the docked/floating panel
                    // clamp logic — so the FAB→panel transition lands
                    // inside the actual content area regardless of
                    // whether the audio player is docked at the top
                    // or bottom of the layout.
                    const r = _rect('#mainContentColumns');
                    chatPanelState.value = 'floating';
                    chatPanelX.value = r ? (r.right - chatPanelW.value - 24) : (window.innerWidth - chatPanelW.value - 24);
                    chatPanelY.value = r ? (r.bottom - chatPanelH.value - 24) : (window.innerHeight - chatPanelH.value - 80);
                }
                saveChatPanelPosition();
            };

            // South-east corner resize handle drag — only active in
            // floating mode. Touch-friendly.
            const startChatPanelResize = (event) => {
                if (chatPanelState.value !== 'floating') return;
                const isTouch = event.touches != null;
                const point = isTouch ? event.touches[0] : event;
                chatResizeActive.value = true;
                const startW = chatPanelW.value;
                const startH = chatPanelH.value;
                const startPX = point.clientX;
                const startPY = point.clientY;

                const MIN_W = 320, MIN_H = 360;
                const MAX_W = Math.min(window.innerWidth - chatPanelX.value - 16, 1200);
                const MAX_H = Math.min(window.innerHeight - chatPanelY.value - 16, 900);

                const moveHandler = (e) => {
                    const p = e.touches ? e.touches[0] : e;
                    chatPanelW.value = Math.max(MIN_W, Math.min(MAX_W, startW + (p.clientX - startPX)));
                    chatPanelH.value = Math.max(MIN_H, Math.min(MAX_H, startH + (p.clientY - startPY)));
                };
                const endHandler = () => {
                    chatResizeActive.value = false;
                    document.removeEventListener('mousemove', moveHandler);
                    document.removeEventListener('mouseup', endHandler);
                    document.removeEventListener('touchmove', moveHandler);
                    document.removeEventListener('touchend', endHandler);
                    saveChatPanelPosition();
                };
                document.addEventListener('mousemove', moveHandler);
                document.addEventListener('mouseup', endHandler);
                document.addEventListener('touchmove', moveHandler, { passive: true });
                document.addEventListener('touchend', endHandler);
                event.preventDefault();
                event.stopPropagation();
            };
            const collapseChatPanel = () => {
                chatPanelState.value = 'collapsed';
                saveChatPanelPosition();
            };
            const dockChatPanel = (target) => {
                // target: 'left' | 'right' | 'full' | 'floating'
                if (target === 'floating' && chatPanelX.value == null) {
                    const panelEl = document.querySelector('.floating-chat-panel');
                    if (panelEl) {
                        const rect = panelEl.getBoundingClientRect();
                        chatPanelX.value = rect.left;
                        chatPanelY.value = rect.top;
                    }
                }
                chatPanelState.value = target === 'floating' ? 'floating' : ('dock-' + target);
                // Remember non-floating dock choices for the split button.
                if (target !== 'floating') setChatLastDock(target);
                chatDockMenuOpen.value = false;
                saveChatPanelPosition();
            };
            const toggleChatDockToLast = () => {
                // Primary action of the split button: if currently
                // docked to chatLastDock, undock back to floating;
                // otherwise dock to the last-used target.
                const currentDock = chatPanelState.value.startsWith('dock-')
                    ? chatPanelState.value.slice(5) : null;
                if (currentDock === chatLastDock.value) {
                    dockChatPanel('floating');
                } else {
                    dockChatPanel(chatLastDock.value);
                }
            };
            const toggleChatPanelMax = () => {
                // Backward-compatible alias used by old template; toggle full vs floating
                if (chatPanelState.value === 'dock-full') {
                    dockChatPanel('floating');
                } else {
                    dockChatPanel('full');
                }
            };

            const startChatPanelDrag = (event) => {
                // Click vs drag: don't enter drag until pointer moves
                // beyond DRAG_THRESHOLD. A pure click on the header is
                // a no-op (no accidental docking / floating switch).
                const DRAG_THRESHOLD = 6;
                const isTouch = event.touches != null;
                const point = isTouch ? event.touches[0] : event;
                const startX = point.clientX;
                const startY = point.clientY;
                let dragStarted = false;
                let startedFromState = chatPanelState.value;

                const ensureDragStarted = (p) => {
                    if (dragStarted) return;
                    if (Math.abs(p.clientX - startX) < DRAG_THRESHOLD &&
                        Math.abs(p.clientY - startY) < DRAG_THRESHOLD) {
                        return;
                    }
                    dragStarted = true;
                    chatDragActive.value = true;

                    // If panel was docked, pop out into floating mode
                    // at the cursor position with the user's preferred
                    // floating size.
                    if (startedFromState !== 'floating') {
                        chatPanelX.value = Math.max(0, p.clientX - 100);
                        chatPanelY.value = Math.max(0, p.clientY - 18);
                    } else {
                        // Use current panel rect so it doesn't jump.
                        const panelEl = document.querySelector('.floating-chat-panel');
                        if (panelEl) {
                            const r = panelEl.getBoundingClientRect();
                            chatPanelX.value = r.left;
                            chatPanelY.value = r.top;
                        }
                    }
                    chatPanelState.value = 'floating';
                    chatDragStartX = p.clientX;
                    chatDragStartY = p.clientY;
                    chatDragInitialX = chatPanelX.value || 0;
                    chatDragInitialY = chatPanelY.value || 0;
                };

                const moveHandler = (e) => {
                    const p = e.touches ? e.touches[0] : e;
                    ensureDragStarted(p);
                    if (!dragStarted) return;
                    chatPanelX.value = chatDragInitialX + (p.clientX - chatDragStartX);
                    chatPanelY.value = chatDragInitialY + (p.clientY - chatDragStartY);
                };
                const endHandler = () => {
                    chatDragActive.value = false;
                    document.removeEventListener('mousemove', moveHandler);
                    document.removeEventListener('mouseup', endHandler);
                    document.removeEventListener('touchmove', moveHandler);
                    document.removeEventListener('touchend', endHandler);
                    if (!dragStarted) return; // Pure click — no-op.

                    // On release: snap to a corner of the main content
                    // area if released near one. Otherwise stay floating
                    // clamped inside the main content rect. We never
                    // auto-dock to a column on drop — that's an
                    // explicit user action via the dock buttons.
                    const main = document.querySelector('main.main-content');
                    const mainRect = main ? main.getBoundingClientRect() : null;
                    const w = chatPanelW.value;
                    const h = chatPanelH.value;

                    // Use the actual #mainContentColumns rect for
                    // clamping, NOT main.main-content. The <main>
                    // element has padding-left: 320px when the sidebar
                    // is open, but getBoundingClientRect includes the
                    // padding region, so clamping to main.rect.left
                    // would happily let the panel slip behind the
                    // sidebar. #mainContentColumns is the column-split
                    // area only — its left edge is the actual start of
                    // content, its right is the viewport right edge,
                    // its top/bottom are below the meta strip and
                    // above the bottom audio bar.
                    const cols = _rect('#mainContentColumns');
                    if (cols) {
                        const CORNER_SNAP = 80;
                        const distL = Math.abs(chatPanelX.value - cols.left);
                        const distR = Math.abs(chatPanelX.value + w - cols.right);
                        const distT = Math.abs(chatPanelY.value - cols.top);
                        const distB = Math.abs(chatPanelY.value + h - cols.bottom);
                        if (distL < CORNER_SNAP) chatPanelX.value = cols.left + 12;
                        else if (distR < CORNER_SNAP) chatPanelX.value = cols.right - w - 12;
                        if (distT < CORNER_SNAP) chatPanelY.value = cols.top + 12;
                        else if (distB < CORNER_SNAP) chatPanelY.value = cols.bottom - h - 12;

                        // Final hard clamp inside the columns area.
                        chatPanelX.value = Math.max(
                            cols.left + 8,
                            Math.min(chatPanelX.value, cols.right - w - 8)
                        );
                        chatPanelY.value = Math.max(
                            cols.top + 8,
                            Math.min(chatPanelY.value, cols.bottom - h - 8)
                        );
                    }
                    saveChatPanelPosition();
                };
                document.addEventListener('mousemove', moveHandler);
                document.addEventListener('mouseup', endHandler);
                document.addEventListener('touchmove', moveHandler, { passive: true });
                document.addEventListener('touchend', endHandler);
                // Don't preventDefault — we need to allow click on
                // children (close button etc) to work if the user
                // didn't actually drag.
            };

            // Bump the layout tick on window resize / sidebar toggle so
            // the docked positioning style recomputes.
            const _bumpChatLayout = () => { chatLayoutTick.value += 1; };
            if (typeof window !== 'undefined') {
                window.addEventListener('resize', _bumpChatLayout);
                // Close the floating-chat dock dropdown on any outside click.
                document.addEventListener('click', (e) => {
                    if (!chatDockMenuOpen.value) return;
                    if (!e.target.closest('.floating-chat-dock-split')) {
                        chatDockMenuOpen.value = false;
                    }
                });
            }

            // Single source of truth for chat bounds is the
            // #mainContentColumns rect (right of sidebar, between
            // meta strip / top player and bottom audio bar). A
            // ResizeObserver watching that one element catches every
            // future layout change automatically — player moved
            // top↔bottom, sidebar collapsed/expanded, columns resized,
            // viewport resized, mobile-bottom-nav appearing or not —
            // so we don't have to add a new bump for every layout
            // input. Re-attach if/when the detail view re-mounts.
            let _chatBoundsObserver = null;
            const _attachChatBoundsObserver = () => {
                if (typeof ResizeObserver === 'undefined') return;
                const target = document.getElementById('mainContentColumns');
                if (!target) return;
                if (_chatBoundsObserver) _chatBoundsObserver.disconnect();
                _chatBoundsObserver = new ResizeObserver(_bumpChatLayout);
                _chatBoundsObserver.observe(target);
            };

            // Restore chat panel position whenever the selected recording changes.
            watch(selectedRecording, (newRec) => {
                if (newRec) {
                    restoreChatPanelPosition();
                    nextTick(() => {
                        _bumpChatLayout();
                        // The detail view (and #mainContentColumns) just
                        // mounted/re-mounted; (re-)attach the observer.
                        _attachChatBoundsObserver();
                    });
                }
            }, { immediate: false });

            // Note: a top↔bottom player swap also changes the height of
            // #mainContentColumns (because the player occupies space on
            // a different side of the flex column), so the
            // ResizeObserver above is the only handler needed for that
            // case. No explicit watch on audioPlayerPosition required.

            // --- Audio Player State (Main Player) ---
            const playerVolume = ref(1.0);
            const audioIsPlaying = ref(false);
            const audioCurrentTime = ref(0);
            const audioDuration = ref(0);
            const audioIsMuted = ref(false);
            const audioIsLoading = ref(false);
            const asrEditorAudio = ref(null);
            const playbackRate = ref(1.0);
            const showSpeedMenu = ref(false);
            const playbackSpeeds = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0];
            const speedMenuPosition = ref({});
            const showVolumeSlider = ref(false);
            const showModalVolumeSlider = ref(false);
            const showDuplicatesModal = ref(false);
            const videoCollapsed = ref(false);
            // Desktop docked-video panel: shows the video in a strip across
            // the bottom of the transcript column via a SEPARATE muted
            // <video id="dockVideoElement"> that follows the main player
            // (no teleport — that crashed Vue's patcher previously).
            // In-memory ref: off on fresh load, sticky across video
            // recordings during the session.
            const videoDockEnabled = ref(false);
            const isVideoRecording = computed(() =>
                !!(selectedRecording.value
                   && selectedRecording.value.mime_type
                   && selectedRecording.value.mime_type.startsWith('video/'))
            );
            const toggleVideoDock = () => { videoDockEnabled.value = !videoDockEnabled.value; };
            // Which of the four column tiles the docked video parks in.
            // Persisted to localStorage (unlike the on/off toggle, which is
            // session-only). Validated against the known set so a stale/bad
            // value can't render the dock into nowhere.
            const _validDockPositions = ['left-top', 'left-bottom', 'right-top', 'right-bottom'];
            const _storedDockPosition = (() => {
                try { return localStorage.getItem('videoDockPosition'); } catch (e) { return null; }
            })();
            const videoDockPosition = ref(_validDockPositions.includes(_storedDockPosition) ? _storedDockPosition : 'left-bottom');
            const _persistVideoDockPosition = () => {
                try { localStorage.setItem('videoDockPosition', videoDockPosition.value); } catch (e) { /* ignore */ }
            };
            const cycleVideoDockColumn = () => {
                const [col, vert] = videoDockPosition.value.split('-');
                videoDockPosition.value = (col === 'left' ? 'right' : 'left') + '-' + vert;
                _persistVideoDockPosition();
            };
            const cycleVideoDockVertical = () => {
                const [col, vert] = videoDockPosition.value.split('-');
                videoDockPosition.value = col + '-' + (vert === 'bottom' ? 'top' : 'bottom');
                _persistVideoDockPosition();
            };
            // Adjustable dock height, persisted to localStorage. Clamped to a
            // sane min and a viewport-relative max at drag time.
            const VIDEO_DOCK_MIN_H = 120;
            const _videoDockMaxH = () => Math.round((window.innerHeight || 800) * 0.7);
            const _storedDockHeight = (() => {
                try { return parseInt(localStorage.getItem('videoDockHeight'), 10); } catch (e) { return NaN; }
            })();
            const videoDockHeight = ref(Number.isFinite(_storedDockHeight) && _storedDockHeight >= VIDEO_DOCK_MIN_H ? _storedDockHeight : 260);
            // Drag the dock's content-facing edge to resize. Direction depends
            // on which tile it's in: a bottom dock grows when its top edge is
            // dragged up; a top dock grows when its bottom edge is dragged down.
            const startVideoDockResize = (event) => {
                event.preventDefault();
                const startY = event.clientY;
                const startH = videoDockHeight.value;
                const isBottom = videoDockPosition.value.endsWith('bottom');
                const maxH = _videoDockMaxH();
                document.body.style.userSelect = 'none';
                document.body.style.cursor = 'ns-resize';
                const onMove = (e) => {
                    const dy = e.clientY - startY;
                    let h = startH + (isBottom ? -dy : dy);
                    h = Math.max(VIDEO_DOCK_MIN_H, Math.min(maxH, h));
                    videoDockHeight.value = h;
                };
                const onUp = () => {
                    document.removeEventListener('mousemove', onMove);
                    document.removeEventListener('mouseup', onUp);
                    document.body.style.userSelect = '';
                    document.body.style.cursor = '';
                    try { localStorage.setItem('videoDockHeight', String(videoDockHeight.value)); } catch (e) { /* ignore */ }
                };
                document.addEventListener('mousemove', onMove);
                document.addEventListener('mouseup', onUp);
            };
            const videoFullscreen = ref(false);
            const fullscreenControlsVisible = ref(true);
            const fullscreenControlsTimer = ref(null);
            const duplicatesModalData = ref(null);

            // --- Modal Audio Player State (Independent from main) ---
            const modalAudioCurrentTime = ref(0);
            const modalAudioDuration = ref(0);
            const modalAudioIsPlaying = ref(false);
            const modalPlaybackRate = ref(1.0);

            // --- Column Resizing State ---
            const leftColumnWidth = ref(60);
            const rightColumnWidth = ref(40);
            const isResizing = ref(false);
            // Re-flow the docked chat panel whenever the columns resize
            // (otherwise the chat keeps the previous column width).
            watch([leftColumnWidth, rightColumnWidth], () => {
                chatLayoutTick.value += 1;
            });
            // Also re-flow when the sidebar toggles, since the main
            // content area shifts.
            watch(isSidebarCollapsed, () => {
                nextTick(() => { chatLayoutTick.value += 1; });
            });

            // --- Dropdown Positioning ---
            const dropdownPositions = ref({});
            // Single-ref dropdown tracking for ASR editor (performance optimization)
            const openAsrDropdownIndex = ref(null);

            // --- App Configuration ---
            const useAsrEndpoint = ref(false);
            const connectorSupportsDiarization = ref(false);  // Connector capability for diarization UI
            const connectorSupportsSpeakerCount = ref(false);  // Connector capability for min/max speakers
            const connectorSupportsHotwords = ref(false);     // Connector accepts hotword/keyword biasing
            const connectorSupportsInitialPrompt = ref(false); // Connector accepts initial prompt / context hint
            const showTimestampsSimpleView = ref(false);     // User pref: display timestamps in simple view
            const editorAutosave = ref(false);                // User pref: autosave transcript editor
            const audioPlayerPosition = ref('bottom');        // User pref: 'bottom' or 'top' for desktop player placement
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

            // --- Virtual Scroll Container Refs ---
            const speakerModalTranscriptRef = ref(null);
            const mainTranscriptRef = ref(null);
            const asrEditorRef = ref(null);
            const asrEditorSaveFlash = ref(false);  // Brief "Saved" indicator after Save (without close)
            const asrEditorHighlightIndex = ref(null);  // Segment index briefly highlighted after double-click open

            // Lazy-hydration was previously wired for both modals but
            // (a) the ASR editor went back to virtual scrolling (uniform
            // row heights make fixed-itemHeight the right tool) and
            // (b) the speaker modal hydration scheme made the speaker
            // highlight glow intermittent because the hydrated/placeholder
            // template split interacted badly with v-memo as rows
            // promoted/demoted mid-selection. Speaker modal now mounts
            // all rows in full; content-visibility: auto on .speaker-segment
            // still skips paint for off-screen rows so most of the perf
            // win is preserved. Keep the hydrate stubs as no-ops in case
            // any leftover template reference still calls them.
            const hydrateAsrEditorRow = () => {};
            const hydrateSpeakerModalRow = () => {};

            // --- Computed properties needed by composables ---
            const isMobileScreen = computed(() => windowWidth.value < 1024);

            // Word-count meta surfaced in the right-rail tab labels. We
            // strip HTML and collapse whitespace before counting so
            // markdown-rendered summary HTML doesn't inflate the count.
            const _countWords = (raw) => {
                if (!raw) return 0;
                const text = String(raw).replace(/<[^>]*>/g, ' ').trim();
                if (!text) return 0;
                return text.split(/\s+/).length;
            };
            const _formatCount = (n) => {
                if (n < 1000) return String(n);
                if (n < 10000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
                return Math.round(n / 1000) + 'k';
            };
            const summaryWordCount = computed(() => {
                if (!selectedRecording.value) return '';
                const n = _countWords(selectedRecording.value.summary);
                return n ? _formatCount(n) : '';
            });
            const notesWordCount = computed(() => {
                if (!selectedRecording.value) return '';
                const n = _countWords(selectedRecording.value.notes);
                return n ? _formatCount(n) : '';
            });

            // ---------------------------------------------------------------
            // Recording stats — per-speaker speaking time, % of audio, turn
            // counts, word counts and WPM, plus a silence row aggregating the
            // gaps between consecutive segments. Surfaces a Stats tab in the
            // right rail only when diarized transcript segments with
            // start/end times are available; otherwise empty/null.
            //
            // Pure derivation from processedTranscription.simpleSegments +
            // recording.duration. No backend change.
            // ---------------------------------------------------------------
            const _fmtMmSs = (totalSec) => {
                if (totalSec == null || !isFinite(totalSec)) return '—';
                const s = Math.max(0, Math.round(totalSec));
                const m = Math.floor(s / 60);
                const r = s % 60;
                return `${m}:${String(r).padStart(2, '0')}`;
            };
            const recordingStats = computed(() => {
                const rec = selectedRecording.value;
                const pt = processedTranscription.value;
                if (!rec || !pt) return null;
                const segs = pt.simpleSegments || [];
                if (segs.length === 0) return null;
                // Require time info on at least the first segment.
                const hasTimes = segs.every(s =>
                    typeof s.startTime === 'number' &&
                    typeof s.endTime === 'number' &&
                    isFinite(s.startTime) && isFinite(s.endTime)
                );
                if (!hasTimes) return null;

                // Per-speaker aggregation.
                const bySpeaker = new Map();  // speakerId → { name, color, seconds, turns, words, firstStart }
                let totalSpeakingSeconds = 0;
                segs.forEach((seg, idx) => {
                    const dur = Math.max(0, (seg.endTime || 0) - (seg.startTime || 0));
                    const wordCount = (seg.sentence || '').trim().split(/\s+/).filter(Boolean).length;
                    const id = seg.speakerId || 'unknown';
                    let agg = bySpeaker.get(id);
                    if (!agg) {
                        agg = {
                            speakerId: id,
                            name: seg.speaker || id,
                            color: seg.color || 'speaker-color-1',
                            seconds: 0,
                            words: 0,
                            turns: 0,
                            firstStart: seg.startTime,
                        };
                        bySpeaker.set(id, agg);
                    }
                    agg.seconds += dur;
                    agg.words += wordCount;
                    // A new "turn" = previous segment had a different speaker.
                    const prev = segs[idx - 1];
                    if (!prev || prev.speakerId !== seg.speakerId) {
                        agg.turns += 1;
                    }
                    totalSpeakingSeconds += dur;
                });

                // Silence: gaps between consecutive segments + leading/trailing gap.
                const totalAudioSeconds = Number(rec.duration) || segs[segs.length - 1].endTime || 0;
                let silenceSeconds = 0;
                for (let i = 0; i < segs.length - 1; i++) {
                    const gap = (segs[i + 1].startTime || 0) - (segs[i].endTime || 0);
                    if (gap > 0) silenceSeconds += gap;
                }
                // Leading silence before first segment.
                if (segs[0].startTime > 0) silenceSeconds += segs[0].startTime;
                // Trailing silence after last segment (only if duration known).
                if (totalAudioSeconds > 0) {
                    const trail = totalAudioSeconds - segs[segs.length - 1].endTime;
                    if (trail > 0) silenceSeconds += trail;
                }

                // Build sorted speaker rows.
                const denominator = totalAudioSeconds > 0 ? totalAudioSeconds : (totalSpeakingSeconds + silenceSeconds);
                const speakerRows = Array.from(bySpeaker.values())
                    .sort((a, b) => b.seconds - a.seconds)
                    .map(agg => ({
                        speakerId: agg.speakerId,
                        name: agg.name,
                        color: agg.color,
                        seconds: agg.seconds,
                        durationLabel: _fmtMmSs(agg.seconds),
                        pct: denominator > 0 ? (agg.seconds / denominator) * 100 : 0,
                        turns: agg.turns,
                        words: agg.words,
                        wpm: agg.seconds > 0 ? Math.round((agg.words / agg.seconds) * 60) : 0,
                    }));

                return {
                    speakerRows,
                    silence: {
                        seconds: silenceSeconds,
                        durationLabel: _fmtMmSs(silenceSeconds),
                        pct: denominator > 0 ? (silenceSeconds / denominator) * 100 : 0,
                    },
                    total: {
                        seconds: denominator,
                        durationLabel: _fmtMmSs(denominator),
                        speakingSeconds: totalSpeakingSeconds,
                        speakers: speakerRows.length,
                        turns: segs.length,
                        words: speakerRows.reduce((s, r) => s + r.words, 0),
                    },
                };
            });
            const hasRecordingStats = computed(() =>
                !!recordingStats.value && recordingStats.value.speakerRows.length > 0
            );
            // True when the mobile bottom nav needs a More overflow:
            // either Events or Stats is available alongside Notes.
            // Otherwise the bar keeps Notes as a direct 4th tab.
            const hasMobileMoreOverflow = computed(() =>
                hasRecordingStats.value
                || (selectedRecording.value
                    && Array.isArray(selectedRecording.value.events)
                    && selectedRecording.value.events.length > 0)
            );

            // Aggregate `{{name}}` variables across the currently selected
            // tags / folder / user / admin prompt chain. The pure helpers
            // live in modules/utils/prompt-variables.js so they can be unit
            // tested with Vitest.
            const selectedPromptVariables = computed(() => {
                const tagsWithPrompts = (Array.isArray(selectedTagIds.value) && Array.isArray(availableTags.value))
                    ? selectedTagIds.value.map(id => availableTags.value.find(t => t.id === id)).filter(Boolean)
                    : [];
                const folder = (selectedFolderId.value && Array.isArray(availableFolders.value))
                    ? (availableFolders.value.find(f => f.id === selectedFolderId.value) || null)
                    : null;
                return buildVariableList({
                    tagsWithPrompts,
                    folder,
                    userPrompt: userSummaryPrompt.value,
                    adminPrompt: adminDefaultSummaryPrompt.value,
                });
            });

            // Same shape as selectedPromptVariables but for a recording that
            // already has its tags/folder assigned. Used by the reprocess
            // summary modal to render input fields pre-populated from the
            // recording's stored values.
            const reprocessAvailableVariables = computed(() => {
                if (!selectedRecording.value) return [];
                const recording = selectedRecording.value;
                // Surface the variables that match the prompt source the user
                // is about to run. Otherwise the panel keeps showing the
                // recording's *original* tag's variables even after the user
                // picks a different tag, which is confusing when the chosen
                // tag has no placeholders.
                const source = summaryReprocessPromptSource.value;
                if (source === 'tag') {
                    const tagId = summaryReprocessSelectedTagId.value;
                    if (!tagId) return [];
                    const tag = (availableTags.value || []).find(t => t.id == tagId);
                    if (!tag || !tag.custom_prompt) return [];
                    return buildVariableList({
                        tagsWithPrompts: [tag],
                        folder: null,
                        userPrompt: '',
                        adminPrompt: '',
                    });
                }
                if (source === 'custom') {
                    const text = summaryReprocessCustomPrompt.value || '';
                    if (!text.trim()) return [];
                    return buildVariableList({
                        tagsWithPrompts: [{ name: 'Custom prompt', custom_prompt: text }],
                        folder: null,
                        userPrompt: '',
                        adminPrompt: '',
                    });
                }
                // source === 'default' — fall through to the standard priority
                // chain that the backend will resolve at task time.
                return buildVariableList({
                    tagsWithPrompts: Array.isArray(recording.tags) ? recording.tags : [],
                    folder: recording.folder || null,
                    userPrompt: userSummaryPrompt.value,
                    adminPrompt: adminDefaultSummaryPrompt.value,
                });
            });
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

            // Compact mm:ss / h:mm:ss formatter for transcript timestamps. Returns
            // "Start" for the recording's first segment (any value below half a
            // second) so the leading pill reads as a label rather than a bare
            // 00:00. Returns an empty string when the input is missing so
            // templates can render "" without a guard. Declared before `state`
            // because state references it.
            const formatTimestamp = (seconds) => {
                if (seconds == null || isNaN(seconds)) return '';
                if (seconds < 0.5) return 'Start';
                const total = Math.floor(seconds);
                const h = Math.floor(total / 3600);
                const m = Math.floor((total % 3600) / 60);
                const s = total % 60;
                const pad = (n) => n < 10 ? '0' + n : '' + n;
                return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
            };

            // =========================================================================
            // COLLECT ALL STATE INTO SINGLE OBJECT FOR COMPOSABLES
            // =========================================================================
            const state = {
                // Core
                currentView, showUploadModal, uploadDeepLinkPending, dragover, recordings, selectedRecording, selectedTab, searchQuery,
                isLoadingRecordings, globalError, csrfToken,

                // Filters
                showAdvancedFilters, filterTags, filterSpeakers, filterTagSearch, filterSpeakerSearch,
                filterDateRange, filterDatePreset, filterTextQuery, filterStarred, filterInbox,
                showArchivedRecordings, showSharedWithMe, sortBy, selectedTagFilter,

                // Pagination
                currentPage, perPage, totalRecordings, totalPages, hasNextPage, hasPrevPage,
                isLoadingMore, searchDebounceTimer,

                // UI
                browser, isSidebarCollapsed, searchTipsExpanded, isUserMenuOpen, showHeaderFolderMenu, tokenBudget, isDarkMode,
                currentColorScheme, showColorSchemeModal, windowWidth, mobileTab, mobileMoreOpen, isMetadataExpanded, expandedSection,
                showSortOptions, currentLanguage, currentLanguageName, availableLanguages, showLanguageMenu,
                colorSchemes, isMobileScreen, isMobileDevice,
                summaryWordCount, notesWordCount, recordingStats, hasRecordingStats, hasMobileMoreOverflow,

                // Upload
                uploadQueue, allJobs, currentlyProcessingFile, processingProgress, processingMessage,
                isProcessingActive, pollInterval, progressPopupMinimized, progressPopupClosed,
                maxFileSizeMB, chunkingEnabled, chunkingMode, chunkingLimit, chunkingLimitDisplay,
                maxConcurrentUploads, recordingDisclaimer, showRecordingDisclaimerModal, pendingRecordingMode,
                uploadDisclaimer, showUploadDisclaimerModal,
                customBanner, showBanner,
                showAdvancedOptions, userTranscriptionLanguage, uploadLanguage, uploadMinSpeakers, uploadMaxSpeakers, uploadHotwords, uploadInitialPrompt, initialPromptTemplates, applyInitialPromptTemplate, showHintsPopover, uploadTranscriptionModel, uploadPromptVariables, showPromptVariablesPanel, selectedPromptVariables, reprocessAvailableVariables, transcriptionModelOptions,
                availableTags, selectedTagIds, uploadTagSearchFilter,
                availableFolders, selectedFolderId, foldersEnabled, filterFolder,

                // Audio Recording
                isRecording, mediaRecorder, audioChunks, audioBlobURL, recordingTime, recordingInterval,
                canRecordAudio, canRecordSystemAudio, systemAudioSupported, systemAudioError,
                recordingNotes, showSystemAudioHelp, showSystemAudioHelpModal, disableAudioProcessing,
                inputAudioDevices, selectedMicDeviceId, selectedSecondaryDeviceId, refreshInputAudioDevices,
                platformInfo, audioCaps, helpModalOsTab, virtualAudioDevices, refreshVirtualAudioDevices,
                asrLanguage, asrMinSpeakers, asrMaxSpeakers,
                audioContext, analyser, micAnalyser, systemAnalyser, visualizer, micVisualizer,
                systemVisualizer, animationFrameId, recordingMode, activeStreams,
                wakeLock, recordingNotification, isPageVisible,
                estimatedFileSize, fileSizeWarningShown, recordingQuality, actualBitrate,
                maxRecordingMB, sizeCheckInterval,

                // PWA Features
                deferredInstallPrompt, showInstallButton, isPWAInstalled,
                notificationPermission, pushSubscription, appBadgeCount,
                currentMediaMetadata, isMediaSessionActive,

                // Incognito Mode
                enableIncognitoMode, incognitoMode, incognitoRecording, incognitoProcessing,
                videoRetentionEnabled, keepAudioOnly, maxAudioOnlyVideoSizeMB,

                // Bulk Selection
                selectionMode, selectedRecordingIds, bulkActionInProgress,

                // Modals
                showEditModal, showDeleteModal, showEditTagsModal, selectedNewTagId, tagSearchFilter,
                showReprocessModal, showResetModal, showSpeakerModal, speakerModalTab, speakerModalVideoCollapsed, toggleSpeakerModalVideo, showShareModal, showSharesListModal,
                showTextEditorModal, showAsrEditorModal, asrEditorHydratedRows, hydrateAsrEditorRow, speakerModalHydratedRows, hydrateSpeakerModalRow, showCustomizeSummaryModal, customizeSummaryPrompt, customizeSummaryMode, editingRecording, editingTranscriptionContent,
                editingSegments, availableSpeakers, showEditSpeakersModal, editingSpeakersList,
                databaseSpeakers, editingSpeakerSuggestions,
                showEditParticipantsModal, editingParticipantsList, editingParticipantSuggestions, allParticipants,
                recordingToShare, shareOptions,
                generatedShareLink, existingShareDetected, recordingPublicShares, isLoadingPublicShares,
                userShares, isLoadingShares, copiedShareId,
                shareToDelete, showShareDeleteModal, recordingToDelete, recordingToReset,
                reprocessType, reprocessRecording, reprocessPromptVariables, isAutoIdentifying, asrReprocessOptions,
                summaryReprocessPromptSource, summaryReprocessSelectedTagId, summaryReprocessCustomPrompt, summaryReprocessPromptMode,
                speakerMap, speakerColorMap, modalSpeakers, speakerDisplayMap, regenerateSummaryAfterSpeakerUpdate, speakerSuggestions,
                loadingSuggestions, activeSpeakerInput, voiceSuggestions, loadingVoiceSuggestions,

                // DateTime Picker
                showDateTimePicker, pickerMonth, pickerYear, pickerHour, pickerMinute,
                pickerAmPm, pickerSelectedDate, dateTimePickerTarget, dateTimePickerCallback,

                // Transcript Editing
                editingSegmentIndex, editingSpeakerIndex, showEditTextModal, editedText,
                showAddSpeakerModal, newSpeakerName, newSpeakerIsMe, newSpeakerSuggestions,
                loadingNewSpeakerSuggestions, showNewSpeakerSuggestions, editedTranscriptData,

                // Inline Editing
                editingTitle, originalTitle, regeneratingTitle,
                editingParticipants, editingMeetingDate, editingSummary, editingNotes,
                tempNotesContent, tempSummaryContent, autoSaveTimer, autoSaveDelay,

                // Markdown
                notesMarkdownEditor, markdownEditorInstance, summaryMarkdownEditor,
                summaryMarkdownEditorInstance, recordingNotesEditor, recordingMarkdownEditorInstance,

                // Transcription
                transcriptionViewMode, legendExpanded, highlightedSpeaker, showDownloadMenu,
                currentPlayingSegmentIndex, followPlayerMode, processingIndicatorMinimized,

                // Chat
                showChat, isChatMaximized, chatMessages, chatInput, isChatLoading, chatMessagesRef, chatInputRef,
                chatPanelState, chatPanelX, chatPanelY, chatPanelW, chatPanelH,
                chatDragActive, chatResizeActive, floatingChatPanelStyle, floatingChatFabStyle,
                chatLastDock, chatDockMenuOpen,
                openChatPanel, collapseChatPanel, toggleChatPanelMax,
                startChatPanelDrag, startChatPanelResize, dockChatPanel,
                toggleChatDockToLast,

                // Audio Player
                playerVolume, audioIsPlaying, audioCurrentTime, audioDuration, audioIsMuted, audioIsLoading, asrEditorAudio,
                modalAudioCurrentTime, modalAudioDuration, modalAudioIsPlaying, modalPlaybackRate,
                playbackRate, showSpeedMenu, playbackSpeeds, speedMenuPosition, showVolumeSlider, showModalVolumeSlider,
                videoFullscreen, fullscreenControlsVisible, fullscreenControlsTimer, videoCollapsed,
                videoDockEnabled, isVideoRecording, toggleVideoDock,
                videoDockPosition, cycleVideoDockColumn, cycleVideoDockVertical,
                videoDockHeight, startVideoDockResize,

                // Column Resizing
                leftColumnWidth, rightColumnWidth, isResizing,

                // Dropdown Positioning
                dropdownPositions,
                openAsrDropdownIndex,

                // App Config
                useAsrEndpoint, connectorSupportsDiarization, connectorSupportsSpeakerCount, connectorSupportsHotwords, connectorSupportsInitialPrompt, showTimestampsSimpleView, editorAutosave, audioPlayerPosition, formatTimestamp, currentUserName, canDeleteRecordings, enableInternalSharing, enableArchiveToggle, showUsernamesInUI,

                // Internal Sharing
                showUnifiedShareModal, internalShareUserSearch, internalShareSearchResults,
                internalShareRecording, internalSharePermissions, internalShareMaxPermissions, recordingInternalShares,
                isLoadingInternalShares, isSearchingUsers, allUsers, isLoadingAllUsers,

                // Reprocessing
                reprocessingPolls,

                // Speaker Groups
                currentSpeakerGroupIndex, speakerGroups,

                // Virtual Scroll
                speakerModalTranscriptRef, mainTranscriptRef, asrEditorRef, asrEditorSaveFlash, asrEditorHighlightIndex
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

            const loadTokenBudget = async () => {
                try {
                    const response = await fetch('/api/user/token-budget');
                    if (response.ok) {
                        tokenBudget.value = await response.json();
                    }
                } catch (error) {
                    console.error('Error loading token budget:', error);
                }
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

            // Modal audio handlers (independent from main player)
            const handleModalAudioTimeUpdate = (event) => {
                modalAudioCurrentTime.value = event.target.currentTime;
            };
            const handleModalAudioLoadedMetadata = (event) => {
                const duration = event.target.duration;
                if (duration && isFinite(duration) && duration > 0) {
                    modalAudioDuration.value = duration;
                }
            };
            const handleModalAudioPlayPause = (event) => {
                modalAudioIsPlaying.value = !event.target.paused;
            };
            const modalAudioProgressPercent = computed(() => {
                if (!modalAudioDuration.value) return 0;
                return (modalAudioCurrentTime.value / modalAudioDuration.value) * 100;
            });
            const resetModalAudioState = () => {
                modalAudioCurrentTime.value = 0;
                modalAudioDuration.value = 0;
                modalAudioIsPlaying.value = false;
            };

            const formatFileSize = (bytes) => {
                if (!bytes) return '0 B';
                const k = 1024;
                const sizes = ['B', 'KB', 'MB', 'GB'];
                const i = Math.floor(Math.log(bytes) / Math.log(k));
                return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
            };

            // Parse a backend INSTANT timestamp. The backend stores/serializes
            // naive UTC (no zone designator); new Date() would parse a zoneless
            // string as LOCAL, showing the UTC clock value instead of converting
            // it. Append 'Z' so it's parsed as UTC; toLocale* then renders it in
            // the viewer's own timezone. Do NOT use this for calendar fields
            // (meeting_date) or wall-clock event times — those are not UTC and
            // must keep their literal value (parse with plain new Date()).
            const parseServerInstant = (s) => {
                if (s == null) return new Date(NaN);
                if (typeof s === 'string' && !/(?:Z|[+-]\d{2}:?\d{2})$/.test(s)) {
                    s = s.replace(' ', 'T') + 'Z';
                }
                return new Date(s);
            };

            const formatDisplayDate = (dateString, isCalendar = false) => {
                if (!dateString) return '';
                try {
                    let date = isCalendar ? new Date(dateString) : parseServerInstant(dateString);
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

            const formatShortDate = (dateString, isCalendar = false) => {
                if (!dateString) return '';
                try {
                    let date = isCalendar ? new Date(dateString) : parseServerInstant(dateString);
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

            // Date helper functions. meeting_date is a calendar day (parse local,
            // no tz shift); created_at is a UTC instant (parseServerInstant).
            const getDateForSorting = (recording) => {
                if (sortBy.value === 'meeting_date' && recording.meeting_date) {
                    return new Date(recording.meeting_date);
                }
                return recording.created_at ? parseServerInstant(recording.created_at) : null;
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
                parseServerInstant,
                formatStatus, getStatusClass, formatTime, formatDuration, formatEventDateTime,
                getDateForSorting, isToday, isYesterday, isThisWeek, isLastWeek, isThisMonth, isLastMonth, isSameDay,
                nextTick,
                onChatComplete: loadTokenBudget,  // Refresh token budget after chat
                refreshVirtualAudioDevices,        // Re-scan installed BlackHole / VB-Cable / monitor sources
                refreshInputAudioDevices           // Re-scan all audio inputs for the device picker
            };

            // =========================================================================
            // COMPUTED PROPERTIES (define before composables that need them)
            // =========================================================================

            const processedTranscription = computed(() => {
                if (!selectedRecording.value?.transcription) {
                    return { hasDialogue: false, content: '', speakers: [], simpleSegments: [], bubbleRows: [], isError: false };
                }

                const transcription = selectedRecording.value.transcription;

                // Check for error message format
                const errorInfo = parseTranscriptionError(transcription);
                if (errorInfo) {
                    return {
                        hasDialogue: false,
                        isJson: false,
                        isError: true,
                        error: errorInfo,
                        content: '',
                        speakers: [],
                        simpleSegments: [],
                        bubbleRows: []
                    };
                }

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

                    // Extract unique speakers in order of first appearance
                    const speakers = [...new Set(transcriptionData.map(segment => segment.speaker).filter(Boolean))];

                    // Build stable color map: assign colors 1, 2, 3... based on order of first appearance
                    // This map is stored and reused - colors never change once assigned
                    const speakerColors = {};
                    speakers.forEach((speaker, index) => {
                        // Use existing color if already mapped, otherwise assign next color
                        if (speakerColorMap.value[speaker]) {
                            speakerColors[speaker] = speakerColorMap.value[speaker];
                        } else {
                            const colorIndex = Object.keys(speakerColorMap.value).length;
                            speakerColors[speaker] = `speaker-color-${(colorIndex % SPEAKER_COLOR_COUNT) + 1}`;
                            speakerColorMap.value[speaker] = speakerColors[speaker];
                        }
                    });

                    const simpleSegments = transcriptionData.map(segment => ({
                        speakerId: segment.speaker,
                        speaker: speakerMap.value[segment.speaker]?.name || segment.speaker,
                        sentence: segment.sentence,
                        // Use nullish coalescing so a real 0 (recording's first segment)
                        // is preserved rather than falling through to segment.startTime.
                        startTime: segment.start_time ?? segment.startTime,
                        endTime: segment.end_time ?? segment.endTime,
                        color: speakerColors[segment.speaker] || 'speaker-color-1'
                    }));

                    const processedSimpleSegments = [];
                    // Group consecutive same-speaker segments into runs
                    // so the sticky speaker tablet at the top of each
                    // run sticks for the entire run, not just the first
                    // segment. Each run is { speaker, color, startTime,
                    // speakerId, segments: [original simpleSegments] }.
                    const simpleSegmentRuns = [];
                    let lastSpeakerId = null;
                    let runOffset = 0;  // index of the first segment in the current run
                    simpleSegments.forEach((segment, idx) => {
                        const isNewSpeaker = segment.speakerId !== lastSpeakerId;
                        processedSimpleSegments.push({
                            ...segment,
                            // The true index into the raw transcription array.
                            // The speaker modal MUST use this (not the v-for
                            // loop index) for change-speaker / edit-text so the
                            // edit hits the exact segment — matches the public
                            // release and avoids v-memo/index drift.
                            _originalIndex: idx,
                            showSpeaker: isNewSpeaker
                        });
                        if (isNewSpeaker) {
                            simpleSegmentRuns.push({
                                speaker: segment.speaker,
                                color: segment.color,
                                speakerId: segment.speakerId,
                                startTime: segment.startTime || segment.start_time,
                                runOffset: idx,
                                segments: []
                            });
                            runOffset = idx;
                        }
                        simpleSegmentRuns[simpleSegmentRuns.length - 1].segments.push({
                            ...segment,
                            originalIndex: idx
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
                        simpleSegmentRuns: simpleSegmentRuns,
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
                    speakerList.forEach((speaker) => {
                        // Use existing color if already mapped, otherwise assign next color
                        if (speakerColorMap.value[speaker]) {
                            speakerColors[speaker] = speakerColorMap.value[speaker];
                        } else {
                            const colorIndex = Object.keys(speakerColorMap.value).length;
                            speakerColors[speaker] = `speaker-color-${(colorIndex % SPEAKER_COLOR_COUNT) + 1}`;
                            speakerColorMap.value[speaker] = speakerColors[speaker];
                        }
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

            // Subtitle computed for fullscreen video overlay
            const currentSubtitle = computed(() => {
                const idx = currentPlayingSegmentIndex.value;
                if (idx === null) return null;
                const t = processedTranscription.value;
                if (!t?.simpleSegments?.[idx]) return null;
                const seg = t.simpleSegments[idx];
                return {
                    text: seg.sentence,
                    speaker: t.hasDialogue ? seg.speaker : null,
                    color: seg.color
                };
            });

            // =========================================================================
            // INITIALIZE COMPOSABLES (after processedTranscription is defined)
            // =========================================================================
            // Create reprocess composable first so it can be passed to recordings
            const reprocessComposable = useReprocess(state, utils);
            const recordingsComposable = useRecordings(state, utils, reprocessComposable);
            // Expose selectRecording so other composables can navigate to a
            // recording the same way a sidebar click does (which fetches its
            // full detail). Used by delete-neighbour selection in modals.
            utils.selectRecording = recordingsComposable.selectRecording;

            // Hydration safety net: if selectedRecording is ever set to a light
            // sidebar/list object (to_list_dict omits transcription/summary),
            // fetch the full recording so the detail pane never shows a stale
            // "No transcription available". The normal selectRecording() path
            // sets utils._hydratingSelectedRecording while it does its own fetch,
            // so this only acts on assignments that bypass it. Keyed on the id so
            // it fires once per navigation, not on in-place field updates.
            watch(() => (selectedRecording.value && selectedRecording.value.id) || null, async (id) => {
                const rec = selectedRecording.value;
                if (!id || id === 'incognito' || !rec) return;
                if ('transcription' in rec) return;            // already a full detail object
                if (utils._hydratingSelectedRecording) return; // selectRecording is fetching it
                try {
                    const resp = await fetch(`/api/recordings/${id}`);
                    if (!resp.ok) return;
                    const full = await resp.json();
                    if (selectedRecording.value && selectedRecording.value.id === id) {
                        selectedRecording.value = full;
                        const idx = recordings.value.findIndex(r => r.id === id);
                        if (idx !== -1) recordings.value[idx] = full;
                    }
                } catch (_) { /* non-fatal; a manual re-click still hydrates */ }
            });

            const uploadComposable = useUpload(state, utils);

            // Upload disclaimer handlers
            const acceptUploadDisclaimer = () => {
                showUploadDisclaimerModal.value = false;
                // Temporarily clear disclaimer to prevent re-trigger, then call startUpload
                const saved = uploadDisclaimer.value;
                uploadDisclaimer.value = '';
                uploadComposable.startUpload();
                uploadDisclaimer.value = saved;
            };

            const cancelUploadDisclaimer = () => {
                showUploadDisclaimerModal.value = false;
            };

            // Add startUpload to utils for audio composable to use
            utils.startUploadQueue = uploadComposable.startUpload;

            const audioComposable = useAudio(state, utils);
            // Expose the full discard (clears the blob, aborts any server
            // session, clears IndexedDB) so the navigation guard can actually
            // tear down an unsaved/recovered recording when the user chooses to
            // leave — otherwise audioBlobURL lingers and the "unsaved recording"
            // prompt fires on every subsequent navigation.
            utils.discardActiveRecording = audioComposable.discardRecording;
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

            // Folders composable
            const foldersComposable = useFolders({
                recordings,
                availableFolders,
                selectedRecording,
                showToast,
                setGlobalError
            });

            // Bulk selection composable
            const bulkSelectionComposable = useBulkSelection({
                selectionMode,
                selectedRecordingIds,
                recordings,
                selectedRecording,
                currentView
            });

            // Bulk operations composable (needs selection composable methods)
            const bulkOperationsComposable = useBulkOperations({
                selectedRecordingIds,
                selectedRecordings: bulkSelectionComposable.selectedRecordings,
                recordings,
                selectedRecording,
                bulkActionInProgress,
                availableTags,
                availableFolders,
                showToast,
                setGlobalError,
                exitSelectionMode: bulkSelectionComposable.exitSelectionMode,
                startReprocessingPoll: reprocessComposable.startReprocessingPoll
            });

            // =========================================================================
            // VIRTUAL SCROLL SETUP (for performance with long transcriptions)
            // Must be before speakers composable since it uses scrollToSegmentIndex
            // =========================================================================
            // Create a computed ref for the segments array
            const transcriptSegments = computed(() => processedTranscription.value.simpleSegments || []);

            // Virtual scroll for speaker modal transcript (main performance bottleneck)
            const speakerModalVirtualScroll = useVirtualScroll({
                items: transcriptSegments,
                itemHeight: 52,  // Approximate height of each segment row
                containerRef: speakerModalTranscriptRef,
                overscan: 8
            });

            // Virtual scroll for main transcription panel
            const mainTranscriptVirtualScroll = useVirtualScroll({
                items: transcriptSegments,
                itemHeight: 48,
                containerRef: mainTranscriptRef,
                overscan: 5
            });

            // Virtual scroll for ASR editor modal (uses editingSegments)
            const asrEditorVirtualScroll = useVirtualScroll({
                items: editingSegments,
                itemHeight: 44,  // Table row height
                containerRef: asrEditorRef,
                overscan: 10
            });

            // Scroll a transcript segment into view by index.
            //
            // The speaker modal no longer uses virtual scrolling — see the
            // top-of-file comment in speaker-modal.html for the
            // content-visibility + v-memo design that replaced it. Every
            // segment is in the DOM, so this is just querySelector +
            // scrollIntoView using MEASURED positions. Robust against
            // variable segment heights, smooth animation works because
            // nothing is fighting it, scrollbar is stable.
            //
            // The main transcript panel STILL uses virtual scrolling
            // (smaller panel, different perf characteristics, no Prev/Next
            // nav buttons to expose the variable-height inaccuracy).
            // Robust scroll-to-element for the transcript. Both the main panel
            // and the speaker modal use content-visibility:auto with an
            // ESTIMATED 60px intrinsic size for off-screen segments (paint
            // perf). A single scrollIntoView is inaccurate for a far jump: it
            // targets the estimate, and the act of scrolling then renders the
            // real (taller) heights, which shifts the target out from under the
            // animation — the "moving target" — so it overshoots and only
            // settles on a later change. This converges on the REAL measured
            // position by re-centering INSTANTLY each frame until the target
            // stops moving. A near target (normal playback advancing one
            // segment) is already rendered, so a single smooth scroll is
            // accurate and keeps the follow animation smooth.
            const scrollSegmentIntoView = (el) => {
                if (!el) return;
                const r0 = el.getBoundingClientRect();
                const near = r0.bottom > -50 && r0.top < window.innerHeight + 50;
                if (near) {
                    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
                    return;
                }
                let attempts = 0;
                let lastTop = null;
                const step = () => {
                    if (!el.isConnected) return;
                    el.scrollIntoView({ block: 'center', behavior: 'auto' });
                    const top = el.getBoundingClientRect().top;
                    attempts += 1;
                    if ((lastTop !== null && Math.abs(top - lastTop) < 1) || attempts >= 8) return;
                    lastTop = top;
                    requestAnimationFrame(step);
                };
                requestAnimationFrame(step);
            };
            utils.scrollSegmentIntoView = scrollSegmentIntoView;

            const scrollToSegmentIndex = (index) => {
                let el = null;
                if (showSpeakerModal.value) {
                    const container = speakerModalTranscriptRef.value;
                    if (!container) return;
                    el = container.querySelector(`[data-segment-index="${index}"]`);
                } else {
                    el = document.querySelector(`.transcript-segment[data-segment-index="${index}"], .speaker-segment[data-segment-index="${index}"], .speaker-bubble[data-segment-index="${index}"]`);
                }
                if (el) scrollSegmentIntoView(el);
            };

            // Add scrollToSegmentIndex to utils for composables that need it
            utils.scrollToSegmentIndex = scrollToSegmentIndex;
            utils.resetModalAudioState = resetModalAudioState;
            utils.resetAsrEditorScroll = () => asrEditorVirtualScroll.reset();
            // Scroll to the target row but offset upward so the row lands
            // comfortably below the sticky table header (~2 rows of clearance)
            // instead of being clipped at the top.
            utils.scrollAsrEditorToIndex = (index) => asrEditorVirtualScroll.scrollToIndex(Math.max(0, index - 2), 'auto');
            utils.setAsrEditorScrollTop = (scrollTop) => {
                if (asrEditorRef.value) {
                    asrEditorRef.value.scrollTop = scrollTop;
                }
            };
            utils.resetSpeakerModalScroll = () => {
                if (speakerModalTranscriptRef.value) {
                    speakerModalTranscriptRef.value.scrollTop = 0;
                }
            };
            // Compute the visible segment range from the DOM rather than
            // from a virtual scroller. Walks the rendered segments inside
            // the speaker modal's transcript container, comparing each
            // element's bounding rect to the container's; returns the
            // [start, end) range of segment indices currently in view.
            // Used by speakers.js to decide whether the highlighted
            // speaker's nearest group is already visible (and we can skip
            // the scroll). O(n) walk; only called when the user picks a
            // new speaker to highlight, not on every scroll event.
            utils.getSpeakerModalVisibleRange = () => {
                const container = speakerModalTranscriptRef.value;
                if (!container) return null;
                const segments = container.querySelectorAll('[data-segment-index]');
                if (!segments.length) return { start: 0, end: 0 };
                const cTop = container.getBoundingClientRect().top;
                const cBottom = cTop + container.clientHeight;
                let start = -1, end = 0;
                for (const el of segments) {
                    const r = el.getBoundingClientRect();
                    if (r.bottom < cTop) continue;          // above viewport
                    if (r.top > cBottom) break;             // below viewport (segments are in document order)
                    const idx = parseInt(el.dataset.segmentIndex, 10);
                    if (Number.isNaN(idx)) continue;
                    if (start === -1) start = idx;
                    end = idx + 1;
                }
                return start === -1 ? { start: 0, end: 0 } : { start, end };
            };

            // Speakers composable needs processedTranscription and scrollToSegmentIndex
            const speakersComposable = useSpeakers(state, utils, processedTranscription);

            const groupedRecordings = computed(() => {
                const groups = {};
                const groupDates = {}; // Track the most recent date in each group

                recordings.value.forEach(recording => {
                    const date = getDateForSorting(recording);
                    if (!date) return;

                    let group;
                    const now = new Date();

                    // Check for future dates first
                    if (date > now && !isToday(date)) {
                        group = t('sidebar.upcoming');
                    } else if (isToday(date)) {
                        group = t('sidebar.today');
                    } else if (isYesterday(date)) {
                        group = t('sidebar.yesterday');
                    } else if (isThisWeek(date)) {
                        group = t('sidebar.thisWeek');
                    } else if (isLastWeek(date)) {
                        group = t('sidebar.lastWeek');
                    } else if (isThisMonth(date)) {
                        group = t('sidebar.thisMonth');
                    } else if (isLastMonth(date)) {
                        group = t('sidebar.lastMonth');
                    } else {
                        group = t('sidebar.older');
                    }

                    if (!groups[group]) {
                        groups[group] = [];
                        groupDates[group] = date;
                    }
                    groups[group].push(recording);

                    // Track the most recent (largest) date in each group
                    if (date > groupDates[group]) {
                        groupDates[group] = date;
                    }
                });

                // Sort groups by their most recent date (descending - newest first)
                return Object.entries(groups)
                    .sort(([a], [b]) => groupDates[b] - groupDates[a])
                    .map(([title, items]) => ({ title, items }));
            });

            const filteredAvailableTags = computed(() => {
                return availableTags.value.filter(tag =>
                    !selectedTagIds.value.includes(tag.id) &&
                    (!tagSearchFilter.value || tag.name.toLowerCase().includes(tagSearchFilter.value.toLowerCase()))
                );
            });

            // Filtered tags for sidebar filter (searches by name)
            const filteredTagsForFilter = computed(() => {
                if (!filterTagSearch.value) return availableTags.value;
                const search = filterTagSearch.value.toLowerCase();
                return availableTags.value.filter(tag =>
                    tag.name.toLowerCase().includes(search)
                );
            });

            // Filtered speakers for sidebar filter (searches by name)
            const filteredSpeakersForFilter = computed(() => {
                if (!filterSpeakerSearch.value) return availableSpeakers.value;
                const search = filterSpeakerSearch.value.toLowerCase();
                return availableSpeakers.value.filter(speaker =>
                    speaker.name.toLowerCase().includes(search)
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

            // Job queue polling state
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

            // Bridge for the in-app recorder (#287): a server-side recording
            // finalizes entirely on the backend, so nothing enters the client
            // uploadQueue to trip hasActiveProcessing. Without this nudge the
            // new recording + its stitch/transcribe jobs only appear after a
            // manual refresh. The audio composable calls this right after a
            // successful finalize so the sidebar and processing-queue panel
            // update live, exactly like a drag-drop upload.
            utils.onServerRecordingQueued = async () => {
                try { await recordingsComposable.loadRecordings(); } catch (_) { /* non-fatal */ }
                await fetchJobQueueStatus(true);
                startJobQueuePolling();
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

            // Track completed recording IDs to detect new completions
            const completedRecordingIds = new Set();

            // Watch allJobs for completed/failed transitions - update local recordings state
            watch(allJobs, async (jobs) => {
                for (const job of jobs) {
                    // A 'stitch' job is an intermediate step for server-side
                    // recordings (#287): a 'transcribe' job always follows it.
                    // Only the terminal job is the recording's completion. If
                    // we acted on the stitch completion we would capture the
                    // still-processing state and then dedupe away the real
                    // transcribe completion — leaving the recording stuck on
                    // "Processing". A FAILED stitch IS terminal (no transcribe
                    // follows), so only skip the completed case.
                    if (job.job_type === 'stitch' && job.job_status === 'completed') continue;
                    if (job.job_status === 'completed' && !completedRecordingIds.has(job.recording_id)) {
                        completedRecordingIds.add(job.recording_id);
                        try {
                            const fullResponse = await fetch(`/api/recordings/${job.recording_id}`);
                            if (fullResponse.ok) {
                                const data = await fullResponse.json();
                                const idx = recordings.value.findIndex(r => r.id === job.recording_id);
                                if (idx !== -1) {
                                    recordings.value[idx] = data;
                                }
                                if (selectedRecording.value?.id === job.recording_id) {
                                    selectedRecording.value = data;
                                }
                                // Update display name on upload queue item
                                const queueItem = uploadQueue.value.find(u => u.recordingId === job.recording_id);
                                if (queueItem) {
                                    queueItem.displayName = data.title || data.original_filename || queueItem.file?.name;
                                    queueItem.status = 'completed';
                                }
                                // Refresh token budget
                                if (typeof loadTokenBudget === 'function') loadTokenBudget();
                            }
                        } catch (err) {
                            console.error(`Error fetching completed recording ${job.recording_id}:`, err);
                        }
                    } else if (job.job_status === 'failed' && !completedRecordingIds.has(`fail_${job.recording_id}`)) {
                        completedRecordingIds.add(`fail_${job.recording_id}`);
                        try {
                            const failedResponse = await fetch(`/api/recordings/${job.recording_id}`);
                            if (failedResponse.ok) {
                                const failedData = await failedResponse.json();
                                const idx = recordings.value.findIndex(r => r.id === job.recording_id);
                                if (idx !== -1) {
                                    recordings.value[idx] = failedData;
                                }
                                if (selectedRecording.value?.id === job.recording_id) {
                                    selectedRecording.value = failedData;
                                }
                                const queueItem = uploadQueue.value.find(u => u.recordingId === job.recording_id);
                                if (queueItem) {
                                    queueItem.status = 'failed';
                                    queueItem.error = failedData.error_message || safeT('errors.processingFailedOnServer');
                                }
                            }
                        } catch (err) {
                            console.error(`Error fetching failed recording ${job.recording_id}:`, err);
                        }
                    }
                }
            }, { deep: true });

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
                        showToast(safeT('messages.jobQueuedForRetry'), 'success');
                    } else {
                        const data = await response.json();
                        showToast(data.error || safeT('messages.failedToRetryJob'), 'error');
                    }
                } catch (error) {
                    console.error('Error retrying job:', error);
                    showToast(safeT('messages.failedToRetryJob'), 'error');
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
                        showToast(data.error || safeT('messages.failedToDeleteJob'), 'error');
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
                            friendlyError: job.error_message ? parseUnformattedError(job.error_message) : null,
                            completedAt: job.completed_at,
                            source: 'job'
                        });
                    }
                }

                // 2. Add upload queue items (client-side tracking)
                for (const upload of uploadQueue.value) {
                    // If we have a recordingId and it's already tracked from jobs, merge upload info
                    if (upload.recordingId) {
                        const key = `rec_${upload.recordingId}`;
                        const existing = items.get(key);

                        if (existing) {
                            existing.clientId = upload.clientId;
                            existing.file = upload.file;
                            existing.duplicateWarning = upload.duplicateWarning || null;
                            // If still uploading, override job status with upload status
                            if (upload.status === 'uploading') {
                                existing.status = 'uploading';
                                existing.progress = upload.progress || 0;
                                existing.progressMessage = 'Uploading...';
                                existing.title = upload.displayName || upload.file?.name || existing.title;
                            }
                            continue;
                        }
                    }

                    // Determine unified status from upload status (per-item progress)
                    let unifiedStatus = 'ready';
                    let progressVal = upload.progress || 0;
                    let progressMsg = 'Waiting to upload...';

                    if (upload.status === 'uploading') {
                        unifiedStatus = 'uploading';
                        progressMsg = 'Uploading...';
                    } else if (upload.status === 'pending') {
                        unifiedStatus = 'queued';
                        progressVal = 100;
                        progressMsg = 'Uploaded, waiting for processing...';
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
                        duplicateWarning: upload.duplicateWarning || null,
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

            // On phones the queue shows as a compact top-right pill by
            // default — auto-minimize it when it first appears so it doesn't
            // pop open over the content. The user can tap to expand. Desktop
            // (and tablets ≥ 641 px) keep the existing expanded card.
            watch(showProcessingPopup, (show) => {
                if (show && windowWidth.value <= 640) {
                    progressPopupMinimized.value = true;
                }
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

            // Recording disclaimer parsed as markdown
            const recordingDisclaimerHtml = computed(() => {
                if (!recordingDisclaimer.value || recordingDisclaimer.value.trim() === '') {
                    return '';
                }
                return marked.parse(recordingDisclaimer.value);
            });

            // Upload disclaimer parsed as markdown
            const uploadDisclaimerHtml = computed(() => {
                if (!uploadDisclaimer.value || uploadDisclaimer.value.trim() === '') {
                    return '';
                }
                return marked.parse(uploadDisclaimer.value);
            });

            // Custom banner parsed as markdown
            const customBannerHtml = computed(() => {
                if (!customBanner.value || customBanner.value.trim() === '') {
                    return '';
                }
                return marked.parse(customBanner.value);
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

            // Duplicates modal
            const openDuplicatesModal = (duplicateInfo) => {
                duplicatesModalData.value = duplicateInfo;
                showDuplicatesModal.value = true;
            };

            const navigateToDuplicate = (id) => {
                showDuplicatesModal.value = false;
                const rec = recordings.value.find(r => r.id === id);
                // selectRecording always re-fetches full data from the API
                recordingsComposable.selectRecording(rec || { id });
            };

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

            watch(filterSpeakers, () => {
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

            watch(filterStarred, () => {
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(filterInbox, () => {
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(filterFolder, (newValue) => {
                // Persist folder selection to localStorage
                if (newValue) {
                    localStorage.setItem('selectedFolder', newValue);
                } else {
                    localStorage.removeItem('selectedFolder');
                }
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            // Guard against being stuck filtering by a folder the user can't
            // access — e.g. a foreign folder ID persisted from a shared recording
            // (#314). Once folders load, drop any filter that isn't a real,
            // owned/accessible folder so "All Recordings" is always reachable.
            watch(availableFolders, (folders) => {
                const f = filterFolder.value;
                if (f && f !== 'none' && Array.isArray(folders) &&
                    !folders.some(folder => String(folder.id) === String(f))) {
                    filterFolder.value = '';
                }
            });

            // Keep the address bar in sync with the open recording so links are
            // shareable/bookmarkable (#301). replaceState avoids history spam;
            // the path is only changed (query + hash preserved). Incognito (id
            // 'incognito') and the list view map back to '/'.
            watch(selectedRecording, (rec) => {
                try {
                    const path = (rec && typeof rec.id === 'number') ? `/recordings/${rec.id}` : '/';
                    if (window.location.pathname !== path) {
                        window.history.replaceState({}, '', path + window.location.search + window.location.hash);
                    }
                } catch (_) { /* best-effort */ }
            });

            watch(sortBy, (newValue) => {
                localStorage.setItem('recordingsSortBy', newValue);
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(showArchivedRecordings, (newValue, oldValue) => {
                // Prevent unnecessary reloads when being set by the other watcher
                if (newValue === oldValue) return;

                // Reload recordings when switching between archived/normal view
                if (showArchivedRecordings.value) {
                    showSharedWithMe.value = false;  // Can't show both at once
                }
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(showSharedWithMe, (newValue, oldValue) => {
                // Prevent unnecessary reloads when being set by the other watcher
                if (newValue === oldValue) return;

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

                // Clear incognito data when navigating away from detail view
                // This ensures incognito data doesn't linger when user goes to upload/recording view
                if (oldView === 'detail' && newView !== 'detail') {
                    if (uploadComposable.hasIncognitoRecording()) {
                        console.log('[Incognito] Clearing data on view change from detail');
                        sessionStorage.removeItem('speakr_incognito_recording');
                        incognitoRecording.value = null;
                    }
                }
            });

            // Re-initialize recording notes editor when recording stops (DOM switches from recording template to accordion template)
            watch(isRecording, async (newVal, oldVal) => {
                if (oldVal === true && newVal === false && currentView.value === 'recording') {
                    uiComposable.destroyRecordingNotesEditor();
                    // Default to the notes section expanded so the smart
                    // editor mounts on a VISIBLE textarea. EasyMDE renders
                    // blank if initialised while its textarea is display:none
                    // (the collapsed-accordion case), which is why the editor
                    // sometimes showed as a plain box.
                    expandedSection.value = 'notes';
                    await nextTick();
                    uiComposable.initializeRecordingNotesEditor();
                }
            });

            // When the notes section becomes visible in the accordion:
            // refresh CodeMirror if the editor already exists (so it
            // remeasures now that it's visible), or initialise it if it
            // doesn't yet — covers the case where the editor was never
            // mounted because the section was collapsed at view entry.
            watch(expandedSection, async (newSection) => {
                if (newSection === 'notes') {
                    await nextTick();
                    if (recordingMarkdownEditorInstance.value) {
                        recordingMarkdownEditorInstance.value.codemirror.refresh();
                    } else {
                        uiComposable.initializeRecordingNotesEditor();
                    }
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

            // Measure the mobile bottom UI (persistent audio player + bottom
            // nav) and publish it as the --mobile-bottom-inset CSS variable,
            // so the processing-queue bar can anchor just above it regardless
            // of whether the player is shown or how tall it is (audio vs
            // video). This replaces the old magic 60+56px offset that
            // overlapped the player once it grew taller.
            let _mobileInsetObserver = null;
            const updateMobileBottomInset = () => {
                const player = document.querySelector('[data-mobile-player]');
                const nav = document.querySelector('[data-mobile-bottom-nav]');
                const h = (player ? player.offsetHeight : 0) + (nav ? nav.offsetHeight : 0);
                document.documentElement.style.setProperty('--mobile-bottom-inset', h + 'px');
                // Re-observe the current elements so live height changes
                // (e.g. toggling video in the player) keep the inset in sync.
                if (typeof ResizeObserver !== 'undefined') {
                    if (!_mobileInsetObserver) {
                        _mobileInsetObserver = new ResizeObserver(() => {
                            const p = document.querySelector('[data-mobile-player]');
                            const n = document.querySelector('[data-mobile-bottom-nav]');
                            const hh = (p ? p.offsetHeight : 0) + (n ? n.offsetHeight : 0);
                            document.documentElement.style.setProperty('--mobile-bottom-inset', hh + 'px');
                        });
                    }
                    _mobileInsetObserver.disconnect();
                    if (player) _mobileInsetObserver.observe(player);
                    if (nav) _mobileInsetObserver.observe(nav);
                }
            };
            // Re-measure whenever the bottom layout can change (view switch,
            // recording change, player position, video toggle, or the bar
            // first appearing) — this also catches the player mounting /
            // unmounting, which the ResizeObserver alone can't.
            watch([currentView, () => selectedRecording.value?.id, mobileTab,
                   videoCollapsed, audioPlayerPosition, showProcessingPopup],
                  () => nextTick(updateMobileBottomInset));

            // =========================================================================
            // LIFECYCLE
            // =========================================================================
            onMounted(async () => {
                // Get config from data attributes
                const appElement = document.getElementById('app');
                if (appElement) {
                    useAsrEndpoint.value = appElement.dataset.useAsrEndpoint === 'True';
                    connectorSupportsDiarization.value = appElement.dataset.connectorSupportsDiarization === 'True';
                    connectorSupportsSpeakerCount.value = appElement.dataset.connectorSupportsSpeakerCount === 'True';
                    connectorSupportsHotwords.value = appElement.dataset.connectorSupportsHotwords === 'True';
                    connectorSupportsInitialPrompt.value = appElement.dataset.connectorSupportsInitialPrompt === 'True';
                    showTimestampsSimpleView.value = appElement.dataset.showTimestampsSimpleView === 'True';
                    editorAutosave.value = appElement.dataset.editorAutosave === 'True';
                    const pos = appElement.dataset.audioPlayerPosition;
                    audioPlayerPosition.value = (pos === 'top') ? 'top' : 'bottom';
                    currentUserName.value = appElement.dataset.currentUserName || '';
                }

                // Load saved transcription initial-prompt templates (#309) for
                // the upload-modal picker. Non-fatal if it fails.
                try {
                    const r = await fetch('/api/initial-prompt-templates');
                    if (r.ok) initialPromptTemplates.value = await r.json();
                } catch (e) { /* picker simply stays empty */ }

                // Initialize UI
                uiComposable.initializeDarkMode();
                uiComposable.initializeColorScheme();
                uiComposable.initializeSidebar();

                // PWA Web Share Target feedback (issue #285). When the user
                // shares audio from the native share sheet, the backend
                // redirects to /?share_target=ok or /?share_target_error=...;
                // surface a toast and strip the flag from the URL so refresh
                // does not re-show it.
                try {
                    const params = new URLSearchParams(window.location.search);
                    const shareOk = params.get('share_target');
                    const shareErr = params.get('share_target_error');
                    if (shareOk === 'ok') {
                        showToast(t('toasts.shareTargetReceived') || 'Shared audio received and queued for transcription', 'fa-share');
                    } else if (shareErr) {
                        const errMsgKey = `errors.shareTarget_${shareErr}`;
                        const errMsg = (t(errMsgKey) !== errMsgKey ? t(errMsgKey) : null)
                            || t('errors.shareTargetGeneric')
                            || `Share failed: ${shareErr}`;
                        setGlobalError(errMsg);
                    }
                    if (shareOk || shareErr) {
                        params.delete('share_target');
                        params.delete('share_target_error');
                        params.delete('recording_id');
                        const cleanQuery = params.toString();
                        const newUrl = window.location.pathname + (cleanQuery ? `?${cleanQuery}` : '') + window.location.hash;
                        window.history.replaceState({}, '', newUrl);
                    }
                } catch (e) {
                    console.warn('[App] share-target query handling failed:', e);
                }

                // Unified recording recovery (#287 task #4): ONE decision so
                // the user never sees two prompts for the same recording.
                // Preference order:
                //   1. A durable in-progress SERVER session (survives device
                //      loss) → themed recovery modal (finish / discard).
                //   2. Otherwise a local IndexedDB copy → themed modal
                //      (restore / discard). This is the fallback path used
                //      when server chunking is off or the server was
                //      unreachable.
                // If a remembered server session exists but is no longer
                // recoverable (already finalized / aborted / expired), we
                // forget it AND clear the now-redundant local copy so the old
                // double-prompt + duplicate-restore bug can't recur.
                try {
                    const ServerSessions = await import('./modules/db/server-recording-sessions.js');
                    const remembered = ServerSessions.getRememberedSession();
                    let serverSession = null;
                    if (remembered && remembered.session_id) {
                        try { serverSession = await ServerSessions.getSessionStatus(remembered.session_id); }
                        catch (_) { serverSession = null; }
                    }

                    if (serverSession && serverSession.status === 'recording' && serverSession.chunk_count > 0) {
                        // Pull any local metadata (mode/notes) just for display.
                        let localMeta = null;
                        try { localMeta = await audioComposable.checkForRecoverableRecording(); } catch (_) { /* ignore */ }
                        recoverableRecording.value = {
                            source: 'server',
                            sessionId: remembered.session_id,
                            mimeType: remembered.mime_type || (localMeta && localMeta.mimeType) || 'audio/webm',
                            chunkCount: serverSession.chunk_count,
                            totalSize: serverSession.bytes_received,
                            duration: (localMeta && localMeta.duration) || null,
                            startTime: serverSession.created_at,
                            mode: (localMeta && localMeta.mode) || null,
                            notes: (localMeta && localMeta.notes) || null,
                        };
                        showRecoveryModal.value = true;
                        console.log('[App] In-progress server recording detected, offering recovery');
                    } else if (remembered && remembered.session_id) {
                        // Remembered session is no longer recoverable → forget
                        // the pointer and clear the redundant local copy.
                        ServerSessions.forgetActiveSession();
                        try { await audioComposable.clearRecordingSession(); } catch (_) { /* ignore */ }
                    } else {
                        // No server session involved → legacy local recovery.
                        const recoverable = await audioComposable.checkForRecoverableRecording();
                        if (recoverable && recoverable.chunks && recoverable.chunks.length > 0) {
                            recoverableRecording.value = { source: 'local', ...recoverable };
                            showRecoveryModal.value = true;
                            console.log('[App] Found local recoverable recording, showing recovery dialog');
                        }
                    }
                } catch (error) {
                    console.warn('[App] Recording recovery check failed:', error);
                }

                // Detect the ?upload=1 deep-link (inquire mode's "+ New
                // Recording") BEFORE loading recordings. loadRecordings
                // auto-selects the last viewed recording via an unawaited
                // selectRecording(), which sets showUploadModal=false AFTER an
                // awaited fetch — so a post-load setTimeout race always loses.
                // Instead we set uploadDeepLinkPending now so that first
                // selectRecording leaves the modal alone, then open the modal
                // once data has loaded. Strip the query param so a refresh
                // doesn't re-trigger.
                let wantUploadDeepLink = false;
                try {
                    const params = new URLSearchParams(window.location.search);
                    if (params.get('upload') === '1') {
                        wantUploadDeepLink = true;
                        uploadDeepLinkPending.value = true;
                        params.delete('upload');
                        const qs = params.toString();
                        const newUrl = window.location.pathname + (qs ? '?' + qs : '') + window.location.hash;
                        window.history.replaceState({}, '', newUrl);
                    }
                } catch (_) { /* no-op: param parsing/replaceState are best-effort */ }

                // Deep link to a specific recording: /recordings/<id> (#301).
                // Captured before load; selected once recordings are in.
                let deepLinkRecordingId = null;
                try {
                    const m = window.location.pathname.match(/^\/recordings\/(\d+)\/?$/);
                    if (m) deepLinkRecordingId = m[1];
                } catch (_) { /* best-effort */ }

                // Load initial data
                await Promise.all([
                    recordingsComposable.loadRecordings(),
                    recordingsComposable.loadTags(),
                    recordingsComposable.loadFolders(),
                    recordingsComposable.loadSpeakers(),
                    loadTokenBudget()
                ]);

                // Now open the upload modal. The pending flag (set above)
                // prevents the auto-selected recording's selectRecording() from
                // closing it once its fetch resolves.
                if (wantUploadDeepLink) {
                    await nextTick();
                    uiComposable.switchToUploadView();
                } else if (deepLinkRecordingId) {
                    // Honour the /recordings/<id> deep link now that data is loaded.
                    await recordingsComposable.selectRecordingById(deepLinkRecordingId);
                }

                // Auto-retry any uploads that previously failed and were saved to
                // IndexedDB (#313): once on load, and again whenever the browser
                // reports it's back online. Works in every browser (no Background
                // Sync) and resubmits through the normal CSRF-correct upload path.
                uploadComposable.resurfaceFailedUploads();
                window.addEventListener('online', () => uploadComposable.resurfaceFailedUploads());

                // Clean up orphaned incognito data if we're not viewing incognito recording
                // This can happen if user navigated away without the cleanup triggering
                if (uploadComposable.hasIncognitoRecording() && selectedRecording.value?.id !== 'incognito') {
                    console.log('[App] Cleaning up orphaned incognito data from sessionStorage');
                    sessionStorage.removeItem('speakr_incognito_recording');
                    incognitoRecording.value = null;
                }

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
                        uploadDisclaimer.value = config.upload_disclaimer || '';
                        customBanner.value = config.custom_banner || '';
                        canDeleteRecordings.value = config.can_delete_recordings !== false;
                        enableInternalSharing.value = config.enable_internal_sharing === true;
                        enableArchiveToggle.value = config.enable_archive_toggle === true;
                        showUsernamesInUI.value = config.show_usernames_in_ui === true;
                        enableIncognitoMode.value = config.enable_incognito_mode === true;
                        foldersEnabled.value = config.enable_folders === true;
                        maxConcurrentUploads.value = config.max_concurrent_uploads || 3;
                        // Video / audio-only upload config. videoRetention from
                        // the server controls whether the "Keep audio only"
                        // toggle is shown; max_audio_only_video_size_mb sets
                        // the larger limit applied to video uploads in
                        // audio-only mode.
                        videoRetentionEnabled.value = config.video_retention === true;
                        if (typeof config.max_audio_only_video_size_mb === 'number') {
                            maxAudioOnlyVideoSizeMB.value = config.max_audio_only_video_size_mb;
                        } else if (config.max_audio_only_video_size_mb) {
                            maxAudioOnlyVideoSizeMB.value = parseInt(config.max_audio_only_video_size_mb, 10) || 1000;
                        }

                        // Per-upload transcription model dropdown options
                        // (issue #266). Only set when admin configured at least
                        // one model in TRANSCRIPTION_MODELS_AVAILABLE.
                        transcriptionModelOptions.value = Array.isArray(config.transcription_model_options)
                            ? config.transcription_model_options
                            : [];

                        // Set user's default transcription language for upload and reprocess forms
                        if (config.user_transcription_language) {
                            userTranscriptionLanguage.value = config.user_transcription_language;
                            uploadLanguage.value = config.user_transcription_language;
                        }

                        // Capture the user's personal summary prompt and the
                        // site-wide admin default so the upload form can scan
                        // them for `{{variable}}` placeholders when no tag /
                        // folder prompt is active.
                        userSummaryPrompt.value = config.user_summary_prompt || '';
                        adminDefaultSummaryPrompt.value = config.admin_default_summary_prompt || '';

                        // Restore saved folder selection from localStorage
                        if (foldersEnabled.value) {
                            const savedFolder = localStorage.getItem('selectedFolder');
                            if (savedFolder) {
                                filterFolder.value = savedFolder;
                            }
                        }

                        // Set default incognito mode state if feature enabled and default is true
                        if (config.enable_incognito_mode && config.incognito_mode_default) {
                            incognitoMode.value = true;
                        }
                    }
                } catch (error) {
                    console.error('Failed to load config:', error);
                }

                // Initialize UI settings from localStorage
                uiComposable.initializeUI();

                // Load incognito recording from sessionStorage if exists (only if feature is enabled)
                if (enableIncognitoMode.value) {
                    uploadComposable.loadIncognitoRecording();
                }

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
                    updateMobileBottomInset();
                });
                // Initial measurement once the layout has painted.
                nextTick(updateMobileBottomInset);

                // Visibility change handler for wake lock
                document.addEventListener('visibilitychange', audioComposable.handleVisibilityChange);

                // Prevent data loss on tab close/refresh during recording or incognito mode
                window.addEventListener('beforeunload', (e) => {
                    // Check for unsaved recording
                    if (audioComposable.hasUnsavedRecording()) {
                        e.preventDefault();
                        e.returnValue = ''; // Chrome requires this
                        return 'You have an unsaved recording. Are you sure you want to leave?';
                    }
                    // Check for uploads still in flight. A freshly-recorded file
                    // may still be mid-transfer with no copy on the server yet;
                    // leaving now risks losing it. (Items that already reached
                    // 'pending'/'failed' are on the server or saved for retry.)
                    const uploadsInFlight = uploadQueue.value.some(item =>
                        item.status === 'queued' || item.status === 'ready' || item.status === 'uploading');
                    if (uploadsInFlight) {
                        e.preventDefault();
                        e.returnValue = ''; // Chrome requires this
                        return 'Uploads are still in progress. Leaving now may lose recordings that have not finished uploading.';
                    }
                    // Check for incognito recording that would be lost
                    // Only warn if we're currently viewing the incognito recording
                    // (if user navigated away, they've implicitly abandoned it or already been warned)
                    if (uploadComposable.hasIncognitoRecording() && selectedRecording.value?.id === 'incognito') {
                        e.preventDefault();
                        e.returnValue = ''; // Chrome requires this
                        return 'You have an incognito recording that will be lost. Are you sure you want to leave?';
                    }
                });

                // Initialize bulk selection keyboard listeners
                bulkSelectionComposable.initSelectionKeyboardListeners();
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
                        showUploadModal.value = false;
                        showToast(safeT('messages.recordingRecovered'), 'success');
                    } else {
                        showToast(safeT('messages.failedToRecoverRecording'), 'error');
                    }

                    recoverableRecording.value = null;
                } catch (error) {
                    console.error('[App] Failed to recover recording:', error);
                    showToast(safeT('messages.errorRecoveringRecording'), 'error');
                }
            };

            const cancelRecovery = async () => {
                try {
                    showRecoveryModal.value = false;

                    // Clear the recording from IndexedDB
                    await audioComposable.clearRecordingSession();

                    showToast(safeT('messages.recordingDiscarded'), 'info');
                    recoverableRecording.value = null;
                } catch (error) {
                    console.error('[App] Failed to discard recording:', error);
                }
            };

            // Non-destructive close (X button / click-outside): just dismiss
            // the modal without finalizing or discarding. The recording stays
            // recoverable and is offered again next load.
            const dismissRecovery = () => {
                showRecoveryModal.value = false;
                recoverableRecording.value = null;
            };

            // Server-session recovery: finish processing what was uploaded.
            // Routes through the same finalize → stitch → transcribe pipeline
            // as a normal Stop+Upload, and clears the redundant local copy so
            // it isn't offered again.
            const finalizeRecoveredSession = async () => {
                const rec = recoverableRecording.value;
                showRecoveryModal.value = false;
                recoverableRecording.value = null;
                if (!rec || !rec.sessionId) return;
                try {
                    const ServerSessions = await import('./modules/db/server-recording-sessions.js');
                    await ServerSessions.finalizeSession(rec.sessionId, {});
                    try { await audioComposable.clearRecordingSession(); } catch (_) { /* ignore */ }
                    showToast(safeT('messages.recordingRecovered') || safeT('toasts.recordingFinalized') || 'Recording uploaded for processing', 'success');
                    // Surface it live in the sidebar + processing-queue panel.
                    try { await utils.onServerRecordingQueued?.(); } catch (_) { /* non-fatal */ }
                } catch (e) {
                    console.warn('[App] Could not finalize recovered server session:', e);
                    setGlobalError(`Could not recover the session: ${e.message}`);
                }
            };

            // Server-session recovery: RESUME. Continue recording into the
            // same server session — a fresh MediaRecorder appends a new segment
            // after the audio already on the server (the segment-aware stitch
            // concatenates them at finalize). We re-read the live chunk count so
            // the new segment starts at the correct index.
            const resumeRecoveredSession = async () => {
                const rec = recoverableRecording.value;
                showRecoveryModal.value = false;
                recoverableRecording.value = null;
                if (!rec || !rec.sessionId) return;
                try {
                    const ServerSessions = await import('./modules/db/server-recording-sessions.js');
                    let chunkCount = rec.chunkCount || 0;
                    let priorBytes = rec.totalSize || 0;
                    try {
                        const status = await ServerSessions.getSessionStatus(rec.sessionId);
                        if (!status || status.status !== 'recording') {
                            setGlobalError(safeT('errors.recordingNotResumable') || 'That recording can no longer be resumed.');
                            ServerSessions.forgetActiveSession();
                            return;
                        }
                        if (typeof status.chunk_count === 'number') chunkCount = status.chunk_count;
                        if (typeof status.bytes_received === 'number') priorBytes = status.bytes_received;
                    } catch (_) { /* fall back to the modal's count */ }
                    const mode = rec.mode || 'microphone';
                    const priorSeconds = rec.duration || (chunkCount * 5);
                    await audioComposable.startRecording(mode, {
                        sessionId: rec.sessionId,
                        mimeType: rec.mimeType,
                        startIndex: chunkCount + 1,
                        priorSeconds,
                        priorBytes,
                    });
                } catch (e) {
                    console.warn('[App] Could not resume server session:', e);
                    setGlobalError(`Could not resume the recording: ${e.message}`);
                }
            };

            // Server-session recovery: discard. Abort on the server (reaps the
            // chunks now) and clear the local copy.
            const discardRecoveredSession = async () => {
                const rec = recoverableRecording.value;
                showRecoveryModal.value = false;
                recoverableRecording.value = null;
                if (!rec || !rec.sessionId) return;
                try {
                    const ServerSessions = await import('./modules/db/server-recording-sessions.js');
                    await ServerSessions.abortSession(rec.sessionId);
                } catch (_) { /* ignore */ }
                try { await audioComposable.clearRecordingSession(); } catch (_) { /* ignore */ }
                showToast(safeT('messages.recordingDiscarded'), 'info');
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
                filteredTagsForFilter,
                filteredSpeakersForFilter,
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
                showDuplicatesModal,
                videoCollapsed,
                videoFullscreen,
                fullscreenControlsVisible,
                currentSubtitle,
                duplicatesModalData,
                openDuplicatesModal,
                navigateToDuplicate,
                tagsWithCustomPrompts,
                recordingDisclaimerHtml,
                uploadDisclaimerHtml,
                customBannerHtml,
                acceptUploadDisclaimer,
                cancelUploadDisclaimer,
                getTagPromptPreview,

                // Utilities
                formatFileSize,
                parseServerInstant,
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
                loadTokenBudget,
                getContrastTextColor,
                getBubbleGlobalIndex,
                formatRecordingMode,

                // Modal audio (independent from main player)
                modalAudioCurrentTime,
                modalAudioDuration,
                modalAudioIsPlaying,
                modalAudioProgressPercent,
                handleModalAudioTimeUpdate,
                handleModalAudioLoadedMetadata,
                handleModalAudioPlayPause,
                resetModalAudioState,

                // Virtual scroll
                speakerModalTranscriptRef,
                mainTranscriptRef,
                asrEditorRef,
                asrEditorSaveFlash,
                speakerModalVisibleSegments: speakerModalVirtualScroll.visibleItems,
                speakerModalSpacerBefore: speakerModalVirtualScroll.spacerBefore,
                speakerModalSpacerAfter: speakerModalVirtualScroll.spacerAfter,
                onSpeakerModalScroll: speakerModalVirtualScroll.onScroll,
                mainTranscriptVisibleSegments: mainTranscriptVirtualScroll.visibleItems,
                mainTranscriptSpacerBefore: mainTranscriptVirtualScroll.spacerBefore,
                mainTranscriptSpacerAfter: mainTranscriptVirtualScroll.spacerAfter,
                onMainTranscriptScroll: mainTranscriptVirtualScroll.onScroll,
                asrEditorVisibleSegments: asrEditorVirtualScroll.visibleItems,
                asrEditorSpacerBefore: asrEditorVirtualScroll.spacerBefore,
                asrEditorSpacerAfter: asrEditorVirtualScroll.spacerAfter,
                onAsrEditorScroll: asrEditorVirtualScroll.onScroll,
                scrollToSegmentIndex,
                getVirtualItemKey,

                // Recording recovery
                showRecoveryModal,
                recoverableRecording,
                recoverRecording,
                cancelRecovery,
                dismissRecovery,
                finalizeRecoveredSession,
                discardRecoveredSession,
                resumeRecoveredSession,

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
                ...foldersComposable,
                ...pwaComposable,
                ...bulkSelectionComposable,
                ...bulkOperationsComposable
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
