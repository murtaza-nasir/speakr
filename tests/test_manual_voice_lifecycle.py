"""Manual/automatic union and real lifecycle paths on owned SQLite fixtures."""
import uuid
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import numpy as np
import pytest
from flask import Flask
from flask_login import LoginManager

from src.database import db
from src.models import (User, Speaker, Recording, VoiceEmbeddingSpace,
                        SpeakerVoiceSample, ManualVoiceSample, ManualVoiceSampleReceipt)
from src.services import voice_profiles as vp


@pytest.fixture(params=[False, True], ids=['sqlite-no-fk', 'sqlite-fk'])
def fixture(request, tmp_path):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY='manual-lifecycle-fixture', WTF_CSRF_ENABLED=False,
                      SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / 'fixture.db'))
    db.init_app(app)
    login = LoginManager(app)
    login.user_loader(lambda uid: db.session.get(User, int(uid)))
    from src.api.speakers import speakers_bp
    app.register_blueprint(speakers_bp)
    from src.api.admin import admin_bp
    app.register_blueprint(admin_bp)
    with app.app_context():
        db.session.execute(db.text('PRAGMA foreign_keys=' + ('ON' if request.param else 'OFF')))
        db.create_all()
        user = User(username='owner', email='owner@fixture.test', password='x')
        space = VoiceEmbeddingSpace(dimension=3)
        db.session.add_all([user, space])
        db.session.commit()
        sp = Speaker(user_id=user.id, name='Person')
        rec = Recording(user_id=user.id, title='Source', status='COMPLETED', audio_path=None,
                        original_filename='source.wav', speaker_embeddings_space_id=space.id)
        db.session.add_all([sp, rec])
        db.session.commit()
        vp._calibration_cache.clear()
        from src.services import manual_voice_samples as service
        with patch.object(vp, 'current_space_id', return_value=space.id), \
                patch.object(vp, 'legacy_space_id', return_value=space.id), \
                patch.object(service, 'manual_jobs', service.ManualJobs(root=tmp_path / 'jobs')):
            yield app, user, sp, rec, space
        db.session.remove()
        vp._calibration_cache.clear()


def manual(user, speaker, recording, space, vector=(1, 0, 0), start=0):
    job_id = str(uuid.uuid4())
    row = ManualVoiceSample(user_id=user.id, speaker_id=speaker.id,
                           recording_id=recording.id if recording else None,
                           start_ms=start, end_ms=start + 20000, speech_ms=19000,
                           source_audio_sha256='a' * 64, preparation_id=job_id,
                           space_id=space.id, embedding=vp.to_bytes(vp.normalize(vector)), dimension=3)
    db.session.add(row)
    db.session.flush()
    receipt = ManualVoiceSampleReceipt(preparation_id=job_id, user_id=user.id,
                                      initial_speaker_id=speaker.id,
                                      initial_speaker_created_at=speaker.created_at,
                                      sample_id=row.id, expires_at=datetime.utcnow() + timedelta(seconds=900))
    db.session.add(receipt)
    vp.refresh_speaker_summary(speaker)
    db.session.commit()
    return row, receipt


def client(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def ready_job(user, speaker, recording):
    import time
    from src.services import manual_voice_samples as service
    jobs = service.manual_jobs
    jobs._prepare()
    job_id = str(uuid.uuid4())
    directory = jobs.directory(job_id)
    directory.mkdir(mode=0o700)
    (directory / 'work').mkdir(mode=0o700)
    jobs._write(directory, {'job_id': job_id, 'user_id': user.id, 'speaker_id': speaker.id,
                            'speaker_created_at': speaker.created_at.isoformat(),
                            'recording_id': recording.id, 'state': 'ready', 'finished': time.time(),
                            'start_ms': 0, 'end_ms': 20000, 'result': {}})
    return job_id


def test_manual_only_matching_preload_and_delete_undo(fixture):
    app, user, speaker, rec, space = fixture
    row, receipt = manual(user, speaker, rec, space)
    assert vp.find_matches([1, 0, 0], user.id, space.id, threshold=.9)[0]['speaker_id'] == speaker.id
    assert vp.find_matches([1, 0, 0], user.id, space.id + 100, threshold=.9) == []
    assert client(app, user).get('/speakers').json[0]['voice']['sample_count'] == 1
    old_list = client(app, user).get(f'/speakers/{speaker.id}/voice_samples').json
    assert old_list['samples'] == []  # manual-derived mean is never a legacy row
    assert old_list['summary']['sample_count'] == 1
    db.session.delete(row)
    db.session.flush()
    vp.refresh_speaker_summary(speaker)
    db.session.commit()
    assert vp.find_matches([1, 0, 0], user.id, space.id, threshold=.9) == []
    assert speaker.average_embedding is None
    assert db.session.get(ManualVoiceSampleReceipt, receipt.preparation_id) is not None


def test_manual_average_never_materializes_as_legacy(fixture):
    _, user, speaker, rec, space = fixture
    manual(user, speaker, rec, space)
    assert vp.record_sample(speaker, rec, 'A', [1, 0, 0]) == 'stored'
    assert SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id, source='legacy').count() == 0
    vp.refresh_speaker_summary(speaker)
    assert speaker.embedding_count == 2
    vp.forget_recording_samples(rec)
    assert ManualVoiceSample.query.filter_by(speaker_id=speaker.id).count() == 1
    assert speaker.embedding_count == 1
    assert vp.record_sample(speaker, rec, 'B', [1, 0, 0]) == 'stored'
    assert SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id, source='legacy').count() == 0


def test_real_legacy_preserved_before_manual_and_auto_trim_independent(fixture):
    _, user, speaker, rec, space = fixture
    speaker.average_embedding = vp.to_bytes(vp.normalize([0, 1, 0]))
    speaker.embedding_count = 3
    vp._materialize_legacy(speaker)
    manual(user, speaker, rec, space)
    legacy = SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id, source='legacy').one()
    assert legacy.weight == 3
    for i in range(22):
        vp.record_sample(speaker, rec, str(i), [1, 0, 0])
    assert SpeakerVoiceSample.query.filter_by(speaker_id=speaker.id).count() == vp.MAX_SAMPLES
    assert ManualVoiceSample.query.filter_by(speaker_id=speaker.id).count() == 1


@pytest.mark.parametrize('action', ['clear', 'single', 'bulk'])
def test_profile_lifecycle_explicit_cleanup_retains_receipt(fixture, action):
    app, user, speaker, rec, space = fixture
    row, receipt = manual(user, speaker, rec, space)
    sid, job_id = speaker.id, receipt.preparation_id
    from src.services import manual_voice_samples as service
    pending_id = ready_job(user, speaker, rec)
    with patch.object(service, 'invalidate_profile', wraps=service.invalidate_profile) as invalidated:
        endpoint = {'clear': f'/speakers/{sid}/clear_embeddings', 'single': f'/speakers/{sid}',
                    'bulk': '/speakers/delete_all'}[action]
        response = client(app, user).open(endpoint, method='POST' if action == 'clear' else 'DELETE')
        assert response.status_code == 200, response.json
        invalidated.assert_called_with(user.id, sid)
    with service.manual_jobs.locked(pending_id) as (_, meta):
        assert meta['state'] == 'cancelled'
    assert ManualVoiceSample.query.filter_by(speaker_id=sid).count() == 0
    assert db.session.get(ManualVoiceSampleReceipt, job_id) is not None
    if action == 'clear':
        assert db.session.get(Speaker, sid).average_embedding is None
    else:
        replacement = Speaker(id=sid, user_id=user.id, name='Replacement')
        db.session.add(replacement)
        db.session.commit()
        assert db.session.get(ManualVoiceSampleReceipt, job_id).initial_speaker_created_at != replacement.created_at
    with pytest.raises(service.ManualVoiceError) as stale:
        service.commit_sample(user.id, sid, pending_id, authorize=lambda _: 'unused')
    assert stale.value.code == ('cancelled' if action == 'clear' else 'missing')
    db.session.rollback()


def test_merge_keeps_manual_uuid_provenance_and_receipt_initial_target(fixture):
    _, user, source, rec, space = fixture
    row, receipt = manual(user, source, rec, space)
    source_id, sample_id, job_id = source.id, row.id, receipt.preparation_id
    target = Speaker(user_id=user.id, name='Target')
    db.session.add(target)
    db.session.commit()
    # Existing policy permits merge overflow; it must never evict manual rows.
    for i in range(20):
        manual(user, target, rec, space, start=i * 10)
    from src.services.speaker_merge import merge_speakers
    merged = merge_speakers(target.id, [source.id], user.id)
    assert ManualVoiceSample.query.filter_by(speaker_id=target.id).count() == 21
    assert db.session.get(ManualVoiceSample, sample_id).speaker_id == target.id
    assert db.session.get(ManualVoiceSampleReceipt, job_id).initial_speaker_id == source_id
    assert db.session.get(Speaker, source_id) is None
    assert merged.embedding_count == 21
    assert SpeakerVoiceSample.query.filter_by(source='legacy').count() == 0


def test_recording_deletion_keeps_voice_and_range_without_fk(fixture):
    _, user, speaker, rec, space = fixture
    row, receipt = manual(user, speaker, rec, space, start=71250)
    sample_id, recording_id = row.id, rec.id
    from src.services.recording_deletion import delete_recording_completely
    with patch('src.services.webhook_dispatch.emit_webhook_event'), \
            patch('src.file_exporter.mark_export_as_deleted'):
        delete_recording_completely(rec, storage=Mock())
    db.session.expire_all()
    saved = db.session.get(ManualVoiceSample, sample_id)
    assert saved.recording_id is None
    assert saved.start_ms == 71250 and saved.end_ms == 91250
    assert saved.source_audio_sha256 == 'a' * 64
    assert vp.find_matches([1, 0, 0], user.id, space.id, threshold=.9)
    assert db.session.get(Recording, recording_id) is None


@pytest.mark.parametrize('enabled', [False, True])
def test_orphan_policy_respected_for_manual(fixture, monkeypatch, enabled):
    _, user, speaker, _, space = fixture
    row, receipt = manual(user, speaker, None, space)
    sid, job_id = speaker.id, receipt.preparation_id
    monkeypatch.setenv('DELETE_ORPHANED_SPEAKERS', str(enabled).lower())
    from src.services.speaker_cleanup import cleanup_orphaned_speakers
    cleanup_orphaned_speakers()
    assert (db.session.get(Speaker, sid) is None) == enabled
    assert ManualVoiceSample.query.filter_by(speaker_id=sid).count() == (0 if enabled else 1)
    assert db.session.get(ManualVoiceSampleReceipt, job_id) is not None


def test_admin_user_deletion_removes_manual_and_receipts(fixture):
    app, user, speaker, rec, space = fixture
    manual(user, speaker, rec, space)
    uid = user.id
    admin = User(username='admin', email='admin@fixture.test', password='x', is_admin=True)
    db.session.add(admin)
    db.session.commit()
    with patch('src.services.storage.get_storage_service', return_value=Mock()):
        response = client(app, admin).delete(f'/admin/users/{uid}')
    assert response.status_code == 200, response.json
    assert ManualVoiceSample.query.filter_by(user_id=uid).count() == 0
    assert ManualVoiceSampleReceipt.query.filter_by(user_id=uid).count() == 0
    assert db.session.get(User, uid) is None


def test_spaces_status_counts_manual_nonlegacy_without_phantom_average(fixture):
    _, user, speaker, rec, legacy_space = fixture
    new_space = VoiceEmbeddingSpace(dimension=3)
    db.session.add(new_space)
    db.session.commit()
    from src.services.voice_embedding_check import spaces_status
    with patch.object(vp, 'current_space_id', return_value=new_space.id):
        manual(user, speaker, rec, new_space)
        assert speaker.average_embedding is not None
        status = {space['id']: space for space in spaces_status()}
        assert status[legacy_space.id]['sample_count'] == 0
        assert status[new_space.id]['sample_count'] == 1
        # A genuine average-only profile retains the previous legacy semantics.
        old_profile = Speaker(user_id=user.id, name='Genuine legacy',
                              average_embedding=vp.to_bytes(vp.normalize([0, 1, 0])),
                              embedding_count=5)
        db.session.add(old_profile)
        db.session.commit()
        status = {space['id']: space for space in spaces_status()}
        assert status[legacy_space.id]['sample_count'] == 1
        assert status[new_space.id]['sample_count'] == 1
