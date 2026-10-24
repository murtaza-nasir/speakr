"""Page-scoped lookups for the recording lists (D#483).

Serializing a page one row at a time cost several queries per row: the two
share counts, the duplicate copies, the viewer's inbox/star/archive state,
tag visibility, and the tag and folder recording counts. RecordingListBatch
reads each of them once for the page. Recording.to_list_dict takes it as
``batch``; without one it keeps its per-row queries, which single-row
callers still use, so both paths must give the same payload.
"""

from sqlalchemy import func
from sqlalchemy.orm import defer, joinedload, selectinload

from src.database import db
from src.models import (
    Folder, GroupMembership, InternalShare, Recording, RecordingTag, Share, SharedRecordingState, Tag, User,
)

# Long columns the list payload never reads. Nothing on the list path may
# touch them: a deferred column read later costs one query per row.
_LIST_DEFERRED = (
    Recording.transcription, Recording.summary, Recording.error_message, Recording.speaker_embeddings,
    Recording.speaker_label_map, Recording.prompt_variables, Recording.resolved_hotwords,
    Recording.resolved_initial_prompt,
)


def list_load_options():
    """Loader options for a page query whose rows go through to_list_dict."""
    names = (joinedload(Tag.group), joinedload(Tag.naming_template), joinedload(Tag.export_template))
    folder_names = (joinedload(Folder.group), joinedload(Folder.naming_template),
                    joinedload(Folder.export_template))
    return [
        selectinload(Recording.tag_associations).joinedload(RecordingTag.tag).options(*names),
        selectinload(Recording.folder).options(*folder_names),
        *(defer(column) for column in _LIST_DEFERRED),
    ]


def group_ids_of(viewer, recordings):
    """The viewer's groups among those of the recordings' group tags, in one query."""
    group_ids = sorted({t.group_id for r in recordings for t in r.tags if t.group_id})
    if not group_ids:
        return set()
    return {gid for (gid,) in db.session.query(GroupMembership.group_id).filter(
        GroupMembership.user_id == viewer.id, GroupMembership.group_id.in_(group_ids))}


def _counts(column, ids):
    if not ids:
        return {}
    rows = db.session.query(column, func.count()).filter(column.in_(ids)).group_by(column)
    return {key: count for key, count in rows}


class RecordingListBatch:
    """The lookups for one page of recordings seen by one viewer.

    share_map is {recording_id: InternalShare} of the viewer's shares for the
    page, when the caller has already read it.
    """

    def __init__(self, recordings, viewer, share_map=None):
        self.viewer = viewer
        ids = [r.id for r in recordings]
        foreign = [r for r in recordings if r.user_id != viewer.id]
        foreign_ids = [r.id for r in foreign]

        if share_map is None:
            share_map = {}
            if foreign_ids:
                share_map = {s.recording_id: s for s in InternalShare.query.filter(
                    InternalShare.recording_id.in_(foreign_ids),
                    InternalShare.shared_with_user_id == viewer.id)}
        self.share_map = share_map

        self.shared_with_counts = _counts(InternalShare.recording_id, ids)
        self.public_share_counts = _counts(Share.recording_id, ids)

        self.states = {}
        if foreign_ids:
            self.states = {s.recording_id: s for s in SharedRecordingState.query.filter(
                SharedRecordingState.recording_id.in_(foreign_ids),
                SharedRecordingState.user_id == viewer.id)}
        owner_ids = sorted({r.user_id for r in foreign if r.user_id is not None})
        self.owners = {u.id: u for u in User.query.filter(User.id.in_(owner_ids))} if owner_ids else {}

        self._duplicates = self._load_duplicates(recordings)

        member_group_ids = group_ids_of(viewer, recordings)
        self._visible = {r.id: r.get_visible_tags(viewer, member_group_ids) for r in recordings}

        self.tag_counts = _counts(RecordingTag.tag_id,
                                  sorted({t.id for tags in self._visible.values() for t in tags}))
        self.folder_counts = _counts(Recording.folder_id,
                                     sorted({r.folder_id for r in recordings if r.folder_id is not None}))

    def _load_duplicates(self, recordings):
        # Scoped to each row's owner, as Recording.get_duplicate_info is, so a
        # shared row lists its owner's copies and never the viewer's.
        pairs = {(r.user_id, r.file_hash) for r in recordings if r.file_hash and r.user_id is not None}
        if not pairs:
            return {}
        rows = db.session.query(Recording.id, Recording.title, Recording.created_at, Recording.user_id,
                                Recording.file_hash).filter(
            Recording.user_id.in_(sorted({u for u, _ in pairs})),
            Recording.file_hash.in_(sorted({h for _, h in pairs})),
        ).order_by(Recording.created_at, Recording.id)
        copies = {}
        for rid, title, created_at, user_id, file_hash in rows:
            if (user_id, file_hash) in pairs:
                copies.setdefault((user_id, file_hash), []).append((rid, title, created_at))
        return copies

    def duplicate_info(self, recording):
        """Recording.get_duplicate_info for a row of the page."""
        if not recording.file_hash:
            return None
        dupes = self._duplicates.get((recording.user_id, recording.file_hash), [])
        if len(dupes) <= 1:
            return None
        return {
            'total_copies': len(dupes),
            'copies': [
                {
                    'id': rid,
                    'title': title or f'#{rid}',
                    'created_at': created_at.isoformat() if created_at else None,
                    'is_self': rid == recording.id,
                }
                for rid, title, created_at in dupes
            ],
        }

    def visible_tags(self, recording):
        return self._visible[recording.id]

    def tag_dict(self, tag):
        return tag.to_dict(recording_count=self.tag_counts.get(tag.id, 0))

    def folder_dict(self, folder):
        return folder.to_dict(recording_count=self.folder_counts.get(folder.id, 0))

    def user_status(self, recording):
        """get_user_recording_status for the viewer: (is_inbox, is_highlighted)."""
        if recording.user_id == self.viewer.id:
            return (recording.is_inbox, recording.is_highlighted)
        state = self.states.get(recording.id)
        return (state.is_inbox, state.is_highlighted) if state else (True, False)

    def user_archived(self, recording):
        """get_user_archived for the viewer."""
        if recording.user_id == self.viewer.id:
            return bool(recording.is_archived)
        state = self.states.get(recording.id)
        return bool(state and state.is_archived)
