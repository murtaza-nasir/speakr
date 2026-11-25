/**
 * Whether the open recording is kept after the job-queue reconcile (#702).
 *
 * The reconcile step reloads page 1 of the list when a job completes for a
 * recording that is not in the local list (an API upload, another tab, a
 * merge). The open recording is then often missing from page 1 although it
 * still exists, for example when it is further down the list or outside the
 * current search. It is cleared only when the server no longer has it.
 *
 * @param {object|null} selected  the open recording
 * @param {Array<object>} list    the recordings loaded after the reconcile
 * @param {(id: number|string) => Promise<{status: number}>} fetchRecording
 * @returns {Promise<boolean>} true to keep the open recording
 */
export async function keepSelectionAfterReconcile(selected, list, fetchRecording) {
    if (!selected) return true;
    // An incognito recording exists only in this tab and never in the list.
    if (selected.id === 'incognito' || selected.incognito) return true;
    if ((list || []).some(r => r.id === selected.id)) return true;
    try {
        const response = await fetchRecording(selected.id);
        // 404: deleted (a merged source, or deleted in another tab).
        // 403: no longer shared with this user.
        return !(response && (response.status === 404 || response.status === 403));
    } catch (e) {
        // A network error says nothing about the recording: keep it.
        return true;
    }
}
