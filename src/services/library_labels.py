"""The tags and folders one user can see on a set of recordings (Ambrose
search): for the recording chat's metadata block and the library agent's
tag and folder filters, results and names line.

Visibility follows the rules the web app applies:
- a personal tag is visible only to the user who made it, a group tag to the
  members of its group (Recording.get_visible_tags);
- the owner sees a recording's folder; anyone else sees it only while it is
  the group folder that shared the recording with them
  (recording_scope.visible_folder).

A folder is named by its full path, "Projects / Alpha / 2026", as the folder
selects show it. Parent links are followed only within the folder's own scope
(the same user's personal folders, or the same group), as in
folder_tree.py; a database without nested folders has no parent_id column and
every path is one name.
"""

import re

from src.database import db
from src.models import Folder, GroupMembership, InternalShare, Recording, RecordingTag, Tag

PATH_SEPARATOR = ' / '


def _scope_key(folder):
    return ('group', folder.group_id) if folder.group_id is not None else ('user', folder.user_id)


def path_parts(text):
    """'Projects/Alpha' or 'projects / alpha ' -> ['projects', 'alpha']."""
    return [part.strip().lower() for part in str(text or '').split('/') if part.strip()]


class LibraryLabels:
    """Visible tags and folders of the recordings `recording_ids` for one user,
    read in a handful of queries."""

    def __init__(self, user_id, recording_ids):
        self.user_id = user_id
        ids = sorted({int(i) for i in recording_ids or ()})
        self.tags = {}          # recording id -> [tag name], in the recording's order
        self.folder_of = {}     # recording id -> visible folder id
        self._folders = {}      # folder id -> Folder (visible folders and their ancestors)
        if not ids:
            return

        rows = db.session.query(Recording.id, Recording.user_id, Recording.folder_id) \
            .filter(Recording.id.in_(ids)).all()
        owners = {rid: owner for rid, owner, _f in rows}
        foreign = [rid for rid, owner, _f in rows if owner != user_id]

        member_groups = {gid for (gid,) in db.session.query(GroupMembership.group_id)
                         .filter(GroupMembership.user_id == user_id)}
        tag_rows = db.session.query(RecordingTag.recording_id, Tag.name, Tag.user_id, Tag.group_id) \
            .join(Tag, Tag.id == RecordingTag.tag_id) \
            .filter(RecordingTag.recording_id.in_(ids)) \
            .order_by(RecordingTag.recording_id, RecordingTag.order).all()
        for rid, name, tag_user, tag_group in tag_rows:
            visible = (tag_group in member_groups) if tag_group is not None else (tag_user == user_id)
            if visible and rid in owners:
                self.tags.setdefault(rid, []).append(name)

        share_folder = {}
        if foreign:
            share_folder = {rid: folder for rid, folder in db.session.query(
                InternalShare.recording_id, InternalShare.source_folder_id).filter(
                InternalShare.shared_with_user_id == user_id, InternalShare.recording_id.in_(foreign))}
        for rid, owner, folder_id in rows:
            if folder_id is None:
                continue
            if owner == user_id or (share_folder.get(rid) and share_folder[rid] == folder_id):
                self.folder_of[rid] = folder_id

        wanted = set(self.folder_of.values())
        while wanted:
            loaded = Folder.query.filter(Folder.id.in_(sorted(wanted))).all()
            wanted = set()
            for folder in loaded:
                self._folders[folder.id] = folder
                parent = getattr(folder, 'parent_id', None)
                if parent and parent not in self._folders:
                    wanted.add(parent)

    # -- folders ------------------------------------------------------------

    def chain(self, folder_id):
        """[folder, parent, grandparent, ...] within the folder's scope."""
        out, seen = [], set()
        folder = self._folders.get(folder_id)
        while folder is not None and folder.id not in seen:
            seen.add(folder.id)
            out.append(folder)
            parent = self._folders.get(getattr(folder, 'parent_id', None))
            if parent is None or _scope_key(parent) != _scope_key(folder):
                break
            folder = parent
        return out

    def path(self, folder_id):
        """'Projects / Alpha / 2026', or None for an unknown folder."""
        chain = self.chain(folder_id)
        return PATH_SEPARATOR.join(f.name for f in reversed(chain)) if chain else None

    def folder_path_of(self, recording_id):
        folder_id = self.folder_of.get(recording_id)
        return self.path(folder_id) if folder_id else None

    def tags_of(self, recording_id):
        return list(self.tags.get(recording_id, ()))

    def relation(self, a, b):
        """How the folders of recordings a and b relate: 'same folder',
        'parent folder' (a's folder holds b's), 'subfolder' (a's folder is
        inside b's), or None."""
        fa, fb = self.folder_of.get(a), self.folder_of.get(b)
        if not fa or not fb:
            return None
        if fa == fb:
            return 'same folder'
        if fa in {f.id for f in self.chain(fb)}:
            return 'parent folder'
        if fb in {f.id for f in self.chain(fa)}:
            return 'subfolder'
        return None

    # -- filters ------------------------------------------------------------

    def match_tags(self, names):
        """(recording ids having any of the tag names, case-insensitive;
        the names that match no visible tag)."""
        wanted = {str(n).strip().lower() for n in names or () if str(n).strip()}
        found, hits = set(), set()
        for rid, tags in self.tags.items():
            for name in tags:
                if name.lower() in wanted:
                    found.add(name.lower())
                    hits.add(rid)
        return hits, sorted(wanted - found)

    def match_folder(self, text):
        """Recording ids in the folder named by `text` (a name or a full path,
        case-insensitive; a path may leave out leading folders) or in any of
        its subfolders. Empty when no visible folder matches."""
        wanted = path_parts(text)
        if not wanted:
            return set()
        matched = set()
        for folder_id in set(self.folder_of.values()):
            for folder in self.chain(folder_id):
                parts = [f.name.strip().lower() for f in reversed(self.chain(folder.id))]
                if parts[-len(wanted):] == wanted:
                    matched.add(folder.id)
        return {rid for rid, folder_id in self.folder_of.items()
                if matched & {f.id for f in self.chain(folder_id)}}

    # -- names line -----------------------------------------------------------

    def tag_names_in_use(self):
        """Visible tag names on the recordings, most used first."""
        counts = {}
        for tags in self.tags.values():
            for name in tags:
                counts[name] = counts.get(name, 0) + 1
        return sorted(counts, key=lambda n: (-counts[n], n.lower()))

    def folder_paths_in_use(self):
        """Paths of the folders holding the recordings and of their parents,
        most used first (a parent counts its subfolders' recordings)."""
        counts = {}
        for folder_id in self.folder_of.values():
            for folder in self.chain(folder_id):
                counts[folder.id] = counts.get(folder.id, 0) + 1
        paths = {fid: self.path(fid) for fid in counts}
        out = []
        for fid in sorted(counts, key=lambda f: (-counts[f], paths[f].lower())):
            if paths[fid] not in out:
                out.append(paths[fid])
        return out


def capped(names, cap):
    """'a, b, c and 12 more' for at most `cap` names."""
    names = list(names)
    if len(names) <= cap:
        return ', '.join(names)
    return f"{', '.join(names[:cap])} and {len(names) - cap} more"


_GENERIC_SPEAKER = re.compile(r'^(speaker|spk|unknown|unidentified)[\s_-]*\d*$', re.IGNORECASE)


def is_generic_speaker(name):
    """True for a diarization label such as SPEAKER_00, never a person's name."""
    return not name or bool(_GENERIC_SPEAKER.match(name.strip()))
