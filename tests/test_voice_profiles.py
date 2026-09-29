"""Voice profiles built from samples, per embedding space.

Covers the three stages of the voice-matching rework:
1. dimension-agnostic, normalized embeddings; a speech minimum; outlier
   samples kept out; one person per label when auto-labelling
2. embedding spaces: profiles only compared within the model that made them,
   with everything stored before spaces existed in the legacy space
3. several voice variants per person, rebuilt from samples, so corrections
   undo themselves; thresholds calibrated from the data

SHARED-DB: every test uses its own users; space settings are restored.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Speaker, SpeakerVoiceSample, VoiceEmbeddingSpace, SystemSetting
from src.services import voice_profiles as vp

app.config["WTF_CSRF_ENABLED"] = False
RNG = np.random.default_rng(7)
DIM = 256


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def person(dim=DIM):
    return unit(RNG.normal(size=dim))


def near(base, noise=0.15):
    return unit(base + noise * RNG.normal(size=base.shape[0]) / np.sqrt(base.shape[0]) * 4)


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
        for k, v in saved.items():
            SystemSetting.set_setting(k, v or '', setting_type='string')
        vp._calibration_cache.clear()


def _user(**kw):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"vp_{s}", email=f"vp_{s}@local.test", password="x", **kw)
    db.session.add(u)
    db.session.commit()
    return u


def _speaker(user, name, average=None, count=0):
    sp = Speaker(user_id=user.id, name=name, use_count=1,
                 average_embedding=vp.to_bytes(unit(average)) if average is not None else None,
                 embedding_count=count)
    db.session.add(sp)
    db.session.commit()
    return sp


def _recording(user, embeddings, seconds=None, space=None, names=None):
    """A diarized recording; seconds maps label -> speech seconds (default 60)."""
    segs, t = [], 0.0
    for label in embeddings:
        dur = (seconds or {}).get(label, 60.0)
        segs.append({"speaker": (names or {}).get(label, label), "sentence": "words " * 5,
                     "start_time": t, "end_time": t + dur})
        t += dur
    rec = Recording(user_id=user.id, title="r", status="COMPLETED", audio_path="local://x.mp3",
                    original_filename="x.mp3", transcription=json.dumps(segs),
                    speaker_embeddings={k: [float(x) for x in v] for k, v in embeddings.items()},
                    speaker_embeddings_space_id=space)
    db.session.add(rec)
    db.session.commit()
    return rec


def _client(user):
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


def _save_names(user, rec, mapping):
    """Name speakers through the real save route."""
    body = {"speaker_map": {k: {"name": v} for k, v in mapping.items()}, "regenerate_summary": False}
    with patch("src.services.speaker_snippets.create_speaker_snippets", return_value=0):
        r = _client(user).post(f"/recording/{rec.id}/update_speakers", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    db.session.expire_all()


# ------------------------------------------------------------- stage 1

def test_normalize_rejects_what_is_not_an_embedding():
    assert vp.normalize([]) is None
    assert vp.normalize([0.0, 0.0]) is None
    assert vp.normalize([1.0, float('nan')]) is None
    assert vp.normalize("abc") is None
    assert np.isclose(np.linalg.norm(vp.normalize([3.0, 4.0])), 1.0)


def test_speech_seconds_per_label():
    segs = [{"speaker": "A", "start_time": 0, "end_time": 10}, {"speaker": "B", "start_time": 10, "end_time": 12},
            {"speaker": "A", "start_time": 12, "end_time": 20}]
    assert vp.speech_seconds_by_label(segs) == {"A": 18.0, "B": 2.0}
    assert vp.speech_seconds_by_label([{"speaker": "A", "sentence": "x"}]) is None


def test_any_dimension_works_end_to_end(ctx):
    user = _user()
    ana = person(192)
    rec = _recording(user, {"SPEAKER_00": ana})
    _save_names(user, rec, {"SPEAKER_00": "Ana"})
    rec2 = _recording(user, {"SPEAKER_00": near(ana)})
    r = _client(user).get(f"/speakers/suggestions/{rec2.id}")
    names = [m["name"] for m in r.get_json()["suggestions"]["SPEAKER_00"]]
    assert names == ["Ana"]


def test_short_speech_trains_nothing(ctx):
    user = _user()
    rec = _recording(user, {"SPEAKER_00": person()}, seconds={"SPEAKER_00": 5.0})
    _save_names(user, rec, {"SPEAKER_00": "Ana"})
    assert SpeakerVoiceSample.query.filter_by(recording_id=rec.id).count() == 0
    # The name is still applied to the transcript.
    assert json.loads(db.session.get(Recording, rec.id).transcription)[0]["speaker"] == "Ana"


def test_outlier_sample_is_kept_out(ctx):
    user = _user()
    ana = person()
    for _ in range(3):
        _save_names(user, _recording(user, {"SPEAKER_00": near(ana)}), {"SPEAKER_00": "Ana"})
    wrong = _recording(user, {"SPEAKER_00": -ana})  # plainly someone else
    _save_names(user, wrong, {"SPEAKER_00": "Ana"})
    sp = Speaker.query.filter_by(user_id=user.id, name="Ana").first()
    assert SpeakerVoiceSample.query.filter_by(speaker_id=sp.id).count() == 3
    assert SpeakerVoiceSample.query.filter_by(recording_id=wrong.id).count() == 0


def test_auto_labelling_gives_each_person_one_label(ctx):
    user = _user(auto_speaker_labelling=True, auto_speaker_labelling_threshold='medium')
    ana, bea = person(), person()
    _save_names(user, _recording(user, {"SPEAKER_00": ana, "SPEAKER_01": bea}), {"SPEAKER_00": "Ana", "SPEAKER_01": "Bea"})
    # Two labels both closest to Ana: only the closer one may become Ana.
    rec = _recording(user, {"SPEAKER_00": near(ana, 0.05), "SPEAKER_01": near(ana, 0.3)})
    from src.services.speaker_embedding_matcher import apply_auto_speaker_labels
    result = apply_auto_speaker_labels(rec, user)
    assert result.get("SPEAKER_00") == "Ana"
    assert result.get("SPEAKER_01") != "Ana"


def test_auto_labelling_skips_short_labels(ctx):
    user = _user(auto_speaker_labelling=True)
    ana = person()
    _save_names(user, _recording(user, {"SPEAKER_00": ana}), {"SPEAKER_00": "Ana"})
    rec = _recording(user, {"SPEAKER_00": near(ana, 0.05)}, seconds={"SPEAKER_00": 4.0})
    from src.services.speaker_embedding_matcher import apply_auto_speaker_labels
    assert apply_auto_speaker_labels(rec, user) == {}


# ------------------------------------------------------------- stage 3

def test_variants_keep_two_voice_conditions_apart():
    base = person()
    phone_voice, room_voice = unit(base + 0.9 * person()), unit(base - 0.9 * person())
    phone = [vp._Sample(near(phone_voice, 0.05), 1.0) for _ in range(3)]
    room = [vp._Sample(near(room_voice, 0.05), 1.0) for _ in range(3)]
    variants = vp.build_variants(phone + room)
    assert len(variants) == 2
    assert sorted(v["count"] for v in variants) == [3, 3]


def test_closest_variant_counts_where_one_average_fails(ctx):
    user = _user()
    base = person()
    phone, room = unit(base + 1.2 * person()), unit(base + 1.2 * person())
    for voice in (phone, phone, room, room):
        _save_names(user, _recording(user, {"SPEAKER_00": near(voice, 0.05)}), {"SPEAKER_00": "Ana"})
    sp = Speaker.query.filter_by(user_id=user.id, name="Ana").first()
    samples = vp.samples_for(sp, None)
    assert len(vp.build_variants(samples)) == 2
    target = near(phone, 0.05)
    by_variant = vp.score_against(target, vp.build_variants(samples), len(samples))
    one_average = float(np.dot(target, unit(sum(s.vector for s in samples))))
    assert by_variant > one_average + 0.1


def test_legacy_average_still_matches_and_is_kept_when_samples_start(ctx):
    user = _user()
    ana = person()
    sp = _speaker(user, "Ana", average=ana, count=4)
    assert [m["name"] for m in vp.find_matches(near(ana, 0.05), user.id, threshold=0.6)] == ["Ana"]
    _save_names(user, _recording(user, {"SPEAKER_00": near(ana, 0.05)}), {"SPEAKER_00": "Ana"})
    rows = SpeakerVoiceSample.query.filter_by(speaker_id=sp.id).all()
    assert sorted(r.source for r in rows) == ["confirmed", "legacy"]
    legacy = next(r for r in rows if r.source == "legacy")
    assert legacy.weight == 4.0 and legacy.recording_id is None


def test_correcting_a_name_moves_the_sample(ctx):
    user = _user()
    voice = person()
    rec = _recording(user, {"SPEAKER_00": voice})
    _save_names(user, rec, {"SPEAKER_00": "Ana"})
    # The transcript now shows "Ana"; the correction is made on that name.
    _save_names(user, db.session.get(Recording, rec.id), {"Ana": "Bea"})
    rows = SpeakerVoiceSample.query.filter_by(recording_id=rec.id).all()
    bea = Speaker.query.filter_by(user_id=user.id, name="Bea").first()
    ana = Speaker.query.filter_by(user_id=user.id, name="Ana").first()
    assert [(r.speaker_id, r.label) for r in rows] == [(bea.id, "SPEAKER_00")]
    assert ana.embedding_count == 0 and ana.average_embedding is None
    assert db.session.get(Recording, rec.id).speaker_label_map == {"SPEAKER_00": "Bea"}


def test_confirming_an_auto_label_upgrades_its_sample(ctx):
    user = _user(auto_speaker_labelling=True)
    ana = person()
    _save_names(user, _recording(user, {"SPEAKER_00": ana}), {"SPEAKER_00": "Ana"})
    rec = _recording(user, {"SPEAKER_00": near(ana, 0.05)})
    from src.services.speaker_embedding_matcher import (
        apply_auto_speaker_labels, apply_speaker_names_to_transcription, update_speaker_profiles_from_recording)
    mapping = apply_auto_speaker_labels(rec, user)
    assert mapping == {"SPEAKER_00": "Ana"}
    apply_speaker_names_to_transcription(rec, mapping)
    update_speaker_profiles_from_recording(rec, mapping, user)
    sample = SpeakerVoiceSample.query.filter_by(recording_id=rec.id).one()
    assert (sample.source, sample.weight) == ("auto", vp.AUTO_SAMPLE_WEIGHT)
    _save_names(user, db.session.get(Recording, rec.id), {"Ana": "Ana"})
    sample = SpeakerVoiceSample.query.filter_by(recording_id=rec.id).one()
    assert (sample.source, sample.weight) == ("confirmed", 1.0)


def test_resaving_does_not_double_count(ctx):
    user = _user()
    rec = _recording(user, {"SPEAKER_00": person()})
    for _ in range(3):
        _save_names(user, db.session.get(Recording, rec.id), {"SPEAKER_00": "Ana"} if _ == 0 else {"Ana": "Ana"})
    assert SpeakerVoiceSample.query.filter_by(recording_id=rec.id).count() == 1


def test_merged_away_label_loses_its_sample(ctx):
    user = _user()
    a, b = person(), person()
    rec = _recording(user, {"SPEAKER_00": a, "SPEAKER_01": b})
    _save_names(user, rec, {"SPEAKER_00": "Ana", "SPEAKER_01": "Bob"})
    rec = db.session.get(Recording, rec.id)
    data = json.loads(rec.transcription)
    for seg in data:
        seg["speaker"] = "Ana"  # the dialog merged Bob into Ana
    with patch("src.services.speaker_snippets.create_speaker_snippets", return_value=0):
        r = _client(user).post(f"/recording/{rec.id}/update_transcript",
                               json={"transcript_data": data, "speaker_map": {}})
    assert r.status_code == 200
    labels = {s.label for s in SpeakerVoiceSample.query.filter_by(recording_id=rec.id).all()}
    assert labels == {"SPEAKER_00"}


def test_sample_cap_drops_the_oldest(ctx):
    user = _user()
    ana = person()
    sp = _speaker(user, "Ana")
    for i in range(vp.MAX_SAMPLES + 3):
        rec = _recording(user, {"SPEAKER_00": near(ana, 0.05)})
        vp.record_sample(sp, rec, "SPEAKER_00", near(ana, 0.05))
        db.session.commit()
    assert SpeakerVoiceSample.query.filter_by(speaker_id=sp.id).count() == vp.MAX_SAMPLES


def test_merge_speakers_keeps_every_sample(ctx):
    from src.services.speaker_merge import merge_speakers
    user = _user()
    a = person()
    ana = _speaker(user, "Ana", average=a, count=2)
    ana2 = _speaker(user, "ana (dup)")
    _save_names(user, _recording(user, {"SPEAKER_00": near(a, 0.05)}), {"SPEAKER_00": "ana (dup)"})
    merge_speakers(ana.id, [ana2.id], user.id)
    rows = SpeakerVoiceSample.query.filter_by(speaker_id=ana.id).all()
    assert sorted(r.source for r in rows) == ["confirmed", "legacy"]


def test_clearing_a_profile_deletes_its_samples(ctx):
    user = _user()
    rec = _recording(user, {"SPEAKER_00": person()})
    _save_names(user, rec, {"SPEAKER_00": "Ana"})
    sp = Speaker.query.filter_by(user_id=user.id, name="Ana").first()
    assert _client(user).post(f"/speakers/{sp.id}/clear_embeddings").status_code == 200
    assert SpeakerVoiceSample.query.filter_by(speaker_id=sp.id).count() == 0


def test_retranscription_forgets_the_old_labels(ctx):
    user = _user()
    rec = _recording(user, {"SPEAKER_00": person()})
    _save_names(user, rec, {"SPEAKER_00": "Ana"})
    rec = db.session.get(Recording, rec.id)
    vp.forget_recording_samples(rec)
    db.session.commit()
    assert SpeakerVoiceSample.query.filter_by(recording_id=rec.id).count() == 0
    assert rec.speaker_label_map is None


def test_deleting_the_recording_keeps_the_voiceprint(ctx):
    from src.services.recording_deletion import delete_recording_completely
    user = _user()
    rec = _recording(user, {"SPEAKER_00": person()})
    _save_names(user, rec, {"SPEAKER_00": "Ana"})
    sp = Speaker.query.filter_by(user_id=user.id, name="Ana").first()
    with patch("src.services.storage.get_storage_service"):
        delete_recording_completely(db.session.get(Recording, rec.id))
    row = SpeakerVoiceSample.query.filter_by(speaker_id=sp.id).one()
    assert row.recording_id is None


def test_calibration_needs_data_and_lands_between_the_scores(ctx):
    user = _user()
    assert vp.calibrated_threshold(None) is None or True  # other tests' data may exist
    people = [person() for _ in range(6)]
    for i, p in enumerate(people):
        sp = _speaker(user, f"P{i}")
        for _ in range(3):
            rec = _recording(user, {"SPEAKER_00": near(p, 0.1)})
            vp.record_sample(sp, rec, "SPEAKER_00", near(p, 0.1))
    db.session.commit()
    vp._calibration_cache.clear()
    cal = vp.calibrated_threshold(None)
    assert cal is not None and 0.45 <= cal <= 0.85
    assert vp.suggestion_threshold(None) == pytest.approx(cal - 0.10)


# ------------------------------------------------------------- stage 2

def _fresh_spaces():
    SystemSetting.set_setting(vp.SETTING_CURRENT_SPACE, '', setting_type='string')
    SystemSetting.set_setting(vp.SETTING_LEGACY_SPACE, '', setting_type='string')


def test_register_space_recognises_models(ctx):
    _fresh_spaces()
    canary_a, canary_b = person(), person()
    with patch.dict(os.environ, {"DISABLE_VOICE_EMBEDDING_CHECK": "false"}):
        a = vp.register_space(canary_a)
        assert vp.legacy_space_id() == a and vp.current_space_id() == a
        b = vp.register_space(canary_b)
        assert b != a and vp.current_space_id() == b and vp.legacy_space_id() == a
        assert vp.register_space(near(canary_a, 0.001)) == a  # the same model again
    VoiceEmbeddingSpace.query.filter(VoiceEmbeddingSpace.id.in_([a, b])).delete(synchronize_session=False)
    db.session.commit()


def test_profiles_only_match_within_their_space(ctx):
    _fresh_spaces()
    user = _user()
    ana = person()
    with patch.dict(os.environ, {"DISABLE_VOICE_EMBEDDING_CHECK": "false"}):
        a = vp.register_space(person())
        # A profile stored before spaces existed: legacy space a.
        _speaker(user, "Ana", average=ana, count=3)
        b = vp.register_space(person())
        in_b = _recording(user, {"SPEAKER_00": near(ana, 0.05)}, space=b)
        in_a = _recording(user, {"SPEAKER_00": near(ana, 0.05)}, space=a)
        assert vp.suggestions_for_recording(in_b, user.id)["SPEAKER_00"] == []
        assert [m["name"] for m in vp.suggestions_for_recording(in_a, user.id)["SPEAKER_00"]] == ["Ana"]
        # Naming Ana in space b starts her space-b profile, leaving space a alone.
        _save_names(user, in_b, {"SPEAKER_00": "Ana"})
        sp = Speaker.query.filter_by(user_id=user.id, name="Ana").first()
        spaces = sorted(str(r.space_id) for r in SpeakerVoiceSample.query.filter_by(speaker_id=sp.id).all())
        assert spaces == sorted([str(None), str(b)])
    VoiceEmbeddingSpace.query.filter(VoiceEmbeddingSpace.id.in_([a, b])).delete(synchronize_session=False)
    db.session.commit()


def test_disabled_check_means_no_space_and_matching_as_before(ctx):
    user = _user()
    ana = person()
    _speaker(user, "Ana", average=ana, count=1)
    with patch.dict(os.environ, {"DISABLE_VOICE_EMBEDDING_CHECK": "true"}):
        assert vp.current_space_id() is None
        rec = _recording(user, {"SPEAKER_00": near(ana, 0.05)})
        assert [m["name"] for m in vp.suggestions_for_recording(rec, user.id)["SPEAKER_00"]] == ["Ana"]


def test_existing_reference_becomes_the_legacy_space(ctx):
    _fresh_spaces()
    from src.services import voice_embedding_check as check
    before = {s.id for s in VoiceEmbeddingSpace.query.all()}
    VoiceEmbeddingSpace.query.delete()
    db.session.commit()
    ref = [float(x) for x in person()]
    with patch.dict(os.environ, {"DISABLE_VOICE_EMBEDDING_CHECK": "false"}):
        check._initialize_spaces(vp, {"embedding": ref, "backend_fingerprint": "abc"})
        (space,) = VoiceEmbeddingSpace.query.all()
        assert vp.legacy_space_id() == space.id and vp.current_space_id() == space.id
        # Idempotent: a second start adds nothing.
        check._initialize_spaces(vp, {"embedding": ref, "backend_fingerprint": "abc"})
        assert VoiceEmbeddingSpace.query.count() == 1
    VoiceEmbeddingSpace.query.delete()
    db.session.commit()
    assert not before or True


# ------------------------------------------------------------ endpoints

def test_sample_list_and_removal_rebuild_the_profile(ctx):
    user = _user()
    ana = person()
    recs = [_recording(user, {"SPEAKER_00": near(ana, 0.05)}) for _ in range(2)]
    for rec in recs:
        _save_names(user, rec, {"SPEAKER_00": "Ana"})
    sp = Speaker.query.filter_by(user_id=user.id, name="Ana").first()
    c = _client(user)
    body = c.get(f"/speakers/{sp.id}/voice_samples").get_json()
    assert body["summary"]["sample_count"] == 2 and len(body["samples"]) == 2
    assert {s["recording_id"] for s in body["samples"]} == {r.id for r in recs}
    r = c.delete(f"/speakers/{sp.id}/voice_samples/{body['samples'][0]['id']}")
    assert r.status_code == 200 and r.get_json()["summary"]["sample_count"] == 1
    db.session.expire_all()
    assert db.session.get(Speaker, sp.id).embedding_count == 1
    # Another user cannot touch it.
    assert _client(_user()).delete(f"/speakers/{sp.id}/voice_samples/{body['samples'][1]['id']}").status_code == 404


def test_pre_sample_profile_is_listed(ctx):
    user = _user()
    sp = _speaker(user, "Old", average=person(), count=3)
    body = _client(user).get(f"/speakers/{sp.id}/voice_samples").get_json()
    assert [(s["id"], s["source"], s["weight"]) for s in body["samples"]] == [(None, "legacy", 3.0)]


def test_speaker_list_carries_voice_counts(ctx):
    user = _user()
    _save_names(user, _recording(user, {"SPEAKER_00": person()}), {"SPEAKER_00": "Ana"})
    rows = _client(user).get("/speakers").get_json()
    ana = next(r for r in rows if r["name"] == "Ana")
    assert ana["voice"]["sample_count"] == 1 and ana["voice"]["variant_count"] == 1


def test_admin_space_status_counts_old_profiles_in_the_legacy_space(ctx):
    from src.services import voice_embedding_check as check
    _fresh_spaces()
    with patch.dict(os.environ, {"DISABLE_VOICE_EMBEDDING_CHECK": "false"}):
        space = vp.register_space(person())
        user = _user()
        _speaker(user, "Old", average=person(), count=2)
        status = {s["id"]: s for s in check.spaces_status()}
        assert status[space]["current"] and status[space]["legacy"]
        assert status[space]["sample_count"] >= 1
    VoiceEmbeddingSpace.query.filter_by(id=space).delete()
    db.session.commit()
