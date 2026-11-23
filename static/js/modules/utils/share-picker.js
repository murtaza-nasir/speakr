/**
 * Share picker helpers (D#517, D#519): the search, the selection of people
 * and the result of a batch share, without Vue or the DOM.
 */

// The server returns nothing for a shorter query
export const MIN_SHARE_SEARCH_CHARS = 2;
// Same cap as MAX_SHARE_RECIPIENTS in src/api/shares.py
export const MAX_SHARE_SELECTION = 25;

// Levels of the user_search_scope setting (src/services/user_lookup.py)
export const USER_SEARCH_SCOPES = ['everyone', 'groups', 'exact'];

/**
 * The search scope from /api/config. A server that does not send one (an
 * older version) searched everyone with usernames shown, else exact names.
 */
export function resolveSearchScope(scope, showUsernames) {
    if (USER_SEARCH_SCOPES.includes(scope)) return scope;
    return showUsernames === true ? 'everyone' : 'exact';
}

/** Whether the share window searches while typing (not at the exact level). */
export function searchesWhileTyping(scope) {
    return scope === 'everyone' || scope === 'groups';
}

/**
 * Whether an empty result needs the hint to type the full username or email:
 * at every level but everyone, part of a name does not find every account.
 */
export function needsFullIdentifierHint(scope) {
    return scope !== 'everyone';
}

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
 * The recipients of a share request. The server accepts a user id only for
 * an account the person sharing could find in the search
 * (src/services/user_lookup.py), so each id goes with the username the search
 * returned for it; the server looks that username up again.
 */
export function shareRecipients(selected) {
    const people = (selected || []).filter(user => user && user.id);
    return {
        user_ids: people.map(user => user.id),
        identifiers: people.map(user => user.username).filter(name => typeof name === 'string' && name.trim()),
    };
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
 * Whether the signed-in user may create public links now (#685): public
 * sharing is on and the user has the permission. Anything but a clear yes
 * from the server counts as no, so the dialog never offers a link the server
 * refuses.
 */
export async function fetchCanSharePublicly(fetchFn) {
    try {
        const response = await fetchFn('/api/permissions/can-share-publicly');
        if (!response.ok) return false;
        const data = await response.json();
        return data.can_share_publicly === true;
    } catch (_) {
        return false;
    }
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
