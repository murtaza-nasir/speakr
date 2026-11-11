"""Nested folder creation, tree safety, counts, and recursive filters."""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Folder, Group, GroupMembership, Recording, SystemSetting, User

app.config["WTF_CSRF_ENABLED"] = False


def _user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"{prefix}_{suffix}", email=f"{prefix}_{suffix}@local.test", password="x")
    db.session.add(user)
    db.session.commit()
    return user


def _client(user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    from flask import g
    for attr in ("_login_user", "_flask_login_user"):
        if hasattr(g, attr):
            delattr(g, attr)
    return client


@pytest.fixture
def owner():
    with app.app_context():
        SystemSetting.set_setting("enable_folders", "true", setting_type="boolean")
        user = _user("nf")
        yield _client(user), user
        Recording.query.filter_by(user_id=user.id).delete()
        Folder.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()


def _create(client, name, parent_id=None):
    response = client.post("/api/folders", json={"name": name, "parent_id": parent_id})
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_create_nested_folders_and_recursive_counts(owner):
    client, _ = owner
    root = _create(client, "root")
    child = _create(client, "child", root["id"])
    grandchild = _create(client, "grandchild", child["id"])
    assert root["parent_id"] is None
    assert child["parent_id"] == root["id"]
    assert grandchild["parent_id"] == child["id"]

    user_id = owner[1].id
    db.session.add_all([
        Recording(user_id=user_id, title="root item", audio_path="/tmp/root.mp3",
                  status="COMPLETED", folder_id=root["id"]),
        Recording(user_id=user_id, title="grandchild item", audio_path="/tmp/child.mp3",
                  status="COMPLETED", folder_id=grandchild["id"]),
    ])
    db.session.commit()
    folders = {f["id"]: f for f in client.get("/api/folders").get_json()}
    assert folders[root["id"]]["recording_count"] == 1
    assert folders[root["id"]]["total_recording_count"] == 2
    assert folders[child["id"]]["total_recording_count"] == 1
    assert folders[grandchild["id"]]["total_recording_count"] == 1


def test_parent_must_belong_to_the_same_user(owner):
    client, _ = owner
    other = _user("nf_other")
    other_root = _create(_client(other), "other root")
    client = _client(owner[1])
    response = client.post("/api/folders", json={"name": "invalid child", "parent_id": other_root["id"]})
    assert response.status_code == 400
    assert response.get_json()["code"] == "folder_parent_invalid"
    Folder.query.filter_by(user_id=other.id).delete()
    db.session.delete(other)
    db.session.commit()


def test_folder_cannot_be_moved_beneath_itself_or_a_descendant(owner):
    client, _ = owner
    root = _create(client, "root")
    child = _create(client, "child", root["id"])
    for parent_id in (root["id"], child["id"]):
        response = client.put(f"/api/folders/{root['id']}", json={"parent_id": parent_id})
        assert response.status_code == 400
        assert response.get_json()["code"] == "folder_parent_cycle"
    db.session.refresh(db.session.get(Folder, root["id"]))
    assert db.session.get(Folder, root["id"]).parent_id is None


def test_deleting_a_parent_promotes_its_children(owner):
    client, user = owner
    root = _create(client, "root")
    child = _create(client, "child", root["id"])
    grandchild = _create(client, "grandchild", child["id"])
    response = client.delete(f"/api/folders/{root['id']}")
    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(Folder, child["id"]).parent_id is None
    assert db.session.get(Folder, grandchild["id"]).parent_id == child["id"]


def test_web_folder_filter_can_include_descendants(owner):
    client, user = owner
    root = _create(client, "root")
    child = _create(client, "child", root["id"])
    db.session.add_all([
        Recording(user_id=user.id, title="root item", audio_path="/tmp/root.mp3",
                  status="COMPLETED", folder_id=root["id"]),
        Recording(user_id=user.id, title="child item", audio_path="/tmp/child.mp3",
                  status="COMPLETED", folder_id=child["id"]),
    ])
    db.session.commit()
    direct = client.get(f"/api/recordings?folder={root['id']}").get_json()["recordings"]
    recursive = client.get(f"/api/recordings?folder={root['id']}&include_children=true").get_json()["recordings"]
    assert [r["title"] for r in direct] == ["root item"]
    assert {r["title"] for r in recursive} == {"root item", "child item"}


def test_v1_folder_filter_can_include_descendants(owner):
    client, user = owner
    root = _create(client, "root")
    child = _create(client, "child", root["id"])
    db.session.add(Recording(user_id=user.id, title="child item", audio_path="/tmp/child.mp3",
                             status="COMPLETED", folder_id=child["id"]))
    db.session.commit()
    direct = client.get(f"/api/v1/recordings?folder_id={root['id']}").get_json()["recordings"]
    recursive = client.get(f"/api/v1/recordings?folder_id={root['id']}&include_children=true").get_json()["recordings"]
    assert direct == []
    assert [r["title"] for r in recursive] == ["child item"]


@pytest.mark.parametrize("parent_id", [True, "1", -1, 999999999])
def test_invalid_parent_ids_are_refused(owner, parent_id):
    client, _ = owner
    response = client.post("/api/folders", json={"name": f"invalid-{parent_id}", "parent_id": parent_id})
    assert response.status_code == 400
    assert response.get_json()["code"] == "folder_parent_invalid"


def test_group_folder_parents_stay_in_the_same_group(owner):
    client, user = owner
    groups = [Group(name=f"nf-group-{uuid.uuid4().hex[:8]}") for _ in range(2)]
    db.session.add_all(groups)
    db.session.commit()
    for group in groups:
        db.session.add(GroupMembership(user_id=user.id, group_id=group.id, role="admin"))
    db.session.commit()
    roots = [client.post("/api/folders", json={"name": f"group-root-{group.id}", "group_id": group.id}).get_json()
             for group in groups]
    response = client.post("/api/folders", json={"name": "group-child", "parent_id": roots[0]["id"],
                                                   "group_id": groups[0].id})
    assert response.status_code == 201
    child = response.get_json()

    response = client.post("/api/folders", json={"name": "wrong-group-child", "parent_id": roots[0]["id"],
                                                   "group_id": groups[1].id})
    assert response.status_code == 400
    assert response.get_json()["code"] == "folder_parent_invalid"
    response = client.put(f"/api/folders/{roots[0]['id']}", json={"group_id": groups[1].id})
    assert response.status_code == 400
    assert response.get_json()["code"] == "folder_group_tree_invalid"
    assert db.session.get(Folder, child["id"]).parent_id == roots[0]["id"]

    Folder.query.filter_by(user_id=user.id).delete()
    GroupMembership.query.filter_by(user_id=user.id).delete()
    for group in groups:
        db.session.delete(group)
    db.session.commit()
