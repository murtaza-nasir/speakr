"""Fixes from the pre-release review of v0.10.7.

1. A shared editor's save trains their own voice profiles and leaves the
   owner's samples alone.
2. Renaming a person everywhere keeps the recording label maps in step, so
   the next save does not read the voice as removed.
3. Bulk delete, API v1 batch delete and merge go through the one complete
   delete (voice samples detached, snippets and jobs removed, webhook sent).
4. API v1 is_archived is per user and parsed strictly.
5. The vectorised threshold calibration matches the pairwise definition.

SHARED-DB: every test uses its own users and removes its recordings.
"""

import json
import os
import sys
import uuid
from unittest.mock import patch, MagicMock

import numpy as np
import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Speaker, SpeakerVoiceSample, SystemSetting, ProcessingJob
from src.models.sharing import InternalShare, SharedRecordingState
from src.services import voice_profiles as vp
from src.services.recording_state import parse_archived_flag

app.config["WTF_CSRF_ENABLED"] = False
RNG = np.random.default_rng(11)
DIM = 256
_created = []


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def person():
    return unit(RNG.normal(size=DIM))


def near(base, noise=0.15):
    return unit(base + noise * RNG.normal(size=DIM) / np.sqrt(DIM) * 4)


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def ctx():
    with app.app_context():
        saved = {k: SystemSetting.get_setting(k, None) for k in (vp.SETTING_CURRENT_SPACE, vp.SETTING_LEGACY_SPACE)}
        vp._calibration_cache.clear()
        yield
        db.session.rollback()
        for rec_id in _created:
            SpeakerVoiceSample.query.filter_by(recording_id=rec_id).update({'recording_id': None})
            InternalShare.query.filter_by(recording_id=rec_id).delete()
            SharedRecordingState.query.filter_by(recording_id=rec_id).delete()
            ProcessingJob.query.filter_by(recording_id=rec_id).delete()
            Recording.query.filter_by(id=rec_id).delete()
        db.session.commit()
        _created.clear()
        for k, v in saved.items():
            SystemSetting.set_setting(k, v or '', setting_type='string')
        vp._calibration_cache.clear()


def _user():
    s = uuid.uuid4().hex[:8]
    u = User(username=f"rf_{s}", email=f"rf_{s}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


def _recording(user, embeddings=None, names=None):
    embeddings = embeddings or {}
    segs, t = [], 0.0
    for label in embeddings or {"A": None}:
        segs.append({"speaker": (names or {}).get(label, label), "sentence": "words " * 5,
                     "start_time": t, "end_time": t + 60.0})
        t += 60.0
    rec = Recording(user_id=user.id, title=f"rf_{uuid.uuid4().hex[:8]}", status="COMPLETED",
                    audio_path="local://recordings/x.mp3", original_filename="x.mp3",
                    transcription=json.dumps(segs),
                    speaker_embeddings={k: [float(x) for x in v] for k, v in embeddings.items()} or None)
    db.session.add(rec)
    db.session.commit()
    _created.append(rec.id)
    return rec


def _client(user):
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _share(rec, owner, other, can_edit=True):
    db.session.add(InternalShare(recording_id=rec.id, owner_id=owner.id,
                                 shared_with_user_id=other.id, can_edit=can_edit))
    db.session.commit()


def _save_names(user, rec, mapping):
    body = {"speaker_map": {k: {"name": v} for k, v in mapping.items()}, "regenerate_summary": False}
    with patch("src.services.speaker_snippets.create_speaker_snippets", return_value=0):
        r = _client(user).post(f"/recording/{rec.id}/update_speakers", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    db.session.expire_all()


def _samples(user, rec):
    return {s.label: s for s in SpeakerVoiceSample.query.filter_by(user_id=user.id, recording_id=rec.id).all()}


# ------------------------------------------------ 1. shared editor's save

def test_shared_editors_save_leaves_the_owners_samples(ctx):
    owner, editor = _user(), _user()
    rec = _recording(owner, {"SPEAKER_00": person(), "SPEAKER_01": person()})
    _save_names(owner, rec, {"SPEAKER_00": "Alice", "SPEAKER_01": "Ben"})
    owners = _samples(owner, rec)
    assert set(owners) == {"SPEAKER_00", "SPEAKER_01"}
    alice_id = owners["SPEAKER_00"].speaker_id

    _share(rec, owner, editor)
    _save_names(editor, rec, {"Alice": "Alice Ng"})

    owners = _samples(owner, rec)
    assert set(owners) == {"SPEAKER_00", "SPEAKER_01"}
    assert owners["SPEAKER_00"].speaker_id == alice_id
    editors = _samples(editor, rec)
    assert set(editors) == {"SPEAKER_00"}
    assert db.session.get(Speaker, editors["SPEAKER_00"].speaker_id).user_id == editor.id


def test_sample_list_hides_titles_of_recordings_no_longer_shared(ctx):
    owner, editor = _user(), _user()
    rec = _recording(owner, {"SPEAKER_00": person()})
    _share(rec, owner, editor)
    _save_names(editor, rec, {"SPEAKER_00": "Dana"})
    speaker = Speaker.query.filter_by(user_id=editor.id, name="Dana").first()

    body = _client(editor).get(f"/speakers/{speaker.id}/voice_samples").get_json()
    assert body["samples"][0]["recording_title"] == rec.title

    InternalShare.query.filter_by(recording_id=rec.id).delete()
    db.session.commit()
    body = _client(editor).get(f"/speakers/{speaker.id}/voice_samples").get_json()
    assert body["samples"][0]["recording_title"] is None
    assert body["samples"][0]["recording_id"] is None


# --------------------------------------------------- 2. rename everywhere

def test_global_rename_keeps_the_sample_on_the_next_save(ctx):
    user = _user()
    rec = _recording(user, {"SPEAKER_00": person(), "SPEAKER_01": person()})
    _save_names(user, rec, {"SPEAKER_00": "Alice", "SPEAKER_01": "Ben"})
    alice = Speaker.query.filter_by(user_id=user.id, name="Alice").first()

    r = _client(user).put(f"/speakers/{alice.id}", json={"name": "Alicia"})
    assert r.status_code == 200, r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Recording, rec.id).speaker_label_map["SPEAKER_00"] == "Alicia"

    _save_names(user, db.session.get(Recording, rec.id), {"Ben": "Benjamin"})
    samples = _samples(user, rec)
    assert samples["SPEAKER_00"].speaker_id == alice.id


def test_stale_label_map_still_keeps_a_renamed_persons_sample(ctx):
    """A recording whose map was not rewritten (for example one only shared
    with the user) still counts the person's current name as present."""
    user = _user()
    rec = _recording(user, {"SPEAKER_00": person(), "SPEAKER_01": person()})
    _save_names(user, rec, {"SPEAKER_00": "Alice", "SPEAKER_01": "Ben"})
    alice = Speaker.query.filter_by(user_id=user.id, name="Alice").first()
    alice.name = "Alicia"
    rec = db.session.get(Recording, rec.id)
    segs = json.loads(rec.transcription)
    for s in segs:
        if s["speaker"] == "Alice":
            s["speaker"] = "Alicia"
    rec.transcription = json.dumps(segs)
    db.session.commit()

    _save_names(user, rec, {"Ben": "Benjamin"})
    assert "SPEAKER_00" in _samples(user, rec)


# ------------------------------------------------ 3. one complete delete

def _with_trail(user):
    rec = _recording(user, {"SPEAKER_00": person()})
    _save_names(user, rec, {"SPEAKER_00": "Erin"})
    db.session.add(ProcessingJob(user_id=user.id, recording_id=rec.id, job_type='transcribe', status='COMPLETED'))
    db.session.commit()
    sample_id = _samples(user, rec)["SPEAKER_00"].id
    return rec.id, sample_id


def _assert_completely_deleted(rec_id, sample_id, webhook):
    db.session.expire_all()
    assert db.session.get(Recording, rec_id) is None
    assert ProcessingJob.query.filter_by(recording_id=rec_id).count() == 0
    sample = db.session.get(SpeakerVoiceSample, sample_id)
    assert sample is not None and sample.recording_id is None
    assert any(c.kwargs.get('event_type') == 'recording.deleted' and c.kwargs['data']['recording_id'] == rec_id
               for c in webhook.call_args_list)


def test_bulk_delete_is_a_complete_delete(ctx):
    user = _user()
    rec_id, sample_id = _with_trail(user)
    storage = MagicMock()
    with patch("src.api.recordings.get_storage_service", return_value=storage), \
         patch("src.services.webhook_dispatch.emit_webhook_event") as webhook:
        r = _client(user).delete("/api/recordings/bulk", json={"recording_ids": [rec_id]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["deleted_ids"] == [rec_id]
    storage.delete.assert_called_once_with("local://recordings/x.mp3", missing_ok=True)
    _assert_completely_deleted(rec_id, sample_id, webhook)


def test_api_v1_batch_delete_is_a_complete_delete(ctx):
    user = _user()
    rec_id, sample_id = _with_trail(user)
    storage = MagicMock()
    with patch("src.services.storage.get_storage_service", return_value=storage), \
         patch("src.services.webhook_dispatch.emit_webhook_event") as webhook:
        r = _client(user).delete("/api/v1/recordings/batch", json={"recording_ids": [rec_id]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["deleted"] == 1
    storage.delete.assert_called_once_with("local://recordings/x.mp3", missing_ok=True)
    _assert_completely_deleted(rec_id, sample_id, webhook)


def test_merge_deletes_originals_through_the_complete_delete():
    from src.services import recording_merge
    assert not hasattr(recording_merge, "_delete_recording")
    assert recording_merge.delete_recording_completely.__module__ == "src.services.recording_deletion"


# ------------------------------------------------ 4. API v1 archive per user

@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), ("true", True), ("false", False), ("False", False),
    (1, True), (0, False), ("1", True), ("0", False), ("maybe", None), (None, None), (2, None), ([], None),
])
def test_parse_archived_flag(value, expected):
    assert parse_archived_flag(value) is expected


def test_api_v1_archive_by_a_shared_editor_is_personal(ctx):
    owner, editor = _user(), _user()
    rec = _recording(owner)
    _share(rec, owner, editor)

    r = _client(editor).patch(f"/api/v1/recordings/{rec.id}", json={"is_archived": True})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["recording"]["is_archived"] is True
    db.session.expire_all()
    assert not db.session.get(Recording, rec.id).is_archived

    assert _client(editor).get(f"/api/v1/recordings/{rec.id}").get_json()["is_archived"] is True
    assert _client(owner).get(f"/api/v1/recordings/{rec.id}").get_json()["is_archived"] is False


def test_api_v1_archive_parses_strings_and_rejects_junk(ctx):
    user = _user()
    rec = _recording(user)
    c = _client(user)
    c.patch(f"/api/v1/recordings/{rec.id}", json={"is_archived": True})
    r = c.patch(f"/api/v1/recordings/{rec.id}", json={"is_archived": "false"})
    assert r.status_code == 200 and r.get_json()["recording"]["is_archived"] is False
    assert c.patch(f"/api/v1/recordings/{rec.id}", json={"is_archived": "maybe"}).status_code == 400

    r = c.patch("/api/v1/recordings/batch", json={"recording_ids": [rec.id], "updates": {"is_archived": "yes please"}})
    assert r.status_code == 400
    r = c.patch("/api/v1/recordings/batch", json={"recording_ids": [rec.id], "updates": {"is_archived": "true"}})
    assert r.status_code == 200, r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Recording, rec.id).is_archived is True


def test_api_v1_batch_archive_by_a_shared_editor_is_personal(ctx):
    owner, editor = _user(), _user()
    rec = _recording(owner)
    _share(rec, owner, editor)
    r = _client(editor).patch("/api/v1/recordings/batch",
                              json={"recording_ids": [rec.id], "updates": {"is_archived": True}})
    assert r.status_code == 200, r.get_data(as_text=True)
    db.session.expire_all()
    assert not db.session.get(Recording, rec.id).is_archived
    state = SharedRecordingState.query.filter_by(recording_id=rec.id, user_id=editor.id).first()
    assert state is not None and state.is_archived


# ------------------------------------------------ 5. calibration

def test_vectorised_calibration_matches_the_pairwise_definition(ctx):
    user = _user()
    people = {name: person() for name in ("P1", "P2", "P3", "P4")}
    vectors = {}
    for i, (name, base) in enumerate(people.items()):
        sp = Speaker(user_id=user.id, name=f"{name}_{uuid.uuid4().hex[:6]}", use_count=1)
        db.session.add(sp)
        db.session.flush()
        for k in range(5):
            v = near(base, noise=0.3)
            vectors.setdefault(sp.id, []).append(v)
            db.session.add(SpeakerVoiceSample(user_id=user.id, speaker_id=sp.id, label=f"S{i}{k}",
                                              embedding=vp.to_bytes(v), dimension=DIM,
                                              space_id=None, source='confirmed', weight=1.0))
    db.session.commit()
    try:
        # Only this user's samples, so other tests' rows do not shift the result.
        mine = db.session.query(SpeakerVoiceSample).filter_by(user_id=user.id).all()
        with patch.object(SpeakerVoiceSample, "query") as q:
            q.all.return_value = mine
            got = vp.calibrated_threshold(None)

        genuine, impostor = [], []
        for pid, vecs in vectors.items():
            for i, v in enumerate(vecs):
                genuine.append(max(float(np.dot(v, w)) for j, w in enumerate(vecs) if j != i))
                impostor.append(max(float(np.dot(v, w)) for qid, ws in vectors.items() if qid != pid for w in ws))
        want = round(min(0.85, max(0.45, (np.percentile(genuine, 10) + np.percentile(impostor, 95)) / 2.0)), 3)
        assert got is not None
        assert got == pytest.approx(want, abs=1e-3)
    finally:
        db.session.query(SpeakerVoiceSample).filter_by(user_id=user.id).delete()
        db.session.commit()
