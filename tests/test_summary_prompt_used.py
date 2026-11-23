"""The prompt that produced the current summary is stored with the recording (#584).

Stored in its editable form (before variable substitution, without the
instructions added for one run) on every path that saves a summary, with its
source and the user whose run it was. Only that user gets it back from the
Regenerate summary dialog's route. The stored fields are cleared with the
summary and restored with it when a reprocess is canceled.

SHARED-DB: the world fixture of test_regenerate_summary_prompt removes its rows.
"""

import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.tasks.processing as proc
from src.app import app, db
from src.models import Recording, Tag, User
from src.services.job_queue import job_queue
from src.services.summary_prompt import describe_summary_prompt

from tests.test_folder_group_shares import _client
from tests.test_regenerate_summary_prompt import _attach, _user, _v1_token, world  # noqa: F401

app.config["WTF_CSRF_ENABLED"] = False

FIELDS = ("summary_prompt_used", "summary_prompt_source", "summary_prompt_source_name", "summary_prompt_user_id")


def _stored(ids):
    db.session.expire_all()
    rec = db.session.get(Recording, ids["rec"])
    return tuple(getattr(rec, f) for f in FIELDS)


def _store(ids, prompt="STALE", source="custom", name=None, who="owner"):
    rec = db.session.get(Recording, ids["rec"])
    rec.set_summary_prompt_used(prompt, source, name, ids[who])
    db.session.commit()


def _run(ids, params=None, reply="## New summary", error=None):
    """Run the summary job with the given job parameters; returns what the LLM was sent."""
    seen = []

    def fake(messages, **kw):
        seen.append("\n".join(str(m["content"]) for m in messages))
        if error:
            raise error
        return MagicMock(choices=[MagicMock(message=MagicMock(content=reply, reasoning=None))])

    # The POST above ran in this app context, so the audit row it added to the
    # session is still uncommitted (enqueue is stubbed). Drop it before the job
    # runs on a session of its own, or that session's commit meets the lock.
    db.session.rollback()
    rec = db.session.get(Recording, ids["rec"])
    with patch.object(proc.llm_config, "is_configured", return_value=True), patch.object(proc, "call_llm_completion", side_effect=fake), \
            patch.object(proc, "finish_processing"):
        try:
            if params is None:
                proc.generate_summary_only_task(app.app_context(), ids["rec"])
            else:
                job_queue._run_reprocess_summary(None, rec, params)
        finally:
            # finish_processing is patched out, so set the status it would set.
            db.session.expire_all()
            rec = db.session.get(Recording, ids["rec"])
            if rec.status == "SUMMARIZING":
                rec.status = "COMPLETED"
                db.session.commit()
    return seen[0] if seen else ""


def _queue(ids, who, body, route="reprocess_summary"):
    """POST a web summary route; returns the job parameters it queued."""
    with patch("src.services.llm.client", MagicMock()), patch("src.services.llm_config.is_configured", return_value=True), \
            patch.object(job_queue, "enqueue", return_value=1) as enq:
        resp = _client(_user(ids, who)).post(f"/recording/{ids['rec']}/{route}", json=body)
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return enq.call_args.kwargs["params"]


def _prompt_info(ids, who):
    resp = _client(_user(ids, who)).get(f"/recording/{ids['rec']}/summary_prompt")
    assert resp.status_code == 200
    return resp.get_json()


# --- Storage on each path -------------------------------------------------------

def test_an_automatic_summary_stores_the_resolved_prompt_before_substitution(world):
    with app.app_context():
        rec = _attach(world, "owner")
        tag = db.session.get(Tag, world["tag_owner"])
        tag.custom_prompt = "OWNER TAG for {{client}}"
        rec.prompt_variables = {"client": "Acme"}
        db.session.commit()
        sent = _run(world)
        assert "OWNER TAG for Acme" in sent
        assert _stored(world) == ("OWNER TAG for {{client}}", "tag", tag.name, world["owner"])
        assert db.session.get(Recording, world["rec"]).summary == "## New summary"


def test_joined_tag_prompts_are_stored_as_tags(world):
    with app.app_context():
        rec = _attach(world, "owner", "group")
        names = ", ".join(t.name for t in rec.get_visible_tags(_user(world, "owner")))
        _run(world)
        assert _stored(world) == ("OWNER TAG\n\nGROUP TAG", "tags", names, world["owner"])


@pytest.mark.parametrize("folder,expected", [(True, ("OWNER FOLDER", "folder")), (False, ("OWNER DEFAULT", "user"))])
def test_folder_and_default_sources(world, folder, expected):
    with app.app_context():
        _attach(world, folder=folder)
        _run(world)
        prompt, source, name, user_id = _stored(world)
        assert (prompt, source, user_id) == expected + (world["owner"],)
        assert (name is not None) == folder


def test_regenerate_with_a_custom_prompt_stores_it_without_the_added_instructions(world):
    with app.app_context():
        _attach(world)
        _store(world)
        params = _queue(world, "owner", {"custom_prompt": "Mine for {{client}}", "prompt_mode": "replace",
                                         "additional_instructions": "Focus on dates",
                                         "prompt_variables": {"client": "Acme"}})
        assert _stored(world) == (None, None, None, None)      # cleared with the old summary
        assert params["prompt_used"] == {"prompt": "Mine for {{client}}", "source": "custom", "name": None}
        sent = _run(world, params)
        assert "Mine for Acme" in sent and "Focus on dates" in sent
        assert _stored(world) == ("Mine for {{client}}", "custom", None, world["owner"])


def test_a_resolved_prompt_sent_unchanged_keeps_its_source(world):
    with app.app_context():
        _attach(world)
        params = _queue(world, "owner", {"custom_prompt": "OWNER DEFAULT\r\n", "prompt_mode": "replace"})
        _run(world, params)
        assert _stored(world) == ("OWNER DEFAULT", "user", None, world["owner"])


def test_added_instructions_alone_store_the_resolved_prompt(world):
    with app.app_context():
        _attach(world, "owner")
        params = _queue(world, "owner", {"additional_instructions": "Focus on dates"})
        assert params["prompt_used"] is None and params["custom_prompt_append"] is True
        sent = _run(world, params)
        assert "OWNER TAG" in sent and "Focus on dates" in sent
        prompt, source, _name, user_id = _stored(world)
        assert (prompt, source, user_id) == ("OWNER TAG", "tag", world["owner"])


def test_an_editors_run_stores_the_editors_prompt_and_id(world):
    with app.app_context():
        _attach(world, "owner")
        params = _queue(world, "editor", {"additional_instructions": "Focus on dates"})
        sent = _run(world, params)
        assert "EDITOR DEFAULT" in sent and "Focus on dates" in sent and "OWNER TAG" not in sent
        assert _stored(world) == ("EDITOR DEFAULT", "user", None, world["editor"])


def test_generate_summary_stores_the_prompt(world):
    with app.app_context():
        rec = _attach(world)
        rec.summary = None
        db.session.commit()
        params = _queue(world, "owner", {"custom_prompt": "First try", "additional_instructions": "Agenda"},
                        route="generate_summary")
        assert params["prompt_used"] == {"prompt": "First try", "source": "custom", "name": None}
        _run(world, params)
        assert _stored(world) == ("First try", "custom", None, world["owner"])


def _v1(ids, who, method, path, body):
    from src.models import APIToken
    plain = _v1_token(ids[who])
    try:
        with patch("src.services.llm.client", MagicMock()), \
                patch.object(job_queue, "enqueue", return_value=1) as enq:
            with app.test_client() as c:
                resp = getattr(c, method)(f"/api/v1/recordings/{ids['rec']}{path}", json=body,
                                          headers={"Authorization": f"Bearer {plain}"})
        return resp, (enq.call_args.kwargs["params"] if enq.call_args else None)
    finally:
        APIToken.query.filter_by(user_id=ids[who], name="rg-v1").delete()
        db.session.commit()


def test_v1_summarize_stores_the_prompt(world):
    with app.app_context():
        _attach(world)
        resp, params = _v1(world, "owner", "post", "/summarize", {"custom_prompt": "Only the decisions"})
        assert resp.status_code == 200
        _run(world, params)
        assert _stored(world) == ("Only the decisions", "custom", None, world["owner"])
        # A prompt sent to append is one-run text: the resolved prompt is stored.
        resp, params = _v1(world, "owner", "post", "/summarize",
                           {"custom_prompt": "Agenda: budget", "prompt_mode": "append"})
        assert resp.status_code == 200 and params["prompt_used"] is None
        sent = _run(world, params)
        assert "Agenda: budget" in sent
        assert _stored(world) == ("OWNER DEFAULT", "user", None, world["owner"])


def test_bulk_reprocess_stores_the_prompt(world):
    with app.app_context():
        _attach(world, "owner")
        _store(world)
        with patch("src.services.llm.client", MagicMock()), \
                patch.object(job_queue, "enqueue", return_value=1) as enq:
            resp = _client(_user(world, "owner")).post(
                "/api/recordings/bulk-reprocess", json={"recording_ids": [world["rec"]], "type": "summary"})
        assert resp.status_code == 200 and enq.call_count == 1
        assert _stored(world) == (None, None, None, None)
        _run(world, enq.call_args.kwargs["params"])
        prompt, source, _name, user_id = _stored(world)
        assert (prompt, source, user_id) == ("OWNER TAG", "tag", world["owner"])


# --- Visibility -----------------------------------------------------------------

def test_only_the_user_whose_run_it_was_gets_the_used_prompt(world):
    with app.app_context():
        _attach(world)
        _run(world, _queue(world, "editor", {"custom_prompt": "EDITOR SECRET", "prompt_mode": "replace"}))
        assert _stored(world) == ("EDITOR SECRET", "custom", None, world["editor"])

        used = _prompt_info(world, "editor")["used"]
        assert used == {"key": "used", "kind": "used", "source": "custom", "name": None, "id": None,
                        "prompt": "EDITOR SECRET", "applies": True,
                        "save_target": {"kind": "user", "id": None, "name": None}}
        for other in ("owner", "member"):
            info = _prompt_info(world, other)
            assert info["used"] is None
            assert "EDITOR SECRET" not in str(info)

        # The owner's own run replaces it; the editor no longer gets one.
        _run(world, _queue(world, "owner", {"custom_prompt": "OWNER SECRET", "prompt_mode": "replace"}))
        assert _prompt_info(world, "owner")["used"]["prompt"] == "OWNER SECRET"
        info = _prompt_info(world, "editor")
        assert info["used"] is None and "OWNER SECRET" not in str(info)


def test_the_owners_prompt_run_for_an_editor_is_stored_as_the_owners(world):
    # A summary after new speaker names is queued with the editor's id and no
    # prompt: the task runs the owner's prompt, which the editor must not get.
    with app.app_context():
        _attach(world, "owner")
        sent = _run(world, {"user_id": world["editor"]})
        assert "OWNER TAG" in sent
        prompt, source, _name, user_id = _stored(world)
        assert (prompt, source, user_id) == ("OWNER TAG", "tag", world["owner"])
        info = _prompt_info(world, "editor")
        assert info["used"] is None and "OWNER TAG" not in str(info)
        assert _prompt_info(world, "owner")["used"]["prompt"] == "OWNER TAG"


def test_the_used_entry_names_the_source_of_a_resolved_prompt(world):
    with app.app_context():
        rec = _attach(world, "owner")
        _run(world)
        db.session.expire_all()
        rec = db.session.get(Recording, world["rec"])
        used = describe_summary_prompt(rec, _user(world, "owner"))["used"]
        assert (used["source"], used["name"], used["prompt"]) == ("tag", rec.tags[0].name, "OWNER TAG")


def test_older_summaries_have_no_used_entry(world):
    with app.app_context():
        _attach(world)
        assert _stored(world) == (None, None, None, None)
        assert _prompt_info(world, "owner")["used"] is None


def test_the_prompt_is_not_part_of_the_recording_payloads(world):
    with app.app_context():
        _attach(world)
        _run(world, _queue(world, "owner", {"custom_prompt": "OWNER SECRET", "prompt_mode": "replace"}))
        rec = db.session.get(Recording, world["rec"])
        for payload in (rec.to_dict(viewer_user=_user(world, "editor")), rec.to_list_dict(viewer_user=_user(world, "editor"))):
            assert "OWNER SECRET" not in str(payload)
            assert not any(key.startswith("summary_prompt") for key in payload)
        resp, _params = _v1(world, "editor", "get", "", None)
        assert resp.status_code == 200 and "OWNER SECRET" not in resp.get_data(as_text=True)
        resp, _params = _v1(world, "owner", "get", "", None)
        assert resp.status_code == 200 and "OWNER SECRET" not in resp.get_data(as_text=True)


# --- Clearing -------------------------------------------------------------------

def test_a_failed_summary_has_no_stored_prompt(world):
    from src.services.error_classification import SummaryStepFailed
    with app.app_context():
        _attach(world)
        _store(world)
        with pytest.raises(SummaryStepFailed):
            _run(world, error=RuntimeError("provider down"))
        assert _stored(world) == (None, None, None, None)


def test_a_skipped_summary_has_no_stored_prompt(world):
    with app.app_context():
        rec = _attach(world)
        rec.transcription = "short"
        db.session.commit()
        _store(world)
        _run(world)
        assert _stored(world) == (None, None, None, None)
        assert db.session.get(Recording, world["rec"]).summary.startswith("[Summary skipped")


def test_a_transcription_reprocess_clears_the_stored_prompt(world):
    from src.services.reprocessing import queue_transcription_reprocess
    with app.app_context():
        rec = _attach(world)
        rec.audio_path = "x.wav"
        db.session.commit()
        _store(world)
        storage = MagicMock()
        storage.exists.return_value = True
        with patch("src.services.storage.get_storage_service", return_value=storage), \
                patch.object(job_queue, "enqueue", return_value=1) as enq:
            queue_transcription_reprocess(rec, _user(world, "owner"))
        assert _stored(world) == (None, None, None, None)
        assert enq.call_args.kwargs["restore_state"]["summary_prompt_used"] == "STALE"


def test_a_canceled_reprocess_restores_the_stored_prompt(world):
    from src.services.job_cancel import _restore_snapshot, snapshot_for_reprocess
    with app.app_context():
        rec = _attach(world)
        _store(world, prompt="KEPT", source="custom", who="editor")
        state = snapshot_for_reprocess(rec, "summary")
        rec.summary = None
        rec.clear_summary_prompt_used()
        db.session.commit()
        _restore_snapshot(rec, state)
        db.session.commit()
        assert _stored(world) == ("KEPT", "custom", None, world["editor"])
        assert db.session.get(Recording, world["rec"]).summary == "Old summary"


def test_an_edited_summary_keeps_its_prompt_and_an_emptied_one_does_not(world):
    with app.app_context():
        _attach(world)
        _store(world, prompt="KEPT")
        owner = _user(world, "owner")
        resp = _client(owner).post("/save", json={"id": world["rec"], "summary": "Old summary, fixed"})
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        assert _stored(world)[0] == "KEPT"
        resp = _client(owner).post("/save", json={"id": world["rec"], "summary": ""})
        assert resp.status_code == 200
        assert _stored(world) == (None, None, None, None)


def test_v1_summary_writes(world):
    with app.app_context():
        _attach(world)
        _store(world, prompt="KEPT")
        resp, _params = _v1(world, "owner", "patch", "", {"summary": "Edited by hand"})
        assert resp.status_code == 200 and _stored(world)[0] == "KEPT"
        resp, _params = _v1(world, "owner", "patch", "", {"summary": ""})
        assert resp.status_code == 200 and _stored(world) == (None, None, None, None)
        _store(world, prompt="KEPT")
        resp, _params = _v1(world, "owner", "put", "/summary", {"summary": "Written elsewhere"})
        assert resp.status_code == 200 and _stored(world) == (None, None, None, None)


def test_deleting_an_account_removes_its_stored_prompts(world):
    with app.app_context():
        _attach(world)
        _store(world, prompt="EDITOR SECRET", who="editor")
        admin = User(username=f"rg_adm_{uuid.uuid4().hex[:6]}", email=f"adm{uuid.uuid4().hex[:6]}@local.test",
                     password="x", is_admin=True)
        db.session.add(admin)
        db.session.commit()
        try:
            resp = _client(admin).delete(f"/admin/users/{world['editor']}")
            assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
            assert _stored(world) == (None, None, None, None)
        finally:
            db.session.delete(db.session.get(User, admin.id))
            db.session.commit()


def test_the_columns_are_added_to_an_existing_database():
    from sqlalchemy import inspect
    with app.app_context():
        columns = {c["name"] for c in inspect(db.engine).get_columns("recording")}
        assert set(FIELDS) <= columns
    import inspect as pyinspect
    import src.init_db as init_db
    source = pyinspect.getsource(init_db)
    for field in FIELDS:
        assert f"'{field}'" in source


# --- The summary after new speaker names or an edited transcript ---------------

def _follow_up(ids, who):
    from src.services.reprocessing import follow_up_summary_params
    db.session.expire_all()
    return follow_up_summary_params(db.session.get(Recording, ids["rec"]), _user(ids, who))


def test_the_follow_up_summary_runs_the_users_custom_prompt_again(world):
    with app.app_context():
        _attach(world, "owner")
        _run(world, _queue(world, "owner", {"custom_prompt": "MY FORM for {{client}}", "prompt_mode": "replace",
                                            "additional_instructions": "ONE RUN ONLY"}))
        params = _follow_up(world, "owner")
        assert params["custom_prompt"] == "MY FORM for {{client}}" and params["custom_prompt_append"] is False
        sent = _run(world, params)
        assert "MY FORM" in sent and "OWNER TAG" not in sent and "ONE RUN ONLY" not in sent
        prompt, source, _name, user_id = _stored(world)
        assert (prompt, source, user_id) == ("MY FORM for {{client}}", "custom", world["owner"])
        assert _prompt_info(world, "owner")["used"]["prompt"] == "MY FORM for {{client}}"


def test_the_follow_up_summary_of_a_resolved_prompt_resolves_it_again(world):
    with app.app_context():
        _attach(world, "owner")
        _run(world)
        assert _stored(world)[1] == "tag"
        assert _follow_up(world, "owner") == {"user_id": world["owner"]}


def test_the_follow_up_summary_does_not_run_another_users_custom_prompt(world):
    with app.app_context():
        _attach(world, "owner")
        _run(world, _queue(world, "editor", {"custom_prompt": "EDITOR SECRET", "prompt_mode": "replace"}))
        assert _stored(world)[0] == "EDITOR SECRET"
        assert _follow_up(world, "owner") == {"user_id": world["owner"]}
        assert _follow_up(world, "editor")["custom_prompt"] == "EDITOR SECRET"


def test_the_follow_up_summary_without_a_summary_has_no_custom_prompt(world):
    with app.app_context():
        _attach(world, "owner")
        _store(world, "LEFTOVER", "custom", None, "owner")
        rec = db.session.get(Recording, world["rec"])
        rec.summary = ""
        db.session.commit()
        assert _follow_up(world, "owner") == {"user_id": world["owner"]}


def test_new_speaker_names_queue_the_summary_with_the_custom_prompt(world):
    from src.services import speaker_assignment
    with app.app_context():
        _attach(world, "owner")
        _run(world, _queue(world, "owner", {"custom_prompt": "MY FORM", "prompt_mode": "replace"}))
        rec = db.session.get(Recording, world["rec"])
        import src.api.recordings as recordings_module
        with patch.object(job_queue, "enqueue", return_value=1) as enq, \
                patch.object(recordings_module, "reindex_recording_chunks_async"), \
                patch("src.services.speaker.update_voice_profiles", return_value=(0, 0)):
            result = speaker_assignment.apply_speaker_names(rec, _user(world, "owner"), {}, True)
        assert result["summary_queued"] is True
        params = enq.call_args.kwargs["params"]
        assert params["custom_prompt"] == "MY FORM" and params["prompt_used"]["source"] == "custom"
