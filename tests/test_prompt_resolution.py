"""Summary prompt resolution and the owner's view of tags (#412 audit S3, S4, S5).

S5: the summary prompt order (tag > folder > user > admin > default) lives in
resolve_summary_instructions, next to the title resolver.
S3/S4: every processing step uses the tags the owner can see. Before, a
summary reprocessed by an editor used the editor's personal tag prompts, and
transcription settings and naming templates used every tag on the recording,
including another user's personal tag.

SHARED-DB: users, tags and the recording are removed afterwards.
"""

import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Folder, Recording, RecordingTag, SystemSetting, Tag, User
import src.tasks.processing as proc
from src.services.transcription_defaults import resolve_transcription_params


@pytest.fixture
def world():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        owner = User(username=f"own_{s}", email=f"own_{s}@local.test", password="x",
                     summary_prompt="OWNER PROMPT", transcription_language="en")
        editor = User(username=f"ed_{s}", email=f"ed_{s}@local.test", password="x")
        db.session.add_all([owner, editor])
        db.session.commit()
        own_tag = Tag(name=f"own-{s}", user_id=owner.id, custom_prompt="OWNER TAG PROMPT")
        ed_tag = Tag(name=f"ed-{s}", user_id=editor.id, custom_prompt="EDITOR TAG PROMPT", default_language="fr")
        folder = Folder(name=f"f-{s}", user_id=owner.id, custom_prompt="FOLDER PROMPT")
        db.session.add_all([own_tag, ed_tag, folder])
        db.session.commit()
        r = Recording(user_id=owner.id, title="Sync", original_filename="s.wav", status="COMPLETED",
                      transcription="Speaker 1: " + "we agreed on the plan. " * 5)
        db.session.add(r)
        db.session.commit()
        ids = dict(owner=owner.id, editor=editor.id, own_tag=own_tag.id, ed_tag=ed_tag.id,
                   folder=folder.id, rec=r.id)
    yield ids
    with app.app_context():
        db.session.rollback()
        RecordingTag.query.filter_by(recording_id=ids["rec"]).delete()
        for model, key in ((Recording, "rec"), (Tag, "own_tag"), (Tag, "ed_tag"), (Folder, "folder"),
                           (User, "editor"), (User, "owner")):
            obj = db.session.get(model, ids[key])
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def _attach(ids, *tag_keys, folder=False):
    for order, key in enumerate(tag_keys):
        db.session.add(RecordingTag(recording_id=ids["rec"], tag_id=ids[key], order=order))
    if folder:
        db.session.get(Recording, ids["rec"]).folder_id = ids["folder"]
    db.session.commit()
    return db.session.get(Recording, ids["rec"])


@pytest.mark.parametrize("tags,folder,expected", [
    (("own_tag",), True, ("OWNER TAG PROMPT", "tag")),
    ((), True, ("FOLDER PROMPT", "folder")),
    ((), False, ("OWNER PROMPT", "user")),
])
def test_summary_prompt_order(world, tags, folder, expected):
    with app.app_context():
        rec = _attach(world, *tags, folder=folder)
        assert proc.resolve_summary_instructions(rec) == expected


def test_an_editors_personal_tag_does_not_shape_the_summary(world):
    with app.app_context():
        _attach(world, "ed_tag")
        seen = []

        def fake(messages, **kw):
            seen.append("\n".join(m["content"] for m in messages))
            return MagicMock(choices=[MagicMock(message=MagicMock(content="## Summary", reasoning=None))])

        with patch.object(proc, "client", MagicMock()), patch.object(proc, "call_llm_completion", side_effect=fake), \
             patch.object(proc, "finish_processing"):
            proc.generate_summary_only_task(app.app_context(), world["rec"], user_id=world["editor"])
        assert "EDITOR TAG PROMPT" not in seen[0]
        assert "OWNER PROMPT" in seen[0]


def test_an_editors_personal_tag_does_not_change_transcription_settings(world):
    with app.app_context():
        rec = _attach(world, "ed_tag")
        assert resolve_transcription_params(rec)["language"] == "en"
