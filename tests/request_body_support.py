"""Shared fixture for the request body tests (#653-#659).

One owner with a recording, two tags, a speaker, a group the owner
administers and a full-scope API token, and one admin with a token. Every
row is removed afterwards.
"""

import json
import os
import secrets
import sys
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import (APIToken, ExportTemplate, Group, GroupMembership, InitialPromptTemplate,
                        NamingTemplate, Recording, RecordingTag, Speaker, Tag, TranscriptTemplate, User)
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False

SEGMENTS = [{"speaker": "SPEAKER_00", "sentence": "Hello there.", "start_time": 0.0, "end_time": 1.0}]

TEMPLATE_MODELS = (NamingTemplate, ExportTemplate, InitialPromptTemplate, TranscriptTemplate)


@contextmanager
def make_world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"rb_{s}", email=f"rb_{s}@local.test", password="x", ui_language="en")
        admin = User(username=f"rba_{s}", email=f"rba_{s}@local.test", password="x", is_admin=True)
        db.session.add_all([owner, admin])
        db.session.commit()
        group = Group(name=f"rb-team-{s}")
        db.session.add(group)
        db.session.commit()
        db.session.add(GroupMembership(group_id=group.id, user_id=owner.id, role="admin"))
        rec = Recording(user_id=owner.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                        audio_path="local://s.wav", transcription=json.dumps(SEGMENTS))
        a = Tag(name=f"a-{s}", user_id=owner.id)
        b = Tag(name=f"b-{s}", user_id=owner.id)
        speaker = Speaker(name=f"Dana {s}", user_id=owner.id)
        db.session.add_all([rec, a, b, speaker])
        db.session.commit()
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=a.id, order=1))
        tokens = {}
        for key, user in (("owner", owner), ("admin", admin)):
            plain = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=user.id, token_hash=hash_token(plain), name=key))
            tokens[key] = plain
        db.session.commit()
        ids = dict(owner=owner.id, admin=admin.id, group=group.id, rec=rec.id, a=a.id, b=b.id,
                   speaker=speaker.id, tokens=tokens, suffix=s)
    try:
        yield ids
    finally:
        with app.app_context():
            db.session.rollback()
            users = [ids["owner"], ids["admin"]]
            RecordingTag.query.filter_by(recording_id=ids["rec"]).delete()
            Recording.query.filter(Recording.user_id.in_(users)).delete(synchronize_session=False)
            Tag.query.filter(Tag.user_id.in_(users)).delete(synchronize_session=False)
            Tag.query.filter_by(group_id=ids["group"]).delete()
            Speaker.query.filter(Speaker.user_id.in_(users)).delete(synchronize_session=False)
            for model in TEMPLATE_MODELS:
                model.query.filter(model.user_id.in_(users)).delete(synchronize_session=False)
            GroupMembership.query.filter_by(group_id=ids["group"]).delete()
            Group.query.filter_by(id=ids["group"]).delete()
            APIToken.query.filter(APIToken.user_id.in_(users)).delete(synchronize_session=False)
            User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
            db.session.commit()


def headers(world, who="owner"):
    return {"Authorization": f"Bearer {world['tokens'][who]}"}


_UNSET = object()


def send(world, method, path, data=None, content_type="application/json", who="owner", json_body=_UNSET):
    """One request; `data` is the raw body, `json_body` a value to encode
    (None sends the JSON literal null)."""
    with app.test_client() as c:
        kwargs = {"headers": headers(world, who)}
        if json_body is not _UNSET:
            kwargs["data"] = json.dumps(json_body)
            kwargs["content_type"] = "application/json"
        else:
            if data is not None:
                kwargs["data"] = data
            if content_type is not None:
                kwargs["content_type"] = content_type
        return c.open(path, method=method, **kwargs)


def code_of(resp):
    assert resp.is_json, resp.get_data(as_text=True)[:200]
    return resp.get_json().get("code")
