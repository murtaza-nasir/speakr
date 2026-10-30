"""A folder filter that is not an id or "none" is refused, not ignored (#514)."""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Folder, Recording, SystemSetting, User

app.config["WTF_CSRF_ENABLED"] = False


@pytest.fixture
def owner():
    with app.app_context():
        SystemSetting.set_setting("enable_folders", "true", setting_type="boolean")
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f"ff_{suffix}", email=f"ff_{suffix}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        folder = Folder(name="filtered", user_id=user.id)
        db.session.add(folder)
        db.session.commit()
        db.session.add_all([
            Recording(user_id=user.id, title="in folder", audio_path="/tmp/a.mp3",
                      status="COMPLETED", folder_id=folder.id),
            Recording(user_id=user.id, title="loose", audio_path="/tmp/b.mp3", status="COMPLETED"),
        ])
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield client, folder.id
        Recording.query.filter_by(user_id=user.id).delete()
        Folder.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()


@pytest.mark.parametrize("value", ["not-an-id", "+5", "-1", "1.0", "²"])
def test_v1_list_rejects_bad_folder_id(owner, value):
    client, _ = owner
    resp = client.get("/api/v1/recordings", query_string={"folder_id": value})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_parameter"


def test_v1_list_keeps_valid_folder_filters(owner):
    client, folder_id = owner
    titles = lambda r: sorted(x["title"] for x in r.get_json()["recordings"])
    assert titles(client.get(f"/api/v1/recordings?folder_id={folder_id}")) == ["in folder"]
    assert titles(client.get("/api/v1/recordings?folder_id=none")) == ["loose"]
    assert titles(client.get("/api/v1/recordings")) == ["in folder", "loose"]


@pytest.mark.parametrize("value", ["nope", "²"])
def test_v1_search_rejects_bad_folder_id(owner, value):
    client, _ = owner
    resp = client.get("/api/v1/search", query_string={"q": "test", "folder_id": value})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_parameter"


@pytest.mark.parametrize("value", ["not-an-id", "²"])
def test_web_list_rejects_bad_folder(owner, value):
    client, _ = owner
    resp = client.get("/api/recordings", query_string={"folder": value})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_parameter"


def test_web_list_keeps_valid_folder_filters(owner):
    client, folder_id = owner
    titles = lambda r: sorted(x["title"] for x in r.get_json()["recordings"])
    assert titles(client.get(f"/api/recordings?folder={folder_id}")) == ["in folder"]
    assert titles(client.get("/api/recordings?folder=none")) == ["loose"]
