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


# --- Review additions: scope, deletion paths, payload shape ---------------


def _group_with_admins(*users):
    group = Group(name=f"nf-group-{uuid.uuid4().hex[:8]}")
    db.session.add(group)
    db.session.commit()
    for user in users:
        db.session.add(GroupMembership(user_id=user.id, group_id=group.id, role="admin"))
    db.session.commit()
    return group


def _drop(*objects):
    for obj in objects:
        obj = db.session.merge(obj)
        if isinstance(obj, User):
            Folder.query.filter_by(user_id=obj.id).delete()
            GroupMembership.query.filter_by(user_id=obj.id).delete()
        if isinstance(obj, Group):
            Folder.query.filter_by(group_id=obj.id).delete()
            GroupMembership.query.filter_by(group_id=obj.id).delete()
        db.session.delete(obj)
    db.session.commit()


def test_descendants_stay_in_the_folder_scope(owner):
    """A stale parent link into another user's folder is not followed."""
    from src.services.folder_tree import descendant_folder_ids
    client, user = owner
    root = _create(client, "root")
    child = _create(client, "child", root["id"])
    other = _user("nf_scope")
    foreign = Folder(name="foreign", user_id=other.id, parent_id=root["id"])
    db.session.add(foreign)
    db.session.commit()

    assert descendant_folder_ids(db.session.get(Folder, root["id"])) == {root["id"], child["id"]}
    folders = {f["id"]: f for f in client.get("/api/folders").get_json()}
    assert foreign.id not in folders
    other_folders = {f["id"]: f for f in _client(other).get("/api/folders").get_json()}
    assert other_folders[foreign.id]["total_recording_count"] == 0
    _drop(other)


def test_a_cycle_in_bad_data_ends_the_recursion(owner):
    from src.services.folder_tree import descendant_folder_ids
    client, _ = owner
    a = _create(client, "a")
    b = _create(client, "b", a["id"])
    db.session.get(Folder, a["id"]).parent_id = b["id"]
    db.session.commit()
    assert descendant_folder_ids(db.session.get(Folder, a["id"])) == {a["id"], b["id"]}
    folders = {f["id"]: f for f in client.get("/api/folders").get_json()}
    assert folders[a["id"]]["total_recording_count"] == 0


def test_personal_and_group_parents_do_not_mix(owner):
    client, user = owner
    group = _group_with_admins(user)
    personal = _create(client, "personal")
    group_root = client.post("/api/folders", json={"name": "group-root", "group_id": group.id}).get_json()

    response = client.post("/api/folders", json={"name": "g-under-p", "group_id": group.id,
                                                   "parent_id": personal["id"]})
    assert response.get_json()["code"] == "folder_parent_invalid"
    response = client.post("/api/folders", json={"name": "p-under-g", "parent_id": group_root["id"]})
    assert response.get_json()["code"] == "folder_parent_invalid"
    response = client.post(f"/api/groups/{group.id}/folders", json={"name": "gp", "parent_id": personal["id"]})
    assert response.status_code in (400, 403)
    response = client.put(f"/api/folders/{personal['id']}", json={"parent_id": group_root["id"]})
    assert response.get_json()["code"] == "folder_parent_invalid"
    _drop(group)


def test_moving_a_child_out_of_its_group_needs_a_valid_parent(owner):
    client, user = owner
    group = _group_with_admins(user)
    root = client.post("/api/folders", json={"name": "g-root", "group_id": group.id}).get_json()
    child = client.post("/api/folders", json={"name": "g-child", "group_id": group.id,
                                               "parent_id": root["id"]}).get_json()

    response = client.put(f"/api/folders/{child['id']}", json={"group_id": None})
    assert response.status_code == 400
    assert response.get_json()["code"] == "folder_parent_invalid"
    db.session.expire_all()
    assert db.session.get(Folder, child["id"]).group_id == group.id

    response = client.put(f"/api/folders/{child['id']}", json={"group_id": None, "parent_id": None})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["parent_id"] is None
    assert response.get_json()["group_id"] is None
    _drop(group)


def test_update_without_parent_id_keeps_the_parent(owner):
    """Today's folder modal sends no parent_id; an edit must not flatten the tree."""
    client, _ = owner
    root = _create(client, "root")
    child = _create(client, "child", root["id"])
    response = client.put(f"/api/folders/{child['id']}", json={"name": "renamed", "color": "#123456"})
    assert response.status_code == 200
    assert response.get_json()["parent_id"] == root["id"]
    response = client.put(f"/api/folders/{child['id']}", json={"parent_id": None})
    assert response.get_json()["parent_id"] is None


def test_deleting_a_middle_folder_promotes_to_its_parent(owner):
    client, _ = owner
    root = _create(client, "root")
    middle = _create(client, "middle", root["id"])
    leaf = _create(client, "leaf", middle["id"])
    assert client.delete(f"/api/folders/{middle['id']}").status_code == 200
    db.session.expire_all()
    assert db.session.get(Folder, leaf["id"]).parent_id == root["id"]


def test_removing_a_user_promotes_children_created_by_others():
    """The user cascade deletes the user's group folders; another member's
    subfolder moves up instead of keeping a dangling parent_id."""
    with app.app_context():
        SystemSetting.set_setting("enable_folders", "true", setting_type="boolean")
        alice, bob = _user("nf_alice"), _user("nf_bob")
        group = _group_with_admins(alice, bob)
        a_client = _client(alice)
        a_root = a_client.post("/api/folders", json={"name": "a-root", "group_id": group.id}).get_json()
        a_mid = a_client.post("/api/folders", json={"name": "a-mid", "group_id": group.id,
                                                     "parent_id": a_root["id"]}).get_json()
        b_leaf = _client(bob).post("/api/folders", json={"name": "b-leaf", "group_id": group.id,
                                                          "parent_id": a_mid["id"]}).get_json()
        assert b_leaf["parent_id"] == a_mid["id"]

        GroupMembership.query.filter_by(user_id=alice.id).delete()
        db.session.delete(db.session.get(User, alice.id))
        db.session.commit()
        db.session.expire_all()

        assert db.session.get(Folder, a_root["id"]) is None
        assert db.session.get(Folder, a_mid["id"]) is None
        leaf = db.session.get(Folder, b_leaf["id"])
        assert leaf is not None and leaf.parent_id is None and leaf.group_id == group.id
        _drop(bob, group)


def test_deleting_a_group_keeps_each_creators_tree():
    with app.app_context():
        SystemSetting.set_setting("enable_folders", "true", setting_type="boolean")
        alice, bob = _user("nf_galice"), _user("nf_gbob")
        site_admin = User(username=f"nf_admin_{uuid.uuid4().hex[:8]}",
                          email=f"nf_admin_{uuid.uuid4().hex[:8]}@local.test", password="x", is_admin=True)
        db.session.add(site_admin)
        db.session.commit()
        group = _group_with_admins(alice, bob)
        a_client = _client(alice)
        root = a_client.post("/api/folders", json={"name": "g-root", "group_id": group.id}).get_json()
        a_child = a_client.post("/api/folders", json={"name": "a-child", "group_id": group.id,
                                                       "parent_id": root["id"]}).get_json()
        b_child = _client(bob).post("/api/folders", json={"name": "b-child", "group_id": group.id,
                                                           "parent_id": root["id"]}).get_json()

        assert _client(site_admin).delete(f"/api/admin/groups/{group.id}").status_code == 200
        db.session.expire_all()
        kept_root, kept_a, kept_b = (db.session.get(Folder, f["id"]) for f in (root, a_child, b_child))
        assert kept_root.group_id is None and kept_a.group_id is None and kept_b.group_id is None
        assert kept_a.parent_id == root["id"]
        assert kept_b.parent_id is None
        _drop(alice, bob, site_admin)


def test_folder_payloads_keep_their_fields(owner):
    """Every field of a folder dict survives; the folder endpoints add
    parent_id and total_recording_count, and a recording's embedded folder
    adds parent_id only."""
    client, user = owner
    root = _create(client, "root")
    child = _create(client, "child", root["id"])
    db.session.add(Recording(user_id=user.id, title="in child", audio_path="/tmp/c.mp3",
                             status="COMPLETED", folder_id=child["id"]))
    db.session.commit()

    master_fields = {
        'id', 'name', 'color', 'group_id', 'is_group_folder', 'group_name', 'custom_prompt',
        'title_prompt', 'default_language', 'default_min_speakers', 'default_max_speakers',
        'default_hotwords', 'default_initial_prompt', 'default_transcription_model',
        'protect_from_deletion', 'retention_days', 'auto_share_on_apply', 'share_with_group_lead',
        'naming_template_id', 'naming_template_name', 'export_template_id', 'export_template_name',
        'created_at', 'recording_count',
    }
    assert set(root) == master_fields | {'parent_id', 'total_recording_count'}
    listed = {f["id"]: f for f in client.get("/api/folders").get_json()}
    assert master_fields | {'parent_id', 'total_recording_count', 'can_edit', 'user_role'} == set(listed[root["id"]])
    assert listed[root["id"]]["recording_count"] == 0
    assert listed[root["id"]]["total_recording_count"] == 1

    single = client.get(f"/api/v1/folders/{root['id']}").get_json()
    assert single["recording_count"] == 0 and single["total_recording_count"] == 1

    updated = client.put(f"/api/folders/{root['id']}", json={"color": "#654321"}).get_json()
    assert updated["total_recording_count"] == 1

    recordings = client.get(f"/api/recordings?folder={child['id']}").get_json()["recordings"]
    embedded = recordings[0]["folder"]
    assert embedded["parent_id"] == root["id"]
    assert "total_recording_count" not in embedded
    assert master_fields <= set(embedded)


def test_include_children_is_off_unless_asked(owner):
    client, user = owner
    root = _create(client, "root")
    child = _create(client, "child", root["id"])
    db.session.add(Recording(user_id=user.id, title="child item", audio_path="/tmp/c.mp3",
                             status="COMPLETED", folder_id=child["id"]))
    db.session.commit()
    for value in ("", "false", "1", "yes"):
        response = client.get(f"/api/recordings?folder={root['id']}&include_children={value}")
        assert response.get_json()["recordings"] == []
    response = client.get("/api/recordings?folder=999999999&include_children=true")
    assert response.status_code == 200
    assert response.get_json()["recordings"] == []
