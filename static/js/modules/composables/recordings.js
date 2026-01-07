/**
 * Recording management composable
 * Handles loading, selecting, filtering, and managing recordings
 */

export function useRecordings(state, utils, reprocessComposable) {
    const {
        recordings, selectedRecording, isLoadingRecordings, isLoadingMore,
        currentPage, perPage, totalRecordings, totalPages, hasNextPage, hasPrevPage,
        showSharedWithMe, showArchivedRecordings, searchQuery, searchDebounceTimer,
        filterTags, filterSpeakers, filterDatePreset, filterDateRange, filterTextQuery,
        filterStarred, filterInbox, sortBy,
        availableTags, availableSpeakers, selectedTagIds, uploadLanguage, uploadMinSpeakers, uploadMaxSpeakers,
        useAsrEndpoint, globalError, uploadQueue, isProcessingActive, currentView,
        isMobileScreen, isSidebarCollapsed, isRecording, audioBlobURL
    } = state;

    const { setGlobalError, showToast } = utils;

    // Load recordings from API
    const loadRecordings = async (page = 1, append = false, searchQueryParam = '') => {
        globalError.value = null;
        if (!append) {
            isLoadingRecordings.value = true;
        } else {
            isLoadingMore.value = true;
        }

        try {
            const endpoint = '/api/recordings';

            const params = new URLSearchParams({
                page: page.toString(),
                per_page: perPage.value.toString()
            });

            if (searchQueryParam.trim()) {
                params.set('q', searchQueryParam.trim());
            }

            // Add sort parameter
            if (sortBy.value) {
                params.set('sort_by', sortBy.value);
            }

            // Add archived/shared/starred/inbox filters as query params (ANDed with other filters)
            if (showArchivedRecordings.value) {
                params.set('archived', 'true');
            }
            if (showSharedWithMe.value) {
                params.set('shared', 'true');
            }
            if (filterStarred.value) {
                params.set('starred', 'true');
            }
            if (filterInbox.value) {
                params.set('inbox', 'true');
            }

            const response = await fetch(`${endpoint}?${params}`);
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to load recordings');

            const recordingsList = data.recordings;
            const pagination = data.pagination;

            if (!Array.isArray(recordingsList)) {
                console.error('Unexpected response format:', data);
                throw new Error('Invalid response format from server');
            }

            if (pagination) {
                currentPage.value = pagination.page;
                totalRecordings.value = pagination.total;
                totalPages.value = pagination.total_pages;
                hasNextPage.value = pagination.has_next;
                hasPrevPage.value = pagination.has_prev;
            } else {
                currentPage.value = 1;
                totalRecordings.value = recordingsList.length;
                totalPages.value = 1;
                hasNextPage.value = false;
                hasPrevPage.value = false;
            }

            if (append) {
                recordings.value = [...recordings.value, ...recordingsList];
            } else {
                recordings.value = recordingsList;
                const lastRecordingId = localStorage.getItem('lastSelectedRecordingId');
                if (lastRecordingId && recordingsList.length > 0) {
                    const recordingToSelect = recordingsList.find(r => r.id == lastRecordingId);
                    if (recordingToSelect) {
                        selectRecording(recordingToSelect);
                    }
                }
            }

            // NOTE: Removed auto-queueing of incomplete recordings.
            // Backend processing recordings are now shown via backendProcessingRecordings
            // computed property, which filters recordings by status (PENDING, PROCESSING, etc.)
            // The job queue system (ProcessingJob) handles background processing.

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

    const loadMoreRecordings = async () => {
        if (!hasNextPage.value || isLoadingMore.value) return;
        await loadRecordings(currentPage.value + 1, true, searchQuery.value);
    };

    const performSearch = async (query = '') => {
        currentPage.value = 1;
        await loadRecordings(1, false, query);
    };

    const debouncedSearch = (query) => {
        if (searchDebounceTimer.value) {
            clearTimeout(searchDebounceTimer.value);
        }
        searchDebounceTimer.value = setTimeout(() => {
            performSearch(query);
        }, 300);
    };

    const loadTags = async () => {
        try {
            const response = await fetch('/api/tags');
            if (response.ok) {
                availableTags.value = await response.json();
            } else {
                availableTags.value = [];
            }
        } catch (error) {
            console.warn('Error loading tags:', error);
            availableTags.value = [];
        }
    };

    const loadSpeakers = async () => {
        try {
            const response = await fetch('/speakers');
            if (response.ok) {
                availableSpeakers.value = await response.json();
            } else {
                availableSpeakers.value = [];
            }
        } catch (error) {
            console.warn('Error loading speakers:', error);
            availableSpeakers.value = [];
        }
    };

    const selectRecording = async (recording) => {
        if (hasUnsavedRecording()) {
            if (!confirm('You have an unsaved recording. Are you sure you want to leave?')) {
                return;
            }
        }

        // Reset modal audio state when switching recordings
        if (utils.resetModalAudioState) {
            utils.resetModalAudioState();
        }

        selectedRecording.value = recording;

        if (recording && recording.id) {
            localStorage.setItem('lastSelectedRecordingId', recording.id);

            try {
                const response = await fetch(`/api/recordings/${recording.id}`);
                if (response.ok) {
                    const fullRecording = await response.json();
                    selectedRecording.value = fullRecording;

                    const index = recordings.value.findIndex(r => r.id === recording.id);
                    if (index !== -1) {
                        recordings.value[index] = fullRecording;
                    }

                    // Auto-start polling if recording is still processing or summarizing
                    if (['PROCESSING', 'SUMMARIZING'].includes(fullRecording.status)) {
                        console.log(`[AUTO-POLL] Recording ${fullRecording.id} is in ${fullRecording.status} state, starting auto-polling`);
                        if (reprocessComposable && reprocessComposable.startReprocessingPoll) {
                            reprocessComposable.startReprocessingPoll(fullRecording.id);
                        } else {
                            console.warn('[AUTO-POLL] reprocessComposable.startReprocessingPoll not available');
                        }
                    }
                }
            } catch (error) {
                console.error('Error loading full recording:', error);
            }
        }

        if (isMobileScreen.value) {
            isSidebarCollapsed.value = true;
        }

        currentView.value = 'detail';

        if (isRecording.value) {
            // Don't interrupt recording
        }
        if (audioBlobURL.value) {
            // Don't discard recorded audio
        }
    };

    const hasUnsavedRecording = () => {
        return isRecording.value || audioBlobURL.value;
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

    const getRecordingTags = (recording) => {
        if (!recording || !recording.tags) return [];
        return recording.tags || [];
    };

    const getAvailableTagsForRecording = (recording) => {
        if (!recording || !availableTags.value) return [];
        const recordingTagIds = getRecordingTags(recording).map(tag => tag.id);
        return availableTags.value.filter(tag => !recordingTagIds.includes(tag.id));
    };

    const filterByTag = (tag) => {
        filterTags.value = [tag.id];
        applyAdvancedFilters();
    };

    const buildSearchQuery = () => {
        let query = [];

        if (filterTextQuery.value.trim()) {
            query.push(filterTextQuery.value.trim());
        }

        if (filterTags.value.length > 0) {
            const tagNames = filterTags.value.map(tagId => {
                const tag = availableTags.value.find(t => t.id === tagId);
                return tag ? `tag:${tag.name.replace(/\s+/g, '_')}` : '';
            }).filter(Boolean);
            query.push(...tagNames);
        }

        if (filterSpeakers.value.length > 0) {
            const speakerNames = filterSpeakers.value.map(name =>
                `speaker:${name.replace(/\s+/g, '_')}`
            );
            query.push(...speakerNames);
        }

        if (filterDatePreset.value) {
            query.push(`date:${filterDatePreset.value}`);
        } else if (filterDateRange.value.start || filterDateRange.value.end) {
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
        filterSpeakers.value = [];
        filterDateRange.value = { start: '', end: '' };
        filterDatePreset.value = '';
        filterTextQuery.value = '';
        filterStarred.value = false;
        filterInbox.value = false;
        searchQuery.value = '';
    };

    const clearTagFilter = () => {
        searchQuery.value = '';
        clearAllFilters();
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
        const selectedTags = selectedTagIds.value.map(tagId =>
            availableTags.value.find(tag => tag.id == tagId)
        ).filter(Boolean);

        const firstTag = selectedTags[0];
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

    const pollInboxRecordings = async () => {
        try {
            const response = await fetch('/api/recordings/inbox-count');
            if (response.ok) {
                const data = await response.json();
                // Update inbox count in UI if needed
            }
        } catch (error) {
            // Silent fail for polling
        }
    };

    return {
        loadRecordings,
        loadMoreRecordings,
        performSearch,
        debouncedSearch,
        loadTags,
        loadSpeakers,
        selectRecording,
        hasUnsavedRecording,
        toggleInbox,
        toggleHighlight,
        getRecordingTags,
        getAvailableTagsForRecording,
        filterByTag,
        buildSearchQuery,
        applyAdvancedFilters,
        clearAllFilters,
        clearTagFilter,
        addTagToSelection,
        removeTagFromSelection,
        applyTagDefaults,
        pollInboxRecordings
    };
}
