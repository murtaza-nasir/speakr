/**
 * Local draft of the segment editor: the applied operations of one recording,
 * kept in the browser so that a closed tab or a crash does not lose edits.
 *
 * A draft belongs to the transcript it was made on (`base`, a hash of the
 * saved text). It is offered only while the saved transcript is still that
 * one, and it is removed when the edits are saved.
 */

/** A short fingerprint of a text: its length and an FNV-1a hash. */
export function hashText(text) {
    const s = String(text == null ? '' : text);
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) {
        h ^= s.charCodeAt(i);
        h = Math.imul(h, 0x01000193);
    }
    return `${s.length}:${(h >>> 0).toString(16)}`;
}

export function createDraftStore(storage, { maxChars = 1500000, prefix = 'speakr.asrDraft.' } = {}) {
    const keyOf = (recordingId, field) => `${prefix}${recordingId}.${field}`;

    const discard = (recordingId, field) => {
        try {
            storage.removeItem(keyOf(recordingId, field));
        } catch (e) { /* storage unavailable */ }
    };

    /** Store the log. Returns 'saved', 'empty', 'too-large' or 'failed'. */
    const save = (recordingId, field, base, log, savedAt = Date.now()) => {
        if (!Array.isArray(log) || !log.length) {
            discard(recordingId, field);
            return 'empty';
        }
        try {
            const text = JSON.stringify({ v: 1, base, savedAt, log });
            if (text.length > maxChars) {
                discard(recordingId, field);
                return 'too-large';
            }
            storage.setItem(keyOf(recordingId, field), text);
            return 'saved';
        } catch (e) {
            // Quota exceeded or storage unavailable: no draft is better than a partial one.
            discard(recordingId, field);
            return 'failed';
        }
    };

    /** The draft made on this transcript ({ log, savedAt }), or null. A draft of another version is removed. */
    const load = (recordingId, field, base) => {
        let text = null;
        try {
            text = storage.getItem(keyOf(recordingId, field));
        } catch (e) {
            return null;
        }
        if (!text) return null;
        try {
            const draft = JSON.parse(text);
            if (draft && draft.v === 1 && draft.base === base && Array.isArray(draft.log) && draft.log.length) {
                return { log: draft.log, savedAt: draft.savedAt || 0 };
            }
        } catch (e) { /* not a draft */ }
        discard(recordingId, field);
        return null;
    };

    return { save, load, discard, keyOf };
}
