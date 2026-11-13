"""The Regenerate summary dialog's prompt (#584).

Everyone sees only their own prompt: the viewer's tags with prompts on the
recording, the folder prompt only for the owner, then the viewer's own default,
the admin default and the shipped default. An editor runs the prompt they see;
automatic summaries keep the owner's view. Save writes only to a target the
viewer may change. An unchanged prompt sends the same request as before.

SHARED-DB: users, groups, tags, folders and the recording are removed afterwards.
"""

import json
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.app as app_module
import src.tasks.processing as proc
from src.app import app, db
from src.models import (Folder, Group, GroupMembership, InternalShare, Recording, RecordingTag,
                        SharedRecordingState, SystemSetting, Tag, User)
from src.services.job_queue import job_queue
from src.services.reprocessing import summary_prompt_params
from src.services.summary_prompt import describe_summary_prompt

from tests.test_folder_group_shares import _client

app.config["WTF_CSRF_ENABLED"] = False

TRANSCRIPT = "Speaker 1: " + "we agreed on the plan for the launch. " * 5


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setattr(app_module, "ENABLE_INTERNAL_SHARING", True)
    with app.app_context():
        previous_folders = SystemSetting.get_setting("enable_folders", False)
        SystemSetting.set_setting("enable_folders", "true", setting_type="boolean")
        s = uuid.uuid4().hex[:8]
        users = {}
        for name, prompt in (("owner", "OWNER DEFAULT"), ("editor", "EDITOR DEFAULT"),
                             ("viewer", None), ("member", None)):
            users[name] = User(username=f"rg_{name}_{s}", email=f"rg_{name}_{s}@local.test",
                               password="x", summary_prompt=prompt)
            db.session.add(users[name])
        db.session.commit()
        group = Group(name=f"rg_{s}")
        db.session.add(group)
        db.session.commit()
        db.session.add_all([
            GroupMembership(user_id=users["owner"].id, group_id=group.id, role="admin"),
            GroupMembership(user_id=users["member"].id, group_id=group.id, role="member"),
        ])
        tags = {
            "owner": Tag(name=f"own-{s}", user_id=users["owner"].id, custom_prompt="OWNER TAG"),
            "editor": Tag(name=f"ed-{s}", user_id=users["editor"].id, custom_prompt="EDITOR TAG"),
            "editor_other": Tag(name=f"ed2-{s}", user_id=users["editor"].id, custom_prompt="EDITOR OTHER TAG"),
            "group": Tag(name=f"grp-{s}", user_id=users["owner"].id, group_id=group.id,
                         custom_prompt="GROUP TAG", auto_share_on_apply=False, share_with_group_lead=False),
        }
        folder = Folder(name=f"f-{s}", user_id=users["owner"].id, custom_prompt="OWNER FOLDER")
        db.session.add_all(list(tags.values()) + [folder])
        db.session.commit()
        rec = Recording(user_id=users["owner"].id, title="Sync", original_filename="s.wav",
                        status="COMPLETED", transcription=TRANSCRIPT, summary="Old summary")
        db.session.add(rec)
        db.session.commit()
        db.session.add_all([
            InternalShare(recording_id=rec.id, owner_id=users["owner"].id, shared_with_user_id=users["editor"].id,
                          can_edit=True, can_reshare=False, source_type="manual"),
            InternalShare(recording_id=rec.id, owner_id=users["owner"].id, shared_with_user_id=users["viewer"].id,
                          can_edit=False, can_reshare=False, source_type="manual"),
            InternalShare(recording_id=rec.id, owner_id=users["owner"].id, shared_with_user_id=users["member"].id,
                          can_edit=True, can_reshare=False, source_type="manual"),
        ])
        db.session.commit()
        ids = {k: u.id for k, u in users.items()}
        ids.update({f"tag_{k}": t.id for k, t in tags.items()})
        ids.update(folder=folder.id, rec=rec.id, group=group.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        for model in (InternalShare, SharedRecordingState, RecordingTag):
            model.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter_by(id=ids["rec"]).delete()
        user_ids = [ids[k] for k in ("owner", "editor", "viewer", "member")]
        Tag.query.filter(Tag.user_id.in_(user_ids)).delete(synchronize_session=False)
        Folder.query.filter_by(id=ids["folder"]).delete()
        GroupMembership.query.filter_by(group_id=ids["group"]).delete()
        Group.query.filter_by(id=ids["group"]).delete()
        User.query.filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        SystemSetting.set_setting("enable_folders", "true" if previous_folders else "false",
                                  setting_type="boolean")
        db.session.commit()


def _attach(ids, *tag_keys, folder=False):
    rec = db.session.get(Recording, ids["rec"])
    for order, key in enumerate(tag_keys):
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=ids[f"tag_{key}"], order=order))
    if folder:
        rec.folder_id = ids["folder"]
    db.session.commit()
    return db.session.get(Recording, ids["rec"])


def _user(ids, key):
    return db.session.get(User, ids[key])


# --- Resolution ---------------------------------------------------------------

@pytest.mark.parametrize("tags,folder,expected", [
    (("owner",), True, ("OWNER TAG", "tag")),
    ((), True, ("OWNER FOLDER", "folder")),
    ((), False, ("OWNER DEFAULT", "user")),
])
def test_owner_view_is_the_automatic_summary_prompt(world, tags, folder, expected):
    with app.app_context():
        rec = _attach(world, *tags, folder=folder)
        owner = _user(world, "owner")
        assert proc.resolve_summary_instructions(rec) == expected
        assert proc.resolve_summary_instructions(rec, viewer=owner) == expected


def test_an_editor_resolves_their_own_tags_then_their_default(world):
    with app.app_context():
        rec = _attach(world, "owner", "editor", folder=True)
        editor = _user(world, "editor")
        assert proc.resolve_summary_instructions(rec, viewer=editor) == ("EDITOR TAG", "tag")
        RecordingTag.query.filter_by(recording_id=rec.id, tag_id=world["tag_editor"]).delete()
        db.session.commit()
        # The owner's tag and folder do not apply to the editor.
        assert proc.resolve_summary_instructions(rec, viewer=editor) == ("EDITOR DEFAULT", "user")


def test_the_folder_prompt_is_the_owners_only(world):
    with app.app_context():
        rec = _attach(world, folder=True)
        member = _user(world, "member")
        prompt, source = proc.resolve_summary_instructions(rec, viewer=member)
        assert source in ("admin", "default") and prompt != "OWNER FOLDER"
        info = describe_summary_prompt(rec, member)
        assert all(o["kind"] != "folder" for o in info["start_from"])
        assert "OWNER FOLDER" not in json.dumps(info)


def test_a_group_tag_applies_to_a_group_member(world):
    with app.app_context():
        rec = _attach(world, "group")
        member = _user(world, "member")
        assert proc.resolve_summary_instructions(rec, viewer=member) == ("GROUP TAG", "tag")


def test_an_editor_never_sees_the_owners_prompts(world):
    with app.app_context():
        rec = _attach(world, "owner", folder=True)
        info = describe_summary_prompt(rec, _user(world, "editor"))
        text = json.dumps(info)
        for owner_text in ("OWNER DEFAULT", "OWNER TAG", "OWNER FOLDER"):
            assert owner_text not in text
        assert info["prompt"] == "EDITOR DEFAULT" and info["source"] == "user"
        assert info["is_owner"] is False
        kinds = [(o["kind"], o["applies"]) for o in info["start_from"]]
        assert ("user", False) in kinds and ("tag", False) in kinds
        assert {o["prompt"] for o in info["start_from"] if o["kind"] == "tag"} == {"EDITOR TAG", "EDITOR OTHER TAG"}


def test_the_owner_sees_tags_and_folder_first(world):
    with app.app_context():
        rec = _attach(world, "owner", folder=True)
        info = describe_summary_prompt(rec, _user(world, "owner"))
        assert (info["prompt"], info["source"], info["source_key"]) == ("OWNER TAG", "tag", f"tag:{world['tag_owner']}")
        applies = [o["key"] for o in info["start_from"] if o["applies"]]
        assert applies == [f"tag:{world['tag_owner']}", f"folder:{world['folder']}"]
        assert info["save_target"] == {"kind": "tag", "id": world["tag_owner"], "name": rec.tags[0].name}


def test_joined_tag_prompts_have_no_single_save_target(world):
    with app.app_context():
        rec = _attach(world, "owner", "group")
        info = describe_summary_prompt(rec, _user(world, "owner"))
        assert info["prompt"] == "OWNER TAG\n\nGROUP TAG"
        assert info["source_key"] == "tags" and info["save_target"] is None


def test_the_admin_default_saves_to_the_viewers_default(world):
    with app.app_context():
        rec = _attach(world)
        viewer = _user(world, "member")
        info = describe_summary_prompt(rec, viewer)
        assert info["source"] in ("admin", "default")
        assert info["save_target"] == {"kind": "user", "id": None, "name": None}


# --- Routes -------------------------------------------------------------------

def test_the_prompt_route_needs_edit_access(world):
    with app.app_context():
        _attach(world)
        users = {k: _user(world, k) for k in ("owner", "editor", "viewer")}
        stranger = User(username=f"rg_x_{uuid.uuid4().hex[:6]}", email=f"x{uuid.uuid4().hex[:6]}@local.test", password="x")
        db.session.add(stranger)
        db.session.commit()
        try:
            path = f"/recording/{world['rec']}/summary_prompt"
            assert _client(users["owner"]).get(path).status_code == 200
            assert _client(users["editor"]).get(path).status_code == 200
            assert _client(users["viewer"]).get(path).status_code == 403
            assert _client(users["viewer"]).post(path, json={"target": "user", "prompt": "x"}).status_code == 403
            assert _client(stranger).get(path).status_code == 404
            assert _client(stranger).post(path, json={"target": "user", "prompt": "x"}).status_code == 404
        finally:
            db.session.delete(stranger)
            db.session.commit()


def _save(ids, who, body):
    with app.app_context():
        return _client(_user(ids, who)).post(f"/recording/{ids['rec']}/summary_prompt", json=body)


def test_save_to_own_targets(world):
    with app.app_context():
        _attach(world, "owner", folder=True)
    assert _save(world, "owner", {"target": "tag", "id": world["tag_owner"], "prompt": "NEW TAG"}).status_code == 200
    assert _save(world, "owner", {"target": "folder", "id": world["folder"], "prompt": "NEW FOLDER"}).status_code == 200
    resp = _save(world, "editor", {"target": "user", "prompt": "NEW EDITOR DEFAULT"})
    assert resp.status_code == 200
    assert resp.get_json()["prompt"] == "NEW EDITOR DEFAULT"
    with app.app_context():
        assert db.session.get(Tag, world["tag_owner"]).custom_prompt == "NEW TAG"
        assert db.session.get(Folder, world["folder"]).custom_prompt == "NEW FOLDER"
        assert _user(world, "editor").summary_prompt == "NEW EDITOR DEFAULT"
        assert _user(world, "owner").summary_prompt == "OWNER DEFAULT"


@pytest.mark.parametrize("who,body", [
    ("editor", {"target": "tag", "id": "tag_owner"}),       # another user's personal tag
    ("editor", {"target": "folder", "id": "folder"}),       # the owner's folder
    ("member", {"target": "tag", "id": "tag_group"}),       # group tag, not a group admin
    ("member", {"target": "folder", "id": "folder"}),
])
def test_save_refuses_targets_the_viewer_cannot_change(world, who, body):
    with app.app_context():
        _attach(world, "owner", "group", folder=True)
    body = dict(body, id=world[body["id"]], prompt="HIJACK")
    resp = _save(world, who, body)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "summary_prompt_save_denied"
    with app.app_context():
        assert db.session.get(Tag, world["tag_owner"]).custom_prompt == "OWNER TAG"
        assert db.session.get(Tag, world["tag_group"]).custom_prompt == "GROUP TAG"
        assert db.session.get(Folder, world["folder"]).custom_prompt == "OWNER FOLDER"


@pytest.mark.parametrize("body", [
    {"target": "admin", "prompt": "x"},
    {"target": "user", "prompt": "   "},
    {"target": "user", "prompt": "x" * 8001},
    {"target": "tag", "id": "nope", "prompt": "x"},
])
def test_save_rejects_bad_requests(world, body):
    resp = _save(world, "owner", body)
    assert resp.status_code in (400, 403)
    with app.app_context():
        assert _user(world, "owner").summary_prompt == "OWNER DEFAULT"


# --- Running ------------------------------------------------------------------

def test_prompt_params_keep_the_old_requests():
    assert summary_prompt_params() == (None, False)
    assert summary_prompt_params("", "replace", "") == (None, False)
    assert summary_prompt_params("Mine", "replace") == ("Mine", False)
    assert summary_prompt_params("Agenda", "append") == ("Agenda", True)
    # Added instructions alone are the old append request.
    assert summary_prompt_params(None, None, "Agenda") == ("Agenda", True)
    assert summary_prompt_params("Mine", "replace", "Agenda") == (
        "Mine\n\nAdditional context for this recording:\nAgenda", False)


def test_unchanged_prompt_reprocess_queues_the_resolved_prompt(world):
    with app.app_context():
        _attach(world)
        owner = _user(world, "owner")
        with patch("src.services.llm.client", MagicMock()), \
                patch.object(job_queue, "enqueue", return_value=1) as enq:
            resp = _client(owner).post(f"/recording/{world['rec']}/reprocess_summary",
                                       json={"reprocess_summary": True, "prompt_variables": {}})
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        params = enq.call_args.kwargs["params"]
        assert (params["custom_prompt"], params["custom_prompt_append"]) == (None, False)


def test_an_edited_prompt_with_instructions_runs_as_sent(world):
    with app.app_context():
        _attach(world)
        editor = _user(world, "editor")
        with patch("src.services.llm.client", MagicMock()), \
                patch.object(job_queue, "enqueue", return_value=1) as enq:
            resp = _client(editor).post(f"/recording/{world['rec']}/reprocess_summary", json={
                "reprocess_summary": True, "custom_prompt": "EDITOR DEFAULT", "prompt_mode": "replace",
                "additional_instructions": "Focus on dates"})
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        params = enq.call_args.kwargs["params"]
        assert params["custom_prompt_append"] is False
        assert params["custom_prompt"] == "EDITOR DEFAULT\n\nAdditional context for this recording:\nFocus on dates"


def test_an_automatic_summary_still_uses_the_owners_prompt(world):
    with app.app_context():
        _attach(world, "owner", "editor")
        seen = []

        def fake(messages, **kw):
            seen.append("\n".join(m["content"] for m in messages))
            return MagicMock(choices=[MagicMock(message=MagicMock(content="## Summary", reasoning=None))])

        with patch.object(proc, "client", MagicMock()), patch.object(proc, "call_llm_completion", side_effect=fake), \
                patch.object(proc, "finish_processing"):
            proc.generate_summary_only_task(app.app_context(), world["rec"], user_id=world["editor"])
        assert "OWNER TAG" in seen[0] and "EDITOR TAG" not in seen[0]


def test_generate_summary_accepts_instructions_and_variables(world):
    with app.app_context():
        rec = _attach(world)
        rec.summary = None
        db.session.commit()
        owner = _user(world, "owner")
        import src.api.recordings as recordings_module
        with patch.object(recordings_module, "client", MagicMock()), \
                patch.object(job_queue, "enqueue", return_value=1) as enq:
            resp = _client(owner).post(f"/recording/{world['rec']}/generate_summary", json={
                "additional_instructions": "Agenda", "prompt_variables": {"client": "Acme"}})
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        params = enq.call_args.kwargs["params"]
        assert (params["custom_prompt"], params["custom_prompt_append"]) == ("Agenda", True)
        assert db.session.get(Recording, world["rec"]).prompt_variables == {"client": "Acme"}


def _v1_token(user_id):
    from src.models import APIToken
    from src.utils.token_auth import hash_token
    plain = f"test-token-{uuid.uuid4().hex}"
    db.session.add(APIToken(user_id=user_id, token_hash=hash_token(plain), name="rg-v1"))
    db.session.commit()
    return plain


def _v1_summarize(world, who, body):
    from src.models import APIToken
    plain = _v1_token(world[who])
    try:
        with patch("src.services.llm.client", MagicMock()), \
                patch.object(job_queue, "enqueue", return_value=1) as enq:
            with app.test_client() as c:
                resp = c.post(f"/api/v1/recordings/{world['rec']}/summarize", json=body,
                              headers={"Authorization": f"Bearer {plain}"})
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        return enq.call_args.kwargs["params"]
    finally:
        APIToken.query.filter_by(user_id=world[who], name="rg-v1").delete()
        db.session.commit()


def test_v1_summarize_by_an_editor_runs_the_editors_prompt(world):
    with app.app_context():
        _attach(world, "owner", "editor")
        params = _v1_summarize(world, "editor", {})
        assert (params["custom_prompt"], params["custom_prompt_append"]) == ("EDITOR TAG", False)


def test_v1_summarize_by_an_editor_adds_instructions_to_the_editors_prompt(world):
    with app.app_context():
        _attach(world)
        params = _v1_summarize(world, "editor", {"additional_instructions": "Focus on dates"})
        assert params["custom_prompt_append"] is False
        assert params["custom_prompt"] == "EDITOR DEFAULT\n\nAdditional context for this recording:\nFocus on dates"


def test_v1_summarize_by_an_editor_keeps_a_replacing_prompt(world):
    with app.app_context():
        _attach(world, "editor")
        params = _v1_summarize(world, "editor", {"custom_prompt": "Only the decisions"})
        assert (params["custom_prompt"], params["custom_prompt_append"]) == ("Only the decisions", False)


def test_v1_summarize_by_the_owner_is_unchanged(world):
    with app.app_context():
        _attach(world, "owner")
        params = _v1_summarize(world, "owner", {})
        assert (params["custom_prompt"], params["custom_prompt_append"]) == (None, False)
