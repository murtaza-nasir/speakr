#!/usr/bin/env python3
"""
#314: a recipient of a shared recording must only see the tag/folder that
GRANTED access (the share-reason), flagged foreign — never the owner's other
tags/folder. Owners see their own metadata unchanged.

Run:
  docker run --rm -v $PWD:/app:ro -e UPLOAD_FOLDER=/tmp/up \
    -e ASR_BASE_URL=http://x:9999 speakr-test:cov \
    sh -c "cd /app && python -m pytest tests/test_share_reason_visibility.py -q"
"""

import uuid

import pytest

from src.app import app, db
from src.models import (
    User, Recording, Folder, Group, GroupMembership, Tag, RecordingTag,
)
from src.models.sharing import InternalShare


def _u():
    s = uuid.uuid4().hex[:8]
    u = User(username=f"u_{s}", email=f"{s}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def ctx():
    with app.app_context():
        yield


def _tag_recording(rec, tag):
    db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=0))
    db.session.commit()


def test_foreign_recording_shows_only_share_reason_tag(ctx):
    owner, viewer = _u(), _u()
    grp = Group(name=f"g_{uuid.uuid4().hex[:6]}")
    db.session.add(grp)
    db.session.commit()
    db.session.add(GroupMembership(group_id=grp.id, user_id=viewer.id, role='member'))
    db.session.add(GroupMembership(group_id=grp.id, user_id=owner.id, role='admin'))

    group_tag = Tag(name="ShareTag", user_id=owner.id, group_id=grp.id)
    personal_tag = Tag(name="OwnerPrivate", user_id=owner.id)
    personal_folder = Folder(name="OwnerFolder", user_id=owner.id)
    db.session.add_all([group_tag, personal_tag, personal_folder])
    db.session.commit()

    rec = Recording(user_id=owner.id, title="R", audio_path="local://r.mp3",
                    status="COMPLETED", folder_id=personal_folder.id)
    db.session.add(rec)
    db.session.commit()
    _tag_recording(rec, group_tag)
    _tag_recording(rec, personal_tag)

    db.session.add(InternalShare(
        recording_id=rec.id, owner_id=owner.id, shared_with_user_id=viewer.id,
        source_type='group_tag', source_tag_id=group_tag.id))
    db.session.commit()

    # Viewer (recipient): only the share-reason group tag, flagged foreign; no
    # personal tag, and no folder (the share was via a tag, not a folder).
    vd = rec.to_dict(viewer_user=viewer)
    assert [t['name'] for t in vd['tags']] == ["ShareTag"]
    assert vd['tags'][0]['is_foreign'] is True
    assert vd['folder'] is None and vd['folder_id'] is None

    # Owner: sees their own personal tag + group tag and their folder, no foreign flag.
    od = rec.to_dict(viewer_user=owner)
    names = sorted(t['name'] for t in od['tags'])
    assert names == ["OwnerPrivate", "ShareTag"]
    assert all('is_foreign' not in t for t in od['tags'])
    assert od['folder']['name'] == "OwnerFolder"


def test_foreign_recording_shows_only_share_reason_folder(ctx):
    owner, viewer = _u(), _u()
    grp = Group(name=f"g_{uuid.uuid4().hex[:6]}")
    db.session.add(grp)
    db.session.commit()
    db.session.add(GroupMembership(group_id=grp.id, user_id=viewer.id, role='member'))
    db.session.commit()

    group_folder = Folder(name="TeamFolder", user_id=owner.id, group_id=grp.id)
    db.session.add(group_folder)
    db.session.commit()

    rec = Recording(user_id=owner.id, title="R2", audio_path="local://r2.mp3",
                    status="COMPLETED", folder_id=group_folder.id)
    db.session.add(rec)
    db.session.commit()

    db.session.add(InternalShare(
        recording_id=rec.id, owner_id=owner.id, shared_with_user_id=viewer.id,
        source_type='group_folder', source_folder_id=group_folder.id))
    db.session.commit()

    vd = rec.to_dict(viewer_user=viewer)
    assert vd['folder']['name'] == "TeamFolder"
    assert vd['folder']['is_foreign'] is True
    assert vd['folder_id'] == group_folder.id
    assert vd['tags'] == []


def test_manual_share_hides_owner_tags_and_folder(ctx):
    owner, viewer = _u(), _u()
    personal_tag = Tag(name="Private", user_id=owner.id)
    personal_folder = Folder(name="Mine", user_id=owner.id)
    db.session.add_all([personal_tag, personal_folder])
    db.session.commit()
    rec = Recording(user_id=owner.id, title="R3", audio_path="local://r3.mp3",
                    status="COMPLETED", folder_id=personal_folder.id)
    db.session.add(rec)
    db.session.commit()
    _tag_recording(rec, personal_tag)
    db.session.add(InternalShare(
        recording_id=rec.id, owner_id=owner.id, shared_with_user_id=viewer.id,
        source_type='manual'))
    db.session.commit()

    # A manually shared recording exposes none of the owner's tags/folder.
    vd = rec.to_dict(viewer_user=viewer)
    assert vd['tags'] == []
    assert vd['folder'] is None and vd['folder_id'] is None
