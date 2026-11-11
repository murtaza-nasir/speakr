export function flattenFolderTree(folders, expandedIds) {
    const byParent = new Map();
    const ids = new Set(folders.map(folder => folder.id));
    for (const folder of folders) {
        const parent = ids.has(folder.parent_id) ? folder.parent_id : null;
        if (!byParent.has(parent)) byParent.set(parent, []);
        byParent.get(parent).push(folder);
    }
    const result = [];
    const visit = (parentId, depth, ancestors) => {
        for (const folder of byParent.get(parentId) || []) {
            if (ancestors.has(folder.id)) continue;
            const children = byParent.get(folder.id) || [];
            result.push({ folder, depth, hasChildren: children.length > 0 });
            if (expandedIds.has(folder.id)) visit(folder.id, depth + 1, new Set(ancestors).add(folder.id));
        }
    };
    visit(null, 0, new Set());
    return result;
}

export function folderPath(folderId, folders) {
    const byId = new Map(folders.map(folder => [folder.id, folder]));
    let current = byId.get(Number(folderId));
    const path = [];
    const seen = new Set();
    while (current && !seen.has(current.id)) {
        path.unshift(current);
        seen.add(current.id);
        current = byId.get(current.parent_id);
    }
    return path;
}
