const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue

// Wait for the DOM to be fully loaded before mounting the Vue app
document.addEventListener('DOMContentLoaded', () => {
    // CSRF Protection for Fetch API
    const csrfToken = document.querySelector('meta[name="csrf-token"]').getAttribute('content');
    const originalFetch = window.fetch;
    window.fetch = function(url, options) {
        const newOptions = { ...options };
        newOptions.headers = {
            'X-CSRFToken': csrfToken,
            ...newOptions.headers
        };
        return originalFetch(url, newOptions);
    };

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

    createApp({
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

            // --- Enhanced Search & Organization State ---
            const sortBy = ref('created_at'); // 'created_at' or 'meeting_date'

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

            // --- Modal State ---
            const showEditModal = ref(false);
            const showDeleteModal = ref(false);
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
            const userShares = ref([]);
            const isLoadingShares = ref(false);
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

            // --- Inline Editing State ---
            const editingParticipants = ref(false);
            const editingMeetingDate = ref(false);
            const editingSummary = ref(false);
            const editingNotes = ref(false);
            
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

            const filteredRecordings = computed(() => {
                if (!searchQuery.value.trim()) {
                    return recordings.value;
                }
                
                const query = searchQuery.value.toLowerCase().trim();
                
                // Check for date search syntax (date:YYYY-MM-DD or date:today, date:yesterday, etc.)
                const dateMatch = query.match(/date:(\S+)/);
                if (dateMatch) {
                    const dateQuery = dateMatch[1];
                    return recordings.value.filter(recording => {
                        const recordingDate = getDateForSorting(recording);
                        if (!recordingDate) return false;
                        
                        if (dateQuery === 'today') {
                            return isToday(recordingDate);
                        } else if (dateQuery === 'yesterday') {
                            return isYesterday(recordingDate);
                        } else if (dateQuery === 'thisweek') {
                            return isThisWeek(recordingDate);
                        } else if (dateQuery === 'lastweek') {
                            return isLastWeek(recordingDate);
                        } else if (dateQuery === 'thismonth') {
                            return isThisMonth(recordingDate);
                        } else if (dateQuery === 'lastmonth') {
                            return isLastMonth(recordingDate);
                        } else if (/^\d{4}-\d{2}-\d{2}$/.test(dateQuery)) {
                            // Exact date match
                            const searchDate = new Date(dateQuery + 'T00:00:00');
                            return isSameDay(recordingDate, searchDate);
                        } else if (/^\d{4}-\d{2}$/.test(dateQuery)) {
                            // Month match (YYYY-MM)
                            const [year, month] = dateQuery.split('-');
                            return recordingDate.getFullYear() === parseInt(year) && 
                                   recordingDate.getMonth() === parseInt(month) - 1;
                        } else if (/^\d{4}$/.test(dateQuery)) {
                            // Year match
                            return recordingDate.getFullYear() === parseInt(dateQuery);
                        }
                        return false;
                    });
                }
                
                // Regular text search
                return recordings.value.filter(recording => {
                    return (
                        (recording.title && recording.title.toLowerCase().includes(query)) ||
                        (recording.participants && recording.participants.toLowerCase().includes(query)) ||
                        (recording.transcription && recording.transcription.toLowerCase().includes(query)) ||
                        (recording.notes && recording.notes.toLowerCase().includes(query))
                    );
                });
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
                    // Use a more specific and stylish class structure with color
                    return `<span class="speaker-tag ${colorClass} ${isHighlighted ? 'speaker-highlight' : ''}" data-speaker-id="${speakerId}">${match}</span>`;
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
                    { title: 'Today', items: groups.today },
                    { title: 'Yesterday', items: groups.yesterday },
                    { title: 'This Week', items: groups.thisWeek },
                    { title: 'Last Week', items: groups.lastWeek },
                    { title: 'This Month', items: groups.thisMonth },
                    { title: 'Last Month', items: groups.lastMonth },
                    { title: 'Older', items: groups.older }
                ].filter(g => g.items.length > 0);
            });

            const totalInQueue = computed(() => uploadQueue.value.length);
            const completedInQueue = computed(() => uploadQueue.value.filter(item => item.status === 'completed' || item.status === 'failed').length);
            const finishedFilesInQueue = computed(() => uploadQueue.value.filter(item => ['completed', 'failed'].includes(item.status)));

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
                    // JSON format - extract speakers from segments
                    const speakers = new Set();
                    transcriptionData.forEach(segment => {
                        if (segment.speaker && String(segment.speaker).trim()) {
                            speakers.add(segment.speaker);
                        }
                    });
                    return Array.from(speakers).sort(); // Sort for consistent color mapping
                } else if (typeof transcription === 'string') {
                    // Plain text format - use regex to find speaker patterns
                    const speakerRegex = /\[([^\]]+)\]:/g;
                    const speakers = new Set();
                    let match;
                    while ((match = speakerRegex.exec(transcription)) !== null) {
                        const speaker = match[1].trim();
                        if (speaker) {
                            speakers.add(speaker);
                        }
                    }
                    return Array.from(speakers).sort();
                }
                return [];
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
                        speaker: segment.speaker,
                        sentence: segment.sentence,
                        startTime: segment.start_time || segment.startTime,
                        endTime: segment.end_time || segment.endTime,
                        color: speakerColors[segment.speaker] || 'speaker-color-1'
                    }));

                    const processedSimpleSegments = [];
                    let lastSpeaker = null;
                    simpleSegments.forEach(segment => {
                        processedSimpleSegments.push({
                            ...segment,
                            showSpeaker: segment.speaker !== lastSpeaker
                        });
                        lastSpeaker = segment.speaker;
                    });

                    const bubbleRows = [];
                    let lastBubbleSpeaker = null;
                    simpleSegments.forEach(segment => {
                        if (bubbleRows.length === 0 || segment.speaker !== lastBubbleSpeaker) {
                            bubbleRows.push({
                                speaker: segment.speaker,
                                color: segment.color,
                                isMe: segment.speaker && (typeof segment.speaker === 'string') && segment.speaker.toLowerCase().includes('me'),
                                bubbles: []
                            });
                            lastBubbleSpeaker = segment.speaker;
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
                    speakerList.forEach((speaker, index) => {
                        speakerColors[speaker] = `speaker-color-${(index % 8) + 1}`;
                    });

                    const segments = [];
                    const lines = transcription.split('\n');
                    let currentSpeaker = null;
                    let currentText = '';

                    for (const line of lines) {
                        const speakerMatch = line.match(/^\[([^\]]+)\]:\s*(.*)$/);
                        if (speakerMatch) {
                            if (currentSpeaker && currentText.trim()) {
                                segments.push({
                                    speaker: currentSpeaker,
                                    sentence: currentText.trim(),
                                    color: speakerColors[currentSpeaker] || 'speaker-color-1'
                                });
                            }
                            currentSpeaker = speakerMatch[1];
                            currentText = speakerMatch[2];
                        } else if (currentSpeaker && line.trim()) {
                            currentText += ' ' + line.trim();
                        } else if (!currentSpeaker && line.trim()) {
                            segments.push({
                                speaker: null,
                                sentence: line.trim(),
                                color: 'speaker-color-1'
                            });
                        }
                    }

                    if (currentSpeaker && currentText.trim()) {
                        segments.push({
                            speaker: currentSpeaker,
                            sentence: currentText.trim(),
                            color: speakerColors[currentSpeaker] || 'speaker-color-1'
                        });
                    }

                    const simpleSegments = [];
                    let lastSpeaker = null;
                    segments.forEach(segment => {
                        simpleSegments.push({
                            ...segment,
                            showSpeaker: segment.speaker !== lastSpeaker,
                            sentence: segment.sentence || segment.text 
                        });
                        lastSpeaker = segment.speaker;
                    });

                    const bubbleRows = [];
                    let currentRow = null;
                    segments.forEach(segment => {
                        if (!currentRow || currentRow.speaker !== segment.speaker) {
                            if (currentRow) bubbleRows.push(currentRow);
                            currentRow = {
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
                            name: speaker,
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
                return status.charAt(0).toUpperCase() + status.slice(1).toLowerCase();
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
            
            const openSpeakerModal = () => {
                // Clear any existing speaker map data first
                speakerMap.value = {};
                
                // Initialize speaker map only for speakers in the current recording
                speakerMap.value = identifiedSpeakers.value.reduce((acc, speaker, index) => {
                    acc[speaker] = { 
                        name: '', 
                        isMe: false,
                        color: `speaker-color-${(index % 8) + 1}` // Assign same colors as transcription view
                    };
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
    
            const highlightSpeakerInTranscript = (speakerId) => {
                highlightedSpeaker.value = speakerId;
                // Scroll to the first instance in the transcript when a speaker is focused
                if (speakerId) {
                    nextTick(() => {
                        // Try both the modal transcript and the main transcript
                        const modalTranscript = document.querySelector('div.speaker-modal-transcript');
                        const mainTranscript = document.querySelector('.transcription-simple-view, .transcription-with-speakers, .transcription-content');                        
                        const transcriptContainer = modalTranscript || mainTranscript;
                        if (transcriptContainer) {
                            const firstInstance = transcriptContainer.querySelector(`[data-speaker-id="${speakerId}"]`);
                            if (firstInstance) {
                                firstInstance.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            }
                        }
                    });
                }
            };

            // Enhanced speaker highlighting with focus/blur events for text inputs
            const focusSpeaker = (speakerId) => {
                highlightSpeakerInTranscript(speakerId);
            };

            const blurSpeaker = () => {
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

                    // Check if it's an audio file or has AMR extension
                    const isAudioFile = fileObject && (
                        fileObject.type.startsWith('audio/') || 
                        fileObject.name.toLowerCase().endsWith('.amr') ||
                        fileObject.name.toLowerCase().endsWith('.3gp') ||
                        fileObject.name.toLowerCase().endsWith('.3gpp')
                    );
                    
                    if (isAudioFile) {
                        // Check OpenAI-specific 25MB limit when not using ASR endpoint
                        if (!useAsrEndpoint.value && fileObject.size > 25 * 1024 * 1024) {
                            setGlobalError(`File "${fileObject.name}" exceeds OpenAI's 25MB limit. Please use a smaller file or enable the ASR endpoint for larger files.`);
                            continue;
                        }
                        
                        // Check general file size limit
                        if (fileObject.size > maxFileSizeMB.value * 1024 * 1024) {
                            setGlobalError(`File "${fileObject.name}" exceeds the maximum size of ${maxFileSizeMB.value} MB and was skipped.`);
                            continue;
                        }
                        
                        const clientId = `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
                        uploadQueue.value.push({
                            file: fileObject, 
                            notes: notes,
                            status: 'queued', 
                            recordingId: null, 
                            clientId: clientId, 
                            error: null
                        });
                        filesAdded++;
                    } else if (fileObject) {
                        setGlobalError(`Invalid file type "${fileObject.name}". Only audio files (including AMR) are accepted. File skipped.`);
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
                    nextFileItem.status = 'uploading';
                    processingMessage.value = 'Preparing upload...';
                    processingProgress.value = 5;

                    try {
                        const formData = new FormData();
                        formData.append('file', nextFileItem.file);
                        if (nextFileItem.notes) {
                            formData.append('notes', nextFileItem.notes);
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
                    if (!currentlyProcessingFile.value || currentlyProcessingFile.value.clientId !== fileItem.clientId || ['completed', 'failed'].includes(fileItem.status)) {
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

                        fileItem.status = data.status;
                        fileItem.file.name = data.title || data.original_filename;

                        if (data.status === 'COMPLETED') {
                            console.log(`Processing COMPLETED for ${fileItem.file.name} (ID: ${recordingId})`);
                            processingMessage.value = 'Processing complete!';
                            processingProgress.value = 100;
                            fileItem.status = 'completed';
                            clearInterval(pollInterval.value);
                            pollInterval.value = null;
                            resetCurrentFileProcessingState();
                            isProcessingActive.value = false;
                            await nextTick();
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
                            processingMessage.value = 'Transcription in progress...';
                            processingProgress.value = Math.round(Math.min(65, processingProgress.value + Math.random() * 5));
                        } else if (data.status === 'SUMMARIZING') {
                            processingMessage.value = 'Generating title & summary...';
                            processingProgress.value = Math.round(Math.min(95, processingProgress.value + Math.random() * 5));
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
            const loadRecordings = async () => {
                globalError.value = null;
                isLoadingRecordings.value = true;
                try {
                    const response = await fetch('/recordings');
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to load recordings');
                    recordings.value = data;

                    const lastRecordingId = localStorage.getItem('lastSelectedRecordingId');
                    if (lastRecordingId) {
                        const recordingToSelect = recordings.value.find(r => r.id == lastRecordingId);
                        if (recordingToSelect) {
                            selectRecording(recordingToSelect);
                        }
                    }

                    const incompleteRecordings = recordings.value.filter(r => ['PENDING', 'PROCESSING', 'SUMMARIZING'].includes(r.status));
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
                    recordings.value = [];
                } finally {
                    isLoadingRecordings.value = false;
                }
            };

            // --- Audio Recording ---
            const startRecording = async (mode = 'microphone') => {
                recordingMode.value = mode;
                
                try {
                    // Reset state
                    audioChunks.value = [];
                    audioBlobURL.value = null;
                    recordingNotes.value = '';
                    activeStreams.value = [];

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
                        audioContext.value = new (window.AudioContext || window.webkitAudioContext)();
                        
                        const micSource = audioContext.value.createMediaStreamSource(micStream);
                        const systemSource = audioContext.value.createMediaStreamSource(systemStream);
                        const destination = audioContext.value.createMediaStreamDestination();
                        
                        micSource.connect(destination);
                        systemSource.connect(destination);
                        
                        // The destination stream contains the mixed audio
                        combinedStream = destination.stream;

                        // We also need to add the video track from the system stream to keep the browser's "sharing this screen" UI active.
                        // This video track will not be recorded.
                        const videoTrack = systemStream.getVideoTracks()[0];
                        if (videoTrack) {
                            combinedStream.addTrack(videoTrack);
                        }
                        
                        showToast('Recording both microphone and system audio', 'fa-microphone');
                    } else if (systemStream) {
                        combinedStream = systemStream;
                        showToast('Recording system audio only', 'fa-desktop');
                    } else if (micStream) {
                        combinedStream = micStream;
                        showToast('Recording microphone only', 'fa-microphone');
                    } else {
                        throw new Error('No audio streams available for recording.');
                    }

                    // Setup MediaRecorder
                    mediaRecorder.value = new MediaRecorder(combinedStream);
                    mediaRecorder.value.ondataavailable = event => audioChunks.value.push(event.data);
                    mediaRecorder.value.onstop = () => {
                        const audioBlob = new Blob(audioChunks.value, { type: 'audio/webm' });
                        audioBlobURL.value = URL.createObjectURL(audioBlob);
                        
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

                // Pass notes along with the file
                addFilesToQueue([{ file: recordedFile, notes: recordingNotes.value }]);
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
                
                const themeColor = getComputedStyle(document.documentElement).getPropertyValue('--bg-accent').trim();
                const gradient = canvasCtx.createLinearGradient(0, 0, 0, HEIGHT);
                gradient.addColorStop(0, themeColor);
                gradient.addColorStop(1, 'rgba(255, 255, 255, 0.1)');

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
                // Reset notes to original value if needed
                if (selectedRecording.value) {
                    // You might want to store the original value before editing
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

            const initializeMarkdownEditor = () => {
                if (!notesMarkdownEditor.value) return;
                
                try {
                    markdownEditorInstance.value = new EasyMDE({
                        element: notesMarkdownEditor.value,
                        spellChecker: false,
                        autofocus: true,
                        placeholder: "Enter notes in Markdown format...",
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
                if (chatMessagesRef.value) {
                    chatMessagesRef.value.scrollTop = chatMessagesRef.value.scrollHeight;
                }

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
                                            if (data.delta) {
                                                if (isFirstChunk) {
                                                    isChatLoading.value = false;
                                                    assistantMessage = reactive({ role: 'assistant', content: '', html: '' });
                                                    chatMessages.value.push(assistantMessage);
                                                    isFirstChunk = false;
                                                }
                                                assistantMessage.content += data.delta;
                                                assistantMessage.html = marked.parse(assistantMessage.content);
                                                
                                                await nextTick();
                                                if (chatMessagesRef.value) {
                                                    chatMessagesRef.value.scrollTop = chatMessagesRef.value.scrollHeight;
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
                    if (chatMessagesRef.value) {
                        chatMessagesRef.value.scrollTop = chatMessagesRef.value.scrollHeight;
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

            const openShareModal = (recording) => {
                recordingToShare.value = recording;
                shareOptions.share_summary = true;
                shareOptions.share_notes = true;
                generatedShareLink.value = '';
                showShareModal.value = true;
            };

            const closeShareModal = () => {
                showShareModal.value = false;
                recordingToShare.value = null;
            };

            const createShare = async () => {
                if (!recordingToShare.value) return;
                try {
                    const response = await fetch(`/api/recording/${recordingToShare.value.id}/share`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(shareOptions)
                    });
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to create share link');
                    generatedShareLink.value = data.share_url;
                    showToast('Share link created successfully!', 'fa-check-circle');
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

            const deleteShare = async (shareId) => {
                if (!confirm('Are you sure you want to delete this share? This will revoke access to the public link.')) return;
                try {
                    const response = await fetch(`/api/share/${shareId}`, { method: 'DELETE' });
                    const data = await response.json();
                    if (!response.ok) throw new Error(data.error || 'Failed to delete share');
                    userShares.value = userShares.value.filter(s => s.id !== shareId);
                    showToast('Share deleted successfully.', 'fa-check-circle');
                } catch (error) {
                    setGlobalError(`Failed to delete share: ${error.message}`);
                }
            };

            // --- Watchers ---
            watch(uploadQueue, (newQueue, oldQueue) => {
                if (newQueue.length === 0 && oldQueue.length > 0 && !isProcessingActive.value) {
                    console.log("Upload queue processing finished.");
                    setTimeout(() => progressPopupMinimized.value = true, 1000);
                    setTimeout(() => {
                        if (completedInQueue.value === totalInQueue.value && !isProcessingActive.value) {
                            progressPopupClosed.value = true;
                        }
                    }, 5000);
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

            watch(selectedRecording, (newVal, oldVal) => {
                if (newVal?.id !== oldVal?.id) {
                    chatMessages.value = [];
                    showChat.value = false;
                    selectedTab.value = 'summary';
                    
                    editingParticipants.value = false;
                    editingMeetingDate.value = false;
                    editingSummary.value = false;
                    editingNotes.value = false;
                }
            });

            watch(currentView, (newView) => {
                if (newView === 'recording') {
                    // Initialize recording markdown editor when switching to recording view
                    nextTick(() => {
                        initializeRecordingMarkdownEditor();
                    });
                } else {
                    // Clean up recording markdown editor when leaving recording view
                    if (recordingMarkdownEditorInstance.value) {
                        recordingMarkdownEditorInstance.value.toTextArea();
                        recordingMarkdownEditorInstance.value = null;
                    }
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

            // --- Configuration Loading ---
            const loadConfiguration = async () => {
                try {
                    const response = await fetch('/api/config');
                    if (response.ok) {
                        const config = await response.json();
                        maxFileSizeMB.value = config.max_file_size_mb || 250;
                        console.log(`Loaded max file size: ${maxFileSizeMB.value} MB`);
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
                            }

                            // Check if this recording is already in the upload queue
                            const existingItem = uploadQueue.value.find(item => item.recordingId === recording.id);
                            if (!existingItem) {
                                console.log(`Found new inbox recording: ${recording.original_filename}`);
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
                            }
                        });
                    }
                } catch (error) {
                    console.error('Error polling for inbox recordings:', error);
                }
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
                
                loadRecordings();
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
                        const userMenuButton = e.target.closest('button[class*="flex items-center gap-2"]');
                        const userMenuDropdown = e.target.closest('div[class*="absolute right-0"]');
                        const userMenuContainer = e.target.closest('.relative');
                        
                        // Check if the click was on the user menu button specifically
                        const isUserMenuButtonClick = userMenuButton && userMenuButton.querySelector('i.fa-user-circle');
                        
                        // If we didn't click on the user menu button or dropdown, close it
                        if (!isUserMenuButtonClick && !userMenuDropdown && !userMenuContainer) {
                            isUserMenuOpen.value = false;
                        }
                    }
                };
                document.addEventListener('click', handleClickAway);
            });

            return {
                // Core State
                currentView, dragover, recordings, selectedRecording, selectedTab, searchQuery,
                isLoadingRecordings, globalError, maxFileSizeMB, sortBy,
                
                // UI State
                browser,
                isSidebarCollapsed, searchTipsExpanded, isUserMenuOpen, isDarkMode, currentColorScheme, 
                showColorSchemeModal, windowWidth, isMobileScreen,
                mobileTab, isMetadataExpanded,
                
                // Upload State
                uploadQueue, currentlyProcessingFile, processingProgress, processingMessage,
                isProcessingActive, progressPopupMinimized, progressPopupClosed,
                totalInQueue, completedInQueue, finishedFilesInQueue,
                
                // Audio Recording
                isRecording, canRecordAudio, canRecordSystemAudio, systemAudioSupported, systemAudioError, audioBlobURL, recordingTime, recordingNotes, visualizer, micVisualizer, systemVisualizer, recordingMode,
                showSystemAudioHelp,
                
                // Modal State
                showEditModal, showDeleteModal, showResetModal, editingRecording, recordingToDelete,
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
                filteredRecordings, groupedRecordings, activeRecordingMetadata,
                
                // Color Schemes
                colorSchemes,
                
                // Methods
                setGlobalError, formatFileSize, formatDisplayDate, formatStatus, getStatusClass, formatTime,
                toggleDarkMode, applyColorScheme, initializeColorScheme, openColorSchemeModal, 
                closeColorSchemeModal, selectColorScheme, resetColorScheme,
                toggleSidebar, switchToUploadView, selectRecording,
                handleDragOver, handleDragLeave, handleDrop, handleFileSelect, addFilesToQueue,
                startRecording, stopRecording, uploadRecordedAudio, discardRecording,
                loadRecordings, saveMetadata, editRecording, cancelEdit, saveEdit,
                confirmDelete, cancelDelete, deleteRecording,
                toggleEditParticipants, toggleEditMeetingDate, toggleEditSummary, cancelEditSummary, saveEditSummary, toggleEditNotes, 
                cancelEditNotes, saveEditNotes, initializeMarkdownEditor, saveInlineEdit,
                sendChatMessage, startColumnResize, handleChatKeydown, seekAudio, seekAudioFromEvent, onPlayerVolumeChange,
                showToast, copyMessage, copyTranscription, copySummary, copyNotes,
                toggleInbox, toggleHighlight,
                toggleTranscriptionViewMode,
                reprocessTranscription,
                reprocessSummary,
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
                userShares,
                isLoadingShares,
                showSharesListModal,
                speakerMap,
                regenerateSummaryAfterSpeakerUpdate,
                identifiedSpeakers,
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
                speakerSuggestions,
                loadingSuggestions,
                searchSpeakers,
                selectSpeakerSuggestion,
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
                copyShareLink
            }
        },
        delimiters: ['${', '}']
    }).mount('#app');
});
