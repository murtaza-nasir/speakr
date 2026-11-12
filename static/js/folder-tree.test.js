import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// folder-tree.js is a classic script that sets window.SpeakrFolderTree.
const SOURCE = readFileSync(fileURLToPath(new URL('./folder-tree.js', import.meta.url)), 'utf8');

function load() {
    const window = {};
    vm.runInContext(SOURCE, vm.createContext({ window }));
    return window.SpeakrFolderTree;
}

const folder = (id, name, parent_id = null, group_id = null) => ({ id, name, parent_id, group_id });

describe('orderTree', () => {
    it('keeps a flat list in its order, all at depth 0', () => {
        const T = load();
        const flat = [folder(3, 'Courses'), folder(1, 'Hiring'), folder(2, 'Team', null, 7)];
        const ordered = T.orderTree(flat);
        expect(ordered.map((f) => f.id)).toEqual([3, 1, 2]);
        expect(ordered.map((f) => f.depth)).toEqual([0, 0, 0]);
        expect(ordered.map((f) => T.pathLabel(f))).toEqual(['Courses', 'Hiring', 'Team']);
    });

    it('puts each child after its parent, depth-first, siblings in order', () => {
        const T = load();
        const list = [
            folder(1, 'Alpha', 4), folder(2, 'Beta', 4), folder(3, 'Courses'),
            folder(4, 'Projects'), folder(5, '2026', 1),
        ];
        const ordered = T.orderTree(list);
        expect(ordered.map((f) => f.name)).toEqual(['Courses', 'Projects', 'Alpha', '2026', 'Beta']);
        expect(ordered.map((f) => f.depth)).toEqual([0, 0, 1, 2, 1]);
        const year = ordered.find((f) => f.id === 5);
        expect(T.pathLabel(year)).toBe('Projects / Alpha / 2026');
        expect(T.pathLabel(ordered.find((f) => f.id === 4))).toBe('Projects');
        // Option text is the path with no leading indent (closed selects).
        expect(ordered.every((f) => !/^[\s\u00a0]/.test(T.pathLabel(f)))).toBe(true);
        expect(T.indentedName).toBeUndefined();
    });

    it('treats a parent in another scope or missing from the list as top level', () => {
        const T = load();
        const ordered = T.orderTree([folder(1, 'Mine'), folder(2, 'Group child', 1, 9), folder(3, 'Orphan', 99)]);
        expect(ordered.map((f) => [f.name, f.depth])).toEqual([['Mine', 0], ['Group child', 0], ['Orphan', 0]]);
    });

    it('keeps every folder of a cycle in bad data', () => {
        const T = load();
        const ordered = T.orderTree([folder(1, 'A', 2), folder(2, 'B', 1), folder(3, 'C')]);
        expect(ordered.map((f) => f.id).sort()).toEqual([1, 2, 3]);
    });
});

describe('parent candidates', () => {
    const list = () => [
        folder(1, 'Projects'), folder(2, 'Alpha', 1), folder(3, '2026', 2), folder(4, 'Hiring'),
        folder(5, 'Team root', null, 7), folder(6, 'Team child', 5, 7), folder(8, 'Other group', null, 9),
    ];

    it('offers personal folders for a personal folder', () => {
        const T = load();
        expect(T.parentCandidates(list(), '', null).map((f) => f.id)).toEqual([1, 2, 3, 4]);
    });

    it('offers only that group\'s folders for a group folder', () => {
        const T = load();
        expect(T.parentCandidates(list(), '7', null).map((f) => f.id)).toEqual([5, 6]);
        expect(T.parentCandidates(list(), 7, null).map((f) => f.id)).toEqual([5, 6]);
    });

    it('leaves out the edited folder and its descendants', () => {
        const T = load();
        expect(T.parentCandidates(list(), '', 2).map((f) => f.id)).toEqual([1, 4]);
        expect([...T.descendantIds(list(), 1)].sort()).toEqual([2, 3]);
        expect(T.hasChildren(list(), 3)).toBe(false);
        expect(T.hasChildren(list(), 5)).toBe(true);
    });
});
