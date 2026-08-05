/**
 * Bulk Operations Composable
 * Handles bulk API operations for multiple recordings
 */

const { ref, computed, watch } = Vue;

export function useBulkOperations({
    selectedRecordingIds,
    selectedRecordings,
    recordings,
    selectedRecording,
    bulkActionInProgress,
    availableTags,
    availableFolders,
    showToast,
    setGlobalError,
    exitSelectionMode,
    startReprocessingPoll,
    t,
    finalizeRecordingMerge,
    fetchRecordingsPage
}) {
    const _t = (k, fallback) => (typeof t === 'function' ? t(k) : (fallback || k));
    // Modal state
    const showBulkDeleteModal = ref(false);
    const showBulkTagModal = ref(false);
    const showBulkReprocessModal = ref(false);
    const showBulkFolderModal = ref(false);
    const showBulkMergeModal = ref(false);
    const bulkTagAction = ref('add'); // 'add' or 'remove'
    const bulkTagSelectedId = ref('');
    const bulkReprocessType = ref('summary'); // 'transcription' or 'summary'

    // Merge state
    const mergeOrderedList = ref([]);   // ordered array of recording objects
    const mergeTitle = ref('');
    const mergeDeleteOriginals = ref(false);
    const mergeInProgress = ref(false);
    // 'bulk' = merge existing recordings (POST /api/recordings/merge).
    // 'recording' = merge a just-recorded clip into existing ones; the list
    // holds a virtual { __self__: true } entry for the not-yet-uploaded clip and
    // confirm finalizes the recording session with the merge intent instead.
    const mergeMode = ref('bulk');
    // Which source's notes to keep on the merged recording. Notes cannot be
    // concatenated, so only one source's are kept. Value is a recording id,
    // '__self__' (the recorded clip), or null (keep no notes).
    const mergeNotesSourceId = ref(null);
    // Sources in the merge that actually have notes — drives the selector.
    // List dicts carry a cheap `has_notes` flag; detail dicts and the recorded
    // clip carry full `notes` text. Accept either.
    const _entryHasNotes = (r) => !!(r && (r.has_notes || (r.notes && String(r.notes).trim())));
    const mergeNotesCandidates = computed(() => mergeOrderedList.value.filter(_entryHasNotes));
    // Keep the notes selection valid as the list changes: default to the first
    // source with notes; respect an explicit "keep none" ('none'); and if the
    // chosen source leaves the list, fall back to the first remaining one.
    watch(mergeNotesCandidates, (cands) => {
        const ids = cands.map(r => r.id);
        if (mergeNotesSourceId.value === 'none') return;      // explicit opt-out
        if (mergeNotesSourceId.value === null || !ids.includes(mergeNotesSourceId.value)) {
            mergeNotesSourceId.value = ids.length ? ids[0] : null;
        }
    }, { deep: false });
    // In-modal "add recording" picker (lets the user append existing recordings
    // to the merge from within the modal — used by the normal merge and by the
    // merge-from-recording flow that seeds the modal with a just-recorded clip).
    const mergeAddPickerOpen = ref(false);
    const mergeAddSearch = ref('');
    // Server-side candidate search so the picker never downloads the whole list.
    // Mirrors the sidebar's paginated /api/recordings search, filtered to
    // mergeable (COMPLETED) recordings via the status param.
    const mergePickerResults = ref([]);
    const mergePickerLoading = ref(false);
    const mergePickerPage = ref(1);
    const mergePickerHasMore = ref(false);
    const MERGE_PICKER_PER_PAGE = 20;

    const loadMergeCandidates = async ({ append = false } = {}) => {
        if (typeof fetchRecordingsPage !== 'function') return;
        const page = append ? mergePickerPage.value + 1 : 1;
        mergePickerLoading.value = true;
        try {
            const data = await fetchRecordingsPage({
                page,
                per_page: MERGE_PICKER_PER_PAGE,
                q: mergeAddSearch.value.trim(),
                status: 'COMPLETED',
                // Deliberately ignores the sidebar's archived/starred/inbox/
                // folder filters — the picker searches all completed recordings.
            });
            const list = Array.isArray(data.recordings) ? data.recordings : [];
            mergePickerResults.value = append ? [...mergePickerResults.value, ...list] : list;
            mergePickerPage.value = data.pagination ? data.pagination.page : page;
            mergePickerHasMore.value = data.pagination ? !!data.pagination.has_next : false;
        } catch (e) {
            console.error('Merge candidate search failed:', e);
            if (!append) mergePickerResults.value = [];
            mergePickerHasMore.value = false;
        } finally {
            mergePickerLoading.value = false;
        }
    };

    const loadMoreMergeCandidates = async () => {
        if (mergePickerHasMore.value && !mergePickerLoading.value) {
            await loadMergeCandidates({ append: true });
        }
    };

    // Candidates = server results minus what's already in the merge list and
    // anything without settled audio.
    const mergeCandidates = computed(() => {
        const inList = new Set(mergeOrderedList.value.map(r => r.id));
        return mergePickerResults.value.filter(r =>
            !inList.has(r.id) && r.audio_ready !== false
        );
    });

    // Debounced re-search when the query changes (only while the picker is open).
    let _mergeSearchTimer = null;
    watch(mergeAddSearch, () => {
        if (!mergeAddPickerOpen.value) return;
        clearTimeout(_mergeSearchTimer);
        _mergeSearchTimer = setTimeout(() => loadMergeCandidates({ append: false }), 250);
    });

    // Load the first page whenever the picker opens.
    watch(mergeAddPickerOpen, (open) => {
        if (open) loadMergeCandidates({ append: false });
    });

    // Get CSRF token
    const getCsrfToken = () => {
        return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
    };

    // Helper to get selected IDs as array
    const getSelectedIds = () => {
        return Array.from(selectedRecordingIds.value);
    };

    // =========================================
    // Bulk Delete
    // =========================================

    const openBulkDeleteModal = () => {
        showBulkDeleteModal.value = true;
    };

    const closeBulkDeleteModal = () => {
        showBulkDeleteModal.value = false;
    };

    const executeBulkDelete = async () => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        bulkActionInProgress.value = true;
        closeBulkDeleteModal();

        try {
            const response = await fetch('/api/recordings/bulk', {
                method: 'DELETE',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({ recording_ids: ids })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to delete recordings');
            }

            // Remove deleted recordings from local state
            const deletedIds = new Set(data.deleted_ids || ids);
            recordings.value = recordings.value.filter(r => !deletedIds.has(r.id));

            // Clear selected recording if it was deleted
            if (selectedRecording.value && deletedIds.has(selectedRecording.value.id)) {
                selectedRecording.value = null;
            }

            // Remove deleted IDs from selection
            deletedIds.forEach(id => selectedRecordingIds.value.delete(id));

            const count = deletedIds.size;
            showToast(`${count} recording${count !== 1 ? 's' : ''} deleted`, 'fa-trash', 3000, 'success');
        } catch (error) {
            console.error('Bulk delete error:', error);
            setGlobalError(`Failed to delete recordings: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    // =========================================
    // Bulk Tag Operations
    // =========================================

    const openBulkTagModal = (action = 'add') => {
        bulkTagAction.value = action;
        bulkTagSelectedId.value = '';
        showBulkTagModal.value = true;
    };

    const closeBulkTagModal = () => {
        showBulkTagModal.value = false;
        bulkTagSelectedId.value = '';
    };

    const executeBulkTag = async () => {
        const ids = getSelectedIds();
        const tagId = bulkTagSelectedId.value;
        const action = bulkTagAction.value;

        // Validate before making API call
        if (ids.length === 0) {
            console.warn('No recordings selected for bulk tag operation');
            return;
        }
        if (!tagId && tagId !== 0) {
            console.warn('No tag selected for bulk tag operation');
            return;
        }

        bulkActionInProgress.value = true;
        closeBulkTagModal();

        try {
            const response = await fetch('/api/recordings/bulk-tags', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    tag_id: parseInt(tagId),
                    action: action
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || `Failed to ${action} tag`);
            }

            // Update local state
            const tag = availableTags.value.find(t => t.id == tagId);
            if (tag) {
                const affectedIds = new Set(data.affected_ids || ids);
                recordings.value.forEach(recording => {
                    if (affectedIds.has(recording.id)) {
                        if (!recording.tags) recording.tags = [];

                        if (action === 'add') {
                            // Add tag if not already present
                            if (!recording.tags.find(t => t.id === tag.id)) {
                                recording.tags.push(tag);
                            }
                        } else {
                            // Remove tag
                            recording.tags = recording.tags.filter(t => t.id !== tag.id);
                        }
                    }
                });

                // Update selected recording if affected
                if (selectedRecording.value && affectedIds.has(selectedRecording.value.id)) {
                    if (!selectedRecording.value.tags) selectedRecording.value.tags = [];

                    if (action === 'add') {
                        if (!selectedRecording.value.tags.find(t => t.id === tag.id)) {
                            selectedRecording.value.tags.push(tag);
                        }
                    } else {
                        selectedRecording.value.tags = selectedRecording.value.tags.filter(t => t.id !== tag.id);
                    }
                }
            }

            const count = data.affected_ids?.length || ids.length;
            const actionText = action === 'add' ? 'added to' : 'removed from';
            showToast(`Tag ${actionText} ${count} recording${count !== 1 ? 's' : ''}`, 'fa-tags', 3000, 'success');
        } catch (error) {
            console.error('Bulk tag error:', error);
            setGlobalError(`Failed to ${action} tag: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    // =========================================
    // Bulk Reprocess
    // =========================================

    const openBulkReprocessModal = () => {
        bulkReprocessType.value = 'summary';
        showBulkReprocessModal.value = true;
    };

    const closeBulkReprocessModal = () => {
        showBulkReprocessModal.value = false;
    };

    const executeBulkReprocess = async () => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        bulkActionInProgress.value = true;
        closeBulkReprocessModal();

        try {
            const response = await fetch('/api/recordings/bulk-reprocess', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    type: bulkReprocessType.value
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to queue reprocessing');
            }

            // Update status for queued recordings
            const queuedIds = new Set(data.queued_ids || ids);
            const newStatus = bulkReprocessType.value === 'transcription' ? 'PROCESSING' : 'SUMMARIZING';

            recordings.value.forEach(recording => {
                if (queuedIds.has(recording.id)) {
                    recording.status = newStatus;
                    // Start polling for each
                    if (startReprocessingPoll) {
                        startReprocessingPoll(recording.id);
                    }
                }
            });

            if (selectedRecording.value && queuedIds.has(selectedRecording.value.id)) {
                selectedRecording.value.status = newStatus;
            }

            const count = queuedIds.size;
            const baseKey = bulkReprocessType.value === 'summary'
                ? 'bulkReprocessModal.queuedSummaryToast'
                : 'bulkReprocessModal.queuedTranscriptionToast';
            const toastKey = count === 1 ? baseKey : `${baseKey}Plural`;
            showToast(t(toastKey, { count }), 'fa-sync-alt', 3000, 'success');
        } catch (error) {
            console.error('Bulk reprocess error:', error);
            setGlobalError(`Failed to queue reprocessing: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    // =========================================
    // Merge Recordings (issue #323)
    // =========================================

    const openBulkMergeModal = () => {
        // Seed the ordered list from the current selection. Order matters for a
        // merge, so present the selection as an explicit, reorderable list.
        mergeMode.value = 'bulk';
        mergeOrderedList.value = selectedRecordings.value.slice();
        mergeTitle.value = '';
        mergeDeleteOriginals.value = false;
        mergeNotesSourceId.value = null;
        mergeAddPickerOpen.value = false;
        mergeAddSearch.value = '';
        showBulkMergeModal.value = true;
    };

    // Open the merge modal for a just-recorded clip that has NOT been uploaded
    // yet. The list starts with a virtual placeholder for the clip; the user
    // adds existing recordings to merge it into. Defaults to replacing the
    // sources (the chosen behavior for this flow). ``clipNotes`` carries the
    // notes typed in the recording view so the clip can be a notes source.
    const openMergeForRecording = ({ clipNotes = '' } = {}) => {
        mergeMode.value = 'recording';
        mergeOrderedList.value = [{
            id: '__self__', __self__: true,
            title: _t('mergeRecordings.thisRecording', 'This recording'),
            notes: clipNotes || '',
        }];
        mergeTitle.value = '';
        mergeDeleteOriginals.value = true;
        mergeNotesSourceId.value = null;
        mergeAddPickerOpen.value = false;
        mergeAddSearch.value = '';
        showBulkMergeModal.value = true;
    };

    const closeBulkMergeModal = () => {
        showBulkMergeModal.value = false;
        mergeAddPickerOpen.value = false;
        mergeAddSearch.value = '';
    };

    // Open the merge modal seeded with a specific set of recordings and options.
    // Used by the "merge this recording with an existing one" flow (upload view)
    // — it seeds the just-recorded clip and defaults to replacing the sources.
    const openMergeWith = (seedRecordings, { deleteOriginals = false, title = '' } = {}) => {
        mergeMode.value = 'bulk';
        mergeOrderedList.value = (seedRecordings || []).slice();
        mergeTitle.value = title || '';
        mergeDeleteOriginals.value = !!deleteOriginals;
        mergeNotesSourceId.value = null;
        mergeAddPickerOpen.value = false;
        mergeAddSearch.value = '';
        showBulkMergeModal.value = true;
    };

    const addMergeCandidate = (recording) => {
        if (!recording) return;
        if (mergeOrderedList.value.some(r => r.id === recording.id)) return;
        const list = mergeOrderedList.value.slice();
        // In recording mode the just-recorded clip is a CONTINUATION of the
        // existing recording(s) — the common case is an interrupted recording
        // restarted — so newly added existing recordings go BEFORE the clip and
        // the clip stays at the end by default (still reorderable). In bulk mode
        // there is no clip; just append.
        const selfIdx = list.findIndex(r => r.__self__);
        if (mergeMode.value === 'recording' && selfIdx !== -1) {
            list.splice(selfIdx, 0, recording);
        } else {
            list.push(recording);
        }
        mergeOrderedList.value = list;
        mergeAddSearch.value = '';
        mergeAddPickerOpen.value = false;
    };

    const removeMergeItem = (index) => {
        // The virtual clip entry is the anchor of a recording-mode merge and
        // cannot be removed.
        if (mergeOrderedList.value[index] && mergeOrderedList.value[index].__self__) return;
        const next = mergeOrderedList.value.slice();
        next.splice(index, 1);
        mergeOrderedList.value = next;
    };

    const moveMergeItem = (index, direction) => {
        const list = mergeOrderedList.value;
        const target = index + direction;
        if (target < 0 || target >= list.length) return;
        const next = list.slice();
        [next[index], next[target]] = [next[target], next[index]];
        mergeOrderedList.value = next;
    };

    const executeBulkMerge = async () => {
        if (mergeOrderedList.value.length < 2) return;

        // Recording mode: hand off to the recorder, which finalizes the session
        // carrying the ordered merge intent (with '__self__' marking the clip).
        // The server routes the stitched clip straight into the merge — no
        // standalone transcription of the clip.
        if (mergeMode.value === 'recording') {
            const orderedSpec = mergeOrderedList.value.map(r => (r.__self__ ? '__self__' : r.id));
            const hasExisting = orderedSpec.some(x => x !== '__self__');
            if (!hasExisting || typeof finalizeRecordingMerge !== 'function') return;
            const title = mergeTitle.value.trim() || undefined;
            const deleteOriginals = mergeDeleteOriginals.value;
            // notes source: 'none' -> keep none (null); else the id or '__self__'.
            const notesSource = mergeNotesSourceId.value === 'none' ? null : mergeNotesSourceId.value;
            closeBulkMergeModal();
            try {
                await finalizeRecordingMerge(orderedSpec, { deleteOriginals, title, notesSource });
            } catch (error) {
                console.error('Recording merge finalize error:', error);
                setGlobalError(`Failed to start merge: ${error.message}`);
            }
            return;
        }

        const orderedIds = mergeOrderedList.value.map(r => r.id);
        if (orderedIds.length < 2) return;

        mergeInProgress.value = true;
        bulkActionInProgress.value = true;

        try {
            const response = await fetch('/api/recordings/merge', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: orderedIds,
                    title: mergeTitle.value.trim() || undefined,
                    delete_originals: mergeDeleteOriginals.value,
                    // 'none' -> keep no notes (null); otherwise the chosen source id.
                    notes_source_id: mergeNotesSourceId.value === 'none' ? null : mergeNotesSourceId.value
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to merge recordings');
            }

            // Show the merged recording immediately (in PROCESSING) for instant
            // feedback. Everything after this — advancing it to COMPLETED and
            // removing the deleted source recordings once the concat finishes —
            // is handled by the single merge-aware reconcile in watch(allJobs),
            // the same path the merge-from-recording flow relies on. This keeps
            // one source of truth for "a merge landed" instead of duplicating the
            // add/remove logic here.
            if (data.recording) {
                recordings.value.unshift(data.recording);
            }

            closeBulkMergeModal();
            if (exitSelectionMode) exitSelectionMode();

            const count = orderedIds.length;
            showToast(`Merging ${count} recordings — the combined recording is processing`, 'fa-object-group', 4000, 'success');
        } catch (error) {
            console.error('Merge error:', error);
            setGlobalError(`Failed to merge recordings: ${error.message}`);
        } finally {
            mergeInProgress.value = false;
            bulkActionInProgress.value = false;
        }
    };

    // =========================================
    // Bulk Toggle (Inbox/Highlight)
    // =========================================

    const bulkToggleInbox = async (value = null) => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        // If no value specified, toggle based on majority
        if (value === null) {
            const inboxCount = selectedRecordings.value.filter(r => r.is_inbox).length;
            value = inboxCount < ids.length / 2;
        }

        bulkActionInProgress.value = true;

        try {
            const response = await fetch('/api/recordings/bulk-toggle', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    field: 'inbox',
                    value: value
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to update inbox status');
            }

            // Update local state
            const affectedIds = new Set(data.affected_ids || ids);
            recordings.value.forEach(recording => {
                if (affectedIds.has(recording.id)) {
                    recording.is_inbox = value;
                }
            });

            if (selectedRecording.value && affectedIds.has(selectedRecording.value.id)) {
                selectedRecording.value.is_inbox = value;
            }

            const count = affectedIds.size;
            const actionText = value ? 'added to' : 'removed from';
            showToast(`${count} recording${count !== 1 ? 's' : ''} ${actionText} inbox`, 'fa-inbox', 3000, 'success');
        } catch (error) {
            console.error('Bulk toggle inbox error:', error);
            setGlobalError(`Failed to update inbox status: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    const bulkToggleHighlight = async (value = null) => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        // If no value specified, toggle based on majority
        if (value === null) {
            const highlightCount = selectedRecordings.value.filter(r => r.is_highlighted).length;
            value = highlightCount < ids.length / 2;
        }

        bulkActionInProgress.value = true;

        try {
            const response = await fetch('/api/recordings/bulk-toggle', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    field: 'highlight',
                    value: value
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to update highlight status');
            }

            // Update local state
            const affectedIds = new Set(data.affected_ids || ids);
            recordings.value.forEach(recording => {
                if (affectedIds.has(recording.id)) {
                    recording.is_highlighted = value;
                }
            });

            if (selectedRecording.value && affectedIds.has(selectedRecording.value.id)) {
                selectedRecording.value.is_highlighted = value;
            }

            const count = affectedIds.size;
            const actionText = value ? 'highlighted' : 'unhighlighted';
            showToast(`${count} recording${count !== 1 ? 's' : ''} ${actionText}`, 'fa-star', 3000, 'success');
        } catch (error) {
            console.error('Bulk toggle highlight error:', error);
            setGlobalError(`Failed to update highlight status: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    // =========================================
    // Bulk Folder Assignment
    // =========================================

    const bulkAssignFolder = async (folderId) => {
        const ids = getSelectedIds();
        if (ids.length === 0) return;

        bulkActionInProgress.value = true;
        showBulkFolderModal.value = false;

        try {
            const response = await fetch('/api/recordings/bulk/folder', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify({
                    recording_ids: ids,
                    folder_id: folderId
                })
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Failed to update folders');
            }

            // Update local state
            const folder = folderId ? availableFolders.value.find(f => f.id === folderId) : null;
            recordings.value.forEach(recording => {
                if (ids.includes(recording.id)) {
                    recording.folder_id = folderId;
                    recording.folder = folder;
                }
            });

            // Update selected recording if affected
            if (selectedRecording.value && ids.includes(selectedRecording.value.id)) {
                selectedRecording.value.folder_id = folderId;
                selectedRecording.value.folder = folder;
            }

            // Update folder recording counts
            if (availableFolders.value) {
                availableFolders.value.forEach(f => {
                    const count = recordings.value.filter(r => r.folder_id === f.id).length;
                    f.recording_count = count;
                });
            }

            const count = data.updated_count || ids.length;
            if (folderId) {
                showToast(`${count} recording${count !== 1 ? 's' : ''} moved to "${folder?.name || 'folder'}"`, 'fa-folder', 3000, 'success');
            } else {
                showToast(`${count} recording${count !== 1 ? 's' : ''} removed from folder`, 'fa-folder-minus', 3000, 'success');
            }
        } catch (error) {
            console.error('Bulk folder assignment error:', error);
            setGlobalError(`Failed to update folders: ${error.message}`);
        } finally {
            bulkActionInProgress.value = false;
        }
    };

    return {
        // Modal state
        showBulkDeleteModal,
        showBulkTagModal,
        showBulkReprocessModal,
        showBulkFolderModal,
        showBulkMergeModal,
        bulkTagAction,
        bulkTagSelectedId,
        bulkReprocessType,
        mergeOrderedList,
        mergeTitle,
        mergeDeleteOriginals,
        mergeInProgress,
        mergeAddPickerOpen,
        mergeAddSearch,
        mergeCandidates,
        mergePickerLoading,
        mergePickerHasMore,
        loadMoreMergeCandidates,
        mergeMode,
        mergeNotesSourceId,
        mergeNotesCandidates,

        // Bulk Delete
        openBulkDeleteModal,
        closeBulkDeleteModal,
        executeBulkDelete,

        // Bulk Tag
        openBulkTagModal,
        closeBulkTagModal,
        executeBulkTag,

        // Bulk Reprocess
        openBulkReprocessModal,
        closeBulkReprocessModal,
        executeBulkReprocess,

        // Merge
        openBulkMergeModal,
        openMergeForRecording,
        closeBulkMergeModal,
        openMergeWith,
        addMergeCandidate,
        removeMergeItem,
        moveMergeItem,
        executeBulkMerge,

        // Bulk Toggle
        bulkToggleInbox,
        bulkToggleHighlight,

        // Bulk Folder
        bulkAssignFolder
    };
}
