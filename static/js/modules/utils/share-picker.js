/**
 * Share picker helpers (D#517, D#519): the search, the selection of people
 * and the result of a batch share, without Vue or the DOM.
 */

// The server returns nothing for a shorter query
export const MIN_SHARE_SEARCH_CHARS = 2;
// Same cap as MAX_SHARE_RECIPIENTS in src/api/shares.py
export const MAX_SHARE_SELECTION = 25;

/** Whether a query is long enough to be sent. */
export function isSearchableQuery(query) {
    return typeof query === 'string' && query.trim().length >= MIN_SHARE_SEARCH_CHARS;
}

/**
 * Search results without the people who already have the recording (the
 * owner row included) and without the people already selected.
 */
export function availableSearchResults(results, existingShares, selected) {
    const taken = new Set((existingShares || []).map(share => share.user_id));
    (selected || []).forEach(user => taken.add(user.id));
    return (results || []).filter(user => !taken.has(user.id));
}

/**
 * Add a person to the selection, or remove them when already selected.
 * Returns the new list and whether the cap stopped an addition.
 */
export function toggleSelection(selected, user, max = MAX_SHARE_SELECTION) {
    const current = selected || [];
    if (current.some(item => item.id === user.id)) {
        return { selected: current.filter(item => item.id !== user.id), limited: false };
    }
    if (current.length >= max) {
        return { selected: current, limited: true };
    }
    return { selected: [...current, { id: user.id, username: user.username, email: user.email || null }], limited: false };
}

/**
 * Sort the per-person results of a batch share. People who were shared with
 * or already had access leave the selection; the others stay selected so the
 * problem can be seen and corrected.
 */
export function summarizeBatchResults(results) {
    const summary = { shared: 0, alreadyShared: 0, failed: 0, keepIds: [] };
    (results || []).forEach(row => {
        if (row.status === 'shared') {
            summary.shared += 1;
        } else if (row.status === 'already_shared') {
            summary.alreadyShared += 1;
        } else {
            summary.failed += 1;
            summary.keepIds.push(row.user_id);
        }
    });
    return summary;
}

/**
 * Whether a permission can be switched on for an existing share: it is already
 * on, or the person changing it holds that permission.
 */
export function canGrantPermission(current, ceiling, flag) {
    return Boolean((current && current[flag]) || (ceiling && ceiling[flag]));
}

/** The flags that differ between a share and its edited copy, or null. */
export function changedPermissions(current, draft) {
    const changes = {};
    ['can_edit', 'can_reshare'].forEach(flag => {
        if (Boolean(draft[flag]) !== Boolean(current[flag])) {
            changes[flag] = Boolean(draft[flag]);
        }
    });
    return Object.keys(changes).length ? changes : null;
}
