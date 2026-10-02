"""updated_at, tombstones and the changes feed (mailr spec G2).

Every write path a client can see moves Recording.updated_at, through one
before_flush listener; processing-only fields do not. Deleting a recording or
an internal share writes tombstones. GET /api/v1/recordings/changes returns
each create, edit and delete once, in latest state, with a cursor.

SHARED-DB: users, tokens, tags, recordings and tombstones are removed afterwards.
"""

import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import (APIToken, Event, InternalShare, Recording, RecordingTag, RecordingTombstone,
                        SystemSetting, Tag, User)
from src.services import recording_changes as rc
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False
PAST = datetime(2020, 1, 1, 12, 0, 0)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEGMENTS = [{"speaker": "SPEAKER_00", "sentence": "Launch moves to Q3.", "start_time": 0.0, "end_time": 2.0}]


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        me = User(username=f"chg_{s}", email=f"chg_{s}@local.test", password="x")
        other = User(username=f"chgo_{s}", email=f"chgo_{s}@local.test", password="x")
        db.session.add_all([me, other])
        db.session.commit()
        rec = Recording(user_id=me.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                        transcription=json.dumps(SEGMENTS))
        tag = Tag(name=f"t-{s}", user_id=me.id)
        db.session.add_all([rec, tag])
        db.session.commit()
        plain = f"tok-{secrets.token_urlsafe(16)}"
        db.session.add(APIToken(user_id=me.id, token_hash=hash_token(plain), name="chg"))
        db.session.commit()
        ids = dict(me=me.id, other=other.id, rec=rec.id, tag=tag.id, token=plain)
    yield ids
    with app.app_context():
        db.session.rollback()
        users = [ids["me"], ids["other"]]
        rec_ids = [r.id for r in Recording.query.filter(Recording.user_id.in_(users))]
        RecordingTag.query.filter(RecordingTag.recording_id.in_(rec_ids)).delete(synchronize_session=False)
        Event.query.filter(Event.recording_id.in_(rec_ids)).delete(synchronize_session=False)
        InternalShare.query.filter(InternalShare.recording_id.in_(rec_ids)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(rec_ids)).delete(synchronize_session=False)
        RecordingTombstone.query.filter(RecordingTombstone.user_id.in_(users)).delete(synchronize_session=False)
        Tag.query.filter(Tag.user_id.in_(users)).delete(synchronize_session=False)
        APIToken.query.filter(APIToken.user_id.in_(users)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
        db.session.commit()


def _backdate(rid, when=PAST):
    with app.app_context():
        r = db.session.get(Recording, rid)
        r.updated_at = when
        db.session.commit()


def _updated(rid):
    with app.app_context():
        return db.session.get(Recording, rid).updated_at


def _session(uid):
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return c


def _api(world):
    return {"Authorization": f"Bearer {world['token']}"}


# ------------------------------------------------------------ updated_at

def _no_side_effects():
    return (patch("src.api.recordings.reindex_recording_chunks_async"),
            patch("src.file_exporter.export_recording"),
            patch("src.services.speaker.update_voice_profiles", return_value=(0, 0)))


WRITE_PATHS = {
    "v1 PATCH": lambda w, c: c.patch(f"/api/v1/recordings/{w['rec']}", json={"title": "New"}, headers=_api(w)),
    "v1 PUT notes": lambda w, c: c.put(f"/api/v1/recordings/{w['rec']}/notes", json={"notes": "n"}, headers=_api(w)),
    "v1 PUT summary": lambda w, c: c.put(f"/api/v1/recordings/{w['rec']}/summary", json={"summary": "s"}, headers=_api(w)),
    "v1 tag add": lambda w, c: c.post(f"/api/v1/recordings/{w['rec']}/tags", json={"tag_ids": [w["tag"]]}, headers=_api(w)),
    "web /save": lambda w, c: c.post("/save", json={"id": w["rec"], "title": "Saved"}),
    "web update_speakers": lambda w, c: c.post(f"/recording/{w['rec']}/update_speakers",
                                               json={"speaker_map": {"SPEAKER_00": {"name": "Jane", "isMe": False}}}),
    "web update_transcript": lambda w, c: c.post(f"/recording/{w['rec']}/update_transcript",
                                                 json={"transcript_data": [dict(SEGMENTS[0], sentence="Edited.")]}),
    "web tag add": lambda w, c: c.post(f"/api/recordings/{w['rec']}/tags", json={"tag_id": w["tag"]}),
    "web archive": lambda w, c: c.post(f"/recording/{w['rec']}/toggle_archive"),
    "web inbox": lambda w, c: c.post(f"/recording/{w['rec']}/toggle_inbox"),
}


@pytest.mark.parametrize("path", sorted(WRITE_PATHS))
def test_each_write_path_moves_updated_at(world, path):
    _backdate(world["rec"])
    a, b, d = _no_side_effects()
    with a, b, d:
        resp = WRITE_PATHS[path](world, _session(world["me"]))
    assert resp.status_code in (200, 201), (path, resp.get_data(as_text=True)[:300])
    assert _updated(world["rec"]) > PAST + timedelta(days=1000), path


def test_tag_removal_moves_updated_at(world):
    with app.app_context():
        db.session.add(RecordingTag(recording_id=world["rec"], tag_id=world["tag"], order=0))
        db.session.commit()
    _backdate(world["rec"])
    resp = _session(world["me"]).delete(f"/api/v1/recordings/{world['rec']}/tags/{world['tag']}", headers=_api(world))
    assert resp.status_code == 200
    assert _updated(world["rec"]) > PAST + timedelta(days=1000)


@pytest.mark.parametrize("change", ["status", "event", "audio_deleted", "share"])
def test_processing_and_related_rows_move_updated_at(world, change):
    _backdate(world["rec"])
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        if change == "status":
            r.status = "SUMMARIZING"
        elif change == "event":
            db.session.add(Event(recording_id=r.id, title="Review", start_datetime=datetime(2026, 10, 9, 15)))
        elif change == "audio_deleted":
            r.audio_deleted_at = datetime.utcnow()
        else:
            db.session.add(InternalShare(recording_id=r.id, owner_id=world["me"], shared_with_user_id=world["other"]))
        db.session.commit()
    assert _updated(world["rec"]) > PAST + timedelta(days=1000)


def test_processing_only_fields_do_not_move_updated_at(world):
    _backdate(world["rec"])
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        r.processing_time_seconds = 12
        r.speaker_embeddings = {"SPEAKER_00": [0.1, 0.2]}
        r.export_filename = "x.md"
        db.session.commit()
    assert _updated(world["rec"]) == PAST


def test_a_new_recording_starts_with_updated_at_equal_to_created_at(world):
    with app.app_context():
        r = Recording(user_id=world["me"], title="New", status="PENDING", original_filename="n.wav")
        db.session.add(r)
        db.session.commit()
        assert r.updated_at is not None and r.updated_at == r.created_at


def test_no_bulk_update_bypasses_the_listener():
    pattern = re.compile(r"(Recording\.query[^\n]*\.update\(|query\(Recording\)[^\n]*\.update\()")
    hits = []
    for folder, _, files in os.walk(os.path.join(ROOT, "src")):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(folder, name)
                for n, line in enumerate(open(path, encoding="utf-8"), 1):
                    if pattern.search(line):
                        hits.append(f"{os.path.relpath(path, ROOT)}:{n}")
    assert not hits, f"bulk update on recording bypasses updated_at: {hits}"


# ------------------------------------------------------------ feed

def _feed(c, world, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return c.get(f"/api/v1/recordings/changes?{q}", headers=_api(world))


@pytest.fixture
def no_settle():
    with patch.object(rc, "SETTLE_SECONDS", 0):
        yield


def test_the_feed_returns_each_change_once(world, no_settle):
    _backdate(world["rec"])
    with app.app_context():
        second = Recording(user_id=world["me"], title="Second", status="COMPLETED", original_filename="b.wav")
        db.session.add(second)
        db.session.commit()
        second_id = second.id
    c = app.test_client()
    first = _feed(c, world).get_json()
    assert [ch["recording"]["id"] for ch in first["changes"]] == [world["rec"], second_id]
    assert first["has_more"] is False and first["changes"][0]["recording"]["updated_at"].endswith("Z")

    idle = _feed(c, world, cursor=first["next_cursor"]).get_json()
    assert idle["changes"] == [] and idle["next_cursor"]

    c.patch(f"/api/v1/recordings/{world['rec']}", json={"title": "Edited"}, headers=_api(world))
    edited = _feed(c, world, cursor=idle["next_cursor"]).get_json()
    assert [(ch["type"], ch["recording"]["title"]) for ch in edited["changes"]] == [("upsert", "Edited")]

    with patch("src.services.storage.get_storage_service"):
        assert c.delete(f"/api/v1/recordings/{second_id}", headers=_api(world)).status_code == 200
    gone = _feed(c, world, cursor=edited["next_cursor"]).get_json()
    assert gone["changes"] == [{"type": "delete", "id": second_id, "deleted_at": gone["changes"][0]["deleted_at"],
                                "reason": "deleted"}]


def test_retention_deletes_are_marked(world, no_settle):
    c = app.test_client()
    start = _feed(c, world).get_json()["next_cursor"]
    from src.services.recording_deletion import delete_recording_completely
    with app.app_context(), patch("src.services.storage.get_storage_service"):
        delete_recording_completely(db.session.get(Recording, world["rec"]), storage=type("S", (), {"delete": lambda *a, **k: None})(),
                                    reason="retention")
    changes = _feed(c, world, cursor=start).get_json()["changes"]
    assert [(ch["type"], ch["reason"]) for ch in changes] == [("delete", "retention")]


def test_paging_visits_every_change_once(world, no_settle):
    with app.app_context():
        made = []
        for i in range(3):
            r = Recording(user_id=world["me"], title=f"R{i}", status="COMPLETED", original_filename="r.wav")
            db.session.add(r)
            db.session.commit()
            made.append(r.id)
    c = app.test_client()
    cursor, seen = None, []
    for step in range(10):
        page = _feed(c, world, limit=1, **({"cursor": cursor} if cursor else {})).get_json()
        if step == 0:   # a delete during the full pass is reported
            with app.app_context():
                db.session.delete(db.session.get(Recording, made[1]))
                db.session.commit()
        seen += [(ch["type"], ch.get("id") or ch["recording"]["id"]) for ch in page["changes"]]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    # made[1] may be listed before its delete; each change still appears once
    assert len(seen) == len(set(seen))
    assert {("upsert", world["rec"]), ("upsert", made[0]), ("upsert", made[2]), ("delete", made[1])} <= set(seen)


def test_a_change_inside_the_settle_window_waits(world):
    c = app.test_client()
    first = _feed(c, world)
    assert first.status_code == 200, first.get_json()
    cursor = first.get_json()["next_cursor"]
    c.patch(f"/api/v1/recordings/{world['rec']}", json={"title": "Fresh"}, headers=_api(world))
    waiting = _feed(c, world, cursor=cursor)
    assert waiting.status_code == 200, waiting.get_json()
    assert waiting.get_json()["changes"] == []
    with patch.object(rc, "SETTLE_SECONDS", 0):
        later = _feed(c, world, cursor=cursor).get_json()["changes"]
    assert [ch["recording"]["title"] for ch in later] == ["Fresh"]


def test_bad_and_expired_cursors(world, no_settle):
    c = app.test_client()
    assert _feed(c, world, cursor="garbage!").status_code == 400
    old = rc.encode_cursor(datetime.utcnow() - timedelta(days=400), 1, 1)
    with app.app_context():
        before = SystemSetting.get_setting(rc.PRUNED_BEFORE_KEY)
        SystemSetting.set_setting(rc.PRUNED_BEFORE_KEY, rc.iso_z(datetime.utcnow() - timedelta(days=90)))
    try:
        resp = _feed(c, world, cursor=old)
        assert resp.status_code == 410 and resp.get_json()["code"] == "cursor_expired"
    finally:
        with app.app_context():
            if before is None:
                SystemSetting.query.filter_by(key=rc.PRUNED_BEFORE_KEY).delete()
                db.session.commit()
            else:
                SystemSetting.set_setting(rc.PRUNED_BEFORE_KEY, before)


def test_another_users_changes_never_appear(world, no_settle):
    c = app.test_client()
    cursor = _feed(c, world).get_json()["next_cursor"]
    with app.app_context():
        r = Recording(user_id=world["other"], title="Theirs", status="COMPLETED", original_filename="t.wav")
        db.session.add(r)
        db.session.commit()
        theirs = r.id
        db.session.delete(r)
        db.session.commit()
    ids = [ch.get("id") or ch["recording"]["id"] for ch in _feed(c, world, cursor=cursor).get_json()["changes"]]
    assert theirs not in ids


def test_a_shared_recording_writes_tombstones_for_owner_and_recipient(world):
    with app.app_context():
        db.session.add(InternalShare(recording_id=world["rec"], owner_id=world["me"], shared_with_user_id=world["other"]))
        db.session.commit()
        db.session.delete(db.session.get(Recording, world["rec"]))
        db.session.commit()
        stones = {(t.user_id, t.reason) for t in RecordingTombstone.query.filter(
            RecordingTombstone.recording_id == world["rec"],
            RecordingTombstone.user_id.in_([world["me"], world["other"]]))}
    assert stones == {(world["me"], "deleted"), (world["other"], "deleted")}


def test_removing_a_share_writes_access_revoked(world):
    with app.app_context():
        share = InternalShare(recording_id=world["rec"], owner_id=world["me"], shared_with_user_id=world["other"])
        db.session.add(share)
        db.session.commit()
        db.session.delete(share)
        db.session.commit()
        stones = [(t.user_id, t.reason) for t in RecordingTombstone.query.filter(
            RecordingTombstone.recording_id == world["rec"],
            RecordingTombstone.user_id.in_([world["me"], world["other"]]))]
    assert stones == [(world["other"], "access_revoked")]


def test_pruning_removes_old_tombstones_and_records_the_horizon(world):
    with app.app_context():
        db.session.add(RecordingTombstone(recording_id=999999, user_id=world["me"],
                                          deleted_at=datetime.utcnow() - timedelta(days=400), reason="deleted"))
        db.session.commit()
        assert rc.prune_tombstones() >= 1
        assert RecordingTombstone.query.filter_by(recording_id=999999, user_id=world["me"]).count() == 0
        assert SystemSetting.get_setting(rc.PRUNED_BEFORE_KEY)


# ------------------------------------------------------------ list filters

def test_list_filters_updated_since_and_date_field(world):
    _backdate(world["rec"])
    with app.app_context():
        r = db.session.get(Recording, world["rec"])
        r.meeting_date = datetime(2021, 6, 1, 10)
        db.session.commit()
        fresh = Recording(user_id=world["me"], title="Fresh", status="COMPLETED", original_filename="f.wav")
        db.session.add(fresh)
        db.session.commit()
        fresh_id = fresh.id
    c = app.test_client()
    since = c.get("/api/v1/recordings?updated_since=2025-01-01T00:00:00Z", headers=_api(world)).get_json()
    ids = [r["id"] for r in since["recordings"]]
    assert fresh_id in ids and world["rec"] in ids          # the meeting_date edit moved it
    by_meeting = c.get("/api/v1/recordings?date_field=meeting_date&date_from=2021-06-01&date_to=2021-06-01",
                       headers=_api(world)).get_json()
    assert [r["id"] for r in by_meeting["recordings"]] == [world["rec"]]
    assert c.get("/api/v1/recordings?date_field=bogus", headers=_api(world)).status_code == 400
    assert c.get("/api/v1/recordings?updated_since=nope", headers=_api(world)).status_code == 400
    ordered = c.get("/api/v1/recordings?sort_by=updated_at&sort_order=desc", headers=_api(world)).get_json()
    stamps = [r["updated_at"] for r in ordered["recordings"]]
    assert stamps == sorted(stamps, reverse=True)
