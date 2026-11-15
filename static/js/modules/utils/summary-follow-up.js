/**
 * What follows a save that asked for the summary to be regenerated
 * (regenerate_summary: true on /recording/<id>/update_transcript or
 * /update_speakers, which queue a summarize job and answer with
 * summary_queued). The recording is shown as summarizing, and its status is
 * polled until the new summary is there. One code path for the Identify
 * Speakers dialog and the segment editor.
 */

const POLL_MS = 3000;
const MAX_ATTEMPTS = 40;     // two minutes

export function createSummaryFollowUp({
    recordings, selectedRecording, showToast, t, nextTick = null, onDone = null,
    fetchImpl = null, pollMs = POLL_MS, maxAttempts = MAX_ATTEMPTS,
}) {
    const doFetch = (...args) => (fetchImpl || fetch)(...args);

    const replaceInList = (recordingId, make) => {
        const index = recordings.value.findIndex((r) => r.id === recordingId);
        if (index !== -1) recordings.value[index] = make(recordings.value[index]);
    };
    const replaceSelected = (recordingId, make) => {
        if (selectedRecording.value && selectedRecording.value.id === recordingId) {
            selectedRecording.value = make(selectedRecording.value);
        }
    };

    /** Poll the status of the recording until the summary is done, has failed, or two minutes have passed. */
    const poll = (recordingId) => {
        let attempts = 0;
        const timer = setInterval(async () => {
            attempts++;
            try {
                // The lightweight status-only endpoint
                const response = await doFetch(`/recording/${recordingId}/status`);
                if (!response.ok) {
                    clearInterval(timer);
                    return;
                }
                const { status } = await response.json();
                // New objects, so that Vue sees the change
                replaceInList(recordingId, (recording) => ({ ...recording, status }));
                replaceSelected(recordingId, (recording) => ({ ...recording, status }));

                if (status === 'COMPLETED') {
                    clearInterval(timer);
                    // The full recording, with the new summary
                    const fullResponse = await doFetch(`/api/recordings/${recordingId}`);
                    if (fullResponse.ok) {
                        const full = await fullResponse.json();
                        replaceInList(recordingId, () => full);
                        replaceSelected(recordingId, () => full);
                        if (nextTick) await nextTick();
                    }
                    showToast(t('help.summaryUpdated'), 'fa-check-circle');
                    // The token budget changed with the LLM call
                    if (onDone) onDone();
                } else if (status === 'FAILED' || status === 'ERROR') {
                    clearInterval(timer);
                    showToast(t('help.summaryGenerationFailed'), 'fa-exclamation-circle', 3000, 'error');
                } else if (attempts >= maxAttempts) {
                    clearInterval(timer);
                    showToast(t('help.summaryGenerationTimedOut'), 'fa-clock', 3000, 'warning');
                }
            } catch (error) {
                console.error('Error polling for summary:', error);
                clearInterval(timer);
            }
        }, pollMs);
        return timer;
    };

    /**
     * The save queued a regeneration: show the recording as summarizing, say
     * so, and wait for the summary. `recording` is the saved recording as the
     * server returned it; without it the recording on the page only gets the
     * status (the segment editor, which stays open over it).
     */
    const started = (recordingId, recording = null) => {
        const index = recordings.value.findIndex((r) => r.id === recordingId);
        const selected = selectedRecording.value && selectedRecording.value.id === recordingId ? selectedRecording.value : null;
        const base = recording || selected || (index !== -1 ? recordings.value[index] : null);
        if (base) {
            // One object for the list and the open recording
            const summarizing = { ...base, status: 'SUMMARIZING' };
            if (index !== -1) recordings.value[index] = summarizing;
            if (selected) selectedRecording.value = summarizing;
        }
        showToast(t('help.summaryRegenerationStarted'), 'fa-sync-alt');
        return poll(recordingId);
    };

    return { started, poll };
}
