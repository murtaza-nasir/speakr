"""Every way of putting a recording in a group folder shares it the same way (#515).

The web move (PUT /api/recordings/<id>/folder) applied the folder's sharing
settings; the v1 update, the v1 batch update, uploads and joined uploads set
folder_id without them.
"""
import io
import os
import sys
import uuid
from unittest.mock import patch

import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.api.folders as folders_module
from src.app import app, db
from src.models import (Folder, Group, GroupMembership, InternalShare, ProcessingJob,
                        Recording, RecordingTag, SharedRecordingState, SystemSetting, User)
from src.models.upload_join import UploadJoinPart
from src.services import upload_join as uj

from tests.test_upload_join import _upload_mocks

app.config["WTF_CSRF_ENABLED"] = False


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


def _user(made):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"gs_{s}", email=f"gs_{s}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    made["users"].append(u.id)
    return u


def _client(user):
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


@pytest.fixture
def team(monkeypatch, tmp_path):
    monkeypatch.setattr(folders_module, "ENABLE_INTERNAL_SHARING", True)
    with app.app_context():
        SystemSetting.set_setting("enable_folders", "true", setting_type="boolean")
        made = {"users": []}
        owner, other_admin, member = _user(made), _user(made), _user(made)
        group = Group(name=f"g_{uuid.uuid4().hex[:8]}")
        db.session.add(group)
        db.session.commit()
        db.session.add_all([
            GroupMembership(user_id=owner.id, group_id=group.id, role="admin"),
            GroupMembership(user_id=other_admin.id, group_id=group.id, role="admin"),
            GroupMembership(user_id=member.id, group_id=group.id, role="member"),
        ])
        db.session.commit()

        def folder(auto=True, lead=True, group_id=group.id):
            f = Folder(name=f"f_{uuid.uuid4().hex[:8]}", user_id=owner.id, group_id=group_id,
                       auto_share_on_apply=auto, share_with_group_lead=lead)
            db.session.add(f)
            db.session.commit()
            return f

        def recording():
            r = Recording(user_id=owner.id, title="r", audio_path="/tmp/r.mp3", status="COMPLETED")
            db.session.add(r)
            db.session.commit()
            return r

        yield dict(owner=owner, other_admin=other_admin, member=member, group=group,
                   folder=folder, recording=recording, tmp=tmp_path)

        db.session.rollback()
        rec_ids = [r.id for r in Recording.query.filter(Recording.user_id.in_(made["users"])).all()]
        if rec_ids:
            for model in (InternalShare, SharedRecordingState, RecordingTag, ProcessingJob):
                model.query.filter(model.recording_id.in_(rec_ids)).delete(synchronize_session=False)
        UploadJoinPart.query.filter(UploadJoinPart.user_id.in_(made["users"])).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(rec_ids)).delete(synchronize_session=False)
        Folder.query.filter(Folder.user_id.in_(made["users"])).delete(synchronize_session=False)
        GroupMembership.query.filter_by(group_id=group.id).delete()
        db.session.delete(group)
        User.query.filter(User.id.in_(made["users"])).delete(synchronize_session=False)
        db.session.commit()


def _shares(recording_id):
    return {s.shared_with_user_id: s for s in InternalShare.query.filter_by(recording_id=recording_id)}


def _assert_group_shared(t, recording_id, folder):
    shares = _shares(recording_id)
    assert set(shares) == {t["other_admin"].id, t["member"].id}
    assert shares[t["other_admin"].id].can_edit is True
    assert shares[t["member"].id].can_edit is False
    assert all(s.source_type == "group_folder" and s.source_folder_id == folder.id
               and s.owner_id == t["owner"].id for s in shares.values())


def test_web_move_still_shares(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    resp = _client(t["owner"]).put(f"/api/recordings/{r.id}/folder", json={"folder_id": f.id})
    assert resp.status_code == 200
    _assert_group_shared(t, r.id, f)


def test_v1_patch_shares(team):
    t = team
    f, r = t["folder"](), t["recording"]()
    resp = _client(t["owner"]).patch(f"/api/v1/recordings/{r.id}", json={"folder_id": f.id})
    assert resp.status_code == 200
    _assert_group_shared(t, r.id, f)


def test_v1_patch_follows_the_folder_settings(team):
    t = team
    lead_only, neither = t["folder"](auto=False, lead=True), t["folder"](auto=False, lead=False)
    r1, r2 = t["recording"](), t["recording"]()
    client = _client(t["owner"])
    assert client.patch(f"/api/v1/recordings/{r1.id}", json={"folder_id": lead_only.id}).status_code == 200
    assert client.patch(f"/api/v1/recordings/{r2.id}", json={"folder_id": neither.id}).status_code == 200
    assert set(_shares(r1.id)) == {t["other_admin"].id}
    assert _shares(r2.id) == {}


def test_v1_patch_into_a_personal_folder_shares_nothing(team):
    t = team
    personal, r = t["folder"](group_id=None), t["recording"]()
    assert _client(t["owner"]).patch(f"/api/v1/recordings/{r.id}", json={"folder_id": personal.id}).status_code == 200
    assert _shares(r.id) == {}


def test_v1_batch_shares_and_clearing_does_not_fail(team):
    t = team
    f, r1, r2 = t["folder"](), t["recording"](), t["recording"]()
    client = _client(t["owner"])
    resp = client.patch("/api/v1/recordings/batch",
                        json={"recording_ids": [r1.id, r2.id], "updates": {"folder_id": f.id}})
    assert resp.status_code == 200
    _assert_group_shared(t, r1.id, f)
    _assert_group_shared(t, r2.id, f)

    resp = client.patch("/api/v1/recordings/batch",
                        json={"recording_ids": [r1.id], "updates": {"folder_id": None}})
    assert resp.status_code == 200
    assert db.session.get(Recording, r1.id).folder_id is None


@pytest.mark.parametrize("url", ["/upload", "/api/v1/recordings/upload"])
def test_upload_into_a_group_folder_shares(team, url):
    t = team
    f = t["folder"]()
    data = {"folder_id": str(f.id), "file": (io.BytesIO(os.urandom(2048)), "meeting.mp3")}
    with _upload_mocks(t["tmp"]):
        resp = _client(t["owner"]).post(url, data=data, content_type="multipart/form-data")
    assert resp.status_code == 202, resp.get_data(as_text=True)
    rec = Recording.query.filter_by(user_id=t["owner"].id, folder_id=f.id).one()
    _assert_group_shared(t, rec.id, f)


def test_joined_upload_into_a_group_folder_shares(team):
    t = team
    f = t["folder"]()
    group_id = uuid.uuid4().hex
    parts = []
    for i in range(2):
        p = UploadJoinPart(user_id=t["owner"].id, group_id=group_id, part_index=i, part_count=2,
                           audio_path=f"local://p{i}", original_filename=f"p{i}.wav",
                           file_size=10, audio_duration_seconds=1.0)
        db.session.add(p)
        parts.append(p)
    db.session.commit()
    assert uj.claim_group(t["owner"].id, group_id, 2) is not None
    with patch("src.services.job_queue.job_queue.enqueue", return_value=1):
        rec = uj.create_joined_recording(t["owner"], parts, folder=f)
    _assert_group_shared(t, rec.id, f)
