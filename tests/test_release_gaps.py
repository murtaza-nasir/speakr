"""Branches of the v0.10.7 changes that the feature tests did not reach.

Found by diff coverage against v0.10.6-alpha: the variant cap, the singleton
penalty, ambiguity and calibration in auto-labelling, sample removal when a
re-save leaves too little speech, refusal paths of the new endpoints, and the
failure handling of the shared deletion routine.

SHARED-DB: every test uses its own users and cleans the rows it creates
where later files could see them.
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
from src.models import (User, Recording, Speaker, SpeakerVoiceSample, SystemSetting, APIToken, Tag, Folder)
from src.services import voice_profiles as vp
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False
RNG = np.random.default_rng(11)


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def person(dim=64):
    return unit(RNG.normal(size=dim))


def near(base, noise=0.05):
    return unit(base + noise * RNG.normal(size=base.shape[0]) / np.sqrt(base.shape[0]) * 4)


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


_created = []


@pytest.fixture
def ctx():
    with app.app_context():
        vp._calibration_cache.clear()
        yield
        db.session.rollback()
        for rid in _created:
            SpeakerVoiceSample.query.filter_by(recording_id=rid).delete()
            Recording.query.filter_by(id=rid).delete()
        db.session.commit()
        _created.clear()
        vp._calibration_cache.clear()


def _user(**kw):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"gap_{s}", email=f"gap_{s}@local.test", password="x", **kw)
    db.session.add(u)
    db.session.commit()
    return u


def _rec(user, embeddings=None, status="COMPLETED", seconds=60.0, transcription=None):
    embeddings = embeddings or {}
    if transcription is None:
        segs, t = [], 0.0
        for label in embeddings or {"SPEAKER_00": None}:
            segs.append({"speaker": label, "sentence": "words", "start_time": t, "end_time": t + seconds})
            t += seconds
        transcription = json.dumps(segs)
    r = Recording(user_id=user.id, title="g", status=status, audio_path="local://g.mp3", original_filename="g.mp3",
                  transcription=transcription,
                  speaker_embeddings={k: [float(x) for x in v] for k, v in embeddings.items()} or None)
    db.session.add(r)
    db.session.commit()
    _created.append(r.id)
    return r


def _client(user):
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _token(user):
    raw = f"tok_{uuid.uuid4().hex}"
    db.session.add(APIToken(user_id=user.id, token_hash=hash_token(raw), name="t"))
    db.session.commit()
    return {"Authorization": f"Bearer {raw}"}


# ------------------------------------------------------- voice_profiles

def test_variant_count_is_capped_by_merging_the_closest():
    samples = [vp._Sample(person(), 1.0) for _ in range(vp.MAX_VARIANTS + 2)]
    variants = vp.build_variants(samples)
    assert len(variants) == vp.MAX_VARIANTS
    assert sum(v["count"] for v in variants) == len(samples)
    assert all(np.isclose(np.linalg.norm(v["centroid"]), 1.0) for v in variants)


def test_single_sample_variant_counts_a_little_less():
    a, b = person(), person()
    samples = [vp._Sample(near(a), 1.0), vp._Sample(near(a), 1.0), vp._Sample(b, 1.0)]
    variants = vp.build_variants(samples)
    score = vp.score_against(b, variants, len(samples))
    assert score == pytest.approx(1.0 - vp.SINGLETON_PENALTY, abs=1e-4)
    # A person with a single sample is not penalised.
    assert vp.score_against(b, vp.build_variants([vp._Sample(b, 1.0)]), 1) == pytest.approx(1.0, abs=1e-4)


def test_nearly_tied_candidates_leave_the_label_alone(ctx):
    user = _user(auto_speaker_labelling=True)
    voice = person()
    for name in ("Twin A", "Twin B"):
        sp = Speaker(user_id=user.id, name=name, average_embedding=vp.to_bytes(near(voice, 0.01)), embedding_count=1)
        db.session.add(sp)
    db.session.commit()
    rec = _rec(user, {"SPEAKER_00": near(voice, 0.01)})
    assert vp.auto_label_map(rec, user, 0.6) == {}


def test_calibrated_auto_label_thresholds_follow_the_setting():
    with patch.object(vp, "calibrated_threshold", return_value=0.7):
        assert vp.auto_label_threshold("low", None) == pytest.approx(0.6)
        assert vp.auto_label_threshold("medium", None) == pytest.approx(0.7)
        assert vp.auto_label_threshold("high", None) == pytest.approx(0.78)
        assert vp.auto_label_threshold(None, None) == pytest.approx(0.7)
    with patch.object(vp, "calibrated_threshold", return_value=0.9):
        assert vp.auto_label_threshold("high", None) == 0.95  # clamped
    with patch.object(vp, "calibrated_threshold", return_value=None):
        assert vp.auto_label_threshold("low", None) == vp.DEFAULT_AUTO_LABEL_THRESHOLDS["low"]


def test_resave_with_too_little_speech_removes_the_sample(ctx):
    user = _user()
    rec = _rec(user, {"SPEAKER_00": person()})
    sp = Speaker(user_id=user.id, name="Ana")
    db.session.add(sp)
    db.session.commit()
    stats = vp.apply_names_to_profiles(rec, {"SPEAKER_00": "Ana"}, {"SPEAKER_00": 60.0}, user)
    assert stats["stored"] == 1
    # Line edits moved most of the speech away; the label now has 4 seconds.
    stats = vp.apply_names_to_profiles(rec, {"SPEAKER_00": "Ana"}, {"SPEAKER_00": 4.0}, user)
    db.session.commit()
    assert stats["skipped_short"] == 1
    assert SpeakerVoiceSample.query.filter_by(recording_id=rec.id).count() == 0


def test_names_without_a_saved_speaker_train_nothing(ctx):
    user = _user()
    rec = _rec(user, {"SPEAKER_00": person()})
    stats = vp.apply_names_to_profiles(rec, {"SPEAKER_00": "Nobody Saved"}, None, user)
    assert stats["stored"] == 0
    assert db.session.get(Recording, rec.id).speaker_label_map == {"SPEAKER_00": "Nobody Saved"}


def test_invalid_vectors_and_stored_forms(ctx):
    user = _user()
    sp = Speaker(user_id=user.id, name="Ana")
    db.session.add(sp)
    db.session.commit()
    rec = _rec(user, {"SPEAKER_00": person()})
    assert vp.record_sample(sp, rec, "SPEAKER_00", [0.0, 0.0]) == "invalid"
    assert vp.find_matches([float("nan")], user.id) == []
    rec.speaker_embeddings = json.dumps({"SPEAKER_00": [1.0, 0.0]})
    assert list(vp.load_embeddings(rec)) == ["SPEAKER_00"]
    rec.speaker_embeddings = "not json"
    assert vp.load_embeddings(rec) == {}
    rec.speaker_embeddings = None
    assert vp.load_embeddings(rec) == {}


def test_outlier_replacing_an_existing_sample_removes_it(ctx):
    user = _user()
    ana = person()
    sp = Speaker(user_id=user.id, name="Ana")
    db.session.add(sp)
    db.session.commit()
    for _ in range(3):
        r = _rec(user, {"SPEAKER_00": near(ana)})
        vp.record_sample(sp, r, "SPEAKER_00", near(ana))
    target = _rec(user, {"SPEAKER_00": near(ana)})
    assert vp.record_sample(sp, target, "SPEAKER_00", near(ana)) == "stored"
    assert vp.record_sample(sp, target, "SPEAKER_00", -ana) == "rejected"
    db.session.commit()
    assert SpeakerVoiceSample.query.filter_by(recording_id=target.id).count() == 0


def test_suggestions_survive_an_unreadable_transcript(ctx):
    user = _user()
    rec = _rec(user, {"SPEAKER_00": person()}, transcription="not json")
    assert list(vp.suggestions_for_recording(rec, user.id)) == ["SPEAKER_00"]


# ------------------------------------------------------------------ API

def test_api_v1_delete_audio_refusals(ctx):
    owner, other = _user(), _user()
    done, busy = _rec(owner), _rec(owner, status="PROCESSING")
    c, h = _Client(app, app.response_class), _token(owner)
    assert c.post('/api/v1/recordings/99999999/delete-audio', headers=h).status_code == 404
    assert c.post(f'/api/v1/recordings/{done.id}/delete-audio', headers=_token(other)).status_code == 403
    assert c.post(f'/api/v1/recordings/{busy.id}/delete-audio', headers=h).status_code == 409
    with patch.dict(os.environ, {"USERS_CAN_DELETE": "false"}):
        assert c.post(f'/api/v1/recordings/{done.id}/delete-audio', headers=h).status_code == 403
    with patch("src.services.retention.get_storage_service", return_value=MagicMock()):
        assert c.post(f'/api/v1/recordings/{done.id}/delete-audio', headers=h).status_code == 200
        assert c.post(f'/api/v1/recordings/{done.id}/delete-audio', headers=h).status_code == 409


def test_api_v1_batch_archives_and_tag_title_prompt(ctx):
    user = _user()
    a, b = _rec(user), _rec(user)
    c, h = _Client(app, app.response_class), _token(user)
    r = c.patch('/api/v1/recordings/batch', headers=h,
                json={"recording_ids": [a.id, b.id], "updates": {"is_archived": True}})
    assert r.status_code == 200, r.get_data(as_text=True)
    db.session.expire_all()
    assert db.session.get(Recording, a.id).is_archived and db.session.get(Recording, b.id).is_archived
    tag = Tag(name=f"t_{uuid.uuid4().hex[:6]}", user_id=user.id)
    db.session.add(tag)
    db.session.commit()
    r = c.put(f'/api/v1/tags/{tag.id}', headers=h, json={"title_prompt": "- Name the court"})
    assert r.status_code == 200
    assert r.get_json()["tag"]["title_prompt"] == "- Name the court"


def test_web_refusals_for_the_new_routes(ctx):
    user = _user()
    c = _client(user)
    assert c.post('/recording/99999999/toggle_archive').status_code == 404
    assert c.post('/recording/99999999/delete_audio').status_code == 404
    assert c.post('/api/recordings/bulk-toggle', json={"recording_ids": [1], "field": "nope", "value": True}).status_code == 400


def test_folder_title_prompt_can_be_set_and_cleared(ctx):
    user = _user()
    folder = Folder(name=f"f_{uuid.uuid4().hex[:6]}", user_id=user.id)
    db.session.add(folder)
    db.session.commit()
    previous = SystemSetting.get_setting('enable_folders', False)
    SystemSetting.set_setting('enable_folders', 'true', setting_type='boolean')
    try:
        c = _client(user)
        assert c.put(f'/api/folders/{folder.id}', json={"title_prompt": "- Course code first"}).status_code == 200
        db.session.expire_all()
        assert db.session.get(Folder, folder.id).title_prompt == "- Course code first"
        c.put(f'/api/folders/{folder.id}', json={"title_prompt": ""})
        db.session.expire_all()
        assert db.session.get(Folder, folder.id).title_prompt is None
    finally:
        SystemSetting.set_setting('enable_folders', 'true' if previous else 'false', setting_type='boolean')


def test_speaker_endpoints_refuse_other_users_and_bulk_delete_takes_samples(ctx):
    owner, other = _user(), _user()
    sp = Speaker(user_id=owner.id, name="Ana")
    db.session.add(sp)
    db.session.commit()
    rec = _rec(owner, {"SPEAKER_00": person()})
    vp.record_sample(sp, rec, "SPEAKER_00", person())
    db.session.commit()
    sample_id = SpeakerVoiceSample.query.filter_by(speaker_id=sp.id).one().id
    assert _client(other).get(f'/speakers/{sp.id}/voice_samples').status_code == 404
    assert _client(owner).delete(f'/speakers/{sp.id}/voice_samples/99999999').status_code == 404
    assert _client(owner).delete('/speakers/delete_all').status_code == 200
    assert db.session.get(SpeakerVoiceSample, sample_id) is None


# ------------------------------------------------------------ deletion

def test_deletion_media_failure_is_tolerated_for_users_and_kept_for_retention(ctx):
    from src.services.recording_deletion import delete_recording_completely
    user = _user()
    failing = MagicMock()
    failing.delete.side_effect = RuntimeError("storage down")
    rec = _rec(user)
    with pytest.raises(RuntimeError):
        delete_recording_completely(rec, storage=failing, strict_media=True)
    db.session.rollback()
    assert db.session.get(Recording, rec.id) is not None
    delete_recording_completely(db.session.get(Recording, rec.id), storage=failing)
    assert db.session.get(Recording, rec.id) is None


def test_deletion_survives_webhook_and_export_failures(ctx):
    from src.services.recording_deletion import delete_recording_completely
    user = _user()
    rec = _rec(user)
    with patch("src.services.webhook_dispatch.emit_webhook_event", side_effect=RuntimeError("hook")), \
         patch("src.file_exporter.mark_export_as_deleted", side_effect=RuntimeError("export")):
        delete_recording_completely(rec, storage=MagicMock())
    assert db.session.get(Recording, rec.id) is None


# ------------------------------------------------------- canary / speaker

def test_space_bookkeeping_never_fails_the_check():
    from src.services import voice_embedding_check as check

    def boom(_):
        raise RuntimeError("db gone")

    assert check._spaces(boom) is None
    with app.app_context(), patch("src.models.VoiceEmbeddingSpace.query") as q:
        q.order_by.side_effect = RuntimeError("db gone")
        assert check.spaces_status() == []


def test_speaker_helpers_on_empty_and_failing_input(ctx):
    from src.services import speaker as sp_mod
    user = _user()
    assert sp_mod.find_user_speaker(user.id, "") is None
    assert sp_mod.find_user_speaker(user.id, "   ") is None
    rec = _rec(user, {"SPEAKER_00": person()})
    with patch("src.services.voice_profiles.apply_names_to_profiles", side_effect=RuntimeError("x")):
        assert sp_mod.update_voice_profiles(rec, {"SPEAKER_00": "Ana"}, user) == (0, 0)
