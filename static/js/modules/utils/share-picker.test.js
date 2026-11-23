import { describe, expect, it } from 'vitest';
import {
    MAX_SHARE_SELECTION,
    availableSearchResults,
    canGrantPermission,
    changedPermissions,
    fetchCanSharePublicly,
    isSearchableQuery,
    needsFullIdentifierHint,
    resolveSearchScope,
    searchesWhileTyping,
    shareRecipients,
    summarizeBatchResults,
    toggleSelection,
} from './share-picker.js';

const ann = { id: 2, username: 'ann', email: 'ann@example.com' };
const bob = { id: 3, username: 'bob', email: 'bob@example.com' };

describe('user search scope', () => {
    it('keeps a level the server sent', () => {
        expect(resolveSearchScope('everyone', false)).toBe('everyone');
        expect(resolveSearchScope('groups', false)).toBe('groups');
        expect(resolveSearchScope('exact', true)).toBe('exact');
    });

    it('follows the username setting when the server sent no level', () => {
        expect(resolveSearchScope(undefined, true)).toBe('everyone');
        expect(resolveSearchScope(undefined, false)).toBe('exact');
        expect(resolveSearchScope('all', true)).toBe('everyone');
        expect(resolveSearchScope(null, undefined)).toBe('exact');
    });

    it('searches while typing at every level but exact', () => {
        expect(searchesWhileTyping('everyone')).toBe(true);
        expect(searchesWhileTyping('groups')).toBe(true);
        expect(searchesWhileTyping('exact')).toBe(false);
        expect(searchesWhileTyping(undefined)).toBe(false);
    });

    it('asks for the full username or email unless everyone is searched', () => {
        expect(needsFullIdentifierHint('everyone')).toBe(false);
        expect(needsFullIdentifierHint('groups')).toBe(true);
        expect(needsFullIdentifierHint('exact')).toBe(true);
    });
});

describe('isSearchableQuery', () => {
    it('needs two characters after trimming', () => {
        expect(isSearchableQuery('')).toBe(false);
        expect(isSearchableQuery(' a ')).toBe(false);
        expect(isSearchableQuery('an')).toBe(true);
        expect(isSearchableQuery(undefined)).toBe(false);
    });
});

describe('availableSearchResults', () => {
    it('leaves out people who already have the recording and people already selected', () => {
        const shares = [{ user_id: 1, is_owner: true }, { user_id: 2 }];
        const results = [{ id: 1 }, ann, bob, { id: 4 }];
        expect(availableSearchResults(results, shares, [{ id: 4 }]).map(u => u.id)).toEqual([3]);
    });
});

describe('toggleSelection', () => {
    it('adds and removes a person', () => {
        const added = toggleSelection([], ann);
        expect(added.selected.map(u => u.id)).toEqual([2]);
        expect(toggleSelection(added.selected, ann).selected).toEqual([]);
    });

    it('stops at the cap', () => {
        const full = Array.from({ length: MAX_SHARE_SELECTION }, (_, i) => ({ id: 100 + i }));
        const result = toggleSelection(full, bob);
        expect(result.limited).toBe(true);
        expect(result.selected).toHaveLength(MAX_SHARE_SELECTION);
        expect(toggleSelection(full, { id: 100 }).selected).toHaveLength(MAX_SHARE_SELECTION - 1);
    });
});

describe('summarizeBatchResults', () => {
    it('counts each outcome and keeps the people who could not be added', () => {
        const summary = summarizeBatchResults([
            { user_id: 2, status: 'shared' },
            { user_id: 3, status: 'already_shared' },
            { user_id: 4, status: 'not_found' },
            { user_id: 5, status: 'share_with_owner' },
        ]);
        expect(summary).toEqual({ shared: 1, alreadyShared: 1, failed: 2, keepIds: [4, 5] });
    });
});

describe('shareRecipients', () => {
    it('names each selected person by id and by username', () => {
        expect(shareRecipients([ann, bob])).toEqual({ user_ids: [2, 3], identifiers: ['ann', 'bob'] });
    });

    it('is empty for an empty selection', () => {
        expect(shareRecipients([])).toEqual({ user_ids: [], identifiers: [] });
        expect(shareRecipients(null)).toEqual({ user_ids: [], identifiers: [] });
    });

    it('leaves out a missing username and keeps the id', () => {
        expect(shareRecipients([{ id: 4 }, ann])).toEqual({ user_ids: [4, 2], identifiers: ['ann'] });
    });
});

describe('canGrantPermission', () => {
    it('allows a flag that is on, or one the grantor holds', () => {
        expect(canGrantPermission({ can_edit: true }, { can_edit: false }, 'can_edit')).toBe(true);
        expect(canGrantPermission({ can_edit: false }, { can_edit: true }, 'can_edit')).toBe(true);
        expect(canGrantPermission({ can_edit: false }, { can_edit: false }, 'can_edit')).toBe(false);
    });
});

describe('changedPermissions', () => {
    it('returns only the changed flags', () => {
        expect(changedPermissions({ can_edit: false, can_reshare: true }, { can_edit: true, can_reshare: true }))
            .toEqual({ can_edit: true });
        expect(changedPermissions({ can_edit: true, can_reshare: false }, { can_edit: true, can_reshare: false }))
            .toBeNull();
    });
});

// #685: the share dialog offers public links only on a clear yes
describe('fetchCanSharePublicly', () => {
    const answer = (ok, body) => async (url) => {
        expect(url).toBe('/api/permissions/can-share-publicly');
        return { ok, json: async () => body };
    };

    it('is true when the server says so', async () => {
        expect(await fetchCanSharePublicly(answer(true, { can_share_publicly: true }))).toBe(true);
    });

    it('is false when public sharing is off or not permitted', async () => {
        expect(await fetchCanSharePublicly(answer(true, { can_share_publicly: false }))).toBe(false);
    });

    it('is false on an error answer, a missing field or a failed request', async () => {
        expect(await fetchCanSharePublicly(answer(false, { can_share_publicly: true }))).toBe(false);
        expect(await fetchCanSharePublicly(answer(true, {}))).toBe(false);
        expect(await fetchCanSharePublicly(async () => { throw new Error('offline'); })).toBe(false);
    });
});
