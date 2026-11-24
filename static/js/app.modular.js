const { createApp, ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } = Vue;

// Import composables
import { useRecordings } from './modules/composables/recordings.js';
import { isValidDateInput } from './modules/utils/date-filter.js';
import { resolveInitialSort } from './modules/utils/sort-preference.js';
import { releasesToShow, releaseVersion } from './modules/utils/whats-new.js';
import { useUpload } from './modules/composables/upload.js';
import { useAudio } from './modules/composables/audio.js';
import { useUI } from './modules/composables/ui.js';
import { useUiTheme } from './modules/composables/ui-theme.js';
import { useModals } from './modules/composables/modals.js';
import { useSharing } from './modules/composables/sharing.js';
import { resolveSearchScope } from './modules/utils/share-picker.js';
import { useReprocess } from './modules/composables/reprocess.js';
import { useTranscription } from './modules/composables/transcription.js';
import { useFindReplace } from './modules/composables/find-replace.js';
import { useAsrEditor } from './modules/composables/asr-editor.js';
import { createAsrEditorPane } from './modules/asr-editor/pane.js';
import { createAsrEditorTimeline } from './modules/asr-editor/timeline.js';
import { useSpeakers } from './modules/composables/speakers.js';
import { useSpeakerModal } from './modules/composables/speaker-modal.js';
import { useChat } from './modules/composables/chat.js';
import { createChatThreads, clearThreadsOnSignOut, threadKey } from './modules/composables/chat-threads.js';
import { useTags } from './modules/composables/tags.js';
import { usePWA } from './modules/composables/pwa.js';
import { useNotifications } from './modules/composables/notifications.js';
import { scrollSegmentIntoView, useTranscriptView } from './modules/composables/transcript-view.js';
import { segmentRuns } from './modules/utils/transcript-window.js';
import { useBulkSelection } from './modules/composables/bulk-selection.js';
import { useBulkOperations } from './modules/composables/bulk-operations.js';
import { useFolders } from './modules/composables/folders.js';
import { usePhoneLayout } from './modules/composables/phone-layout.js';
import { useAppShortcuts } from './modules/composables/shortcuts.js';

// Import utilities
import { placeToasts, showToast } from './modules/utils/toast.js';
import { getContrastTextColor } from './modules/utils/colors.js';
import { labelPathForFilter, parseLabelPath } from './modules/utils/label-links.js';
import { buildVariableList } from './modules/utils/prompt-variables.js';
import {
    MAX_PROMPT_LENGTH as REGEN_MAX_PROMPT_LENGTH, groupStartFrom, normalizePrompt, optionLabel,
    baseSourceKind, promptsDiffer, runVariables, saveLabel, sourceLabel,
} from './modules/utils/regenerate-summary.js';
import { errorTextMatches } from './modules/utils/error-patterns.js';
import { cancelResult, clockOffsetMs, parseServerInstant as parseQueueInstant, queueFieldsFromJob, settledCancels } from './modules/utils/queue-data.js';
import {
    cancelNeedsConfirm, finishedAgo, formatElapsed, groupQueueItems, queueSummary as summarizeQueue,
    rowMark, rowProgress, stageLabel, summaryParts,
} from './modules/utils/queue-view.js';
import { canCancelUpload } from './modules/utils/upload-cancel.js';
import { installCloseOutside } from './modules/utils/close-outside.js';
import { installTabsBarFit } from './modules/utils/tabs-bar-fit.js';
import {
    detectPlatform,
    getAudioCapabilities,
    enumerateVirtualAudioDevices,
    normalizeAudioInputDevices
} from './modules/utils/platform.js';

// Number of speaker colors available in CSS (must match styles.css)
const SPEAKER_COLOR_COUNT = 16;

// Parse transcription text to detect if it's an error message
// Remove ANSI terminal colour codes (real ESC or the literal  form that
// survives JSON) that some ASR runtimes inject into error bodies. Cleans errors
// already stored in the database at display time, complementing the backend
// which strips them before storing new ones.
const stripAnsi = (s) => typeof s === 'string'
    ? s.replace(/(?:\x1b|\\x1b|\\u001b)\[[0-9;]*m/g, '')
    : s;

const parseTranscriptionError = (text) => {
    if (!text) return null;

    // Check for JSON-formatted error from backend
    if (text.startsWith('ERROR_JSON:')) {
        try {
            const jsonStr = text.substring(11);
            const data = JSON.parse(jsonStr);
            const _t = (key, fb) => (window.i18n && window.i18n.t) ? window.i18n.t(key) : fb;
            // A canceled first transcription (src/services/job_cancel.py): not
            // an error, and worded in the language of the interface
            if (data.y === 'canceled') {
                return {
                    title: _t('errors.canceledTitle', data.t || 'Processing canceled'),
                    message: _t('errors.canceledMessage', data.m || ''),
                    guidance: _t('errors.canceledGuidance', data.g || ''),
                    icon: data.i || 'fa-ban',
                    type: 'canceled',
                    level: 'info',
                    isKnown: true,
                    technical: ''
                };
            }
            return {
                title: data.t || _t('errors.fallbackTitle', 'Error'),
                message: stripAnsi(data.m) || _t('errors.fallbackMessage', 'Something went wrong.'),
                guidance: data.g || '',
                icon: data.i || 'fa-exclamation-circle',
                type: data.y || 'unknown',
                isKnown: data.k || false,
                technical: stripAnsi(data.d || '')
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
    text = stripAnsi(String(text));
    const lowerText = text.toLowerCase();

    // Known error patterns
    const patterns = [
        {
            // Same list as the no-speech entry of ERROR_PATTERNS in src/utils/error_formatting.py.
            patterns: [/\bno_speech\b/, /no (?:recognized |recognised |usable )?speech (?:was )?detected/, 'asr_response_have_no_words'],
            title: _t('errors.noSpeechTitle', 'No speech detected'),
            message: _t('errors.noSpeechMessage', 'No recognizable speech was found in this recording.'),
            guidance: _t('errors.noSpeechGuidance', 'Check that the microphone is not muted, the correct input device was selected, and the audio volume is adequate, then record or upload the audio again.'),
            icon: 'fa-microphone-slash',
            type: 'no_speech'
        },
        {
            patterns: ['maximum content size limit', 'file too large', '413', 'payload too large', 'exceeded'],
            title: _t('errors.fileTooLargeTitle', 'File too large'),
            message: _t('errors.fileTooLargeMessage', 'The file is over the transcription service\'s size limit.'),
            guidance: _t('errors.fileTooLargeGuidance', 'Enable audio chunking in your admin settings, or compress the audio file before uploading. If chunking is already enabled the file may still exceed the per-chunk limit.'),
            icon: 'fa-file-audio',
            type: 'size_limit'
        },
        {
            patterns: ['timed out', 'timeout', 'deadline exceeded'],
            title: _t('errors.processingTimeout', 'Processing timeout'),
            message: _t('errors.processingTimeoutMessage', 'The transcription took too long to complete.'),
            guidance: _t('errors.processingTimeoutGuidance', 'This can happen with very long recordings. Try splitting the audio into smaller parts.'),
            icon: 'fa-clock',
            type: 'timeout'
        },
        {
            patterns: ['401', 'unauthorized', 'invalid api key', 'authentication failed', 'incorrect api key'],
            title: _t('errors.authenticationError', 'Authentication error'),
            message: _t('errors.authenticationErrorMessage', 'The transcription service\'s API key was rejected.'),
            guidance: _t('errors.authenticationErrorGuidance', 'Check that the API key is correct and has not expired.'),
            icon: 'fa-key',
            type: 'auth'
        },
        {
            patterns: ['rate limit', 'too many requests', '429', 'quota exceeded'],
            title: _t('errors.rateLimitExceeded', 'Rate limit exceeded'),
            message: _t('errors.rateLimitExceededMessage', 'Too many requests were sent to the transcription service.'),
            guidance: _t('errors.rateLimitExceededGuidance', 'Wait a few minutes and try reprocessing.'),
            icon: 'fa-hourglass-half',
            type: 'rate_limit'
        },
        {
            patterns: ['connection refused', 'connection reset', 'could not connect', 'network unreachable'],
            title: _t('errors.connectionError', 'Connection error'),
            message: _t('errors.connectionErrorMessage', 'Couldn\'t connect to the transcription service.'),
            guidance: _t('errors.connectionErrorGuidance', 'Check your connection and that the transcription service is running.'),
            icon: 'fa-wifi',
            type: 'connection'
        },
        {
            patterns: ['503', '502', '500', 'service unavailable', 'server error', 'internal server error'],
            title: _t('errors.serviceUnavailable', 'Service unavailable'),
            message: _t('errors.serviceUnavailableMessage', 'The transcription service is temporarily unavailable.'),
            guidance: _t('errors.serviceUnavailableGuidance', 'Usually temporary. Try again in a few minutes.'),
            icon: 'fa-server',
            type: 'service_error'
        },
        {
            patterns: ['invalid file format', 'unsupported format', 'could not decode', 'corrupt', 'not valid audio', 'invalid data found when processing input'],
            title: _t('errors.invalidAudioFormat', 'Invalid audio format'),
            message: _t('errors.invalidAudioFormatMessage', 'The audio file format is not supported or the file may be corrupted.'),
            guidance: _t('errors.invalidAudioFormatGuidance', 'Try converting the audio to MP3 or WAV format before uploading.'),
            icon: 'fa-file-audio',
            type: 'format'
        },
        {
            patterns: ['audio extraction failed', 'ffmpeg failed', 'no audio stream'],
            title: _t('errors.audioExtractionFailed', 'Audio extraction failed'),
            message: _t('errors.audioExtractionFailedMessage', 'Couldn\'t extract audio from the uploaded file.'),
            guidance: _t('errors.audioExtractionFailedGuidance', 'Try converting the file to a standard audio format (MP3, WAV) before uploading.'),
            icon: 'fa-file-video',
            type: 'extraction'
        }
    ];

    // Check patterns
    for (const pattern of patterns) {
        for (const p of pattern.patterns) {
            if (errorTextMatches(lowerText, p)) {
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
        title: _t('errors.processingError', 'Processing error'),
        message: cleanMessage,
        guidance: _t('errors.processingErrorGuidance', 'Try reprocessing the recording.'),
        icon: 'fa-exclamation-circle',
        type: 'unknown',
        isKnown: false,
        technical: text
    };
};

// Wait for the DOM to be fully loaded before mounting the Vue app
document.addEventListener('DOMContentLoaded', async () => {
    installCloseOutside();
    installTabsBarFit();
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

    // The service worker is registered by js/service-worker-client.js, which
    // every signed-in page loads.

    // The help markup returned by helpCode/helpLink escapes its text (#692).
    const safeHtml = window.safeHtml;

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
            // Set true on mount when arriving via the /label/<name> deep link.
            // The first loadRecordings would otherwise auto-select the last
            // viewed recording, opening the detail view and rewriting the
            // address bar to /recordings/<id>. The flag suppresses that one
            // auto-selection so the label list is what the user lands on.
            const labelDeepLinkPending = ref(false);
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
            const filterNeedsTranscription = ref(false);
            const filterNeedsSummary = ref(false);
            const filterNeedsSpeakers = ref(false);
            const filterAudioRemoved = ref(false);  // recordings whose media file was removed
            const showArchivedRecordings = ref(false);  // the user's archive view (#394)
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
            // (discussion #263). A browser that has not chosen yet starts from the
            // account's default sort (Account > Preferences), then 'created_at'.
            // Unknown values are ignored, so a stale or tampered localStorage
            // value cannot put us in an unknown state.
            const sortBy = ref(resolveInitialSort(
                localStorage.getItem('recordingsSortBy'),
                document.getElementById('app')?.dataset.defaultRecordingSort));
            const selectedTagFilter = ref(null);

            // --- UI State ---
            const browser = ref('unknown');
            const isSidebarCollapsed = ref(false);
            const searchTipsExpanded = ref(false);
            const isUserMenuOpen = ref(false);
            // Detail-header "assign folder" dropdown (replaces a raw <select>
            // whose OS-rendered option list ignored the app theme).
            const showHeaderFolderMenu = ref(false);
            const showHeaderReprocessMenu = ref(false);
            const showHeaderMoreMenu = ref(false);
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
            // The panel of the processing queue starts minimized: the page shows
            // its one-line indicator. An upload or a recording that starts
            // opens the panel (composables/upload.js, audio.js).
            const progressPopupMinimized = ref(true);
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
            const whatsNew = ref({ show: false, items: [], version: '', releases: [] });
            // Close the What's new dialog and record it for the account, so
            // other devices do not show it again.
            const dismissWhatsNew = async () => {
                whatsNew.value = { ...whatsNew.value, show: false };
                try {
                    await fetch('/api/user/whats-new/seen', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken.value },
                    });
                } catch (e) {
                    console.warn('Could not record that What\'s new was read:', e);
                }
            };
            const customBanner = ref('');
            const showBanner = ref(true);

            // --- Audio Recording State ---
            const isRecording = ref(false);
            const isPaused = ref(false);
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

            // Opt-in: also record the shared tab/window/screen VIDEO during
            // system-audio and mic+system recordings (#303). Only offered when
            // the server has VIDEO_RETENTION enabled, since the resulting
            // video/webm recording relies on the existing video-retention
            // pipeline to keep its video stream. Persisted in localStorage.
            const recordSystemVideo = ref(
                (typeof localStorage !== 'undefined' && localStorage.getItem('recordSystemVideo') === 'true')
            );
            watch(recordSystemVideo, (v) => {
                try { localStorage.setItem('recordSystemVideo', v ? 'true' : 'false'); } catch (_) {}
            });
            // True while a video-capturing recording is live — drives the
            // preview panel in the recording view (audio.js attaches the
            // display stream to the <video id="recordingVideoPreview">).
            const recordingVideoActive = ref(false);

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
                if (typeof navigator === 'undefined'
                    || !navigator.mediaDevices
                    || typeof navigator.mediaDevices.enumerateDevices !== 'function') {
                    return [];
                }
                try {
                    const devs = await navigator.mediaDevices.enumerateDevices();
                    const inputs = normalizeAudioInputDevices(devs);
                    inputAudioDevices.value = inputs;
                    // Deliberately no reconciliation of the persisted device
                    // IDs here: a device can be momentarily absent from an
                    // enumeration (undock/redock, USB re-plug, devicechange
                    // races), and clearing the saved selection on that signal
                    // silently switches later recordings to the default mic.
                    // A genuinely stale selection is handled loudly at
                    // acquisition time instead: acquireMicrophoneStream falls
                    // back to the default, clears the saved ID, and shows the
                    // "selected microphone unavailable" toast.
                    return inputs;
                } catch (_) {
                    inputAudioDevices.value = [];
                    return [];
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

            const handleAudioDeviceChange = () => {
                refreshInputAudioDevices();
                refreshVirtualAudioDevices();
            };
            onMounted(() => {
                if (typeof navigator !== 'undefined'
                    && navigator.mediaDevices
                    && typeof navigator.mediaDevices.addEventListener === 'function') {
                    navigator.mediaDevices.addEventListener('devicechange', handleAudioDeviceChange);
                }
            });
            onUnmounted(() => {
                if (typeof navigator !== 'undefined'
                    && navigator.mediaDevices
                    && typeof navigator.mediaDevices.removeEventListener === 'function') {
                    navigator.mediaDevices.removeEventListener('devicechange', handleAudioDeviceChange);
                }
            });

            const showRecoveryModal = ref(false);
            const recoverableRecording = ref(null);
            const asrLanguage = ref('');
            const asrMinSpeakers = ref('');
            const asrMaxSpeakers = ref('');
            // Recorder-side transcription hints (#375): the in-app recorder
            // has its own asr* option refs (separate from the upload modal's
            // upload* refs) so a recording in progress keeps its options when
            // the upload UI state changes.
            const asrInitialPrompt = ref('');
            const asrHotwords = ref('');
            const asrTranscriptionModel = ref('');  // Per-recording model override (#266 parity with upload)
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
            // The floating bulk bar sits against the right edge of the
            // sidebar, whose width differs per theme: the edge is measured
            // into --bulk-bar-left while selection mode is on.
            const placeBulkBar = () => {
                const side = document.querySelector('aside.sidebar');
                if (!side) return;
                const right = Math.round(side.getBoundingClientRect().right);
                document.documentElement.style.setProperty('--bulk-bar-left', right + 'px');
            };
            watch(selectionMode, (on) => {
                const fn = on ? 'addEventListener' : 'removeEventListener';
                window[fn]('resize', placeBulkBar);
                if (on) nextTick(placeBulkBar);
            });
            const selectedRecordingIds = ref(new Set());
            const bulkActionInProgress = ref(false);

            // --- Recording Size Monitoring ---
            const estimatedFileSize = ref(0);
            const fileSizeWarningShown = ref(false);
            const recordingQuality = ref('optimized');
            const actualBitrate = ref(0);
            const maxRecordingMB = ref(200);
            const sizeCheckInterval = ref(null);
            // True while the CURRENT recording streams its chunks to the
            // server (#287 Phase B/C). Set by the audio composable when the
            // upload session actually opens — not from the feature flag —
            // because session creation can fail and fall back to local-only
            // RAM recording, where the size limit still applies. Gates the
            // size-limit warning UI, which is meaningless when streaming
            // (#332: there is no size cap in that mode, only an hours cap).
            const isServerStreamedRecording = ref(false);
            // True from the moment the user clicks Upload on a finished
            // recording until the finalize (or legacy queue hand-off)
            // resolves. Drives the button's disabled/spinner state and the
            // re-entrancy guard in uploadRecordedAudio: draining the chunk
            // backlog can take many seconds, and without immediate feedback
            // users double-click — which used to create duplicate recordings.
            const isFinalizingRecording = ref(false);

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
            // Same picker for the in-app recorder's Advanced ASR Options,
            // which use the recorder-side asr* refs (#375).
            const applyInitialPromptTemplateToRecording = (id) => {
                const tpl = initialPromptTemplates.value.find(t => String(t.id) === String(id));
                if (!tpl) return;
                asrInitialPrompt.value = tpl.template || '';
                asrHotwords.value = tpl.hotwords || '';
            };
            // Detail-view popover showing which hotwords/initial-prompt a
            // recording actually used (#309).
            const showHintsPopover = ref(false);
            // Inline position overrides for the hints popover. The popover is
            // anchored `absolute left-0 top-full` to its chip, so when the chip
            // sits near the right viewport edge (or the prompt text is long)
            // the panel would clip off-screen. On open we measure and clamp:
            // shift left to stay inside the right edge, flip above the chip
            // when there is clearly more room there, and cap height with
            // internal scrolling otherwise.
            const hintsPopoverStyle = ref({});
            const toggleHintsPopover = (event) => {
                if (showHintsPopover.value) {
                    showHintsPopover.value = false;
                    return;
                }
                const btn = event.currentTarget;
                hintsPopoverStyle.value = {};
                showHintsPopover.value = true;
                nextTick(() => {
                    const pop = btn.closest('.relative')?.querySelector('.hints-popover');
                    if (!pop) return;
                    const margin = 8;
                    const rect = pop.getBoundingClientRect();
                    const btnRect = btn.getBoundingClientRect();
                    const style = {};

                    const overflowRight = rect.right - (window.innerWidth - margin);
                    if (overflowRight > 0) {
                        // Never shift so far that the left edge leaves the viewport.
                        const shift = Math.min(overflowRight, Math.max(rect.left - margin, 0));
                        style.transform = `translateX(-${Math.round(shift)}px)`;
                    }

                    const spaceBelow = window.innerHeight - margin - rect.top;
                    const spaceAbove = btnRect.top - margin;
                    if (rect.height > spaceBelow) {
                        if (spaceBelow < 120 && spaceAbove > spaceBelow) {
                            style.top = 'auto';
                            style.bottom = '100%';
                            style.marginTop = '0';
                            style.marginBottom = '0.25rem';
                            style.maxHeight = `${Math.floor(spaceAbove)}px`;
                        } else {
                            style.maxHeight = `${Math.floor(spaceBelow)}px`;
                        }
                        style.overflowY = 'auto';
                    }

                    hintsPopoverStyle.value = style;
                });
            };
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
            // Folder filter also lists recordings in subfolders (#585); off by default.
            const includeChildFolders = ref(false);

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
            // Segment editor: the open editing session (model and pane API,
            // not reactive: see modules/composables/asr-editor.js), whether
            // it has unsaved changes, a counter of its changes for find and
            // replace, and whether a save is under way.
            const asrEditorSession = Vue.shallowRef(null);
            const asrEditorDirty = ref(false);
            const asrEditorRev = ref(0);
            const asrEditorSaving = ref(false);
            // The footer's checkbox: regenerate the summary after the save (off when the editor opens)
            const asrEditorRegenerateSummary = ref(false);
            // Regenerate summary dialog (#584). prompt is the textarea; base is
            // the start-from entry it began as; resolvedPrompt is the viewer's
            // resolved prompt, which an unchanged owner run leaves to the server.
            const showRegenerateSummaryModal = ref(false);
            const regenSummary = reactive({
                recording: null, generate: false, loading: false, loadError: '', saving: false,
                prompt: '', resolvedPrompt: '', instructions: '', isOwner: false,
                base: null, used: null, startFrom: [], startFromOpen: false, variablesOpen: false,
            });
            const editingRecording = ref(null);
            const editingTranscriptionContent = ref('');
            // Find and replace bar of the transcript editors (#440)
            const findReplace = reactive({ open: false, find: '', replace: '', matchCase: false, wholeWord: false, current: -1 });
            const availableSpeakers = ref([]);
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
            const speakerMap = ref({});
            const speakerColorMap = ref({}); // Stable mapping of speaker ID → color class
            const modalSpeakers = ref([]);
            const regenerateSummaryAfterSpeakerUpdate = ref(true);
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
            // Format menu on the summary download button (Word / Markdown, #584).
            const showSummaryDownloadMenu = ref(false);
            // Split-button menu on the recording-view "Upload Recording & Notes"
            // action (offers "merge with an existing recording").
            const showUploadSplitMenu = ref(false);
            // Preferred transcript download format ('txt' | 'md'), persisted.
            const transcriptDownloadFormat = ref(localStorage.getItem('transcriptDownloadFormat') === 'md' ? 'md' : 'txt');
            watch(transcriptDownloadFormat, (val) => {
                localStorage.setItem('transcriptDownloadFormat', val === 'md' ? 'md' : 'txt');
            });
            const currentPlayingSegmentIndex = ref(null);
            const followPlayerMode = ref(false);
            const processingIndicatorMinimized = ref(false);

            // --- Translation view state ---
            const viewingTranslation = ref(false);
            const showTranslationModal = ref(false);
            // Default target language resolved server-side: preferred output
            // language, then interface language. Used when the recording has
            // no translation yet.
            const defaultTranslationLanguage = ref('');
            const translationTargetLanguage = ref('');
            // (full_name, iso_code) list shared with the account page and the
            // target-language select, read from #app's data-output-languages.
            // Parsed up front so the tab label is localized on first render.
            const outputLanguages = ref((() => {
                try {
                    return JSON.parse(document.getElementById('app')?.dataset.outputLanguages || '[]');
                } catch (_) {
                    return [];
                }
            })());
            // Label a stored output-language name ("Spanish") in the interface
            // language, keeping the stored value when the list does not know it.
            // Accepts an ISO code too, for rows written before the name was
            // canonicalized.
            const translationLanguageLabel = (stored) => {
                if (!stored) return '';
                const needle = String(stored).trim().toLowerCase();
                const entry = outputLanguages.value.find(([name, iso]) =>
                    String(name).toLowerCase() === needle || String(iso).toLowerCase() === needle);
                if (!entry) return stored;
                return outputLanguageLabel(entry[0], entry[1]);
            };
            // Locale key, then the browser's name of the language, then the
            // English name (asr-languages.js), so the entries without a
            // `languages.*` key are still shown in the interface language.
            const outputLanguageLabel = (name, iso) => {
                const locale = (window.i18n && window.i18n.currentLocale) || 'en';
                if (window.SpeakrASRLanguages?.outputLabel) {
                    return window.SpeakrASRLanguages.outputLabel(name, iso, locale);
                }
                const key = `languages.${iso}`;
                const label = t(key);
                return label && label !== key ? label : name;
            };
            // Options of the target-language select, sorted by label. A
            // language already used for the recording that the list does not
            // hold stays selectable.
            const translationLanguageOptions = computed(() => {
                const locale = (window.i18n && window.i18n.currentLocale) || 'en';
                const options = outputLanguages.value.map(([name, iso]) => (
                    { value: name, label: outputLanguageLabel(name, iso) }));
                options.sort((a, b) => a.label.localeCompare(b.label, locale));
                const current = translationTargetLanguage.value;
                if (current && !options.some(o => o.value === current)) {
                    options.push({ value: current, label: current });
                }
                return options;
            });
            // Keep a re-run on the language already used for this recording;
            // otherwise offer the account's preferred language.
            const openTranslationModal = () => {
                translationTargetLanguage.value =
                    selectedRecording.value?.transcription_translation_language
                    || defaultTranslationLanguage.value
                    || '';
                showTranslationModal.value = true;
            };
            watch(() => selectedRecording.value?.id, () => {
                viewingTranslation.value = false;
                showTranslationModal.value = false;
            });

            // --- Chat State ---
            const showChat = ref(false);
            const isChatMaximized = ref(false);
            // One Ambrose thread per recording, kept in this tab
            // (composables/chat-threads.js). These three are the open
            // recording's thread.
            const chatThreads = createChatThreads({
                reactive,
                userId: document.getElementById('app')?.dataset.currentUserId || '',
            });
            chatThreads.restore();
            clearThreadsOnSignOut(() => chatThreads.clearAll());
            window.addEventListener('pagehide', () => chatThreads.save());
            const threadField = (name) => computed({
                get: () => chatThreads.current()[name],
                set: (value) => { chatThreads.current()[name] = value; },
            });
            const chatMessages = threadField('messages');
            const chatInput = threadField('input');
            const isChatLoading = threadField('loading');
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

            // Height of the strip under the recording view's content columns
            // (bottom audio player, theme status bars), published on the root
            // element as --detail-bottom-inset so the processing queue
            // (progress-popup.html, primitives.css) can sit above it. Removed
            // outside the recording view. Re-measured on the layout changes the
            // watcher sees, and by a ResizeObserver for a theme switch, a
            // breakpoint or the player growing for video.
            let _detailInsetObserver = null;
            let _detailInsetObserved = null;
            const publishDetailBottomInset = () => {
                const root = document.documentElement;
                const cols = document.querySelector('#mainContentColumns');
                if (typeof ResizeObserver !== 'undefined' && cols !== _detailInsetObserved) {
                    if (!_detailInsetObserver) _detailInsetObserver = new ResizeObserver(() => publishDetailBottomInset());
                    _detailInsetObserver.disconnect();
                    if (cols) _detailInsetObserver.observe(cols);
                    _detailInsetObserved = cols;
                }
                const colsRect = cols && cols.getClientRects().length > 0 ? cols.getBoundingClientRect() : null;
                if (colsRect && colsRect.height > 0) {
                    root.style.setProperty('--detail-bottom-inset', Math.max(0, Math.round(window.innerHeight - colsRect.bottom)) + 'px');
                } else {
                    root.style.removeProperty('--detail-bottom-inset');
                }
            };
            watch(
                () => [chatLayoutTick.value, currentView.value, selectedRecording.value?.id],
                publishDetailBottomInset,
                { flush: 'post' }
            );

            // Reactive on chatLayoutTick so resize / sidebar toggle
            // trigger recompute.
            const floatingChatPanelStyle = computed(() => {
                // Touch the tick to register reactivity for resize updates.
                void chatLayoutTick.value;

                if (chatPanelState.value === 'floating' && chatPanelX.value == null) {
                    const r = _rect('#mainContentColumns');
                    if (r && r.width > 0) {
                        return {
                            top: (r.bottom - chatPanelH.value - 24) + 'px',
                            left: (r.right - chatPanelW.value - 24) + 'px',
                            width: chatPanelW.value + 'px',
                            height: chatPanelH.value + 'px',
                            right: 'auto',
                            bottom: 'auto'
                        };
                    }
                }
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

            // One chat layout for every recording (best-effort; ignores
            // errors). It used to be stored per recording with absolute
            // viewport coordinates, so the panel opened on some recordings
            // only and landed mid-page after the window or sidebar changed.
            // The floating position is now kept as the gap to the
            // bottom-right corner of #mainContentColumns, the corner the
            // panel opens from, and every layout change re-applies that gap.
            const CHAT_PANEL_KEY = 'chat_panel_layout';
            let _chatAnchor = null;  // {dx, dy} of the floating panel
            const saveChatPanelPosition = () => {
                const data = { state: chatPanelState.value, w: chatPanelW.value, h: chatPanelH.value };
                const r = _rect('#mainContentColumns');
                if (chatPanelState.value === 'floating' && chatPanelX.value != null && r && r.width > 0) {
                    data.dx = Math.round(r.right - (chatPanelX.value + chatPanelW.value));
                    data.dy = Math.round(r.bottom - (chatPanelY.value + chatPanelH.value));
                    _chatAnchor = { dx: data.dx, dy: data.dy };
                } else if (_chatAnchor) {
                    data.dx = _chatAnchor.dx;
                    data.dy = _chatAnchor.dy;
                }
                try { localStorage.setItem(CHAT_PANEL_KEY, JSON.stringify(data)); } catch (e) { /* ignore */ }
            };
            const _applyChatAnchor = () => {
                if (!_chatAnchor || chatPanelState.value !== 'floating') return;
                if (chatDragActive.value || chatResizeActive.value) return;
                const r = _rect('#mainContentColumns');
                if (!r || r.width <= 0) return;
                chatPanelX.value = r.right - chatPanelW.value - _chatAnchor.dx;
                chatPanelY.value = r.bottom - chatPanelH.value - _chatAnchor.dy;
            };
            const restoreChatPanelPosition = () => {
                const validStates = ['collapsed', 'floating', 'dock-left', 'dock-right', 'dock-full'];
                let data = null;
                try {
                    data = JSON.parse(localStorage.getItem(CHAT_PANEL_KEY) || 'null');
                    // Drop the old per-recording entries.
                    for (let i = localStorage.length - 1; i >= 0; i--) {
                        const k = localStorage.key(i);
                        if (k && k.startsWith('chat_panel_pos_')) localStorage.removeItem(k);
                    }
                } catch (e) { data = null; }
                if (!data || !validStates.includes(data.state)) {
                    chatPanelState.value = 'collapsed';
                    return;
                }
                if (data.w) chatPanelW.value = data.w;
                if (data.h) chatPanelH.value = data.h;
                chatPanelState.value = data.state;
                _chatAnchor = Number.isFinite(data.dx) && Number.isFinite(data.dy)
                    ? { dx: data.dx, dy: data.dy } : null;
                if (data.state === 'floating') {
                    if (!_chatAnchor) _chatAnchor = { dx: 24, dy: 24 };
                    // Placed by the next layout pass, once the detail view
                    // of this recording has its final size.
                    chatPanelX.value = null;
                    chatPanelY.value = null;
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
                // Focus the input so the user can type immediately, instead of
                // having to click into it first. nextTick waits for the panel
                // (v-else of the FAB) to mount. A disabled input (recording not
                // yet transcribed) just ignores focus, which is fine.
                nextTick(() => {
                    try { chatInputRef.value?.focus(); } catch (_) { /* element not mounted */ }
                });
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

            // Pull a floating panel back inside the content area (#383).
            // Position and size are persisted per recording and restored
            // verbatim, and the drag handler only clamps on release, so a
            // panel saved on a large window came back off-screen on a
            // smaller one — with its header, and therefore its close and
            // dock buttons, above the visible area and unreachable. The
            // only way out was clearing localStorage by hand. Clamping
            // here covers both the restore (the selectedRecording watch
            // bumps the layout on the next tick) and a live resize.
            const clampChatPanelIntoView = () => {
                if (chatPanelState.value !== 'floating') return;
                if (chatPanelX.value == null || chatPanelY.value == null) return;
                // Don't fight the user mid-gesture; the drag and resize
                // handlers clamp on release.
                if (chatDragActive.value || chatResizeActive.value) return;
                const cols = _rect('#mainContentColumns');
                if (!cols || cols.width <= 0) return;

                // Shrink first: clamping a panel that no longer fits would
                // otherwise push its left/top edge off the other side.
                const MIN_W = 320, MIN_H = 360;
                chatPanelW.value = Math.max(MIN_W, Math.min(chatPanelW.value, cols.width - 16));
                chatPanelH.value = Math.max(MIN_H, Math.min(chatPanelH.value, cols.height - 16));

                // Math.max last so the top-left corner always wins when the
                // panel is larger than the area it has to fit into: a
                // reachable header matters more than a fully visible panel.
                chatPanelX.value = Math.max(
                    cols.left + 8,
                    Math.min(chatPanelX.value, cols.right - chatPanelW.value - 8)
                );
                chatPanelY.value = Math.max(
                    cols.top + 8,
                    Math.min(chatPanelY.value, cols.bottom - chatPanelH.value - 8)
                );
            };

            // Bump the layout tick on window resize / sidebar toggle so
            // the docked positioning style recomputes.
            const _bumpChatLayout = () => {
                _applyChatAnchor();
                clampChatPanelIntoView();
                chatLayoutTick.value += 1;
            };
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

            // --- App Configuration ---
            const useAsrEndpoint = ref(false);
            const connectorSupportsDiarization = ref(false);  // Connector capability for diarization UI
            const connectorSupportsSpeakerCount = ref(false);  // Connector capability for min/max speakers
            const connectorSupportsExactSpeakerCount = ref(false); // Connector accepts exactly-N speakers only (#362)
            const speakerCountMode = ref('range');           // User pref: 'range' (min/max) or 'single' (one count)
            const connectorSupportsHotwords = ref(false);     // Connector accepts hotword/keyword biasing
            const connectorSupportsInitialPrompt = ref(false); // Connector accepts initial prompt / context hint
            const connectorSupportsLanguage = ref(false);     // Connector sends a chosen language to the service
            const showTimestampsSimpleView = ref(false);     // User pref: display timestamps in simple view
            const editorAutosave = ref(false);                // User pref: autosave transcript editor
            const audioPlayerPosition = ref('bottom');        // User pref: 'bottom' or 'top' for desktop player placement
            const currentUserName = ref('');
            const canDeleteRecordings = ref(true);
            const enableInternalSharing = ref(false);
            const enableAudioRemovedFilter = ref(false);
            const showUsernamesInUI = ref(false);
            const userSearchScope = ref('exact');             // user_search_scope: everyone, groups or exact

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

            // --- Reprocessing Polls ---
            const reprocessingPolls = ref(new Map());

            // --- Virtual Scroll Container Refs ---
            const speakerModalTranscriptRef = ref(null);
            const mainTranscriptRef = ref(null);
            const asrEditorSaveFlash = ref(false);  // Brief "Saved" indicator after Save (without close)

            // --- Computed properties needed by composables ---
            const isMobileScreen = computed(() => windowWidth.value < 1024);

            // Collapse the sidebar when the window becomes narrow enough that
            // it turns into an overlay. Below the lg breakpoint an expanded
            // sidebar draws a backdrop over the whole app, so a desktop window
            // dragged narrow -- or, far more often, an iPad turned to portrait
            // -- would otherwise grey itself out with no hint as to why.
            // Only the crossing matters, so someone who opens the sidebar on a
            // narrow screen keeps it open.
            watch(isMobileScreen, (narrow) => {
                if (narrow) isSidebarCollapsed.value = true;
            });

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

            // Variables of the Regenerate summary run: those in the prompt
            // being run and in the added instructions (#584).
            const reprocessAvailableVariables = computed(() => {
                if (!regenSummary.recording) return [];
                const base = regenSummary.base || {};
                const typeOf = { tag: 'tag', tags: 'tag', folder: 'folder', user: 'user', admin: 'admin', default: 'admin' };
                return runVariables({
                    prompt: regenSummary.prompt,
                    instructions: regenSummary.instructions,
                    sourceType: typeOf[baseSourceKind(base)] || 'instructions',
                    sourceName: base.name || regenSourceLabel.value,
                    instructionsName: safeT('regenerateSummary.instructionsSource'),
                });
            });
            const regenEdited = computed(() => {
                const base = regenSummary.base;
                return !!base && promptsDiffer(regenSummary.prompt, base.prompt);
            });
            const regenSourceLabel = computed(() => (regenSummary.base
                ? sourceLabel(safeT, regenSummary.base, regenEdited.value) : ''));
            const regenSaveLabel = computed(() => {
                const base = regenSummary.base;
                if (!regenEdited.value || !base || !base.save_target) return '';
                if (!normalizePrompt(regenSummary.prompt)) return '';
                return saveLabel(safeT, base.save_target);
            });
            const regenStartFromGroups = computed(() => groupStartFrom(regenSummary.startFrom));
            const regenOptionLabel = (option) => optionLabel(safeT, option);
            const regenVariablesSetCount = computed(() => reprocessAvailableVariables.value
                .filter(v => String(reprocessPromptVariables[v.name] || '').trim() !== '').length);
            const regenCanRun = computed(() => !!regenSummary.recording && !regenSummary.loading
                && !regenSummary.loadError && !!normalizePrompt(regenSummary.prompt)
                && regenSummary.prompt.length <= REGEN_MAX_PROMPT_LENGTH);
            const isMobileDevice = computed(() => {
                return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) ||
                       ('ontouchstart' in window) ||
                       (navigator.maxTouchPoints > 0);
            });

            const colorSchemes = {
                light: [
                    { id: 'blue', class: '' },
                    { id: 'emerald', class: 'theme-light-emerald' },
                    { id: 'purple', class: 'theme-light-purple' },
                    { id: 'rose', class: 'theme-light-rose' },
                    { id: 'amber', class: 'theme-light-amber' },
                    { id: 'teal', class: 'theme-light-teal' }
                ],
                dark: [
                    { id: 'blue', class: '' },
                    { id: 'emerald', class: 'theme-dark-emerald' },
                    { id: 'purple', class: 'theme-dark-purple' },
                    { id: 'rose', class: 'theme-dark-rose' },
                    { id: 'amber', class: 'theme-dark-amber' },
                    { id: 'teal', class: 'theme-dark-teal' }
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
            // UI theme (templates/includes/ui_theme.html); the theme slots
            // (templates/themes/slot.html) switch on it without a reload.
            const { uiTheme, setUiTheme } = useUiTheme(ref);

            const state = {
                // Core
                currentView, showUploadModal, uploadDeepLinkPending, labelDeepLinkPending, dragover, recordings, selectedRecording, selectedTab, searchQuery,
                isLoadingRecordings, globalError, csrfToken,

                // Filters
                showAdvancedFilters, filterTags, filterSpeakers, filterTagSearch, filterSpeakerSearch,
                filterDateRange, filterDatePreset, filterTextQuery, filterStarred, filterInbox,
                filterNeedsTranscription, filterNeedsSummary, filterNeedsSpeakers,
                filterAudioRemoved, showArchivedRecordings, showSharedWithMe, sortBy, selectedTagFilter,

                // Pagination
                currentPage, perPage, totalRecordings, totalPages, hasNextPage, hasPrevPage,
                isLoadingMore, searchDebounceTimer,

                // UI
                browser, isSidebarCollapsed, searchTipsExpanded, isUserMenuOpen, showHeaderFolderMenu, showHeaderReprocessMenu, showHeaderMoreMenu, tokenBudget, isDarkMode,
                currentColorScheme, showColorSchemeModal, windowWidth, mobileTab, isMetadataExpanded, expandedSection,
                showSortOptions, currentLanguage, currentLanguageName, availableLanguages, showLanguageMenu,
                colorSchemes, isMobileScreen, isMobileDevice,
                summaryWordCount, notesWordCount, recordingStats, hasRecordingStats,

                // Upload
                uploadQueue, allJobs, currentlyProcessingFile, processingProgress, processingMessage,
                isProcessingActive, pollInterval, progressPopupMinimized,
                maxFileSizeMB, chunkingEnabled, chunkingMode, chunkingLimit, chunkingLimitDisplay,
                maxConcurrentUploads, recordingDisclaimer, showRecordingDisclaimerModal, pendingRecordingMode,
                uploadDisclaimer, showUploadDisclaimerModal,
                whatsNew, dismissWhatsNew,
                customBanner, showBanner,
                showAdvancedOptions, userTranscriptionLanguage, uploadLanguage, uploadMinSpeakers, uploadMaxSpeakers, uploadHotwords, uploadInitialPrompt, initialPromptTemplates, applyInitialPromptTemplate, showHintsPopover, hintsPopoverStyle, toggleHintsPopover, uploadTranscriptionModel, uploadPromptVariables, showPromptVariablesPanel, selectedPromptVariables, reprocessAvailableVariables, transcriptionModelOptions,
                availableTags, selectedTagIds, uploadTagSearchFilter,
                availableFolders, selectedFolderId, foldersEnabled, filterFolder, includeChildFolders,

                // Audio Recording
                isRecording, isPaused, mediaRecorder, audioChunks, audioBlobURL, recordingTime, recordingInterval,
                canRecordAudio, canRecordSystemAudio, systemAudioSupported, systemAudioError,
                recordingNotes, showSystemAudioHelp, showSystemAudioHelpModal, disableAudioProcessing,
                recordSystemVideo, recordingVideoActive,
                inputAudioDevices, selectedMicDeviceId, selectedSecondaryDeviceId, refreshInputAudioDevices,
                platformInfo, audioCaps, helpModalOsTab, virtualAudioDevices, refreshVirtualAudioDevices,
                asrLanguage, asrMinSpeakers, asrMaxSpeakers, asrTranscriptionModel,
                asrInitialPrompt, asrHotwords, applyInitialPromptTemplateToRecording,
                audioContext, analyser, micAnalyser, systemAnalyser, visualizer, micVisualizer,
                systemVisualizer, animationFrameId, recordingMode, activeStreams,
                wakeLock, recordingNotification, isPageVisible,
                estimatedFileSize, fileSizeWarningShown, recordingQuality, actualBitrate,
                maxRecordingMB, sizeCheckInterval, isServerStreamedRecording, isFinalizingRecording,

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
                showTextEditorModal, showAsrEditorModal, asrEditorSession, asrEditorDirty, asrEditorRev, asrEditorSaving, asrEditorRegenerateSummary, showRegenerateSummaryModal, regenSummary, userSummaryPrompt, editingRecording, editingTranscriptionContent,
                findReplace, availableSpeakers,
                showEditParticipantsModal, editingParticipantsList, editingParticipantSuggestions, allParticipants,
                recordingToShare, shareOptions,
                generatedShareLink, existingShareDetected, recordingPublicShares, isLoadingPublicShares,
                userShares, isLoadingShares, copiedShareId,
                shareToDelete, showShareDeleteModal, recordingToDelete, recordingToReset,
                reprocessType, reprocessRecording, reprocessPromptVariables, isAutoIdentifying, asrReprocessOptions,
                speakerMap, speakerColorMap, modalSpeakers, regenerateSummaryAfterSpeakerUpdate,
                voiceSuggestions, loadingVoiceSuggestions,

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
                transcriptionViewMode, legendExpanded, highlightedSpeaker, showDownloadMenu, showSummaryDownloadMenu,
                transcriptDownloadFormat, showUploadSplitMenu,
                currentPlayingSegmentIndex, followPlayerMode, processingIndicatorMinimized,
                viewingTranslation, showTranslationModal, translationTargetLanguage,
                openTranslationModal, translationLanguageLabel, translationLanguageOptions,

                // Chat
                showChat, isChatMaximized, chatMessages, chatInput, isChatLoading, chatMessagesRef, chatInputRef, chatThreads,
                chatPanelState, chatPanelX, chatPanelY, chatPanelW, chatPanelH,
                chatDragActive, chatResizeActive, floatingChatPanelStyle, floatingChatFabStyle,
                chatLastDock, chatDockMenuOpen,
                openChatPanel, collapseChatPanel, toggleChatPanelMax,
                startChatPanelDrag, startChatPanelResize, dockChatPanel,
                toggleChatDockToLast,

                // Audio Player
                playerVolume, audioIsPlaying, audioCurrentTime, audioDuration, audioIsMuted, audioIsLoading, asrEditorAudio,
                modalAudioCurrentTime, modalAudioDuration, modalAudioIsPlaying, modalPlaybackRate,
                playbackRate, showSpeedMenu, playbackSpeeds, speedMenuPosition, showVolumeSlider,
                videoFullscreen, fullscreenControlsVisible, fullscreenControlsTimer, videoCollapsed,
                videoDockEnabled, isVideoRecording, toggleVideoDock,
                videoDockPosition, cycleVideoDockColumn, cycleVideoDockVertical,
                videoDockHeight, startVideoDockResize,

                // Column Resizing
                leftColumnWidth, rightColumnWidth, isResizing,

                // Dropdown Positioning

                // App Config
                useAsrEndpoint, connectorSupportsDiarization, connectorSupportsSpeakerCount, connectorSupportsExactSpeakerCount, speakerCountMode, connectorSupportsHotwords, connectorSupportsInitialPrompt, connectorSupportsLanguage, showTimestampsSimpleView, editorAutosave, audioPlayerPosition, formatTimestamp, currentUserName, canDeleteRecordings, enableInternalSharing, enableAudioRemovedFilter, showUsernamesInUI, userSearchScope,

                // Internal Sharing
                showUnifiedShareModal, internalShareUserSearch, internalShareSearchResults,
                internalShareRecording, internalSharePermissions, internalShareMaxPermissions, recordingInternalShares,
                isLoadingInternalShares, isSearchingUsers,

                // Reprocessing
                reprocessingPolls,

                // Virtual Scroll
                speakerModalTranscriptRef, mainTranscriptRef, asrEditorSaveFlash
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

            // Localized number and units (#481)
            // Without i18n.js (not loaded yet) the page still shows a size.
            const formatFileSize = (bytes) => (window.i18n?.formatFileSize ? window.i18n.formatFileSize(bytes) : `${Number(bytes) || 0} bytes`);
            const formatPercent = (pct, options) => (window.i18n?.formatPercent ? window.i18n.formatPercent(pct, options) : `${Math.round((Number(pct) || 0) * 10) / 10}%`);
            const formatNumber = (n, options) => (window.i18n?.formatNumber ? window.i18n.formatNumber(n, options) : String(n));

            // Parse a backend INSTANT timestamp. The backend stores/serializes
            // naive UTC (no zone designator); new Date() would parse a zoneless
            // string as LOCAL, showing the UTC clock value instead of converting
            // it. Append 'Z' so it's parsed as UTC; toLocale* then renders it in
            // the viewer's own timezone. meeting_date is stored as naive UTC too
            // (src/utils/dates.py, #320), so it uses this as well. Do NOT use it
            // for wall-clock event times, which are not UTC.
            const parseServerInstant = (s) => {
                if (s == null) return new Date(NaN);
                if (typeof s === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s) && !/(?:Z|[+-]\d{2}:?\d{2})$/.test(s)) {
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
                    return date.toLocaleDateString(window.i18n?.getIntlLocale?.(), {
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
                        return date.toLocaleDateString(window.i18n?.getIntlLocale?.(), { month: 'short', day: 'numeric' });
                    }
                    return date.toLocaleDateString(window.i18n?.getIntlLocale?.(), { year: 'numeric', month: 'short', day: 'numeric' });
                } catch (e) {
                    return dateString;
                }
            };

            // Clock time of a list row ("1:13 AM"), for themes with a time column in the sidebar list.
            const formatShortTime = (dateString) => {
                if (!dateString) return '';
                const date = parseServerInstant(dateString);
                if (isNaN(date.getTime())) return '';
                return date.toLocaleTimeString(window.i18n?.getIntlLocale?.(), { hour: 'numeric', minute: '2-digit' });
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
                    return date.toLocaleTimeString(window.i18n?.getIntlLocale?.(), { hour: '2-digit', minute: '2-digit' });
                }
                return date.toLocaleString(window.i18n?.getIntlLocale?.(), {
                    weekday: 'short', month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit'
                });
            };

            // Date helper functions. meeting_date and created_at are both naive
            // UTC instants, so the sidebar groups by the same local day the
            // label shows (#412: meeting_date was parsed as local, so a
            // recording near midnight was labelled one day and filed under another).
            const getDateForSorting = (recording) => {
                if (sortBy.value === 'meeting_date' && recording.meeting_date) {
                    return parseServerInstant(recording.meeting_date);
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

            const displayTranscription = computed(() => {
                if (!selectedRecording.value) return '';
                if (viewingTranslation.value) {
                    return selectedRecording.value.transcription_translation || '';
                }
                return selectedRecording.value.transcription || '';
            });

            const translationJob = computed(() => {
                const rec = selectedRecording.value;
                if (!rec) return null;
                return (allJobs.value || []).find(j =>
                    j.recording_id === rec.id &&
                    j.job_type === 'translate_transcription' &&
                    (j.job_status === 'queued' || j.job_status === 'processing')
                ) || null;
            });
            const translationJobActive = computed(() => !!translationJob.value);
            // "Translating · 12 of 18" once the running job has reported its
            // chunk count; the plain in-progress title before that.
            const translationProgressPercent = (job) => {
                if (!job || job.job_status !== 'processing' || !(job.progress_total > 0)) return null;
                return Math.min(100, Math.round(100 * (job.progress_done || 0) / job.progress_total));
            };
            const translationProgressTitle = computed(() => {
                const job = translationJob.value;
                if (translationProgressPercent(job) === null) return t('translation.inProgress');
                return t('translation.progress', { done: job.progress_done || 0, total: job.progress_total });
            });
            const translationProgress = computed(() => translationProgressPercent(translationJob.value));

            const processedTranscription = computed(() => {
                if (!displayTranscription.value) {
                    return { hasDialogue: false, content: '', speakers: [], simpleSegments: [], bubbleRows: [], isError: false };
                }

                const transcription = displayTranscription.value;

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
                        speaker: segment.speaker,
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
                                startTime: segment.startTime ?? segment.start_time,
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
                            startTime: segment.startTime ?? segment.start_time,
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
                            name: speaker,
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
                                    speaker: currentSpeakerId,
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
                            speaker: currentSpeakerId,
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
                        // The simple view renders the turns, also for a
                        // plain-text transcript with [Speaker]: lines
                        simpleSegmentRuns: segmentRuns(simpleSegments),
                        bubbleRows: bubbleRows,
                        speakers: speakerList.map(speaker => ({
                            name: speaker,
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
            // The recording chat's toasts open the recording that answered.
            utils.openRecordingById = recordingsComposable.selectRecordingById;
            utils.assistantName = window.speakrAssistantName || 'Ambrose';
            utils.onRecordingsDeleted = (ids) => chatThreads.drop(ids);

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

            // --- Inline tag/folder creation from the upload dialog ---
            // Opens the shared organizer modals (organizer-modals.js standalone
            // mode) and applies the created entity to the current selection.
            const createTagFromUploadSearch = () => {
                if (!window.SpeakrOrganizer) return;
                window.SpeakrOrganizer.openTagCreate({
                    name: (uploadTagSearchFilter.value || '').trim(),
                    onSaved: async (tag) => {
                        await recordingsComposable.loadTags();
                        uploadComposable.addTagToSelection(tag.id);
                        uploadTagSearchFilter.value = '';
                    },
                });
            };
            const createFolderFromUpload = () => {
                if (!window.SpeakrOrganizer) return;
                window.SpeakrOrganizer.openFolderCreate({
                    folders: availableFolders.value,
                    onSaved: async (folder) => {
                        await recordingsComposable.loadFolders();
                        selectedFolderId.value = folder.id;
                    },
                });
            };
            // --- New-recording dialog: tab, record source, tag combobox ---
            // The last-used tab and record source are remembered per browser.
            const _stored = (key, allowed, fallback) => {
                try {
                    const v = localStorage.getItem(key);
                    return allowed.includes(v) ? v : fallback;
                } catch (_) { return fallback; }
            };
            const uploadDialogTab = ref(_stored('uploadDialogTab', ['upload', 'record'], 'upload'));
            watch(uploadDialogTab, (v) => {
                try { localStorage.setItem('uploadDialogTab', v); } catch (_) {}
            });
            const recordSource = ref(_stored('recordSource', ['microphone', 'system', 'both'], 'microphone'));
            watch(recordSource, (v) => {
                try { localStorage.setItem('recordSource', v); } catch (_) {}
            });
            // Files dropped or chosen always belong to the Upload tab.
            watch(() => uploadQueue.value.filter(item => item.status === 'queued').length, (n, old) => {
                if (n > (old || 0)) uploadDialogTab.value = 'upload';
            });
            // A typed date outside 1900-2100, or a day that does not exist, is
            // not sent (#631): the panel says so and does not count it.
            const dateRangeActive = computed(() => {
                const r = filterDateRange.value || {};
                return isValidDateInput(r.start) || isValidDateInput(r.end);
            });
            const dateRangeInvalid = computed(() => {
                const r = filterDateRange.value || {};
                return [r.start, r.end].some(v => v && !isValidDateInput(v));
            });
            const showUploadTagMenu = ref(false);
            // The tag list is teleported to <body> and placed under its
            // field with fixed coordinates (above it where there is no room
            // beneath), so it lies over the dialog and never scrolls it.
            const uploadTagMenuStyle = ref({});
            let uploadTagField = null;
            const placeUploadTagMenu = () => {
                if (!uploadTagField || !uploadTagField.isConnected) return;
                const r = uploadTagField.getBoundingClientRect();
                const below = window.innerHeight - r.bottom - 12;
                const above = r.top - 12;
                const up = below < 180 && above > below;
                const style = {
                    left: r.left + 'px',
                    width: r.width + 'px',
                    maxHeight: Math.max(120, Math.min(280, up ? above : below)) + 'px',
                };
                if (up) style.bottom = (window.innerHeight - r.top + 4) + 'px';
                else style.top = (r.bottom + 4) + 'px';
                uploadTagMenuStyle.value = style;
            };
            const openUploadTagMenu = (event) => {
                uploadTagField = event.currentTarget.closest('.tag-combo__field');
                placeUploadTagMenu();
                showUploadTagMenu.value = true;
            };
            // Focus that moves between the field and its list keeps it open.
            const closeUploadTagMenu = (event) => {
                const to = event && event.relatedTarget;
                if (to && to.closest && (to.closest('.tag-combo__menu')
                        || (uploadTagField && uploadTagField.contains(to)))) return;
                showUploadTagMenu.value = false;
            };
            const pickUploadTag = (id) => {
                uploadComposable.addTagToSelection(id);
                uploadTagSearchFilter.value = '';
                const input = uploadTagField && uploadTagField.querySelector('input');
                if (input) input.focus();
                nextTick(placeUploadTagMenu);
            };
            const _followUploadTagField = (event) => {
                if (event && event.target && event.target.closest
                        && event.target.closest('.tag-combo__menu')) return;
                placeUploadTagMenu();
            };
            watch(showUploadTagMenu, (open) => {
                const fn = open ? 'addEventListener' : 'removeEventListener';
                window[fn]('resize', _followUploadTagField);
                document[fn]('scroll', _followUploadTagField, true);
            });
            watch(showUploadModal, (open) => { if (!open) showUploadTagMenu.value = false; });
            // Enter in the tag field: the first match, or the create dialog
            // when nothing matches the text.
            const pickFirstUploadTag = () => {
                const matches = uploadComposable.filteredAvailableTagsForUpload.value;
                if (!(uploadTagSearchFilter.value || '').trim()) return;
                if (matches.length > 0) {
                    uploadComposable.addTagToSelection(matches[0].id);
                    uploadTagSearchFilter.value = '';
                } else {
                    showUploadTagMenu.value = false;
                    createTagFromUploadSearch();
                }
            };
            // The folder select ends with a "New folder" option.
            const onUploadFolderChange = (event) => {
                const value = event.target.value;
                if (value === '__new__') {
                    event.target.value = selectedFolderId.value == null ? '' : String(selectedFolderId.value);
                    createFolderFromUpload();
                    return;
                }
                selectedFolderId.value = value === '' ? null : Number(value);
            };
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
                uploadComposable.cancelPendingUploadClose();
            };

            // Add startUpload to utils for audio composable to use
            utils.startUploadQueue = uploadComposable.startUpload;

            const generateTranslation = async (targetLanguage) => {
                if (!selectedRecording.value) return;
                if (translationJobActive.value) {
                    showToast(t('messages.translationInProgress') || 'Translation already in progress', 'fa-exclamation-circle');
                    return;
                }
                try {
                    const body = {};
                    const lang = (targetLanguage !== undefined && targetLanguage !== null && String(targetLanguage).trim() !== '')
                        ? targetLanguage
                        : (translationTargetLanguage.value || '');
                    if (lang) body.target_language = lang;
                    const res = await fetch(`/recording/${selectedRecording.value.id}/translate_transcription`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken.value },
                        body: JSON.stringify(body)
                    });
                    let data = {};
                    try { data = await res.json(); } catch (e) { data = {}; }
                    if (res.ok && data.same_language) {
                        // Nothing to translate: no job was queued.
                        showTranslationModal.value = false;
                        showToast(t('translation.alreadyInLanguage', { language: translationLanguageLabel(data.language) }));
                    } else if (res.ok) {
                        showTranslationModal.value = false;
                        viewingTranslation.value = true;
                        showToast(data.already_running
                            ? (data.message || t('messages.translationInProgress'))
                            : (data.message || t('server.apiMessage.translationQueued')));
                        // The translation job leaves recording.status alone, so
                        // translationJobActive (backed by the job-queue poll) is
                        // the only signal the UI has. Fetch it right away so the
                        // in-progress spinner replaces the empty state without
                        // waiting for the next poll tick, and keep polling so the
                        // finished job's transcript refetch lands dynamically.
                        await fetchJobQueueStatus(true);
                        startJobQueuePolling();
                    } else {
                        showToast(data.error || data.message || t('messages.translationFailed') || 'Translation failed', 'fa-exclamation-circle');
                    }
                } catch (e) {
                    console.error('Translation error:', e);
                    showToast(t('messages.translationFailed') || 'Translation failed', 'fa-exclamation-circle');
                }
            };

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
            const asrEditorComposable = useAsrEditor(state, utils, {
                saveTranscriptionContent: transcriptionComposable.saveTranscriptionContent
            });
            // The transcript's Edit button opens the segment editor through utils.
            utils.openAsrEditorModal = asrEditorComposable.openAsrEditorModal;
            const findReplaceComposable = useFindReplace(state, utils);
            // The recording chat (shared AssistantChat): it reads the
            // transcript's error state to disable its composer.
            const chatComposable = useChat(state, { ...utils, processedTranscription });
            const pwaComposable = usePWA(state, utils);
            const notificationsComposable = useNotifications(state, utils);
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
                filterFolder,
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
                startReprocessingPoll: reprocessComposable.startReprocessingPoll,
                t,
                finalizeRecordingMerge: audioComposable.finalizeRecordingMerge,
                fetchRecordingsPage: recordingsComposable.fetchRecordingsPage,
                onRecordingsDeleted: (ids) => chatThreads.drop(ids),
                searchQuery,
                showArchivedRecordings
            });

            // Bridge merge-modal openers to the audio composable so the
            // "merge this recording with an existing one" split-button action in
            // the recording view can open the merge modal (recording mode) and,
            // on confirm, finalize the session with the merge intent.
            utils.openMergeWith = bulkOperationsComposable.openMergeWith;
            utils.openMergeForRecording = bulkOperationsComposable.openMergeForRecording;

            // =========================================================================
            // TRANSCRIPT SCROLLING
            // Must be before speakers composable since it uses scrollToSegmentIndex
            // =========================================================================
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
            // The main transcript panel renders only the units near the
            // viewport (composables/transcript-view.js); a segment outside
            // that window is rendered first, by revealTranscriptSegment.
            // scrollSegmentIntoView (composables/transcript-view.js) converges
            // on the measured position of a far segment.
            utils.scrollSegmentIntoView = scrollSegmentIntoView;

            // The transcript views of the recording page, windowed.
            const transcriptViewComposable = useTranscriptView({
                processedTranscription,
                transcriptionViewMode,
                recordingId: () => (selectedRecording.value ? selectedRecording.value.id : null),
                recordingTitle: () => (selectedRecording.value ? selectedRecording.value.title || '' : ''),
                relayoutOn: [currentView, mobileTab, windowWidth, uiTheme, showTimestampsSimpleView, viewingTranslation, legendExpanded, leftColumnWidth],
                scrollSegmentIntoView,
                t,
            });
            utils.revealTranscriptSegment = transcriptViewComposable.revealSegment;

            const scrollToSegmentIndex = (index) => {
                // Both transcripts render only the rows near their viewport
                if (showSpeakerModal.value) {
                    if (utils.revealSpeakerModalSegment) utils.revealSpeakerModalSegment(index);
                    return;
                }
                transcriptViewComposable.revealSegment(index);
            };

            // Add scrollToSegmentIndex to utils for composables that need it
            utils.scrollToSegmentIndex = scrollToSegmentIndex;
            utils.resetModalAudioState = resetModalAudioState;
            // Speakers composable needs processedTranscription and scrollToSegmentIndex
            const speakersComposable = useSpeakers(state, utils, processedTranscription);
            const speakerModalComposable = useSpeakerModal(state, utils);

            // The phone layout: sheets, the list as a screen, the details line,
            // the player strip and the composer (templates/components/phone/).
            const phoneLayoutComposable = usePhoneLayout({
                isMobile: uiComposable.isMobile, isSidebarCollapsed, currentView, selectedRecording, mobileTab,
                processedTranscription, currentPlayingSegmentIndex, audioCurrentTime, audioDuration,
                seekAudioTo: uiComposable.seekAudioTo,
                editingSummary, editingNotes, editingTitle, toggleEditTitle: uiComposable.toggleEditTitle,
                filterByTag: recordingsComposable.filterByTag,
                filterTags, filterSpeakers, filterDatePreset, filterDateRange, filterAudioRemoved,
                filterNeedsTranscription, filterNeedsSummary, filterNeedsSpeakers,
                parseServerInstant,
                composerText: () => chatComposable.assistant.input,
                locale: () => window.i18n?.getIntlLocale?.(),
            });
            const { phoneListOpen } = phoneLayoutComposable;

            // Recording ids in the order the sidebar shows them, for
            // Shift-click span selection.
            const visibleRecordingIds = computed(() =>
                groupedRecordings.value.flatMap(group => group.items.map(r => r.id)));

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

            // Language options for ASR: the full Whisper language set with
            // localized labels, from the shared asr-languages.js module
            // (also feeds the tag/folder modals' Default Language selects).
            const languageOptions = computed(() => {
                const options = window.SpeakrASRLanguages
                    ? window.SpeakrASRLanguages.buildOptions((window.i18n && window.i18n.currentLocale) || 'en')
                    : [];
                return [{ value: '', label: t('form.autoDetect') }, ...options];
            });

            // --- Speaker count entry (#362) ---
            // Canonical storage is always (min, max); a single count N is
            // min == max == N. exactSpeakerEntry decides which editor renders:
            // exact-only connectors force the single field, range connectors
            // follow the user's persisted toggle.
            const exactSpeakerEntry = computed(() =>
                (connectorSupportsExactSpeakerCount.value && !connectorSupportsSpeakerCount.value)
                || speakerCountMode.value === 'single');

            const setSpeakerCountMode = async (mode) => {
                speakerCountMode.value = mode;
                try {
                    await fetch('/api/user/preferences', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken.value },
                        body: JSON.stringify({ speaker_count_mode: mode }),
                    });
                } catch (e) { /* non-fatal; the toggle still works this session */ }
            };

            const makeExactSpeakers = (minRef, maxRef) => computed({
                get: () => {
                    const mn = minRef.value, mx = maxRef.value;
                    return (mn && mn === mx) ? mn : (mx || mn || '');
                },
                set: (v) => {
                    const n = v === '' || v === null ? '' : Number(v);
                    minRef.value = n;
                    maxRef.value = n;
                },
            });
            const uploadSpeakersExact = makeExactSpeakers(uploadMinSpeakers, uploadMaxSpeakers);
            const asrSpeakersExact = makeExactSpeakers(asrMinSpeakers, asrMaxSpeakers);
            const reprocessSpeakersExact = computed({
                get: () => {
                    const mn = asrReprocessOptions.min_speakers, mx = asrReprocessOptions.max_speakers;
                    return (mn && mn === mx) ? mn : (mx || mn || '');
                },
                set: (v) => {
                    const n = v === '' || v === null ? null : Number(v);
                    asrReprocessOptions.min_speakers = n;
                    asrReprocessOptions.max_speakers = n;
                },
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
                    if (recording.translation_duration_seconds) {
                        tooltipParts.push(`Translation: ${formatProcessingDuration(recording.translation_duration_seconds)}`);
                    } else if (recording.transcription_translation && recording.translation_duration_seconds === null) {
                        // NULL duration alongside an existing translation means
                        // the source already matched the target and the text was
                        // copied without an LLM call.
                        tooltipParts.push('Translation: copied');
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

            // A queued recording waits for its conversion or its
            // transcription (#543). Only the viewer's own jobs are known, so
            // other recordings keep the plain status.
            const formatRecordingStatus = (recording) => {
                const job = activeJobs.value.find(j => j.recording_id === recording.id
                    && !['translate_transcription', 'index_chunks'].includes(j.job_type));
                if (job && job.job_type === 'convert') {
                    return job.job_status === 'processing' ? t('status.converting') : t('status.queuedConversion');
                }
                if (job && job.queue_type === 'transcription' && job.job_status === 'queued') {
                    return t('status.queuedTranscription');
                }
                return formatStatus(recording.status);
            };
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

            // Added to Date.now() before an elapsed time is computed from a
            // job's started_at (queue-data.js elapsedSeconds).
            const jobQueueClockOffset = ref(0);

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
                        // Server clock minus this browser's, for elapsed times.
                        jobQueueClockOffset.value = clockOffsetMs(data.server_time);
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


            // Track completed recording IDs to detect new completions
            const completedRecordingIds = new Set();

            // Watch allJobs for completed/failed transitions - update local recordings state
            watch(allJobs, async (jobs) => {
                let reconcileNeeded = false;
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
                    // A 'merge' job's completion means the concat finished: the
                    // merged recording now exists and (with delete_originals) the
                    // source recordings are gone, but a transcribe still follows.
                    // Reconcile the sidebar once so the merged recording appears
                    // and deleted sources drop — WITHOUT marking the recording
                    // done, so its transcribe completion still moves it to
                    // COMPLETED below.
                    if (job.job_type === 'merge' && job.job_status === 'completed') {
                        if (!completedRecordingIds.has(`merge_${job.id}`)) {
                            completedRecordingIds.add(`merge_${job.id}`);
                            reconcileNeeded = true;
                        }
                        continue;
                    }
                    if (job.job_status === 'completed' && !completedRecordingIds.has(`${job.job_type}_${job.id}`)) {
                        completedRecordingIds.add(`${job.job_type}_${job.id}`);
                        try {
                            const fullResponse = await fetch(`/api/recordings/${job.recording_id}`);
                            if (fullResponse.ok) {
                                const data = await fullResponse.json();
                                const idx = recordings.value.findIndex(r => r.id === job.recording_id);
                                if (idx !== -1) {
                                    recordings.value[idx] = data;
                                } else {
                                    // Completed recording we don't have locally —
                                    // e.g. a merge result, an auto-processed file,
                                    // or one created in another tab. Reconcile.
                                    reconcileNeeded = true;
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
                    } else if (job.job_status === 'failed' && !completedRecordingIds.has(`fail_${job.id}`)) {
                        completedRecordingIds.add(`fail_${job.id}`);
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

                // A merge created a new recording and/or deleted its sources that
                // our local list doesn't reflect. Reload page 1 (respecting the
                // current search) so the merged recording appears and the deleted
                // sources — including the recorded clip — drop out, without a
                // manual refresh. If the open recording was one of the deleted
                // sources, clear the stale detail view.
                if (reconcileNeeded) {
                    try {
                        await recordingsComposable.loadRecordings(1, false, searchQuery.value || '');
                        if (selectedRecording.value && !recordings.value.some(r => r.id === selectedRecording.value.id)) {
                            selectedRecording.value = null;
                        }
                    } catch (e) {
                        console.error('Reconcile after merge failed:', e);
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
                        fetchJobQueueStatus(true);
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
                        await fetchJobQueueStatus(true);
                    } else {
                        const data = await response.json();
                        showToast(data.error || safeT('messages.failedToDeleteJob'), 'error');
                    }
                } catch (error) {
                    console.error('Error deleting job:', error);
                }
            };

            // Reload one recording after its job was canceled: the server
            // settled it (completed, put back as it was, or canceled).
            const refreshRecordingAfterCancel = async (recordingId) => {
                if (!recordingId) return;
                try {
                    const response = await fetch(`/api/recordings/${recordingId}`);
                    if (!response.ok) return;
                    const data = await response.json();
                    const idx = recordings.value.findIndex(r => r.id === recordingId);
                    if (idx !== -1) recordings.value[idx] = data;
                    if (selectedRecording.value?.id === recordingId) selectedRecording.value = data;
                } catch (error) {
                    console.error(`Error reloading recording ${recordingId} after a cancel:`, error);
                }
            };

            // Running jobs whose cancel was accepted: job id -> recording id.
            // The worker stops at its next safe point; the row then leaves
            // the queue answer and the recording is reloaded.
            const pendingCancels = new Map();
            watch(allJobs, (jobs) => {
                for (const recordingId of settledCancels(pendingCancels, jobs)) {
                    refreshRecordingAfterCancel(recordingId);
                }
            });

            // Cancel a waiting or running job. Resolves to
            // { ok, canceled, cancelRequested }: canceled for a job that was
            // removed at once, cancelRequested for a running job that stops
            // at its next safe point (its row stays, marked, until then).
            const cancelJob = async (jobId, recordingId = null) => {
                try {
                    const response = await fetch(`/api/recordings/jobs/${jobId}`, { method: 'DELETE' });
                    const data = await response.json().catch(() => ({}));
                    const result = cancelResult(response.status, data);
                    if (result.cancelRequested) pendingCancels.set(jobId, recordingId);
                    if (!result.ok) {
                        showToast(data.error || safeT('messages.failedToDeleteJob'), 'error');
                    }
                    await fetchJobQueueStatus(true);
                    if (result.canceled) await refreshRecordingAfterCancel(recordingId);
                    return result;
                } catch (error) {
                    console.error('Error canceling job:', error);
                    return { ok: false, canceled: false, cancelRequested: false };
                }
            };

            // Cancel the job of a queue item that waits or runs.
            const cancelProgressItem = async (item) => {
                if (!item || !item.jobId || !item.canCancel) return { ok: false, canceled: false, cancelRequested: false };
                return cancelJob(item.jobId, item.recordingId);
            };

            // Clear all completed jobs
            const clearCompletedJobs = async () => {
                try {
                    const response = await fetch('/api/recordings/jobs/clear-completed', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    if (response.ok) {
                        // Finished uploads are rows too; failed ones have
                        // their own button (clearFailedJobs)
                        uploadQueue.value = uploadQueue.value.filter(item =>
                            !['completed', 'COMPLETED'].includes(item.status)
                        );
                        // Force fetch to update the job list (bypass debounce)
                        await fetchJobQueueStatus(true);
                    }
                } catch (error) {
                    console.error('Error clearing completed jobs:', error);
                }
            };

            // Clear all failed jobs in one step. The rows leave the queue;
            // the recordings stay in the library as failed.
            const clearFailedJobs = async () => {
                try {
                    const response = await fetch('/api/recordings/jobs/clear-failed', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    if (response.ok) {
                        // Uploads that failed before a job existed are rows too.
                        uploadQueue.value = uploadQueue.value.filter(item =>
                            !['failed', 'FAILED'].includes(item.status)
                        );
                        await fetchJobQueueStatus(true);
                        return true;
                    }
                } catch (error) {
                    console.error('Error clearing failed jobs:', error);
                }
                return false;
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
                    // A translation job shares the summary workers and runs
                    // beside the recording's other jobs, so it gets its own
                    // item, labelled with its chunk progress.
                    if (job.job_type === 'translate_transcription') {
                        const key = `tr_${job.recording_id}`;
                        const status = job.job_status === 'processing' ? 'translating' : job.job_status;
                        if (items.has(key) && !['queued', 'translating'].includes(status)) continue;
                        const percent = translationProgressPercent(job);
                        items.set(key, {
                            // Stage code, real progress, times and actions
                            // (queue-data.js)
                            ...queueFieldsFromJob(job),
                            id: key,
                            recordingId: job.recording_id,
                            jobId: job.id,
                            jobType: job.job_type,
                            isNewUpload: !!job.is_new_upload,
                            clientId: null,
                            title: job.recording_title || t('common.untitled'),
                            status,
                            stage: null,
                            progress: percent,
                            errorMessage: job.error_message,
                            friendlyError: job.error_message ? parseUnformattedError(job.error_message) : null,
                            completedAt: job.completed_at,
                            source: 'job'
                        });
                        continue;
                    }
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
                    // A merge job (joining uploaded files, or merging recordings)
                    // runs in the transcription queue but is not transcribing yet.
                    const isJoining = job.job_type === 'merge' && unifiedStatus === 'transcribing';
                    // A convert job prepares an upload's audio before it is
                    // queued for transcription (#543).
                    const isConverting = job.job_type === 'convert' && unifiedStatus === 'transcribing';
                    const stage = isJoining ? 'joining' : (isConverting ? 'converting' : null);
                    if (!existing || ['queued', 'transcribing', 'summarizing'].includes(unifiedStatus)) {
                        const queueFields = queueFieldsFromJob(job);
                        items.set(key, {
                            ...queueFields,
                            id: key,
                            recordingId: job.recording_id,
                            jobId: job.id,
                            jobType: job.job_type,
                            isNewUpload: !!job.is_new_upload,
                            clientId: null,
                            title: job.recording_title || t('common.untitled'),
                            status: unifiedStatus,
                            stage,
                            // A percent only where the server holds a real
                            // number (chunks, a measured fraction); null
                            // means a moving bar with the elapsed time.
                            progress: queueFields.progress,
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
                                existing.progressInfo = null;
                                existing.serverProcessing = !!upload.serverProcessing;
                                existing.serverProcessingAt = upload.serverProcessingAt || null;
                                existing.canCancel = false;
                                existing.title = upload.displayName || upload.file?.name || existing.title;
                            }
                            continue;
                        }
                    }

                    // Determine unified status from upload status (per-item progress)
                    let unifiedStatus = 'ready';
                    let progressVal = upload.progress || 0;
                    let progressMsg = t('progressQueue.waitingToUpload');

                    if (upload.status === 'uploading') {
                        // With serverProcessing the bytes are sent and the
                        // server is checking and converting the file before
                        // it answers (the row says so, queueRowView).
                        unifiedStatus = 'uploading';
                        progressMsg = '';
                    } else if (upload.status === 'pending' && upload.joinWaiting) {
                        unifiedStatus = 'queued';
                        progressVal = 100;
                        const waitingFor = upload.joinWaiting.total - upload.joinWaiting.received;
                        progressMsg = waitingFor > 0
                            ? t(waitingFor === 1 ? 'progressQueue.waitingForJoin' : 'progressQueue.waitingForJoinPlural', { count: waitingFor })
                            : t('progressQueue.joiningMessage');
                    } else if (upload.status === 'pending') {
                        unifiedStatus = 'queued';
                        progressVal = 100;
                        progressMsg = t('progressQueue.waitingForProcessing');
                    } else if (upload.status === 'completed' || upload.status === 'COMPLETED') {
                        unifiedStatus = 'completed';
                        progressMsg = t('progressQueue.done');
                    } else if (upload.status === 'failed' || upload.status === 'FAILED') {
                        unifiedStatus = 'upload_failed';
                        progressMsg = upload.error || t('progressQueue.uploadFailed');
                    } else if (upload.status === 'ready') {
                        unifiedStatus = 'ready';
                        progressMsg = t('progressQueue.waitingToUpload');
                    } else if (upload.status === 'queued') {
                        unifiedStatus = 'ready';
                        progressMsg = t('progressQueue.waitingToUpload');
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
                        title: upload.displayName || upload.file?.name || t('progressQueue.unknownFile'),
                        status: unifiedStatus,
                        progress: progressVal,
                        progressMessage: progressMsg,
                        serverProcessing: !!upload.serverProcessing,
                        serverProcessingAt: upload.serverProcessingAt || null,
                        // A file that waits is removed, one in flight is
                        // aborted; the upload of a recording made in the app
                        // is the only copy and is not canceled.
                        canCancelUpload: canCancelUpload(upload),
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
                    'translating': 3,
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

            // ============================================
            // THE QUEUE ON THE PAGE
            // An indicator line and a panel (templates/components/queue/,
            // static/css/queue.css). The groups, the counts and the wording
            // of a row come from utils/queue-view.js.
            // ============================================

            // What one row shows: stage in words, a real number or a moving
            // bar with the elapsed time, and what can be done with it.
            const queueRowView = (item, group) => {
                const label = stageLabel(item);
                const fromUpload = !item.jobId;
                const progress = rowProgress(item, parseQueueInstant);
                // Times of the server are shown against the clock of this browser
                const toLocal = (ms) => (ms === null || ms === undefined ? null
                    : (item.jobId ? ms - jobQueueClockOffset.value : ms));
                let number = '';
                if (progress.kind === 'percent') number = formatPercent(progress.percent);
                else if (progress.kind === 'count') number = t('progressQueue.partsOf', { done: progress.done, total: progress.total });
                let stage = t(label.key, label.params || undefined);
                let detail = label.detailKey ? t(label.detailKey) : '';
                if (group === 'waiting' && fromUpload) stage = item.progressMessage || stage;
                if (item.status === 'uploading' && item.serverProcessing) detail = t('progressQueue.detail.onServer');
                const error = group !== 'failed' ? null : item.friendlyError
                    // A cause that is not recognized is shown in the words of the server (a used-up
                    // budget, for example); the advice to reprocess says nothing about it
                    ? { title: item.friendlyError.title,
                        text: (item.friendlyError.isKnown === false
                            ? item.friendlyError.message || item.friendlyError.guidance
                            : item.friendlyError.guidance || item.friendlyError.message) || '' }
                    : { title: item.status === 'upload_failed' ? t('progressQueue.uploadFailed') : t('progressQueue.failed'),
                        text: item.errorMessage || '' };
                const finished = group === 'done' ? parseQueueInstant(item.finishedAt || item.completedAt) : null;
                return {
                    mark: rowMark(item),
                    stage,
                    detail,
                    progress,
                    number,
                    since: progress.kind === 'moving' ? toLocal(progress.since) : null,
                    finished: toLocal(finished),
                    error,
                    canOpen: !!item.recordingId,
                    canCancel: fromUpload ? !!item.canCancelUpload : !!item.canCancel,
                    canRetry: group === 'failed' && !!item.jobId,
                    canRemove: group === 'failed' || group === 'done',
                };
            };

            const QUEUE_GROUP_TITLES = {
                active: 'progressQueue.groups.active',
                waiting: 'progressQueue.groups.waiting',
                failed: 'progressQueue.groups.failed',
                done: 'progressQueue.groups.done',
            };
            // Active, Waiting, Failed, Recently finished
            const queueGroupList = computed(() => {
                const groups = groupQueueItems(unifiedProgressItems.value);
                return Object.keys(QUEUE_GROUP_TITLES).map(name => ({
                    name,
                    title: t(QUEUE_GROUP_TITLES[name]),
                    items: groups[name].map(item => ({ ...item, view: queueRowView(item, name) })),
                }));
            });
            const queueSummary = computed(() => {
                const groups = {};
                for (const group of queueGroupList.value) groups[group.name] = group.items;
                return summarizeQueue(groups);
            });
            // "2 processing · 1 queued · 1 failed"
            const queueParts = computed(() => summaryParts(queueSummary.value).map(part => ({
                ...part,
                text: t(`progressQueue.count.${part.key}`, { count: part.count }),
            })));
            // The pill in the top bar has less room: "5 in queue · 1 failed"
            const queuePillParts = computed(() => {
                const { processing, queued } = queueSummary.value;
                if (!(processing + queued)) return queueParts.value;
                return [
                    { key: 'inQueue', count: processing + queued, tone: 'plain', text: t('progressQueue.count.inQueue', { count: processing + queued }) },
                    ...queueParts.value.filter(part => part.key === 'failed'),
                ];
            });
            const queueSummaryText = computed(() => queueParts.value.map(part => part.text).join(' · ')
                || t('progressQueue.empty'));

            // Filtered views of unified items
            const activeProgressItems = computed(() =>
                unifiedProgressItems.value.filter(item =>
                    ['uploading', 'transcribing', 'summarizing', 'translating', 'queued', 'ready'].includes(item.status)
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
                    title: j.recording_title || t('common.untitled'),
                    status: 'completed',
                    completedAt: j.completed_at
                }));
            });

            // Combined processing queue count
            const totalProcessingCount = computed(() => {
                return activeProgressItems.value.length;
            });

            // The queue has rows
            const showProcessingPopup = computed(() => {
                return unifiedProgressItems.value.length > 0;
            });

            // Where the queue is on the page (the indicator cannot be closed;
            // it is shown while the queue has rows):
            //   phone   a strip above the player strip and at the foot of the
            //           list; the panel is the sheet named 'queue'
            //   dock    at the foot of the recording list; the open panel
            //           takes its room from the list
            //   float   the list is collapsed: a pill in the top bar and the
            //           panel under it
            // In the Desk theme the button in the status bar is the indicator
            // while that bar is shown.
            const { phoneSheet, openPhoneSheet, closePhoneSheet } = phoneLayoutComposable;
            const isPhoneLayout = uiComposable.isMobile;
            const queuePlace = computed(() => (isPhoneLayout.value ? 'phone' : (isSidebarCollapsed.value ? 'float' : 'dock')));
            const queuePanelOpen = computed(() => (isPhoneLayout.value
                ? phoneSheet.value === 'queue' : !progressPopupMinimized.value));
            // An open panel stays when its last row is cleared (the empty state)
            const queuePopupShown = computed(() => showProcessingPopup.value || (!isPhoneLayout.value && queuePanelOpen.value));
            const queueStatusBar = computed(() => uiTheme.value === 'desk' && !isMobileScreen.value);
            const queueIndicatorLabel = computed(() => `${t('progressQueue.title')}: ${queueParts.value.map(part => part.text).join(', ')}`);

            const toggleQueuePanel = () => {
                if (isPhoneLayout.value) {
                    if (phoneSheet.value === 'queue') closePhoneSheet();
                    else openPhoneSheet('queue');
                    return;
                }
                progressPopupMinimized.value = !progressPopupMinimized.value;
                if (!progressPopupMinimized.value) fetchJobQueueStatus();
            };
            watch(phoneSheet, (name) => { if (name === 'queue') fetchJobQueueStatus(); });

            const openQueueRecording = async (item) => {
                if (!item || !item.recordingId) return;
                if (isPhoneLayout.value && phoneSheet.value === 'queue') closePhoneSheet();
                await recordingsComposable.selectRecordingById(item.recordingId);
            };

            // Cancel of a row that waits or runs. Only a running first
            // transcription is asked about first: it leaves a recording
            // without a transcript.
            const cancelQueueItem = async (item) => {
                if (!item || !item.view || !item.view.canCancel) return;
                if (!item.jobId) {
                    await uploadComposable.cancelUpload(item.clientId);
                    return;
                }
                if (cancelNeedsConfirm(item)) {
                    const agreed = await window.confirmDialog({
                        title: t('progressQueue.cancelConfirm.title'),
                        message: t('progressQueue.cancelConfirm.message', { title: item.title }),
                        confirmLabel: t('progressQueue.cancelConfirm.confirm'),
                        cancelLabel: t('progressQueue.cancelConfirm.keep'),
                        danger: true,
                    });
                    if (!agreed) return;
                }
                await cancelProgressItem(item);
            };

            // The state of the queue and of the sidebar as classes on
            // <body>, for the stylesheets (queue.css, phone.css and the
            // themes). A :has() on <body> would make the browser look at the
            // whole page again whenever an element is added or removed
            // anywhere.
            watch([queuePopupShown, queuePanelOpen, isSidebarCollapsed, isMobileScreen, isPhoneLayout], () => {
                const classes = document.body.classList;
                const sidebarOpen = !isSidebarCollapsed.value && !isMobileScreen.value;
                const open = queuePopupShown.value && queuePanelOpen.value && !isPhoneLayout.value;
                classes.toggle('queue-open', open);
                classes.toggle('queue-minimized', queuePopupShown.value && !open);
                classes.toggle('app-sidebar-open', sidebarOpen);
                classes.toggle('app-sidebar-closed', !sidebarOpen);
                classes.toggle('app-rail-open', !isSidebarCollapsed.value);
            }, { immediate: true });
            // The panel over the page starts under the top bar, whose height
            // differs between the themes
            watch([queuePlace, queuePanelOpen, uiTheme], () => {
                if (queuePlace.value !== 'float' || !queuePanelOpen.value) return;
                const header = document.querySelector('#app > header');
                if (header) document.documentElement.style.setProperty('--queue-float-top', `${Math.round(header.getBoundingClientRect().bottom)}px`);
            }, { flush: 'post', immediate: true });
            // A sheet covers the bottom of a phone screen: the toasts go
            // under the top bar meanwhile (styles.css). The menu of the list
            // is a drawer of the full height, with its close button at the
            // top: the toasts stay at the bottom over it.
            watch(phoneSheet, (name) => {
                document.body.classList.toggle('phone-sheet-open', !!name && name !== 'menu');
                nextTick(placeToasts);
            }, { immediate: true });

            // One clock for every elapsed time and "finished ... ago" in the
            // panel. It writes the text into the rows itself, so a second
            // that passes re-renders nothing, and it runs only while the
            // panel is open and the tab is in front.
            let queueClock = null;
            const tickQueueClock = () => {
                const now = Date.now();
                for (const el of document.querySelectorAll('[data-queue-panel] [data-queue-since]')) {
                    const since = Number(el.dataset.queueSince);
                    const text = since > 0 ? formatElapsed((now - since) / 1000) : '';
                    if (el.textContent !== text) el.textContent = text;
                }
                for (const el of document.querySelectorAll('[data-queue-panel] [data-queue-finished]')) {
                    const ago = finishedAgo(Number(el.dataset.queueFinished) || NaN, now);
                    const text = ago ? t(ago.key, { count: ago.count }) : '';
                    if (el.textContent !== text) el.textContent = text;
                }
            };
            const syncQueueClock = () => {
                const run = queuePanelOpen.value && queuePopupShown.value && document.visibilityState === 'visible';
                if (run) tickQueueClock();
                if (run && !queueClock) queueClock = setInterval(tickQueueClock, 1000);
                if (!run && queueClock) { clearInterval(queueClock); queueClock = null; }
            };
            watch([queuePanelOpen, queuePopupShown, queueGroupList], syncQueueClock, { flush: 'post' });
            document.addEventListener('visibilitychange', syncQueueClock);

            // Speaker computed properties
            const hasSpeakerNames = computed(() => {
                // Check if any speaker has a non-empty name
                return Object.values(speakerMap.value).some(speakerData =>
                    speakerData && speakerData.name && speakerData.name.trim() !== ''
                );
            });

            // Save is also valid with no names when per-segment edits are
            // staged (speaker reassignments / text edits set
            // editedTranscriptData and are persisted by the same button).
            const canSaveSpeakerEdits = computed(() => {
                return hasSpeakerNames.value || editedTranscriptData.value !== null;
            });

            // Recording disclaimer parsed as markdown
            const recordingDisclaimerHtml = computed(() => {
                if (!recordingDisclaimer.value || recordingDisclaimer.value.trim() === '') {
                    return '';
                }
                return window.renderMarkdownSafe(recordingDisclaimer.value);
            });

            // Upload disclaimer parsed as markdown
            const uploadDisclaimerHtml = computed(() => {
                if (!uploadDisclaimer.value || uploadDisclaimer.value.trim() === '') {
                    return '';
                }
                return window.renderMarkdownSafe(uploadDisclaimer.value);
            });

            // Custom banner parsed as markdown
            const customBannerHtml = computed(() => {
                if (!customBanner.value || customBanner.value.trim() === '') {
                    return '';
                }
                return window.renderMarkdownSafe(customBanner.value);
            });

            // Summary and notes of the open recording. The server's HTML is
            // used when present and sanitized again here, at the sink, like
            // every other v-html value (#695); without it (a summary saved in
            // this tab, an incognito recording) the markdown is rendered here
            // and sanitized. The raw text is never used as HTML (#439).
            const renderedField = (field) => {
                const rec = selectedRecording.value;
                if (!rec) return '';
                const serverHtml = rec[field + '_html'];
                if (serverHtml) return window.sanitizeHtml(serverHtml);
                return rec[field] ? window.renderMarkdownSafe(rec[field]) : '';
            };
            const summaryHtml = computed(() => renderedField('summary'));
            const notesHtml = computed(() => renderedField('notes'));

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

            watch(filterNeedsTranscription, () => {
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(filterNeedsSummary, () => {
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(filterNeedsSpeakers, () => {
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

            watch(includeChildFolders, () => {
                if (filterFolder.value && filterFolder.value !== 'none') {
                    recordingsComposable.loadRecordings(1, false, searchQuery.value);
                }
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

            // Keep the address bar in sync with what's on screen so links are
            // shareable/bookmarkable (#301). An open recording wins
            // (/recordings/<id>); otherwise a lone active label filter is
            // reflected as /label/<name>, so clicking a tag hands the user a
            // link to that group of recordings without them having to know the
            // URL shape. Everything else maps back to '/'. replaceState avoids
            // history spam; the path is only changed (query + hash preserved).
            // Incognito (id 'incognito') is not a number, so it maps to '/'.
            const syncAddressBar = () => {
                try {
                    const rec = selectedRecording.value;
                    const path = (rec && typeof rec.id === 'number')
                        ? `/recordings/${rec.id}`
                        : (labelPathForFilter(filterTags.value, availableTags.value) || '/');
                    if (window.location.pathname !== path) {
                        window.history.replaceState({}, '', path + window.location.search + window.location.hash);
                    }
                } catch (_) { /* best-effort */ }
            };

            watch(selectedRecording, syncAddressBar);
            watch(filterTags, syncAddressBar, { deep: true });

            watch(sortBy, (newValue) => {
                localStorage.setItem('recordingsSortBy', newValue);
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(showArchivedRecordings, (newValue, oldValue) => {
                if (newValue === oldValue) return;
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(filterAudioRemoved, (newValue, oldValue) => {
                if (newValue === oldValue) return;
                // A filter like starred or inbox, so it combines with Shared.
                recordingsComposable.loadRecordings(1, false, searchQuery.value);
            });

            watch(showSharedWithMe, (newValue, oldValue) => {
                // Prevent unnecessary reloads when being set by the other watcher
                if (newValue === oldValue) return;

                // Reload recordings when switching to/from shared view
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

            // Show the open recording's Ambrose thread. An answer still
            // arriving elsewhere keeps writing to its own thread.
            watch(() => threadKey(selectedRecording.value), () => {
                chatThreads.select(selectedRecording.value);
                nextTick(() => chatComposable.assistant.jumpToLatest());
            }, { immediate: true, flush: 'sync' });

            // Measure the mobile bottom UI (persistent audio player + bottom
            // nav) and publish it as the --mobile-bottom-inset CSS variable,
            // so the processing-queue bar can anchor just above it regardless
            // of whether the player is shown or how tall it is (audio vs
            // video). This replaces the old magic 60+56px offset that
            // overlapped the player once it grew taller.
            let _mobileInsetObserver = null;
            // The player strip, the tab bar and, on the assistant's tab, the composer
            const mobileBottomParts = () => ['[data-mobile-player]', '[data-mobile-bottom-nav]', '[data-phone-composer]']
                .map((selector) => document.querySelector(selector)).filter(Boolean);
            // Over the recording list of a phone (a screen of its own) the bar
            // sits at the bottom edge: the player and the tabs are behind the list.
            const mobileBottomHeight = (parts) =>
                (phoneListOpen.value ? 0 : parts.reduce((sum, el) => sum + el.offsetHeight, 0));
            const updateMobileBottomInset = () => {
                const parts = mobileBottomParts();
                document.documentElement.style.setProperty('--mobile-bottom-inset', mobileBottomHeight(parts) + 'px');
                // Re-observe the current elements so live height changes
                // (e.g. toggling video in the player, the composer growing
                // with its text) keep the inset in sync.
                if (typeof ResizeObserver !== 'undefined') {
                    if (!_mobileInsetObserver) {
                        _mobileInsetObserver = new ResizeObserver(() => {
                            document.documentElement.style.setProperty('--mobile-bottom-inset', mobileBottomHeight(mobileBottomParts()) + 'px');
                        });
                    }
                    _mobileInsetObserver.disconnect();
                    parts.forEach((el) => _mobileInsetObserver.observe(el));
                }
            };
            // Re-measure whenever the bottom layout can change (view switch,
            // recording change, player position, video toggle, or the bar
            // first appearing) — this also catches the player mounting /
            // unmounting, which the ResizeObserver alone can't.
            watch([currentView, () => selectedRecording.value?.id, mobileTab,
                   videoCollapsed, audioPlayerPosition, showProcessingPopup, phoneListOpen],
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
                    connectorSupportsExactSpeakerCount.value = appElement.dataset.connectorSupportsExactSpeakerCount === 'True';
                    speakerCountMode.value = appElement.dataset.speakerCountMode === 'single' ? 'single' : 'range';
                    connectorSupportsHotwords.value = appElement.dataset.connectorSupportsHotwords === 'True';
                    connectorSupportsInitialPrompt.value = appElement.dataset.connectorSupportsInitialPrompt === 'True';
                    connectorSupportsLanguage.value = appElement.dataset.connectorSupportsLanguage === 'True';
                    showTimestampsSimpleView.value = appElement.dataset.showTimestampsSimpleView === 'True';
                    editorAutosave.value = appElement.dataset.editorAutosave === 'True';
                    const pos = appElement.dataset.audioPlayerPosition;
                    audioPlayerPosition.value = (pos === 'top') ? 'top' : 'bottom';
                    currentUserName.value = appElement.dataset.currentUserName || '';
                    defaultTranslationLanguage.value = appElement.dataset.defaultTranslationLanguage || '';
                    if (!translationTargetLanguage.value) {
                        translationTargetLanguage.value = defaultTranslationLanguage.value;
                    }
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
                // Start the notification badge poll. Cheap and slow; see the
                // composable for why this is polled rather than pushed.
                notificationsComposable.startNotificationPolling();

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
                        showToast(t('toasts.shareTargetReceived') || 'Shared audio received. Transcription is queued.', 'fa-share');
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
                // Copies of another account that signed in on this browser
                // before (a session that ended without a sign-out) are
                // deleted first, with a notice: they are neither offered
                // nor uploaded to this account (#650).
                try { await uploadComposable.removeOtherAccountsCopies(); }
                catch (error) { console.warn('[App] Copies of other accounts not checked:', error); }
                try {
                    const ServerSessions = await import('./modules/db/server-recording-sessions.js');
                    const remembered = ServerSessions.getRememberedSession();
                    let serverSession = null;
                    if (remembered && remembered.session_id) {
                        try { serverSession = await ServerSessions.getSessionStatus(remembered.session_id); }
                        catch (_) { serverSession = null; }
                    }

                    // A copy whose recording waits in this browser as a failed
                    // upload is not offered: the upload of that record (below,
                    // resurfaceFailedUploads) is the one copy that goes up, and
                    // its success removes the others (#650).
                    const serverCovered = !!(serverSession && remembered && await uploadComposable.pendingUploadCovers({ sessionId: remembered.session_id }));
                    if (serverCovered) {
                        console.log('[App] The remembered server session waits as a failed upload; not offered for recovery');
                    } else if (serverSession && serverSession.status === 'recording' && serverSession.chunk_count > 0) {
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
                        if (recoverable && recoverable.chunks && recoverable.chunks.length > 0
                            && await uploadComposable.pendingUploadCovers({ startTime: recoverable.startTime })) {
                            // The whole recording waits as a failed upload: the
                            // chunks saved while it was recorded are redundant.
                            console.log('[App] The local recording waits as a failed upload; its recovery copy is removed');
                            try { await audioComposable.clearRecordingSession(); } catch (_) { /* ignore */ }
                        } else if (recoverable && recoverable.chunks && recoverable.chunks.length > 0) {
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

                // The queue as it stands: rows that failed or finished in
                // the last hour are in the indicator after a reload too
                // (polling starts only with something in progress).
                fetchJobQueueStatus(true);

                // ?open=shares|appearance: the header on the other pages opens
                // these main-app dialogs by linking here.
                let openDialogDeepLink = null;
                try {
                    const params = new URLSearchParams(window.location.search);
                    const wanted = params.get('open');
                    if (wanted === 'shares' || wanted === 'appearance') {
                        openDialogDeepLink = wanted;
                        params.delete('open');
                        const qs = params.toString();
                        window.history.replaceState({}, '', window.location.pathname + (qs ? '?' + qs : '') + window.location.hash);
                    }
                } catch (_) { /* best-effort */ }

                // Deep link to a specific recording: /recordings/<id> (#301).
                // Captured before load; selected once recordings are in.
                let deepLinkRecordingId = null;
                // Optional ?t=<seconds> seek (Inquire citations link to the
                // cited moment): after the recording opens, seek the main
                // player there and start playback.
                let deepLinkSeekSeconds = null;
                try {
                    const m = window.location.pathname.match(/^\/recordings\/(\d+)\/?$/);
                    if (m) deepLinkRecordingId = m[1];
                    const t = new URLSearchParams(window.location.search).get('t');
                    if (m && t !== null && /^\d+(\.\d+)?$/.test(t)) deepLinkSeekSeconds = parseFloat(t);
                } catch (_) { /* best-effort */ }

                const applyDeepLinkSeek = (seconds) => {
                    const startedAt = Date.now();
                    const attempt = () => {
                        // The detail view's player is the media element whose
                        // source is the recording's /audio/ stream.
                        const media = [...document.querySelectorAll('audio, video')]
                            .find(el => ((el.currentSrc || el.src || '').includes('/audio/')));
                        if (media && media.readyState >= 1) {
                            const target = isFinite(media.duration)
                                ? Math.min(seconds, Math.max(0, media.duration - 1))
                                : seconds;
                            media.currentTime = target;
                            // Best-effort autoplay; stays paused at the right
                            // spot if the browser blocks it.
                            media.play().catch(() => {});
                            return;
                        }
                        if (Date.now() - startedAt < 15000) setTimeout(attempt, 400);
                    };
                    setTimeout(attempt, 400);
                };

                // Deep link to a label: /label/<name>. Unlike the recording
                // deep link this has to be resolved BEFORE the list loads —
                // the name only becomes tag ids once the tags are in, and the
                // filter has to be in place for the very first page fetch so
                // the user never sees the unfiltered list flash past.
                const deepLinkLabelName = parseLabelPath(window.location.pathname);

                let labelDeepLinkResolved = false;
                if (deepLinkLabelName) {
                    labelDeepLinkPending.value = true;
                    await recordingsComposable.loadTags();
                    labelDeepLinkResolved = recordingsComposable.filterByLabelName(deepLinkLabelName);
                    if (labelDeepLinkResolved) {
                        // Applying the filter also wakes the filterTags ->
                        // searchQuery watchers, which queue a debounced
                        // re-search. Let them run, then drop the timer: the
                        // load below already carries the filter, so a second
                        // identical fetch 300ms later is pure waste.
                        await nextTick();
                        clearTimeout(searchDebounceTimer.value);
                    } else {
                        labelDeepLinkPending.value = false;
                    }
                }

                // Load initial data
                await Promise.all([
                    recordingsComposable.loadRecordings(1, false, searchQuery.value),
                    ...(deepLinkLabelName ? [] : [recordingsComposable.loadTags()]),
                    recordingsComposable.loadFolders(),
                    recordingsComposable.loadSpeakers(),
                    loadTokenBudget()
                ]);

                // The viewer has no label by that name (wrong case is fine,
                // a typo or someone else's private tag is not). Say so rather
                // than let an unfiltered list masquerade as the label's
                // contents, and drop the dead path from the address bar.
                if (deepLinkLabelName && !labelDeepLinkResolved) {
                    showToast(t('tags.noLabelNamed', { name: deepLinkLabelName }), 'fa-tag', 4000, 'error');
                    syncAddressBar();
                }

                // Now open the upload modal. The pending flag (set above)
                // prevents the auto-selected recording's selectRecording() from
                // closing it once its fetch resolves.
                if (openDialogDeepLink === 'shares') {
                    sharingComposable.openSharesList();
                } else if (openDialogDeepLink === 'appearance') {
                    uiComposable.openColorSchemeModal();
                }
                if (wantUploadDeepLink) {
                    await nextTick();
                    uiComposable.switchToUploadView();
                } else if (deepLinkRecordingId) {
                    // Honour the /recordings/<id> deep link now that data is loaded.
                    await recordingsComposable.selectRecordingById(deepLinkRecordingId);
                    if (deepLinkSeekSeconds !== null) {
                        applyDeepLinkSeek(deepLinkSeekSeconds);
                    }
                }

                // Auto-retry any uploads that previously failed and were saved to
                // IndexedDB (#313): once on load, and again whenever the browser
                // reports it's back online. Works in every browser (no Background
                // Sync) and resubmits through the normal CSRF-correct upload path.
                uploadComposable.resurfaceFailedUploads();
                window.addEventListener('online', () => uploadComposable.resurfaceFailedUploads());

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
                        enableAudioRemovedFilter.value = config.enable_audio_removed_filter === true;
                        showUsernamesInUI.value = config.show_usernames_in_ui === true;
                        userSearchScope.value = resolveSearchScope(config.user_search_scope, showUsernamesInUI.value);
                        enableIncognitoMode.value = config.enable_incognito_mode === true;
                        foldersEnabled.value = config.enable_folders === true;
                        // What's new after an update: once per account. Every
                        // release with notes since the one the account last saw
                        // is listed, newest first (utils/whats-new.js).
                        const wn = config.whats_new;
                        if (wn && wn.enabled && wn.release && wn.seen !== wn.release && window.i18n?.items) {
                            const available = window.i18n.keys ? window.i18n.keys('whatsNew.releases') : [wn.release];
                            const releases = releasesToShow(available, wn.release, wn.seen)
                                .map((key) => ({ key, version: releaseVersion(key),
                                                 items: window.i18n.items('whatsNew.releases.' + key) }))
                                .filter((release) => release.items.length);
                            if (releases.length) {
                                whatsNew.value = { show: true, releases, items: releases[0].items,
                                    version: String(wn.version || '').replace(/^v/, '').replace(/-.*$/, '') };
                            }
                        }
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

                // The account and language for the offline recorder page,
                // and its files in the service worker's cache (#650).
                try {
                    const { rememberForOfflineRecorder } = await import('./modules/db/offline-recorder-context.js');
                    const appEl = document.getElementById('app');
                    const data = (appEl && appEl.dataset) || {};
                    const MB = 1024 * 1024;
                    rememberForOfflineRecorder({
                        userId: data.currentUserId || '',
                        locale: (window.i18n && window.i18n.currentLocale) || 'en',
                        incognitoDefault: !!(enableIncognitoMode.value && incognitoMode.value),
                        // REMEMBER_COOKIE_DAYS: the note lasts as long as a remembered sign-in.
                        days: parseFloat(data.rememberCookieDays) || 30,
                        // The limits of the in-app recorder and of the server's upload.
                        maxSeconds: (parseFloat(data.recordingMaxHours) || 8) * 3600,
                        maxRecorderBytes: (maxRecordingMB.value || 0) * MB,
                        maxUploadBytes: (maxFileSizeMB.value || 0) * MB,
                    });
                } catch (error) {
                    console.warn('[App] Offline recorder not prepared:', error);
                }

                // Initialize UI settings from localStorage
                uiComposable.initializeUI();

                // Restore the incognito recording after a reload; sessionStorage
                // keeps it until the tab closes (#528). Only if the feature is enabled.
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
                    showToast(safeT('messages.recordingRecovered') || safeT('toasts.recordingFinalized') || 'Recording uploaded', 'success');
                    // Surface it live in the sidebar + processing-queue panel.
                    try { await utils.onServerRecordingQueued?.(); } catch (_) { /* non-fatal */ }
                } catch (e) {
                    console.warn('[App] Could not finalize recovered server session:', e);
                    setGlobalError(safeT('errors.recoverSessionFailed', { error: e.message }));
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
                    setGlobalError(safeT('errors.resumeRecordingFailed', { error: e.message }));
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
            // KEYBOARD SHORTCUTS (composables/shortcuts.js)
            // =========================================================================
            const shortcutsComposable = useAppShortcuts({
                t, currentView, selectedRecording, recordings, visibleRecordingIds, selectedTab, isSidebarCollapsed,
                showAdvancedFilters, selectionMode, chatPanelState,
                queuePopupShown, queuePanelOpen, videoFullscreen, hasRecordingStats, processedTranscription,
                viewingTranslation, editingSummary, editingNotes, playbackRate, playbackSpeeds, canDeleteRecordings,
                transcriptFind: transcriptViewComposable.transcriptFind,
                menus: [isUserMenuOpen, showHeaderFolderMenu, showHeaderReprocessMenu, showHeaderMoreMenu, showSortOptions,
                    showDownloadMenu, showSummaryDownloadMenu, showSpeedMenu],
                selectRecording: recordingsComposable.selectRecording,
                toggleHighlight: recordingsComposable.toggleHighlight,
                toggleInbox: recordingsComposable.toggleInbox,
                toggleArchive: recordingsComposable.toggleArchive,
                confirmDelete: modalsComposable.confirmDelete,
                switchToUploadView: uiComposable.switchToUploadView,
                toggleSidebar: uiComposable.toggleSidebar,
                toggleAudioPlayback: uiComposable.toggleAudioPlayback,
                skipAudio: phoneLayoutComposable.skipAudio,
                setPlaybackRate: uiComposable.setPlaybackRate,
                copyTranscription: uiComposable.copyTranscription,
                copySummary: uiComposable.copySummary,
                copyNotes: uiComposable.copyNotes,
                toggleEditSummary: uiComposable.toggleEditSummary,
                toggleEditNotes: uiComposable.toggleEditNotes,
                openTranscriptionEditor: transcriptionComposable.openTranscriptionEditor,
                toggleTranscriptionViewMode: uiComposable.toggleTranscriptionViewMode,
                openChatPanel, collapseChatPanel, toggleQueuePanel,
                closeTranscriptFind: transcriptViewComposable.closeTranscriptFind,
            });

            // =========================================================================
            // RETURN ALL STATE AND METHODS
            // =========================================================================
            return {
                // Translation
                t, tc,
                // The assistant's name for {{name}} in its strings
                // (window.speakrAssistantName, includes/i18n_bootstrap.html)
                assistantName: window.speakrAssistantName || 'Ambrose',

                // State
                ...state,

                // Computed
                summaryHtml,
                notesHtml,
                isMobileScreen,
                isMobileDevice,
                processedTranscription,
                displayTranscription,
                translationJobActive,
                translationProgress,
                translationProgressTitle,
                groupedRecordings,
                visibleRecordingIds,
                filteredAvailableTags,
                filteredTagsForFilter,
                filteredSpeakersForFilter,
                selectedTags,
                colorSchemes,
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
                queuePopupShown,
                jobQueueDetails,
                getJobDetails,
                allJobs,
                activeJobs,
                completedJobs,
                failedJobs,
                retryJob,
                deleteJob,
                clearCompletedJobs,
                jobQueueClockOffset,
                cancelJob,
                cancelProgressItem,
                clearFailedJobs,
                recentlyCompletedBackend,
                clearAllCompleted,
                // Unified progress tracking
                unifiedProgressItems,
                activeProgressItems,
                completedProgressItems,
                failedProgressItems,
                removeProgressItem,
                retryProgressItem,
                queueGroupList,
                queueSummary,
                queueParts,
                queuePillParts,
                queueSummaryText,
                queuePlace,
                queuePanelOpen,
                queueStatusBar,
                queueIndicatorLabel,
                toggleQueuePanel,
                openQueueRecording,
                cancelQueueItem,
                hasSpeakerNames,
                canSaveSpeakerEdits,
                showDuplicatesModal,
                videoCollapsed,
                videoFullscreen,
                fullscreenControlsVisible,
                currentSubtitle,
                duplicatesModalData,
                openDuplicatesModal,
                navigateToDuplicate,
                regenEdited, regenSourceLabel, regenSaveLabel, regenStartFromGroups, regenOptionLabel,
                regenVariablesSetCount, regenCanRun, REGEN_MAX_PROMPT_LENGTH,
                recordingDisclaimerHtml,
                uploadDisclaimerHtml,
                customBannerHtml,
                acceptUploadDisclaimer,
                cancelUploadDisclaimer,

                // Utilities
                formatFileSize,
                formatPercent,
                formatNumber,
                // The interface locale for toLocale*String calls in templates (#480)
                uiLocale: () => window.i18n?.getIntlLocale?.(),
                // Markup for translated help text shown with v-html (the
                // system-audio guide): a command or path, an external link.
                helpCode: (text) => String(safeHtml`<code style="background: var(--bg-tertiary); padding: 0 4px; border-radius: 3px;">${text}</code>`),
                helpLink: (href, label) => String(safeHtml`<a href="${href}" target="_blank" rel="noopener" style="color: var(--text-accent); text-decoration: underline;">${label}</a>`),
                parseServerInstant,
                formatDisplayDate,
                formatShortDate,
                formatShortTime,
                uiTheme,
                setUiTheme,
                formatStatus,
                formatRecordingStatus,
                getStatusClass,
                formatTime,
                formatDuration,
                formatEventDateTime,
                formatDateTime: formatEventDateTime, // Alias for recovery modal
                setGlobalError,
                showToast,
                loadTokenBudget,
                getContrastTextColor,
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
                asrEditorSaveFlash,
                asrEditorSession,
                asrEditorDirty,
                asrEditorSaving,
                asrEditorRegenerateSummary,
                transcriptView: transcriptViewComposable.transcriptView,
                transcriptFind: transcriptViewComposable.transcriptFind,
                transcriptFindLabel: transcriptViewComposable.transcriptFindLabel,
                toggleTranscriptFind: transcriptViewComposable.toggleTranscriptFind,
                closeTranscriptFind: transcriptViewComposable.closeTranscriptFind,
                goToTranscriptMatch: transcriptViewComposable.goToTranscriptMatch,
                scrollToSegmentIndex,

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
                ...notificationsComposable,
                ...modalsComposable,
                ...sharingComposable,
                ...reprocessComposable,
                ...transcriptionComposable,
                ...asrEditorComposable,
                ...findReplaceComposable,
                ...speakersComposable,
                ...speakerModalComposable,
                ...chatComposable,
                ...tagsComposable,
                ...foldersComposable,
                ...pwaComposable,
                ...bulkSelectionComposable,
                ...bulkOperationsComposable,
                ...phoneLayoutComposable,
                ...shortcutsComposable,

                // Inline tag/folder creation (upload dialog)
                createTagFromUploadSearch,
                createFolderFromUpload,
                uploadDialogTab, recordSource, showUploadTagMenu,
                dateRangeActive, dateRangeInvalid,
                pickFirstUploadTag, onUploadFolderChange,
                uploadTagMenuStyle, openUploadTagMenu, closeUploadTagMenu, pickUploadTag,

                // Translation
                generateTranslation,

                // Speaker count entry (#362)
                exactSpeakerEntry, setSpeakerCountMode,
                uploadSpeakersExact, asrSpeakersExact, reprocessSpeakersExact
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

    // The transcript pane of the segment editor (templates/components/asr-editor-pane.html).
    app.component('asr-editor-pane', createAsrEditorPane());
    // Its timeline (templates/components/asr-editor-timeline.html).
    app.component('asr-editor-timeline', createAsrEditorTimeline());

    app.mount('#app');
});
