"""Real InternalShare/group authorization against isolated SQLite API fixtures.

Provider/space doubles and locally seeded ready metadata isolate authorization.
This file does not claim an actual provider or subprocess preparation workflow.
"""
import importlib
import time
import wave
from unittest.mock import Mock

import pytest
from flask import Flask
from flask_login import LoginManager

from src.database import db
from src.models import (User, Speaker, Recording, InternalShare, Group, GroupMembership,
                        Tag, RecordingTag, VoiceEmbeddingSpace, ManualVoiceSample,
                        ManualVoiceSampleReceipt)
from src.services import manual_voice_samples as service, manual_voice_worker as worker
from src.services import voice_profiles as vp


@pytest.fixture
def access_fixture(tmp_path, monkeypatch):
    # Use the production authorization function, with feature configuration enabled.
    actual_app = importlib.import_module('src.app')
    monkeypatch.setattr(actual_app, 'ENABLE_INTERNAL_SHARING', True)
    from src.api import recordings
    monkeypatch.setattr(recordings, 'has_recording_access', actual_app.has_recording_access)
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY='owned-real-share-fixture', WTF_CSRF_ENABLED=False,
                      SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / 'fixture.db'))
    db.init_app(app)
    login = LoginManager(app)
    login.user_loader(lambda uid: db.session.get(User, int(uid)))
    from src.api.manual_voice_samples import manual_voice_samples_bp
    app.register_blueprint(manual_voice_samples_bp)
    jobs = service.ManualJobs(tmp_path / 'private-jobs')
    monkeypatch.setattr(service, 'manual_jobs', jobs)
    # No provider call: start still writes actual private preparation metadata.
    monkeypatch.setattr(jobs, '_supervise', lambda *args: None)
    snapshot = {'space_id': 1, 'dimension': 3, 'fingerprint': 'offline-access-fixture'}
    monkeypatch.setattr(service, 'space_snapshot', lambda: snapshot)
    monkeypatch.setattr(service, 'check_space', lambda _: None)
    monkeypatch.setattr(vp, 'current_space_id', lambda: 1)
    monkeypatch.setattr(vp, 'legacy_space_id', lambda: 1)
    monkeypatch.setenv('VOICE_PROFILE_MIN_SPEECH_SECONDS', '15')
    registry = Mock()
    registry.get_active_connector_name.return_value = 'offline-access-fixture'
    registry.get_active_connector.return_value.config = {}
    monkeypatch.setattr('src.services.transcription.get_registry', lambda: registry)
    with app.app_context():
        db.create_all()
        owner = User(username='source-owner', email='owner@access.test', password='x')
        recipient = User(username='recipient', email='recipient@access.test', password='x')
        db.session.add_all([owner, recipient, VoiceEmbeddingSpace(id=1, dimension=3)])
        db.session.commit()
        speaker = Speaker(user_id=recipient.id, name='Recipient profile')
        path = tmp_path / 'source.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\x01\x00' * 16000 * 24)
        rec = Recording(user_id=owner.id, title='Private source title', audio_path=str(path),
                        status='COMPLETED', transcription='dirty transcript fixture')
        db.session.add_all([speaker, rec])
        db.session.commit()
        share = InternalShare(recording_id=rec.id, owner_id=owner.id,
                              shared_with_user_id=recipient.id, can_edit=True)
        db.session.add(share)
        db.session.commit()
        monkeypatch.setattr('src.api.manual_voice_samples.local_path', lambda _: str(path))
        vp._calibration_cache.clear()
        yield app, recipient, speaker, rec, share, jobs, path, actual_app.has_recording_access
        db.session.remove()
        vp._calibration_cache.clear()


def login_client(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


def prepare_ready(fixture, client):
    _, user, speaker, rec, _, jobs, path, _ = fixture
    response = client.post(f'/recordings/{rec.id}/manual_voice_samples/prepare',
                           json={'speaker_id': speaker.id, 'start_ms': 2000, 'end_ms': 22000})
    assert response.status_code == 202, response.json
    job_id = response.json['job_id']
    with jobs.locked(job_id) as (directory, meta):
        # Real crop/digest, seeded embedding/timing: only access is under test.
        worker.crop_pcm(path, directory / 'work', 2000, 22000)
        digest, size = worker.source_digest(path)
        meta.update(state='ready', finished=time.time(), result={
            'vector': [1, 0, 0], 'speech_ms': 20000,
            'source_audio_sha256': digest, 'source_size': size})
        jobs._write(directory, meta)
    return job_id


@pytest.mark.parametrize('change', ['revoke', 'downgrade', 'group-role'])
@pytest.mark.parametrize('consumed', [False, True], ids=['first-commit', 'consumed-retry'])
def test_real_access_revocation_and_role_change_rechecked(access_fixture, change, consumed):
    app, user, speaker, rec, share, jobs, path, real_access = access_fixture
    membership = None
    if change == 'group-role':
        group = Group(name='Fixture group')
        db.session.add(group)
        db.session.flush()
        membership = GroupMembership(group_id=group.id, user_id=user.id, role='admin')
        tag = Tag(name='group-edit-tag', user_id=rec.user_id, group_id=group.id,
                  auto_share_on_apply=True, share_with_group_lead=True)
        db.session.add_all([membership, tag])
        db.session.flush()
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id))
        share.can_edit = False
        db.session.commit()
    assert rec.user_id != user.id and speaker.user_id == user.id
    assert real_access(rec, user, require_edit=True)
    client = login_client(app, user)
    job_id = prepare_ready(access_fixture, client)
    base = f'/recordings/{rec.id}/manual_voice_samples/preparations/{job_id}'
    target = f'/speakers/{speaker.id}/manual_voice_samples'
    assert client.get(base).json['state'] == 'ready'
    audio = client.get(base + '/audio')
    assert audio.status_code == 200 and audio.data[:4] == b'RIFF'
    assert audio.headers['Cache-Control'] == 'private, no-store'
    audio.close()
    if consumed:
        committed = client.post(target, json={'job_id': job_id})
        assert committed.status_code == 201, committed.json
        assert client.post(target, json={'job_id': job_id}).status_code == 200
        visible = client.get(target).json['samples'][0]
        assert visible['recording_id'] == rec.id and visible['recording_title'] == rec.title
    if change == 'revoke':
        db.session.delete(share)
    elif change == 'downgrade':
        share.can_edit = False
    else:
        membership.role = 'member'
    db.session.commit()
    assert not real_access(rec, user, require_edit=True)
    assert real_access(rec, user) == (change != 'revoke')
    for response in [client.get(base), client.get(base + '/audio'),
                     client.post(target, json={'job_id': job_id}),
                     client.post(f'/recordings/{rec.id}/manual_voice_samples/prepare',
                                 json={'speaker_id': speaker.id, 'start_ms': 0, 'end_ms': 20000})]:
        assert response.status_code == 403, response.json
        assert response.json['code'] == 'access'
        body = response.get_data(as_text=True)
        assert str(path) not in body and rec.title not in body and 'vector' not in body
    assert ManualVoiceSample.query.count() == int(consumed)
    assert ManualVoiceSampleReceipt.query.count() == int(consumed)
    assert rec.transcription == 'dirty transcript fixture'
    if consumed:
        item = client.get(target).json['samples'][0]
        if change == 'revoke':
            assert item['recording_id'] is None and item['recording_title'] is None
            assert item['source_available'] is False
            assert item['source_deleted'] is False  # sharing revoked; original still exists
        else:
            # Read-only sharing retains provenance, while prepare/commit/audio remain denied.
            assert item['recording_id'] == rec.id and item['recording_title'] == rec.title
        assert vp.find_matches([1, 0, 0], user.id, 1, threshold=.9)[0]['speaker_id'] == speaker.id
