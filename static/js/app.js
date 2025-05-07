const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = Vue

createApp({
    setup() {
        const currentView = ref('gallery');
        const dragover = ref(false);
        const recordings = ref([]);
        const selectedRecording = ref(null);
        const selectedTab = ref('summary'); // For Summary/Notes tabs

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
        const editingRecording = ref(null); // Holds a *copy* for the modal
        const recordingToDelete = ref(null);
        // const autoSaveTimeout = ref(null); // Autosave not implemented for modal
        const isLoadingRecordings = ref(true);
        const globalError = ref(null);
        const maxFileSizeMB = ref(250); // Default, could fetch from config if needed
        const isDarkMode = ref(false); // Dark mode state
        const isSidebarCollapsed = ref(false); // Sidebar state
        const isUserMenuOpen = ref(false); // User dropdown menu state
        
        // Inline editing state
        const editingParticipants = ref(false);
        const editingMeetingDate = ref(false);
        const editingSummary = ref(false);
        const editingNotes = ref(false);

        // --- Computed Properties ---
        const groupedRecordings = computed(() => {
            const sortedRecordings = [...recordings.value].sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
            // Grouping logic (same as before)...
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
        const toggleSidebar = () => {
            isSidebarCollapsed.value = !isSidebarCollapsed.value;
            // Optional: Save state to localStorage if persistence is desired
            // localStorage.setItem('sidebarCollapsed', isSidebarCollapsed.value);
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

        // --- Lifecycle Hooks ---
        onMounted(() => {
            loadRecordings();
            initializeDarkMode(); // Initialize dark mode on load
        });

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


        // --- Chat functionality ---
        // Resizable panels state
        const transcriptionFlex = ref(2);
        const tabsFlex = ref(1);
        const isResizing = ref(false);
        const startY = ref(0);
        const startTranscriptionFlex = ref(0);
        const startTabsFlex = ref(0);
        
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

            // Ensure chatMessages.value is an array before pushing
            if (!Array.isArray(chatMessages.value)) {
                console.warn('chatMessages.value was not an array! Resetting. Value was:', chatMessages.value);
                chatMessages.value = []; // Reset if corrupted
            }

            chatMessages.value.push({ role: 'user', content: message });
            chatInput.value = '';
            isChatLoading.value = true;
            
            // Scroll to bottom of chat
            await nextTick();
            // Use the correctly named ref
            if (chatMessagesRef.value) { 
                chatMessagesRef.value.scrollTop = chatMessagesRef.value.scrollHeight;
            }
            
            try {
                // Prepare message history for the API call
                // We need to convert our UI messages to the format expected by the API
                const messageHistory = chatMessages.value
                    .slice(0, -1) // Exclude the message we just added (it will be sent separately)
                    .map(msg => ({
                        role: msg.role,
                        content: msg.content
                    }));
                
                const response = await fetch('/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        recording_id: selectedRecording.value.id,
                        message: message,
                        message_history: messageHistory
                    })
                });
                
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || 'Failed to get chat response');
                
                chatMessages.value.push({ 
                    role: 'assistant', 
                    content: data.response,
                    html: data.response_html 
                });
            } catch (error) {
                console.error('Chat Error:', error);
                setGlobalError(`Chat error: ${error.message}`);
                chatMessages.value.push({ role: 'assistant', content: `Error: ${error.message}` });
            } finally {
                isChatLoading.value = false;
                // Scroll to bottom of chat
                await nextTick();
                 // Use the correctly named ref
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
            
            // Get the button that was clicked
            const button = event.currentTarget;
            
            // Show visual feedback on button
            animateCopyButton(button);
            
            if (navigator.clipboard && window.isSecureContext) {
                // Use clipboard API if available (secure context)
                navigator.clipboard.writeText(selectedRecording.value.transcription)
                    .then(() => {
                        showToast('Transcription copied to clipboard!');
                    })
                    .catch(err => {
                        console.error('Copy failed:', err);
                        showToast('Failed to copy: ' + err.message, 'fa-exclamation-circle');
                        fallbackCopyTextToClipboard(selectedRecording.value.transcription);
                    });
            } else {
                // Fallback for non-secure contexts
                fallbackCopyTextToClipboard(selectedRecording.value.transcription);
            }
        };
        
        // Resize functionality
        const startResize = (e) => {
            isResizing.value = true;
            startY.value = e.clientY;
            startTranscriptionFlex.value = transcriptionFlex.value;
            startTabsFlex.value = tabsFlex.value;
            
            // Add event listeners for mousemove and mouseup
            document.addEventListener('mousemove', handleResize);
            document.addEventListener('mouseup', stopResize);
            
            // Prevent text selection during resize
            document.body.style.userSelect = 'none';
        };
        
        const handleResize = (e) => {
            if (!isResizing.value) return;
            
            const deltaY = e.clientY - startY.value;
            const totalFlex = startTranscriptionFlex.value + startTabsFlex.value;
            
            // Calculate new flex values based on mouse movement
            // Moving down increases transcription section, decreases tabs
            // This is more intuitive - drag down to make transcription bigger
            const flexChange = deltaY / 200; // Reduced sensitivity (was 10)
            
            let newTranscriptionFlex = Math.max(1, startTranscriptionFlex.value + flexChange);
            let newTabsFlex = Math.max(0.5, totalFlex - newTranscriptionFlex);
            
            // Ensure minimum heights are maintained
            if (newTranscriptionFlex < 1) {
                newTranscriptionFlex = 1;
                newTabsFlex = totalFlex - 1;
            } else if (newTabsFlex < 0.5) {
                newTabsFlex = 0.5;
                newTranscriptionFlex = totalFlex - 0.5;
            }
            
            transcriptionFlex.value = newTranscriptionFlex;
            tabsFlex.value = newTabsFlex;
        };
        
        const stopResize = () => {
            isResizing.value = false;
            document.removeEventListener('mousemove', handleResize);
            document.removeEventListener('mouseup', stopResize);
            document.body.style.userSelect = '';
        };
        
        // Clear chat messages when recording changes
        watch(selectedRecording, (newVal) => {
            chatMessages.value = [];
            showChat.value = false;
            selectedTab.value = 'summary'; // Reset tab when recording changes
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
            transcriptionFlex, tabsFlex, isResizing, startY, startTranscriptionFlex, startTabsFlex,
            startResize, handleResize, stopResize,
            // Computed
            groupedRecordings, totalInQueue, completedInQueue, queuedFiles, finishedFilesInQueue,
            // Inline editing state
            editingParticipants, editingMeetingDate, editingSummary, editingNotes,
            // Methods
            handleDrop, handleFileSelect, /*autoSave removed*/ loadRecordings, selectedTab, // <-- Added selectedTab
            selectRecording, editRecording, cancelEdit, saveEdit, confirmDelete,
            cancelDelete, deleteRecording, switchToUploadView, switchToGalleryView,
            formatFileSize, setGlobalError, handleDragLeave, formatStatus, getStatusClass,
            formatDisplayDate, // <-- ADDED: Expose date formatting function
            toggleDarkMode, // <-- Added toggleDarkMode
            toggleSidebar, isSidebarCollapsed, // <-- Added sidebar state and function
            // Inbox and Highlight methods
            toggleInbox, toggleHighlight,
            // Inline editing methods
            toggleEditParticipants, toggleEditMeetingDate, toggleEditSummary, toggleEditNotes, saveInlineEdit,
            // Chat Methods
            sendChatMessage, copyMessage, copyTranscription,
            // User menu
            isUserMenuOpen,
         }
    },
    delimiters: ['${', '}'] // Keep Vue delimiters distinct from Flask's Jinja
}).mount('#app');
