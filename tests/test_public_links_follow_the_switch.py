"""Public links answer 404 while public sharing is off (privacy review A1).

enable_public_sharing (ENABLE_PUBLIC_SHARING) used to be checked only when a
link was created, so links made before the switch were still served. The
public page and the public audio now follow the same rule as creation: the
switch is on and the owner of the link has the permission to share publicly.
The link is kept, so it works again when the switch or the permission returns.

SHARED-DB: the users, the recording, the links and the setting are restored.
"""

import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, Share, SystemSetting, User
from tests.audit_log_support import unfreeze_hot_settings

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

KEY = 'enable_public_sharing'


@pytest.fixture
def world():
    unfreeze_hot_settings()
    with app.app_context():
        row = SystemSetting.query.filter_by(key=KEY).first()
        saved = (row.value, row.setting_type) if row else None
        SystemSetting.set_setting(KEY, 'true', setting_type='boolean')
        s = secrets.token_hex(4)
        owner = User(username=f"pl_{s}", email=f"pl_{s}@local.test", password="x", can_share_publicly=True)
        db.session.add(owner)
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Link switch", status="COMPLETED", original_filename="l.wav",
                        transcription="hello")
        db.session.add(rec)
        db.session.commit()
        share = Share(recording_id=rec.id, user_id=owner.id)
        db.session.add(share)
        db.session.commit()
        ids = dict(owner=owner.id, rec=rec.id, share=share.id, public_id=share.public_id)
    yield ids
    with app.app_context():
        db.session.rollback()
        Share.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter_by(id=ids["rec"]).delete()
        User.query.filter_by(id=ids["owner"]).delete()
        db.session.commit()
        if saved is None:
            row = SystemSetting.query.filter_by(key=KEY).first()
            if row:
                db.session.delete(row)
                db.session.commit()
        else:
            SystemSetting.set_setting(KEY, saved[0], None, saved[1])


def _switch(value):
    with app.app_context():
        SystemSetting.set_setting(KEY, value, setting_type='boolean')


def _permission(world, value):
    with app.app_context():
        db.session.get(User, world["owner"]).can_share_publicly = value
        db.session.commit()


def _statuses(world):
    with app.test_client() as c:
        page = c.get(f"/share/{world['public_id']}").status_code
        audio = c.get(f"/share/audio/{world['public_id']}").status_code
    return page, audio


def _owner_client(world):
    c = app.test_client()
    with c.session_transaction() as s:
        s['_user_id'] = str(world["owner"])
        s['_fresh'] = True
    return c


def test_link_is_served_while_sharing_is_on(world):
    page, audio = _statuses(world)
    assert page == 200
    # The recording has no audio file; the route was reached and answered for the link.
    assert audio == 404


def test_page_and_audio_answer_404_while_the_switch_is_off_and_return_with_it(world):
    _switch('false')
    try:
        assert _statuses(world) == (404, 404)
        with app.test_client() as c:
            body = c.get(f"/share/{world['public_id']}").get_data(as_text=True)
        assert "Link switch" not in body and "hello" not in body
        with app.app_context():
            assert db.session.get(Share, world["share"]) is not None, "the link is kept"
    finally:
        _switch('true')
    assert _statuses(world)[0] == 200


def test_env_value_applies_while_no_value_is_saved(world, monkeypatch):
    with app.app_context():
        SystemSetting.set_setting(KEY, None, setting_type='boolean')
    monkeypatch.setenv('ENABLE_PUBLIC_SHARING', 'false')
    assert _statuses(world)[0] == 404
    monkeypatch.setenv('ENABLE_PUBLIC_SHARING', 'true')
    assert _statuses(world)[0] == 200


def test_link_of_an_owner_without_the_permission_answers_404_and_returns_with_it(world):
    _permission(world, False)
    assert _statuses(world) == (404, 404)
    with app.app_context():
        assert db.session.get(Share, world["share"]) is not None
    _permission(world, True)
    assert _statuses(world)[0] == 200


def test_audio_of_an_inactive_link_is_not_served(world, tmp_path):
    audio = tmp_path / "l.wav"
    audio.write_bytes(b"RIFF0000WAVE")
    with app.app_context():
        rec = db.session.get(Recording, world["rec"])
        rec.audio_path = str(audio)
        rec.mime_type = "audio/wav"
        db.session.commit()
    with app.test_client() as c:
        assert c.get(f"/share/audio/{world['public_id']}").status_code == 200
    _switch('false')
    try:
        with app.test_client() as c:
            resp = c.get(f"/share/audio/{world['public_id']}")
        assert resp.status_code == 404 and b"RIFF" not in resp.data
    finally:
        _switch('true')


def test_owner_lists_show_the_link_as_inactive(world):
    c = _owner_client(world)
    assert [s["active"] for s in c.get("/api/shares").get_json()] == [True]
    assert [s["active"] for s in c.get("/api/shares/overview").get_json()["public_links"]] == [True]
    _switch('false')
    try:
        assert [s["active"] for s in c.get("/api/shares").get_json()] == [False]
        overview = c.get("/api/shares/overview").get_json()
        assert overview["public_sharing_enabled"] is False
        assert [s["active"] for s in overview["public_links"]] == [False]
    finally:
        _switch('true')
    _permission(world, False)
    assert [s["active"] for s in c.get("/api/shares").get_json()] == [False]


def test_api_v1_share_has_the_active_field(world):
    from src.services.public_shares import share_dict
    with app.app_context():
        assert share_dict(db.session.get(Share, world["share"]))["active"] is True
    _permission(world, False)
    with app.app_context():
        assert share_dict(db.session.get(Share, world["share"]))["active"] is False


def _public_answers(c):
    """What the share dialog, the Shared Transcripts list and API v1 say about public links."""
    dialog = c.get("/api/permissions/can-share-publicly").get_json()["can_share_publicly"]
    overview = c.get("/api/shares/overview").get_json()
    features = c.get("/api/v1/capabilities").get_json()["features"]
    return (dialog, overview["can_share_publicly"], features["can_share_publicly"],
            overview["public_sharing_enabled"], features["public_sharing"])


def test_the_share_dialog_the_list_and_api_v1_agree_on_public_links(world):
    """#685: the share dialog reads one answer, and the list and API v1 give the same one."""
    c = _owner_client(world)
    assert _public_answers(c) == (True, True, True, True, True)
    _switch('false')
    try:
        assert _public_answers(c) == (False, False, False, False, False)
    finally:
        _switch('true')
    _permission(world, False)
    assert _public_answers(c) == (False, False, False, True, True)


def test_the_env_value_applies_to_every_answer_while_no_value_is_saved(world, monkeypatch):
    with app.app_context():
        SystemSetting.query.filter_by(key=KEY).delete()
        db.session.commit()
    c = _owner_client(world)
    for raw, expected in (('false', False), (' TRUE ', True), ('1', False)):
        monkeypatch.setenv('ENABLE_PUBLIC_SHARING', raw)
        answers = _public_answers(c)
        assert answers == (expected,) * 5, (raw, answers)
