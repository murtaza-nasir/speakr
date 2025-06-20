const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue

// Wait for the DOM to be fully loaded before mounting the Vue app
document.addEventListener('DOMContentLoaded', () => {
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
        const currentView = ref('gallery');
        const dragover = ref(false);
        const recordings = ref([]);
        const selectedRecording = ref(null);
        const selectedTab = ref('summary'); // For Summary/Notes tabs
        const selectedMobileTab = ref('transcript'); // For mobile 5-tab layout
        const searchQuery = ref(''); // Search input for filtering recordings

        // --- Multi-Upload State ---
        // Status: 'queued'|'uploading'|'processing'|'summarizing'|'completed'|'failed'
        const uploadQueue = ref([]);
        const currentlyProcessingFile = ref(null);
        const processingProgress = ref(0);
        const processingMessage = ref('');
        const isProcessingActive = ref(false);
        const pollInterval = ref(null);
        const progressPopupMinimized = ref(false);
        const progressPopupClosed = ref(false);

        const showEditModal = ref(false);
        const showDeleteModal = ref(false);
        const showReprocessModal = ref(false);
        const showSpeakerModal = ref(false);
        const editingRecording = ref(null); // Holds a *copy* for the modal
        const recordingToDelete = ref(null);
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
        const highlightedSpeaker = ref(null);
        const transcriptionViewMode = ref('simple'); // 'simple' or 'bubble'
        const legendExpanded = ref(false); // Speaker legend expansion state
        // const autoSaveTimeout = ref(null); // Autosave not implemented for modal
        const isLoadingRecordings = ref(true);
        const globalError = ref(null);
        const maxFileSizeMB = ref(250); // Default, could fetch from config if needed
        const isDarkMode = ref(false); // Dark mode state
        const isSidebarCollapsed = ref(false); // Sidebar state
        const isUserMenuOpen = ref(false); // User dropdown menu state
        const isMobileMenuOpen = ref(false); // Mobile fly-in menu state
        const isMobileUserMenuOpen = ref(false); // Mobile user icon dropdown menu state
        const windowWidth = ref(window.innerWidth); // For reactive screen size
        const useAsrEndpoint = ref(false);
        const currentUserName = ref('');
        const playerVolume = ref(1.0); // Default to full volume

        // --- Audio Recording State ---
        const isRecording = ref(false);
        const mediaRecorder = ref(null);
        const audioChunks = ref([]);
        const audioBlobURL = ref(null);
        const recordingTime = ref(0);
        const recordingInterval = ref(null);
        const canRecordAudio = ref(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
        
        // Inline editing state
        const editingParticipants = ref(false);
        const editingMeetingDate = ref(false); // For desktop
        const editingMobileMeetingDate = ref(false); // For mobile metadata tab
        const editingSummary = ref(false);
        const editingNotes = ref(false);

        // --- Main Column Resize State ---
        const leftMainColumn = ref(null);
        const rightMainColumn = ref(null);
        const mainColumnResizer = ref(null);
        const mainContentColumns = ref(null);
        const isResizingMainColumns = ref(false);
        let initialMainColumnResizeX = 0;
        let initialLeftMainColumnWidthPx = 0;
        const MIN_MAIN_COLUMN_WIDTH_PERCENT = 30; // Min width for left column
        const MAX_MAIN_COLUMN_WIDTH_PERCENT = 70; // Max width for left column (ensures right column gets at least 20%)
        const ABSOLUTE_MIN_COLUMN_PX = 50; // Absolute minimum pixel width for either column

        // --- Vertical Resize State ---
        const transcriptionSection = ref(null);
        const tabSection = ref(null);
        const verticalResizeHandle = ref(null);
        const isResizingVertical = ref(false);
        let initialVerticalResizeY = 0;
        let initialTranscriptionHeightPx = 0;
        let initialTabHeightPx = 0;
        
        // Resizable panels state
        const transcriptionFlex = ref(2);
        const tabsFlex = ref(1);

        // Track initialization status
        const isDesktopLayoutInitialized = ref(false);

        // --- Computed Properties ---
        // Filter recordings based on search query
        const filteredRecordings = computed(() => {
            if (!searchQuery.value.trim()) {
                return recordings.value; // Return all recordings if no search query
            }
            
            const query = searchQuery.value.toLowerCase().trim();
            return recordings.value.filter(recording => {
                // Search in title, participants, and transcription
                return (
                    (recording.title && recording.title.toLowerCase().includes(query)) ||
                    (recording.participants && recording.participants.toLowerCase().includes(query)) ||
                    (recording.transcription && recording.transcription.toLowerCase().includes(query))
                );
            });
        });
        
        // Group recordings by date
        const groupedRecordings = computed(() => {
            const sortedRecordings = [...filteredRecordings.value].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
            // Grouping logic
            const groups = { today: [], yesterday: [], thisWeek: [], older: [] };
            const now = new Date();
            const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
            const yesterdayStart = new Date(todayStart);
            yesterdayStart.setDate(todayStart.getDate() - 1);
            const currentDayOfWeek = now.getDay();
            const daysToSubtract = currentDayOfWeek === 0 ? 6 : currentDayOfWeek - 1;
            const weekStart = new Date(todayStart);
            weekStart.setDate(todayStart.getDate() - daysToSubtract);

            sortedRecordings.forEach(r => {
                 const date = new Date(r.created_at);
                if (date >= todayStart) groups.today.push(r);
                else if (date >= yesterdayStart) groups.yesterday.push(r);
                else if (date >= weekStart) groups.thisWeek.push(r);
                else groups.older.push(r);
            });
             return [
                { title: 'Today', items: groups.today },
                { title: 'Yesterday', items: groups.yesterday },
                { title: 'This Week', items: groups.thisWeek },
                { title: 'Older', items: groups.older }
            ].filter(g => g.items.length > 0);
        });

         const totalInQueue = computed(() => uploadQueue.value.length);
        const completedInQueue = computed(() => uploadQueue.value.filter(item => item.status === 'completed' || item.status === 'failed').length);
        const queuedFiles = computed(() => uploadQueue.value.filter(item => item.status === 'queued'));
        // Filter finished: includes completed and failed
         const finishedFilesInQueue = computed(() => uploadQueue.value.filter(item => ['completed', 'failed'].includes(item.status)));

        const isMobileScreen = computed(() => {
            // Align with Tailwind's 'lg' breakpoint for sidebar visibility and mobile menu toggling
            return windowWidth.value < 1024; 
        });

        const identifiedSpeakers = computed(() => {
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

            // Updated to handle new simplified JSON format (array of segments)
            if (transcriptionData && Array.isArray(transcriptionData)) {
                // If no segments have a speaker, it wasn't diarized. Treat as plain text.
                const wasDiarized = transcriptionData.some(segment => segment.speaker);

                if (!wasDiarized) {
                    const plainText = transcriptionData.map(segment => segment.sentence).join('\n');
                    return {
                        hasDialogue: false,
                        isJson: false, // Treat as plain text for rendering
                        content: plainText,
                        speakers: [],
                        simpleSegments: [],
                        bubbleRows: []
                    };
                }
                
                const speakerColors = {};
                const speakerList = identifiedSpeakers.value; // Use computed property which is already sorted
                speakerList.forEach((speaker, index) => {
                    speakerColors[speaker] = `speaker-color-${(index % 8) + 1}`;
                });

                const simpleSegments = transcriptionData.map(segment => ({
                    speakerId: segment.speaker,
                    speaker: (speakerMap.value[segment.speaker]?.name || segment.speaker),
                    sentence: segment.sentence,
                    startTime: segment.start_time,
                    endTime: segment.end_time,
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
                        startTime: segment.startTime,
                        color: segment.color
                    });
                });

                return {
                    hasDialogue: true,
                    isJson: true,
                    segments: simpleSegments,
                    simpleSegments: processedSimpleSegments,
                    bubbleRows: bubbleRows,
                    speakers: speakerList.map(speaker => ({
                        name: (speakerMap.value[speaker]?.name || speaker),
                        color: speakerColors[speaker] || 'speaker-color-1'
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
                        // Ensure sentence property exists for plain text
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


        // --- Methods ---
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
        // --- End Dark Mode ---

        // --- Sidebar Toggle ---
        const toggleSidebar = () => { // This is for DESKTOP sidebar
            if (!isMobileScreen.value) {
                isSidebarCollapsed.value = !isSidebarCollapsed.value;
                // localStorage.setItem('sidebarCollapsed', isSidebarCollapsed.value);
                
                // Ensure main content layout is preserved after sidebar toggle
                nextTick(() => {
                    // Re-initialize layout if needed to maintain column widths
                    if (leftMainColumn.value && mainContentColumns.value) {
                        // Preserve the current column width percentage
                        const currentWidth = leftMainColumn.value.style.width;
                        if (currentWidth) {
                            // Force a layout recalculation to ensure the resizer still works
                            leftMainColumn.value.style.width = currentWidth;
                        }
                    }
                });
            }
        };

        const toggleMobileMenu = () => {
            isMobileMenuOpen.value = !isMobileMenuOpen.value;
            if (isMobileMenuOpen.value) {
                document.body.classList.add('mobile-menu-open');
            } else {
                document.body.classList.remove('mobile-menu-open');
            }
        };

        const closeMobileMenu = () => {
            if (isMobileMenuOpen.value) {
                isMobileMenuOpen.value = false;
                document.body.classList.remove('mobile-menu-open');
            }
        };
        // --- End Sidebar Toggle ---
        
        // Helper to format date for display (e.g., "May 4, 2025")
        const formatDisplayDate = (dateString) => {
            if (!dateString) return '';
            try {
                // Input is expected as 'YYYY-MM-DD'
                const date = new Date(dateString + 'T00:00:00'); // Add time to avoid timezone issues
                return date.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
            } catch (e) {
                console.error("Error formatting date:", e);
                return dateString; // Return original string if formatting fails
            }
        };

        // Helper for status display
         const formatStatus = (status) => {
             if (!status) return 'Unknown';
             // Capitalize first letter, handle 'SUMMARIZING' specifically
             return status.charAt(0).toUpperCase() + status.slice(1).toLowerCase();
        };

        // Helper for status badge class
        const getStatusClass = (status) => {
            switch(status) {
                case 'PENDING': return 'status-pending';
                case 'PROCESSING': return 'status-processing';
                case 'SUMMARIZING': return 'status-summarizing';
                case 'COMPLETED': return 'status-completed';
                case 'FAILED': return 'status-failed';
                default: return 'status-pending'; // Default or unknown
            }
        };


        const resetCurrentFileProcessingState = () => {
            if (pollInterval.value) clearInterval(pollInterval.value);
            pollInterval.value = null;
            currentlyProcessingFile.value = null;
            processingProgress.value = 0;
            processingMessage.value = '';
        };

        const switchToUploadView = () => {
            currentView.value = 'upload';
            selectedRecording.value = null;
         };

        const switchToGalleryView = async () => {
            currentView.value = 'gallery';
             await loadRecordings(); // Refresh recordings when switching back
        };

        const handleDragLeave = (e) => {
            if (e.relatedTarget && e.currentTarget.contains(e.relatedTarget)) {
                 return;
            }
            dragover.value = false;
        }

        const handleDrop = (e) => {
            dragover.value = false;
            addFilesToQueue(e.dataTransfer.files);
         };

        const handleFileSelect = (e) => {
            addFilesToQueue(e.target.files);
            e.target.value = null; // Reset input
        };

        // --- Queue Management ---
        const addFilesToQueue = (files) => {
            let filesAdded = 0;
            for (const file of files) {
                if (file && file.type.startsWith('audio/')) {
                     if (file.size > maxFileSizeMB.value * 1024 * 1024) {
                        setGlobalError(`File "${file.name}" exceeds the maximum size of ${maxFileSizeMB.value} MB and was skipped.`);
                        continue;
                    }
                     const clientId = `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
                    uploadQueue.value.push({
                        file: file, status: 'queued', recordingId: null, clientId: clientId, error: null
                    });
                    filesAdded++;
                } else if (file) {
                    setGlobalError(`Invalid file type "${file.name}". Only audio files are accepted. File skipped.`);
                }
            }
             if(filesAdded > 0) {
                console.log(`Added ${filesAdded} file(s) to the queue.`);
                progressPopupMinimized.value = false; // Show popup
                progressPopupClosed.value = false; // Reset closed state to make popup reappear
                if (!isProcessingActive.value) {
                    startProcessingQueue();
                }
            }
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
                        // Status is now 'PENDING' on backend, will be updated by poll
                        nextFileItem.status = 'pending'; // Reflect initial backend status
                        nextFileItem.recordingId = data.id;
                        processingMessage.value = 'Upload complete. Waiting for processing...';
                        processingProgress.value = 30;

                         // Add preliminary record to gallery immediately
                        recordings.value.unshift(data);
                        // Optionally select the new item
                        // selectRecording(data);

                        pollProcessingStatus(nextFileItem); // Start polling

                    } else {
                        throw new Error('Unexpected success response from server after upload.');
                    }

                } catch (error) {
                    console.error(`Upload/Processing Error for ${nextFileItem.file.name} (Client ID: ${nextFileItem.clientId}):`, error);
                    nextFileItem.status = 'failed';
                    nextFileItem.error = error.message;
                     // Find the potentially added preliminary record and mark it failed
                     const failedRecordIndex = recordings.value.findIndex(r => r.id === nextFileItem.recordingId);
                     if(failedRecordIndex !== -1) {
                        recordings.value[failedRecordIndex].status = 'FAILED';
                        recordings.value[failedRecordIndex].transcription = `Upload/Processing failed: ${error.message}`;
                     } else {
                        // If record wasn't even created, add a note
                        setGlobalError(`Failed to process "${nextFileItem.file.name}": ${error.message}`);
                     }

                     // Reset state and try next file
                     resetCurrentFileProcessingState();
                     isProcessingActive.value = false;
                     await nextTick();
                     startProcessingQueue();
                 }
            } else {
                console.log("Upload queue is empty or no files are queued.");
                isProcessingActive.value = false;
                // Optional: Auto-minimize popup after a delay
                 // setTimeout(() => {
                //     if (!isProcessingActive.value && uploadQueue.value.every(f => ['completed', 'failed'].includes(f.status))) {
                 //         progressPopupMinimized.value = true;
                //     }
                 // }, 5000);
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
                nextTick(startProcessingQueue); // Try next file
                return;
            }

            // Initial message based on fileItem status (should be 'pending' initially)
            processingMessage.value = 'Waiting for transcription...';
            processingProgress.value = 40;

            pollInterval.value = setInterval(async () => {
                // Check if the item is still the one being processed and hasn't finished/failed
                 if (!currentlyProcessingFile.value || currentlyProcessingFile.value.clientId !== fileItem.clientId || ['completed', 'failed'].includes(fileItem.status)) {
                     console.log(`Polling stopped for ${fileItem.clientId} as it's no longer active or finished.`);
                    clearInterval(pollInterval.value);
                    pollInterval.value = null;
                     // If this was the active file, allow queue to restart
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

                    // Update item in the main recordings list
                    if (galleryIndex !== -1) {
                        recordings.value[galleryIndex] = data;
                        if(selectedRecording.value?.id === recordingId) {
                            selectedRecording.value = data; // Update selection if viewing details
                        }
                    }

                    // Update the status in the uploadQueue item as well
                    fileItem.status = data.status;

                    // Update progress display based on backend status
                    if (data.status === 'COMPLETED') {
                        console.log(`Processing COMPLETED for ${fileItem.file.name} (ID: ${recordingId})`);
                        processingMessage.value = 'Processing complete!';
                        processingProgress.value = 100;
                        fileItem.status = 'completed'; // Final status for queue item
                        // Stop polling, reset state, and trigger next item
                        clearInterval(pollInterval.value);
                        pollInterval.value = null;
                        resetCurrentFileProcessingState();
                        isProcessingActive.value = false;
                        await nextTick();
                        startProcessingQueue();

                    } else if (data.status === 'FAILED') {
                        console.log(`Processing FAILED for ${fileItem.file.name} (ID: ${recordingId})`);
                        processingMessage.value = 'Processing failed.';
                        processingProgress.value = 100; // Show 100% but failed state
                        fileItem.status = 'failed'; // Final status for queue item
                        fileItem.error = data.transcription || data.summary || 'Processing failed on server.';
                        setGlobalError(`Processing failed for "${data.title || fileItem.file.name}".`);
                        // Stop polling, reset state, and trigger next item
                        clearInterval(pollInterval.value);
                        pollInterval.value = null;
                        resetCurrentFileProcessingState();
                        isProcessingActive.value = false;
                        await nextTick();
                        startProcessingQueue();

                    } else if (data.status === 'PROCESSING') {
                        processingMessage.value = 'Transcription in progress...';
                        processingProgress.value = Math.min(65, processingProgress.value + Math.random() * 5); // Mid-range progress
                    } else if (data.status === 'SUMMARIZING') {
                        processingMessage.value = 'Generating title & summary...';
                        processingProgress.value = Math.min(95, processingProgress.value + Math.random() * 5); // Higher progress
                    } else { // PENDING
                        processingMessage.value = 'Waiting in queue...';
                        processingProgress.value = 45; // Keep progress indication while pending
                    }
                } catch (error) {
                    console.error(`Polling Error for ${fileItem.file.name} (ID: ${recordingId}):`, error);
                     // Assume failed if polling error occurs
                    fileItem.status = 'failed';
                    fileItem.error = `Error checking status: ${error.message}`;
                    setGlobalError(`Error checking status for "${fileItem.file.name}": ${error.message}.`);
                     // Update record in gallery if found
                     const galleryIndex = recordings.value.findIndex(r => r.id === recordingId);
                    if (galleryIndex !== -1) recordings.value[galleryIndex].status = 'FAILED';

                    clearInterval(pollInterval.value);
                    pollInterval.value = null;
                    resetCurrentFileProcessingState();
                    isProcessingActive.value = false;
                    await nextTick();
                    startProcessingQueue(); // Process the next file
                }
            }, 5000); // Poll every 5 seconds (adjust as needed)
        };

        // --- Gallery and Detail Methods ---
        // AutoSave removed in favor of explicit save in modal
        // const autoSave = () => { ... };

        // Toggle inbox status
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
                    summary: recordingDataToSave.summary, // <-- ADDED: Include summary
                    meeting_date: recordingDataToSave.meeting_date // <-- ADDED: Include meeting_date (should be YYYY-MM-DD)
                };
                const response = await fetch('/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Failed to save metadata');

                console.log('Save successful:', data.recording.id);
                // Update the master recordings list
                const index = recordings.value.findIndex(r => r.id === data.recording.id);
                if (index !== -1) {
                    // Update only the editable fields, preserve others like status/transcription from backend
                     recordings.value[index].title = payload.title;
                     recordings.value[index].participants = payload.participants;
                     recordings.value[index].notes = payload.notes;
                     recordings.value[index].notes_html = data.recording.notes_html;
                     recordings.value[index].summary = payload.summary;
                     recordings.value[index].summary_html = data.recording.summary_html;
                     recordings.value[index].meeting_date = payload.meeting_date; // <-- ADDED: Update meeting_date
                 }
                 // Update selected if it's the one being saved
                 if (selectedRecording.value?.id === data.recording.id) {
                    selectedRecording.value.title = payload.title;
                    selectedRecording.value.participants = payload.participants;
                    selectedRecording.value.notes = payload.notes;
                    selectedRecording.value.notes_html = data.recording.notes_html;
                    selectedRecording.value.summary = payload.summary;
                    selectedRecording.value.summary_html = data.recording.summary_html;
                    selectedRecording.value.meeting_date = payload.meeting_date; // <-- ADDED: Update meeting_date
                 }
                return data.recording; // Return the full updated object from backend
            } catch (error) {
                console.error('Save Metadata Error:', error);
                setGlobalError(`Save failed: ${error.message}`);
                return null;
            }
        };

        const loadRecordings = async () => {
            globalError.value = null;
            isLoadingRecordings.value = true;
            try {
                const response = await fetch('/recordings');
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Failed to load recordings');
                recordings.value = data;

                // After loading recordings, check for a saved recording ID
                const lastRecordingId = localStorage.getItem('lastSelectedRecordingId');
                if (lastRecordingId) {
                    const recordingToSelect = recordings.value.find(r => r.id == lastRecordingId);
                    if (recordingToSelect) {
                        selectRecording(recordingToSelect);
                    }
                }

                // On load, check for any recordings stuck in PENDING, PROCESSING, or SUMMARIZING
                // and ensure polling is active if the queue processor isn't running.
                const incompleteRecordings = recordings.value.filter(r => ['PENDING', 'PROCESSING', 'SUMMARIZING'].includes(r.status));

                if (incompleteRecordings.length > 0 && !isProcessingActive.value) {
                    console.warn(`Found ${incompleteRecordings.length} incomplete recording(s) on load. Attempting to resume polling if not already queued.`);
                    for (const recording of incompleteRecordings) {
                        // Is this item already being tracked in the queue?
                        let queueItem = uploadQueue.value.find(item => item.recordingId === recording.id);
                        if (!queueItem) {
                            // If not in queue (likely page reload), create a placeholder to track polling
                            console.log(`Re-attaching poll for recording ${recording.id}.`);
                            queueItem = {
                                // Mock file object, size might be useful for display
                                file: { name: recording.title || `Recording ${recording.id}`, size: recording.file_size },
                                status: recording.status, // Use current status from DB
                                recordingId: recording.id,
                                clientId: `reload-${recording.id}`, // Unique ID for tracking
                                error: null
                            };
                            // Add to the *beginning* of the visual queue *if* we want to show it there
                            // Or just manage polling without adding to queue visually?
                            // Let's add it to the queue for consistency in management.
                            uploadQueue.value.unshift(queueItem);
                            // Start processing queue if it wasn't active
                            if (!isProcessingActive.value) {
                                startProcessingQueue();
                            } else if (currentlyProcessingFile.value?.recordingId !== recording.id) {
                                // If processor is active but on a different file, just ensure polling starts
                                // Note: startProcessingQueue handles finding the next 'queued'. This item
                                // might not be 'queued'. We need direct polling.
                                // Let's rethink: The queue processor should handle finding PENDING/PROCESSING items.
                                // For simplicity, let's just trigger the queue processor. It will find this item if it's next.
                                // If something else is truly processing, this won't interrupt.
                                console.log("Triggering queue processor check due to reloaded incomplete item.")
                                // Ensure it's marked conceptually 'queued' for the processor to pick up
                                queueItem.status = 'queued';
                                startProcessingQueue(); // Let the queue logic handle it
                            }

                        } else if (queueItem.status !== recording.status && !['completed', 'failed'].includes(queueItem.status)) {
                            // If queue status differs from DB, update queue status
                            console.log(`Correcting queue status for ${queueItem.clientId} from ${queueItem.status} to ${recording.status}`);
                            queueItem.status = recording.status;
                            // Restart queue processing if needed
                            if (!isProcessingActive.value) startProcessingQueue();
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

         const selectRecording = (recording) => {
            selectedRecording.value = recording;
            if (recording && recording.id) {
                localStorage.setItem('lastSelectedRecordingId', recording.id);
            } else {
                localStorage.removeItem('lastSelectedRecordingId');
            }
             // Optional: Check if polling needs to be restarted if user selects an incomplete item
             // This logic is complex and might be redundant with the loadRecordings check.
             // Let's rely on loadRecordings and the queue processor for robustness.
         };

        const editRecording = (recording) => {
             // Create a deep copy for the modal to prevent modifying original object directly
            editingRecording.value = JSON.parse(JSON.stringify(recording));
            showEditModal.value = true;
        };

        const cancelEdit = () => {
            showEditModal.value = false;
            editingRecording.value = null;
        };

         const saveEdit = async () => {
            // Save using the saveMetadata function which updates the main list & selected item
             const success = await saveMetadata(editingRecording.value);
            if (success) {
                cancelEdit(); // Close modal on success
             }
             // Keep modal open on failure, error shown via globalError
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
            const titleToDelete = recordingToDelete.value.title; // For logging/messaging
            try {
                const response = await fetch(`/recording/${idToDelete}`, { method: 'DELETE' });
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Failed to delete recording');

                 // Remove from gallery list
                recordings.value = recordings.value.filter(r => r.id !== idToDelete);

                // Find and remove from upload queue
                const queueIndex = uploadQueue.value.findIndex(item => item.recordingId === idToDelete);
                 if (queueIndex !== -1) {
                    const deletedItem = uploadQueue.value.splice(queueIndex, 1)[0];
                     console.log(`Removed item ${deletedItem.clientId} from queue.`);
                     // If deleting the file currently being processed, stop polling and move to next
                    if (currentlyProcessingFile.value?.clientId === deletedItem.clientId) {
                        console.log(`Deleting currently processing file: ${titleToDelete}. Stopping poll and moving to next.`);
                        clearInterval(pollInterval.value); // Stop polling explicitly
                        pollInterval.value = null;
                        resetCurrentFileProcessingState();
                        isProcessingActive.value = false; // Allow queue to restart
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
                cancelDelete(); // Still close modal on error
            }
        };
        
        // Inline editing methods
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
            if (!editingSummary.value) {
                saveInlineEdit('summary');
            }
        };
        
        const toggleEditNotes = () => {
            editingNotes.value = !editingNotes.value;
            if (!editingNotes.value) {
                saveInlineEdit('notes');
            }
        };
        
        const saveInlineEdit = async (field) => {
            if (!selectedRecording.value) return;
            
            // Create a payload with just the field being edited
            const payload = {
                id: selectedRecording.value.id,
                [field]: selectedRecording.value[field]
            };
            
            // For completeness, include all editable fields in the payload
            // This ensures the backend has all the data it needs
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
                    // Update the HTML versions from the response
                    if (field === 'notes') {
                        selectedRecording.value.notes_html = updatedRecording.notes_html;
                    } else if (field === 'summary') {
                        selectedRecording.value.summary_html = updatedRecording.summary_html;
                    }
                    
                    // Reset the editing state for the field
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

        // --- Audio Recording Methods ---
        const formatTime = (seconds) => {
            const minutes = Math.floor(seconds / 60);
            const secs = seconds % 60;
            return `${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        };

        const startRecording = async () => {
            if (!canRecordAudio.value) {
                setGlobalError('Audio recording is not supported by your browser or permission was denied.');
                return;
            }
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder.value = new MediaRecorder(stream);
                audioChunks.value = [];
                audioBlobURL.value = null; // Clear previous recording

                mediaRecorder.value.ondataavailable = event => {
                    audioChunks.value.push(event.data);
                };

                mediaRecorder.value.onstop = () => {
                    const audioBlob = new Blob(audioChunks.value, { type: 'audio/webm' }); // or audio/ogg, audio/wav
                    audioBlobURL.value = URL.createObjectURL(audioBlob);
                    stream.getTracks().forEach(track => track.stop()); // Stop microphone access
                    clearInterval(recordingInterval.value);
                };

                mediaRecorder.value.start();
                isRecording.value = true;
                recordingTime.value = 0;
                recordingInterval.value = setInterval(() => {
                    recordingTime.value++;
                }, 1000);
                setGlobalError(null); // Clear any previous errors
            } catch (err) {
                console.error("Error starting recording:", err);
                setGlobalError(`Could not start recording: ${err.message}. Please ensure microphone access is allowed.`);
                isRecording.value = false;
                canRecordAudio.value = false; // Assume permission denied or no device
            }
        };

        const stopRecording = () => {
            if (mediaRecorder.value && isRecording.value) {
                mediaRecorder.value.stop();
                isRecording.value = false;
                // URL and blob are set in onstop handler
            }
        };

        const uploadRecordedAudio = () => {
            if (!audioBlobURL.value) {
                setGlobalError("No recorded audio to upload.");
                return;
            }
            // Create a File object from the Blob
            const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
            const recordedFile = new File(audioChunks.value, `recording-${timestamp}.webm`, { type: 'audio/webm' });

            addFilesToQueue([recordedFile]); // Use the existing queue mechanism
            discardRecording(); // Clear the recording UI
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
        };

        // --- Lifecycle Hooks ---
        onMounted(async () => {
            // Check for the ASR endpoint flag from the template
            const appDiv = document.getElementById('app');
            if (appDiv) {
                const asrFlag = appDiv.dataset.useAsrEndpoint;
                useAsrEndpoint.value = asrFlag === 'True' || asrFlag === 'true';
                currentUserName.value = appDiv.dataset.currentUserName || '';
            }
            loadRecordings();
            initializeDarkMode(); // Initialize dark mode on load
            
            // Load saved settings
            const savedVolume = localStorage.getItem('playerVolume');
            if (savedVolume !== null) {
                playerVolume.value = parseFloat(savedVolume);
            }
            const savedTranscriptionViewMode = localStorage.getItem('transcriptionViewMode');
            if (savedTranscriptionViewMode) {
                transcriptionViewMode.value = savedTranscriptionViewMode;
            }
            
            // Check initial screen size and handle window resize
            const updateMobileStatus = () => {
                windowWidth.value = window.innerWidth;
                // If transitioning from mobile to desktop and mobile menu was open, close it.
                if (!isMobileScreen.value && isMobileMenuOpen.value) {
                    closeMobileMenu();
                }
            };
            
            window.addEventListener('resize', updateMobileStatus);
            updateMobileStatus(); // Set initial window width

            // Close mobile menu on escape key
            const handleEsc = (e) => {
                if (e.key === 'Escape' && isMobileMenuOpen.value) {
                    closeMobileMenu();
                }
            };
            document.addEventListener('keydown', handleEsc);
        });
        
        // --- Main Column Resizer Methods ---
        const loadMainColumnWidths = () => {
            if (isMobileScreen.value || !leftMainColumn.value) return;

            const savedLeftWidthPercent = localStorage.getItem('mainColumnLeftWidthPercent');
            if (savedLeftWidthPercent) {
                const percent = parseFloat(savedLeftWidthPercent);
                // Validate against the new percentage-based constraints
                if (percent >= MIN_MAIN_COLUMN_WIDTH_PERCENT && percent <= MAX_MAIN_COLUMN_WIDTH_PERCENT) {
                    leftMainColumn.value.style.width = `${percent}%`;
                } else {
                    // If stored value is out of bounds, apply default within valid range
                    const defaultPercent = Math.max(MIN_MAIN_COLUMN_WIDTH_PERCENT, Math.min(40, MAX_MAIN_COLUMN_WIDTH_PERCENT));
                    leftMainColumn.value.style.width = `${defaultPercent}%`;
                    localStorage.removeItem('mainColumnLeftWidthPercent'); // Clear invalid stored value
                }
            } else if (leftMainColumn.value.style.width === '') {
                // If no saved width and no inline style, set default within valid range
                const defaultPercent = Math.max(MIN_MAIN_COLUMN_WIDTH_PERCENT, Math.min(40, MAX_MAIN_COLUMN_WIDTH_PERCENT));
                leftMainColumn.value.style.width = `${defaultPercent}%`;
            }
        };

        const saveMainColumnWidths = (leftWidthPercentString) => {
            if (isMobileScreen.value) return;
            localStorage.setItem('mainColumnLeftWidthPercent', leftWidthPercentString);
        };

        const initMainColumnResizer = () => {
            if (mainColumnResizer.value && !isMobileScreen.value) {
                mainColumnResizer.value.addEventListener('mousedown', startMainColumnResize);
            }
        };

        const startMainColumnResize = (event) => {
            if (isMobileScreen.value || !leftMainColumn.value || !mainContentColumns.value) return;
            event.preventDefault();

            isResizingMainColumns.value = true;
            initialMainColumnResizeX = event.clientX;
            initialLeftMainColumnWidthPx = leftMainColumn.value.offsetWidth;

            document.addEventListener('mousemove', handleMainColumnResize);
            document.addEventListener('mouseup', stopMainColumnResize);
            document.body.style.cursor = 'ew-resize';
            document.body.style.userSelect = 'none';
        };

        const handleMainColumnResize = (event) => {
            if (!isResizingMainColumns.value || !leftMainColumn.value || !mainContentColumns.value || !mainColumnResizer.value) return;

            const dx = event.clientX - initialMainColumnResizeX;
            
            // Get CURRENT parent width (not cached) to handle sidebar state changes
            const currentParentWidthPx = mainContentColumns.value.offsetWidth;
            const resizerActualWidth = mainColumnResizer.value.offsetWidth;
            const resizerMarginLeft = parseFloat(getComputedStyle(mainColumnResizer.value).marginLeft) || 0;
            const resizerMarginRight = parseFloat(getComputedStyle(mainColumnResizer.value).marginRight) || 0;
            const resizerTotalSpacePx = resizerActualWidth + resizerMarginLeft + resizerMarginRight;

            const currentEffectiveParentWidthPx = currentParentWidthPx - resizerTotalSpacePx;

            if (currentEffectiveParentWidthPx <= 0) return; // Guard against division by zero or invalid state
            
            // Calculate new width based on mouse movement
            let newLeftWidthPx = initialLeftMainColumnWidthPx + dx;
            
            // Apply constraints smoothly - use pixel-based constraints for better control
            const minLeftPx = Math.max(ABSOLUTE_MIN_COLUMN_PX, (MIN_MAIN_COLUMN_WIDTH_PERCENT / 100) * currentEffectiveParentWidthPx);
            const maxLeftPx = Math.min(currentEffectiveParentWidthPx - ABSOLUTE_MIN_COLUMN_PX, (MAX_MAIN_COLUMN_WIDTH_PERCENT / 100) * currentEffectiveParentWidthPx);

            // Smooth constraint application - don't snap to limits immediately
            if (newLeftWidthPx < minLeftPx) {
                newLeftWidthPx = minLeftPx;
            } else if (newLeftWidthPx > maxLeftPx) {
                newLeftWidthPx = maxLeftPx;
            }
            
            // Calculate percentage based on CURRENT effective width
            const newLeftWidthPercent = (newLeftWidthPx / currentEffectiveParentWidthPx) * 100;

            leftMainColumn.value.style.width = `${newLeftWidthPercent.toFixed(2)}%`;
        };

        const stopMainColumnResize = () => {
            if (!isResizingMainColumns.value) return;
            isResizingMainColumns.value = false;

            document.removeEventListener('mousemove', handleMainColumnResize);
            document.removeEventListener('mouseup', stopMainColumnResize);
            document.body.style.cursor = '';
            document.body.style.userSelect = '';

            if (leftMainColumn.value && leftMainColumn.value.style.width) {
                saveMainColumnWidths(parseFloat(leftMainColumn.value.style.width).toFixed(2));
            }
        };
        
        // Watch for screen size changes to re-init or clear resizer
        watch(isMobileScreen, (isMobile) => {
            if (isMobile) {
                if (mainColumnResizer.value) {
                    mainColumnResizer.value.removeEventListener('mousedown', startMainColumnResize);
                }
                // Optionally reset widths to default for mobile if needed, though CSS should handle layout
            } else {
                // Desktop: ensure elements are available and initialize
                nextTick(() => { // Ensure DOM is updated
                    initializeDesktopLayout();
                });
            }
        });

        // --- Vertical Resizer Methods ---
        const initVerticalResizer = () => {
            transcriptionSection.value = document.querySelector('.transcription-section');
            tabSection.value = document.querySelector('.tab-section');
            verticalResizeHandle.value = document.querySelector('.resize-handle');
            
            if (verticalResizeHandle.value && !isMobileScreen.value) {
                verticalResizeHandle.value.addEventListener('mousedown', startVerticalResize);
                console.log('Vertical resizer initialized with addEventListener');
                return true; // Successfully initialized
            } else {
                console.log('Vertical resizer elements not found:', {
                    transcriptionSection: !!transcriptionSection.value,
                    tabSection: !!tabSection.value,
                    verticalResizeHandle: !!verticalResizeHandle.value,
                    isMobileScreen: isMobileScreen.value
                });
                return false; // Failed to initialize
            }
        };

        const startVerticalResize = (event) => {
            if (isMobileScreen.value || !transcriptionSection.value || !tabSection.value) return;
            event.preventDefault();

            isResizingVertical.value = true;
            initialVerticalResizeY = event.clientY;
            initialTranscriptionHeightPx = transcriptionSection.value.offsetHeight;
            initialTabHeightPx = tabSection.value.offsetHeight;

            document.addEventListener('mousemove', handleVerticalResize);
            document.addEventListener('mouseup', stopVerticalResize);
            document.body.style.cursor = 'ns-resize';
            document.body.style.userSelect = 'none';
            
            console.log('Started vertical resize');
        };

        const handleVerticalResize = (event) => {
            if (!isResizingVertical.value) return;

            requestAnimationFrame(() => {
                if (!transcriptionSection.value || !tabSection.value) return;

                const dy = event.clientY - initialVerticalResizeY;
                const totalHeight = initialTranscriptionHeightPx + initialTabHeightPx;
                
                let newTranscriptionHeight = initialTranscriptionHeightPx + dy;
                
                // Enforce minimum heights (e.g., 100px)
                const minHeight = 100;
                if (newTranscriptionHeight < minHeight) {
                    newTranscriptionHeight = minHeight;
                } else if (totalHeight - newTranscriptionHeight < minHeight) {
                    newTranscriptionHeight = totalHeight - minHeight;
                }
                
                const newTabHeight = totalHeight - newTranscriptionHeight;

                // Convert heights to flex-grow values
                // The sum of flex-grow values will be used for proportioning
                const newTranscriptionFlex = newTranscriptionHeight / totalHeight;
                const newTabFlex = newTabHeight / totalHeight;

                transcriptionFlex.value = newTranscriptionFlex;
                tabsFlex.value = newTabFlex;
            });
        };

        const stopVerticalResize = () => {
            if (!isResizingVertical.value) return;
            isResizingVertical.value = false;

            document.removeEventListener('mousemove', handleVerticalResize);
            document.removeEventListener('mouseup', stopVerticalResize);
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
            
            console.log('Stopped vertical resize');
        };

        // Helper function to initialize desktop layout
        const initializeDesktopLayout = () => {
            console.log('Attempting to initialize desktop layout...');
            
            // First check if we're actually in desktop mode
            if (isMobileScreen.value) {
                console.log('Skipping desktop layout initialization - mobile screen detected');
                return false;
            }
            
            // Check if desktop layout elements are visible and rendered
            const desktopLayout = document.querySelector('.desktop-layout');
            if (!desktopLayout) {
                console.log('Desktop layout container not found');
                return false;
            }
            
            // Check if desktop layout is actually visible (not hidden by CSS)
            const desktopLayoutStyle = window.getComputedStyle(desktopLayout);
            if (desktopLayoutStyle.display === 'none') {
                console.log('Desktop layout is hidden by CSS');
                return false;
            }
            
            leftMainColumn.value = document.getElementById('leftMainColumn');
            rightMainColumn.value = document.getElementById('rightMainColumn');
            mainColumnResizer.value = document.getElementById('mainColumnResizer');
            mainContentColumns.value = document.getElementById('mainContentColumns');
            
            let mainColumnsInitialized = false;
            if (leftMainColumn.value && rightMainColumn.value && mainColumnResizer.value && mainContentColumns.value) {
                // Additional check to ensure elements are actually visible
                const leftStyle = window.getComputedStyle(leftMainColumn.value);
                const rightStyle = window.getComputedStyle(rightMainColumn.value);
                
                if (leftStyle.display !== 'none' && rightStyle.display !== 'none') {
                    loadMainColumnWidths();
                    initMainColumnResizer();
                    mainColumnsInitialized = true;
                    console.log('Main columns initialized successfully');
                } else {
                    console.log('Main column elements are hidden by CSS');
                }
            } else {
                console.log('Main column elements not found:', {
                    leftMainColumn: !!leftMainColumn.value,
                    rightMainColumn: !!rightMainColumn.value,
                    mainColumnResizer: !!mainColumnResizer.value,
                    mainContentColumns: !!mainContentColumns.value
                });
            }
            
            // Initialize vertical resizer
            const verticalInitialized = initVerticalResizer();
            
            // Initialize vertical resizing flex values
            const defaultTranscriptionFlex = 2; // Default initial value
            const defaultTabsFlex = 1;       // Default initial value

            transcriptionFlex.value = defaultTranscriptionFlex;
            tabsFlex.value = defaultTabsFlex;
            
            console.log('Initialized vertical flex values:', { transcriptionFlex: transcriptionFlex.value, tabsFlex: tabsFlex.value });
            
            // Return true if at least one resizer was initialized successfully
            const success = mainColumnsInitialized || verticalInitialized;
            console.log('Desktop layout initialization result:', { mainColumnsInitialized, verticalInitialized, success });
            return success;
        };


        // --- Watchers ---
         watch(uploadQueue, (newQueue, oldQueue) => {
            if (newQueue.length === 0 && oldQueue.length > 0 && !isProcessingActive.value) {
                console.log("Upload queue processing finished.");
                // Auto-minimize after delay
                setTimeout(() => progressPopupMinimized.value = true, 1000);
                // Auto-hide popup after all uploads are complete
                setTimeout(() => {
                    if (completedInQueue.value === totalInQueue.value && !isProcessingActive.value) {
                        progressPopupClosed.value = true;
                    }
                }, 5000);
             }
         }, { deep: true });

        watch(speakerMap, (newMap) => {
            for (const speakerId in newMap) {
                if (newMap[speakerId].isMe) {
                    newMap[speakerId].name = currentUserName.value || 'Me';
                }
            }
        }, { deep: true });


        // --- Reprocessing functionality ---
        const confirmReprocess = (type, recording) => {
            reprocessType.value = type;
            reprocessRecording.value = recording;
            showReprocessModal.value = true;
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
        
        const reprocessTranscription = (recordingId) => {
            const recording = recordings.value.find(r => r.id === recordingId) || selectedRecording.value;
            confirmReprocess('transcription', recording);
        };
        
        const reprocessSummary = (recordingId) => {
            const recording = recordings.value.find(r => r.id === recordingId) || selectedRecording.value;
            confirmReprocess('summary', recording);
        };

        const confirmAsrReprocess = (recording) => {
            reprocessRecording.value = recording;
            // Reset/pre-fill options when opening the modal
            asrReprocessOptions.language = recording.owner?.transcription_language || '';
            asrReprocessOptions.min_speakers = null;
            asrReprocessOptions.max_speakers = null;
            showAsrReprocessModal.value = true;
        };

        const cancelAsrReprocess = () => {
            showAsrReprocessModal.value = false;
            reprocessRecording.value = null; // Clear the recording being reprocessed
        };

        const executeAsrReprocess = async () => {
            if (!reprocessRecording.value) return;
            
            const recordingId = reprocessRecording.value.id;
            
            // Hide modal before starting
            showAsrReprocessModal.value = false;

            await performAsrReprocess(
                recordingId,
                asrReprocessOptions.language,
                asrReprocessOptions.min_speakers,
                asrReprocessOptions.max_speakers
            );

            // Clear the recording after execution
            reprocessRecording.value = null;
        };

        // Speaker database functionality
        const speakerSuggestions = ref({});
        const loadingSuggestions = ref({});
        
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

                // Update the recording in the UI
                const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
                if (index !== -1) {
                    recordings.value[index] = data.recording;
                }
                selectedRecording.value = data.recording;

                showToast('Speaker names updated successfully!', 'fa-check-circle');
                closeSpeakerModal();

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
                    const modalTranscript = document.querySelector('div.speaker-modal-transcript');
                    if (modalTranscript) {
                        const firstInstance = modalTranscript.querySelector(`[data-speaker-id="${speakerId}"]`);
                        if (firstInstance) {
                            firstInstance.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        }
                    }
                });
            }
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
                
                // Start polling for status updates
                startReprocessingPoll(recordingId);
                
            } catch (error) {
                console.error('Reprocess Summary Error:', error);
                setGlobalError(`Failed to start summary reprocessing: ${error.message}`);
            }
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
                    
                    // Stop polling if processing is complete
                    if (data.status === 'COMPLETED' || data.status === 'FAILED') {
                        console.log(`Reprocessing ${data.status.toLowerCase()} for recording ${recordingId}`);
                        stopReprocessingPoll(recordingId);
                        
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
        
        const stopReprocessingPoll = (recordingId) => {
            if (reprocessingPolls.value.has(recordingId)) {
                clearInterval(reprocessingPolls.value.get(recordingId));
                reprocessingPolls.value.delete(recordingId);
                console.log(`Stopped reprocessing poll for recording ${recordingId}`);
            }
        };

        // --- Chat functionality ---
        
        const showChat = ref(false);
        const chatMessages = ref([]);
        const chatInput = ref('');
        const isChatLoading = ref(false);
        
        // Create a ref for the chat messages container
        const chatMessagesRef = ref(null);
        
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
                    .slice(0, -1) // Exclude the new user message
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
        
        // Toast notification system
        const showToast = (message, icon = 'fa-check-circle', duration = 2000) => {
            const toastContainer = document.getElementById('toastContainer');
            
            // Create toast element
            const toast = document.createElement('div');
            toast.className = 'toast';
            toast.innerHTML = `<i class="fas ${icon}"></i> ${message}`;
            
            // Add to container
            toastContainer.appendChild(toast);
            
            // Trigger animation
            setTimeout(() => {
                toast.classList.add('show');
            }, 10);
            
            // Remove after duration
            setTimeout(() => {
                toast.classList.remove('show');
                setTimeout(() => {
                    toastContainer.removeChild(toast);
                }, 300);
            }, duration);
        };
        
        // Add animation to copy button
        const animateCopyButton = (button) => {
            button.classList.add('copy-success');
            
            // Store original content
            const originalContent = button.innerHTML;
            button.innerHTML = '<i class="fas fa-check"></i>';
            
            setTimeout(() => {
                button.classList.remove('copy-success');
                button.innerHTML = originalContent;
            }, 1500);
        };
        
        const copyMessage = (text, event) => {
            // Get the button that was clicked
            const button = event.currentTarget;
            
            if (navigator.clipboard && window.isSecureContext) {
                // Use clipboard API if available (secure context)
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
                // Fallback for non-secure contexts
                fallbackCopyTextToClipboard(text, button);
            }
        };
        
        // Fallback method using document.execCommand
        const fallbackCopyTextToClipboard = (text, button = null) => {
            try {
                const textArea = document.createElement("textarea");
                textArea.value = text;
                
                // Make the textarea out of viewport
                textArea.style.position = "fixed";
                textArea.style.left = "-999999px";
                textArea.style.top = "-999999px";
                document.body.appendChild(textArea);
                
                // Select and copy
                textArea.focus();
                textArea.select();
                const successful = document.execCommand('copy');
                
                // Clean up
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
                    // It's our simplified JSON, format it for copying
                    textToCopy = transcriptionData.map(segment => {
                        const speakerName = speakerMap.value[segment.speaker]?.name || segment.speaker;
                        return `[${speakerName}]: ${segment.sentence}`;
                    }).join('\n');
                } else {
                    // It's some other JSON or plain text, copy as is
                    textToCopy = selectedRecording.value.transcription;
                }
            } catch (e) {
                // Not JSON, so it's plain text
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
        

        
        
        // Mobile tab switching
        const switchMobileTab = (tabName) => {
            selectedMobileTab.value = tabName;
        };
        
        // Clear chat messages when recording changes
        watch(selectedRecording, (newVal) => {
            chatMessages.value = [];
            showChat.value = false;
            selectedTab.value = 'summary'; // Reset tab when recording changes
            selectedMobileTab.value = 'transcript'; // Reset mobile tab when recording changes
            editingMobileMeetingDate.value = false; // Reset mobile edit state
            
            // Initialize desktop layout when a recording is first selected
            if (newVal && !isMobileScreen.value && !isDesktopLayoutInitialized.value) {
                // Use nextTick to ensure Vue has rendered the conditional elements
                nextTick(() => {
                    const success = initializeDesktopLayout();
                    if (success) {
                        isDesktopLayoutInitialized.value = true;
                        console.log('Desktop layout initialized after recording selection');
                    }
                });
            }
        });

        const toggleEditMobileMeetingDate = () => {
            editingMobileMeetingDate.value = !editingMobileMeetingDate.value;
            if (!editingMobileMeetingDate.value && selectedRecording.value) {
                // If toggling off (i.e., saving), call saveInlineEdit
                saveInlineEdit('meeting_date');
            }
        };

        // Toggle transcription view mode
        const toggleTranscriptionViewMode = () => {
            transcriptionViewMode.value = transcriptionViewMode.value === 'simple' ? 'bubble' : 'simple';
            localStorage.setItem('transcriptionViewMode', transcriptionViewMode.value);
        };

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

        // Watch for volume changes to apply to all audio elements
        watch(playerVolume, (newVolume) => {
            const audioElements = document.querySelectorAll('audio');
            audioElements.forEach(audio => {
                if (audio.volume !== newVolume) {
                    audio.volume = newVolume;
                }
            });
        });

        return {
            // State
            currentView, dragover, recordings, selectedRecording, // currentRecording removed
            showEditModal, showDeleteModal, editingRecording, recordingToDelete,
            isLoadingRecordings, globalError, maxFileSizeMB, isDarkMode, // <-- Added isDarkMode
            // Multi-upload State
            uploadQueue, currentlyProcessingFile, processingProgress, processingMessage,
            isProcessingActive, progressPopupMinimized, progressPopupClosed,
            // Chat State
            showChat, chatMessages, chatInput, isChatLoading, chatMessagesRef, // <-- Added chatMessagesRef
            // Resize functionality
            transcriptionFlex, tabsFlex,
            // Computed
            groupedRecordings, totalInQueue, completedInQueue, queuedFiles, finishedFilesInQueue, isMobileScreen,
            processedTranscription, // <-- ADDED: Expose processedTranscription computed property
            // Inline editing state
            editingParticipants, editingMeetingDate, editingSummary, editingNotes,
            // Methods
            handleDrop, handleFileSelect, /*autoSave removed*/ loadRecordings, selectedTab, // <-- Added selectedTab
            selectRecording, editRecording, cancelEdit, saveEdit, confirmDelete,
            cancelDelete, deleteRecording, switchToUploadView, switchToGalleryView,
            formatFileSize, setGlobalError, handleDragLeave, formatStatus, getStatusClass,
            formatDisplayDate, // <-- ADDED: Expose date formatting function
            formatDuration,
            toggleDarkMode, // <-- Added toggleDarkMode
            toggleSidebar, isSidebarCollapsed, // <-- Added sidebar state and function
            toggleMobileMenu, closeMobileMenu, isMobileMenuOpen, // <-- Added mobile menu state and functions
            // Inbox and Highlight methods
            toggleInbox, toggleHighlight,
            // Inline editing methods
            toggleEditParticipants, toggleEditMeetingDate, toggleEditSummary, toggleEditNotes, saveInlineEdit,
            editingMobileMeetingDate, // Expose for mobile
            // Chat Methods
            sendChatMessage, copyMessage, copyTranscription,
            // User menu
            isUserMenuOpen,
            isMobileUserMenuOpen, // <-- Added mobile user menu state
            // Search
            searchQuery,
            // Audio Recording
            isRecording,
            canRecordAudio,
            audioBlobURL,
            recordingTime,
            startRecording,
            stopRecording,
            uploadRecordedAudio,
            discardRecording,
            formatTime,
            // Reprocessing
            reprocessTranscription,
            reprocessSummary,
            showReprocessModal,
            reprocessType,
            reprocessRecording,
            confirmReprocess,
            cancelReprocess,
            executeReprocess,
            // ASR Reprocessing
            asrReprocessOptions,
            // Speaker Identification
            showSpeakerModal,
            speakerMap,
            regenerateSummaryAfterSpeakerUpdate,
            identifiedSpeakers,
            openSpeakerModal,
            closeSpeakerModal,
            saveSpeakerNames,
            highlightedTranscript,
            highlightedSpeaker,
            highlightSpeakerInTranscript,
            clearSpeakerHighlight,
            useAsrEndpoint,
            // Mobile tabs
            selectedMobileTab,
            switchMobileTab,
            toggleEditMobileMeetingDate, // Expose for mobile
            transcriptionViewMode,
            toggleTranscriptionViewMode,
            legendExpanded, // Speaker legend expansion state
            // Speaker database functionality
            speakerSuggestions,
            loadingSuggestions,
            searchSpeakers,
            selectSpeakerSuggestion,
            autoIdentifySpeakers,
            isAutoIdentifying,
            // Main column resizer refs (not needed in template but good practice if they were)
            // leftMainColumn, rightMainColumn, mainColumnResizer, mainContentColumns,
            seekAudio,
            seekAudioFromEvent,
            playerVolume,
            onPlayerVolumeChange
         }
    },
    delimiters: ['${', '}'] // Keep Vue delimiters distinct from Flask's Jinja
}).mount('#app');

});
