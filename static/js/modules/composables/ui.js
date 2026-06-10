/**
 * UI management composable
 * Handles dark mode, color schemes, sidebar, and other UI state
 */

export function useUI(state, utils, processedTranscription) {
    const {
        isDarkMode, currentColorScheme, colorSchemes, isSidebarCollapsed,
        showColorSchemeModal, isUserMenuOpen, showHeaderFolderMenu, currentView, showUploadModal, selectedRecording,
        windowWidth, isMobileScreen, showAdvancedFilters, showSortOptions,
        searchTipsExpanded, isMetadataExpanded, editingParticipants, editingMeetingDate,
        editingSummary, tempSummaryContent, summaryMarkdownEditorInstance,
        leftColumnWidth, rightColumnWidth, isResizing, playerVolume,
        audioIsPlaying, audioCurrentTime, audioDuration, audioIsMuted, audioIsLoading,
        editingNotes, tempNotesContent, transcriptionViewMode,
        notesMarkdownEditor, markdownEditorInstance, autoSaveTimer, csrfToken,
        summaryMarkdownEditor, recordingNotesEditor, recordingMarkdownEditorInstance,
        recordingNotes, showDownloadMenu, currentPlayingSegmentIndex, followPlayerMode,
        playbackRate, showSpeedMenu, playbackSpeeds, modalPlaybackRate, speedMenuPosition,
        videoFullscreen, fullscreenControlsVisible, fullscreenControlsTimer, videoCollapsed,
        regeneratingTitle,
        // Sidebar folder filter and upload-form folder selection — used by
        // switchToUploadView to seed the new-recording dialog from the
        // currently-selected sidebar folder.
        filterFolder, selectedFolderId,
    } = state;

    const autoSaveDelay = 2000; // 2 seconds

    const { showToast, nextTick, t } = utils;
    const { ref, computed, watch } = Vue;

    // isMobile computed
    const isMobile = computed(() => windowWidth.value < 768);

    // Toggle dark mode
    const toggleDarkMode = () => {
        isDarkMode.value = !isDarkMode.value;
        localStorage.setItem('darkMode', isDarkMode.value);

        if (isDarkMode.value) {
            document.documentElement.classList.add('dark');
        } else {
            document.documentElement.classList.remove('dark');
        }

        // Re-apply current color scheme for new mode
        applyColorScheme(currentColorScheme.value);
    };

    // Initialize dark mode from storage
    const initializeDarkMode = () => {
        const savedMode = localStorage.getItem('darkMode');
        if (savedMode !== null) {
            isDarkMode.value = savedMode === 'true';
        } else {
            // Check system preference
            isDarkMode.value = window.matchMedia('(prefers-color-scheme: dark)').matches;
        }

        if (isDarkMode.value) {
            document.documentElement.classList.add('dark');
        } else {
            document.documentElement.classList.remove('dark');
        }
    };

    // Apply a color scheme
    const applyColorScheme = (schemeId, mode = null) => {
        const targetMode = mode || (isDarkMode.value ? 'dark' : 'light');
        const scheme = colorSchemes[targetMode].find(s => s.id === schemeId);

        if (!scheme) {
            console.warn(`Color scheme '${schemeId}' not found for mode '${targetMode}'`);
            return;
        }

        // Remove all theme classes
        const allThemeClasses = [
            ...colorSchemes.light.map(s => s.class),
            ...colorSchemes.dark.map(s => s.class)
        ].filter(c => c !== '');

        document.documentElement.classList.remove(...allThemeClasses);

        // Add new theme class if not default
        if (scheme.class) {
            document.documentElement.classList.add(scheme.class);
        }

        currentColorScheme.value = schemeId;
        localStorage.setItem('colorScheme', schemeId);
    };

    // Initialize color scheme from storage
    const initializeColorScheme = () => {
        const savedScheme = localStorage.getItem('colorScheme');
        if (savedScheme) {
            applyColorScheme(savedScheme);
        } else {
            // Apply default scheme
            applyColorScheme('blue');
        }
    };

    // Open color scheme modal
    const openColorSchemeModal = () => {
        showColorSchemeModal.value = true;
        isUserMenuOpen.value = false;
    };

    // Close color scheme modal
    const closeColorSchemeModal = () => {
        showColorSchemeModal.value = false;
    };

    // Select a color scheme
    const selectColorScheme = (schemeId) => {
        applyColorScheme(schemeId);
        showToast(t('messages.colorSchemeApplied'), 'fa-palette');
    };

    // Reset to default color scheme
    const resetColorScheme = () => {
        applyColorScheme('blue');
        showToast(t('messages.colorSchemeReset'), 'fa-undo');
    };

    // Toggle sidebar
    const toggleSidebar = () => {
        isSidebarCollapsed.value = !isSidebarCollapsed.value;
        localStorage.setItem('sidebarCollapsed', isSidebarCollapsed.value);
    };

    // Initialize sidebar state
    const initializeSidebar = () => {
        const saved = localStorage.getItem('sidebarCollapsed');
        if (saved !== null) {
            isSidebarCollapsed.value = saved === 'true';
        }
    };

    // Switch to upload view. Guarded against silently abandoning an unsaved
    // in-app recording (issue #287(a)): if a recording is in progress or
    // stopped-but-not-yet-uploaded, confirm before leaving the recording view.
    const switchToUploadView = () => {
        const isRecording = state.isRecording;
        const audioBlobURL = state.audioBlobURL;
        const hasUnsaved = currentView.value === 'recording'
            && ((isRecording && isRecording.value === true)
                || (audioBlobURL && audioBlobURL.value));
        if (hasUnsaved) {
            const tFn = utils.t || ((key) => key);
            const message = tFn('confirms.discardUnsavedRecording')
                || 'You have an unsaved recording. Are you sure you want to leave?';
            if (!window.confirm(message)) {
                return;
            }
        }
        // Mirror the sidebar's folder context into the upload-form folder
        // picker so opening the new-recording dialog always reflects where
        // the user thinks they are:
        //   - sidebar set to a real folder -> form pre-fills to it
        //   - sidebar set to All Recordings or Unfiled -> form clears,
        //     so a stale selection from a previous visit doesn't carry
        //     over and a new recording added now will end up unfiled
        //     unless the user explicitly picks something.
        if (filterFolder && selectedFolderId) {
            const fid = filterFolder.value;
            if (fid !== '' && fid !== 'none' && fid != null) {
                const numeric = typeof fid === 'number' ? fid : parseInt(fid, 10);
                if (Number.isFinite(numeric) && numeric > 0) {
                    selectedFolderId.value = numeric;
                }
            } else {
                selectedFolderId.value = null;
            }
        }
        // Upload is now a modal flag, not a currentView value, so the
        // underlying detail / list stays mounted behind the overlay.
        showUploadModal.value = true;
        if (isMobileScreen.value) {
            isSidebarCollapsed.value = true;
        }
    };

    // Switch to detail view
    const switchToDetailView = () => {
        currentView.value = 'detail';
        // Closing implies dismissing the upload modal too in case it
        // was open while the user navigated into a recording.
        showUploadModal.value = false;
    };

    // Dismiss the upload modal. The underlying view (list or detail)
    // is unchanged — the modal was overlay-only.
    const closeUploadView = () => {
        showUploadModal.value = false;
    };

    // Drag-to-dismiss for the .modal-panel--bottom-sheet variant on
    // mobile. Attached to the modal-header area only so dragging
    // inside the scrollable modal-body doesn't fight the gesture.
    // Pulling the sheet down past the threshold (120 px or 25 % of
    // viewport, whichever is smaller) closes the modal; releasing
    // before the threshold snaps back via the CSS transition. The
    // offset is exposed as bottomSheetDragStyle which the template
    // binds to the modal-panel's :style.
    const bottomSheetDragOffset = ref(0);
    const bottomSheetDragStartY = ref(null);
    const bottomSheetDragStyle = computed(() => {
        if (bottomSheetDragOffset.value <= 0) {
            return { transition: 'transform 200ms cubic-bezier(0.16, 1, 0.3, 1)' };
        }
        // No transition while the finger is actively dragging so it
        // tracks 1:1; transition is restored on release.
        return {
            transform: `translateY(${bottomSheetDragOffset.value}px)`,
            transition: 'none'
        };
    });

    const onSheetDragStart = (e) => {
        // Only engage on mobile (the bottom-sheet variant is
        // viewport-gated to ≤ 640 px in primitives.css; mirroring
        // the gate here avoids touch handler overhead on desktop).
        if (window.innerWidth > 640) return;
        const t = e.touches && e.touches[0];
        if (!t) return;
        bottomSheetDragStartY.value = t.clientY;
        bottomSheetDragOffset.value = 0;
    };

    const onSheetDragMove = (e) => {
        if (bottomSheetDragStartY.value == null) return;
        const t = e.touches && e.touches[0];
        if (!t) return;
        const delta = t.clientY - bottomSheetDragStartY.value;
        // Only follow downward drags; upward gestures fall through
        // so the user can scroll content in the body normally if
        // they drift onto the header by accident.
        if (delta > 0) {
            bottomSheetDragOffset.value = delta;
        } else {
            bottomSheetDragOffset.value = 0;
        }
    };

    const onSheetDragEnd = () => {
        if (bottomSheetDragStartY.value == null) return;
        const threshold = Math.min(120, window.innerHeight * 0.25);
        if (bottomSheetDragOffset.value > threshold) {
            // Snap fully off-screen before dismissing so the close
            // reads as the natural continuation of the gesture
            // rather than a hard jump.
            bottomSheetDragOffset.value = window.innerHeight;
            setTimeout(() => {
                closeUploadView();
                bottomSheetDragOffset.value = 0;
                bottomSheetDragStartY.value = null;
            }, 180);
            return;
        }
        bottomSheetDragOffset.value = 0;
        bottomSheetDragStartY.value = null;
    };

    // Switch to recording view
    const switchToRecordingView = () => {
        currentView.value = 'recording';
        if (isMobileScreen.value) {
            isSidebarCollapsed.value = true;
        }
    };

    // Set global error
    const setGlobalError = (message, duration = 7000) => {
        if (state.globalError) {
            state.globalError.value = message;
            if (duration > 0) {
                setTimeout(() => {
                    if (state.globalError.value === message) {
                        state.globalError.value = null;
                    }
                }, duration);
            }
        }
    };

    // Format file size
    const formatFileSize = (bytes) => {
        if (!bytes) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    };

    // Format display date. (Shadowed by app.modular.js's version; kept correct
    // for INSTANT timestamps via parseServerInstant in case it's ever wired.)
    const formatDisplayDate = (dateString) => {
        if (!dateString) return '';
        const date = utils.parseServerInstant(dateString);
        return date.toLocaleDateString(undefined, {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
        });
    };

    // Format short date
    const formatShortDate = (dateString) => {
        if (!dateString) return '';
        const date = utils.parseServerInstant(dateString);
        const now = new Date();
        const diff = now - date;
        const oneDay = 24 * 60 * 60 * 1000;

        if (diff < oneDay) {
            return date.toLocaleTimeString(undefined, {
                hour: '2-digit',
                minute: '2-digit'
            });
        } else if (diff < 7 * oneDay) {
            return date.toLocaleDateString(undefined, {
                weekday: 'short'
            });
        } else {
            return date.toLocaleDateString(undefined, {
                month: 'short',
                day: 'numeric'
            });
        }
    };

    // Format status
    const formatStatus = (status) => {
        if (!status || status === 'COMPLETED') return '';
        const statusMap = {
            'PENDING': t('status.queued'),
            'QUEUED': t('status.queued'),
            'PROCESSING': t('status.processing'),
            'TRANSCRIBING': t('status.transcribing'),
            'SUMMARIZING': t('status.summarizing'),
            'FAILED': t('status.failed'),
            'UPLOADING': t('status.uploading')
        };
        return statusMap[status] || status.charAt(0).toUpperCase() + status.slice(1).toLowerCase();
    };

    // Get status class
    const getStatusClass = (status) => {
        switch(status) {
            case 'PENDING': return 'status-pending';
            case 'QUEUED': return 'status-pending';
            case 'PROCESSING': return 'status-processing';
            case 'SUMMARIZING': return 'status-summarizing';
            case 'COMPLETED': return '';
            case 'FAILED': return 'status-failed';
            default: return 'status-pending';
        }
    };

    // Format time (seconds to HH:MM:SS)
    const formatTime = (seconds) => {
        if (!seconds && seconds !== 0) return '00:00';
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);

        if (h > 0) {
            return `${h}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
        }
        return `${m}:${s.toString().padStart(2, '0')}`;
    };

    // Format duration in seconds to human readable
    const formatDuration = (seconds) => {
        if (!seconds && seconds !== 0) return '';

        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);

        const parts = [];
        if (h > 0) parts.push(`${h}h`);
        if (m > 0) parts.push(`${m}m`);
        if (s > 0 || parts.length === 0) parts.push(`${s}s`);

        return parts.join(' ');
    };

    // Format processing duration
    const formatProcessingDuration = (seconds) => {
        if (!seconds) return '';
        if (seconds < 60) {
            return `${Math.round(seconds)}s`;
        } else if (seconds < 3600) {
            const m = Math.floor(seconds / 60);
            const s = Math.round(seconds % 60);
            return `${m}m ${s}s`;
        } else {
            const h = Math.floor(seconds / 3600);
            const m = Math.round((seconds % 3600) / 60);
            return `${h}h ${m}m`;
        }
    };

    // --- Inline Editing ---
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
            const csrfTokenValue = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch('/save', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfTokenValue
                },
                body: JSON.stringify(fullPayload)
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to save metadata');

            // Update the recording with returned HTML
            if (data.recording) {
                if (field === 'notes' && data.recording.notes_html) {
                    selectedRecording.value.notes_html = data.recording.notes_html;
                } else if (field === 'summary' && data.recording.summary_html) {
                    selectedRecording.value.summary_html = data.recording.summary_html;
                }
            }
        } catch (error) {
            showToast(t('messages.failedToSave', { error: error.message }), 'fa-exclamation-circle', 3000, 'error');
        }
    };

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

    const toggleEditTitle = () => {
        if (!selectedRecording.value) return;

        // Check if user has permission to edit
        if (selectedRecording.value.can_edit === false) {
            showToast(t('messages.noPermissionToEdit'), 'fa-exclamation-circle', 3000, 'error');
            return;
        }

        if (!state.editingTitle.value) {
            // Start editing
            state.originalTitle.value = selectedRecording.value.title || '';
            state.editingTitle.value = true;
            nextTick(() => {
                // Focus the input field
                const titleInput = document.querySelector('input[ref="titleInput"]');
                if (titleInput) {
                    titleInput.focus();
                    titleInput.select();
                }
            });
        }
    };

    const saveTitle = async () => {
        if (!selectedRecording.value) return;

        state.editingTitle.value = false;

        // Only save if title changed
        if (selectedRecording.value.title !== state.originalTitle.value) {
            await saveInlineEdit('title');
        }
    };

    const cancelEditTitle = () => {
        if (!selectedRecording.value) return;

        // Restore original title
        selectedRecording.value.title = state.originalTitle.value;
        state.editingTitle.value = false;
    };

    const regenerateTitle = async () => {
        if (!selectedRecording.value || regeneratingTitle.value) return;

        regeneratingTitle.value = true;
        try {
            const csrfTokenValue = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${selectedRecording.value.id}/regenerate_title`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfTokenValue
                }
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to regenerate title');

            selectedRecording.value.title = data.title;
            showToast('Title regenerated', 'fa-check-circle', 3000, 'success');
        } catch (error) {
            showToast(error.message, 'fa-exclamation-circle', 3000, 'error');
        } finally {
            regeneratingTitle.value = false;
        }
    };

    const toggleEditSummary = () => {
        editingSummary.value = !editingSummary.value;
        if (editingSummary.value) {
            tempSummaryContent.value = selectedRecording.value?.summary || '';
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

    const initializeSummaryMarkdownEditor = () => {
        if (!summaryMarkdownEditor.value) return;

        try {
            summaryMarkdownEditorInstance.value = new EasyMDE({
                element: summaryMarkdownEditor.value,
                spellChecker: false,
                autofocus: true,
                placeholder: t('form.enterSummaryMarkdown'),
                initialValue: selectedRecording.value?.summary || '',
                status: false,
                toolbar: [
                    "bold", "italic", "heading", "|",
                    "quote", "unordered-list", "ordered-list", "|",
                    "link", "image", "|",
                    "preview", "side-by-side", "fullscreen"
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
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfToken.value
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

    const toggleTranscriptionViewMode = () => {
        transcriptionViewMode.value = transcriptionViewMode.value === 'simple' ? 'bubble' : 'simple';
        localStorage.setItem('transcriptionViewMode', transcriptionViewMode.value);
    };

    const toggleEditNotes = () => {
        editingNotes.value = !editingNotes.value;
        if (editingNotes.value) {
            tempNotesContent.value = selectedRecording.value?.notes || '';
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

    const initializeMarkdownEditor = () => {
        if (!notesMarkdownEditor.value) return;

        try {
            markdownEditorInstance.value = new EasyMDE({
                element: notesMarkdownEditor.value,
                spellChecker: false,
                autofocus: true,
                placeholder: t('form.enterNotesMarkdown'),
                initialValue: selectedRecording.value?.notes || '',
                status: false,
                toolbar: [
                    "bold", "italic", "heading", "|",
                    "quote", "unordered-list", "ordered-list", "|",
                    "link", "image", "|",
                    "preview", "side-by-side", "fullscreen"
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
                        'Content-Type': 'application/json',
                        'X-CSRFToken': csrfToken.value
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

    const downloadNotes = async () => {
        if (!selectedRecording.value || !selectedRecording.value.notes) {
            showToast(t('messages.noNotesAvailableDownload'), 'fa-exclamation-circle');
            return;
        }

        try {
            const response = await fetch(`/recording/${selectedRecording.value.id}/download/notes`);
            if (!response.ok) {
                const error = await response.json();
                showToast(error.error || t('messages.notesDownloadFailed'), 'fa-exclamation-circle');
                return;
            }

            // Create blob and download
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${selectedRecording.value.title || 'notes'}.md`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            showToast(t('messages.notesDownloadSuccess'));
        } catch (error) {
            showToast(t('messages.notesDownloadFailed'), 'fa-exclamation-circle');
        }
    };

    const downloadEventICS = async (event) => {
        if (!event || !event.id) {
            showToast(t('messages.invalidEventData'), 'fa-exclamation-circle');
            return;
        }

        try {
            const response = await fetch(`/api/event/${event.id}/ics`);
            if (!response.ok) {
                const error = await response.json();
                showToast(error.error || t('messages.eventDownloadFailed'), 'fa-exclamation-circle');
                return;
            }

            // Create blob and download
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = `${event.title || 'event'}.ics`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            showToast(t('messages.eventDownloadSuccess', { title: event.title }), 'fa-calendar-check', 3000);
        } catch (error) {
            console.error('Download failed:', error);
            showToast(t('messages.eventDownloadFailed'), 'fa-exclamation-circle');
        }
    };

    const downloadICS = async () => {
        if (!selectedRecording.value || !selectedRecording.value.events || selectedRecording.value.events.length === 0) {
            showToast(t('messages.noEventsToExport'), 'fa-exclamation-circle');
            return;
        }

        try {
            const response = await fetch(`/api/recording/${selectedRecording.value.id}/events/ics`);
            if (!response.ok) {
                const error = await response.json();
                showToast(error.error || t('messages.eventsExportFailed'), 'fa-exclamation-circle');
                return;
            }

            // Create blob and download
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;
            a.download = `events-${selectedRecording.value.title || selectedRecording.value.id}.ics`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            showToast(t('messages.eventsExportSuccess', { count: selectedRecording.value.events.length }), 'fa-calendar-check');
        } catch (error) {
            console.error('Download all events ICS error:', error);
            showToast(t('messages.eventsExportFailed'), 'fa-exclamation-circle');
        }
    };

    const deleteEvent = async (event) => {
        if (!event || !event.id) {
            showToast(t('messages.invalidEventData'), 'fa-exclamation-circle');
            return;
        }

        // Confirm deletion
        if (!confirm(t('events.confirmDelete', { title: event.title }) || `Delete event "${event.title}"?`)) {
            return;
        }

        try {
            const response = await fetch(`/api/event/${event.id}`, {
                method: 'DELETE',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content || ''
                }
            });

            if (!response.ok) {
                const error = await response.json();
                showToast(error.error || t('events.deleteFailed') || 'Failed to delete event', 'fa-exclamation-circle');
                return;
            }

            // Remove event from local state
            if (selectedRecording.value && selectedRecording.value.events) {
                selectedRecording.value.events = selectedRecording.value.events.filter(e => e.id !== event.id);
            }

            showToast(t('events.deleted') || 'Event deleted', 'fa-check-circle');
        } catch (error) {
            console.error('Delete event failed:', error);
            showToast(t('events.deleteFailed') || 'Failed to delete event', 'fa-exclamation-circle');
        }
    };

    const formatEventDateTime = (dateTimeStr) => {
        if (!dateTimeStr) return '';
        try {
            const date = new Date(dateTimeStr);
            const options = {
                weekday: 'short',
                year: 'numeric',
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            };
            return date.toLocaleString(undefined, options);
        } catch (e) {
            return dateTimeStr;
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
            newLeftWidth = Math.max(20, Math.min(80, newLeftWidth));

            leftColumnWidth.value = newLeftWidth;
            rightColumnWidth.value = 100 - newLeftWidth;
        };

        const handleMouseUp = () => {
            isResizing.value = false;
            document.removeEventListener('mousemove', handleMouseMove);
            document.removeEventListener('mouseup', handleMouseUp);

            localStorage.setItem('transcriptColumnWidth', leftColumnWidth.value);
            localStorage.setItem('summaryColumnWidth', rightColumnWidth.value);
        };

        document.addEventListener('mousemove', handleMouseMove);
        document.addEventListener('mouseup', handleMouseUp);
        event.preventDefault();
    };

    // --- Audio Player ---
    const seekAudio = (time, context = 'main') => {
        let audioPlayer = null;
        if (context === 'modal') {
            audioPlayer = document.querySelector('audio.speaker-modal-transcript') || document.querySelector('video.speaker-modal-transcript');
        } else {
            // Prefer the main player by id so we never seek the muted dock follower.
            audioPlayer = document.getElementById('mainPlayerMedia')
                || document.querySelector('.main-content-area audio')
                || document.querySelector('.main-content-area video');
        }

        if (!audioPlayer) {
            audioPlayer = document.querySelector('audio') || document.querySelector('video:not(#dockVideoElement)');
        }

        if (audioPlayer && isFinite(time)) {
            const wasPlaying = !audioPlayer.paused;
            try {
                audioPlayer.currentTime = time;
                if (wasPlaying) {
                    audioPlayer.play().catch(e => console.warn('Play after seek failed:', e));
                }
            } catch (e) {
                console.warn('Seek failed:', e);
            }
        }
    };

    const seekAudioFromEvent = (event) => {
        const segmentElement = event.target.closest('[data-start-time]');
        if (!segmentElement) return;

        const time = parseFloat(segmentElement.dataset.startTime);
        if (isNaN(time)) return;

        const isInSpeakerModal = event.target.closest('.speaker-modal-transcript') !== null;
        const context = isInSpeakerModal ? 'modal' : 'main';

        seekAudio(time, context);
    };

    const onPlayerVolumeChange = (event) => {
        const newVolume = event.target.volume;
        playerVolume.value = newVolume;
        localStorage.setItem('playerVolume', newVolume);
    };

    // --- Custom Audio Player Controls ---
    const getAudioElement = () => {
        // First check fullscreen overlay (teleported to body)
        const fullscreenMedia = document.querySelector('.video-fullscreen-overlay video');
        if (fullscreenMedia) return fullscreenMedia;
        // Then check for audio/video in visible modals (z-50 class) - these take priority
        const modalMedia = document.querySelector('.fixed.z-50 audio') || document.querySelector('.fixed.z-50 video');
        if (modalMedia) {
            return modalMedia;
        }
        // The main player element, addressed by id so it is never confused
        // with the muted docked-video follower (#dockVideoElement), which
        // must NOT be driven by the playback controls.
        const mainMedia = document.getElementById('mainPlayerMedia');
        if (mainMedia) return mainMedia;
        // Fall back to main player in right column (desktop) or detail view
        // (mobile). All generic matches exclude the dock follower.
        return document.querySelector('#rightMainColumn audio') ||
               document.querySelector('#rightMainColumn video') ||
               document.querySelector('.detail-view audio:not(#dockVideoElement)') ||
               document.querySelector('.detail-view video:not(#dockVideoElement)') ||
               document.querySelector('audio') ||
               document.querySelector('video:not(#dockVideoElement)');
    };

    // Keep the muted docked-video follower (#dockVideoElement, rendered in
    // the transcript column when the user enables it) in step with the main
    // player. It carries no audio — the main element is the single audio
    // source — and just mirrors time / rate / play-state, correcting drift
    // past a small threshold. No-op when the dock isn't mounted.
    const syncDockVideo = () => {
        const dock = document.getElementById('dockVideoElement');
        if (!dock) return;
        const main = getAudioElement();
        if (!main || main === dock) return;
        try {
            if (!dock.muted) dock.muted = true;
            if (Math.abs((dock.currentTime || 0) - (main.currentTime || 0)) > 0.3) {
                dock.currentTime = main.currentTime;
            }
            if (dock.playbackRate !== main.playbackRate) dock.playbackRate = main.playbackRate;
            if (main.paused) {
                if (!dock.paused) dock.pause();
            } else if (dock.paused) {
                dock.play().catch(() => {});
            }
        } catch (e) { /* ignore transient media state errors */ }
    };

    const toggleAudioPlayback = () => {
        const audio = getAudioElement();
        if (!audio) return;

        if (audio.paused) {
            audio.play();
        } else {
            audio.pause();
        }
    };

    const toggleAudioMute = () => {
        const audio = getAudioElement();
        if (!audio) return;

        audio.muted = !audio.muted;
        audioIsMuted.value = audio.muted;
    };

    const setAudioVolume = (volume) => {
        const audio = getAudioElement();
        if (!audio) return;

        audio.volume = Math.max(0, Math.min(1, volume));
        playerVolume.value = audio.volume;
        localStorage.setItem('playerVolume', audio.volume);

        if (audio.volume === 0) {
            audio.muted = true;
            audioIsMuted.value = true;
        } else if (audio.muted) {
            audio.muted = false;
            audioIsMuted.value = false;
        }
    };

    const seekAudioTo = (time) => {
        const audio = getAudioElement();
        if (!audio || !isFinite(time)) return;

        // Use our tracked duration (server-side) as fallback if browser duration is broken
        const maxTime = audioDuration.value || (isFinite(audio.duration) ? audio.duration : time);
        try {
            audio.currentTime = Math.max(0, Math.min(time, maxTime));
        } catch (e) {
            console.warn('Seek failed:', e);
        }
    };

    const seekAudioByPercent = (percent) => {
        const audio = getAudioElement();
        // Use our tracked duration (server-side) which works for WebM files without duration metadata
        const dur = audioDuration.value || audio?.duration;
        if (!audio || !dur || !isFinite(dur)) return;

        const time = (percent / 100) * dur;
        try {
            audio.currentTime = time;
        } catch (e) {
            console.warn('Seek by percent failed:', e);
        }
    };

    // Progress bar drag state
    const isDraggingProgress = ref(false);
    const dragPreviewPercent = ref(0);

    // Handle progress bar drag - supports both mouse and touch, only seeks on release
    const startProgressDrag = (event) => {
        const bar = event.currentTarget.querySelector('.progress-track') || event.currentTarget.querySelector('.h-2') || event.currentTarget;
        const rect = bar.getBoundingClientRect();
        const isTouch = event.type === 'touchstart';

        const getPercent = (evt) => {
            const clientX = isTouch ? evt.touches[0].clientX : evt.clientX;
            return Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
        };

        const getPercentFromEnd = (evt) => {
            const clientX = isTouch ? evt.changedTouches[0].clientX : evt.clientX;
            return Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
        };

        // Start dragging - show preview
        isDraggingProgress.value = true;
        dragPreviewPercent.value = getPercent(event);

        const onMove = (evt) => {
            evt.preventDefault();
            const clientX = isTouch ? evt.touches[0].clientX : evt.clientX;
            dragPreviewPercent.value = Math.max(0, Math.min(100, ((clientX - rect.left) / rect.width) * 100));
        };

        const onUp = (evt) => {
            document.removeEventListener(isTouch ? 'touchmove' : 'mousemove', onMove);
            document.removeEventListener(isTouch ? 'touchend' : 'mouseup', onUp);
            // Seek to final position on release
            seekAudioByPercent(dragPreviewPercent.value);
            isDraggingProgress.value = false;
        };

        document.addEventListener(isTouch ? 'touchmove' : 'mousemove', onMove, { passive: false });
        document.addEventListener(isTouch ? 'touchend' : 'mouseup', onUp);
    };

    const handleAudioPlayPause = (event) => {
        audioIsPlaying.value = !event.target.paused;
        syncDockVideo();
    };

    const handleAudioLoadedMetadata = (event) => {
        const duration = event.target.duration;
        // Only set browser duration if we don't already have a server-side duration
        // Server-side duration (from ffprobe) is more reliable for formats like WebM
        if (!audioDuration.value || audioDuration.value === 0) {
            if (duration && isFinite(duration) && duration > 0) {
                audioDuration.value = duration;
            }
        }
        audioIsLoading.value = false;

        // Apply saved playback rate when audio loads
        if (playbackRate.value !== 1) {
            event.target.playbackRate = playbackRate.value;
        }
    };

    const handleAudioEnded = () => {
        audioIsPlaying.value = false;
        audioCurrentTime.value = 0;
    };

    const handleCustomAudioTimeUpdate = (event) => {
        audioCurrentTime.value = event.target.currentTime;
        // Keep the docked-video follower in step (drift-corrected; no-op when off).
        syncDockVideo();

        // Fallback: if duration wasn't set yet, try to get it now
        if (!audioDuration.value || audioDuration.value === 0) {
            const duration = event.target.duration;
            if (duration && isFinite(duration) && duration > 0) {
                audioDuration.value = duration;
            }
        }

        // Also call the existing handler for segment tracking
        handleAudioTimeUpdate(event);
    };

    const handleAudioWaiting = () => {
        audioIsLoading.value = true;
    };

    const handleAudioCanPlay = (event) => {
        audioIsLoading.value = false;

        // Fallback: try to get duration if not set yet
        if (!audioDuration.value || audioDuration.value === 0) {
            const duration = event.target.duration;
            if (duration && isFinite(duration) && duration > 0) {
                audioDuration.value = duration;
            }
        }
    };

    const handleAudioDurationChange = (event) => {
        // WebM and some other formats may initially report Infinity duration
        // This handler catches when the actual duration becomes available
        // Only set if we don't already have a server-side duration
        if (!audioDuration.value || audioDuration.value === 0) {
            const duration = event.target.duration;
            if (duration && isFinite(duration) && duration > 0) {
                audioDuration.value = duration;
            }
        }
    };

    const formatAudioTime = (seconds) => {
        if (!seconds || isNaN(seconds)) return '0:00';
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    };

    // --- Playback Speed Control ---
    const formatPlaybackRate = (rate) => {
        if (rate === 1) return '1x';
        if (rate === Math.floor(rate)) return `${rate}x`;
        return `${rate}x`;
    };

    const setPlaybackRate = (rate) => {
        playbackRate.value = rate;
        localStorage.setItem('playbackRate', rate);

        // Apply to all audio and video elements
        document.querySelectorAll('audio, video').forEach(el => {
            el.playbackRate = rate;
        });
    };

    const cyclePlaybackRate = () => {
        const currentIndex = playbackSpeeds.indexOf(playbackRate.value);
        const nextIndex = (currentIndex + 1) % playbackSpeeds.length;
        setPlaybackRate(playbackSpeeds[nextIndex]);
    };

    const cycleModalPlaybackRate = () => {
        const currentIndex = playbackSpeeds.indexOf(modalPlaybackRate.value);
        const nextIndex = (currentIndex + 1) % playbackSpeeds.length;
        modalPlaybackRate.value = playbackSpeeds[nextIndex];

        // Apply to modal audio elements
        const modalAudio = document.querySelector('.speaker-modal-transcript')?.closest('.fixed')?.querySelector('audio') ||
                          document.querySelector('.speaker-modal-transcript')?.closest('.fixed')?.querySelector('video') ||
                          document.querySelector('[ref="speakerModalAudioRef"]') ||
                          document.querySelector('[ref="asrEditorAudioRef"]');
        if (modalAudio) {
            modalAudio.playbackRate = modalPlaybackRate.value;
        }
    };

    const initializePlaybackRate = () => {
        const savedRate = localStorage.getItem('playbackRate');
        if (savedRate) {
            const rate = parseFloat(savedRate);
            if (playbackSpeeds.includes(rate)) {
                playbackRate.value = rate;
            }
        }
    };

    const updateSpeedMenuPosition = (buttonEl) => {
        if (!buttonEl) return;
        const rect = buttonEl.getBoundingClientRect();
        const menuWidth = 52;
        const menuMaxHeight = 160;
        const gap = 4;
        const safeZone = 60; // Account for header/navbar

        // Check available space above and below
        const spaceAbove = rect.top - safeZone;
        const spaceBelow = window.innerHeight - rect.bottom - 8;

        // Decide direction: prefer above, but use below if not enough space
        const showBelow = spaceAbove < menuMaxHeight && spaceBelow > spaceAbove;

        const right = window.innerWidth - rect.right;

        if (showBelow) {
            // Position below the button
            speedMenuPosition.value = {
                right: `${Math.max(8, right)}px`,
                top: `${rect.bottom + gap}px`,
                bottom: 'auto',
                maxHeight: `${Math.min(menuMaxHeight, spaceBelow)}px`
            };
        } else {
            // Position above the button
            speedMenuPosition.value = {
                right: `${Math.max(8, right)}px`,
                bottom: `${window.innerHeight - rect.top + gap}px`,
                top: 'auto',
                maxHeight: `${Math.min(menuMaxHeight, spaceAbove)}px`
            };
        }
    };

    const audioProgressPercent = computed(() => {
        // Use preview position while dragging for smooth UI
        if (isDraggingProgress.value) {
            return dragPreviewPercent.value;
        }
        if (!audioDuration.value) return 0;
        return (audioCurrentTime.value / audioDuration.value) * 100;
    });

    // Preview time display while dragging
    const displayCurrentTime = computed(() => {
        if (isDraggingProgress.value && audioDuration.value) {
            return (dragPreviewPercent.value / 100) * audioDuration.value;
        }
        return audioCurrentTime.value;
    });

    // Reset audio player state (called when recording changes)
    const resetAudioPlayerState = () => {
        audioIsPlaying.value = false;
        audioCurrentTime.value = 0;
        audioDuration.value = 0;
        audioIsMuted.value = false;
        audioIsLoading.value = false;
    };

    // --- Active Segment Tracking ---

    // Binary search to find the segment containing the current time
    // Returns the index of the last segment where startTime <= currentTime
    // O(log n) instead of O(n) - critical for long transcriptions (4500+ segments)
    const binarySearchSegment = (segments, currentTime) => {
        if (segments.length === 0) return null;

        let low = 0;
        let high = segments.length - 1;
        let result = null;

        while (low <= high) {
            const mid = Math.floor((low + high) / 2);
            const startTime = segments[mid].startTime || segments[mid].start_time;

            if (startTime === undefined) {
                // Skip segments without timing info
                high = mid - 1;
                continue;
            }

            if (startTime <= currentTime) {
                result = mid;  // This segment is a candidate
                low = mid + 1;  // Look for later segments that might also match
            } else {
                high = mid - 1;  // Current time is before this segment
            }
        }

        return result;
    };

    const handleAudioTimeUpdate = (event) => {
        const transcription = processedTranscription.value;

        if (!transcription || !transcription.isJson) {
            return;
        }

        const audioElement = event.target;
        const currentTime = audioElement.currentTime;

        // Find the segment that contains the current time
        const segments = transcription.simpleSegments || [];

        if (segments.length === 0) {
            return;
        }

        // Find the active segment index using binary search - O(log n)
        const activeIndex = binarySearchSegment(segments, currentTime);

        // Only update if changed
        if (activeIndex !== currentPlayingSegmentIndex.value) {
            currentPlayingSegmentIndex.value = activeIndex;

            // Scroll to active segment if follow mode is enabled
            if (followPlayerMode.value && activeIndex !== null) {
                scrollToActiveSegment(activeIndex);
            }
        }
    };

    const scrollToActiveSegment = (segmentIndex) => {
        if (segmentIndex === null || segmentIndex === undefined) return;

        // Select the element BY its data-segment-index (not by NodeList
        // position). Positional indexing breaks in bubble view, where
        // consecutive same-speaker segments are merged into fewer elements, so
        // element[i] is not segment i. The active highlight is keyed off the
        // same data-segment-index, so this always targets the highlighted row.
        const sel = `.transcript-segment[data-segment-index="${segmentIndex}"], .speaker-segment[data-segment-index="${segmentIndex}"], .speaker-bubble[data-segment-index="${segmentIndex}"]`;
        const el = document.querySelector(sel);
        if (!el) return;

        // Use the shared content-visibility-aware scroll: it converges on the
        // real measured position for far jumps (seeks) so it doesn't overshoot,
        // and uses a single smooth scroll for near targets (normal playback).
        if (utils.scrollSegmentIntoView) {
            utils.scrollSegmentIntoView(el);
        } else {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    };

    const toggleFollowPlayerMode = () => {
        followPlayerMode.value = !followPlayerMode.value;
        localStorage.setItem('followPlayerMode', followPlayerMode.value);

        if (followPlayerMode.value) {
            showToast(t('messages.followPlayerEnabled'), 'fa-link');
            // Scroll to current position if we have an active segment
            if (currentPlayingSegmentIndex.value !== null) {
                scrollToActiveSegment(currentPlayingSegmentIndex.value);
            }
        } else {
            showToast(t('messages.followPlayerDisabled'), 'fa-unlink');
        }
    };

    // --- Video Fullscreen ---
    const handleFullscreenKeydown = (e) => {
        if (e.key === 'Escape' || e.key === 'f') {
            exitVideoFullscreen();
        } else if (e.key === ' ' || e.key === 'k') {
            e.preventDefault();
            toggleAudioPlayback();
        } else if (e.key === 'ArrowLeft') {
            e.preventDefault();
            seekAudioTo(audioCurrentTime.value - 10);
        } else if (e.key === 'ArrowRight') {
            e.preventDefault();
            seekAudioTo(audioCurrentTime.value + 10);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            const audio = getAudioElement();
            if (audio) setAudioVolume(Math.min(1, audio.volume + 0.1));
        } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            const audio = getAudioElement();
            if (audio) setAudioVolume(Math.max(0, audio.volume - 0.1));
        }
    };

    const resetFullscreenControlsTimer = () => {
        fullscreenControlsVisible.value = true;
        if (fullscreenControlsTimer.value) {
            clearTimeout(fullscreenControlsTimer.value);
        }
        fullscreenControlsTimer.value = setTimeout(() => {
            if (audioIsPlaying.value) {
                fullscreenControlsVisible.value = false;
            }
        }, 3000);
    };

    const handleFullscreenMouseMove = () => {
        resetFullscreenControlsTimer();
    };

    const enterVideoFullscreen = () => {
        videoFullscreen.value = true;
        videoCollapsed.value = false;
        fullscreenControlsVisible.value = true;
        resetFullscreenControlsTimer();
        document.addEventListener('keydown', handleFullscreenKeydown);
        document.body.style.overflow = 'hidden';
    };

    const exitVideoFullscreen = () => {
        videoFullscreen.value = false;
        fullscreenControlsVisible.value = true;
        if (fullscreenControlsTimer.value) {
            clearTimeout(fullscreenControlsTimer.value);
            fullscreenControlsTimer.value = null;
        }
        document.removeEventListener('keydown', handleFullscreenKeydown);
        document.body.style.overflow = '';
    };

    // --- Copy Functions ---
    const animateCopyButton = (button) => {
        if (!button) return;
        const icon = button.querySelector('i');
        if (icon) {
            const originalClass = icon.className;
            icon.className = 'fas fa-check';
            setTimeout(() => {
                icon.className = originalClass;
            }, 2000);
        }
    };

    const fallbackCopyTextToClipboard = (text) => {
        const textArea = document.createElement('textarea');
        textArea.value = text;
        textArea.style.position = 'fixed';
        textArea.style.left = '-999999px';
        document.body.appendChild(textArea);
        textArea.select();
        try {
            document.execCommand('copy');
            showToast(t('messages.copiedSuccessfully'));
        } catch (err) {
            showToast(t('messages.copyFailed'), 'fa-exclamation-circle');
        }
        document.body.removeChild(textArea);
    };

    const copyTranscription = (event) => {
        if (!selectedRecording.value || !selectedRecording.value.transcription) {
            showToast(t('messages.noTranscriptionToCopy'), 'fa-exclamation-circle');
            return;
        }

        const button = event?.currentTarget;
        let textToCopy = '';

        try {
            const transcriptionData = JSON.parse(selectedRecording.value.transcription);
            if (Array.isArray(transcriptionData)) {
                const wasDiarized = transcriptionData.some(segment => segment.speaker);
                if (wasDiarized) {
                    textToCopy = transcriptionData.map(segment => {
                        return `[${segment.speaker}]: ${segment.text || segment.sentence}`;
                    }).join('\n');
                } else {
                    textToCopy = transcriptionData.map(segment => segment.text || segment.sentence).join('\n');
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
                .then(() => showToast(t('messages.transcriptionCopied')))
                .catch(() => fallbackCopyTextToClipboard(textToCopy));
        } else {
            fallbackCopyTextToClipboard(textToCopy);
        }
    };

    const copySummary = (event) => {
        if (!selectedRecording.value || !selectedRecording.value.summary) {
            showToast(t('messages.noSummaryToCopy'), 'fa-exclamation-circle');
            return;
        }
        const button = event?.currentTarget;
        animateCopyButton(button);

        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(selectedRecording.value.summary)
                .then(() => showToast(t('messages.summaryCopied')))
                .catch(() => fallbackCopyTextToClipboard(selectedRecording.value.summary));
        } else {
            fallbackCopyTextToClipboard(selectedRecording.value.summary);
        }
    };

    const copyNotes = (event) => {
        if (!selectedRecording.value || !selectedRecording.value.notes) {
            showToast(t('messages.noNotesToCopy'), 'fa-exclamation-circle');
            return;
        }
        const button = event?.currentTarget;
        animateCopyButton(button);

        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(selectedRecording.value.notes)
                .then(() => showToast(t('messages.notesCopied')))
                .catch(() => fallbackCopyTextToClipboard(selectedRecording.value.notes));
        } else {
            fallbackCopyTextToClipboard(selectedRecording.value.notes);
        }
    };

    // --- Download Functions ---
    const downloadSummary = async () => {
        if (!selectedRecording.value || !selectedRecording.value.summary) {
            showToast(t('messages.noSummaryToDownload'), 'fa-exclamation-circle');
            return;
        }

        try {
            const response = await fetch(`/recording/${selectedRecording.value.id}/download/summary`);
            if (!response.ok) {
                const error = await response.json();
                showToast(error.error || t('messages.summaryDownloadFailed'), 'fa-exclamation-circle');
                return;
            }

            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;

            const contentDisposition = response.headers.get('Content-Disposition');
            let filename = 'summary.docx';
            if (contentDisposition) {
                const utf8Match = /filename\*=utf-8''(.+)/.exec(contentDisposition);
                if (utf8Match) {
                    filename = decodeURIComponent(utf8Match[1]);
                } else {
                    const regularMatch = /filename="(.+)"/.exec(contentDisposition);
                    if (regularMatch) {
                        filename = regularMatch[1];
                    }
                }
            }
            a.download = filename;

            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            showToast(t('messages.summaryDownloadSuccess'));
        } catch (error) {
            showToast(t('messages.summaryDownloadFailed'), 'fa-exclamation-circle');
        }
    };

    const downloadTranscript = async () => {
        if (!selectedRecording.value || !selectedRecording.value.transcription) {
            showToast(t('messages.noTranscriptionToDownload'), 'fa-exclamation-circle');
            return;
        }

        try {
            // First, fetch available templates
            const templatesResponse = await fetch('/api/transcript-templates');
            let templates = [];
            if (templatesResponse.ok) {
                templates = await templatesResponse.json();
            }

            // If there are templates, show a selection dialog (built as
            // a programmatic modal — uses the same modal-overlay /
            // modal-panel / modal-header / modal-body / modal-footer
            // primitives as Vue-rendered modals so the chrome matches).
            let templateId = null;
            if (templates.length > 0) {
                // Escape user-supplied template name/description before
                // injection. Translations and is_default flag are
                // controlled values; template id is constrained by the
                // DOM data attribute.
                const _esc = (s) => String(s == null ? '' : s)
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
                const modal = document.createElement('div');
                modal.className = 'modal-overlay';
                modal.innerHTML = `
                    <div class="modal-panel modal-panel--sm">
                        <div class="modal-header">
                            <h3 class="modal-title">${_esc(t('transcriptTemplates.selectTemplate'))}</h3>
                            <button class="modal-close cancel-btn" title="Close"><i class="fas fa-times"></i></button>
                        </div>
                        <div class="modal-body" style="padding: var(--sp-2);">
                            <div class="space-y-1 max-h-60 overflow-y-auto">
                                ${templates.map(tmpl => `
                                    <button class="template-option w-full text-left p-3 rounded-md border border-[var(--border-primary)] hover:bg-[var(--bg-tertiary)] transition-colors ${tmpl.is_default ? 'ring-1 ring-[var(--text-accent)]' : ''}" data-template-id="${_esc(tmpl.id)}">
                                        <div class="t-body-strong">${_esc(tmpl.name)}</div>
                                        ${tmpl.description ? `<div class="t-meta">${_esc(tmpl.description)}</div>` : ''}
                                        ${tmpl.is_default ? `<div class="t-meta mt-1" style="color: var(--text-accent);"><i class="fas fa-star mr-1"></i>${_esc(t('transcriptTemplates.default'))}</div>` : ''}
                                    </button>
                                `).join('')}
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button class="cancel-btn btn btn--ghost">${_esc(t('transcriptTemplates.cancel'))}</button>
                            <button class="download-without-template-btn btn btn--primary">${_esc(t('transcriptTemplates.downloadWithoutTemplate'))}</button>
                        </div>
                    </div>
                `;
                document.body.appendChild(modal);

                // Wait for user selection
                await new Promise((resolve) => {
                    modal.querySelectorAll('.template-option').forEach(btn => {
                        btn.addEventListener('click', () => {
                            templateId = btn.dataset.templateId;
                            modal.remove();
                            resolve();
                        });
                    });

                    modal.querySelector('.cancel-btn').addEventListener('click', () => {
                        templateId = 'cancelled';
                        modal.remove();
                        resolve();
                    });

                    modal.querySelector('.download-without-template-btn').addEventListener('click', () => {
                        templateId = 'none';
                        modal.remove();
                        resolve();
                    });

                    modal.addEventListener('click', (e) => {
                        if (e.target === modal) {
                            templateId = 'cancelled';
                            modal.remove();
                            resolve();
                        }
                    });
                });

                if (templateId === null || templateId === undefined || templateId === 'cancelled') {
                    return;
                }
            }

            // If templateId is 'none', download raw transcript without any template
            if (templateId === 'none') {
                let rawText = '';
                try {
                    const transcriptionData = JSON.parse(selectedRecording.value.transcription);
                    if (Array.isArray(transcriptionData)) {
                        rawText = transcriptionData.map(segment => {
                            const speaker = segment.speaker || 'Unknown';
                            const text = segment.sentence || '';
                            return `${speaker}: ${text}`;
                        }).join('\n');
                    } else {
                        rawText = selectedRecording.value.transcription;
                    }
                } catch (e) {
                    rawText = selectedRecording.value.transcription;
                }

                const blob = new Blob([rawText], { type: 'text/plain;charset=utf-8' });
                const downloadUrl = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = downloadUrl;
                a.download = `${selectedRecording.value.title || 'transcript'}_raw.txt`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(downloadUrl);

                showToast(t('messages.transcriptDownloadSuccess'));
                return;
            }

            // Download the transcript with the selected template
            const url = templateId
                ? `/recording/${selectedRecording.value.id}/download/transcript?template_id=${templateId}`
                : `/recording/${selectedRecording.value.id}/download/transcript`;

            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(t('messages.transcriptDownloadFailed'));
            }

            const blob = await response.blob();
            const contentDisposition = response.headers.get('content-disposition');
            let filename = 'transcript.txt';
            if (contentDisposition) {
                const matches = contentDisposition.match(/filename="([^"]+)"/);
                if (matches && matches[1]) {
                    filename = matches[1];
                }
            }

            const downloadUrl = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = downloadUrl;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(downloadUrl);

            showToast(t('messages.transcriptDownloadSuccess'));
        } catch (error) {
            console.error('Error downloading transcript:', error);
            showToast(t('messages.transcriptDownloadFailed'), 'fa-exclamation-circle');
        }
    };

    // Download with default template (no modal)
    const downloadWithDefaultTemplate = async () => {
        if (!selectedRecording.value || !selectedRecording.value.transcription) {
            showToast(t('messages.noTranscriptionToDownload'), 'fa-exclamation-circle');
            return;
        }

        try {
            // Download using the default template (server will use user's default)
            const response = await fetch(`/recording/${selectedRecording.value.id}/download/transcript`);
            if (!response.ok) {
                throw new Error(t('messages.transcriptDownloadFailed'));
            }

            const blob = await response.blob();
            const contentDisposition = response.headers.get('content-disposition');
            let filename = 'transcript.txt';
            if (contentDisposition) {
                const matches = contentDisposition.match(/filename="([^"]+)"/);
                if (matches && matches[1]) {
                    filename = matches[1];
                }
            }

            const downloadUrl = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = downloadUrl;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(downloadUrl);

            showToast(t('messages.transcriptDownloadSuccess'));
        } catch (error) {
            console.error('Error downloading transcript:', error);
            showToast(t('messages.transcriptDownloadFailed'), 'fa-exclamation-circle');
        }
    };

    // Show template selector modal (reuses the modal from downloadTranscript)
    const showTemplateSelector = async () => {
        // This calls the full downloadTranscript which shows the modal
        await downloadTranscript();
    };

    // Initialize UI settings from localStorage
    const initializeUI = () => {
        // Load saved column widths
        const savedLeftWidth = localStorage.getItem('transcriptColumnWidth');
        const savedRightWidth = localStorage.getItem('summaryColumnWidth');
        if (savedLeftWidth && savedRightWidth) {
            leftColumnWidth.value = parseFloat(savedLeftWidth);
            rightColumnWidth.value = parseFloat(savedRightWidth);
        }

        // Load saved transcription view mode
        const savedViewMode = localStorage.getItem('transcriptionViewMode');
        if (savedViewMode) {
            transcriptionViewMode.value = savedViewMode;
        }

        // Load saved player volume
        const savedVolume = localStorage.getItem('playerVolume');
        if (savedVolume) {
            playerVolume.value = parseFloat(savedVolume);
        }

        // Load saved follow player mode
        const savedFollowMode = localStorage.getItem('followPlayerMode');
        if (savedFollowMode !== null) {
            followPlayerMode.value = savedFollowMode === 'true';
        }

        // Load saved playback rate
        initializePlaybackRate();

        // Watch for recording changes to reset active segment and audio player state.
        // IMPORTANT: only reset when the recording IDENTITY changes (different id).
        // selectedRecording gets reassigned to a fresh object with the SAME id by
        // background refreshes — list polling, the processing queue completing,
        // autosave responses, etc. Those reassignments keep the same /audio/<id>
        // src so the <audio> element keeps playing, but a blanket reset here would
        // set audioIsPlaying = false mid-playback, making the play button revert to
        // "play" while audio is still playing. Guard on id so player state survives
        // same-recording refreshes.
        watch(selectedRecording, (newRecording, oldRecording) => {
            if (newRecording && oldRecording && newRecording.id === oldRecording.id) {
                // Same recording, just a refreshed object — keep player state.
                // Pick up a server-side duration if it only just became available.
                if (newRecording.audio_duration && !audioDuration.value) {
                    audioDuration.value = newRecording.audio_duration;
                }
                return;
            }
            if (videoFullscreen.value) exitVideoFullscreen();
            currentPlayingSegmentIndex.value = null;
            resetAudioPlayerState();
            // Use server-side duration if available (more reliable than browser metadata)
            if (newRecording && newRecording.audio_duration) {
                audioDuration.value = newRecording.audio_duration;
            }
        });

        // Set up global click handler to close dropdowns when clicking outside
        setupGlobalClickHandler();
    };

    /**
     * Set up a global click handler to close all dropdowns when clicking outside
     * This provides elegant UX by closing menus when users click elsewhere
     */
    const setupGlobalClickHandler = () => {
        document.addEventListener('click', (event) => {
            const target = event.target;

            // Close user menu if clicking outside of it
            if (isUserMenuOpen.value) {
                const userMenuButton = target.closest('[data-user-menu-toggle]');
                const userMenuDropdown = target.closest('[data-user-menu-dropdown]');

                if (!userMenuButton && !userMenuDropdown) {
                    isUserMenuOpen.value = false;
                }
            }

            // Close the detail-header folder menu if clicking outside
            if (showHeaderFolderMenu.value) {
                const folderToggle = target.closest('[data-header-folder-toggle]');
                const folderDropdown = target.closest('[data-header-folder-dropdown]');
                if (!folderToggle && !folderDropdown) {
                    showHeaderFolderMenu.value = false;
                }
            }

            // Close sort options if clicking outside
            if (showSortOptions.value) {
                const sortButton = target.closest('[data-sort-toggle]');
                const sortDropdown = target.closest('[data-sort-dropdown]');

                if (!sortButton && !sortDropdown) {
                    showSortOptions.value = false;
                }
            }

            // Close download menu if clicking outside
            if (showDownloadMenu.value) {
                const downloadButton = target.closest('[data-download-toggle]');
                const downloadDropdown = target.closest('[data-download-dropdown]');

                if (!downloadButton && !downloadDropdown) {
                    showDownloadMenu.value = false;
                }
            }

            // Close language menu if clicking outside
            if (state.showLanguageMenu && state.showLanguageMenu.value) {
                const languageButton = target.closest('[data-language-toggle]');
                const languageDropdown = target.closest('[data-language-dropdown]');

                if (!languageButton && !languageDropdown) {
                    state.showLanguageMenu.value = false;
                }
            }

            // Close speed menu if clicking outside
            if (showSpeedMenu.value) {
                const speedButton = target.closest('[data-speed-toggle]');
                const speedDropdown = target.closest('[data-speed-dropdown]');

                if (!speedButton && !speedDropdown) {
                    showSpeedMenu.value = false;
                }
            }
        });
    };

    // Initialize recording notes markdown editor
    const initializeRecordingNotesEditor = () => {
        if (!recordingNotesEditor.value) return;

        // Destroy existing instance if any
        if (recordingMarkdownEditorInstance.value) {
            recordingMarkdownEditorInstance.value.toTextArea();
            recordingMarkdownEditorInstance.value = null;
        }

        try {
            recordingMarkdownEditorInstance.value = new EasyMDE({
                element: recordingNotesEditor.value,
                spellChecker: false,
                autofocus: false,
                // Use the same placeholder key as the plain textarea so the
                // wording is consistent ("Type your notes in Markdown format…").
                placeholder: t('form.notesPlaceholder'),
                initialValue: recordingNotes.value || '',
                status: false,
                toolbar: [
                    "bold", "italic", "heading", "|",
                    "quote", "unordered-list", "ordered-list", "|",
                    "link", "|",
                    "preview", "side-by-side", "fullscreen"
                ],
                previewClass: ["editor-preview", "notes-preview"],
                theme: isDarkMode.value ? "dark" : "light"
            });

            // Sync changes back to recordingNotes
            recordingMarkdownEditorInstance.value.codemirror.on('change', () => {
                recordingNotes.value = recordingMarkdownEditorInstance.value.value();
            });
        } catch (error) {
            console.error('Failed to initialize recording notes markdown editor:', error);
        }
    };

    // Destroy recording notes markdown editor
    const destroyRecordingNotesEditor = () => {
        if (recordingMarkdownEditorInstance.value) {
            // Save current value before destroying
            recordingNotes.value = recordingMarkdownEditorInstance.value.value();
            recordingMarkdownEditorInstance.value.toTextArea();
            recordingMarkdownEditorInstance.value = null;
        }
    };

    // =========================================
    // Participants Modal
    // =========================================

    const openParticipantsModal = async () => {
        if (!selectedRecording.value) return;

        // Parse current participants into array
        const participants = selectedRecording.value.participants
            ? selectedRecording.value.participants.split(',').map(p => p.trim()).filter(Boolean)
            : [];

        state.editingParticipantsList.value = participants.map(name => ({ name }));

        // Fetch speakers from database for autocomplete
        try {
            const response = await fetch('/speakers');
            if (response.ok) {
                const speakers = await response.json();
                state.allParticipants.value = speakers.map(s => s.name).sort();
            }
        } catch (e) {
            console.error('Failed to fetch speakers:', e);
            state.allParticipants.value = [];
        }

        state.editingParticipantSuggestions.value = {};
        state.showEditParticipantsModal.value = true;
    };

    const closeEditParticipantsModal = () => {
        state.showEditParticipantsModal.value = false;
        state.editingParticipantsList.value = [];
    };

    const addParticipant = () => {
        state.editingParticipantsList.value.push({ name: '' });
    };

    const removeParticipant = (index) => {
        state.editingParticipantsList.value.splice(index, 1);
        delete state.editingParticipantSuggestions.value[index];
    };

    const filterParticipantSuggestions = (index) => {
        // Close all other dropdowns first
        closeAllParticipantSuggestions();

        const query = state.editingParticipantsList.value[index]?.name?.toLowerCase().trim() || '';
        if (query === '') {
            // Show all participants when field is empty/focused
            state.editingParticipantSuggestions.value[index] = [...state.allParticipants.value];
        } else {
            state.editingParticipantSuggestions.value[index] = state.allParticipants.value.filter(
                p => p.toLowerCase().includes(query)
            );
        }
    };

    const selectParticipantSuggestion = (index, name) => {
        state.editingParticipantsList.value[index].name = name;
        state.editingParticipantSuggestions.value[index] = [];
    };

    const closeParticipantSuggestions = (index) => {
        state.editingParticipantSuggestions.value[index] = [];
    };

    const closeParticipantSuggestionsDelayed = (index) => {
        setTimeout(() => closeParticipantSuggestions(index), 200);
    };

    const closeAllParticipantSuggestions = () => {
        state.editingParticipantSuggestions.value = {};
    };

    const getParticipantDropdownPosition = (index) => {
        // Look up the input by its data attribute (set in the modal
        // template) rather than a fragile class+placeholder selector.
        // The old selector targeted .max-w-md input[placeholder="…"]
        // but the new design-system modal uses .field on the input and
        // a literal English placeholder; the lookup matched no
        // element and the fallback below dropped the dropdown into
        // the top-left corner of the viewport.
        const input = document.querySelector(`input[data-participant-index="${index}"]`);
        if (input) {
            const rect = input.getBoundingClientRect();
            return {
                top: rect.bottom + 2 + 'px',
                left: rect.left + 'px',
                width: rect.width + 'px'
            };
        }
        return { top: '0px', left: '0px', width: '200px' };
    };

    const saveParticipants = async () => {
        if (!selectedRecording.value) return;

        // Join participant names with comma
        const participantsString = state.editingParticipantsList.value
            .map(p => p.name.trim())
            .filter(Boolean)
            .join(', ');

        // Update the recording
        selectedRecording.value.participants = participantsString;

        // Use the same save endpoint as inline editing
        const fullPayload = {
            id: selectedRecording.value.id,
            title: selectedRecording.value.title,
            participants: selectedRecording.value.participants,
            notes: selectedRecording.value.notes,
            summary: selectedRecording.value.summary,
            meeting_date: selectedRecording.value.meeting_date
        };

        try {
            const response = await fetch('/save', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken.value
                },
                body: JSON.stringify(fullPayload)
            });

            const data = await response.json();
            if (!response.ok) throw new Error(data.error || t('messages.failedToSaveParticipants'));

            showToast(t('common.changesSaved'), 'fa-check-circle');
            closeEditParticipantsModal();
        } catch (error) {
            console.error('Save error:', error);
            utils.setGlobalError(t('messages.saveParticipantsFailed', { error: error.message }));
        }
    };

    return {
        // Initialization
        initializeUI,
        toggleDarkMode,
        initializeDarkMode,
        applyColorScheme,
        initializeColorScheme,
        openColorSchemeModal,
        closeColorSchemeModal,
        selectColorScheme,
        resetColorScheme,
        toggleSidebar,
        initializeSidebar,
        switchToUploadView,
        switchToDetailView,
        closeUploadView,
        bottomSheetDragStyle,
        onSheetDragStart,
        onSheetDragMove,
        onSheetDragEnd,
        switchToRecordingView,
        setGlobalError,
        formatFileSize,
        formatDisplayDate,
        formatShortDate,
        formatStatus,
        getStatusClass,
        formatTime,
        formatDuration,
        formatProcessingDuration,
        // Inline editing
        toggleEditTitle,
        saveTitle,
        cancelEditTitle,
        regenerateTitle,
        toggleEditParticipants,
        toggleEditMeetingDate,
        toggleEditSummary,
        cancelEditSummary,
        saveEditSummary,
        initializeSummaryMarkdownEditor,
        autoSaveSummary,
        toggleEditNotes,
        cancelEditNotes,
        saveEditNotes,
        initializeMarkdownEditor,
        autoSaveNotes,
        clickToEditNotes,
        clickToEditSummary,
        // Recording notes editor
        initializeRecordingNotesEditor,
        destroyRecordingNotesEditor,
        downloadNotes,
        downloadEventICS,
        downloadICS,
        deleteEvent,
        formatEventDateTime,
        // View mode
        toggleTranscriptionViewMode,
        // Column resizing
        startColumnResize,
        // Audio player
        seekAudio,
        seekAudioFromEvent,
        syncDockVideo,
        onPlayerVolumeChange,
        handleAudioTimeUpdate,
        toggleFollowPlayerMode,
        scrollToActiveSegment,
        // Custom audio player
        toggleAudioPlayback,
        toggleAudioMute,
        setAudioVolume,
        seekAudioTo,
        seekAudioByPercent,
        startProgressDrag,
        handleAudioPlayPause,
        handleAudioLoadedMetadata,
        handleAudioEnded,
        handleCustomAudioTimeUpdate,
        handleAudioWaiting,
        handleAudioCanPlay,
        handleAudioDurationChange,
        formatAudioTime,
        audioProgressPercent,
        displayCurrentTime,
        isDraggingProgress,
        audioIsPlaying,
        audioCurrentTime,
        audioDuration,
        audioIsMuted,
        audioIsLoading,
        resetAudioPlayerState,
        // Playback speed
        formatPlaybackRate,
        setPlaybackRate,
        cyclePlaybackRate,
        cycleModalPlaybackRate,
        updateSpeedMenuPosition,
        // Video fullscreen
        enterVideoFullscreen,
        exitVideoFullscreen,
        handleFullscreenMouseMove,
        resetFullscreenControlsTimer,
        // Copy functions
        copyTranscription,
        copySummary,
        copyNotes,
        // Download functions
        downloadSummary,
        downloadTranscript,
        downloadWithDefaultTemplate,
        showTemplateSelector,
        // Participants modal
        openParticipantsModal,
        closeEditParticipantsModal,
        addParticipant,
        removeParticipant,
        filterParticipantSuggestions,
        selectParticipantSuggestion,
        closeParticipantSuggestions,
        closeParticipantSuggestionsDelayed,
        closeAllParticipantSuggestions,
        getParticipantDropdownPosition,
        saveParticipants,
        // Computed
        isMobile
    };
}
