"""GET /api/recordings: SQL cost per page and payload parity (D#483).

The paginated list used to run several queries per row while serializing
it. The payload test compares the route against a frozen copy of the
per-row enrichment as it stood before the batching (``_legacy_rows``, which
also exercises the per-row fallback of ``Recording.to_list_dict``), over a
fixture with every kind of row the list serves: owned, shared with and
without edit, editable through a group-admin role, tagged (personal, group,
hidden group), foldered, duplicated (owner-scoped on shared rows) and
inbox / highlighted / archived for the owner and for a recipient.

SHARED-DB: everything is created under fresh users and removed afterwards;
assertions are scoped to this module's viewers.
"""

import json
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.api.recordings as rec_module
from src.app import app, db
from src.models import (
    ExportTemplate, Folder, Group, GroupMembership, InternalShare, NamingTemplate, Recording,
    RecordingTag, Share, SharedRecordingState, Tag, User,
)

app.config["WTF_CSRF_ENABLED"] = False

_PREFIX = "lstb"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

@contextmanager
def count_statements():
    """Collect every SQL statement sent to the database inside the block."""
    statements = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _before)


@contextmanager
def sharing_on():
    with patch("src.app.ENABLE_INTERNAL_SHARING", True), \
         patch.object(rec_module, "ENABLE_INTERNAL_SHARING", True):
        yield


def _user(tag):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"{_PREFIX}{tag}_{s}"[:20], email=f"{_PREFIX}{tag}_{s}@local.test", password="x")
    db.session.add(u)
    db.session.flush()
    return u


def _rec(user, title, marker, **kw):
    kw.setdefault("status", "COMPLETED")
    kw.setdefault("audio_path", "local://lstb/a.mp3")
    rec = Recording(user_id=user.id, title=title, participants=marker,
                    transcription="hello", summary="sum", original_filename="a.mp3", **kw)
    db.session.add(rec)
    db.session.flush()
    return rec


def _tag(rec, tag, order):
    db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=order))


def _share(rec, recipient, **kw):
    share = InternalShare(recording_id=rec.id, owner_id=rec.user_id, shared_with_user_id=recipient.id, **kw)
    db.session.add(share)
    return share


def _cleanup(user_ids, group_ids):
    with app.app_context():
        db.session.rollback()
        rec_ids = [r for (r,) in db.session.query(Recording.id).filter(Recording.user_id.in_(user_ids))]
        if rec_ids:
            for model in (RecordingTag, Share, InternalShare, SharedRecordingState):
                model.query.filter(model.recording_id.in_(rec_ids)).delete(synchronize_session=False)
            Recording.query.filter(Recording.id.in_(rec_ids)).delete(synchronize_session=False)
        Tag.query.filter(Tag.user_id.in_(user_ids)).delete(synchronize_session=False)
        Folder.query.filter(Folder.user_id.in_(user_ids)).delete(synchronize_session=False)
        GroupMembership.query.filter(GroupMembership.user_id.in_(user_ids)).delete(synchronize_session=False)
        if group_ids:
            Group.query.filter(Group.id.in_(group_ids)).delete(synchronize_session=False)
        NamingTemplate.query.filter(NamingTemplate.user_id.in_(user_ids)).delete(synchronize_session=False)
        ExportTemplate.query.filter(ExportTemplate.user_id.in_(user_ids)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.session.commit()


def _client(user_id):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return c


def _page(user_id, **params):
    params.setdefault("per_page", 100)
    resp = _client(user_id).get("/api/recordings", query_string=params)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _legacy_rows(viewer_id, ids):
    """The route's per-row enrichment as it was before D#483, verbatim.

    It calls to_list_dict without page lookups, so it also runs the per-row
    fallback that single-row callers keep using.
    """
    from src.app import get_user_recording_status, has_recording_access
    from src.services.recording_state import get_user_archived

    with app.app_context():
        current_user = db.session.get(User, viewer_id)
        recordings = [db.session.get(Recording, rid) for rid in ids]
        share_map = {}
        for s in InternalShare.query.filter(
            InternalShare.recording_id.in_(ids),
            InternalShare.shared_with_user_id == current_user.id
        ).all():
            share_map[s.recording_id] = s

        enriched_recordings = []
        for recording in recordings:
            rec_dict = recording.to_list_dict(viewer_user=current_user, share_map=share_map)
            is_owner = recording.user_id == current_user.id
            rec_dict['is_owner'] = is_owner
            user_inbox, user_highlighted = get_user_recording_status(recording, current_user)
            rec_dict['is_inbox'] = user_inbox
            rec_dict['is_highlighted'] = user_highlighted
            rec_dict['is_archived'] = get_user_archived(recording, current_user)
            rec_dict['can_edit'] = has_recording_access(recording, current_user, require_edit=True)
            rec_dict['can_delete'] = is_owner and (rec_module.USERS_CAN_DELETE or current_user.is_admin)
            if not is_owner:
                owner = db.session.get(User, recording.user_id)
                rec_dict['owner_username'] = owner.username if owner else "Unknown"
                rec_dict['is_shared'] = True
                rec_dict['shared_with_count'] = 0
                rec_dict['public_share_count'] = 0
                share = share_map.get(recording.id)
                if share:
                    rec_dict['share_info'] = {
                        'share_id': share.id,
                        'owner_username': owner.username if owner else "Unknown",
                        'can_edit': share.can_edit,
                        'can_reshare': share.can_reshare,
                        'shared_at': share.created_at.isoformat()
                    }
                else:
                    rec_dict['share_info'] = {
                        'can_edit': False,
                        'can_reshare': False
                    }
            else:
                rec_dict['is_shared'] = False
            visible_tags = recording.get_visible_tags(current_user)
            rec_dict['has_group_tags'] = any(tag.is_group_tag for tag in visible_tags)
            enriched_recordings.append(rec_dict)
        return json.loads(json.dumps(enriched_recordings))


# --------------------------------------------------------------------------- #
# Rich fixture
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def world():
    marker = f"lstbmark{uuid.uuid4().hex[:8]}"
    with app.app_context():
        viewer, o1, o2, x = _user("v"), _user("o1"), _user("o2"), _user("x")
        sfx = uuid.uuid4().hex[:6]
        g_admin, g_member, g_lead_off, g_out = (Group(name=f"{_PREFIX}_{n}_{sfx}")
                                                for n in ("admin", "member", "leadoff", "out"))
        db.session.add_all([g_admin, g_member, g_lead_off, g_out])
        db.session.flush()
        db.session.add_all([
            GroupMembership(group_id=g_admin.id, user_id=viewer.id, role="admin"),
            GroupMembership(group_id=g_admin.id, user_id=o1.id, role="member"),
            GroupMembership(group_id=g_member.id, user_id=viewer.id, role="member"),
            GroupMembership(group_id=g_member.id, user_id=o1.id, role="admin"),
            GroupMembership(group_id=g_lead_off.id, user_id=viewer.id, role="admin"),
            GroupMembership(group_id=g_out.id, user_id=x.id, role="admin"),
        ])
        nt = NamingTemplate(user_id=viewer.id, name="Naming T", template="{{ai_title}}")
        et = ExportTemplate(user_id=viewer.id, name="Export T", template="{{title}}")
        db.session.add_all([nt, et])
        db.session.flush()

        t_personal = Tag(name="Personal", user_id=viewer.id, color="#111111",
                         naming_template_id=nt.id, export_template_id=et.id)
        t_member = Tag(name="TeamTag", user_id=o1.id, group_id=g_member.id)
        t_out = Tag(name="Outsiders", user_id=x.id, group_id=g_out.id)
        t_o1_private = Tag(name="O1Private", user_id=o1.id)
        t_admin = Tag(name="AdminTag", user_id=o1.id, group_id=g_admin.id, auto_share_on_apply=True)
        t_lead_off = Tag(name="LeadOff", user_id=o1.id, group_id=g_lead_off.id,
                         auto_share_on_apply=False, share_with_group_lead=False)
        f_personal = Folder(name="MyFolder", user_id=viewer.id, naming_template_id=nt.id,
                            export_template_id=et.id)
        f_o1 = Folder(name="O1Folder", user_id=o1.id)
        f_group = Folder(name="TeamFolder", user_id=o2.id, group_id=g_member.id)
        db.session.add_all([t_personal, t_member, t_out, t_o1_private, t_admin, t_lead_off,
                            f_personal, f_o1, f_group])
        db.session.flush()

        base = datetime(2026, 3, 1, 9, 0, 0)
        when = iter(base + timedelta(minutes=i) for i in range(100))

        # --- owned by the viewer
        a = _rec(viewer, "A", marker, folder_id=f_personal.id, file_hash="h1" + sfx, created_at=next(when),
                 is_inbox=False, is_highlighted=True, notes="owner notes", meeting_date=base,
                 audio_duration_seconds=61.5, mime_type="audio/mpeg", file_size=1234)
        b = _rec(viewer, None, marker, folder_id=f_personal.id, file_hash="h1" + sfx, created_at=next(when),
                 is_archived=True)
        c = _rec(viewer, "C", marker, created_at=next(when), audio_deleted_at=base, mime_type="video/mp4",
                 deletion_exempt=True, keep_audio_only=True)
        d = _rec(viewer, "D", marker, file_hash="h2" + sfx, created_at=next(when), completed_at=base)
        v1 = _rec(viewer, "V1", marker, file_hash="h3" + sfx, created_at=next(when))
        v2 = _rec(viewer, "V2", marker, file_hash="h3" + sfx, created_at=next(when))
        _tag(a, t_personal, 0)
        _tag(a, t_member, 1)
        _tag(a, t_out, 2)
        _tag(b, t_personal, 0)
        _share(a, o1, can_edit=True)
        _share(a, o2)
        db.session.add(Share(recording_id=a.id, user_id=viewer.id))

        # --- shared with the viewer
        s1 = _rec(o1, "S1", marker, folder_id=f_o1.id, file_hash="h3" + sfx, created_at=next(when))
        s1_copy = _rec(o1, "S1 copy", marker, file_hash="h3" + sfx, created_at=next(when))
        _tag(s1, t_o1_private, 0)
        _share(s1, viewer, can_edit=True, can_reshare=True)
        db.session.add(SharedRecordingState(recording_id=s1.id, user_id=viewer.id,
                                            is_inbox=False, is_highlighted=True, is_archived=False))
        db.session.add(Share(recording_id=s1.id, user_id=o1.id))
        _share(s1, x)

        s2 = _rec(o1, "S2", marker, created_at=next(when))
        _tag(s2, t_admin, 0)
        _tag(s2, t_o1_private, 1)
        _share(s2, viewer, can_edit=False, source_type="group_tag", source_tag_id=t_admin.id)

        s3 = _rec(o1, "S3", marker, created_at=next(when))
        _tag(s3, t_member, 0)
        _share(s3, viewer, can_edit=False)

        s4 = _rec(o1, "S4", marker, created_at=next(when))
        _tag(s4, t_lead_off, 0)
        _share(s4, viewer, can_edit=False, source_type="group_tag", source_tag_id=t_lead_off.id)

        s5 = _rec(o2, "S5", marker, folder_id=f_group.id, created_at=next(when))
        _rec(o2, "S5 sibling", marker, folder_id=f_group.id, created_at=next(when))
        _share(s5, viewer, source_type="group_folder", source_folder_id=f_group.id)
        db.session.add(SharedRecordingState(recording_id=s5.id, user_id=viewer.id, is_archived=True,
                                            personal_notes="mine"))

        s6 = _rec(o2, "S6", marker, created_at=next(when), file_hash="h4" + sfx)
        _share(s6, viewer, can_reshare=True)
        db.session.add(SharedRecordingState(recording_id=s6.id, user_id=viewer.id,
                                            is_inbox=True, is_highlighted=False))
        db.session.commit()

        ids = dict(viewer=viewer.id, o1=o1.id, o2=o2.id, x=x.id, a=a.id, b=b.id, c=c.id, d=d.id,
                   v1=v1.id, v2=v2.id, s1=s1.id, s1_copy=s1_copy.id, s2=s2.id, s3=s3.id, s4=s4.id,
                   s5=s5.id, s6=s6.id, marker=marker,
                   t_personal=t_personal.id, t_member=t_member.id, t_admin=t_admin.id,
                   f_personal=f_personal.id, f_group=f_group.id)
        groups = [g_admin.id, g_member.id, g_lead_off.id, g_out.id]
    with sharing_on():
        yield ids
    _cleanup([ids["viewer"], ids["o1"], ids["o2"], ids["x"]], groups)


VIEWS = [
    pytest.param({}, id="default"),
    pytest.param({"archived": "true"}, id="archived"),
    pytest.param({"q": "MARKER"}, id="search"),
    pytest.param({"shared": "true"}, id="shared"),
    pytest.param({"starred": "true"}, id="starred"),
    pytest.param({"inbox": "true"}, id="inbox"),
    pytest.param({"per_page": 3, "page": 2}, id="page2"),
    pytest.param({"q": "tag:teamtag"}, id="tag-filter"),
]


@pytest.mark.parametrize("params", VIEWS)
def test_payload_matches_the_per_row_path(world, params):
    params = {k: (world["marker"] if v == "MARKER" else v) for k, v in params.items()}
    body = _page(world["viewer"], **params)
    rows = body["recordings"]
    assert rows, "every view of the fixture lists something"
    assert rows == _legacy_rows(world["viewer"], [r["id"] for r in rows])


def test_fixture_covers_every_kind_of_row(world):
    """Guards the parity test against a fixture that stopped exercising a case."""
    w = world
    rows = {r["id"]: r for r in _page(w["viewer"], q=w["marker"])["recordings"]}
    assert set(rows) == {w[k] for k in ("a", "b", "c", "d", "v1", "v2", "s1", "s2", "s3", "s4", "s5", "s6")}
    default_ids = {r["id"] for r in _page(w["viewer"])["recordings"]}
    assert default_ids == set(rows) - {w["b"], w["s5"]}           # archived by owner / by recipient

    a, b, s1, s2, s3, s4, s5, s6 = (rows[w[k]] for k in ("a", "b", "s1", "s2", "s3", "s4", "s5", "s6"))
    # owned: tags in order, hidden group tag dropped, counts over all recordings
    assert [t["name"] for t in a["tags"]] == ["Personal", "TeamTag"]
    assert a["tags"][0]["recording_count"] == 2 and a["tags"][0]["naming_template_name"] == "Naming T"
    assert a["tags"][1]["group_name"].startswith(f"{_PREFIX}_member_") and a["tags"][1]["recording_count"] == 2
    assert a["folder"]["recording_count"] == 2 and a["folder"]["export_template_name"] == "Export T"
    assert (a["shared_with_count"], a["public_share_count"]) == (2, 1)
    assert (a["is_inbox"], a["is_highlighted"], a["has_group_tags"]) == (False, True, True)
    assert [c["id"] for c in a["duplicate_info"]["copies"]] == [w["a"], w["b"]]
    assert b["duplicate_info"]["copies"][1]["title"] == f"#{w['b']}" and b["is_archived"] is True
    # shared: owner-scoped duplicates, recipient state, edit rights
    assert [c["id"] for c in s1["duplicate_info"]["copies"]] == [w["s1"], w["s1_copy"]]
    assert [c["id"] for c in rows[w["v1"]]["duplicate_info"]["copies"]] == [w["v1"], w["v2"]]
    assert (s1["is_inbox"], s1["is_highlighted"], s1["can_edit"]) == (False, True, True)
    assert s1["tags"] == [] and s1["folder"] is None
    assert (s1["shared_with_count"], s1["public_share_count"]) == (0, 0)
    assert s1["share_info"]["can_reshare"] is True and s1["owner_username"].startswith(f"{_PREFIX}o1")
    assert (s2["can_edit"], s2["share_info"]["can_edit"]) == (True, False)   # group admin
    assert [t["name"] for t in s2["tags"]] == ["AdminTag"] and s2["tags"][0]["is_foreign"] is True
    assert (s3["can_edit"], s3["tags"], s3["has_group_tags"]) == (False, [], True)  # member only
    assert s4["can_edit"] is False                                                  # lead flags off
    assert s5["folder"]["is_foreign"] is True and s5["folder"]["recording_count"] == 2
    assert (s5["is_archived"], s5["is_inbox"]) == (True, True)
    assert (s6["is_inbox"], s6["is_highlighted"], s6["duplicate_info"]) == (True, False, None)
    assert s6["owner_username"].startswith(f"{_PREFIX}o2")
    assert all(r["can_delete"] == r["is_owner"] for r in rows.values())

