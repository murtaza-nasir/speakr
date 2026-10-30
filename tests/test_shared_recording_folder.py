"""A share recipient sees the recording's folder only while it is the group
folder that shared it (#516); the v1 detail, list and changes feed showed the
owner's current folder, personal folders included.
"""
import os
import sys
import uuid

import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.api.folders as folders_module
from src.app import app, db
from src.models import (Folder, Group, GroupMembership, InternalShare, Recording,
                        SharedRecordingState, SystemSetting, User)

app.config["WTF_CSRF_ENABLED"] = False


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


def _client(user):
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


@pytest.fixture
def shared(monkeypatch):
    import src.app as app_module
    monkeypatch.setattr(folders_module, "ENABLE_INTERNAL_SHARING", True)
    monkeypatch.setattr(app_module, "ENABLE_INTERNAL_SHARING", True, raising=False)
    monkeypatch.setenv("ENABLE_INTERNAL_SHARING", "true")
    with app.app_context():
        SystemSetting.set_setting("enable_folders", "true", setting_type="boolean")
        users = []
        for _ in range(2):
            s = uuid.uuid4().hex[:8]
            u = User(username=f"sf_{s}", email=f"sf_{s}@local.test", password="x")
            db.session.add(u)
            users.append(u)
        db.session.commit()
        owner, member = users
        group = Group(name=f"g_{uuid.uuid4().hex[:8]}")
        db.session.add(group)
        db.session.commit()
        db.session.add_all([GroupMembership(user_id=owner.id, group_id=group.id, role="admin"),
                            GroupMembership(user_id=member.id, group_id=group.id, role="member")])
        group_folder = Folder(name="team folder", user_id=owner.id, group_id=group.id)
        personal = Folder(name="owner private", user_id=owner.id, custom_prompt="secret prompt")
        db.session.add_all([group_folder, personal])
        db.session.commit()
        rec = Recording(user_id=owner.id, title="shared", audio_path="/tmp/s.mp3", status="COMPLETED")
        db.session.add(rec)
        db.session.commit()
        resp = _client(owner).put(f"/api/recordings/{rec.id}/folder", json={"folder_id": group_folder.id})
        assert resp.status_code == 200
        assert InternalShare.query.filter_by(recording_id=rec.id, shared_with_user_id=member.id).count() == 1

        yield dict(owner=owner, member=member, rec=rec, group_folder=group_folder, personal=personal)

        db.session.rollback()
        for model in (InternalShare, SharedRecordingState):
            model.query.filter_by(recording_id=rec.id).delete()
        Recording.query.filter_by(id=rec.id).delete()
        Folder.query.filter_by(user_id=owner.id).delete()
        GroupMembership.query.filter_by(group_id=group.id).delete()
        db.session.delete(group)
        User.query.filter(User.id.in_([owner.id, member.id])).delete(synchronize_session=False)
        db.session.commit()


def _move(t, folder):
    resp = _client(t["owner"]).put(f"/api/recordings/{t['rec'].id}/folder", json={"folder_id": folder.id})
    assert resp.status_code == 200


def _list_row(client, rec_id):
    rows = client.get("/api/v1/recordings?scope=all&per_page=100").get_json()["recordings"]
    return next(r for r in rows if r["id"] == rec_id)


def test_detail_shows_the_folder_that_shared_it(shared):
    t = shared
    body = _client(t["member"]).get(f"/api/v1/recordings/{t['rec'].id}").get_json()
    assert body["folder_id"] == t["group_folder"].id
    assert body["folder"] == {"id": t["group_folder"].id, "name": "team folder"}


def test_detail_hides_a_personal_folder_from_the_recipient(shared):
    t = shared
    _move(t, t["personal"])
    body = _client(t["member"]).get(f"/api/v1/recordings/{t['rec'].id}").get_json()
    assert body["folder_id"] is None and body["folder"] is None
    assert "owner private" not in str(body) and "secret prompt" not in str(body)

    owner_body = _client(t["owner"]).get(f"/api/v1/recordings/{t['rec'].id}").get_json()
    assert owner_body["folder"] == {"id": t["personal"].id, "name": "owner private"}


def test_list_hides_a_personal_folder_from_the_recipient(shared):
    t = shared
    _move(t, t["personal"])
    member = _client(t["member"])
    row = _list_row(member, t["rec"].id)
    assert row["is_shared"] is True
    assert row["folder_id"] is None and row["folder"] is None

    # The changes feed builds its rows with the same helpers as the list
    # (_recording_list_item with share_details).

    owner_row = _list_row(_client(t["owner"]), t["rec"].id)
    assert owner_row["folder"] == {"id": t["personal"].id, "name": "owner private"}


def test_list_shows_the_folder_that_shared_it(shared):
    t = shared
    row = _list_row(_client(t["member"]), t["rec"].id)
    assert row["folder"] == {"id": t["group_folder"].id, "name": "team folder"}
