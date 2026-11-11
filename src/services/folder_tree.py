"""Folder-tree lookups shared by folder lists and recording filters."""

from src.models import Folder


def descendant_folder_ids(folder_id):
    """Return a folder and all descendants, with a guard against bad cycles."""
    rows = Folder.query.with_entities(Folder.id, Folder.parent_id).all()
    children = {}
    for child_id, parent_id in rows:
        if parent_id is not None:
            children.setdefault(parent_id, []).append(child_id)

    result = set()
    pending = [folder_id]
    while pending:
        current = pending.pop()
        if current in result:
            continue
        result.add(current)
        pending.extend(children.get(current, []))
    return result


def recursive_recording_counts(folders, direct_counts):
    """Add each folder's direct count and all descendant counts together."""
    children = {}
    for folder in folders:
        if folder.parent_id is not None:
            children.setdefault(folder.parent_id, []).append(folder.id)

    memo = {}
    def total(folder_id, visiting):
        if folder_id in memo:
            return memo[folder_id]
        if folder_id in visiting:
            return 0
        value = direct_counts.get(folder_id, 0)
        for child_id in children.get(folder_id, []):
            value += total(child_id, visiting | {folder_id})
        memo[folder_id] = value
        return value

    return {folder.id: total(folder.id, set()) for folder in folders}
