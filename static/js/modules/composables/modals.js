/**
 * Modal management composable
 * Handles opening, closing, and saving modal dialogs
 */

export function useModals(state, utils) {
    const {
        showEditModal, showDeleteModal, showEditTagsModal,
        showReprocessModal, showResetModal, showShareModal,
        showSharesListModal, showTextEditorModal, showAsrEditorModal,
        showEditSpeakersModal, showAddSpeakerModal, showEditTextModal,
        showShareDeleteModal, showUnifiedShareModal, showColorSchemeModal,
        showSystemAudioHelpModal, editingRecording, recordingToDelete, recordingToReset,
        selectedRecording, recordings, selectedNewTagId, tagSearchFilter,
        availableTags, currentView, totalRecordings, toasts, uploadQueue, allJobs,
        // DateTime picker state
        showDateTimePicker, pickerMonth, pickerYear, pickerHour, pickerMinute,
        pickerAmPm, pickerSelectedDate, dateTimePickerTarget, dateTimePickerCallback
    } = state;

    const { showToast, setGlobalError } = utils;
    const { computed } = Vue;

    // =========================================
    // Edit Recording Modal
    // =========================================

    const openEditModal = (recording) => {
        editingRecording.value = { ...recording };
        showEditModal.value = true;
    };

    const cancelEdit = () => {
        showEditModal.value = false;
        editingRecording.value = null;
    };

    const saveEdit = async () => {
        if (!editingRecording.value) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/api/recordings/${editingRecording.value.id}`, {
                method: 'PUT',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    title: editingRecording.value.title,
                    participants: editingRecording.value.participants,
                    meeting_date: editingRecording.value.meeting_date,
                    notes: editingRecording.value.notes
                })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to save changes');

            // Update local data
            const index = recordings.value.findIndex(r => r.id === editingRecording.value.id);
            if (index !== -1) {
                recordings.value[index] = { ...recordings.value[index], ...editingRecording.value };
            }
            if (selectedRecording.value && selectedRecording.value.id === editingRecording.value.id) {
                selectedRecording.value = { ...selectedRecording.value, ...editingRecording.value };
            }

            showToast('Recording updated!', 'fa-check-circle');
            showEditModal.value = false;
            editingRecording.value = null;
        } catch (error) {
            setGlobalError(`Failed to save changes: ${error.message}`);
        }
    };

    // =========================================
    // Delete Recording Modal
    // =========================================

    const confirmDelete = (recording) => {
        recordingToDelete.value = recording;
        showDeleteModal.value = true;
    };

    const cancelDelete = () => {
        showDeleteModal.value = false;
        recordingToDelete.value = null;
    };

    const deleteRecording = async () => {
        if (!recordingToDelete.value) return;
        const deletedId = recordingToDelete.value.id;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${deletedId}`, {
                method: 'DELETE',
                headers: { 'X-CSRFToken': csrfToken }
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to delete recording');

            // Remove from recordings list
            recordings.value = recordings.value.filter(r => r.id !== deletedId);
            totalRecordings.value--;

            // Remove from upload queue if present (frontend tracking)
            if (uploadQueue?.value) {
                uploadQueue.value = uploadQueue.value.filter(item => item.recordingId !== deletedId);
            }

            // Remove from backend job queue if present (backend processing tracking)
            // This is critical - without this, deleted recordings remain in processing queue
            if (allJobs?.value) {
                allJobs.value = allJobs.value.filter(job => job.recording_id !== deletedId);
            }

            // Clear selected recording if it's the one being deleted
            if (selectedRecording.value?.id === deletedId) {
                selectedRecording.value = null;
                currentView.value = 'upload';
            }

            showToast('Recording deleted.', 'fa-trash');
            showDeleteModal.value = false;
            recordingToDelete.value = null;
        } catch (error) {
            setGlobalError(`Failed to delete recording: ${error.message}`);
        }
    };

    // =========================================
    // Archive Recording
    // =========================================

    const archiveRecording = async (recording) => {
        if (!recording) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/api/recordings/${recording.id}/archive`, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken }
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to archive recording');

            recording.is_archived = true;
            recording.audio_deleted_at = data.audio_deleted_at;

            // Update in recordings list
            const index = recordings.value.findIndex(r => r.id === recording.id);
            if (index !== -1) {
                recordings.value[index].is_archived = true;
                recordings.value[index].audio_deleted_at = data.audio_deleted_at;
            }

            showToast('Recording archived (audio deleted)', 'fa-archive');
        } catch (error) {
            setGlobalError(`Failed to archive recording: ${error.message}`);
        }
    };

    // =========================================
    // Edit Tags Modal
    // =========================================

    const openEditTagsModal = () => {
        selectedNewTagId.value = '';
        tagSearchFilter.value = '';
        showEditTagsModal.value = true;
    };

    const closeEditTagsModal = () => {
        showEditTagsModal.value = false;
    };

    const addTagToRecording = async (tagId) => {
        if (!selectedRecording.value || !tagId) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/api/recordings/${selectedRecording.value.id}/tags`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ tag_id: tagId })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to add tag');

            // Find the tag object
            const tag = availableTags.value.find(t => t.id === tagId);
            if (tag) {
                if (!selectedRecording.value.tags) {
                    selectedRecording.value.tags = [];
                }
                selectedRecording.value.tags.push(tag);
            }

            // Update in recordings list
            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1 && tag) {
                if (!recordings.value[index].tags) {
                    recordings.value[index].tags = [];
                }
                recordings.value[index].tags.push(tag);
            }

            selectedNewTagId.value = '';
            showToast('Tag added!', 'fa-tag');
        } catch (error) {
            setGlobalError(`Failed to add tag: ${error.message}`);
        }
    };

    const removeTagFromRecording = async (tagId) => {
        if (!selectedRecording.value || !tagId) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/api/recordings/${selectedRecording.value.id}/tags/${tagId}`, {
                method: 'DELETE',
                headers: { 'X-CSRFToken': csrfToken }
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to remove tag');

            // Remove from selected recording
            if (selectedRecording.value.tags) {
                selectedRecording.value.tags = selectedRecording.value.tags.filter(t => t.id !== tagId);
            }

            // Update in recordings list
            const index = recordings.value.findIndex(r => r.id === selectedRecording.value.id);
            if (index !== -1 && recordings.value[index].tags) {
                recordings.value[index].tags = recordings.value[index].tags.filter(t => t.id !== tagId);
            }

            showToast('Tag removed!', 'fa-tag');
        } catch (error) {
            setGlobalError(`Failed to remove tag: ${error.message}`);
        }
    };

    // =========================================
    // Reset Modal
    // =========================================

    const openResetModal = (recording) => {
        recordingToReset.value = recording;
        showResetModal.value = true;
    };

    const cancelReset = () => {
        showResetModal.value = false;
        recordingToReset.value = null;
    };

    const resetRecording = async () => {
        if (!recordingToReset.value) return;
        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${recordingToReset.value.id}/reset_status`, {
                method: 'POST',
                headers: { 'X-CSRFToken': csrfToken }
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to reset recording');

            // Update recording status
            const index = recordings.value.findIndex(r => r.id === recordingToReset.value.id);
            if (index !== -1) {
                recordings.value[index].status = 'PENDING';
                recordings.value[index].transcription = '';
                recordings.value[index].summary = '';
            }

            if (selectedRecording.value?.id === recordingToReset.value.id) {
                selectedRecording.value.status = 'PENDING';
                selectedRecording.value.transcription = '';
                selectedRecording.value.summary = '';
            }

            showToast('Recording reset for reprocessing.', 'fa-redo');
            showResetModal.value = false;
            recordingToReset.value = null;
        } catch (error) {
            setGlobalError(`Failed to reset recording: ${error.message}`);
        }
    };

    // =========================================
    // System Audio Help Modal
    // =========================================

    const openSystemAudioHelpModal = () => {
        showSystemAudioHelpModal.value = true;
    };

    const closeSystemAudioHelpModal = () => {
        showSystemAudioHelpModal.value = false;
    };

    // =========================================
    // Toast Management
    // =========================================

    const dismissToast = (id) => {
        toasts.value = toasts.value.filter(t => t.id !== id);
    };

    // Aliases for template compatibility
    const editRecording = openEditModal;
    const editRecordingTags = openEditTagsModal;

    // =========================================
    // DateTime Picker
    // =========================================

    const monthNames = ['January', 'February', 'March', 'April', 'May', 'June',
                        'July', 'August', 'September', 'October', 'November', 'December'];
    const dayNames = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'];

    // Generate available years (10 years before and after current year)
    const availableYears = computed(() => {
        const currentYear = new Date().getFullYear();
        const years = [];
        for (let y = currentYear - 10; y <= currentYear + 10; y++) {
            years.push(y);
        }
        return years;
    });

    // Generate hours for 12-hour format
    const hours12 = computed(() => {
        const hours = [];
        for (let h = 1; h <= 12; h++) {
            hours.push({ value: h, label: h.toString() });
        }
        return hours;
    });

    // Generate minutes
    const minutes = computed(() => {
        const mins = [];
        for (let m = 0; m < 60; m++) {
            mins.push(m);
        }
        return mins;
    });

    // Generate calendar days for current month view
    const calendarDays = computed(() => {
        const days = [];
        const year = pickerYear.value;
        const month = pickerMonth.value;

        // First day of the month
        const firstDay = new Date(year, month, 1);
        const startingDay = firstDay.getDay();

        // Last day of the month
        const lastDay = new Date(year, month + 1, 0);
        const totalDays = lastDay.getDate();

        // Previous month days to fill the grid
        const prevMonthLastDay = new Date(year, month, 0).getDate();
        for (let i = startingDay - 1; i >= 0; i--) {
            days.push({
                day: prevMonthLastDay - i,
                date: new Date(year, month - 1, prevMonthLastDay - i),
                inMonth: false,
                isToday: false,
                isSelected: false
            });
        }

        // Current month days
        const today = new Date();
        for (let d = 1; d <= totalDays; d++) {
            const date = new Date(year, month, d);
            const isToday = date.toDateString() === today.toDateString();
            const isSelected = pickerSelectedDate.value &&
                              date.toDateString() === pickerSelectedDate.value.toDateString();
            days.push({
                day: d,
                date: date,
                inMonth: true,
                isToday: isToday,
                isSelected: isSelected
            });
        }

        // Next month days to fill the grid (6 rows * 7 days = 42 total)
        const remainingDays = 42 - days.length;
        for (let d = 1; d <= remainingDays; d++) {
            days.push({
                day: d,
                date: new Date(year, month + 1, d),
                inMonth: false,
                isToday: false,
                isSelected: false
            });
        }

        return days;
    });

    const openDateTimePicker = (target, currentValue, callback) => {
        dateTimePickerTarget.value = target;
        dateTimePickerCallback.value = callback;

        // Parse current value if exists
        if (currentValue) {
            const date = new Date(currentValue);
            if (!isNaN(date.getTime())) {
                pickerSelectedDate.value = date;
                pickerMonth.value = date.getMonth();
                pickerYear.value = date.getFullYear();

                let hours = date.getHours();
                const ampm = hours >= 12 ? 'PM' : 'AM';
                hours = hours % 12;
                hours = hours === 0 ? 12 : hours;

                pickerHour.value = hours;
                pickerMinute.value = date.getMinutes();
                pickerAmPm.value = ampm;
            } else {
                setPickerToNow();
            }
        } else {
            setPickerToNow();
        }

        showDateTimePicker.value = true;
    };

    const setPickerToNow = () => {
        const now = new Date();
        pickerSelectedDate.value = now;
        pickerMonth.value = now.getMonth();
        pickerYear.value = now.getFullYear();

        let hours = now.getHours();
        const ampm = hours >= 12 ? 'PM' : 'AM';
        hours = hours % 12;
        hours = hours === 0 ? 12 : hours;

        pickerHour.value = hours;
        pickerMinute.value = now.getMinutes();
        pickerAmPm.value = ampm;
    };

    const closeDateTimePicker = () => {
        showDateTimePicker.value = false;
        dateTimePickerTarget.value = null;
        dateTimePickerCallback.value = null;
    };

    const prevMonth = () => {
        if (pickerMonth.value === 0) {
            pickerMonth.value = 11;
            pickerYear.value--;
        } else {
            pickerMonth.value--;
        }
    };

    const nextMonth = () => {
        if (pickerMonth.value === 11) {
            pickerMonth.value = 0;
            pickerYear.value++;
        } else {
            pickerMonth.value++;
        }
    };

    const updatePickerView = () => {
        // Called when month/year dropdowns change
        // The computed calendarDays will automatically update
    };

    const selectDate = (date) => {
        pickerSelectedDate.value = date;
    };

    const setToNow = () => {
        setPickerToNow();
    };

    const setToToday = () => {
        const today = new Date();
        pickerSelectedDate.value = today;
        pickerMonth.value = today.getMonth();
        pickerYear.value = today.getFullYear();
        // Keep the current time
    };

    const clearDateTime = () => {
        pickerSelectedDate.value = null;
        const now = new Date();
        pickerMonth.value = now.getMonth();
        pickerYear.value = now.getFullYear();
        pickerHour.value = 12;
        pickerMinute.value = 0;
        pickerAmPm.value = 'PM';
    };

    const formatPickerPreview = () => {
        if (!pickerSelectedDate.value) return '';

        const date = pickerSelectedDate.value;
        const monthName = monthNames[date.getMonth()];
        const day = date.getDate();
        const year = date.getFullYear();

        const hour = pickerHour.value;
        const minute = pickerMinute.value.toString().padStart(2, '0');
        const ampm = pickerAmPm.value;

        return `${monthName} ${day}, ${year} at ${hour}:${minute} ${ampm}`;
    };

    const applyDateTime = () => {
        if (!pickerSelectedDate.value) {
            // If no date selected, just close
            closeDateTimePicker();
            return;
        }

        // Build the full datetime
        const date = new Date(pickerSelectedDate.value);
        let hours = pickerHour.value;

        // Convert 12-hour to 24-hour
        if (pickerAmPm.value === 'AM') {
            hours = hours === 12 ? 0 : hours;
        } else {
            hours = hours === 12 ? 12 : hours + 12;
        }

        date.setHours(hours);
        date.setMinutes(pickerMinute.value);
        date.setSeconds(0);
        date.setMilliseconds(0);

        // Format as ISO string for storage (YYYY-MM-DDTHH:mm:ss)
        const isoString = date.toISOString().slice(0, 19);

        // Call the callback with the result
        if (dateTimePickerCallback.value) {
            dateTimePickerCallback.value(isoString, date);
        }

        closeDateTimePicker();
    };

    // Helper to open datetime picker for meeting date
    const openMeetingDatePicker = () => {
        if (!selectedRecording.value) return;

        openDateTimePicker(
            'meeting_date',
            selectedRecording.value.meeting_date,
            (isoString) => {
                selectedRecording.value.meeting_date = isoString;
                // Auto-save the change
                saveInlineMeetingDate();
            }
        );
    };

    // Save meeting date inline (similar to other inline edits)
    const saveInlineMeetingDate = async () => {
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
            if (!response.ok) throw new Error(data.error || 'Failed to save meeting date');

            showToast('Meeting date updated!', 'fa-calendar-check');
        } catch (error) {
            showToast(`Failed to save: ${error.message}`, 'fa-exclamation-circle', 3000, 'error');
        }
    };

    return {
        // Edit modal
        openEditModal,
        editRecording,
        cancelEdit,
        saveEdit,

        // Delete modal
        confirmDelete,
        cancelDelete,
        deleteRecording,

        // Archive
        archiveRecording,

        // Tags modal
        openEditTagsModal,
        editRecordingTags,
        closeEditTagsModal,
        addTagToRecording,
        removeTagFromRecording,

        // Reset modal
        openResetModal,
        cancelReset,
        resetRecording,

        // System audio help
        openSystemAudioHelpModal,
        closeSystemAudioHelpModal,

        // Toast
        dismissToast,

        // DateTime picker
        monthNames,
        dayNames,
        availableYears,
        hours12,
        minutes,
        calendarDays,
        openDateTimePicker,
        closeDateTimePicker,
        prevMonth,
        nextMonth,
        updatePickerView,
        selectDate,
        setToNow,
        setToToday,
        clearDateTime,
        formatPickerPreview,
        applyDateTime,
        openMeetingDatePicker
    };
}
