import { describe, expect, it } from 'vitest';
import {
    MAX_SHARE_SELECTION,
    availableSearchResults,
    canGrantPermission,
    changedPermissions,
    isSearchableQuery,
    summarizeBatchResults,
    toggleSelection,
} from './share-picker.js';

const ann = { id: 2, username: 'ann', email: 'ann@example.com' };
const bob = { id: 3, username: 'bob', email: 'bob@example.com' };

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
