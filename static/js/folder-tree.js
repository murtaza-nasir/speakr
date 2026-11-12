/**
 * Nested folder helpers (#585), shared by the main app, the account page and
 * the folder modal. A classic script: it sets window.SpeakrFolderTree.
 *
 * Folders come from GET /api/folders as a flat list with parent_id. A
 * parent is always in the same scope (the same user's personal folders, or
 * the same group); a link into another scope or to a folder missing from
 * the list is treated as top level, as the server does.
 *
 * With no parent links, orderTree returns the list in its original order
 * with depth 0 everywhere, so every list looks exactly as before.
 */
(function (root) {
    'use strict';

    function scopeKey(folder) {
        return folder.group_id !== null && folder.group_id !== undefined
            ? 'group:' + folder.group_id
            : 'user';
    }

    function parentOf(folder, byId) {
        if (folder.parent_id === null || folder.parent_id === undefined) return null;
        const parent = byId.get(folder.parent_id);
        return parent && scopeKey(parent) === scopeKey(folder) ? parent : null;
    }

    /**
     * The folders in depth-first order, each child right after its parent,
     * siblings in their original order. Sets folder.depth (0 for top level)
     * and folder.path (the names from the top folder down to this one) on
     * each folder, and returns a new array of the same objects.
     */
    function orderTree(folders) {
        const list = Array.isArray(folders) ? folders : [];
        const byId = new Map(list.map((f) => [f.id, f]));
        const children = new Map();
        const roots = [];
        list.forEach((folder) => {
            const parent = parentOf(folder, byId);
            if (parent) {
                if (!children.has(parent.id)) children.set(parent.id, []);
                children.get(parent.id).push(folder);
            } else {
                roots.push(folder);
            }
        });

        const ordered = [];
        const seen = new Set();
        const visit = (folder, depth, path) => {
            if (seen.has(folder.id)) return;
            seen.add(folder.id);
            folder.depth = depth;
            folder.path = path.concat(folder.name);
            ordered.push(folder);
            (children.get(folder.id) || []).forEach((child) => visit(child, depth + 1, folder.path));
        };
        roots.forEach((folder) => visit(folder, 0, []));
        // Folders on a cycle in bad data have no top-level ancestor; list
        // them at the end as top level so none disappears.
        list.forEach((folder) => visit(folder, 0, []));
        return ordered;
    }

    /**
     * "Projects / Alpha / 2026" for a folder that orderTree has seen. Used
     * as <option> text: a closed native select shows only the selected
     * option, so leading spaces would hide where a subfolder sits, and an
     * option cannot show different text open and closed.
     */
    function pathLabel(folder) {
        return folder && Array.isArray(folder.path) ? folder.path.join(' / ') : (folder ? folder.name : '');
    }

    /** The ids of a folder's descendants in the same scope (not the folder itself). */
    function descendantIds(folders, folderId) {
        const list = Array.isArray(folders) ? folders : [];
        const byId = new Map(list.map((f) => [f.id, f]));
        const result = new Set();
        let changed = true;
        while (changed) {
            changed = false;
            list.forEach((folder) => {
                const parent = parentOf(folder, byId);
                if (parent && (parent.id === folderId || result.has(parent.id))
                        && folder.id !== folderId && !result.has(folder.id)) {
                    result.add(folder.id);
                    changed = true;
                }
            });
        }
        return result;
    }

    /** Whether any other folder in the list has this folder as its parent. */
    function hasChildren(folders, folderId) {
        return descendantIds(folders, folderId).size > 0;
    }

    /**
     * Folders that can be the parent of a folder in the given scope: the
     * personal folders when groupId is empty, otherwise the folders of that
     * group. The folder being edited and its descendants are left out.
     * Returned in tree order.
     */
    function parentCandidates(folders, groupId, editingId) {
        const scope = groupId ? 'group:' + groupId : 'user';
        const blocked = editingId ? descendantIds(folders, editingId) : new Set();
        if (editingId) blocked.add(editingId);
        return orderTree(folders).filter((f) => scopeKey(f) === scope && !blocked.has(f.id));
    }

    root.SpeakrFolderTree = {
        orderTree, pathLabel, descendantIds, hasChildren, parentCandidates, scopeKey,
    };
})(typeof window !== 'undefined' ? window : globalThis);
