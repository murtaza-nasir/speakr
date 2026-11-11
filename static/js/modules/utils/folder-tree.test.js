import { describe, expect, it } from 'vitest';
import { flattenFolderTree, folderPath } from './folder-tree.js';

const folders = [
    { id: 1, name: 'Projects', parent_id: null },
    { id: 2, name: 'Alpha', parent_id: 1 },
    { id: 3, name: '2026', parent_id: 2 },
    { id: 4, name: 'Personal', parent_id: null },
    { id: 5, name: 'Other parent', parent_id: 999 },
];

describe('folder tree', () => {
    it('flattens only expanded branches and records depth', () => {
        const collapsed = flattenFolderTree(folders, new Set());
        expect(collapsed.map(item => item.folder.id)).toEqual([1, 4, 5]);
        expect(collapsed[0].hasChildren).toBe(true);
        const expanded = flattenFolderTree(folders, new Set([1, 2]));
        expect(expanded.map(item => [item.folder.id, item.depth])).toEqual([[1, 0], [2, 1], [3, 2], [4, 0], [5, 0]]);
    });

    it('returns the root-to-leaf path and accepts a string id', () => {
        expect(folderPath('3', folders).map(folder => folder.name)).toEqual(['Projects', 'Alpha', '2026']);
    });

    it('returns an empty path for no folder or an unknown id', () => {
        expect(folderPath('', folders)).toEqual([]);
        expect(folderPath(999, folders)).toEqual([]);
    });

    it('does not recurse forever if damaged data contains a cycle', () => {
        const cyclic = [{ id: 1, parent_id: 2 }, { id: 2, parent_id: 1 }];
        expect(() => flattenFolderTree(cyclic, new Set([1, 2]))).not.toThrow();
        expect(folderPath(1, cyclic)).toHaveLength(2);
    });
});
