"""Folder create and update: field checks, duplicate names, the group index.

#511: a refused duplicate name is logged at INFO, without the SQL text.
#512: names and colors are checked before they are stored.
#513: a group folder name is unique within its group, whoever created it.
"""
import logging
import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.init_db import _add_folder_group_name_index
from src.models import Folder, Group, GroupMembership, SystemSetting, User

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
def ctx():
    with app.app_context():
        SystemSetting.set_setting("enable_folders", "true", setting_type="boolean")
        made = {"users": [], "groups": []}
        yield made
        db.session.rollback()
        user_ids = [u.id for u in made["users"]]
        group_ids = [g.id for g in made["groups"]]
        Folder.query.filter(Folder.user_id.in_(user_ids)).delete(synchronize_session=False)
        GroupMembership.query.filter(GroupMembership.group_id.in_(group_ids)).delete(synchronize_session=False)
        Group.query.filter(Group.id.in_(group_ids)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.session.commit()


def _new_user(ctx, prefix="fw"):
    user = _user(prefix)
    ctx["users"].append(user)
    return user


def _new_group(ctx, *admins):
    group = Group(name=f"g_{uuid.uuid4().hex[:8]}")
    db.session.add(group)
    db.session.commit()
    ctx["groups"].append(group)
    for admin in admins:
        db.session.add(GroupMembership(user_id=admin.id, group_id=group.id, role="admin"))
    db.session.commit()
    return group


# --- #512: field checks -----------------------------------------------------

@pytest.mark.parametrize("name,code", [
    ("x" * 51, "folder_name_too_long"),
    ("x" * 2000, "folder_name_too_long"),
    ("   ", "folder_name_required"),
    ("line\nbreak", "folder_name_invalid"),
    ("carriage\rreturn", "folder_name_invalid"),
    ("nul\x00byte", "folder_name_invalid"),
    (42, "folder_name_required"),
])
def test_create_rejects_bad_names(ctx, name, code):
    user = _new_user(ctx)
    resp = _client(user).post("/api/folders", json={"name": name})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == code
    assert Folder.query.filter_by(user_id=user.id).count() == 0


def test_create_strips_and_accepts_fifty_characters(ctx):
    user = _new_user(ctx)
    client = _client(user)
    resp = client.post("/api/folders", json={"name": "  padded  "})
    assert resp.status_code == 201
    assert resp.get_json()["name"] == "padded"
    resp = client.post("/api/folders", json={"name": "y" * 50, "color": "#10b981"})
    assert resp.status_code == 201
    assert resp.get_json()["color"] == "#10b981"


@pytest.mark.parametrize("color", ["#10B981FF", "red", "#12345", "", 7])
def test_create_rejects_bad_colors(ctx, color):
    user = _new_user(ctx)
    resp = _client(user).post("/api/folders", json={"name": "c", "color": color})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "folder_color_invalid"


def test_missing_or_null_color_gets_the_default(ctx):
    user = _new_user(ctx)
    client = _client(user)
    assert client.post("/api/folders", json={"name": "a"}).get_json()["color"] == "#10B981"
    assert client.post("/api/folders", json={"name": "b", "color": None}).get_json()["color"] == "#10B981"


def test_update_checks_name_and_color(ctx):
    user = _new_user(ctx)
    client = _client(user)
    folder_id = client.post("/api/folders", json={"name": "orig", "color": "#123456"}).get_json()["id"]

    for body, code in [({"name": "z" * 51}, "folder_name_too_long"),
                       ({"name": "a\nb"}, "folder_name_invalid"),
                       ({"name": "  "}, "folder_name_required"),
                       ({"color": "#10B981FF"}, "folder_color_invalid")]:
        resp = client.put(f"/api/folders/{folder_id}", json=body)
        assert resp.status_code == 400, body
        assert resp.get_json()["code"] == code
    folder = db.session.get(Folder, folder_id)
    db.session.refresh(folder)
    assert (folder.name, folder.color) == ("orig", "#123456")

    resp = client.put(f"/api/folders/{folder_id}", json={"name": " renamed ", "color": "#ABCDEF"})
    assert resp.status_code == 200
    assert (resp.get_json()["name"], resp.get_json()["color"]) == ("renamed", "#ABCDEF")


def test_update_keeps_an_existing_long_name_sent_back_unchanged(ctx):
    """A name stored before the check can come back with the rest of the form."""
    user = _new_user(ctx)
    long_name = "L" * 60
    folder = Folder(name=long_name, user_id=user.id)
    if db.engine.name != "sqlite":
        pytest.skip("only SQLite can hold a name longer than the column")
    db.session.add(folder)
    db.session.commit()
    resp = _client(user).put(f"/api/folders/{folder.id}", json={"name": long_name, "color": "#000000"})
    assert resp.status_code == 200
    assert resp.get_json()["color"] == "#000000"


def test_group_folder_route_checks_name_and_color(ctx, monkeypatch):
    import src.api.folders as folders_module
    monkeypatch.setattr(folders_module, "ENABLE_INTERNAL_SHARING", True)
    admin = _new_user(ctx)
    group = _new_group(ctx, admin)
    client = _client(admin)
    resp = client.post(f"/api/groups/{group.id}/folders", json={"name": "q" * 51})
    assert resp.status_code == 400 and resp.get_json()["code"] == "folder_name_too_long"
    resp = client.post(f"/api/groups/{group.id}/folders", json={"name": "ok", "color": "blue"})
    assert resp.status_code == 400 and resp.get_json()["code"] == "folder_color_invalid"
    resp = client.post(f"/api/groups/{group.id}/folders", json={"name": " ok "})
    assert resp.status_code == 201 and resp.get_json()["name"] == "ok"


# --- #511: duplicate names are not application errors ----------------------

def test_name_clash_at_insert_is_400_and_logged_at_info(ctx, caplog):
    """The creator's own (name, user_id) constraint refuses the insert."""
    admin = _new_user(ctx)
    group = _new_group(ctx, admin)
    client = _client(admin)
    assert client.post("/api/folders", json={"name": "shared"}).status_code == 201

    with caplog.at_level(logging.INFO):
        resp = client.post("/api/folders", json={"name": "shared", "group_id": group.id})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "folder_name_exists"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    refused = [r for r in caplog.records if "name is already in use" in r.getMessage()]
    assert refused and refused[0].levelno == logging.INFO
    assert "INSERT" not in caplog.text and "UNIQUE" not in caplog.text


def test_rename_clash_at_update_is_400_and_logged_at_info(ctx, caplog):
    admin = _new_user(ctx)
    group = _new_group(ctx, admin)
    client = _client(admin)
    assert client.post("/api/folders", json={"name": "taken", "group_id": group.id}).status_code == 201
    personal_id = client.post("/api/folders", json={"name": "mine"}).get_json()["id"]

    with caplog.at_level(logging.INFO):
        resp = client.put(f"/api/folders/{personal_id}", json={"name": "taken"})
    assert resp.status_code == 400
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "UPDATE folder" not in caplog.text


# --- #513: unique within a group -------------------------------------------

def test_group_index_refuses_a_second_creator(ctx):
    first, second = _new_user(ctx), _new_user(ctx)
    group = _new_group(ctx, first, second)
    db.session.add(Folder(name="dup", user_id=first.id, group_id=group.id))
    db.session.commit()
    db.session.add(Folder(name="dup", user_id=second.id, group_id=group.id))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_group_index_allows_the_name_in_another_group_and_as_personal(ctx):
    first, second = _new_user(ctx), _new_user(ctx)
    group_a, group_b = _new_group(ctx, first), _new_group(ctx, second)
    db.session.add_all([
        Folder(name="same", user_id=first.id, group_id=group_a.id),
        Folder(name="same", user_id=second.id, group_id=group_b.id),
        Folder(name="same", user_id=_new_user(ctx).id, group_id=None),
    ])
    db.session.commit()


def test_second_admin_gets_the_group_message(ctx):
    first, second = _new_user(ctx), _new_user(ctx)
    group = _new_group(ctx, first, second)
    assert _client(first).post("/api/folders", json={"name": "team", "group_id": group.id}).status_code == 201
    resp = _client(second).post("/api/folders", json={"name": "team", "group_id": group.id})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "folder_name_exists_for_group"


def test_lost_insert_reports_the_group_message(ctx):
    """When the index refuses a group insert, the message names the group."""
    from src.api.folders import _name_taken_key
    first = _new_user(ctx)
    group = _new_group(ctx, first)
    db.session.add(Folder(name="race", user_id=first.id, group_id=group.id))
    db.session.commit()
    assert _name_taken_key("race", group.id, "folder_name_exists_for_group") == "folder_name_exists_for_group"
    assert _name_taken_key("other", group.id, "folder_name_exists_for_group") == "folder_name_exists"
    assert _name_taken_key("race", None, "folder_name_exists_for_group") == "folder_name_exists"


# --- #513: the startup migration -------------------------------------------

def _old_schema_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Folder.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX uq_folder_group_name"))
    return engine


def _names(engine):
    with engine.connect() as conn:
        return {r.id: r.name for r in conn.execute(text("SELECT id, name FROM folder"))}


def test_migration_renames_duplicates_and_adds_the_index(tmp_path):
    engine = _old_schema_engine(tmp_path)
    with engine.begin() as conn:
        for fid, name, user_id, group_id in [
            (1, "Team", 1, 10), (2, "Team", 2, 10), (3, "Team", 3, 10),
            (4, "Team (2)", 4, 10),          # the first suffix is taken in the group
            (5, "Team", 6, 11),              # another group keeps its name
            (6, "Team", 5, None),            # personal folder keeps its name
            (7, "N" * 50, 1, 12), (8, "N" * 50, 2, 12),
        ]:
            conn.execute(text("INSERT INTO folder (id, name, user_id, group_id, color) "
                              "VALUES (:i, :n, :u, :g, '#10B981')"),
                         {"i": fid, "n": name, "u": user_id, "g": group_id})

    with app.app_context():
        _add_folder_group_name_index(engine, app)

    names = _names(engine)
    assert names[1] == "Team" and names[4] == "Team (2)"
    assert names[2] == "Team (3)" and names[3] == "Team (4)"
    assert names[5] == "Team" and names[6] == "Team"
    assert names[7] == "N" * 50 and names[8] == "N" * 46 + " (2)"
    assert "uq_folder_group_name" in {i["name"] for i in inspect(engine).get_indexes("folder")}

    with engine.begin() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(text("INSERT INTO folder (name, user_id, group_id) VALUES ('Team', 9, 10)"))

    with app.app_context():
        _add_folder_group_name_index(engine, app)   # a second run changes nothing
    assert _names(engine) == names


def test_migration_avoids_the_creators_own_names(tmp_path):
    engine = _old_schema_engine(tmp_path)
    with engine.begin() as conn:
        for fid, name, user_id, group_id in [
            (1, "Ops", 1, 10), (2, "Ops", 2, 10), (3, "Ops (2)", 2, None),
        ]:
            conn.execute(text("INSERT INTO folder (id, name, user_id, group_id, color) "
                              "VALUES (:i, :n, :u, :g, '#10B981')"),
                         {"i": fid, "n": name, "u": user_id, "g": group_id})
    with app.app_context():
        _add_folder_group_name_index(engine, app)
    assert _names(engine)[2] == "Ops (3)"
