const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue

// Wait for the DOM to be fully loaded before mounting the Vue app
document.addEventListener('DOMContentLoaded', async () => {
    // Initialize i18n before creating Vue app
    if (window.i18n) {
        const appElement = document.getElementById('app');
        const userLang = appElement?.dataset.userLanguage || localStorage.getItem('preferredLanguage') || 'en';
        await window.i18n.init(userLang);
        console.log('i18n initialized with language:', userLang);
    }

    // CSRF Token Integration with Vue.js
    const csrfToken = ref(document.querySelector('meta[name="csrf-token"]')?.getAttribute('content'));

    // Register Service Worker
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/static/sw.js')
                .then(registration => {
                    console.log('ServiceWorker registration successful with scope: ', registration.scope);
                })
                .catch(error => {
                    console.log('ServiceWorker registration failed: ', error);
                });
        });
    }

    // Create a safe t function that's always available
    const safeT = (key, params = {}) => {
        if (!window.i18n || !window.i18n.t) {
            return key; // Return key as fallback without warning during initial render
        }
        return window.i18n.t(key, params);
    };
    
    const app = createApp({
        setup() {
            // --- Core State ---
            const currentView = ref('upload'); // 'upload' or 'recording'
            const dragover = ref(false);
            const recordings = ref([]);
            const selectedRecording = ref(null);
            const selectedTab = ref('summary'); // 'summary' or 'notes'
            const searchQuery = ref('');
            const isLoadingRecordings = ref(true);
            const globalError = ref(null);
            
            // Advanced filter state
            const showAdvancedFilters = ref(false);
            const filterTags = ref([]); // Selected tag IDs for filtering
            const filterDateRange = ref({ start: '', end: '' });
            const filterDatePreset = ref(''); // 'today', 'yesterday', 'week', 'month', etc.
            const filterTextQuery = ref('');
            
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
            const sortBy = ref('created_at'); // 'created_at' or 'meeting_date'
            const selectedTagFilter = ref(null); // For filtering by clicked tag

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
            const maxFileSizeMB = ref(250); // Default value, will be updated from API
            const chunkingEnabled = ref(true); // Default value, will be updated from API
            const chunkingMode = ref('size'); // 'size' or 'duration', will be updated from API
            const chunkingLimit = ref(20); // Value in MB or seconds, will be updated from API
            const chunkingLimitDisplay = ref('20MB'); // Human readable display, will be updated from API
            const recordingDisclaimer = ref(''); // Recording disclaimer text from admin settings
            const showRecordingDisclaimerModal = ref(false); // Controls disclaimer modal visibility
            const pendingRecordingMode = ref(null); // Stores the recording mode while showing disclaimer

            // --- Audio Recording State ---
            const isRecording = ref(false);
            const mediaRecorder = ref(null);
            const audioChunks = ref([]);
            const audioBlobURL = ref(null);
            const recordingTime = ref(0);
            const recordingInterval = ref(null);
            const canRecordAudio = ref(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
            const canRecordSystemAudio = ref(false);
            const systemAudioSupported = ref(false);
            const systemAudioError = ref('');
            const recordingNotes = ref('');
            const showSystemAudioHelp = ref(false);
            // ASR options for recording view
            const asrLanguage = ref('');  // Empty string for auto-detect
            const asrMinSpeakers = ref('');  // Empty string for auto-detect
            const asrMaxSpeakers = ref('');  // Empty string for auto-detect
            const audioContext = ref(null);
            const analyser = ref(null);
            const micAnalyser = ref(null);
            const systemAnalyser = ref(null);
            const visualizer = ref(null);
            const micVisualizer = ref(null);
            const systemVisualizer = ref(null);
            const animationFrameId = ref(null);
            const recordingMode = ref('microphone'); // 'microphone', 'system', or 'both'
            const activeStreams = ref([]); // Track active streams for cleanup
            
            // --- Recording Size Monitoring ---
            const estimatedFileSize = ref(0);
            const fileSizeWarningShown = ref(false);
            const recordingQuality = ref('optimized'); // 'optimized', 'standard', 'high'
            const actualBitrate = ref(0);
            const maxRecordingMB = ref(200); // Maximum recording size before auto-stop
            const sizeCheckInterval = ref(null);
            
            // Advanced Options for ASR
            const showAdvancedOptions = ref(false);
            const uploadLanguage = ref('');  // Empty string for auto-detect
            const uploadMinSpeakers = ref('');  // Empty string for auto-detect
            const uploadMaxSpeakers = ref('');  // Empty string for auto-detect

            // Tag Selection
            const availableTags = ref([]);
            const selectedTagIds = ref([]); // Changed to array for multiple selection
            const uploadTagSearchFilter = ref(''); // For filtering tags in upload view
            const selectedTags = computed(() => {
                return selectedTagIds.value.map(tagId => 
                    availableTags.value.find(tag => tag.id == tagId)
                ).filter(Boolean); // Filter out undefined tags
            });
            
            // Computed property for filtered available tags in upload view
            const filteredAvailableTagsForUpload = computed(() => {
                const availableForSelection = availableTags.value.filter(tag => !selectedTagIds.value.includes(tag.id));
                if (!uploadTagSearchFilter.value) return availableForSelection;
                
                const filter = uploadTagSearchFilter.value.toLowerCase();
                return availableForSelection.filter(tag => 
                    tag.name.toLowerCase().includes(filter)
                );
            });

            // --- Modal State ---
            const showEditModal = ref(false);
            const showDeleteModal = ref(false);
            const showEditTagsModal = ref(false);
            const selectedNewTagId = ref('');
            const tagSearchFilter = ref('');  // For filtering tags in the modal
            const showReprocessModal = ref(false);
            const showResetModal = ref(false);
            const showSpeakerModal = ref(false);
            const showShareModal = ref(false);
            const showSharesListModal = ref(false);
            const showTextEditorModal = ref(false);
            const showAsrEditorModal = ref(false);
            const editingRecording = ref(null);
            const editingTranscriptionContent = ref('');
            const editingSegments = ref([]);
            const availableSpeakers = ref([]);
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
            const showShareDeleteModal = ref(false);
            const recordingToDelete = ref(null);
            const recordingToReset = ref(null);
            const reprocessType = ref(null); // 'transcription' or 'summary'
            const reprocessRecording = ref(null);
            const isAutoIdentifying = ref(false);
            const asrReprocessOptions = reactive({
                language: '',
                min_speakers: null,
                max_speakers: null
            });
            const speakerMap = ref({});
            const regenerateSummaryAfterSpeakerUpdate = ref(true);
            const speakerSuggestions = ref({});
            const loadingSuggestions = ref({});
            const activeSpeakerInput = ref(null);

            // --- Inline Editing State ---
            const editingParticipants = ref(false);
            const editingMeetingDate = ref(false);
            const editingSummary = ref(false);
            const editingNotes = ref(false);
            const tempNotesContent = ref('');
            const tempSummaryContent = ref('');
            const autoSaveTimer = ref(null);
            const autoSaveDelay = 2000; // 2 seconds debounce
            
            // --- Markdown Editor State ---
            const notesMarkdownEditor = ref(null);
            const markdownEditorInstance = ref(null);
            const summaryMarkdownEditor = ref(null);
            const summaryMarkdownEditorInstance = ref(null);
            const recordingNotesEditor = ref(null);
            const recordingMarkdownEditorInstance = ref(null);

            // --- Transcription State ---
            const transcriptionViewMode = ref('simple'); // 'simple' or 'bubble'
            const legendExpanded = ref(false);
            const highlightedSpeaker = ref(null);

            // --- Chat State ---
            const showChat = ref(false);
            const chatMessages = ref([]);
            const chatInput = ref('');
            const isChatLoading = ref(false);
            const chatMessagesRef = ref(null);

            // --- Audio Player State ---
            const playerVolume = ref(1.0);

            // --- Column Resizing State ---
            const leftColumnWidth = ref(60); // 60% for left column (transcript)
            const rightColumnWidth = ref(40); // 40% for right column (summary/chat)
            const isResizing = ref(false);

            // --- App Configuration ---
            const useAsrEndpoint = ref(false);
            const currentUserName = ref('');

            // --- Computed Properties ---
            const isMobileScreen = computed(() => {
                return windowWidth.value < 1024;
            });

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

            const filteredRecordings = computed(() => {
                return recordings.value;
            });
            
            const highlightedTranscript = computed(() => {
                if (!selectedRecording.value?.transcription) return '';
                let html = selectedRecording.value.transcription;
                // Escape HTML to prevent injection, but keep it minimal
                html = html.replace(/</g, '<').replace(/>/g, '>');
    
                // 1. Replace newlines with <br> tags for proper line breaks in HTML
                html = html.replace(/\n/g, '<br>');
                
                // 2. Get speaker colors from the speakerMap if available
                const speakerColors = {};
                if (speakerMap.value) {
                    Object.keys(speakerMap.value).forEach((speaker, index) => {
                        speakerColors[speaker] = speakerMap.value[speaker].color || `speaker-color-${(index % 8) + 1}`;
                    });
                }
                
                // 3. Wrap each speaker tag in a span for styling and interaction with colors
                html = html.replace(/\[([^\]]+)\]/g, (match, speakerId) => {
                    const isHighlighted = speakerId === highlightedSpeaker.value;
                    const colorClass = speakerColors[speakerId] || '';
                    // Replace speaker ID with name if available
                    const displayName = speakerMap.value[speakerId]?.name || speakerId;
                    const displayText = `[${displayName}]`;
                    // Use a more specific and stylish class structure with color
                    return `<span class="speaker-tag ${colorClass} ${isHighlighted ? 'speaker-highlight' : ''}" data-speaker-id="${speakerId}">${displayText}</span>`;
                });
                
                return html;
            });

            const activeRecordingMetadata = computed(() => {
                if (!selectedRecording.value) return [];
                
                const recording = selectedRecording.value;
                const metadata = [];

                if (recording.created_at) {
                    metadata.push({
                        icon: 'fas fa-history',
                        text: formatDisplayDate(recording.created_at)
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
                
                // Add tags to metadata
                if (recording.tags && recording.tags.length > 0) {
                    metadata.push({
                        icon: 'fas fa-tags',
                        text: '',  // Empty text since we'll render tags specially
                        tags: recording.tags,  // Pass the tags array
                        isTagItem: true  // Flag to identify this as a tag item
                    });
                }
                
                return metadata;
            });

            const groupedRecordings = computed(() => {
                // Sort recordings based on the selected sort criteria
                const sortedRecordings = [...filteredRecordings.value].sort((a, b) => {
                    const dateA = getDateForSorting(a);
                    const dateB = getDateForSorting(b);
                    
                    // Handle null dates (put them at the end)
                    if (!dateA && !dateB) return 0;
                    if (!dateA) return 1;
                    if (!dateB) return -1;
                    
                    return dateB - dateA; // Most recent first
                });

                const groups = {
                    today: [],
                    yesterday: [],
                    thisWeek: [],
                    lastWeek: [],
                    thisMonth: [],
                    lastMonth: [],
                    older: []
                };

                sortedRecordings.forEach(recording => {
                    const date = getDateForSorting(recording);
                    if (!date) {
                        groups.older.push(recording);
                        return;
                    }

                    if (isToday(date)) {
                        groups.today.push(recording);
                    } else if (isYesterday(date)) {
                        groups.yesterday.push(recording);
                    } else if (isThisWeek(date)) {
                        groups.thisWeek.push(recording);
                    } else if (isLastWeek(date)) {
                        groups.lastWeek.push(recording);
                    } else if (isThisMonth(date)) {
                        groups.thisMonth.push(recording);
                    } else if (isLastMonth(date)) {
                        groups.lastMonth.push(recording);
                    } else {
                        groups.older.push(recording);
                    }
                });

                return [
                    { title: t('sidebar.today'), items: groups.today },
                    { title: t('sidebar.yesterday'), items: groups.yesterday },
                    { title: t('sidebar.thisWeek'), items: groups.thisWeek },
                    { title: t('sidebar.lastWeek'), items: groups.lastWeek },
                    { title: t('sidebar.thisMonth'), items: groups.thisMonth },
                    { title: t('sidebar.lastMonth'), items: groups.lastMonth },
                    { title: t('sidebar.older'), items: groups.older }
                ].filter(g => g.items.length > 0);
            });

            const totalInQueue = computed(() => uploadQueue.value.length);
            const completedInQueue = computed(() => uploadQueue.value.filter(item => item.status === 'completed' || item.status === 'failed').length);
            const finishedFilesInQueue = computed(() => uploadQueue.value.filter(item => ['completed', 'failed'].includes(item.status)));

            const clearCompletedUploads = () => {
                uploadQueue.value = uploadQueue.value.filter(item => !['completed', 'failed'].includes(item.status));
            };

            const identifiedSpeakers = computed(() => {
                // Ensure we have a valid recording and transcription
                if (!selectedRecording.value?.transcription) {
                    return [];
                }
                
                const transcription = selectedRecording.value.transcription;
                let transcriptionData;
    
                try {
                    transcriptionData = JSON.parse(transcription);
                } catch (e) {
                    transcriptionData = null;
                }
    
                // Updated to handle new simplified JSON format (array of segments)
                if (transcriptionData && Array.isArray(transcriptionData)) {
                    // JSON format - extract speakers in order of appearance
                    const speakersInOrder = [];
                    const seenSpeakers = new Set();
                    transcriptionData.forEach(segment => {
                        if (segment.speaker && String(segment.speaker).trim() && !seenSpeakers.has(segment.speaker)) {
                            seenSpeakers.add(segment.speaker);
                            speakersInOrder.push(segment.speaker);
                        }
                    });
                    return speakersInOrder; // Keep order of appearance, don't sort
                } else if (typeof transcription === 'string') {
                    // Plain text format - find speakers in order of appearance
                    const speakerRegex = /\[([^\]]+)\]:/g;
                    const speakersInOrder = [];
                    const seenSpeakers = new Set();
                    let match;
                    while ((match = speakerRegex.exec(transcription)) !== null) {
                        const speaker = match[1].trim();
                        if (speaker && !seenSpeakers.has(speaker)) {
                            seenSpeakers.add(speaker);
                            speakersInOrder.push(speaker);
                        }
                    }
                    return speakersInOrder; // Keep order of appearance, don't sort
                }
                return [];
            });

            // identifiedSpeakersInOrder is now just an alias since identifiedSpeakers already preserves order
            const identifiedSpeakersInOrder = computed(() => {
                return identifiedSpeakers.value;
            });
    
            const hasSpeakerNames = computed(() => {
                // Check if any speaker has a non-empty name
                return Object.values(speakerMap.value).some(speakerData => 
                    speakerData.name && speakerData.name.trim() !== ''
                );
            });

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

            // --- Color Scheme Management ---
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

            // --- Utility Methods ---
            const setGlobalError = (message, duration = 7000) => {
                globalError.value = message;
                if (duration > 0) {
                    setTimeout(() => { if (globalError.value === message) globalError.value = null; }, duration);
                }
            };

            const formatFileSize = (bytes) => {
                if (bytes == null || bytes === 0) return '0 Bytes';
                const k = 1024;
                const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
                if (bytes < 0) bytes = 0;
                const i = bytes === 0 ? 0 : Math.max(0, Math.floor(Math.log(bytes) / Math.log(k)));
                const size = i === 0 ? bytes : parseFloat((bytes / Math.pow(k, i)).toFixed(2));
                return size + ' ' + sizes[i];
            };

            const formatDisplayDate = (dateString) => {
                if (!dateString) return '';
                try {
                    // Try to parse the date string directly first (it might already be formatted)
                    let date = new Date(dateString);
                    
                    // If that fails or results in invalid date, try different approaches
                    if (isNaN(date.getTime())) {
                        // Try appending time if it looks like a date-only string (YYYY-MM-DD format)
                        if (/^\d{4}-\d{2}-\d{2}$/.test(dateString)) {
                            date = new Date(dateString + 'T00:00:00');
                        } else {
                            // If it's already a formatted string, just return it
                            return dateString;
                        }
                    }
                    
                    // If we still have an invalid date, return the original string
                    if (isNaN(date.getTime())) {
                        return dateString;
                    }
                    
                    return date.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
                } catch (e) {
                    console.error("Error formatting date:", e);
                    return dateString;
                }
            };

            const formatStatus = (status) => {
                if (!status || status === 'COMPLETED') return '';
                const statusMap = {
                    'PENDING': t('status.queued'),
                    'PROCESSING': t('status.processing'),
                    'TRANSCRIBING': t('status.transcribing'),
                    'SUMMARIZING': t('status.summarizing'),
                    'FAILED': t('status.failed'),
                    'UPLOADING': t('status.uploading')
                };
                return statusMap[status] || status.charAt(0).toUpperCase() + status.slice(1).toLowerCase();
            };

            const getStatusClass = (status) => {
                switch(status) {
                    case 'PENDING': return 'status-pending';
                    case 'PROCESSING': return 'status-processing';
                    case 'SUMMARIZING': return 'status-summarizing';
                    case 'COMPLETED': return '';
                    case 'FAILED': return 'status-failed';
                    default: return 'status-pending';
                }
            };

            const formatTime = (seconds) => {
                const minutes = Math.floor(seconds / 60);
                const secs = seconds % 60;
                return `${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
            };

            const formatDuration = (totalSeconds) => {
                if (totalSeconds == null || totalSeconds < 0) return 'N/A';
    
                if (totalSeconds < 1) {
                    return `${totalSeconds.toFixed(2)} seconds`;
                }
                
                totalSeconds = Math.round(totalSeconds);
    
                if (totalSeconds < 60) {
                    return `${totalSeconds} sec`;
                }
    
                const hours = Math.floor(totalSeconds / 3600);
                const minutes = Math.floor((totalSeconds % 3600) / 60);
                const seconds = totalSeconds % 60;
    
                let parts = [];
                if (hours > 0) {
                    parts.push(`${hours} hr`);
                }
                if (minutes > 0) {
                    parts.push(`${minutes} min`);
                }
                // Only show seconds if duration is less than an hour
                if (hours === 0 && seconds > 0) {
                    parts.push(`${seconds} sec`);
                }
    
                return parts.join(' ');
            };

            // --- Recording Size Monitoring Functions ---
            const updateFileSizeEstimate = () => {
                if (!isRecording.value || !actualBitrate.value) return;
                
                // Calculate estimated size based on recording time and bitrate
                const recordingTimeSeconds = recordingTime.value;
                const estimatedBits = actualBitrate.value * recordingTimeSeconds;
                const estimatedBytes = estimatedBits / 8;
                estimatedFileSize.value = estimatedBytes;
                
                // Check if we're approaching the size limit
                const sizeMB = estimatedBytes / (1024 * 1024);
                const warningThresholdMB = maxRecordingMB.value * 0.8; // 80% of max size
                
                if (sizeMB > warningThresholdMB && !fileSizeWarningShown.value) {
                    fileSizeWarningShown.value = true;
                    showToast(`Recording size is ${formatFileSize(estimatedBytes)}. Consider stopping soon to avoid auto-stop at ${maxRecordingMB.value}MB.`, 'fa-exclamation-triangle', 5000);
                }
                
                // Auto-stop if we exceed the maximum size
                if (sizeMB > maxRecordingMB.value) {
                    console.log(`Auto-stopping recording: size ${formatFileSize(estimatedBytes)} exceeds limit of ${maxRecordingMB.value}MB`);
                    stopRecording();
                    showToast(`Recording automatically stopped at ${formatFileSize(estimatedBytes)} to prevent excessive file size.`, 'fa-stop-circle', 7000);
                }
            };

            const startSizeMonitoring = () => {
                if (sizeCheckInterval.value) {
                    clearInterval(sizeCheckInterval.value);
                }
                
                // Reset size monitoring state
                estimatedFileSize.value = 0;
                fileSizeWarningShown.value = false;
                
                // Start monitoring every 5 seconds
                sizeCheckInterval.value = setInterval(updateFileSizeEstimate, 5000);
            };

            const stopSizeMonitoring = () => {
                if (sizeCheckInterval.value) {
                    clearInterval(sizeCheckInterval.value);
                    sizeCheckInterval.value = null;
                }
            };

            // --- Enhanced Date Utility Functions ---
            const getDateForSorting = (recording) => {
                const dateStr = sortBy.value === 'meeting_date' ? recording.meeting_date : recording.created_at;
                if (!dateStr) return null;
                return new Date(dateStr);
            };

            const isToday = (date) => {
                const today = new Date();
                return isSameDay(date, today);
            };

            const isYesterday = (date) => {
                const yesterday = new Date();
                yesterday.setDate(yesterday.getDate() - 1);
                return isSameDay(date, yesterday);
            };

            const isThisWeek = (date) => {
                const now = new Date();
                const startOfWeek = new Date(now);
                const day = now.getDay();
                const diff = now.getDate() - day + (day === 0 ? -6 : 1); // Monday as start of week
                startOfWeek.setDate(diff);
                startOfWeek.setHours(0, 0, 0, 0);
                
                const endOfWeek = new Date(startOfWeek);
                endOfWeek.setDate(startOfWeek.getDate() + 6);
                endOfWeek.setHours(23, 59, 59, 999);
                
                return date >= startOfWeek && date <= endOfWeek;
            };

            const isLastWeek = (date) => {
                const now = new Date();
                const startOfLastWeek = new Date(now);
                const day = now.getDay();
                const diff = now.getDate() - day + (day === 0 ? -6 : 1) - 7; // Previous Monday
                startOfLastWeek.setDate(diff);
                startOfLastWeek.setHours(0, 0, 0, 0);
                
                const endOfLastWeek = new Date(startOfLastWeek);
                endOfLastWeek.setDate(startOfLastWeek.getDate() + 6);
                endOfLastWeek.setHours(23, 59, 59, 999);
                
                return date >= startOfLastWeek && date <= endOfLastWeek;
            };

            const isThisMonth = (date) => {
                const now = new Date();
                return date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth();
            };

            const isLastMonth = (date) => {
                const now = new Date();
                const lastMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1);
                return date.getFullYear() === lastMonth.getFullYear() && date.getMonth() === lastMonth.getMonth();
            };

            const isSameDay = (date1, date2) => {
                return date1.getFullYear() === date2.getFullYear() &&
                       date1.getMonth() === date2.getMonth() &&
                       date1.getDate() === date2.getDate();
            };

            const reprocessTranscription = (recordingId) => {
                const recording = recordings.value.find(r => r.id === recordingId) || selectedRecording.value;
                confirmReprocess('transcription', recording);
            };
            
            const reprocessSummary = (recordingId) => {
                const recording = recordings.value.find(r => r.id === recordingId) || selectedRecording.value;
                confirmReprocess('summary', recording);
            };
            
            const generateSummary = async () => {
                if (!selectedRecording.value) return;
                
                try {
                    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
                    
                    const response = await fetch(`/recording/${selectedRecording.value.id}/generate_summary`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': csrfToken
                        }
                    });
                    
                    const data = await response.json();
                    
                    if (!response.ok) {
                        throw new Error(data.error || 'Failed to generate summary');
                    }
                    
                    // Update the recording status to show it's being processed
                    selectedRecording.value.status = 'SUMMARIZING';
                    
                    // Also update in recordings list if it exists
                    const recordingInList = recordings.value.find(r => r.id === selectedRecording.value.id);
                    if (recordingInList) {
                        recordingInList.status = 'SUMMARIZING';
                    }
                    
                    showToast('Summary generation started', 'success');
                    
                } catch (error) {
                    console.error('Error generating summary:', error);
                    setGlobalError(`Failed to generate summary: ${error.message}`);
                }
            };

            const resetRecordingStatus = async (recordingId) => {
                const recording = recordings.value.find(r => r.id === recordingId);
                if (!recording) return;

                try {
                    const response = await fetch(`/recording/${recording.id}/reset_status`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                    });
                    const data = await response.json();
                    if (!response.ok) {
                        throw new Error(data.error || 'Failed to reset status');
                    }
                    const index = recordings.value.findIndex(r => r.id === recording.id);
                    if (index !== -1) {
                        recordings.value[index] = data.recording;
                    }
                    if (selectedRecording.value?.id === recording.id) {
                        selectedRecording.value = data.recording;
                    }
                    showToast('Recording status has been reset.', 'fa-check-circle');
                } catch (error) {
                    setGlobalError(`Failed to reset status: ${error.message}`);
                }
            };

            const confirmReprocess = (type, recording) => {
                reprocessType.value = type;
                reprocessRecording.value = recording;
                showReprocessModal.value = true;
            };

            const openTranscriptionEditor = () => {
                if (processedTranscription.value.isJson) {
                    openAsrEditorModal();
                } else {
                    openTextEditorModal();
                }
            };

            const openTextEditorModal = () => {
                if (!selectedRecording.value) return;
                editingTranscriptionContent.value = selectedRecording.value.transcription;
                showTextEditorModal.value = true;
            };

            const closeTextEditorModal = () => {
                showTextEditorModal.value = false;
                editingTranscriptionContent.value = '';
            };

            const saveTranscription = async () => {
                if (!selectedRecording.value) return;
                await saveTranscriptionContent(editingTranscriptionContent.value);
                closeTextEditorModal();
            };

            const openAsrEditorModal = async () => {
                if (!selectedRecording.value) return;
                try {
                    const segments = JSON.parse(selectedRecording.value.transcription);
                    editingSegments.value = segments.map((s, i) => ({ 
                        ...s, 
                        id: i, 
                        showSuggestions: false, 
                        filteredSpeakers: [] 
                    }));

                    // Populate available speakers
                    const speakersInTranscript = [...new Set(segments.map(s => s.speaker))];
                    const response = await fetch('/speakers');
                    const speakersFromDb = await response.json();
                    const speakerNamesFromDb = speakersFromDb.map(s => s.name);
                    availableSpeakers.value = [...new Set([...speakersInTranscript, ...speakerNamesFromDb])].sort();
                    
                    showAsrEditorModal.value = true;
                } catch (e) {
                    console.error("Could not parse transcription as JSON for ASR editor:", e);
                    setGlobalError("This transcription is not in the correct format for the ASR editor.");
                }
            };

            const closeAsrEditorModal = () => {
                showAsrEditorModal.value = false;
                editingSegments.value = [];
                availableSpeakers.value = [];
            };

            const saveAsrTranscription = async () => {
                const contentToSave = JSON.stringify(editingSegments.value.map(({ id, showSuggestions, filteredSpeakers, ...rest }) => rest));
                await saveTranscriptionContent(contentToSave);
                closeAsrEditorModal();
            };

            const adjustTime = (index, field, amount) => {
                if (editingSegments.value[index]) {
                    editingSegments.value[index][field] = parseFloat((editingSegments.value[index][field] + amount).toFixed(3));
                }
            };

            const filterSpeakers = (index) => {
                const segment = editingSegments.value[index];
                if (segment) {
                    const query = segment.speaker.toLowerCase();
                    segment.filteredSpeakers = availableSpeakers.value.filter(s => s.toLowerCase().includes(query));
                }
            };

            const openSpeakerSuggestions = (index) => {
                if (editingSegments.value[index]) {
                    editingSegments.value[index].showSuggestions = true;
                    filterSpeakers(index);
                }
            };

            const closeSpeakerSuggestions = (index) => {
                if (editingSegments.value[index]) {
                    window.setTimeout(() => {
                        if (editingSegments.value[index]) {
                            editingSegments.value[index].showSuggestions = false;
                        }
                    }, 200); // Delay to allow click event to register
                }
            };

            const selectSpeaker = (index, speaker) => {
                if (editingSegments.value[index]) {
                    editingSegments.value[index].speaker = speaker;
                    editingSegments.value[index].showSuggestions = false;
                }
            };

            const addSegment = () => {
                const lastSegment = editingSegments.value[editingSegments.value.length - 1];
                editingSegments.value.push({
                    id: Date.now(),
                    speaker: lastSegment ? lastSegment.speaker : 'SPEAKER_00',
                    start_time: lastSegment ? lastSegment.end_time : 0,
                    end_time: lastSegment ? lastSegment.end_time + 1 : 1,
                    sentence: ''
                });
            };

            const removeSegment = (index) => {
                editingSegments.value.splice(index, 1);
            };

            const saveTranscriptionContent = async (content) => {
                if (!selectedRecording.value) return;
                try {
                    const response = await fetch(`/recording/${selectedRecording.value.id}/update_transcription`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ transcription: content })
                    });
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to update transcription');
                    const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
                    if (index !== -1) {
                        recordings.value[index] = data.recording;
                    }
                    selectedRecording.value = data.recording;
                    showToast('Transcription updated successfully!', 'fa-check-circle');
                } catch (error) {
                    console.error('Save Transcription Error:', error);
                    setGlobalError(`Failed to save transcription: ${error.message}`);
                }
            };
            
            const cancelReprocess = () => {
                showReprocessModal.value = false;
                reprocessType.value = null;
                reprocessRecording.value = null;
            };
            
            const executeReprocess = async () => {
                if (!reprocessRecording.value || !reprocessType.value) return;
                
                const recordingId = reprocessRecording.value.id;
                const type = reprocessType.value;
                
                // Close the modal first
                cancelReprocess();
                
                if (type === 'transcription') {
                    await performReprocessTranscription(
                        recordingId, 
                        asrReprocessOptions.language, 
                        asrReprocessOptions.min_speakers, 
                        asrReprocessOptions.max_speakers
                    );
                } else if (type === 'summary') {
                    await performReprocessSummary(recordingId);
                }
            };

            const performReprocessTranscription = async (recordingId, language, minSpeakers, maxSpeakers) => {
                if (!recordingId) {
                    setGlobalError('No recording ID provided for reprocessing.');
                    return;
                }
                
                try {
                    const payload = {
                        language: language,
                        min_speakers: minSpeakers,
                        max_speakers: maxSpeakers
                    };
    
                    const response = await fetch(`/recording/${recordingId}/reprocess_transcription`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to start transcription reprocessing');
                    
                    // Update the recording in the UI
                    const index = recordings.value.findIndex(r => r.id === recordingId);
                    if (index !== -1) {
                        recordings.value[index] = data.recording;
                    }
                    
                    // Update selected recording if it's the one being reprocessed
                    if (selectedRecording.value?.id === recordingId) {
                        selectedRecording.value = data.recording;
                    }
                    
                    showToast('Transcription reprocessing started', 'fa-sync-alt');
                    
                    // Show progress modal for reprocessing
                    showProgressModalForReprocessing(recordingId, 'transcription');
                    
                    // Start polling for status updates
                    startReprocessingPoll(recordingId);
                    
                } catch (error) {
                    console.error('Reprocess Transcription Error:', error);
                    setGlobalError(`Failed to start transcription reprocessing: ${error.message}`);
                }
            };
            
            const performReprocessSummary = async (recordingId) => {
                if (!recordingId) {
                    setGlobalError('No recording ID provided for reprocessing.');
                    return;
                }
                
                try {
                    const response = await fetch(`/recording/${recordingId}/reprocess_summary`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to start summary reprocessing');
                    
                    // Update the recording in the UI
                    const index = recordings.value.findIndex(r => r.id === recordingId);
                    if (index !== -1) {
                        recordings.value[index] = data.recording;
                    }
                    
                    // Update selected recording if it's the one being reprocessed
                    if (selectedRecording.value?.id === recordingId) {
                        selectedRecording.value = data.recording;
                    }
                    
                    showToast('Summary reprocessing started', 'fa-sync-alt');
                    
                    // Show progress modal for reprocessing
                    showProgressModalForReprocessing(recordingId, 'summary');
                    
                    // Start polling for status updates
                    startReprocessingPoll(recordingId);
                    
                } catch (error) {
                    console.error('Reprocess Summary Error:', error);
                    setGlobalError(`Failed to start summary reprocessing: ${error.message}`);
                }
            };
            
            // Show progress modal for reprocessing operations
            const showProgressModalForReprocessing = (recordingId, type) => {
                const recording = recordings.value.find(r => r.id === recordingId);
                if (!recording) return;
                
                // Create a mock file item for the progress modal
                const reprocessItem = {
                    file: { 
                        name: recording.title || `Recording ${recordingId}`,
                        size: recording.file_size || 0
                    },
                    status: 'pending',
                    recordingId: recordingId,
                    clientId: `reprocess-${type}-${recordingId}-${Date.now()}`,
                    error: null,
                    isReprocessing: true,
                    reprocessType: type
                };
                
                // Add to upload queue for progress tracking
                uploadQueue.value.unshift(reprocessItem);
                
                // Show progress modal
                progressPopupMinimized.value = false;
                progressPopupClosed.value = false;
                
                // Set as currently processing file
                currentlyProcessingFile.value = reprocessItem;
                processingProgress.value = 10;
                processingMessage.value = type === 'transcription' ? 'Starting transcription reprocessing...' : 'Starting summary reprocessing...';
            };

            // Polling for reprocessing status updates
            const reprocessingPolls = ref(new Map()); // Track active polls by recording ID
            
            const startReprocessingPoll = (recordingId) => {
                // Clear any existing poll for this recording
                if (reprocessingPolls.value.has(recordingId)) {
                    clearInterval(reprocessingPolls.value.get(recordingId));
                }
                
                console.log(`Starting reprocessing poll for recording ${recordingId}`);
                
                const pollInterval = setInterval(async () => {
                    try {
                        const response = await fetch(`/status/${recordingId}`);
                        if (!response.ok) {
                            console.error(`Status check failed for recording ${recordingId}`);
                            stopReprocessingPoll(recordingId);
                            return;
                        }
                        
                        const data = await response.json();
                        
                        // Update the recording in the UI
                        const index = recordings.value.findIndex(r => r.id === recordingId);
                        if (index !== -1) {
                            recordings.value[index] = data;
                        }
                        
                        // Update selected recording if it's the one being reprocessed
                        if (selectedRecording.value?.id === recordingId) {
                            selectedRecording.value = data;
                        }
                        
                        const queueItem = uploadQueue.value.find(item => item.recordingId === recordingId);

                        // Update the item's status and name for intermediate states
                        if (queueItem) {
                            queueItem.status = data.status; // e.g., 'PROCESSING', 'SUMMARIZING'
                            queueItem.file.name = data.title || data.original_filename;
                        }
                        
                        // Update progress modal if this is the currently processing item
                        if (queueItem && currentlyProcessingFile.value?.clientId === queueItem.clientId) {
                            updateReprocessingProgress(data.status, queueItem);
                        }
                        
                        // Stop polling if processing is complete
                        if (data.status === 'COMPLETED' || data.status === 'FAILED') {
                            console.log(`Reprocessing ${data.status.toLowerCase()} for recording ${recordingId}`);
                            stopReprocessingPoll(recordingId);
                            
                            // Update queue item status to final lowercase state
                            if (queueItem) {
                                queueItem.status = data.status === 'COMPLETED' ? 'completed' : 'failed';
                                if (data.status === 'FAILED') {
                                    queueItem.error = data.error_message || 'Reprocessing failed';
                                }
                            }
                            
                            // Clear current processing if this was the active item
                            if (currentlyProcessingFile.value?.recordingId === recordingId) {
                                resetCurrentFileProcessingState();
                            }
                            
                            if (data.status === 'COMPLETED') {
                                showToast('Reprocessing completed successfully', 'fa-check-circle');
                            } else {
                                showToast('Reprocessing failed', 'fa-exclamation-circle');
                            }
                        }
                        
                    } catch (error) {
                        console.error(`Error polling status for recording ${recordingId}:`, error);
                        stopReprocessingPoll(recordingId);
                    }
                }, 3000); // Poll every 3 seconds
                
                reprocessingPolls.value.set(recordingId, pollInterval);
            };
            
            const updateReprocessingProgress = (status, queueItem) => {
                switch (status) {
                    case 'PENDING':
                        processingProgress.value = 20;
                        processingMessage.value = `Waiting to start ${queueItem.reprocessType} reprocessing...`;
                        break;
                    case 'PROCESSING':
                        processingProgress.value = Math.round(Math.min(70, processingProgress.value + Math.random() * 10));
                        processingMessage.value = queueItem.reprocessType === 'transcription' 
                            ? 'Reprocessing transcription...' 
                            : 'Processing audio...';
                        break;
                    case 'SUMMARIZING':
                        processingProgress.value = Math.round(Math.min(90, processingProgress.value + Math.random() * 10));
                        processingMessage.value = queueItem.reprocessType === 'summary' 
                            ? 'Regenerating summary...' 
                            : 'Generating title and summary...';
                        break;
                    case 'COMPLETED':
                        processingProgress.value = 100;
                        processingMessage.value = 'Reprocessing completed!';
                        break;
                    case 'FAILED':
                        processingProgress.value = 100;
                        processingMessage.value = 'Reprocessing failed.';
                        break;
                    default:
                        processingProgress.value = 15;
                        processingMessage.value = 'Starting reprocessing...';
                }
            };

            const stopReprocessingPoll = (recordingId) => {
                if (reprocessingPolls.value.has(recordingId)) {
                    clearInterval(reprocessingPolls.value.get(recordingId));
                    reprocessingPolls.value.delete(recordingId);
                    console.log(`Stopped reprocessing poll for recording ${recordingId}`);
                }
            };
    
            const confirmReset = (recording) => {
                recordingToReset.value = recording;
                showResetModal.value = true;
            };
    
            const cancelReset = () => {
                showResetModal.value = false;
                recordingToReset.value = null;
            };
    
            const executeReset = async () => {
                if (!recordingToReset.value) return;
                const recordingId = recordingToReset.value.id;
                
                // Close the modal first
                cancelReset();
    
                try {
                    const response = await fetch(`/recording/${recordingId}/reset_status`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to reset status');
                    
                    // Update the recording in the UI
                    const index = recordings.value.findIndex(r => r.id === recordingId);
                    if (index !== -1) {
                        recordings.value[index] = data.recording;
                    }
                    
                    // Update selected recording if it's the one being reset
                    if (selectedRecording.value?.id === recordingId) {
                        selectedRecording.value = data.recording;
                    }
                    
                    showToast('Recording status has been reset.', 'fa-check-circle');
                    
                } catch (error) {
                    console.error('Reset Status Error:', error);
                    setGlobalError(`Failed to reset status: ${error.message}`);
                }
            };

            const searchSpeakers = async (query, speakerId) => {
                if (!query || query.length < 2) {
                    speakerSuggestions.value[speakerId] = [];
                    return;
                }
                
                loadingSuggestions.value[speakerId] = true;
                
                try {
                    const response = await fetch(`/speakers/search?q=${encodeURIComponent(query)}`);
                    if (!response.ok) throw new Error('Failed to search speakers');
                    
                    const speakers = await response.json();
                    speakerSuggestions.value[speakerId] = speakers;
                } catch (error) {
                    console.error('Error searching speakers:', error);
                    speakerSuggestions.value[speakerId] = [];
                } finally {
                    loadingSuggestions.value[speakerId] = false;
                }
            };
            
            const selectSpeakerSuggestion = (speakerId, suggestion) => {
                if (speakerMap.value[speakerId]) {
                    speakerMap.value[speakerId].name = suggestion.name;
                    speakerSuggestions.value[speakerId] = [];
                }
            };
            
            const closeSpeakerSuggestionsOnClick = (event) => {
                // Check if the click was on an input field or dropdown
                const clickedInput = event.target.closest('input[type="text"]');
                const clickedDropdown = event.target.closest('.absolute.z-10');
                
                // If not clicking on input or dropdown, close all suggestions
                if (!clickedInput && !clickedDropdown) {
                    Object.keys(speakerSuggestions.value).forEach(speakerId => {
                        speakerSuggestions.value[speakerId] = [];
                    });
                }
            };
            
            // Create a mapping for display-friendly speaker IDs
            const speakerDisplayMap = ref({});
            const modalSpeakers = ref([]);
            
            const openSpeakerModal = () => {
                // Clear any existing speaker map data first
                speakerMap.value = {};
                speakerDisplayMap.value = {};
                
                // Get the same speaker order used in processedTranscription
                const transcription = selectedRecording.value?.transcription;
                let speakers = [];
                
                if (transcription) {
                    try {
                        const transcriptionData = JSON.parse(transcription);
                        if (transcriptionData && Array.isArray(transcriptionData)) {
                            // Use the exact same logic as processedTranscription to get speakers
                            speakers = [...new Set(transcriptionData.map(segment => segment.speaker).filter(Boolean))];
                        }
                    } catch (e) {
                        // Fall back to identifiedSpeakers if JSON parsing fails
                        speakers = identifiedSpeakers.value;
                    }
                }
                
                // Set modalSpeakers for the template to use
                modalSpeakers.value = speakers;
                
                // Initialize speaker map with the same order and colors as the transcript
                speakerMap.value = speakers.reduce((acc, speaker, index) => {
                    acc[speaker] = { 
                        name: '', 
                        isMe: false,
                        color: `speaker-color-${(index % 8) + 1}` // Same color assignment as processedTranscription
                    };
                    // Keep the original speaker ID for display
                    speakerDisplayMap.value[speaker] = speaker;
                    return acc;
                }, {});
                
                highlightedSpeaker.value = null;
                speakerSuggestions.value = {};
                loadingSuggestions.value = {};
                isAutoIdentifying.value = false;
                showSpeakerModal.value = true;
            };
    
            const closeSpeakerModal = () => {
                showSpeakerModal.value = false;
                highlightedSpeaker.value = null;
                // Clear the speaker map to prevent stale data from persisting
                speakerMap.value = {};
                speakerSuggestions.value = {};
                loadingSuggestions.value = {};
                
                // Clean up click handler if it exists
                if (window.speakerModalClickHandler) {
                    const modalContent = document.querySelector('.modal-content');
                    if (modalContent) {
                        modalContent.removeEventListener('click', window.speakerModalClickHandler);
                    }
                    delete window.speakerModalClickHandler;
                }
            };
    
            const saveSpeakerNames = async () => {
                if (!selectedRecording.value) return;
    
                // Create a filtered speaker map that excludes entries with blank names
                const filteredSpeakerMap = Object.entries(speakerMap.value).reduce((acc, [speakerId, speakerData]) => {
                    if (speakerData.name && speakerData.name.trim() !== '') {
                        acc[speakerId] = speakerData;
                    }
                    return acc;
                }, {});
    
                try {
                    const response = await fetch(`/recording/${selectedRecording.value.id}/update_speakers`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            speaker_map: filteredSpeakerMap, // Send the filtered map
                            regenerate_summary: regenerateSummaryAfterSpeakerUpdate.value
                        })
                    });
    
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to update speakers');
    
                    // On success, close the modal and clear the speakerMap state *before*
                    // updating the recording data. This prevents a race condition where the
                    // view could re-render using the new data but the old, lingering speakerMap.
                    closeSpeakerModal();
    
                    // The backend returns the fully updated recording object.
                    // We can directly update our local state with this fresh data.
                    const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
                    if (index !== -1) {
                        recordings.value[index] = data.recording;
                    }
                    selectedRecording.value = data.recording;
    
                    showToast('Speaker names updated successfully!', 'fa-check-circle');
    
                    // If a summary regeneration was requested, start polling for its status.
                    if (regenerateSummaryAfterSpeakerUpdate.value) {
                        startReprocessingPoll(selectedRecording.value.id);
                    }
    
                } catch (error) {
                    console.error('Save Speaker Names Error:', error);
                    setGlobalError(`Failed to save speaker names: ${error.message}`);
                }
            };
    
            // Speaker group navigation state
            const currentSpeakerGroupIndex = ref(-1);
            const speakerGroups = ref([]);
            
            const findSpeakerGroups = (speakerId) => {
                if (!speakerId) return [];
                
                const groups = [];
                const modalTranscript = document.querySelector('div.speaker-modal-transcript');
                const mainTranscript = document.querySelector('.transcription-simple-view, .transcription-with-speakers, .transcription-content');
                const transcriptContainer = modalTranscript || mainTranscript;
                
                if (!transcriptContainer) return [];
                
                // For JSON-based transcripts with segments
                const allSegments = transcriptContainer.querySelectorAll('.speaker-segment');
                if (allSegments.length > 0) {
                    let currentGroup = null;
                    let lastSpeakerId = null;
                    
                    allSegments.forEach(segment => {
                        const speakerTag = segment.querySelector('[data-speaker-id]');
                        const segmentSpeakerId = speakerTag?.dataset.speakerId;
                        
                        if (segmentSpeakerId === speakerId) {
                            // If this is a new group (not consecutive with previous)
                            if (lastSpeakerId !== speakerId) {
                                currentGroup = {
                                    startElement: segment,
                                    elements: [segment]
                                };
                                groups.push(currentGroup);
                            } else if (currentGroup) {
                                // Add to existing group
                                currentGroup.elements.push(segment);
                            }
                        }
                        lastSpeakerId = segmentSpeakerId;
                    });
                } else {
                    // For plain text transcripts with speaker tags
                    const allTags = transcriptContainer.querySelectorAll('[data-speaker-id]');
                    let currentGroup = null;
                    
                    allTags.forEach(tag => {
                        if (tag.dataset.speakerId === speakerId) {
                            // Find the parent element that contains this speaker's content
                            const parentSegment = tag.closest('.speaker-segment') || tag.parentElement;
                            
                            if (!currentGroup || !currentGroup.lastElement || 
                                !parentSegment.previousElementSibling || 
                                parentSegment.previousElementSibling !== currentGroup.lastElement) {
                                // Start a new group
                                currentGroup = {
                                    startElement: parentSegment,
                                    elements: [parentSegment],
                                    lastElement: parentSegment
                                };
                                groups.push(currentGroup);
                            } else {
                                // Continue the group
                                currentGroup.elements.push(parentSegment);
                                currentGroup.lastElement = parentSegment;
                            }
                        }
                    });
                }
                
                return groups;
            };
            
            const highlightSpeakerInTranscript = (speakerId) => {
                highlightedSpeaker.value = speakerId;
                
                if (speakerId) {
                    // Find all speaker groups for navigation
                    speakerGroups.value = findSpeakerGroups(speakerId);
                    currentSpeakerGroupIndex.value = 0;
                    
                    // Scroll to the first group
                    if (speakerGroups.value.length > 0) {
                        nextTick(() => {
                            const firstGroup = speakerGroups.value[0];
                            if (firstGroup && firstGroup.startElement) {
                                firstGroup.startElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            }
                        });
                    }
                } else {
                    speakerGroups.value = [];
                    currentSpeakerGroupIndex.value = -1;
                }
            };
            
            const navigateToNextSpeakerGroup = () => {
                if (speakerGroups.value.length === 0) return;
                
                // Don't reset the speaker groups, just update the index
                currentSpeakerGroupIndex.value = (currentSpeakerGroupIndex.value + 1) % speakerGroups.value.length;
                const group = speakerGroups.value[currentSpeakerGroupIndex.value];
                if (group && group.startElement) {
                    group.startElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            };
            
            const navigateToPrevSpeakerGroup = () => {
                if (speakerGroups.value.length === 0) return;
                
                // Don't reset the speaker groups, just update the index
                currentSpeakerGroupIndex.value = currentSpeakerGroupIndex.value <= 0 
                    ? speakerGroups.value.length - 1 
                    : currentSpeakerGroupIndex.value - 1;
                const group = speakerGroups.value[currentSpeakerGroupIndex.value];
                if (group && group.startElement) {
                    group.startElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
                }
            };

            // Enhanced speaker highlighting with focus/blur events for text inputs
            const focusSpeaker = (speakerId) => {
                // Set this as the active speaker input
                activeSpeakerInput.value = speakerId;
                // Only highlight if not already highlighted (to preserve navigation state)
                if (highlightedSpeaker.value !== speakerId) {
                    highlightSpeakerInTranscript(speakerId);
                }
            };

            const blurSpeaker = () => {
                // Clear the active speaker input after a delay to allow clicking on suggestions
                setTimeout(() => {
                    activeSpeakerInput.value = null;
                    speakerSuggestions.value = {};
                }, 200);
                clearSpeakerHighlight();
            };
    
            const clearSpeakerHighlight = () => {
                highlightedSpeaker.value = null;
            };
            
            const autoIdentifySpeakers = async () => {
                if (!selectedRecording.value) {
                    showToast('No recording selected.', 'fa-exclamation-circle');
                    return;
                }
            
                isAutoIdentifying.value = true;
                showToast('Starting automatic speaker identification...', 'fa-magic');
            
                try {
                    const response = await fetch(`/recording/${selectedRecording.value.id}/auto_identify_speakers`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            current_speaker_map: speakerMap.value
                        })
                    });
            
                    const data = await response.json();
                    if (!response.ok) {
                        throw new Error(data.error || 'Unknown error occurred during auto-identification.');
                    }
            
                    // Check if there's a message (e.g., all speakers already identified)
                    if (data.message) {
                        showToast(data.message, 'fa-info-circle');
                        return;
                    }
            
                    // Update speakerMap with the identified names (only for unidentified speakers)
                    let identifiedCount = 0;
                    for (const speakerId in data.speaker_map) {
                        const identifiedName = data.speaker_map[speakerId];
                        if (speakerMap.value[speakerId] && identifiedName && identifiedName.trim() !== '') {
                            speakerMap.value[speakerId].name = identifiedName;
                            identifiedCount++;
                        }
                    }
            
                    if (identifiedCount > 0) {
                        showToast(`${identifiedCount} speaker(s) identified successfully!`, 'fa-check-circle');
                    } else {
                        showToast('No speakers could be identified from the context.', 'fa-info-circle');
                    }
            
                } catch (error) {
                    console.error('Auto Identify Speakers Error:', error);
                    showToast(`Error: ${error.message}`, 'fa-exclamation-circle', 5000);
                } finally {
                    isAutoIdentifying.value = false;
                }
            };

            const toggleInbox = async (recording) => {
                if (!recording || !recording.id) return;
                
                try {
                    const response = await fetch(`/recording/${recording.id}/toggle_inbox`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to toggle inbox status');
                    
                    // Update the recording in the UI
                    recording.is_inbox = data.is_inbox;
                    
                    // Update in the recordings list
                    const index = recordings.value.findIndex(r => r.id === recording.id);
                    if (index !== -1) {
                        recordings.value[index].is_inbox = data.is_inbox;
                    }
                    
                    showToast(`Recording ${data.is_inbox ? 'moved to inbox' : 'marked as read'}`);
                } catch (error) {
                    console.error('Toggle Inbox Error:', error);
                    setGlobalError(`Failed to toggle inbox status: ${error.message}`);
                }
            };
            
            // Toggle highlighted status
            const toggleHighlight = async (recording) => {
                if (!recording || !recording.id) return;
                
                try {
                    const response = await fetch(`/recording/${recording.id}/toggle_highlight`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    });
                    
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to toggle highlighted status');
                    
                    // Update the recording in the UI
                    recording.is_highlighted = data.is_highlighted;
                    
                    // Update in the recordings list
                    const index = recordings.value.findIndex(r => r.id === recording.id);
                    if (index !== -1) {
                        recordings.value[index].is_highlighted = data.is_highlighted;
                    }
                    
                    showToast(`Recording ${data.is_highlighted ? 'highlighted' : 'unhighlighted'}`);
                } catch (error) {
                    console.error('Toggle Highlight Error:', error);
                    setGlobalError(`Failed to toggle highlighted status: ${error.message}`);
                }
            };

            // --- Dark Mode ---
            const toggleDarkMode = () => {
                isDarkMode.value = !isDarkMode.value;
                if (isDarkMode.value) {
                    document.documentElement.classList.add('dark');
                    localStorage.setItem('darkMode', 'true');
                } else {
                    document.documentElement.classList.remove('dark');
                    localStorage.setItem('darkMode', 'false');
                }
            };

            const initializeDarkMode = () => {
                const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
                const savedMode = localStorage.getItem('darkMode');
                if (savedMode === 'true' || (savedMode === null && prefersDark)) {
                    isDarkMode.value = true;
                    document.documentElement.classList.add('dark');
                } else {
                    isDarkMode.value = false;
                    document.documentElement.classList.remove('dark');
                }
            };

            const applyColorScheme = (schemeId, mode = null) => {
                const targetMode = mode || (isDarkMode.value ? 'dark' : 'light');
                const scheme = colorSchemes[targetMode].find(s => s.id === schemeId);
                
                if (!scheme) {
                    console.warn(`Color scheme '${schemeId}' not found for mode '${targetMode}'`);
                    return;
                }

                const allThemeClasses = [
                    ...colorSchemes.light.map(s => s.class),
                    ...colorSchemes.dark.map(s => s.class)
                ].filter(c => c !== '');

                document.documentElement.classList.remove(...allThemeClasses);

                if (scheme.class) {
                    document.documentElement.classList.add(scheme.class);
                }

                currentColorScheme.value = schemeId;
                localStorage.setItem('colorScheme', schemeId);
            };

            const initializeColorScheme = () => {
                const savedScheme = localStorage.getItem('colorScheme') || 'blue';
                currentColorScheme.value = savedScheme;
                applyColorScheme(savedScheme);
            };

            const openColorSchemeModal = () => {
                showColorSchemeModal.value = true;
            };

            const closeColorSchemeModal = () => {
                showColorSchemeModal.value = false;
            };

            const selectColorScheme = (schemeId) => {
                applyColorScheme(schemeId);
                showToast(`Applied ${colorSchemes[isDarkMode.value ? 'dark' : 'light'].find(s => s.id === schemeId)?.name} theme`, 'fa-palette');
            };

            const resetColorScheme = () => {
                applyColorScheme('blue');
                showToast('Reset to default Ocean Blue theme', 'fa-undo');
            };

            // Watch for dark mode changes to reapply color scheme
            watch(isDarkMode, () => {
                applyColorScheme(currentColorScheme.value);
            });

            // --- Sidebar Toggle ---
            const toggleSidebar = () => {
                isSidebarCollapsed.value = !isSidebarCollapsed.value;
            };

            // --- View Management ---
            const switchToUploadView = () => {
                currentView.value = 'upload';
                selectedRecording.value = null;
                if (isMobileScreen.value) {
                    isSidebarCollapsed.value = true;
                }
            };

            const selectRecording = (recording) => {
                if (currentView.value === 'recording' && isRecording.value) {
                    // If we are in the middle of a recording, don't switch views
                    setGlobalError("Please stop the current recording before selecting another one.");
                    return;
                }
                selectedRecording.value = recording;
                currentView.value = 'detail';
                if (recording && recording.id) {
                    localStorage.setItem('lastSelectedRecordingId', recording.id);
                } else {
                    localStorage.removeItem('lastSelectedRecordingId');
                }
                if (isMobileScreen.value) {
                    isSidebarCollapsed.value = true;
                }
            };

            // --- File Upload ---
            const handleDragOver = (e) => {
                e.preventDefault();
                dragover.value = true;
            };

            const handleDragLeave = (e) => {
                if (e.relatedTarget && e.currentTarget.contains(e.relatedTarget)) {
                    return;
                }
                dragover.value = false;
            };

            const handleDrop = (e) => {
                e.preventDefault();
                dragover.value = false;
                addFilesToQueue(e.dataTransfer.files);
            };

            const handleFileSelect = (e) => {
                addFilesToQueue(e.target.files);
                e.target.value = null;
            };

            const addFilesToQueue = (files) => {
                let filesAdded = 0;
                for (const file of files) {
                    const fileObject = file.file ? file.file : file;
                    const notes = file.notes || null;
                    const tags = file.tags || selectedTags.value || [];
                    const asrOptions = file.asrOptions || {
                        language: asrLanguage.value,
                        min_speakers: asrMinSpeakers.value,
                        max_speakers: asrMaxSpeakers.value
                    };

                    // Check if it's an audio file or video container with audio
                    const isAudioFile = fileObject && (
                        fileObject.type.startsWith('audio/') || 
                        fileObject.type === 'video/mp4' ||
                        fileObject.type === 'video/quicktime' ||
                        fileObject.type === 'video/x-msvideo' ||
                        fileObject.type === 'video/webm' ||
                        fileObject.name.toLowerCase().endsWith('.amr') ||
                        fileObject.name.toLowerCase().endsWith('.3gp') ||
                        fileObject.name.toLowerCase().endsWith('.3gpp') ||
                        fileObject.name.toLowerCase().endsWith('.mp4') ||
                        fileObject.name.toLowerCase().endsWith('.mov') ||
                        fileObject.name.toLowerCase().endsWith('.avi') ||
                        fileObject.name.toLowerCase().endsWith('.mkv') ||
                        fileObject.name.toLowerCase().endsWith('.webm')
                    );
                    
                    if (isAudioFile) {
                        // Only check general file size limit (chunking handles OpenAI 25MB limit automatically)
                        if (fileObject.size > maxFileSizeMB.value * 1024 * 1024) {
                            setGlobalError(`File "${fileObject.name}" exceeds the maximum size of ${maxFileSizeMB.value} MB and was skipped.`);
                            continue;
                        }
                        
                        const clientId = `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
                        
                        // Auto-summarization will always occur for all uploads
                        const willAutoSummarize = true;
                        
                        uploadQueue.value.push({
                            file: fileObject, 
                            notes: notes,
                            tags: tags,
                            asrOptions: asrOptions,
                            status: 'queued', 
                            recordingId: null, 
                            clientId: clientId, 
                            error: null,
                            willAutoSummarize: willAutoSummarize
                        });
                        filesAdded++;
                    } else if (fileObject) {
                        setGlobalError(`Invalid file type "${fileObject.name}". Only audio files and video containers with audio (MP3, WAV, MP4, MOV, AVI, etc.) are accepted. File skipped.`);
                    }
                }
                if(filesAdded > 0) {
                    console.log(`Added ${filesAdded} file(s) to the queue.`);
                    progressPopupMinimized.value = false;
                    progressPopupClosed.value = false;
                    if (!isProcessingActive.value) {
                        startProcessingQueue();
                    }
                }
            };

            const resetCurrentFileProcessingState = () => {
                if (pollInterval.value) clearInterval(pollInterval.value);
                pollInterval.value = null;
                currentlyProcessingFile.value = null;
                processingProgress.value = 0;
                processingMessage.value = '';
            };

            const startProcessingQueue = async () => {
                console.log("Attempting to start processing queue...");
                if (isProcessingActive.value) {
                    console.log("Queue processor already active.");
                    return;
                }

                isProcessingActive.value = true;
                resetCurrentFileProcessingState();

                const nextFileItem = uploadQueue.value.find(item => item.status === 'queued');

                if (nextFileItem) {
                    console.log(`Processing next file: ${nextFileItem.file.name} (Client ID: ${nextFileItem.clientId})`);
                    currentlyProcessingFile.value = nextFileItem;
                    
                    // Check if this is a "reload" item (existing recording being tracked)
                    if (nextFileItem.clientId.startsWith('reload-')) {
                        // Skip upload, go directly to polling existing recording
                        console.log(`Skipping upload for existing recording: ${nextFileItem.recordingId}`);
                        nextFileItem.status = 'processing';
                        startStatusPolling(nextFileItem, nextFileItem.recordingId);
                        return;
                    }
                    
                    nextFileItem.status = 'uploading';
                    processingMessage.value = 'Preparing upload...';
                    processingProgress.value = 5;

                    try {
                        const formData = new FormData();
                        formData.append('file', nextFileItem.file);
                        if (nextFileItem.notes) {
                            formData.append('notes', nextFileItem.notes);
                        }
                        
                        // Add tags if selected (multiple tags)
                        // Use tags from the queue item if available, otherwise use global selectedTagIds
                        const tagsToUse = nextFileItem.tags || selectedTags.value || [];
                        tagsToUse.forEach((tag, index) => {
                            const tagId = tag.id || tag; // Handle both tag objects and tag IDs
                            formData.append(`tag_ids[${index}]`, tagId);
                        });
                        
                        // Add ASR advanced options if ASR endpoint is enabled
                        if (useAsrEndpoint.value) {
                            // Use ASR options from the queue item if available, otherwise use global values
                            const asrOpts = nextFileItem.asrOptions || {};
                            const language = asrOpts.language || uploadLanguage.value;
                            const minSpeakers = asrOpts.min_speakers || uploadMinSpeakers.value;
                            const maxSpeakers = asrOpts.max_speakers || uploadMaxSpeakers.value;
                            
                            if (language) {
                                formData.append('language', language);
                            }
                            // Only send speaker limits if they're actually set
                            if (minSpeakers && minSpeakers !== '') {
                                formData.append('min_speakers', minSpeakers.toString());
                            }
                            if (maxSpeakers && maxSpeakers !== '') {
                                formData.append('max_speakers', maxSpeakers.toString());
                            }
                        }

                        processingMessage.value = 'Uploading file...';
                        processingProgress.value = 10;

                        const response = await fetch('/upload', { method: 'POST', body: formData });
                        const data = await response.json();

                        if (!response.ok) {
                            let errorMsg = data.error || `Upload failed with status ${response.status}`;
                            if (response.status === 413) errorMsg = data.error || `File too large. Max: ${data.max_size_mb?.toFixed(0) || maxFileSizeMB.value} MB.`;
                            throw new Error(errorMsg);
                        }

                        if (response.status === 202 && data.id) {
                            console.log(`File ${nextFileItem.file.name} uploaded. Recording ID: ${data.id}. Starting status poll.`);
                            nextFileItem.status = 'pending';
                            nextFileItem.recordingId = data.id;
                            processingMessage.value = 'Upload complete. Waiting for processing...';
                            processingProgress.value = 30;

                            recordings.value.unshift(data);
                            totalRecordings.value++; // Update total count
                            pollProcessingStatus(nextFileItem);

                        } else {
                            throw new Error('Unexpected success response from server after upload.');
                        }

                    } catch (error) {
                        console.error(`Upload/Processing Error for ${nextFileItem.file.name} (Client ID: ${nextFileItem.clientId}):`, error);
                        nextFileItem.status = 'failed';
                        nextFileItem.error = error.message;
                        const failedRecordIndex = recordings.value.findIndex(r => r.id === nextFileItem.recordingId);
                        if(failedRecordIndex !== -1) {
                            recordings.value[failedRecordIndex].status = 'FAILED';
                            recordings.value[failedRecordIndex].transcription = `Upload/Processing failed: ${error.message}`;
                        } else {
                            setGlobalError(`Failed to process "${nextFileItem.file.name}": ${error.message}`);
                        }

                        resetCurrentFileProcessingState();
                        isProcessingActive.value = false;
                        await nextTick();
                        startProcessingQueue();
                    }
                } else {
                    console.log("Upload queue is empty or no files are queued.");
                    isProcessingActive.value = false;
                }
            };

            const pollProcessingStatus = (fileItem) => {
                if (pollInterval.value) clearInterval(pollInterval.value);

                const recordingId = fileItem.recordingId;
                if (!recordingId) {
                    console.error("Cannot poll status without recording ID for", fileItem.file.name);
                    fileItem.status = 'failed';
                    fileItem.error = 'Internal error: Missing recording ID for polling.';
                    resetCurrentFileProcessingState();
                    isProcessingActive.value = false;
                    nextTick(startProcessingQueue);
                    return;
                }

                processingMessage.value = 'Waiting for transcription...';
                processingProgress.value = 40;

                pollInterval.value = setInterval(async () => {
                    // Check if we should stop polling
                    const shouldStopPolling = !currentlyProcessingFile.value || 
                                             currentlyProcessingFile.value.clientId !== fileItem.clientId || 
                                             fileItem.status === 'failed' ||
                                             (fileItem.status === 'completed' && (!fileItem.willAutoSummarize || fileItem.summaryCompleted));
                    
                    if (shouldStopPolling) {
                        console.log(`Polling stopped for ${fileItem.clientId} as it's no longer active or finished.`);
                        clearInterval(pollInterval.value);
                        pollInterval.value = null;
                        if (currentlyProcessingFile.value && currentlyProcessingFile.value.clientId === fileItem.clientId) {
                            resetCurrentFileProcessingState();
                            isProcessingActive.value = false;
                            await nextTick();
                            startProcessingQueue();
                        }
                        return;
                    }

                    try {
                        console.log(`Polling status for recording ID: ${recordingId} (${fileItem.file.name})`);
                        const response = await fetch(`/status/${recordingId}`);
                        if (!response.ok) throw new Error(`Status check failed with status ${response.status}`);

                        const data = await response.json();
                        const galleryIndex = recordings.value.findIndex(r => r.id === recordingId);

                        if (galleryIndex !== -1) {
                            recordings.value[galleryIndex] = data;
                            if(selectedRecording.value?.id === recordingId) {
                                selectedRecording.value = data;
                            }
                        }

                        const previousStatus = fileItem.status;
                        fileItem.status = data.status;
                        fileItem.file.name = data.title || data.original_filename;

                        if (data.status === 'COMPLETED') {
                            console.log(`Processing COMPLETED for ${fileItem.file.name} (ID: ${recordingId})`);
                            
                            // If this was previously summarizing, it's now fully complete
                            if (previousStatus === 'summarizing') {
                                console.log(`Auto-summary completed for ${fileItem.file.name}`);
                                processingMessage.value = 'Processing complete!';
                                processingProgress.value = 100;
                                fileItem.status = 'completed';
                                fileItem.summaryCompleted = true;
                                
                                // This is final completion - clean up immediately and synchronously
                                clearInterval(pollInterval.value);
                                pollInterval.value = null;
                                resetCurrentFileProcessingState();
                                isProcessingActive.value = false;
                                
                                // Keep completed items visible in the modal - don't remove them
                                console.log(`Completed item ${fileItem.clientId} will remain visible in queue`);
                                
                                // Use immediate startProcessingQueue instead of nextTick to avoid duplication
                                startProcessingQueue();
                                return; // Exit early to prevent further processing
                            }
                            // If auto-summarization will occur and hasn't started yet, wait for it
                            else if (fileItem.willAutoSummarize && !fileItem.hasCheckedForAutoSummary) {
                                processingMessage.value = 'Transcription complete!';
                                processingProgress.value = 85;
                                fileItem.status = 'awaiting_summary'; // Use intermediate status to keep it in upload queue
                                // Don't mark as summaryCompleted yet, continue polling
                            }
                            // No auto-summarization expected, complete normally
                            else {
                                processingMessage.value = 'Processing complete!';
                                processingProgress.value = 100;
                                fileItem.status = 'completed';
                                fileItem.summaryCompleted = true; // No summary expected, so consider it complete
                                
                                // Complete immediately for files without auto-summarization
                                clearInterval(pollInterval.value);
                                pollInterval.value = null;
                                resetCurrentFileProcessingState();
                                isProcessingActive.value = false;
                                
                                // Keep completed items visible in the modal - don't remove them
                                console.log(`Completed item ${fileItem.clientId} will remain visible in queue`);
                                
                                startProcessingQueue();
                                return; // Exit early to prevent further processing
                            }
                            
                            // For files with auto-summarization, mark that they've been checked and continue polling
                            if (fileItem.willAutoSummarize && !fileItem.hasCheckedForAutoSummary) {
                                fileItem.hasCheckedForAutoSummary = true;
                                fileItem.autoSummaryStartTime = Date.now();
                                console.log(`Auto-summary expected for ${fileItem.file.name}, continuing to poll...`);
                                // Don't complete yet, continue polling
                                return;
                            }
                            
                            // If we have auto-summarization and we've been waiting, check if we should timeout
                            if (fileItem.willAutoSummarize && fileItem.hasCheckedForAutoSummary) {
                                const waitTime = Date.now() - fileItem.autoSummaryStartTime;
                                const maxWaitTime = 60000; // 60 seconds
                                
                                if (waitTime > maxWaitTime) {
                                    // Timeout - complete the process
                                    console.log(`Auto-summary timeout for ${fileItem.file.name}, completing...`);
                                    processingMessage.value = 'Processing complete!';
                                    processingProgress.value = 100;
                                    fileItem.status = 'completed';
                                    fileItem.summaryCompleted = true; // Mark as complete due to timeout
                                    clearInterval(pollInterval.value);
                                    pollInterval.value = null;
                                    resetCurrentFileProcessingState();
                                    isProcessingActive.value = false;
                                    
                                    // Keep completed items visible in the modal - don't remove them
                                    console.log(`Timed-out item ${fileItem.clientId} will remain visible in queue`);
                                    
                                    startProcessingQueue();
                                } else {
                                    // Still waiting for auto-summary, continue polling
                                    return;
                                }
                            }
                            
                            // Normal completion path (no auto-summary check needed)
                            clearInterval(pollInterval.value);
                            pollInterval.value = null;
                            resetCurrentFileProcessingState();
                            isProcessingActive.value = false;
                            
                            // Remove this item from uploadQueue immediately to prevent duplication
                            const queueIndex = uploadQueue.value.findIndex(item => item.clientId === fileItem.clientId);
                            if (queueIndex !== -1) {
                                uploadQueue.value.splice(queueIndex, 1);
                                console.log(`Removed completed item ${fileItem.clientId} from queue immediately`);
                            }
                            
                            startProcessingQueue();

                        } else if (data.status === 'FAILED') {
                            console.log(`Processing FAILED for ${fileItem.file.name} (ID: ${recordingId})`);
                            processingMessage.value = 'Processing failed.';
                            processingProgress.value = 100;
                            fileItem.status = 'failed';
                            fileItem.error = data.transcription || data.summary || 'Processing failed on server.';
                            setGlobalError(`Processing failed for "${data.title || fileItem.file.name}".`);
                            clearInterval(pollInterval.value);
                            pollInterval.value = null;
                            resetCurrentFileProcessingState();
                            isProcessingActive.value = false;
                            await nextTick();
                            startProcessingQueue();

                        } else if (data.status === 'PROCESSING') {
                            // Check if this file will actually use chunking based on all conditions:
                            // 1. Chunking must be enabled in config
                            // 2. Must NOT be using ASR endpoint (ASR handles large files natively) 
                            // 3. For size-based: File size must exceed the limit (can determine immediately)
                            // 4. For time-based: Can't determine client-side, but backend logs show it gets duration
                            
                            const couldUseChunking = chunkingEnabled.value && !useAsrEndpoint.value;
                            
                            if (couldUseChunking) {
                                if (chunkingMode.value === 'size') {
                                    // Size-based chunking: we can determine definitively
                                    const chunkThresholdBytes = chunkingLimit.value * 1024 * 1024;
                                    const willUseChunking = fileItem.file.size > chunkThresholdBytes;
                                    
                                    if (willUseChunking) {
                                        processingMessage.value = 'Processing large file (chunking in progress)...';
                                        // If auto-summarization will occur, cap at 70%, otherwise 80%
                                        const maxProgress = fileItem.willAutoSummarize ? 70 : 80;
                                        processingProgress.value = Math.round(Math.min(maxProgress, processingProgress.value + Math.random() * 3));
                                    } else {
                                        processingMessage.value = 'Transcription in progress...';
                                        // If auto-summarization will occur, cap at 65%, otherwise 75%
                                        const maxProgress = fileItem.willAutoSummarize ? 65 : 75;
                                        processingProgress.value = Math.round(Math.min(maxProgress, processingProgress.value + Math.random() * 5));
                                    }
                                } else {
                                    // Duration-based chunking: Backend determines this after getting duration
                                    // Show a neutral processing message since we can't know client-side
                                    processingMessage.value = 'Processing file (chunking determined server-side)...';
                                    const maxProgress = fileItem.willAutoSummarize ? 70 : 80;
                                    processingProgress.value = Math.round(Math.min(maxProgress, processingProgress.value + Math.random() * 3));
                                }
                            } else {
                                processingMessage.value = 'Transcription in progress...';
                                const maxProgress = fileItem.willAutoSummarize ? 65 : 75;
                                processingProgress.value = Math.round(Math.min(maxProgress, processingProgress.value + Math.random() * 5));
                            }
                        } else if (data.status === 'SUMMARIZING') {
                            console.log(`Auto-summary started for ${fileItem.file.name}`);
                            processingMessage.value = 'Generating summary...';
                            processingProgress.value = 90;
                            fileItem.status = 'summarizing';
                        } else {
                            processingMessage.value = 'Waiting in queue...';
                            processingProgress.value = 45;
                        }
                    } catch (error) {
                        console.error(`Polling Error for ${fileItem.file.name} (ID: ${recordingId}):`, error);
                        fileItem.status = 'failed';
                        fileItem.error = `Error checking status: ${error.message}`;
                        setGlobalError(`Error checking status for "${fileItem.file.name}": ${error.message}.`);
                        const galleryIndex = recordings.value.findIndex(r => r.id === recordingId);
                        if (galleryIndex !== -1) recordings.value[galleryIndex].status = 'FAILED';

                        clearInterval(pollInterval.value);
                        pollInterval.value = null;
                        resetCurrentFileProcessingState();
                        isProcessingActive.value = false;
                        await nextTick();
                        startProcessingQueue();
                    }
                }, 5000);
            };

            // --- Data Loading ---
            const loadRecordings = async (page = 1, append = false, searchQuery = '') => {
                globalError.value = null;
                if (!append) {
                    isLoadingRecordings.value = true;
                } else {
                    isLoadingMore.value = true;
                }
                
                try {
                    const params = new URLSearchParams({
                        page: page.toString(),
                        per_page: perPage.value.toString()
                    });
                    
                    if (searchQuery.trim()) {
                        params.set('q', searchQuery.trim());
                    }
                    
                    const response = await fetch(`/api/recordings?${params}`);
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to load recordings');
                    
                    // Update pagination state
                    currentPage.value = data.pagination.page;
                    totalRecordings.value = data.pagination.total;
                    totalPages.value = data.pagination.total_pages;
                    hasNextPage.value = data.pagination.has_next;
                    hasPrevPage.value = data.pagination.has_prev;
                    
                    // Update recordings data
                    if (append) {
                        // Append to existing recordings (infinite scroll)
                        recordings.value = [...recordings.value, ...data.recordings];
                    } else {
                        // Replace recordings (fresh load or search)
                        recordings.value = data.recordings;
                        
                        // Try to restore last selected recording
                        const lastRecordingId = localStorage.getItem('lastSelectedRecordingId');
                        if (lastRecordingId && data.recordings.length > 0) {
                            const recordingToSelect = data.recordings.find(r => r.id == lastRecordingId);
                            if (recordingToSelect) {
                                selectRecording(recordingToSelect);
                            }
                        }
                    }

                    // Handle incomplete recordings for processing queue
                    const incompleteRecordings = data.recordings.filter(r => ['PENDING', 'PROCESSING', 'SUMMARIZING'].includes(r.status));
                    if (incompleteRecordings.length > 0 && !isProcessingActive.value) {
                        console.warn(`Found ${incompleteRecordings.length} incomplete recording(s) on load.`);
                        for (const recording of incompleteRecordings) {
                            let queueItem = uploadQueue.value.find(item => item.recordingId === recording.id);
                            if (!queueItem) {
                                queueItem = {
                                    file: { name: recording.title || `Recording ${recording.id}`, size: recording.file_size },
                                    status: 'queued',
                                    recordingId: recording.id,
                                    clientId: `reload-${recording.id}`,
                                    error: null
                                };
                                uploadQueue.value.unshift(queueItem);
                                if (!isProcessingActive.value) {
                                    startProcessingQueue();
                                }
                            }
                        }
                    }

                } catch (error) {
                    console.error('Load Recordings Error:', error);
                    setGlobalError(`Failed to load recordings: ${error.message}`);
                    if (!append) {
                        recordings.value = [];
                    }
                } finally {
                    isLoadingRecordings.value = false;
                    isLoadingMore.value = false;
                }
            };
            
            // Load more recordings (infinite scroll)
            const loadMoreRecordings = async () => {
                if (!hasNextPage.value || isLoadingMore.value) return;
                await loadRecordings(currentPage.value + 1, true, searchQuery.value);
            };
            
            // Search with debouncing
            const performSearch = async (query = '') => {
                currentPage.value = 1;
                await loadRecordings(1, false, query);
            };
            
            // Debounced search function
            const debouncedSearch = (query) => {
                if (searchDebounceTimer.value) {
                    clearTimeout(searchDebounceTimer.value);
                }
                searchDebounceTimer.value = setTimeout(() => {
                    performSearch(query);
                }, 300); // 300ms debounce
            };

            const loadTags = async () => {
                try {
                    const response = await fetch('/api/tags');
                    if (response.ok) {
                        availableTags.value = await response.json();
                    } else {
                        console.warn('Failed to load tags:', response.status);
                        availableTags.value = [];
                    }
                } catch (error) {
                    console.warn('Error loading tags:', error);
                    availableTags.value = [];
                }
            };

            const addTagToSelection = (tagId) => {
                if (!selectedTagIds.value.includes(tagId)) {
                    selectedTagIds.value.push(tagId);
                    applyTagDefaults();
                }
            };

            const removeTagFromSelection = (tagId) => {
                const index = selectedTagIds.value.indexOf(tagId);
                if (index > -1) {
                    selectedTagIds.value.splice(index, 1);
                    applyTagDefaults();
                }
            };

            const applyTagDefaults = () => {
                // Apply defaults from the first selected tag (highest priority)
                const firstTag = selectedTags.value[0];
                if (firstTag && useAsrEndpoint.value) {
                    if (firstTag.default_language) {
                        uploadLanguage.value = firstTag.default_language;
                    }
                    if (firstTag.default_min_speakers) {
                        uploadMinSpeakers.value = firstTag.default_min_speakers;
                    }
                    if (firstTag.default_max_speakers) {
                        uploadMaxSpeakers.value = firstTag.default_max_speakers;
                    }
                }
            };

            // Legacy function for backward compatibility
            const onTagSelected = applyTagDefaults;

            // Tag helper functions
            const getRecordingTags = (recording) => {
                if (!recording || !recording.tags) return [];
                return recording.tags || [];
            };

            const getAvailableTagsForRecording = (recording) => {
                if (!recording || !availableTags.value) return [];
                const recordingTagIds = getRecordingTags(recording).map(tag => tag.id);
                return availableTags.value.filter(tag => !recordingTagIds.includes(tag.id));
            };
            
            // Computed property for filtered available tags in the modal
            const filteredAvailableTagsForModal = computed(() => {
                if (!editingRecording.value) return [];
                const availableTags = getAvailableTagsForRecording(editingRecording.value);
                if (!tagSearchFilter.value) return availableTags;
                
                const filter = tagSearchFilter.value.toLowerCase();
                return availableTags.filter(tag => 
                    tag.name.toLowerCase().includes(filter)
                );
            });
            const filterByTag = (tag) => {
                // Use advanced filter instead of text-based
                filterTags.value = [tag.id];
                applyAdvancedFilters();
            };
            const clearTagFilter = () => {
                searchQuery.value = '';
                clearAllFilters();
            };
            
            // Build search query from advanced filters
            const buildSearchQuery = () => {
                let query = [];
                
                // Add text search
                if (filterTextQuery.value.trim()) {
                    query.push(filterTextQuery.value.trim());
                }
                
                // Add tag filters
                if (filterTags.value.length > 0) {
                    const tagNames = filterTags.value.map(tagId => {
                        const tag = availableTags.value.find(t => t.id === tagId);
                        return tag ? `tag:${tag.name.replace(/\s+/g, '_')}` : '';
                    }).filter(Boolean);
                    query.push(...tagNames);
                }
                
                // Add date filter
                if (filterDatePreset.value) {
                    query.push(`date:${filterDatePreset.value}`);
                } else if (filterDateRange.value.start || filterDateRange.value.end) {
                    // Custom date range - send as separate parameters
                    // Will be handled differently in the backend
                    if (filterDateRange.value.start) {
                        query.push(`date_from:${filterDateRange.value.start}`);
                    }
                    if (filterDateRange.value.end) {
                        query.push(`date_to:${filterDateRange.value.end}`);
                    }
                }
                
                return query.join(' ');
            };
            
            const applyAdvancedFilters = () => {
                searchQuery.value = buildSearchQuery();
            };
            
            const clearAllFilters = () => {
                filterTags.value = [];
                filterDateRange.value = { start: '', end: '' };
                filterDatePreset.value = '';
                filterTextQuery.value = '';
                searchQuery.value = '';
            };

            const editRecordingTags = (recording) => {
                editingRecording.value = recording;
                selectedNewTagId.value = '';
                showEditTagsModal.value = true;
            };

            const closeEditTagsModal = () => {
                showEditTagsModal.value = false;
                editingRecording.value = null;
                selectedNewTagId.value = '';
                tagSearchFilter.value = '';  // Clear the filter when closing
            };

            const addTagToRecording = async (tagId = null) => {
                // Use provided tagId or fall back to selectedNewTagId
                const tagToAddId = tagId || selectedNewTagId.value;
                if (!tagToAddId || !editingRecording.value) return;
                
                try {
                    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
                    
                    const response = await fetch(`/api/recordings/${editingRecording.value.id}/tags`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': csrfToken
                        },
                        body: JSON.stringify({ tag_id: tagToAddId })
                    });
                    
                    if (!response.ok) {
                        const errorData = await response.json();
                        throw new Error(errorData.error || 'Failed to add tag');
                    }
                    
                    // Update local recording data
                    const tagToAdd = availableTags.value.find(tag => tag.id == tagToAddId);
                    if (tagToAdd) {
                        if (!editingRecording.value.tags) {
                            editingRecording.value.tags = [];
                        }
                        editingRecording.value.tags.push(tagToAdd);
                        
                        // Also update in recordings list if it's a different object
                        const recordingInList = recordings.value.find(r => r.id === editingRecording.value.id);
                        if (recordingInList && recordingInList !== editingRecording.value) {
                            if (!recordingInList.tags) {
                                recordingInList.tags = [];
                            }
                            recordingInList.tags.push(tagToAdd);
                        }
                    }
                    
                    selectedNewTagId.value = '';
                    
                } catch (error) {
                    console.error('Error adding tag to recording:', error);
                    setGlobalError(`Failed to add tag: ${error.message}`);
                }
            };

            const removeTagFromRecording = async (tagId) => {
                if (!editingRecording.value) return;
                
                try {
                    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
                    
                    const response = await fetch(`/api/recordings/${editingRecording.value.id}/tags/${tagId}`, {
                        method: 'DELETE',
                        headers: {
                            'X-CSRFToken': csrfToken
                        }
                    });
                    
                    if (!response.ok) {
                        const errorData = await response.json();
                        throw new Error(errorData.error || 'Failed to remove tag');
                    }
                    
                    // Update local recording data
                    editingRecording.value.tags = editingRecording.value.tags.filter(tag => tag.id !== tagId);
                    
                    // Also update in recordings list if it's a different object
                    const recordingInList = recordings.value.find(r => r.id === editingRecording.value.id);
                    if (recordingInList && recordingInList !== editingRecording.value && recordingInList.tags) {
                        recordingInList.tags = recordingInList.tags.filter(tag => tag.id !== tagId);
                    }
                    
                } catch (error) {
                    console.error('Error removing tag from recording:', error);
                    setGlobalError(`Failed to remove tag: ${error.message}`);
                }
            };

            // --- Audio Recording ---
            const startRecordingWithDisclaimer = async (mode = 'microphone') => {
                // Check if disclaimer needs to be shown
                if (recordingDisclaimer.value && recordingDisclaimer.value.trim()) {
                    pendingRecordingMode.value = mode;
                    showRecordingDisclaimerModal.value = true;
                } else {
                    // No disclaimer configured, proceed directly
                    await startRecordingActual(mode);
                }
            };
            
            const acceptRecordingDisclaimer = async () => {
                showRecordingDisclaimerModal.value = false;
                if (pendingRecordingMode.value) {
                    await startRecordingActual(pendingRecordingMode.value);
                    pendingRecordingMode.value = null;
                }
            };
            
            const cancelRecordingDisclaimer = () => {
                showRecordingDisclaimerModal.value = false;
                pendingRecordingMode.value = null;
            };
            
            const startRecording = startRecordingWithDisclaimer; // Maintain backward compatibility
            
            const startRecordingActual = async (mode = 'microphone') => {
                recordingMode.value = mode;
                
                try {
                    // Load tags if not already loaded
                    if (availableTags.value.length === 0) {
                        await loadTags();
                    }
                    
                    // Reset state
                    audioChunks.value = [];
                    audioBlobURL.value = null;
                    recordingNotes.value = '';
                    activeStreams.value = [];
                    // Clear previous tag selection and ASR options for fresh recording
                    selectedTags.value = [];
                    asrLanguage.value = '';
                    asrMinSpeakers.value = '';
                    asrMaxSpeakers.value = '';

                    let combinedStream = null;
                    let micStream = null;
                    let systemStream = null;

                    // Get microphone stream if needed
                    if (mode === 'microphone' || mode === 'both') {
                        if (!canRecordAudio.value) {
                            throw new Error('Microphone recording is not supported by your browser or permission was denied.');
                        }
                        micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                        activeStreams.value.push(micStream);
                        showToast('Microphone access granted', 'fa-microphone');
                    }

                    // Get system audio stream if needed
                    if (mode === 'system' || mode === 'both') {
                        if (!canRecordSystemAudio.value) {
                            throw new Error('System audio recording is not supported by your browser.');
                        }
                        try {
                            systemStream = await navigator.mediaDevices.getDisplayMedia({
                                audio: true,
                                video: true // Request video to enable system audio sharing prompt
                            });

                            // Check if the user actually granted audio permission
                            if (systemStream.getAudioTracks().length === 0) {
                                // Stop the video track if it exists, since we didn't get audio
                                systemStream.getVideoTracks().forEach(track => track.stop());
                                throw new Error('System audio permission was not granted. Please ensure you check the "Share system audio" box.');
                            }

                            activeStreams.value.push(systemStream);
                            showToast('System audio access granted', 'fa-desktop');
                        } catch (err) {
                            if (mode === 'system') {
                                throw err; // Re-throw the original error to be caught by the outer handler
                            } else {
                                // For 'both' mode, fall back to microphone only
                                showToast('System audio denied, using microphone only', 'fa-exclamation-triangle');
                                mode = 'microphone';
                                systemStream = null; // Make sure systemStream is null so it's not used later
                            }
                        }
                    }

                    // Combine streams if we have both
                    if (micStream && systemStream) {
                        try {
                            audioContext.value = new (window.AudioContext || window.webkitAudioContext)();
                            
                            const micSource = audioContext.value.createMediaStreamSource(micStream);
                            const systemSource = audioContext.value.createMediaStreamSource(systemStream);
                            const destination = audioContext.value.createMediaStreamDestination();
                            
                            micSource.connect(destination);
                            systemSource.connect(destination);
                            
                            // Create a new MediaStream with only the audio track from the destination
                            const mixedAudioTrack = destination.stream.getAudioTracks()[0];
                            if (!mixedAudioTrack) {
                                throw new Error('Failed to create mixed audio track');
                            }
                            
                            combinedStream = new MediaStream([mixedAudioTrack]);
                            
                            // Verify the stream has audio tracks
                            if (combinedStream.getAudioTracks().length === 0) {
                                throw new Error('Combined stream has no audio tracks');
                            }
                            
                            console.log('Successfully created combined audio stream with', combinedStream.getAudioTracks().length, 'audio tracks');
                            showToast('Recording both microphone and system audio', 'fa-microphone');
                            
                        } catch (error) {
                            console.error('Failed to combine audio streams:', error);
                            // Fallback to system audio only
                            if (audioContext.value) {
                                audioContext.value.close().catch(e => console.error("Error closing AudioContext:", e));
                                audioContext.value = null;
                            }
                            combinedStream = systemStream;
                            showToast('Failed to combine audio, using system audio only', 'fa-exclamation-triangle');
                        }
                    } else if (systemStream) {
                        // For system audio only, create a new stream with just the audio tracks
                        const audioTracks = systemStream.getAudioTracks();
                        if (audioTracks.length > 0) {
                            combinedStream = new MediaStream(audioTracks);
                            console.log('Created system audio stream with', audioTracks.length, 'audio tracks');
                            showToast('Recording system audio only', 'fa-desktop');
                        } else {
                            throw new Error('System stream has no audio tracks');
                        }
                    } else if (micStream) {
                        combinedStream = micStream;
                        showToast('Recording microphone only', 'fa-microphone');
                    } else {
                        throw new Error('No audio streams available for recording.');
                    }

                    // Setup MediaRecorder with optimized settings for transcription
                    const getOptimizedRecorderOptions = () => {
                        // Define transcription-optimized options in order of preference
                        const optionsList = [
                            // Best option: Opus codec at 32kbps (excellent compression for speech)
                            {
                                mimeType: 'audio/webm;codecs=opus',
                                audioBitsPerSecond: 32000,
                                description: 'Optimized (32kbps Opus)'
                            },
                            // Good option: Opus at 64kbps (slightly higher quality)
                            {
                                mimeType: 'audio/webm;codecs=opus',
                                audioBitsPerSecond: 64000,
                                description: 'Good quality (64kbps Opus)'
                            },
                            // Fallback 1: WebM with reduced bitrate
                            {
                                mimeType: 'audio/webm',
                                audioBitsPerSecond: 64000,
                                description: 'Standard WebM (64kbps)'
                            },
                            // Fallback 2: MP4 with reduced bitrate
                            {
                                mimeType: 'audio/mp4',
                                audioBitsPerSecond: 64000,
                                description: 'Standard MP4 (64kbps)'
                            },
                            // Fallback 3: Just the codec without bitrate
                            {
                                mimeType: 'audio/webm;codecs=opus',
                                description: 'Opus codec (default bitrate)'
                            },
                            // Fallback 4: Just WebM without bitrate
                            {
                                mimeType: 'audio/webm',
                                description: 'WebM (default bitrate)'
                            }
                        ];

                        // Test each option to find the first supported one
                        for (const options of optionsList) {
                            if (MediaRecorder.isTypeSupported(options.mimeType)) {
                                console.log(`Testing audio recording option: ${options.description} - ${options.mimeType}`);
                                return options;
                            }
                        }

                        // Final fallback: no options (browser default)
                        console.log('Using browser default audio recording settings');
                        return null;
                    };

                    // Try to create MediaRecorder with progressive fallbacks
                    let mediaRecorderCreated = false;
                    let recorderOptions = getOptimizedRecorderOptions();
                    let attemptCount = 0;
                    
                    while (!mediaRecorderCreated && attemptCount < 5) {
                        try {
                            attemptCount++;
                            
                            if (recorderOptions && attemptCount === 1) {
                                // First attempt: try with full options
                                console.log(`Attempt ${attemptCount}: Trying ${recorderOptions.description}`);
                                mediaRecorder.value = new MediaRecorder(combinedStream, recorderOptions);
                                actualBitrate.value = recorderOptions.audioBitsPerSecond || 64000;
                                showToast(`Recording: ${recorderOptions.description}`, 'fa-compress-alt', 3000);
                            } else if (recorderOptions && attemptCount === 2 && recorderOptions.audioBitsPerSecond) {
                                // Second attempt: try same mime type without bitrate constraint
                                console.log(`Attempt ${attemptCount}: Trying ${recorderOptions.mimeType} without bitrate`);
                                mediaRecorder.value = new MediaRecorder(combinedStream, { mimeType: recorderOptions.mimeType });
                                actualBitrate.value = 128000; // Estimate
                                showToast(`Recording: ${recorderOptions.mimeType} (default bitrate)`, 'fa-compress-alt', 3000);
                            } else {
                                // Final attempt: browser default
                                console.log(`Attempt ${attemptCount}: Using browser default`);
                                mediaRecorder.value = new MediaRecorder(combinedStream);
                                actualBitrate.value = 128000; // Estimate browser default
                                showToast('Recording with browser default settings', 'fa-microphone', 3000);
                            }
                            
                            mediaRecorderCreated = true;
                            console.log(`MediaRecorder created successfully on attempt ${attemptCount}`);
                            
                        } catch (error) {
                            console.warn(`MediaRecorder creation attempt ${attemptCount} failed:`, error);
                            mediaRecorder.value = null;
                            
                            if (attemptCount >= 5) {
                                throw new Error(`Failed to create MediaRecorder after ${attemptCount} attempts. Last error: ${error.message}`);
                            }
                        }
                    }
                    
                    if (!mediaRecorder.value) {
                        throw new Error('Failed to create MediaRecorder with any configuration');
                    }
                    
                    console.log(`Recording with estimated bitrate: ${actualBitrate.value} bps`);
                    
                    mediaRecorder.value.ondataavailable = event => audioChunks.value.push(event.data);
                    mediaRecorder.value.onstop = () => {
                        const audioBlob = new Blob(audioChunks.value, { type: 'audio/webm' });
                        audioBlobURL.value = URL.createObjectURL(audioBlob);
                        
                        // Stop size monitoring
                        stopSizeMonitoring();
                        
                        // Stop all active streams
                        activeStreams.value.forEach(stream => {
                            stream.getTracks().forEach(track => track.stop());
                        });
                        activeStreams.value = [];
                        
                        if (audioContext.value) {
                            audioContext.value.close().catch(e => console.error("Error closing AudioContext:", e));
                            audioContext.value = null;
                        }
                        cancelAnimationFrame(animationFrameId.value);
                        clearInterval(recordingInterval.value);
                    };

                    // --- Visualizer Setup ---
                    if (!audioContext.value) {
                        audioContext.value = new (window.AudioContext || window.webkitAudioContext)();
                    }

                    if (mode === 'both' && micStream && systemStream) {
                        // Dual visualizer setup
                        micAnalyser.value = audioContext.value.createAnalyser();
                        micAnalyser.value.fftSize = 256;
                        const micSource = audioContext.value.createMediaStreamSource(micStream);
                        micSource.connect(micAnalyser.value);

                        systemAnalyser.value = audioContext.value.createAnalyser();
                        systemAnalyser.value.fftSize = 256;
                        const systemSource = audioContext.value.createMediaStreamSource(systemStream);
                        systemSource.connect(systemAnalyser.value);

                    } else {
                        // Single visualizer setup
                        const visualizerStream = micStream || systemStream;
                        if (visualizerStream) {
                            analyser.value = audioContext.value.createAnalyser();
                            analyser.value.fftSize = 256;
                            const source = audioContext.value.createMediaStreamSource(visualizerStream);
                            source.connect(analyser.value);
                        }
                    }
                    
                    // Start recording and timer
                    mediaRecorder.value.start();
                    isRecording.value = true;
                    recordingTime.value = 0;
                    recordingInterval.value = setInterval(() => recordingTime.value++, 1000);
                    
                    // Start size monitoring
                    startSizeMonitoring();
                    
                    // Switch to recording view
                    currentView.value = 'recording';
                    
                    // Start visualizer(s)
                    drawVisualizers();

                    setGlobalError(null);
                } catch (err) {
                    console.error("Error starting recording:", err);
                    setGlobalError(`Could not start recording: ${err.message}`);
                    isRecording.value = false;
                    
                    // Clean up any streams that were created
                    activeStreams.value.forEach(stream => {
                        stream.getTracks().forEach(track => track.stop());
                    });
                    activeStreams.value = [];
                }
            };

            const stopRecording = () => {
                if (mediaRecorder.value && isRecording.value) {
                    mediaRecorder.value.stop();
                    isRecording.value = false;
                    stopSizeMonitoring();
                    cancelAnimationFrame(animationFrameId.value);
                    animationFrameId.value = null;
                }
            };

            const uploadRecordedAudio = () => {
                if (!audioBlobURL.value) {
                    setGlobalError("No recorded audio to upload.");
                    return;
                }
                const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
                const recordedFile = new File(audioChunks.value, `recording-${timestamp}.webm`, { type: 'audio/webm' });

                // Pass notes, tags, and ASR options along with the file
                addFilesToQueue([{ 
                    file: recordedFile, 
                    notes: recordingNotes.value,
                    tags: selectedTags.value,
                    asrOptions: {
                        language: asrLanguage.value,
                        min_speakers: asrMinSpeakers.value,
                        max_speakers: asrMaxSpeakers.value
                    }
                }]);
                discardRecording();
                
                // Switch back to upload view
                currentView.value = 'upload';
            };

            const discardRecording = () => {
                if (audioBlobURL.value) {
                    URL.revokeObjectURL(audioBlobURL.value);
                }
                audioBlobURL.value = null;
                audioChunks.value = [];
                isRecording.value = false;
                recordingTime.value = 0;
                if (recordingInterval.value) clearInterval(recordingInterval.value);
                recordingNotes.value = '';
                // Clear tags and ASR options for fresh start
                selectedTags.value = [];
                asrLanguage.value = '';
                asrMinSpeakers.value = '';
                asrMaxSpeakers.value = '';
            };

            const drawSingleVisualizer = (analyserNode, canvasElement) => {
                if (!analyserNode || !canvasElement) return;

                const bufferLength = analyserNode.frequencyBinCount;
                const dataArray = new Uint8Array(bufferLength);
                analyserNode.getByteFrequencyData(dataArray);

                const canvasCtx = canvasElement.getContext('2d');
                const WIDTH = canvasElement.width;
                const HEIGHT = canvasElement.height;

                canvasCtx.clearRect(0, 0, WIDTH, HEIGHT);

                const barWidth = (WIDTH / bufferLength) * 1.5;
                let barHeight;
                let x = 0;
                
                // Use theme-specific colors that work with all color schemes
                const buttonColor = getComputedStyle(document.documentElement).getPropertyValue('--bg-button').trim();
                const buttonHoverColor = getComputedStyle(document.documentElement).getPropertyValue('--bg-button-hover').trim();
                
                // Create gradient that works in both light and dark modes
                const gradient = canvasCtx.createLinearGradient(0, 0, 0, HEIGHT);
                if (isDarkMode.value) {
                    // Dark mode: use button colors with transparency
                    gradient.addColorStop(0, buttonColor);
                    gradient.addColorStop(0.6, buttonHoverColor);
                    gradient.addColorStop(1, 'rgba(0, 0, 0, 0.2)');
                } else {
                    // Light mode: use more saturated colors for visibility
                    gradient.addColorStop(0, buttonColor);
                    gradient.addColorStop(0.5, buttonHoverColor);
                    gradient.addColorStop(1, 'rgba(0, 0, 0, 0.1)');
                }

                for (let i = 0; i < bufferLength; i++) {
                    barHeight = dataArray[i] / 2.5;
                    canvasCtx.fillStyle = gradient;
                    canvasCtx.fillRect(x, HEIGHT - barHeight, barWidth, barHeight);
                    x += barWidth + 2;
                }
            };

            const drawVisualizers = () => {
                if (!isRecording.value) {
                    if (animationFrameId.value) {
                        cancelAnimationFrame(animationFrameId.value);
                        animationFrameId.value = null;
                    }
                    return;
                }

                animationFrameId.value = requestAnimationFrame(drawVisualizers);

                if (recordingMode.value === 'both') {
                    drawSingleVisualizer(micAnalyser.value, micVisualizer.value);
                    drawSingleVisualizer(systemAnalyser.value, systemVisualizer.value);
                } else {
                    drawSingleVisualizer(analyser.value, visualizer.value);
                }
            };

            // --- Recording Management ---
            const saveMetadata = async (recordingDataToSave) => {
                globalError.value = null;
                if (!recordingDataToSave || !recordingDataToSave.id) return null;
                console.log('Saving metadata for:', recordingDataToSave.id);
                try {
                    const payload = {
                        id: recordingDataToSave.id,
                        title: recordingDataToSave.title,
                        participants: recordingDataToSave.participants,
                        notes: recordingDataToSave.notes,
                        summary: recordingDataToSave.summary,
                        meeting_date: recordingDataToSave.meeting_date
                    };
                    const response = await fetch('/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to save metadata');

                    console.log('Save successful:', data.recording.id);
                    const index = recordings.value.findIndex(r => r.id === data.recording.id);
                    if (index !== -1) {
                        recordings.value[index].title = payload.title;
                        recordings.value[index].participants = payload.participants;
                        recordings.value[index].notes = payload.notes;
                        recordings.value[index].notes_html = data.recording.notes_html;
                        recordings.value[index].summary = payload.summary;
                        recordings.value[index].summary_html = data.recording.summary_html;
                        recordings.value[index].meeting_date = payload.meeting_date;
                    }
                    if (selectedRecording.value?.id === data.recording.id) {
                        selectedRecording.value.title = payload.title;
                        selectedRecording.value.participants = payload.participants;
                        selectedRecording.value.notes = payload.notes;
                        selectedRecording.value.notes_html = data.recording.notes_html;
                        selectedRecording.value.summary = payload.summary;
                        selectedRecording.value.summary_html = data.recording.summary_html;
                        selectedRecording.value.meeting_date = payload.meeting_date;
                    }
                    return data.recording;
                } catch (error) {
                    console.error('Save Metadata Error:', error);
                    setGlobalError(`Save failed: ${error.message}`);
                    return null;
                }
            };

            const editRecording = (recording) => {
                editingRecording.value = JSON.parse(JSON.stringify(recording));
                showEditModal.value = true;
            };

            const cancelEdit = () => {
                showEditModal.value = false;
                editingRecording.value = null;
            };

            const saveEdit = async () => {
                const success = await saveMetadata(editingRecording.value);
                if (success) {
                    cancelEdit();
                }
            };

            const confirmDelete = (recording) => {
                recordingToDelete.value = recording;
                showDeleteModal.value = true;
            };

            const cancelDelete = () => {
                showDeleteModal.value = false;
                recordingToDelete.value = null;
            };

            const deleteRecording = async () => {
                globalError.value = null;
                if (!recordingToDelete.value) return;
                const idToDelete = recordingToDelete.value.id;
                const titleToDelete = recordingToDelete.value.title;
                try {
                    const response = await fetch(`/recording/${idToDelete}`, { method: 'DELETE' });
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to delete recording');

                    recordings.value = recordings.value.filter(r => r.id !== idToDelete);
                    totalRecordings.value--; // Update total count

                    const queueIndex = uploadQueue.value.findIndex(item => item.recordingId === idToDelete);
                    if (queueIndex !== -1) {
                        const deletedItem = uploadQueue.value.splice(queueIndex, 1)[0];
                        console.log(`Removed item ${deletedItem.clientId} from queue.`);
                        if (currentlyProcessingFile.value?.clientId === deletedItem.clientId) {
                            console.log(`Deleting currently processing file: ${titleToDelete}. Stopping poll and moving to next.`);
                            clearInterval(pollInterval.value);
                            pollInterval.value = null;
                            resetCurrentFileProcessingState();
                            isProcessingActive.value = false;
                            await nextTick();
                            startProcessingQueue();
                        }
                    }

                    if (selectedRecording.value?.id === idToDelete) selectedRecording.value = null;
                    cancelDelete();
                    console.log(`Successfully deleted recording ${idToDelete} (${titleToDelete})`);

                } catch (error) {
                    console.error('Delete Error:', error);
                    setGlobalError(`Failed to delete recording "${titleToDelete}": ${error.message}`);
                    cancelDelete();
                }
            };

            // --- Inline Editing ---
            const toggleEditParticipants = () => {
                editingParticipants.value = !editingParticipants.value;
                if (!editingParticipants.value) {
                    saveInlineEdit('participants');
                }
            };

            const toggleEditMeetingDate = () => {
                editingMeetingDate.value = !editingMeetingDate.value;
                if (!editingMeetingDate.value) {
                    saveInlineEdit('meeting_date');
                }
            };

            const toggleEditSummary = () => {
                editingSummary.value = !editingSummary.value;
                if (editingSummary.value) {
                    // Store current content in temp variable
                    tempSummaryContent.value = selectedRecording.value.summary || '';
                    nextTick(() => {
                        initializeSummaryMarkdownEditor();
                    });
                }
            };

            const cancelEditSummary = () => {
                if (summaryMarkdownEditorInstance.value) {
                    summaryMarkdownEditorInstance.value.toTextArea();
                    summaryMarkdownEditorInstance.value = null;
                }
                editingSummary.value = false;
                // Restore original content
                if (selectedRecording.value) {
                    selectedRecording.value.summary = tempSummaryContent.value;
                }
            };

            const saveEditSummary = async () => {
                if (summaryMarkdownEditorInstance.value) {
                    selectedRecording.value.summary = summaryMarkdownEditorInstance.value.value();
                    summaryMarkdownEditorInstance.value.toTextArea();
                    summaryMarkdownEditorInstance.value = null;
                }
                editingSummary.value = false;
                await saveInlineEdit('summary');
            };

            const toggleEditNotes = () => {
                editingNotes.value = !editingNotes.value;
                if (editingNotes.value) {
                    // Store current content in temp variable
                    tempNotesContent.value = selectedRecording.value.notes || '';
                    // Initialize markdown editor when entering edit mode
                    nextTick(() => {
                        initializeMarkdownEditor();
                    });
                }
            };

            const cancelEditNotes = () => {
                if (markdownEditorInstance.value) {
                    markdownEditorInstance.value.toTextArea();
                    markdownEditorInstance.value = null;
                }
                editingNotes.value = false;
                // Restore original content
                if (selectedRecording.value) {
                    selectedRecording.value.notes = tempNotesContent.value;
                }
            };

            const saveEditNotes = async () => {
                if (markdownEditorInstance.value) {
                    // Get the markdown content from the editor
                    selectedRecording.value.notes = markdownEditorInstance.value.value();
                    markdownEditorInstance.value.toTextArea();
                    markdownEditorInstance.value = null;
                }
                editingNotes.value = false;
                await saveInlineEdit('notes');
            };

            const clickToEditNotes = () => {
                // Allow clicking on empty notes area to start editing
                if (!editingNotes.value && (!selectedRecording.value?.notes || selectedRecording.value.notes.trim() === '')) {
                    toggleEditNotes();
                }
            };

            const clickToEditSummary = () => {
                // Allow clicking on empty summary area to start editing  
                if (!editingSummary.value && (!selectedRecording.value?.summary || selectedRecording.value.summary.trim() === '')) {
                    toggleEditSummary();
                }
            };

            const autoSaveNotes = async () => {
                if (markdownEditorInstance.value && editingNotes.value) {
                    // Just save the content to the model, don't exit edit mode
                    selectedRecording.value.notes = markdownEditorInstance.value.value();
                    // Silently save to backend without changing UI state
                    try {
                        const payload = {
                            id: selectedRecording.value.id,
                            title: selectedRecording.value.title,
                            participants: selectedRecording.value.participants,
                            notes: selectedRecording.value.notes,
                            summary: selectedRecording.value.summary,
                            meeting_date: selectedRecording.value.meeting_date
                        };
                        const response = await fetch('/save', {
                            method: 'POST',
                            headers: { 
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify(payload)
                        });
                        const data = await response.json();
                        if (response.ok && data.recording) {
                            // Update the HTML rendered versions if they exist
                            if (data.recording.notes_html) {
                                selectedRecording.value.notes_html = data.recording.notes_html;
                            }
                        } else {
                            console.error('Failed to auto-save notes');
                        }
                    } catch (error) {
                        console.error('Error auto-saving notes:', error);
                    }
                }
            };

            const autoSaveSummary = async () => {
                if (summaryMarkdownEditorInstance.value && editingSummary.value) {
                    // Just save the content to the model, don't exit edit mode
                    selectedRecording.value.summary = summaryMarkdownEditorInstance.value.value();
                    // Silently save to backend without changing UI state
                    try {
                        const payload = {
                            id: selectedRecording.value.id,
                            title: selectedRecording.value.title,
                            participants: selectedRecording.value.participants,
                            notes: selectedRecording.value.notes,
                            summary: selectedRecording.value.summary,
                            meeting_date: selectedRecording.value.meeting_date
                        };
                        const response = await fetch('/save', {
                            method: 'POST',
                            headers: { 
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify(payload)
                        });
                        const data = await response.json();
                        if (response.ok && data.recording) {
                            // Update the HTML rendered versions if they exist
                            if (data.recording.summary_html) {
                                selectedRecording.value.summary_html = data.recording.summary_html;
                            }
                        } else {
                            console.error('Failed to auto-save summary');
                        }
                    } catch (error) {
                        console.error('Error auto-saving summary:', error);
                    }
                }
            };

            const initializeMarkdownEditor = () => {
                if (!notesMarkdownEditor.value) return;
                
                try {
                    markdownEditorInstance.value = new EasyMDE({
                        element: notesMarkdownEditor.value,
                        spellChecker: false,
                        autofocus: true,
                        placeholder: "Enter notes in Markdown format...",
                        initialValue: selectedRecording.value?.notes || '',
                        status: false,
                        toolbar: [
                            "bold", "italic", "heading", "|",
                            "quote", "unordered-list", "ordered-list", "|",
                            "link", "image", "|",
                            "preview", "side-by-side", "fullscreen", "|",
                            "guide"
                        ],
                        previewClass: ["editor-preview", "notes-preview"],
                        theme: isDarkMode.value ? "dark" : "light"
                    });
                    
                    // Add auto-save functionality
                    markdownEditorInstance.value.codemirror.on('change', () => {
                        if (autoSaveTimer.value) {
                            clearTimeout(autoSaveTimer.value);
                        }
                        autoSaveTimer.value = setTimeout(() => {
                            autoSaveNotes();
                        }, autoSaveDelay);
                    });
                } catch (error) {
                    console.error('Failed to initialize markdown editor:', error);
                    // Fallback to regular textarea editing
                    editingNotes.value = true;
                }
            };

            const initializeRecordingMarkdownEditor = () => {
                if (!recordingNotesEditor.value) {
                    console.log('Recording notes editor ref not found');
                    return;
                }
                
                // Check if EasyMDE is available
                if (typeof EasyMDE === 'undefined') {
                    console.error('EasyMDE is not loaded');
                    return;
                }
                
                // Clean up existing instance if any
                if (recordingMarkdownEditorInstance.value) {
                    recordingMarkdownEditorInstance.value.toTextArea();
                    recordingMarkdownEditorInstance.value = null;
                }
                
                try {
                    console.log('Initializing recording markdown editor');
                    recordingMarkdownEditorInstance.value = new EasyMDE({
                        element: recordingNotesEditor.value,
                        spellChecker: false,
                        autofocus: false,
                        placeholder: "Type your notes in Markdown format...",
                        status: false,
                        toolbar: [
                            "bold", "italic", "heading", "|",
                            "quote", "unordered-list", "ordered-list", "|",
                            "link", "|",
                            "preview", "guide"
                        ],
                        previewClass: ["editor-preview", "notes-preview"],
                        theme: isDarkMode.value ? "dark" : "light",
                        initialValue: recordingNotes.value || "",
                        maxHeight: "300px",  // Add height constraint to prevent unlimited growth
                        minHeight: "150px"   // Minimum height for usability
                    });
                    
                    // Sync changes back to the reactive variable
                    recordingMarkdownEditorInstance.value.codemirror.on('change', () => {
                        recordingNotes.value = recordingMarkdownEditorInstance.value.value();
                    });
                    
                    console.log('Recording markdown editor initialized successfully');
                } catch (error) {
                    console.error('Failed to initialize recording markdown editor:', error);
                    // Keep as regular textarea if EasyMDE fails
                }
            };

            const toggleTranscriptionViewMode = () => {
                transcriptionViewMode.value = transcriptionViewMode.value === 'simple' ? 'bubble' : 'simple';
                localStorage.setItem('transcriptionViewMode', transcriptionViewMode.value);
            };

            const saveInlineEdit = async (field) => {
                if (!selectedRecording.value) return;

                const fullPayload = {
                    id: selectedRecording.value.id,
                    title: selectedRecording.value.title,
                    participants: selectedRecording.value.participants,
                    notes: selectedRecording.value.notes,
                    summary: selectedRecording.value.summary,
                    meeting_date: selectedRecording.value.meeting_date
                };

                try {
                    const updatedRecording = await saveMetadata(fullPayload);
                    if (updatedRecording) {
                        if (field === 'notes') {
                            selectedRecording.value.notes_html = updatedRecording.notes_html;
                        } else if (field === 'summary') {
                            selectedRecording.value.summary_html = updatedRecording.summary_html;
                        }

                        switch(field) {
                            case 'participants':
                                editingParticipants.value = false;
                                break;
                            case 'meeting_date':
                                editingMeetingDate.value = false;
                                break;
                            case 'summary':
                                editingSummary.value = false;
                                break;
                            case 'notes':
                                editingNotes.value = false;
                                break;
                        }
                        showToast(`${field.charAt(0).toUpperCase() + field.slice(1).replace('_', ' ')} updated successfully`);
                    }
                } catch (error) {
                    console.error(`Save ${field} Error:`, error);
                    setGlobalError(`Failed to save ${field}: ${error.message}`);
                }
            };

            // --- Chat Functionality ---
            // Helper function to check if chat is scrolled to bottom (within bottom 5%)
            const isChatScrolledToBottom = () => {
                if (!chatMessagesRef.value) return true;
                const { scrollTop, scrollHeight, clientHeight } = chatMessagesRef.value;
                const scrollableHeight = scrollHeight - clientHeight;
                if (scrollableHeight <= 0) return true; // No scrolling possible
                const scrollPercentage = scrollTop / scrollableHeight;
                return scrollPercentage >= 0.95; // Within bottom 5%
            };

            // Helper function to scroll chat to bottom with smooth behavior
            const scrollChatToBottom = () => {
                if (chatMessagesRef.value) {
                    requestAnimationFrame(() => {
                        if (chatMessagesRef.value) {
                            chatMessagesRef.value.scrollTop = chatMessagesRef.value.scrollHeight;
                        }
                    });
                }
            };

            const sendChatMessage = async () => {
                if (!chatInput.value.trim() || isChatLoading.value || !selectedRecording.value || selectedRecording.value.status !== 'COMPLETED') {
                    return;
                }

                const message = chatInput.value.trim();

                if (!Array.isArray(chatMessages.value)) {
                    chatMessages.value = [];
                }

                chatMessages.value.push({ role: 'user', content: message });
                chatInput.value = '';
                isChatLoading.value = true;

                await nextTick();
                // Always scroll to bottom when user sends a new message
                scrollChatToBottom();

                let assistantMessage = null;

                try {
                    const messageHistory = chatMessages.value
                        .slice(0, -1)
                        .map(msg => ({ role: msg.role, content: msg.content }));

                    const response = await fetch('/chat', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            recording_id: selectedRecording.value.id,
                            message: message,
                            message_history: messageHistory
                        })
                    });

                    if (!response.ok) {
                        const errorData = await response.json();
                        throw new Error(errorData.error || 'Failed to get chat response');
                    }

                    const reader = response.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';

                    const processStream = async () => {
                        let isFirstChunk = true;
                        while (true) {
                            const { done, value } = await reader.read();
                            if (done) break;

                            buffer += decoder.decode(value, { stream: true });
                            const lines = buffer.split('\n');
                            buffer = lines.pop();

                            for (const line of lines) {
                                if (line.startsWith('data: ')) {
                                    const jsonStr = line.substring(6);
                                    if (jsonStr) {
                                        try {
                                            const data = JSON.parse(jsonStr);
                                            if (data.thinking) {
                                                // Check scroll position BEFORE updating content
                                                const shouldScroll = isChatScrolledToBottom();
                                                
                                                if (isFirstChunk) {
                                                    isChatLoading.value = false;
                                                    assistantMessage = reactive({ 
                                                        role: 'assistant', 
                                                        content: '', 
                                                        html: '',
                                                        thinking: data.thinking,
                                                        thinkingExpanded: false
                                                    });
                                                    chatMessages.value.push(assistantMessage);
                                                    isFirstChunk = false;
                                                } else if (assistantMessage) {
                                                    // Append to existing thinking content
                                                    if (assistantMessage.thinking) {
                                                        assistantMessage.thinking += '\n\n' + data.thinking;
                                                    } else {
                                                        assistantMessage.thinking = data.thinking;
                                                    }
                                                }
                                                
                                                // Scroll if we were at bottom before the update
                                                if (shouldScroll) {
                                                    await nextTick();
                                                    scrollChatToBottom();
                                                }
                                            }
                                            if (data.delta) {
                                                // Check scroll position BEFORE updating content
                                                const shouldScroll = isChatScrolledToBottom();
                                                
                                                if (isFirstChunk) {
                                                    isChatLoading.value = false;
                                                    assistantMessage = reactive({ 
                                                        role: 'assistant', 
                                                        content: '', 
                                                        html: '',
                                                        thinking: '',
                                                        thinkingExpanded: false
                                                    });
                                                    chatMessages.value.push(assistantMessage);
                                                    isFirstChunk = false;
                                                }
                                                
                                                assistantMessage.content += data.delta;
                                                assistantMessage.html = marked.parse(assistantMessage.content);
                                                
                                                // Scroll if we were at bottom before the update
                                                if (shouldScroll) {
                                                    await nextTick();
                                                    scrollChatToBottom();
                                                }
                                            }
                                            if (data.end_of_stream) {
                                                return;
                                            }
                                            if (data.error) {
                                                throw new Error(data.error);
                                            }
                                        } catch (e) {
                                            console.error('Error parsing stream data:', e);
                                        }
                                    }
                                }
                            }
                        }
                    };

                    await processStream();

                } catch (error) {
                    console.error('Chat Error:', error);
                    if (assistantMessage) {
                        assistantMessage.content = `Error: ${error.message}`;
                        assistantMessage.html = `<span class="text-red-500">Error: ${error.message}</span>`;
                    } else {
                        chatMessages.value.push({ role: 'assistant', content: `Error: ${error.message}`, html: `<span class="text-red-500">Error: ${error.message}</span>` });
                    }
                } finally {
                    isChatLoading.value = false;
                    await nextTick();
                    // Final scroll only if user is at bottom
                    if (isChatScrolledToBottom()) {
                        scrollChatToBottom();
                    }
                }
            };

            // --- Column Resizing ---
            const startColumnResize = (event) => {
                isResizing.value = true;
                const startX = event.clientX;
                const startLeftWidth = leftColumnWidth.value;
                
                const handleMouseMove = (e) => {
                    if (!isResizing.value) return;
                    
                    const container = document.getElementById('mainContentColumns');
                    if (!container) return;
                    
                    const containerRect = container.getBoundingClientRect();
                    const deltaX = e.clientX - startX;
                    const containerWidth = containerRect.width;
                    const deltaPercent = (deltaX / containerWidth) * 100;
                    
                    let newLeftWidth = startLeftWidth + deltaPercent;
                    newLeftWidth = Math.max(20, Math.min(80, newLeftWidth)); // Constrain between 20% and 80%
                    
                    leftColumnWidth.value = newLeftWidth;
                    rightColumnWidth.value = 100 - newLeftWidth;
                };
                
                const handleMouseUp = () => {
                    isResizing.value = false;
                    document.removeEventListener('mousemove', handleMouseMove);
                    document.removeEventListener('mouseup', handleMouseUp);
                    
                    // Save to localStorage
                    localStorage.setItem('transcriptColumnWidth', leftColumnWidth.value);
                    localStorage.setItem('summaryColumnWidth', rightColumnWidth.value);
                };
                
                document.addEventListener('mousemove', handleMouseMove);
                document.addEventListener('mouseup', handleMouseUp);
                event.preventDefault();
            };

            // --- Chat Input Handling ---
            const handleChatKeydown = (event) => {
                if (event.key === 'Enter') {
                    if (event.ctrlKey || event.shiftKey) {
                        // Ctrl+Enter or Shift+Enter: add new line (default behavior)
                        return;
                    } else {
                        // Enter: send message
                        event.preventDefault();
                        sendChatMessage();
                    }
                }
            };

            // --- Audio Player ---
            const seekAudio = (time, context = 'main') => {
                let audioPlayer = null;
                if (context === 'modal') {
                    // The audio player in the modal has the class directly on the audio element
                    audioPlayer = document.querySelector('audio.speaker-modal-transcript');
                } else {
                    audioPlayer = document.querySelector('.main-content-area audio');
                }

                if (audioPlayer) {
                    const wasPlaying = !audioPlayer.paused;
                    audioPlayer.currentTime = time;
                    if (wasPlaying) {
                        audioPlayer.play();
                    }
                } else {
                    console.warn(`Audio player not found for context: ${context}`);
                    // Fallback to old method if new one fails
                    const oldPlayer = document.querySelector('audio');
                    if(oldPlayer) {
                        const wasPlaying = !oldPlayer.paused;
                        oldPlayer.currentTime = time;
                        if (wasPlaying) {
                            oldPlayer.play();
                        }
                    }
                }
            };

            const seekAudioFromEvent = (event) => {
                const segmentElement = event.target.closest('[data-start-time]');
                if (!segmentElement) return;

                const time = parseFloat(segmentElement.dataset.startTime);
                if (isNaN(time)) return;

                // Determine context by checking if we're inside the speaker modal
                const isInSpeakerModal = event.target.closest('.speaker-modal-transcript') !== null;
                const context = isInSpeakerModal ? 'modal' : 'main';
                
                seekAudio(time, context);
            };

            const onPlayerVolumeChange = (event) => {
                const newVolume = event.target.volume;
                playerVolume.value = newVolume;
                localStorage.setItem('playerVolume', newVolume);
            };

            // --- i18n Helper Functions ---
            // Use the same safeT function that's globally available
            const t = safeT;
            
            const tc = (key, count, params = {}) => {
                return window.i18n ? window.i18n.tc(key, count, params) : key;
            };
            
            const changeLanguage = async (langCode) => {
                if (window.i18n) {
                    await window.i18n.setLocale(langCode);
                    currentLanguage.value = langCode;
                    const lang = availableLanguages.value.find(l => l.code === langCode);
                    currentLanguageName.value = lang ? lang.nativeName : 'English';
                    showLanguageMenu.value = false;
                    isUserMenuOpen.value = false;
                    
                    // Save preference to backend
                    try {
                        await fetch('/api/user/preferences', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-CSRF-Token': csrfToken.value
                            },
                            body: JSON.stringify({ language: langCode })
                        });
                    } catch (error) {
                        console.error('Failed to save language preference:', error);
                    }
                }
            };
            
            // --- Toast Notifications ---
            const showToast = (message, icon = 'fa-check-circle', duration = 2000) => {
                const toastContainer = document.getElementById('toastContainer');
                
                const toast = document.createElement('div');
                toast.className = 'toast';
                toast.innerHTML = `<i class="fas ${icon}"></i> ${message}`;
                
                toastContainer.appendChild(toast);
                
                setTimeout(() => {
                    toast.classList.add('show');
                }, 10);
                
                setTimeout(() => {
                    toast.classList.remove('show');
                    setTimeout(() => {
                        toastContainer.removeChild(toast);
                    }, 300);
                }, duration);
            };

            const animateCopyButton = (button) => {
                button.classList.add('copy-success');
                
                const originalContent = button.innerHTML;
                button.innerHTML = '<i class="fas fa-check"></i>';
                
                setTimeout(() => {
                    button.classList.remove('copy-success');
                    button.innerHTML = originalContent;
                }, 1500);
            };

            const copyMessage = (text, event) => {
                const button = event.currentTarget;
                
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(text)
                        .then(() => {
                            showToast('Copied to clipboard!');
                            animateCopyButton(button);
                        })
                        .catch(err => {
                            console.error('Copy failed:', err);
                            showToast('Failed to copy: ' + err.message, 'fa-exclamation-circle');
                            fallbackCopyTextToClipboard(text, button);
                        });
                } else {
                    fallbackCopyTextToClipboard(text, button);
                }
            };

            const fallbackCopyTextToClipboard = (text, button = null) => {
                try {
                    const textArea = document.createElement("textarea");
                    textArea.value = text;
                    
                    textArea.style.position = "fixed";
                    textArea.style.left = "-999999px";
                    textArea.style.top = "-999999px";
                    document.body.appendChild(textArea);
                    
                    textArea.focus();
                    textArea.select();
                    const successful = document.execCommand('copy');
                    
                    document.body.removeChild(textArea);
                    
                    if (successful) {
                        showToast('Copied to clipboard!');
                        if (button) animateCopyButton(button);
                    } else {
                        showToast('Copy failed. Your browser may not support this feature.', 'fa-exclamation-circle');
                    }
                } catch (err) {
                    console.error('Fallback copy failed:', err);
                    showToast('Unable to copy: ' + err.message, 'fa-exclamation-circle');
                }
            };

            const copyTranscription = (event) => {
                if (!selectedRecording.value || !selectedRecording.value.transcription) {
                    showToast('No transcription available to copy.', 'fa-exclamation-circle');
                    return;
                }
                
                const button = event.currentTarget;
                let textToCopy = '';
                
                try {
                    const transcriptionData = JSON.parse(selectedRecording.value.transcription);
                    if (Array.isArray(transcriptionData)) {
                        const wasDiarized = transcriptionData.some(segment => segment.speaker);
                        if (wasDiarized) {
                            textToCopy = transcriptionData.map(segment => {
                                const speakerName = segment.speaker;
                                return `[${speakerName}]: ${segment.sentence}`;
                            }).join('\n');
                        } else {
                            textToCopy = transcriptionData.map(segment => segment.sentence).join('\n');
                        }
                    } else {
                        textToCopy = selectedRecording.value.transcription;
                    }
                } catch (e) {
                    textToCopy = selectedRecording.value.transcription;
                }

                animateCopyButton(button);
                
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(textToCopy)
                        .then(() => {
                            showToast('Transcription copied to clipboard!');
                        })
                        .catch(err => {
                            console.error('Copy failed:', err);
                            showToast('Failed to copy: ' + err.message, 'fa-exclamation-circle');
                            fallbackCopyTextToClipboard(textToCopy);
                        });
                } else {
                    fallbackCopyTextToClipboard(textToCopy);
                }
            };

            const copySummary = (event) => {
                if (!selectedRecording.value || !selectedRecording.value.summary) {
                    showToast('No summary available to copy.', 'fa-exclamation-circle');
                    return;
                }
                const button = event.currentTarget;
                const textToCopy = selectedRecording.value.summary;
                animateCopyButton(button);
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(textToCopy)
                        .then(() => {
                            showToast('Summary copied to clipboard!');
                        })
                        .catch(err => {
                            console.error('Copy failed:', err);
                            showToast('Failed to copy: ' + err.message, 'fa-exclamation-circle');
                            fallbackCopyTextToClipboard(textToCopy);
                        });
                } else {
                    fallbackCopyTextToClipboard(textToCopy);
                }
            };

            const copyNotes = (event) => {
                if (!selectedRecording.value || !selectedRecording.value.notes) {
                    showToast('No notes available to copy.', 'fa-exclamation-circle');
                    return;
                }
                const button = event.currentTarget;
                const textToCopy = selectedRecording.value.notes;
                animateCopyButton(button);
                if (navigator.clipboard && window.isSecureContext) {
                    navigator.clipboard.writeText(textToCopy)
                        .then(() => {
                            showToast('Notes copied to clipboard!');
                        })
                        .catch(err => {
                            console.error('Copy failed:', err);
                            showToast('Failed to copy: ' + err.message, 'fa-exclamation-circle');
                            fallbackCopyTextToClipboard(textToCopy);
                        });
                } else {
                    fallbackCopyTextToClipboard(textToCopy);
                }
            };

            const downloadSummary = async () => {
                if (!selectedRecording.value || !selectedRecording.value.summary) {
                    showToast('No summary available to download.', 'fa-exclamation-circle');
                    return;
                }
                
                try {
                    const response = await fetch(`/recording/${selectedRecording.value.id}/download/summary`);
                    if (!response.ok) {
                        const error = await response.json();
                        showToast(error.error || 'Failed to download summary', 'fa-exclamation-circle');
                        return;
                    }
                    
                    // Create blob and download
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.style.display = 'none';
                    a.href = url;
                    
                    // Get filename from response headers or use default
                    const contentDisposition = response.headers.get('Content-Disposition');
                    let filename = 'summary.docx';
                    if (contentDisposition) {
                        const matches = /filename="(.+)"/.exec(contentDisposition);
                        if (matches) {
                            filename = matches[1];
                        }
                    }
                    a.download = filename;
                    
                    document.body.appendChild(a);
                    a.click();
                    window.URL.revokeObjectURL(url);
                    document.body.removeChild(a);
                    
                    showToast('Summary downloaded successfully!');
                } catch (error) {
                    console.error('Download failed:', error);
                    showToast('Failed to download summary', 'fa-exclamation-circle');
                }
            };

            const downloadNotes = async () => {
                if (!selectedRecording.value || !selectedRecording.value.notes) {
                    showToast('No notes available to download.', 'fa-exclamation-circle');
                    return;
                }
                
                try {
                    const response = await fetch(`/recording/${selectedRecording.value.id}/download/notes`);
                    if (!response.ok) {
                        const error = await response.json();
                        showToast(error.error || 'Failed to download notes', 'fa-exclamation-circle');
                        return;
                    }
                    
                    // Create blob and download
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.style.display = 'none';
                    a.href = url;
                    
                    // Get filename from response headers or use default
                    const contentDisposition = response.headers.get('Content-Disposition');
                    let filename = 'notes.docx';
                    if (contentDisposition) {
                        const matches = /filename="(.+)"/.exec(contentDisposition);
                        if (matches) {
                            filename = matches[1];
                        }
                    }
                    a.download = filename;
                    
                    document.body.appendChild(a);
                    a.click();
                    window.URL.revokeObjectURL(url);
                    document.body.removeChild(a);
                    
                    showToast('Notes downloaded successfully!', 'fa-check-circle');
                } catch (error) {
                    console.error('Download failed:', error);
                    showToast('Failed to download notes', 'fa-exclamation-circle');
                }
            };

            const downloadChat = async () => {
                if (!selectedRecording.value || chatMessages.value.length === 0) {
                    showToast('No chat messages to download.', 'fa-exclamation-circle');
                    return;
                }
                
                try {
                    const response = await fetch(`/recording/${selectedRecording.value.id}/download/chat`, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'X-CSRFToken': csrfToken.value
                        },
                        body: JSON.stringify({
                            messages: chatMessages.value
                        })
                    });
                    
                    if (!response.ok) {
                        const error = await response.json();
                        showToast(error.error || 'Failed to download chat', 'fa-exclamation-circle');
                        return;
                    }
                    
                    // Create blob and download
                    const blob = await response.blob();
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.style.display = 'none';
                    a.href = url;
                    
                    // Get filename from response headers or use default
                    const contentDisposition = response.headers.get('Content-Disposition');
                    let filename = 'chat.docx';
                    if (contentDisposition) {
                        const matches = /filename="(.+)"/.exec(contentDisposition);
                        if (matches) {
                            filename = matches[1];
                        }
                    }
                    a.download = filename;
                    
                    document.body.appendChild(a);
                    a.click();
                    window.URL.revokeObjectURL(url);
                    document.body.removeChild(a);
                    
                    showToast('Notes downloaded successfully!');
                } catch (error) {
                    console.error('Download failed:', error);
                    showToast('Failed to download notes', 'fa-exclamation-circle');
                }
            };


            const openShareModal = async (recording) => {
                recordingToShare.value = recording;
                shareOptions.share_summary = true;
                shareOptions.share_notes = true;
                generatedShareLink.value = '';
                existingShareDetected.value = false;
                showShareModal.value = true;
                
                // Check for existing share
                try {
                    const response = await fetch(`/api/recording/${recording.id}/share`, {
                        method: 'GET'
                    });
                    const data = await response.json();
                    if (response.ok && data.exists) {
                        generatedShareLink.value = data.share_url;
                        existingShareDetected.value = true;
                        shareOptions.share_summary = data.share.share_summary;
                        shareOptions.share_notes = data.share.share_notes;
                    }
                } catch (error) {
                    console.error('Error checking for existing share:', error);
                }
            };

            const closeShareModal = () => {
                showShareModal.value = false;
                recordingToShare.value = null;
                existingShareDetected.value = false;
            };

            const createShare = async (forceNew = false) => {
                if (!recordingToShare.value) return;
                try {
                    const response = await fetch(`/api/recording/${recordingToShare.value.id}/share`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            ...shareOptions,
                            force_new: forceNew
                        })
                    });
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to create share link');
                    
                    generatedShareLink.value = data.share_url;
                    existingShareDetected.value = data.existing && !forceNew;
                    
                    if (data.existing && !forceNew) {
                        // Show that we're using an existing share
                        showToast('Using existing share link', 'fa-link');
                    } else {
                        showToast('Share link created successfully!', 'fa-check-circle');
                    }
                } catch (error) {
                    setGlobalError(`Failed to create share link: ${error.message}`);
                }
            };

            const copyShareLink = () => {
                if (!generatedShareLink.value) return;
                navigator.clipboard.writeText(generatedShareLink.value).then(() => {
                    showToast('Share link copied to clipboard!');
                });
            };

            const openSharesList = async () => {
                isLoadingShares.value = true;
                showSharesListModal.value = true;
                try {
                    const response = await fetch('/api/shares');
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to load shared items');
                    userShares.value = data;
                } catch (error) {
                    setGlobalError(`Failed to load shared items: ${error.message}`);
                } finally {
                    isLoadingShares.value = false;
                }
            };

            const closeSharesList = () => {
                showSharesListModal.value = false;
                userShares.value = [];
            };

            const updateShare = async (share) => {
                try {
                    const response = await fetch(`/api/share/${share.id}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            share_summary: share.share_summary,
                            share_notes: share.share_notes
                        })
                    });
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to update share');
                    showToast('Share permissions updated.', 'fa-check-circle');
                } catch (error) {
                    setGlobalError(`Failed to update share: ${error.message}`);
                }
            };

            const confirmDeleteShare = (share) => {
                shareToDelete.value = share;
                showShareDeleteModal.value = true;
            };

            const cancelDeleteShare = () => {
                shareToDelete.value = null;
                showShareDeleteModal.value = false;
            };

            const deleteShare = async () => {
                if (!shareToDelete.value) return;
                const shareId = shareToDelete.value.id;
                try {
                    const response = await fetch(`/api/share/${shareId}`, { method: 'DELETE' });
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to delete share');
                    userShares.value = userShares.value.filter(s => s.id !== shareId);
                    showToast('Share link deleted successfully.', 'fa-check-circle');
                    showShareDeleteModal.value = false;
                    shareToDelete.value = null;
                } catch (error) {
                    setGlobalError(`Failed to delete share: ${error.message}`);
                }
            };

            // --- Watchers ---
            watch(uploadQueue, (newQueue, oldQueue) => {
                if (newQueue.length === 0 && oldQueue.length > 0 && !isProcessingActive.value) {
                    console.log("Upload queue processing finished.");
                    setTimeout(() => progressPopupMinimized.value = true, 500);
                    setTimeout(() => {
                        if (completedInQueue.value === totalInQueue.value && !isProcessingActive.value) {
                            progressPopupClosed.value = true;
                        }
                    }, 2000);
                }
            }, { deep: true });

            // Watch for changes to speakerMap to handle "This is Me" functionality
            watch(speakerMap, (newSpeakerMap) => {
                if (!newSpeakerMap) return;
                
                Object.keys(newSpeakerMap).forEach(speakerId => {
                    const speakerData = newSpeakerMap[speakerId];
                    if (speakerData.isMe && currentUserName.value && !speakerData.name) {
                        // Automatically fill in the user's name when "This is Me" is checked
                        speakerData.name = currentUserName.value;
                    } else if (!speakerData.isMe && speakerData.name === currentUserName.value) {
                        // Clear the name if "This is Me" is unchecked and the name matches the current user
                        speakerData.name = '';
                    }
                });
            }, { deep: true });

            // Watch for tab changes to save content properly
            watch(selectedTab, (newTab, oldTab) => {
                // Save content when switching away from notes tab but keep editor open
                if (oldTab === 'notes' && editingNotes.value && markdownEditorInstance.value) {
                    // Save the current content from the editor
                    const currentContent = markdownEditorInstance.value.value();
                    selectedRecording.value.notes = currentContent;
                    // Call auto-save instead of saveInlineEdit to keep editor open
                    autoSaveNotes();
                    // Store that we need to recreate the editor when coming back
                    tempNotesContent.value = currentContent;
                }
                // Save content when switching away from summary tab but keep editor open
                if (oldTab === 'summary' && editingSummary.value && summaryMarkdownEditorInstance.value) {
                    // Save the current content from the editor
                    const currentContent = summaryMarkdownEditorInstance.value.value();
                    selectedRecording.value.summary = currentContent;
                    // Call auto-save instead of saveInlineEdit to keep editor open
                    autoSaveSummary();
                    // Store that we need to recreate the editor when coming back
                    tempSummaryContent.value = currentContent;
                }
                
                // Re-initialize editors when switching back to tabs
                if (newTab === 'notes' && editingNotes.value) {
                    // Destroy old instance if exists
                    if (markdownEditorInstance.value) {
                        markdownEditorInstance.value.toTextArea();
                        markdownEditorInstance.value = null;
                    }
                    // Re-initialize the editor in next tick
                    nextTick(() => {
                        initializeMarkdownEditor();
                    });
                }
                if (newTab === 'summary' && editingSummary.value) {
                    // Destroy old instance if exists
                    if (summaryMarkdownEditorInstance.value) {
                        summaryMarkdownEditorInstance.value.toTextArea();
                        summaryMarkdownEditorInstance.value = null;
                    }
                    // Re-initialize the editor in next tick
                    nextTick(() => {
                        initializeSummaryMarkdownEditor();
                    });
                }
            });
            
            // Watch for mobile tab changes similarly
            watch(mobileTab, (newTab, oldTab) => {
                // Save content when switching away from notes tab but keep editor open
                if (oldTab === 'notes' && editingNotes.value && markdownEditorInstance.value) {
                    const currentContent = markdownEditorInstance.value.value();
                    selectedRecording.value.notes = currentContent;
                    autoSaveNotes(); // Use auto-save instead
                    tempNotesContent.value = currentContent;
                }
                // Save content when switching away from summary tab but keep editor open
                if (oldTab === 'summary' && editingSummary.value && summaryMarkdownEditorInstance.value) {
                    const currentContent = summaryMarkdownEditorInstance.value.value();
                    selectedRecording.value.summary = currentContent;
                    autoSaveSummary(); // Use auto-save instead
                    tempSummaryContent.value = currentContent;
                }
                
                // Re-initialize editors when switching back to tabs on mobile
                if (newTab === 'notes' && editingNotes.value) {
                    if (markdownEditorInstance.value) {
                        markdownEditorInstance.value.toTextArea();
                        markdownEditorInstance.value = null;
                    }
                    nextTick(() => {
                        initializeMarkdownEditor();
                    });
                }
                if (newTab === 'summary' && editingSummary.value) {
                    if (summaryMarkdownEditorInstance.value) {
                        summaryMarkdownEditorInstance.value.toTextArea();
                        summaryMarkdownEditorInstance.value = null;
                    }
                    nextTick(() => {
                        initializeSummaryMarkdownEditor();
                    });
                }
            });

            watch(selectedRecording, (newVal, oldVal) => {
                if (newVal?.id !== oldVal?.id) {
                    // Save any pending edits before switching recordings
                    if (editingNotes.value && markdownEditorInstance.value && oldVal?.id) {
                        selectedRecording.value = oldVal; // Temporarily restore old recording
                        saveEditNotes(); // This will save and cleanup
                        selectedRecording.value = newVal; // Switch back to new recording
                    }
                    if (editingSummary.value && summaryMarkdownEditorInstance.value && oldVal?.id) {
                        selectedRecording.value = oldVal; // Temporarily restore old recording
                        saveEditSummary(); // This will save and cleanup
                        selectedRecording.value = newVal; // Switch back to new recording
                    }
                    
                    chatMessages.value = [];
                    showChat.value = false;
                    selectedTab.value = 'summary';
                    
                    editingParticipants.value = false;
                    editingMeetingDate.value = false;
                    editingSummary.value = false;
                    editingNotes.value = false;
                    
                    // Fix WebM duration issue by forcing metadata load
                    if (newVal?.id) {
                        nextTick(() => {
                            const audioElements = document.querySelectorAll('audio');
                            audioElements.forEach(audio => {
                                if (audio.src && audio.src.includes(`/audio/${newVal.id}`)) {
                                    // For WebM files, we need to seek to end to get duration
                                    const fixDuration = () => {
                                        if (!isFinite(audio.duration) || audio.duration === 0) {
                                            audio.currentTime = 1e101; // Seek to "infinity" to load duration
                                            audio.addEventListener('timeupdate', function resetTime() {
                                                audio.currentTime = 0;
                                                audio.removeEventListener('timeupdate', resetTime);
                                            }, { once: true });
                                        }
                                    };
                                    
                                    // Try to fix duration when metadata loads
                                    audio.addEventListener('loadedmetadata', fixDuration, { once: true });
                                    
                                    // Also try immediately in case metadata is already loaded
                                    if (audio.readyState >= 1) {
                                        fixDuration();
                                    }
                                }
                            });
                        });
                    }
                }
            });

            watch(currentView, (newView) => {
                if (newView === 'recording') {
                    // Initialize recording markdown editor when switching to recording view
                    nextTick(() => {
                        initializeRecordingMarkdownEditor();
                        // Fix audio duration for recorded files
                        if (audioBlobURL.value) {
                            const recordingAudio = document.querySelector('audio[src*="blob:"]');
                            if (recordingAudio) {
                                const fixDuration = () => {
                                    if (!isFinite(recordingAudio.duration) || recordingAudio.duration === 0) {
                                        const originalTime = recordingAudio.currentTime;
                                        recordingAudio.currentTime = 1e101;
                                        recordingAudio.addEventListener('timeupdate', function resetTime() {
                                            recordingAudio.currentTime = originalTime;
                                            recordingAudio.removeEventListener('timeupdate', resetTime);
                                        }, { once: true });
                                    }
                                };
                                recordingAudio.addEventListener('loadedmetadata', fixDuration, { once: true });
                                recordingAudio.addEventListener('canplay', fixDuration, { once: true });
                                if (recordingAudio.readyState >= 1) {
                                    setTimeout(fixDuration, 100);
                                }
                            }
                        }
                    });
                } else {
                    // Clean up recording markdown editor when leaving recording view
                    if (recordingMarkdownEditorInstance.value) {
                        recordingMarkdownEditorInstance.value.toTextArea();
                        recordingMarkdownEditorInstance.value = null;
                    }
                }
            });

            watch(audioBlobURL, (newURL) => {
                if (newURL) {
                    nextTick(() => {
                        const recordingAudio = document.querySelector('audio[src*="blob:"]');
                        if (recordingAudio) {
                            const fixDuration = () => {
                                if (!isFinite(recordingAudio.duration) || recordingAudio.duration === 0) {
                                    const originalTime = recordingAudio.currentTime;
                                    recordingAudio.currentTime = 1e101;
                                    recordingAudio.addEventListener('timeupdate', function resetTime() {
                                        recordingAudio.currentTime = originalTime;
                                        recordingAudio.removeEventListener('timeupdate', resetTime);
                                    }, { once: true });
                                }
                            };
                            
                            // Multiple events to ensure we catch the duration
                            recordingAudio.addEventListener('loadedmetadata', fixDuration, { once: true });
                            recordingAudio.addEventListener('loadeddata', fixDuration, { once: true });
                            recordingAudio.addEventListener('canplay', fixDuration, { once: true });
                            
                            // Also try after a delay
                            if (recordingAudio.readyState >= 1) {
                                setTimeout(fixDuration, 100);
                            }
                        }
                    });
                }
            });

            watch(playerVolume, (newVolume) => {
                const audioElements = document.querySelectorAll('audio');
                audioElements.forEach(audio => {
                    if (audio.volume !== newVolume) {
                        audio.volume = newVolume;
                    }
                });
            });
            
            // Watch for search query changes
            watch(searchQuery, (newQuery) => {
                debouncedSearch(newQuery);
            });
            
            // Auto-apply filters when they change (except text query which is debounced)
            watch(filterTags, () => {
                applyAdvancedFilters();
            }, { deep: true });
            
            watch(filterDatePreset, () => {
                applyAdvancedFilters();
            });
            
            watch(filterDateRange, () => {
                applyAdvancedFilters();
            }, { deep: true });
            
            // Debounce text query changes
            watch(filterTextQuery, (newValue) => {
                clearTimeout(searchDebounceTimer.value);
                searchDebounceTimer.value = setTimeout(() => {
                    applyAdvancedFilters();
                }, 300);
            });

            // --- Configuration Loading ---
            const loadConfiguration = async () => {
                try {
                    const response = await fetch('/api/config');
                    if (response.ok) {
                        const config = await response.json();
                        maxFileSizeMB.value = config.max_file_size_mb || 250;
                        chunkingEnabled.value = config.chunking_enabled !== undefined ? config.chunking_enabled : true;
                        chunkingMode.value = config.chunking_mode || 'size';
                        chunkingLimit.value = config.chunking_limit || 20;
                        chunkingLimitDisplay.value = config.chunking_limit_display || '20MB';
                        recordingDisclaimer.value = config.recording_disclaimer || '';
                        console.log(`Loaded configuration: max size ${maxFileSizeMB.value}MB, chunking ${chunkingEnabled.value ? 'enabled' : 'disabled'} (${chunkingLimitDisplay.value})`);
                    } else {
                        console.warn('Failed to load configuration, using default values');
                    }
                } catch (error) {
                    console.error('Error loading configuration:', error);
                    // Keep default values on error
                }
            };

            const initializeSummaryMarkdownEditor = () => {
                if (!summaryMarkdownEditor.value) return;

                try {
                    summaryMarkdownEditorInstance.value = new EasyMDE({
                        element: summaryMarkdownEditor.value,
                        spellChecker: false,
                        autofocus: true,
                        placeholder: "Enter summary in Markdown format...",
                        initialValue: selectedRecording.value?.summary || '',
                        status: false,
                        toolbar: [
                            "bold", "italic", "heading", "|",
                            "quote", "unordered-list", "ordered-list", "|",
                            "link", "image", "|",
                            "preview", "side-by-side", "fullscreen", "|",
                            "guide"
                        ],
                        previewClass: ["editor-preview", "notes-preview"],
                        theme: isDarkMode.value ? "dark" : "light"
                    });
                    
                    // Add auto-save functionality
                    summaryMarkdownEditorInstance.value.codemirror.on('change', () => {
                        if (autoSaveTimer.value) {
                            clearTimeout(autoSaveTimer.value);
                        }
                        autoSaveTimer.value = setTimeout(() => {
                            autoSaveSummary();
                        }, autoSaveDelay);
                    });
                } catch (error) {
                    console.error('Failed to initialize summary markdown editor:', error);
                    editingSummary.value = true;
                }
            };

            const pollInboxRecordings = async () => {
                try {
                    const response = await fetch('/api/inbox_recordings');
                    if (!response.ok) {
                        // Silently fail, as this is a background task
                        return;
                    }
                    const inboxRecordings = await response.json();

                    if (inboxRecordings && inboxRecordings.length > 0) {
                        inboxRecordings.forEach(recording => {
                            // Add to main recordings list if it's not there already
                            const existingRecording = recordings.value.find(r => r.id === recording.id);
                            if (!existingRecording) {
                                recordings.value.unshift(recording);
                                totalRecordings.value++; // Update total count
                            }

                            // Check if this recording is already in the upload queue or currently being processed
                            const existingItem = uploadQueue.value.find(item => item.recordingId === recording.id);
                            const isCurrentlyProcessing = currentlyProcessingFile.value && currentlyProcessingFile.value.recordingId === recording.id;
                            
                            // Don't add if it's already in queue or being processed
                            if (existingItem || isCurrentlyProcessing) {
                                // Update status if the existing item status has changed
                                if (existingItem && existingItem.status !== recording.status) {
                                    console.log(`Updating status for existing recording ${recording.original_filename}: ${existingItem.status} -> ${recording.status}`);
                                }
                                return;
                            }
                            
                            // Only add recordings that are still processing (not completed)
                            if (recording.status === 'COMPLETED') {
                                console.log(`Skipping completed inbox recording: ${recording.original_filename}`);
                                return;
                            }
                            
                            console.log(`Found new inbox recording: ${recording.original_filename} (status: ${recording.status})`);
                            
                            // Don't add recordings that are already being handled by main processing  
                            if (recording.status === 'SUMMARIZING' && isProcessingActive.value) {
                                console.log(`Skipping ${recording.original_filename} - already in summarization phase of main processing`);
                                return;
                            }
                            
                            // Add it to the queue to be displayed in the progress modal
                            const inboxItem = {
                                file: { 
                                    name: recording.original_filename || `Recording ${recording.id}`,
                                    size: recording.file_size || 0
                                },
                                status: 'pending', // It's already being processed on backend
                                recordingId: recording.id,
                                clientId: `inbox-${recording.id}-${Date.now()}`,
                                error: null,
                                isReprocessing: true, // Use reprocessing logic to poll for status
                                reprocessType: 'transcription'
                            };
                            uploadQueue.value.unshift(inboxItem);

                            // If nothing is currently being processed, make this the active item for the main progress bar
                            if (!isProcessingActive.value && !currentlyProcessingFile.value) {
                                currentlyProcessingFile.value = inboxItem;
                            }
                            
                            // Show progress modal if it's hidden
                            progressPopupMinimized.value = false;
                            progressPopupClosed.value = false;

                            // Start polling its status
                            startReprocessingPoll(recording.id);
                        });
                    }
                } catch (error) {
                    console.error('Error polling for inbox recordings:', error);
                }
            };

            // --- Audio Format Detection ---
            const detectSupportedAudioFormats = () => {
                const formats = [
                    'audio/webm;codecs=opus',
                    'audio/webm;codecs=vp9',
                    'audio/webm',
                    'audio/mp4;codecs=mp4a.40.2',
                    'audio/mp4',
                    'audio/ogg;codecs=opus',
                    'audio/wav'
                ];

                const supportedFormats = formats.filter(format => MediaRecorder.isTypeSupported(format));
                console.log('Supported audio recording formats:', supportedFormats);
                
                if (supportedFormats.length === 0) {
                    console.warn('No optimized audio formats supported, will use browser default');
                }
                
                return supportedFormats;
            };

            // --- System Audio Detection ---
            const detectSystemAudioCapabilities = async () => {
                systemAudioSupported.value = false;
                canRecordSystemAudio.value = false;
                systemAudioError.value = '';

                // Check if getDisplayMedia is available
                if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
                    systemAudioError.value = 'getDisplayMedia API not supported';
                    return;
                }

                try {
                    // Test if we can request system audio (this will prompt user)
                    // We'll do this only when user actually tries to record
                    systemAudioSupported.value = true;
                    canRecordSystemAudio.value = true;
                    systemAudioError.value = '';
                } catch (error) {
                    systemAudioError.value = error.message;
                    console.warn('System audio detection failed:', error);
                }
            };

            const detectBrowser = () => {
                const userAgent = navigator.userAgent;
                if (userAgent.includes("Firefox")) {
                    browser.value = "firefox";
                } else if (userAgent.includes("Chrome")) {
                    browser.value = "chrome";
                } else if (userAgent.includes("Safari")) {
                    browser.value = "safari";
                } else if (userAgent.includes("MSIE") || userAgent.includes("Trident/")) {
                    browser.value = "ie";
                } else {
                    browser.value = "unknown";
                }
            };

            // --- Lifecycle ---
            onMounted(async () => {
                const loader = document.getElementById('loader');
                const app = document.getElementById('app');
                if (loader) {
                    loader.style.opacity = '0';
                    setTimeout(() => {
                        loader.style.display = 'none';
                    }, 500);
                }
                if (app) {
                    app.style.opacity = '1';
                }
                const appDiv = document.getElementById('app');
                if (appDiv) {
                    const asrFlag = appDiv.dataset.useAsrEndpoint;
                    useAsrEndpoint.value = asrFlag === 'True' || asrFlag === 'true';
                    currentUserName.value = appDiv.dataset.currentUserName || '';
                }
                
                // Load configuration first
                await loadConfiguration();
                
                // Detect system audio capabilities
                await detectSystemAudioCapabilities();

                detectBrowser();
                
                // Check if language was changed in account settings
                if (localStorage.getItem('ui_language_changed') === 'true') {
                    localStorage.removeItem('ui_language_changed');
                    // Force reload to apply new language
                    window.location.reload();
                    return;
                }
                
                // i18n is already initialized before Vue app creation
                if (window.i18n) {
                    currentLanguage.value = window.i18n.getLocale();
                    availableLanguages.value = window.i18n.getAvailableLocales();
                    const lang = availableLanguages.value.find(l => l.code === currentLanguage.value);
                    currentLanguageName.value = lang ? lang.nativeName : 'English';
                    
                    // Listen for locale changes
                    window.addEventListener('localeChanged', (event) => {
                        currentLanguage.value = event.detail.locale;
                        const lang = availableLanguages.value.find(l => l.code === currentLanguage.value);
                        currentLanguageName.value = lang ? lang.nativeName : 'English';
                    });
                }
                
                loadRecordings();
                loadTags();
                initializeDarkMode();
                initializeColorScheme();
                
                const savedVolume = localStorage.getItem('playerVolume');
                if (savedVolume !== null) {
                    playerVolume.value = parseFloat(savedVolume);
                }
                
                const savedTranscriptionViewMode = localStorage.getItem('transcriptionViewMode');
                if (savedTranscriptionViewMode) {
                    transcriptionViewMode.value = savedTranscriptionViewMode;
                }
                
                // Load saved column widths
                const savedLeftWidth = localStorage.getItem('transcriptColumnWidth');
                const savedRightWidth = localStorage.getItem('summaryColumnWidth');
                if (savedLeftWidth && savedRightWidth) {
                    leftColumnWidth.value = parseFloat(savedLeftWidth);
                    rightColumnWidth.value = parseFloat(savedRightWidth);
                }
                
                const updateMobileStatus = () => {
                    windowWidth.value = window.innerWidth;
                };
                
                window.addEventListener('resize', updateMobileStatus);
                updateMobileStatus();

                // Start polling for inbox recordings
                setInterval(pollInboxRecordings, 10000); // Poll every 10 seconds

                const handleEsc = (e) => {
                    if (e.key === 'Escape') {
                        if (showColorSchemeModal.value) {
                            closeColorSchemeModal();
                        }
                        if (showEditModal.value) {
                            cancelEdit();
                        }
                        if (showDeleteModal.value) {
                            cancelDelete();
                        }
                        if (showSortOptions.value) {
                            showSortOptions.value = false;
                        }
                    }
                };
                document.addEventListener('keydown', handleEsc);

                // Click away handler for dropdowns
                const handleClickAway = (e) => {
                    // Close user menu if clicking outside
                    if (isUserMenuOpen.value) {
                        // Check if we clicked within the user menu area (button or dropdown)
                        const userMenuButton = e.target.closest('button[class*="flex items-center gap"]');
                        const userMenuDropdown = e.target.closest('div[class*="absolute right-0"]');
                        
                        // Check if the click was on the user menu button specifically  
                        const isUserMenuButtonClick = userMenuButton && userMenuButton.querySelector('i.fa-user-circle');
                        
                        // If we didn't click on the user menu button or dropdown, close it
                        if (!isUserMenuButtonClick && !userMenuDropdown) {
                            isUserMenuOpen.value = false;
                        }
                    }
                    
                    // Close speaker suggestions if clicking outside in the modal
                    if (showSpeakerModal.value) {
                        // If not clicking on an input field or suggestion dropdown
                        const clickedOnInput = e.target.closest('input[type="text"]');
                        const clickedOnSuggestion = e.target.closest('[class*="absolute z-10"]');
                        
                        if (!clickedOnInput && !clickedOnSuggestion) {
                            // Close all suggestion dropdowns
                            Object.keys(speakerSuggestions.value).forEach(speakerId => {
                                speakerSuggestions.value[speakerId] = [];
                            });
                        }
                    }
                };
                document.addEventListener('click', handleClickAway);
            });

            return {
                // Core State
                currentView, dragover, recordings, selectedRecording, selectedTab, searchQuery,
                isLoadingRecordings, globalError, maxFileSizeMB, chunkingEnabled, chunkingMode, chunkingLimit, chunkingLimitDisplay, sortBy,
                showAdvancedFilters, filterTags, filterDateRange, filterDatePreset, filterTextQuery,
                
                // Pagination State
                currentPage, perPage, totalRecordings, totalPages, hasNextPage, hasPrevPage, isLoadingMore,
                
                // UI State
                browser,
                isSidebarCollapsed, searchTipsExpanded, isUserMenuOpen, isDarkMode, currentColorScheme, 
                showColorSchemeModal, windowWidth, isMobileScreen,
                mobileTab, isMetadataExpanded,
                
                // i18n State
                currentLanguage, currentLanguageName, availableLanguages, showLanguageMenu,
                
                // Upload State
                uploadQueue, currentlyProcessingFile, processingProgress, processingMessage,
                isProcessingActive, progressPopupMinimized, progressPopupClosed,
                totalInQueue, completedInQueue, finishedFilesInQueue, clearCompletedUploads,
                
                // Audio Recording
                isRecording, canRecordAudio, canRecordSystemAudio, systemAudioSupported, systemAudioError, audioBlobURL, recordingTime, recordingNotes, visualizer, micVisualizer, systemVisualizer, recordingMode,
                recordingDisclaimer, showRecordingDisclaimerModal, acceptRecordingDisclaimer, cancelRecordingDisclaimer,
                showAdvancedOptions, uploadLanguage, uploadMinSpeakers, uploadMaxSpeakers,
                asrLanguage, asrMinSpeakers, asrMaxSpeakers,
                availableTags, selectedTagIds, selectedTags, uploadTagSearchFilter, filteredAvailableTagsForUpload, onTagSelected, addTagToSelection, removeTagFromSelection,
                showSystemAudioHelp,
                
                // Modal State
                showEditModal, showDeleteModal, showResetModal, editingRecording, recordingToDelete,
                showEditTagsModal, selectedNewTagId, tagSearchFilter, filteredAvailableTagsForModal, editRecordingTags, closeEditTagsModal, addTagToRecording, removeTagFromRecording,
                getRecordingTags, getAvailableTagsForRecording, filterByTag, clearTagFilter,
                applyAdvancedFilters, clearAllFilters,
                showTextEditorModal, showAsrEditorModal, editingTranscriptionContent, editingSegments, availableSpeakers,
                
                // Inline Editing
                editingParticipants, editingMeetingDate, editingSummary, editingNotes,
                
                // Markdown Editor
                notesMarkdownEditor, markdownEditorInstance, summaryMarkdownEditor, summaryMarkdownEditorInstance, recordingNotesEditor,
                
                // Transcription
                transcriptionViewMode, legendExpanded, highlightedSpeaker, processedTranscription,
                
                // Chat
                showChat, chatMessages, chatInput, isChatLoading, chatMessagesRef,
                
                // Audio Player
                playerVolume,
                
                // Column Resizing
                leftColumnWidth, rightColumnWidth, isResizing,
                
                // App Configuration
                useAsrEndpoint, currentUserName,
                
                // Computed
                filteredRecordings, groupedRecordings, activeRecordingMetadata, datePresetOptions, languageOptions,
                
                // Color Schemes
                colorSchemes,
                
                // Methods
                setGlobalError, formatFileSize, formatDisplayDate, formatStatus, getStatusClass, formatTime,
                t, tc, changeLanguage,
                toggleDarkMode, applyColorScheme, initializeColorScheme, openColorSchemeModal, 
                closeColorSchemeModal, selectColorScheme, resetColorScheme,
                toggleSidebar, switchToUploadView, selectRecording,
                handleDragOver, handleDragLeave, handleDrop, handleFileSelect, addFilesToQueue,
                startRecording, stopRecording, uploadRecordedAudio, discardRecording,
                loadRecordings, loadMoreRecordings, performSearch, debouncedSearch, saveMetadata, editRecording, cancelEdit, saveEdit,
                confirmDelete, cancelDelete, deleteRecording,
                toggleEditParticipants, toggleEditMeetingDate, toggleEditSummary, cancelEditSummary, saveEditSummary, toggleEditNotes, 
                cancelEditNotes, saveEditNotes, initializeMarkdownEditor, saveInlineEdit, clickToEditNotes, clickToEditSummary,
                autoSaveNotes, autoSaveSummary,
                sendChatMessage, isChatScrolledToBottom, scrollChatToBottom, startColumnResize, handleChatKeydown, seekAudio, seekAudioFromEvent, onPlayerVolumeChange,
                showToast, copyMessage, copyTranscription, copySummary, copyNotes,
                downloadSummary, downloadNotes, downloadChat,
                toggleInbox, toggleHighlight,
                toggleTranscriptionViewMode,
                reprocessTranscription,
                reprocessSummary,
                generateSummary,
                resetRecordingStatus,
                confirmReset,
                cancelReset,
                executeReset,
                openTranscriptionEditor,
                openTextEditorModal,
                closeTextEditorModal,
                saveTranscription,
                openAsrEditorModal,
                closeAsrEditorModal,
                saveAsrTranscription,
                adjustTime,
                filterSpeakers,
                openSpeakerSuggestions,
                closeSpeakerSuggestions,
                selectSpeaker,
                addSegment,
                removeSegment,
                showReprocessModal,
                reprocessType,
                reprocessRecording,
                cancelReprocess,
                executeReprocess,
                asrReprocessOptions,
                showSpeakerModal,
                showShareModal,
                recordingToShare,
                shareOptions,
                generatedShareLink,
                existingShareDetected,
                userShares,
                isLoadingShares,
                showSharesListModal,
                shareToDelete,
                showShareDeleteModal,
                confirmDeleteShare,
                cancelDeleteShare,
                speakerMap,
                speakerDisplayMap,
                modalSpeakers,
                regenerateSummaryAfterSpeakerUpdate,
                identifiedSpeakers,
                identifiedSpeakersInOrder,
                hasSpeakerNames,
                openSpeakerModal,
                closeSpeakerModal,
                saveSpeakerNames,
                highlightedTranscript,
                highlightedSpeaker,
                highlightSpeakerInTranscript,
                focusSpeaker,
                blurSpeaker,
                clearSpeakerHighlight,
                currentSpeakerGroupIndex,
                speakerGroups,
                navigateToNextSpeakerGroup,
                navigateToPrevSpeakerGroup,
                speakerSuggestions,
                loadingSuggestions,
                activeSpeakerInput,
                searchSpeakers,
                selectSpeakerSuggestion,
                closeSpeakerSuggestionsOnClick,
                autoIdentifySpeakers,
                isAutoIdentifying,
                formatDuration,
                openShareModal,
                closeShareModal,
                createShare,
                openSharesList,
                closeSharesList,
                updateShare,
                deleteShare,
                copyShareLink,
                
                // Recording Size Monitoring
                estimatedFileSize,
                fileSizeWarningShown,
                recordingQuality,
                actualBitrate,
                maxRecordingMB,
                sizeCheckInterval,
                updateFileSizeEstimate,
                startSizeMonitoring,
                stopSizeMonitoring
            }
        },
        delimiters: ['${', '}']
    });
    
    // Add t function as a global property BEFORE mounting so it's available in templates immediately
    app.config.globalProperties.t = safeT;
    
    // Also add tc for pluralization
    app.config.globalProperties.tc = (key, count, params = {}) => {
        if (!window.i18n || !window.i18n.tc) {
            return key;
        }
        return window.i18n.tc(key, count, params);
    };
    
    // Provide t and tc to the app
    app.provide('t', safeT);
    app.provide('tc', (key, count, params = {}) => {
        if (!window.i18n || !window.i18n.tc) {
            return key;
        }
        return window.i18n.tc(key, count, params);
    });
    
    // Mount the app
    app.mount('#app');
});
