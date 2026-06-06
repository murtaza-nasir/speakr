"""Regression tests for keep_audio_only API serialization and the
PATCH-immutability invariant.

The column was added without being exposed in any to_dict() or v1 list
builder, so external integrations and the frontend had no way to know
whether a given recording was audio-only. PATCH also accepted mutation
attempts silently. These tests pin the fix.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording

app.config["WTF_CSRF_ENABLED"] = False


def _setup_user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _make_recording(user_id, keep_audio_only=False):
    rec = Recording(
        user_id=user_id,
        title='r',
        audio_path='/tmp/x.mp3',
        status='COMPLETED',
        keep_audio_only=keep_audio_only,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def test_v1_list_includes_keep_audio_only():
    with app.app_context():
        user = _setup_user('keep_list')
        rec_audio = _make_recording(user.id, keep_audio_only=True)
        rec_video = _make_recording(user.id, keep_audio_only=False)

        client = app.test_client()
        _login(client, user)
        resp = client.get('/api/v1/recordings')
        assert resp.status_code == 200
        body = resp.get_json()
        flag_by_id = {r['id']: r.get('keep_audio_only') for r in body['recordings']}
        assert flag_by_id.get(rec_audio.id) is True
        assert flag_by_id.get(rec_video.id) is False

        for r in [rec_audio, rec_video]:
            db.session.delete(r)
        db.session.delete(user)
        db.session.commit()


def test_v1_detail_includes_keep_audio_only():
    with app.app_context():
        user = _setup_user('keep_detail')
        rec = _make_recording(user.id, keep_audio_only=True)
        client = app.test_client()
        _login(client, user)
        resp = client.get(f'/api/v1/recordings/{rec.id}')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get('keep_audio_only') is True

        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()


def test_patch_rejects_mutating_keep_audio_only():
    """The flag dictates how the file was already processed. Allowing
    PATCH to flip it would create a misleading record."""
    with app.app_context():
        user = _setup_user('keep_immut')
        rec = _make_recording(user.id, keep_audio_only=False)
        client = app.test_client()
        _login(client, user)
        resp = client.patch(
            f'/api/v1/recordings/{rec.id}',
            json={'keep_audio_only': True},
        )
        assert resp.status_code == 400, resp.data

        # And verify the DB value did not change.
        db.session.expire_all()
        refetched = db.session.get(Recording, rec.id)
        assert refetched.keep_audio_only is False

        db.session.delete(refetched)
        db.session.delete(user)
        db.session.commit()


def test_patch_other_fields_still_works_alongside_rejection():
    """Sending other valid fields in the same body as a rejected
    keep_audio_only attempt fails the whole request (atomic). Without a
    keep_audio_only key, normal fields update."""
    with app.app_context():
        user = _setup_user('keep_other')
        rec = _make_recording(user.id, keep_audio_only=False)
        client = app.test_client()
        _login(client, user)

        # With keep_audio_only present -> 400, title unchanged.
        resp = client.patch(
            f'/api/v1/recordings/{rec.id}',
            json={'title': 'NEW', 'keep_audio_only': True},
        )
        assert resp.status_code == 400
        db.session.expire_all()
        assert db.session.get(Recording, rec.id).title == 'r'

        # Without it -> 200, title updates.
        resp = client.patch(
            f'/api/v1/recordings/{rec.id}',
            json={'title': 'NEW'},
        )
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(Recording, rec.id).title == 'NEW'

        db.session.delete(db.session.get(Recording, rec.id))
        db.session.delete(user)
        db.session.commit()
