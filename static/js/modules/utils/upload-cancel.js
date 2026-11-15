/**
 * What canceling an item of the upload queue means, by its state.
 *
 *   remove   the file has not been sent: it leaves the queue
 *   abort    the upload is in flight: the request is aborted and the server
 *            is told to discard what it may still produce
 *   none     nothing to cancel here; `reason` says why:
 *            'missing'       no such item
 *            'on_server'     the upload has finished; its job is canceled
 *                            through DELETE /api/recordings/jobs/<id>
 *            'only_copy'     a recording made in the app: its audio exists
 *                            only in this browser until the upload finishes,
 *                            so the upload is not canceled
 *            'finished'      the item completed or failed already
 */
export function uploadCancelPlan(item) {
    if (!item) return { action: 'none', reason: 'missing' };
    if (item.status === 'queued' || item.status === 'ready') return { action: 'remove', reason: null };
    if (item.status === 'uploading') {
        if (item.fromInProgressRecording) return { action: 'none', reason: 'only_copy' };
        return { action: 'abort', reason: null };
    }
    if (item.status === 'pending') return { action: 'none', reason: 'on_server' };
    return { action: 'none', reason: 'finished' };
}

/** Whether the panel offers a cancel for this upload queue item. */
export function canCancelUpload(item) {
    return uploadCancelPlan(item).action !== 'none';
}
