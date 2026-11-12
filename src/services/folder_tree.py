"""Folder-tree lookups for nested folders (#585).

A folder's parent is always in the folder's own scope: the same user's
personal folders, or the same group. Every lookup here follows parent links
only within that scope, so a stale link left by an older database never
joins two scopes together.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from src.database import db
from src.models import Folder, Recording


def _same_scope(model, folder):
    """SQL condition: rows of `model` in the same scope as `folder`."""
    if folder.group_id is None:
        return db.and_(model.user_id == folder.user_id, model.group_id.is_(None))
    return model.group_id == folder.group_id


def _scope_key(folder):
    return ('group', folder.group_id) if folder.group_id is not None else ('user', folder.user_id)


def descendant_folder_ids(folder):
    """The ids of `folder` and of all its descendants in the same scope.

    One recursive query. UNION (not UNION ALL) drops rows already found, so
    a cycle in bad data ends the recursion on SQLite and on PostgreSQL.
    """
    tree = select(Folder.id).where(Folder.id == folder.id).cte('folder_subtree', recursive=True)
    child = aliased(Folder)
    tree = tree.union(
        select(child.id).where(child.parent_id == tree.c.id, _same_scope(child, folder))
    )
    return {folder_id for (folder_id,) in db.session.execute(select(tree.c.id))}


def folder_filter_ids(folder_id):
    """Folder ids for a recording-list filter with include_children=true.

    An id that matches no folder filters on that id alone, as the direct
    filter does.
    """
    folder = db.session.get(Folder, folder_id)
    if folder is None:
        return {folder_id}
    return descendant_folder_ids(folder)


def subtree_recording_count(folder):
    """Recordings in `folder` and all its descendants."""
    ids = descendant_folder_ids(folder)
    return db.session.query(func.count(Recording.id)).filter(Recording.folder_id.in_(ids)).scalar() or 0


def recursive_recording_counts(folders, direct_counts):
    """Each folder's direct count plus the counts of all its descendants.

    folders: every folder of each scope involved (a folder list holds all of
    a viewer's personal folders and all folders of the viewer's groups), so
    no descendant is missing. direct_counts: {folder_id: count}.
    """
    by_id = {folder.id: folder for folder in folders}
    children = {}
    for folder in folders:
        parent = by_id.get(folder.parent_id)
        if parent is not None and _scope_key(parent) == _scope_key(folder):
            children.setdefault(parent.id, []).append(folder.id)

    totals = {}
    for folder in folders:
        seen, pending, total = set(), [folder.id], 0
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            total += direct_counts.get(current, 0)
            pending.extend(children.get(current, []))
        totals[folder.id] = total
    return totals
