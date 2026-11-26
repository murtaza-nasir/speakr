"""One get_accessible_recording_ids for the web app, search and Inquire (#709).

The helper was defined twice, in src/api/recordings.py and in
src/services/embeddings.py. Both copies now name the function of
src/services/recording_scope.py. These tests compare it with the deleted
body, which read the internal sharing setting through the module of each
copy, for manual, group tag and group folder shares, for shared recordings
that are not completed, and with internal sharing on and off in System
Settings.

SHARED-DB: users, recordings, shares and the setting row are restored.
"""

import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import InternalShare, Recording, SystemSetting, User
import src.api.recordings as recordings_api
import src.services.embeddings as embeddings
import src.services.recording_scope as recording_scope

KEY = "enable_internal_sharing"


def _deleted_copy(module, user_id):
    """The body both modules had before #709, with its setting read through module."""
    accessible_ids = set()
    own_recordings = db.session.query(Recording.id).filter_by(user_id=user_id).all()
    accessible_ids.update([r.id for r in own_recordings])
    if module._self.ENABLE_INTERNAL_SHARING:
        shared_recordings = db.session.query(InternalShare.recording_id).filter_by(
            shared_with_user_id=user_id
        ).all()
        accessible_ids.update([r.recording_id for r in shared_recordings])
    return list(accessible_ids)


@pytest.fixture
def world():
    with app.app_context():
        row = SystemSetting.query.filter_by(key=KEY).first()
        saved = (row is not None, row.value if row else None, row.setting_type if row else None)
        s = secrets.token_hex(4)
        me = User(username=f"ar_{s}", email=f"ar_{s}@local.test", password="x")
        ann = User(username=f"arann_{s}", email=f"arann_{s}@local.test", password="x")
        bob = User(username=f"arbob_{s}", email=f"arbob_{s}@local.test", password="x")
        db.session.add_all([me, ann, bob])
        db.session.commit()

        def rec(owner, title, status="COMPLETED"):
            r = Recording(user_id=owner.id, title=title, status=status, original_filename="a.wav")
            db.session.add(r)
            db.session.commit()
            return r.id

        ids = {
            "me": me.id, "ann": ann.id, "bob": bob.id,
            "mine": rec(me, "Mine"),
            "mine_queued": rec(me, "Mine queued", status="QUEUED"),
            "manual": rec(ann, "Manual share"),
            "group_tag": rec(ann, "Group tag share"),
            "group_folder": rec(ann, "Group folder share"),
            "processing": rec(ann, "Shared while processing", status="PROCESSING"),
            "ann_only": rec(ann, "Ann only"),
            "to_bob": rec(ann, "Shared with Bob"),
        }
        for key, source in (("manual", "manual"), ("group_tag", "group_tag"),
                            ("group_folder", "group_folder"), ("processing", "manual")):
            db.session.add(InternalShare(recording_id=ids[key], owner_id=ann.id, shared_with_user_id=me.id,
                                         source_type=source))
        db.session.add(InternalShare(recording_id=ids["to_bob"], owner_id=ann.id, shared_with_user_id=bob.id,
                                     source_type="manual"))
        db.session.commit()
    yield ids
    with app.app_context():
        db.session.rollback()
        users = [ids["me"], ids["ann"], ids["bob"]]
        recs = [r.id for r in Recording.query.filter(Recording.user_id.in_(users))]
        InternalShare.query.filter(InternalShare.recording_id.in_(recs)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(recs)).delete(synchronize_session=False)
        User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
        existed, value, setting_type = saved
        row = SystemSetting.query.filter_by(key=KEY).first()
        if existed:
            SystemSetting.set_setting(KEY, value, setting_type=setting_type)
        elif row is not None:
            db.session.delete(row)
        db.session.commit()


def test_every_module_names_the_one_function():
    fn = recording_scope.get_accessible_recording_ids
    assert recordings_api.get_accessible_recording_ids is fn
    assert embeddings.get_accessible_recording_ids is fn
    from src.api import inquire
    from src.services import inquire_agent
    assert inquire.get_accessible_recording_ids is fn
    assert inquire_agent.get_accessible_recording_ids is fn


@pytest.mark.parametrize("sharing", ["true", "false"])
def test_the_ids_equal_those_of_both_deleted_copies(world, sharing):
    with app.app_context():
        SystemSetting.set_setting(KEY, sharing, setting_type="boolean")
        for who in ("me", "ann", "bob"):
            got = set(recording_scope.get_accessible_recording_ids(world[who]))
            assert got == set(_deleted_copy(recordings_api, world[who]))
            assert got == set(_deleted_copy(embeddings, world[who]))


def test_with_sharing_on_every_kind_of_share_counts(world):
    with app.app_context():
        SystemSetting.set_setting(KEY, "true", setting_type="boolean")
        got = set(recording_scope.get_accessible_recording_ids(world["me"]))
    # Unlike scope_condition, shares of recordings that are not completed count.
    assert got == {world[k] for k in ("mine", "mine_queued", "manual", "group_tag", "group_folder", "processing")}


def test_with_sharing_off_only_own_recordings_count(world):
    with app.app_context():
        SystemSetting.set_setting(KEY, "false", setting_type="boolean")
        got = set(recording_scope.get_accessible_recording_ids(world["me"]))
        bob = set(recording_scope.get_accessible_recording_ids(world["bob"]))
    assert got == {world["mine"], world["mine_queued"]}
    assert bob == set()


def test_the_setting_applies_without_a_restart(world):
    with app.app_context():
        SystemSetting.set_setting(KEY, "false", setting_type="boolean")
        before = set(recording_scope.get_accessible_recording_ids(world["bob"]))
        SystemSetting.set_setting(KEY, "true", setting_type="boolean")
        after = set(recording_scope.get_accessible_recording_ids(world["bob"]))
    assert before == set()
    assert after == {world["to_bob"]}
