/**
 * Recordings composable
 * Handles recordings list, selection, and CRUD operations
 */

import { ref, computed } from 'vue';
import { apiRequest } from '../utils/apiClient.js';

export function useRecordings() {
    // State
    const recordings = ref([]);
    const selectedRecording = ref(null);
    const isLoadingRecordings = ref(true);
    const globalError = ref(null);
    const currentView = ref('upload');
    const availableTags = ref([]);
    const selectedTagIds = ref([]);
    const showTagModal = ref(false);
    const showDeleteModal = ref(false);
    const recordingToDelete = ref(null);

    // Computed
    const completedRecordings = computed(() => {
        return recordings.value.filter(r => r.status === 'COMPLETED');
    });

    const processingRecordings = computed(() => {
        return recordings.value.filter(r => ['PENDING', 'PROCESSING', 'SUMMARIZING'].includes(r.status));
    });

    const hasRecordings = computed(() => recordings.value.length > 0);

    // Methods
    const loadRecordings = async (page = 1, filters = {}) => {
        globalError.value = null;
        isLoadingRecordings.value = true;

        try {
            let endpoint = '/api/recordings';
            if (filters.archived) {
                endpoint = '/api/recordings/archived';
            } else if (filters.sharedWithMe) {
                endpoint = '/api/recordings/shared-with-me';
            }

            const params = new URLSearchParams({
                page: page.toString(),
                per_page: '25'
            });

            if (filters.query) {
                params.set('q', filters.query.trim());
            }

            const response = await fetch(`${endpoint}?${params}`);
            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to load recordings');
            }

            const recordingsList = filters.archived || filters.sharedWithMe ? data : data.recordings;

            if (!Array.isArray(recordingsList)) {
                throw new Error('Invalid response format');
            }

            recordings.value = recordingsList;

            // Restore last selected recording
            const lastRecordingId = localStorage.getItem('lastSelectedRecordingId');
            if (lastRecordingId && recordingsList.length > 0) {
                const recordingToSelect = recordingsList.find(r => r.id == lastRecordingId);
                if (recordingToSelect) {
                    selectRecording(recordingToSelect);
                }
            }

            return filters.archived || filters.sharedWithMe ? null : data.pagination;

        } catch (error) {
            globalError.value = error.message;
            throw error;
        } finally {
            isLoadingRecordings.value = false;
        }
    };

    const selectRecording = async (recording) => {
        if (!recording) return;

        selectedRecording.value = recording;
        currentView.value = 'recording';
        localStorage.setItem('lastSelectedRecordingId', recording.id);

        // Load full recording details if needed
        if (!recording.transcription && recording.status === 'COMPLETED') {
            try {
                const data = await apiRequest(`/api/recordings/${recording.id}`);
                Object.assign(selectedRecording.value, data);
            } catch (error) {
                console.error('Error loading recording details:', error);
            }
        }
    };

    const deselectRecording = () => {
        selectedRecording.value = null;
        currentView.value = 'upload';
        localStorage.removeItem('lastSelectedRecordingId');
    };

    const deleteRecording = async (recordingId) => {
        try {
            await apiRequest(`/api/recordings/${recordingId}`, {
                method: 'DELETE'
            });

            recordings.value = recordings.value.filter(r => r.id !== recordingId);

            if (selectedRecording.value && selectedRecording.value.id === recordingId) {
                deselectRecording();
            }

            return true;
        } catch (error) {
            globalError.value = error.message;
            throw error;
        }
    };

    const archiveRecording = async (recordingId) => {
        try {
            await apiRequest(`/api/recordings/${recordingId}/archive`, {
                method: 'POST'
            });

            recordings.value = recordings.value.filter(r => r.id !== recordingId);

            if (selectedRecording.value && selectedRecording.value.id === recordingId) {
                deselectRecording();
            }

            return true;
        } catch (error) {
            globalError.value = error.message;
            throw error;
        }
    };

    const unarchiveRecording = async (recordingId) => {
        try {
            await apiRequest(`/api/recordings/${recordingId}/unarchive`, {
                method: 'POST'
            });

            recordings.value = recordings.value.filter(r => r.id !== recordingId);

            return true;
        } catch (error) {
            globalError.value = error.message;
            throw error;
        }
    };

    const updateRecording = async (recordingId, updates) => {
        try {
            const data = await apiRequest(`/api/recordings/${recordingId}`, {
                method: 'PUT',
                body: JSON.stringify(updates)
            });

            const index = recordings.value.findIndex(r => r.id === recordingId);
            if (index > -1) {
                Object.assign(recordings.value[index], data.recording || data);
            }

            if (selectedRecording.value && selectedRecording.value.id === recordingId) {
                Object.assign(selectedRecording.value, data.recording || data);
            }

            return data.recording || data;
        } catch (error) {
            globalError.value = error.message;
            throw error;
        }
    };

    const regenerateSummary = async (recordingId, customPrompt = null) => {
        try {
            const body = customPrompt ? { custom_prompt: customPrompt } : {};
            const data = await apiRequest(`/api/recordings/${recordingId}/regenerate-summary`, {
                method: 'POST',
                body: JSON.stringify(body)
            });

            if (selectedRecording.value && selectedRecording.value.id === recordingId) {
                selectedRecording.value.status = 'SUMMARIZING';
            }

            return data;
        } catch (error) {
            globalError.value = error.message;
            throw error;
        }
    };

    const loadTags = async () => {
        try {
            const data = await apiRequest('/api/tags');
            availableTags.value = data;
        } catch (error) {
            console.error('Error loading tags:', error);
        }
    };

    const addTagToRecording = async (recordingId, tagId) => {
        try {
            const data = await apiRequest(`/api/recordings/${recordingId}/tags`, {
                method: 'POST',
                body: JSON.stringify({ tag_id: tagId })
            });

            if (selectedRecording.value && selectedRecording.value.id === recordingId) {
                selectedRecording.value.tags = data.tags || [];
            }

            return data;
        } catch (error) {
            globalError.value = error.message;
            throw error;
        }
    };

    const removeTagFromRecording = async (recordingId, tagId) => {
        try {
            await apiRequest(`/api/recordings/${recordingId}/tags/${tagId}`, {
                method: 'DELETE'
            });

            if (selectedRecording.value && selectedRecording.value.id === recordingId) {
                selectedRecording.value.tags = selectedRecording.value.tags.filter(t => t.id !== tagId);
            }

            return true;
        } catch (error) {
            globalError.value = error.message;
            throw error;
        }
    };

    const toggleHighlight = async (recordingId) => {
        const recording = recordings.value.find(r => r.id === recordingId);
        if (!recording) return;

        const newValue = !recording.is_highlighted;

        try {
            await updateRecording(recordingId, { is_highlighted: newValue });
        } catch (error) {
            throw error;
        }
    };

    const setGlobalError = (message) => {
        globalError.value = message;
    };

    const clearGlobalError = () => {
        globalError.value = null;
    };

    const confirmDelete = (recording) => {
        recordingToDelete.value = recording;
        showDeleteModal.value = true;
    };

    const cancelDelete = () => {
        recordingToDelete.value = null;
        showDeleteModal.value = false;
    };

    const executeDelete = async () => {
        if (recordingToDelete.value) {
            await deleteRecording(recordingToDelete.value.id);
            cancelDelete();
        }
    };

    return {
        // State
        recordings,
        selectedRecording,
        isLoadingRecordings,
        globalError,
        currentView,
        availableTags,
        selectedTagIds,
        showTagModal,
        showDeleteModal,
        recordingToDelete,

        // Computed
        completedRecordings,
        processingRecordings,
        hasRecordings,

        // Methods
        loadRecordings,
        selectRecording,
        deselectRecording,
        deleteRecording,
        archiveRecording,
        unarchiveRecording,
        updateRecording,
        regenerateSummary,
        loadTags,
        addTagToRecording,
        removeTagFromRecording,
        toggleHighlight,
        setGlobalError,
        clearGlobalError,
        confirmDelete,
        cancelDelete,
        executeDelete
    };
}
