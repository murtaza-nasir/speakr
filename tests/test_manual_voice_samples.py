"""Offline contracts: real PCM crop, isolated DB/API, supervised subprocesses.

The connector doubles establish request/response safety, never ASR accuracy.
"""
import json
import math
import os
import struct
import sys
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest
from flask import Flask
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect

from src.database import db
from src.models import User, Speaker, Recording, ManualVoiceSample, ManualVoiceSampleReceipt, VoiceEmbeddingSpace
from src.services import manual_voice_samples as service
from src.services import manual_voice_worker as worker
from src.services import voice_profiles as vp
from src.services.transcription import TranscriptionCapability as Cap, TranscriptionResponse, TranscriptionSegment


def wav_file(path, seconds=24, rate=44100, channels=2):
    frames = b''.join(struct.pack('<' + 'h' * channels,
        *([int(12000 * math.sin(i * .037))] * channels)) for i in range(seconds * rate))
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    return frames


@pytest.mark.parametrize('rate,channels,start,end', [(44100, 2, 123, 18007), (16000, 1, 999, 22000)])
def test_pcm_crop_markers_sample_exact(tmp_path, rate, channels, start, end):
    source = tmp_path / 'source.wav'
    frames = wav_file(source, rate=rate, channels=channels)
    before = worker.source_digest(source)
    clip, duration = worker.crop_pcm(source, tmp_path, start, end)
    first, last = start * rate // 1000, (end * rate + 999) // 1000
    with wave.open(str(clip)) as audio:
        assert audio.getnchannels() == channels
        assert audio.getframerate() == rate
        assert audio.getnframes() == last - first
        assert audio.readframes(audio.getnframes()) == frames[first * channels * 2:last * channels * 2]
    assert duration == (last - first) / rate
    assert worker.source_digest(source) == before
    assert not (tmp_path / 'decoded.wav').exists()


@pytest.mark.parametrize('start,end', [(False, 15000), (0, True), (0.0, 15000), (0, '15000'),
                                      (-1, 15000), (1000, 1000), (0, 300001), (0, 14999)])
def test_integer_ms_contract(start, end):
    with pytest.raises(service.ManualVoiceError, match='bounds'):
        service.validate_bounds(start, end, 15)


@pytest.mark.parametrize('value', ['nan', 'inf', '0', '-1', 'oops'])
def test_invalid_minimum_fails_closed(monkeypatch, value):
    monkeypatch.setenv('VOICE_PROFILE_MIN_SPEECH_SECONDS', value)
    with pytest.raises(service.ManualVoiceError, match='unavailable'):
        service.minimum_speech()


def response(vector=(1, 0, 0), segments=None):
    return TranscriptionResponse(text='fixture', speaker_embeddings={'S': list(vector)}, segments=segments or [
        TranscriptionSegment('a', 'S', 0, 10), TranscriptionSegment('b', 'S', 5, 16),
        TranscriptionSegment('c', 'S', 17, 21)])


def test_timed_union_clipped_not_sum_or_clip_duration():
    result = worker.validate_response(response(), 20, 3, 15)
    assert result['speech_ms'] == 19000
    assert np.linalg.norm(result['vector']) == pytest.approx(1)
    with pytest.raises(worker.RangeError):
        worker.validate_response(response(segments=[TranscriptionSegment('few', 'S', 2, 5)]), 20, 3, 15)


@pytest.mark.parametrize('bad', ['nan', 'dimension', 'zero', 'multi', 'unlabelled', 'times', 'silence'])
def test_unusable_response_rejected(bad):
    answer = response()
    if bad == 'nan': answer.speaker_embeddings = {'S': [float('nan'), 0, 1]}
    if bad == 'dimension': answer.speaker_embeddings = {'S': [1, 0]}
    if bad == 'zero': answer.speaker_embeddings = {'S': [0, 0, 0]}
    if bad == 'multi': answer.speaker_embeddings['T'] = [0, 1, 0]
    if bad == 'unlabelled': answer.segments[0].speaker = None
    if bad == 'times': answer.segments[0].start_time = float('nan')
    if bad == 'silence': answer.segments = []
    with pytest.raises(worker.RangeError):
        worker.validate_response(answer, 20, 3, 15)


def test_worker_upload_only_pcm_clip_current_params(tmp_path):
    source = tmp_path / 'original.wav'
    wav_file(source)
    uploads = []
    connector = Mock()
    connector.supports.side_effect = lambda cap: cap in {Cap.DIARIZATION, Cap.TIMESTAMPS, Cap.SPEAKER_EMBEDDINGS}
    def transcribe(request):
        assert request.diarize is True
        assert request.min_speakers is None and request.max_speakers is None
        assert request.model == 'configured-current'
        assert request.prompt == 'historical hint'
        with wave.open(request.audio_file) as audio:
            uploads.append(audio.getnframes() / audio.getframerate())
        return response(segments=[TranscriptionSegment('test', 'S', 0, 20)])
    connector.transcribe.side_effect = transcribe
    payload = {'path': str(source), 'signature': service.source_signature(source), 'work': str(tmp_path),
        'start_ms': 2000, 'end_ms': 22000, 'minimum': 15, 'dimension': 3,
        'connector': 'fixture', 'config': {'secret': 'never-stored'},
        'params': {'transcription_model': 'configured-current', 'initial_prompt': 'historical hint'}}
    with patch('src.services.transcription.get_registry') as registry:
        registry.return_value.create_connector.return_value = connector
        result = worker.prepare_range(payload)
    assert uploads == [20]
    assert result['speech_ms'] == 20000
    assert result['source_audio_sha256'] == worker.source_digest(source)[0]
    assert 'secret' not in json.dumps(result) and 'raw_response' not in result


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY='owned-fixture', WTF_CSRF_ENABLED=False,
                      SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / 'fixture.db'),
                      SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'timeout': 10}})
    db.init_app(app)
    login = LoginManager(app)
    login.user_loader(lambda uid: db.session.get(User, int(uid)))
    CSRFProtect(app)
    from src.api.manual_voice_samples import manual_voice_samples_bp
    from src.api import recordings
    app.register_blueprint(manual_voice_samples_bp)
    monkeypatch.setattr(recordings, 'has_recording_access', lambda rec, user, require_edit=False: rec.user_id == user.id)
    jobs = service.ManualJobs(tmp_path / 'private-jobs')
    monkeypatch.setattr(service, 'manual_jobs', jobs)
    monkeypatch.setattr(service, 'check_space', lambda _: None)
    monkeypatch.setattr(service, 'space_snapshot', lambda: {'space_id': 1, 'dimension': 3, 'fingerprint': 'fixture', 'clip_version': 'v1'})
    monkeypatch.setattr(vp, 'current_space_id', lambda: 1)
    monkeypatch.setattr(vp, 'legacy_space_id', lambda: 1)
    monkeypatch.setenv('VOICE_PROFILE_MIN_SPEECH_SECONDS', '15')
    with app.app_context():
        db.create_all()
        user = User(username='owner', email='owner@test.local', password='x')
        db.session.add_all([user, VoiceEmbeddingSpace(id=1, dimension=3)])
        db.session.commit()
        speaker = Speaker(user_id=user.id, name='Person')
        path = tmp_path / 'source.wav'
        wav_file(path, seconds=24, rate=16000, channels=1)
        recording = Recording(user_id=user.id, title='Source', audio_path=str(path), status='COMPLETED',
                              transcription='dirty draft unchanged')
        db.session.add_all([speaker, recording])
        db.session.commit()
        with patch('src.api.manual_voice_samples.local_path', return_value=str(path)):
            yield app, user, speaker, recording, path, jobs
        db.session.remove()


def client(app, user):
    c = app.test_client()
    with c.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return c


def ready(fixture, start=0, end=20000):
    app, user, speaker, recording, path, jobs = fixture
    jobs._prepare()
    job_id = str(uuid.uuid4())
    directory = jobs.directory(job_id)
    directory.mkdir(mode=0o700)
    work = directory / 'work'
    work.mkdir(mode=0o700)
    worker.crop_pcm(path, work, start, end)
    digest, size = worker.source_digest(path)
    meta = {'job_id': job_id, 'user_id': user.id, 'speaker_id': speaker.id,
        'speaker_created_at': speaker.created_at.isoformat(), 'recording_id': recording.id,
        'path': str(path), 'signature': service.source_signature(path), 'start_ms': start, 'end_ms': end,
        'space': service.space_snapshot(), 'state': 'ready', 'finished': time.time(),
        'result': {'vector': [1, 0, 0], 'speech_ms': end - start,
                   'source_audio_sha256': digest, 'source_size': size}}
    jobs._write(directory, meta)
    return job_id, directory, meta


def test_commit_exactly_once_multiple_ranges_delete_receipt(fixture):
    app, user, speaker, rec, path, jobs = fixture
    c = client(app, user)
    ids = []
    for start, end in [(0, 18000), (3000, 23000)]:
        job, _, _ = ready(fixture, start, end)
        route = f'/speakers/{speaker.id}/manual_voice_samples'
        first = c.post(route, json={'job_id': job})
        retry = c.post(route, json={'job_id': job})
        assert first.status_code == 201 and retry.status_code == 200
        assert first.json['id'] == retry.json['id']
        ids.append(first.json['id'])
    assert len(c.get(route).json['samples']) == 2
    assert rec.transcription == 'dirty draft unchanged'
    assert ManualVoiceSample.query.count() == 2 and ManualVoiceSampleReceipt.query.count() == 2
    assert c.delete(route + '/' + ids[-1]).status_code == 200
    assert c.post(route, json={'job_id': job}).status_code == 410
    assert ManualVoiceSample.query.count() == 1 and ManualVoiceSampleReceipt.query.count() == 2


def test_hash_changed_even_same_stat_commit_rejected(fixture):
    app, user, speaker, rec, path, jobs = fixture
    job, _, _ = ready(fixture)
    stat = path.stat()
    data = bytearray(path.read_bytes())
    data[-4] ^= 4
    path.write_bytes(data)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert service.source_signature(path) == [stat.st_size, stat.st_mtime_ns, stat.st_ino]
    result = client(app, user).post(f'/speakers/{speaker.id}/manual_voice_samples', json={'job_id': job})
    assert result.status_code == 409 and result.json['code'] == 'changed'
    assert ManualVoiceSample.query.count() == 0


@pytest.mark.parametrize('action', ['status', 'audio', 'audio_range', 'cancel', 'commit', 'consumed'])
def test_access_rechecked_every_endpoint_including_receipt(fixture, monkeypatch, action):
    app, user, speaker, rec, path, jobs = fixture
    job, _, _ = ready(fixture)
    c = client(app, user)
    base = f'/recordings/{rec.id}/manual_voice_samples/preparations/{job}'
    commit = f'/speakers/{speaker.id}/manual_voice_samples'
    if action == 'consumed':
        assert c.post(commit, json={'job_id': job}).status_code == 201
    from src.api import recordings
    monkeypatch.setattr(recordings, 'has_recording_access', lambda *args, **kwargs: False)
    if action in ('commit', 'consumed'):
        result = c.post(commit, json={'job_id': job})
    elif action == 'cancel': result = c.delete(base)
    else: result = c.get(base + ('/audio' if action in ('audio', 'audio_range') else ''),
                         headers={'Range': 'bytes=44-75'} if action == 'audio_range' else {})
    assert result.status_code == 403
    assert 'vector' not in result.get_data(as_text=True)


def test_profile_identity_reuse_cancel_expiry_private_audio(fixture):
    app, user, speaker, rec, path, jobs = fixture
    job, directory, meta = ready(fixture)
    c = client(app, user)
    base = f'/recordings/{rec.id}/manual_voice_samples/preparations/{job}'
    status = c.get(base).json
    assert {key: value for key, value in status.items() if key != 'expires_at'} == {'state': 'ready', 'job_id': job,
        'range': {'start_ms': 0, 'end_ms': 20000}, 'speech_ms': 20000, 'space_id': 1}
    assert status['expires_at'].endswith('Z')
    audio = c.get(base + '/audio')
    assert audio.status_code == 200 and audio.mimetype == 'audio/wav'
    assert audio.headers['Cache-Control'] == 'private, no-store'
    full_audio = audio.get_data()
    assert audio.headers['Content-Length'] == str(len(full_audio))
    partial = c.get(base + '/audio', headers={'Range': 'bytes=44-75'})
    assert partial.status_code == 206
    assert partial.get_data() == full_audio[44:76]
    assert partial.headers['Content-Length'] == '32'
    assert partial.headers['Content-Range'] == f'bytes 44-75/{len(full_audio)}'
    assert partial.headers['Cache-Control'] == 'private, no-store'
    unsatisfiable = c.get(base + '/audio', headers={'Range': f'bytes={len(full_audio) + 1}-'})
    assert unsatisfiable.status_code == 416 and unsatisfiable.json['code'] == 'bounds'
    speaker.created_at = speaker.created_at + timedelta(seconds=1)
    db.session.commit()
    assert c.get(base).status_code == 404
    speaker.created_at = speaker.created_at - timedelta(seconds=1)
    db.session.commit()
    assert c.delete(base).json['state'] == 'cancelled'
    assert c.post(f'/speakers/{speaker.id}/manual_voice_samples', json={'job_id': job}).status_code == 410
    with jobs.locked(job) as (_, changed):
        changed['finished'] = time.time() - 901
        changed['expires_at'] = time.time() - 1
        jobs._write(directory, changed)
    assert c.get(base).status_code == 410


def test_cap_19_two_jobs_and_same_job_races(fixture):
    app, user, speaker, rec, path, jobs = fixture
    for i in range(19):
        db.session.add(ManualVoiceSample(user_id=user.id, speaker_id=speaker.id, recording_id=rec.id,
            start_ms=0, end_ms=20000, speech_ms=20000, source_audio_sha256='a' * 64,
            preparation_id=str(uuid.uuid4()), space_id=1, embedding=vp.to_bytes(vp.normalize([1, 0, 0])), dimension=3))
    db.session.commit()
    job1, _, _ = ready(fixture)
    job2, _, _ = ready(fixture)
    uid, sid = user.id, speaker.id
    barrier = threading.Barrier(2)
    results = []
    def commit(job):
        with app.app_context():
            barrier.wait()
            try:
                sample, created = service.commit_sample(uid, sid, job, lambda meta: str(path))
                results.append(('ok', sample.id, created))
            except service.ManualVoiceError as error:
                db.session.rollback()
                results.append((error.code,))
            finally:
                db.session.remove()
    threads = [threading.Thread(target=commit, args=(job,)) for job in [job1, job2]]
    for thread in threads: thread.start()
    for thread in threads: thread.join(15)
    assert sorted(result[0] for result in results) == ['limit', 'ok']
    db.session.expire_all()
    assert ManualVoiceSample.query.count() == 20
    # Both concurrent retries of the consumed job return its original UUID.
    consumed = ManualVoiceSampleReceipt.query.one().preparation_id
    results.clear()
    threads = [threading.Thread(target=commit, args=(consumed,)) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(15)
    assert len(results) == 2 and all(r[0] == 'ok' and r[2] is False for r in results)
    assert results[0][1] == results[1][1]


def test_space_reference_fresh_existing_only(fixture, monkeypatch):
    from src.services import voice_embedding_check as check
    app, user, speaker, rec, path, jobs = fixture
    # Restore real function hidden by the fixture's route stand-in.
    real = ORIGINAL_SNAPSHOT
    space = db.session.get(VoiceEmbeddingSpace, 1)
    space.backend_fingerprint = 'current'
    db.session.commit()
    ref = {'status': 'ok', 'clip_version': 'v1', 'dimension': 3,
           'backend_fingerprint': 'current', 'checked_at': datetime.utcnow().isoformat()}
    connector = Mock()
    connector.config = {}
    connector.model = 'fixture'
    connector.supports.return_value = True
    monkeypatch.setattr(check, 'load_reference', lambda: ref)
    monkeypatch.setattr(check, 'backend_fingerprint', lambda: 'current')
    with patch('src.services.transcription.get_registry') as registry, \
            patch.object(check, 'probe_backend', side_effect=AssertionError('hidden upload')):
        registry.return_value.get_active_connector.return_value = connector
        registry.return_value.get_active_connector_name.return_value = 'fixture'
        accepted = real()
        assert accepted['space_id'] == 1
        connector.config = {'api_key': 'changed-private-key'}
        assert real()['config_fingerprint'] != accepted['config_fingerprint']
        connector.config = {}
        ref['checked_at'] = datetime.utcnow().isoformat()
        assert real()['reference_hash'] != accepted['reference_hash']
        for key, value in [('checked_at', (datetime.utcnow() - timedelta(days=2)).isoformat()),
                           ('dimension', float('nan')), ('backend_fingerprint', 'old'), ('status', 'changed')]:
            previous = ref[key]
            ref[key] = value
            with pytest.raises(service.ManualVoiceError): real()
            ref[key] = previous
        connector.supports.return_value = False
        with pytest.raises(service.ManualVoiceError, match='unsupported'): real()


ORIGINAL_SNAPSHOT = service.space_snapshot


def test_csrf_required_and_client_vector_rejected(fixture):
    app, user, speaker, rec, path, jobs = fixture
    job, _, _ = ready(fixture)
    c = client(app, user)
    route = f'/speakers/{speaker.id}/manual_voice_samples'
    assert c.post(route, json={'job_id': job, 'vector': [1, 0, 0]}).status_code == 400
    app.config['WTF_CSRF_ENABLED'] = True
    assert c.post(route, json={'job_id': job}).status_code == 400
    assert ManualVoiceSample.query.count() == 0


def test_bounded_supervised_worker_deadline_cancel_private_namespace(fixture):
    app, user, speaker, rec, path, _ = fixture
    for mode in ('timeout', 'cancelled'):
        jobs = service.ManualJobs(path.parent / ('jobs-' + mode), deadline=.3,
            worker_command=[sys.executable, '-c', 'import sys,time;sys.stdin.read();time.sleep(20)'])
        payload = jobs.start(app, user.id, rec.id, speaker, path, 0, 20000, 'fixture',
                             {'secret': 'private-credential'}, {}, service.space_snapshot())
        job = payload['job_id']
        if mode == 'cancelled':
            with jobs.locked(job) as (directory, meta): jobs._cancel(directory, meta)
        until = time.monotonic() + 5
        while time.monotonic() < until:
            with jobs.locked(job) as (_, meta):
                if meta['state'] != 'preparing': break
            time.sleep(.05)
        assert meta['code'] == mode
        stored = (jobs.directory(job) / 'meta.json').read_text()
        assert 'private-credential' not in stored
        assert jobs.root.name != 'speakr-segment-transcriptions'


def test_existing_windows_directory_acl_failure_never_bypassed(tmp_path, monkeypatch):
    root = tmp_path / 'existing-jobs'
    root.mkdir()
    jobs = service.ManualJobs(root=root)
    monkeypatch.setenv('USERNAME', 'fixture')
    monkeypatch.setenv('USERDOMAIN', 'fixture-domain')
    runner = Mock(return_value=Mock(returncode=1))
    monkeypatch.setattr(service.subprocess, 'run', runner)
    # Instantiate the native Path before mocking Windows on this Linux runner.
    monkeypatch.setattr(service.os, 'name', 'nt')
    for _ in range(2):
        with pytest.raises(service.ManualVoiceError, match='unavailable'):
            jobs._prepare()
    assert runner.call_count == 2
    for call in runner.call_args_list:
        assert call.kwargs['env']['SPEAKR_MANUAL_JOB_ROOT'] == str(root)
        script = call.args[0][-1]
        assert 'DirectorySecurity]::new()' in script and 'SetAccessRuleProtection($true, $false)' in script
        assert 'Set-Acl -LiteralPath $path -AclObject $acl' in script
        assert '$rules.Count -ne 2' in script


def test_expired_receipts_owned_bounded_and_read_cleanup(fixture):
    app, user, speaker, rec, path, jobs = fixture
    other = User(username='other', email='other@test.local', password='x')
    db.session.add(other)
    db.session.commit()
    rows = []
    for owner, expired in [(user, True), (user, True), (user, False), (other, True)]:
        row = ManualVoiceSampleReceipt(preparation_id=str(uuid.uuid4()), user_id=owner.id,
            initial_speaker_id=speaker.id, initial_speaker_created_at=speaker.created_at,
            sample_id=str(uuid.uuid4()), expires_at=datetime.utcnow() + timedelta(seconds=-1 if expired else 900))
        rows.append(row)
        db.session.add(row)
    db.session.commit()
    ids = [row.preparation_id for row in rows]
    assert service.purge_expired_receipts(user.id, limit=1) == 1
    db.session.commit()
    assert ManualVoiceSampleReceipt.query.filter_by(user_id=user.id).count() == 2
    assert client(app, user).get(f'/speakers/{speaker.id}/manual_voice_samples').status_code == 200
    assert ManualVoiceSampleReceipt.query.filter_by(user_id=user.id).count() == 1
    assert db.session.get(ManualVoiceSampleReceipt, ids[2]) is not None
    assert db.session.get(ManualVoiceSampleReceipt, ids[3]) is not None


def test_empty_embedding_is_provider_not_multiple_speakers():
    answer = response()
    answer.speaker_embeddings = {}
    with pytest.raises(worker.RangeError) as error:
        worker.validate_response(answer, 20, 3, 15)
    assert service.safe_code(error.value.code) == 'provider'


def test_recording_invalidation_rescans_jobs_created_during_initial_discovery(fixture, monkeypatch):
    app, user, speaker, rec, path, jobs = fixture
    jobs._prepare()
    original = service.begin_profile_transaction
    created = []
    def lock_and_create(speaker_id, user_id):
        result = original(speaker_id, user_id)
        if not created:
            created.append(ready(fixture)[0])
        return result
    monkeypatch.setattr(service, 'begin_profile_transaction', lock_and_create)
    service.invalidate_recording(rec.id)
    with jobs.locked(created[0]) as (_, meta):
        assert meta['state'] == 'cancelled'


@pytest.mark.parametrize('action', ['cancel', 'clear'])
def test_commit_cancel_clear_races_serialize_without_resurrection(fixture, action):
    app, user, speaker, rec, path, jobs = fixture
    job, _, _ = ready(fixture)
    uid, sid = user.id, speaker.id
    barrier = threading.Barrier(2)
    outcomes = []
    def commit():
        with app.app_context():
            barrier.wait()
            try:
                _, created = service.commit_sample(uid, sid, job, lambda _: str(path))
                outcomes.append('committed' if created else 'retry')
            except service.ManualVoiceError as error:
                outcomes.append(error.code)
                db.session.rollback()
            finally:
                db.session.remove()
    def mutate():
        with app.app_context():
            barrier.wait()
            current = service.begin_profile_transaction(sid, uid)
            service.invalidate_profile(uid, sid)
            if action == 'clear':
                ManualVoiceSample.query.filter_by(user_id=uid, speaker_id=sid).delete(synchronize_session=False)
                vp.refresh_speaker_summary(current)
            db.session.commit()
            db.session.remove()
    threads = [threading.Thread(target=commit), threading.Thread(target=mutate)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(15)
    assert all(not thread.is_alive() for thread in threads)
    db.session.expire_all()
    assert outcomes[0] in ('committed', 'cancelled')
    if action == 'clear' or outcomes[0] == 'cancelled':
        assert ManualVoiceSample.query.count() == 0
    with pytest.raises(service.ManualVoiceError):
        if action == 'clear':
            service.commit_sample(uid, sid, job, lambda _: str(path))
        else:
            with jobs.locked(job) as (_, meta):
                if meta['state'] != 'ready':
                    raise service.ManualVoiceError('cancelled', 410)
    db.session.rollback()


def test_audio_removal_waits_for_paused_digest_commit_then_preserves_vector(fixture, monkeypatch):
    from src.services import retention
    app, user, speaker, rec, path, jobs = fixture
    job, _, _ = ready(fixture)
    uid, sid, rid = user.id, speaker.id, rec.id
    digested, release, removal_started, deleted = (threading.Event() for _ in range(4))
    errors, results = [], []
    original_digest = service.source_digest
    def paused_digest(source):
        value = original_digest(source)
        digested.set()
        assert release.wait(5)
        return value
    monkeypatch.setattr(service, 'source_digest', paused_digest)
    storage = Mock()
    storage.exists.side_effect = lambda _: path.exists()
    def delete(*args, **kwargs):
        assert release.is_set(), 'Audio bytes were removed before the commit released its profile lock'
        path.unlink()
        deleted.set()
    storage.delete.side_effect = delete
    monkeypatch.setattr(retention, 'get_storage_service', lambda: storage)
    def commit():
        with app.app_context():
            try:
                _, created = service.commit_sample(uid, sid, job, lambda _: str(path))
                results.append(created)
            except Exception as error: errors.append(error)
            finally: db.session.remove()
    def remove():
        with app.app_context():
            try:
                current = db.session.get(Recording, rid)
                removal_started.set()
                retention.remove_recording_audio(current)
            except Exception as error: errors.append(error)
            finally: db.session.remove()
    commit_thread = threading.Thread(target=commit)
    commit_thread.start()
    assert digested.wait(5)
    remove_thread = threading.Thread(target=remove)
    remove_thread.start()
    assert removal_started.wait(5)
    assert not deleted.wait(.2) and path.exists()
    release.set()
    commit_thread.join(10)
    remove_thread.join(10)
    assert not errors and results == [True]
    assert deleted.is_set() and not path.exists()
    db.session.expire_all()
    assert db.session.get(Recording, rid).audio_deleted_at is not None
    assert ManualVoiceSample.query.count() == 1
    assert ManualVoiceSampleReceipt.query.count() == 1
    with jobs.locked(job) as (_, meta): assert meta['state'] == 'cancelled'


def ordered_profile_race(app, monkeypatch, first_action, second_action):
    """Both real operations overlap; the named first owns the DB write lock."""
    held, release, second_started, second_attempted = (threading.Event() for _ in range(4))
    original = service.begin_profile_transaction
    errors = []
    def gated_lock(speaker_id, user_id):
        if threading.current_thread().name == 'manual-race-second':
            second_attempted.set()
        result = original(speaker_id, user_id)
        if threading.current_thread().name == 'manual-race-first' and not held.is_set():
            held.set()
            assert release.wait(5), 'Race controller did not release the first DB lock'
        return result
    monkeypatch.setattr(service, 'begin_profile_transaction', gated_lock)
    def run(action, second=False):
        with app.app_context():
            try:
                if second: second_started.set()
                action()
            except Exception as error:
                errors.append(error)
                db.session.rollback()
            finally:
                db.session.remove()
    first = threading.Thread(target=run, name='manual-race-first', args=(first_action,))
    second = threading.Thread(target=run, name='manual-race-second', args=(second_action, True))
    first.start()
    try:
        assert held.wait(5), 'First operation never acquired the real DB profile lock'
        second.start()
        assert second_started.wait(5)
        assert second_attempted.wait(5), 'Second operation did not contend for the first operation\'s DB lock'
    finally:
        release.set()
    first.join(10)
    second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert not errors, [repr(error) for error in errors]
    db.session.expire_all()


@pytest.mark.parametrize('first', ['commit', 'merge'])
def test_merge_commit_race_and_consumed_retry_no_resurrection(fixture, monkeypatch, first):
    from src.services.speaker_merge import merge_speakers
    app, user, source, rec, path, jobs = fixture
    target = Speaker(user_id=user.id, name='Target')
    db.session.add(target)
    db.session.commit()
    uid, sid, tid, old_created = user.id, source.id, target.id, source.created_at
    job, _, _ = ready(fixture)
    outcomes, sample_ids = [], []
    def commit():
        try:
            sample, created = service.commit_sample(uid, sid, job, lambda _: str(path))
            assert created
            sample_ids.append(sample.id)
            outcomes.append('committed')
        except service.ManualVoiceError as error:
            outcomes.append(error.code)
            db.session.rollback()
    def merge():
        assert merge_speakers(tid, [sid], uid).id == tid
    actions = {'commit': commit, 'merge': merge}
    second = 'merge' if first == 'commit' else 'commit'
    ordered_profile_race(app, monkeypatch, actions[first], actions[second])
    assert db.session.get(Speaker, sid) is None
    rows = ManualVoiceSample.query.all()
    if first == 'commit':
        assert outcomes == ['committed']
        assert len(rows) == 1 and rows[0].speaker_id == tid and rows[0].id == sample_ids[0]
        receipt = db.session.get(ManualVoiceSampleReceipt, job)
        assert receipt.sample_id == sample_ids[0] and receipt.initial_speaker_id == sid
        assert receipt.initial_speaker_created_at == old_created
    else:
        assert outcomes == ['missing'] and rows == []
        assert db.session.get(ManualVoiceSampleReceipt, job) is None
    count = len(rows)
    c = client(app, user)
    # Neither old profile nor target may turn the old preparation into a new sample.
    for speaker_id in (sid, tid):
        assert c.post(f'/speakers/{speaker_id}/manual_voice_samples', json={'job_id': job}).status_code == 404
        assert ManualVoiceSample.query.count() == count
    replacement = Speaker(id=sid, user_id=uid, name='Reused source identity',
                          created_at=old_created + timedelta(seconds=1))
    db.session.add(replacement)
    db.session.commit()
    assert c.post(f'/speakers/{sid}/manual_voice_samples', json={'job_id': job}).status_code == 404
    assert ManualVoiceSample.query.count() == count


@pytest.mark.parametrize('first', ['commit', 'delete'])
@pytest.mark.parametrize('deletion', ['single', 'bulk'])
def test_profile_deletion_commit_race_and_sqlite_id_reuse(fixture, monkeypatch, first, deletion):
    app, user, speaker, rec, path, jobs = fixture
    from src.api.speakers import speakers_bp
    app.register_blueprint(speakers_bp)
    uid, sid, old_created = user.id, speaker.id, speaker.created_at
    job, _, _ = ready(fixture)
    outcomes = []
    def commit():
        try:
            _, created = service.commit_sample(uid, sid, job, lambda _: str(path))
            assert created
            outcomes.append('committed')
        except service.ManualVoiceError as error:
            outcomes.append(error.code)
            db.session.rollback()
    def delete():
        current_user = db.session.get(User, uid)
        route = f'/speakers/{sid}' if deletion == 'single' else '/speakers/delete_all'
        response = client(app, current_user).delete(route)
        assert response.status_code == 200, response.get_json()
    actions = {'commit': commit, 'delete': delete}
    ordered_profile_race(app, monkeypatch, actions[first], actions['delete' if first == 'commit' else 'commit'])
    assert outcomes == ['committed' if first == 'commit' else 'missing']
    assert db.session.get(Speaker, sid) is None
    assert ManualVoiceSample.query.count() == 0
    assert ManualVoiceSampleReceipt.query.count() == (1 if first == 'commit' else 0)
    replacement = Speaker(user_id=uid, name='Replacement', created_at=old_created + timedelta(seconds=1))
    db.session.add(replacement)
    db.session.commit()
    assert replacement.id == sid, 'Fixture must exercise real SQLite ID reuse'
    assert client(app, user).post(f'/speakers/{sid}/manual_voice_samples', json={'job_id': job}).status_code == 404
    assert ManualVoiceSample.query.count() == 0


def test_default_namespaces_isolate_same_ids_across_databases_and_app_contexts(tmp_path, monkeypatch):
    """Real API status/audio/cancel/clear in app B cannot touch app A's clip."""
    import shutil
    from src.api.manual_voice_samples import manual_voice_samples_bp
    from src.api.speakers import speakers_bp
    from src.api import recordings
    registry = service.InstallationJobs()
    monkeypatch.setattr(service, 'manual_jobs', registry)
    monkeypatch.setattr(service, 'check_space', lambda _: None)
    monkeypatch.setattr(recordings, 'has_recording_access', lambda rec, user, require_edit=False: rec.user_id == user.id)
    monkeypatch.setattr(vp, 'current_space_id', lambda: None)
    monkeypatch.setattr(vp, 'legacy_space_id', lambda: None)
    monkeypatch.setattr('src.api.manual_voice_samples.local_path', lambda rec: rec.audio_path)
    apps, managers, owned_roots = [], [], []
    job_id = str(uuid.uuid4())
    try:
        for number in (1, 2):
            app = Flask(f'manual-installation-{number}')
            app.config.update(TESTING=True, SECRET_KEY='same-secret-across-two-installations', WTF_CSRF_ENABLED=False,
                              SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / f'app-{number}.db'))
            db.init_app(app)
            login = LoginManager(app)
            login.user_loader(lambda uid: db.session.get(User, int(uid)))
            app.register_blueprint(manual_voice_samples_bp)
            app.register_blueprint(speakers_bp)
            apps.append(app)
            with app.app_context():
                db.create_all()
                user = User(username='same-owner', email='same@installation.test', password='x')
                db.session.add(user)
                db.session.commit()
                speaker = Speaker(user_id=user.id, name='same-person')
                path = tmp_path / f'source-{number}.wav'
                wav_file(path, rate=16000, channels=1)
                rec = Recording(user_id=user.id, title=f'Private source {number}', audio_path=str(path))
                db.session.add_all([speaker, rec])
                db.session.commit()
                assert user.id == speaker.id == rec.id == 1
                manager = registry.manager()
                managers.append(manager)
                owned_roots.append(manager.root)
                assert service.ManualJobs().root == manager.root
                manager._prepare()
                if number == 1:
                    directory = manager.directory(job_id)
                    directory.mkdir(mode=0o700)
                    work = directory / 'work'
                    work.mkdir(mode=0o700)
                    worker.crop_pcm(path, work, 0, 20000)
                    digest, size = worker.source_digest(path)
                    manager._write(directory, {'job_id': job_id, 'user_id': 1, 'speaker_id': 1, 'recording_id': 1,
                        'speaker_created_at': speaker.created_at.isoformat(), 'path': str(path),
                        'signature': service.source_signature(path), 'state': 'ready', 'finished': time.time(),
                        'start_ms': 0, 'end_ms': 20000, 'space': {'space_id': 1},
                        'result': {'speech_ms': 20000, 'vector': [1, 0, 0], 'source_size': size,
                                   'source_audio_sha256': digest}})
                db.session.remove()
        assert managers[0] is not managers[1] and owned_roots[0] != owned_roots[1]
        base = f'/recordings/1/manual_voice_samples/preparations/{job_id}'
        with apps[1].app_context():
            second = client(apps[1], db.session.get(User, 1))
            assert second.get(base).status_code == 404
            assert second.get(base + '/audio', headers={'Range': 'bytes=0-43'}).status_code == 404
            assert second.delete(base).status_code == 404
            assert second.post('/speakers/1/clear_embeddings', json={}).status_code == 200
            assert registry.manager() is managers[1]
        with apps[0].app_context():
            assert registry.manager() is managers[0]
            first = client(apps[0], db.session.get(User, 1))
            assert first.get(base).json['state'] == 'ready'
            assert first.get(base + '/audio', headers={'Range': 'bytes=0-43'}).status_code == 206
            assert managers[0].root == owned_roots[0], 'A worker manager root must never be reassigned'
            previous = service.default_job_root()
            apps[0].config['SECRET_KEY'] = 'rotated-secret'
            assert service.default_job_root() != previous
            assert 'rotated-secret' not in str(service.default_job_root())
    finally:
        for root in owned_roots:
            assert root.parent == Path(service.tempfile.gettempdir())
            assert root.name.startswith('speakr-manual-voice-ranges-v1-')
            shutil.rmtree(root, ignore_errors=True)


def test_same_database_and_installation_share_stable_default_namespace(tmp_path):
    apps = [Flask('worker-a'), Flask('worker-b')]
    registry = service.InstallationJobs()
    managers = []
    for app in apps:
        app.config.update(SECRET_KEY='shared-worker-secret', SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / 'shared.db'))
        db.init_app(app)
        with app.app_context():
            managers.append(registry.manager())
    assert managers[0] is managers[1]


def test_same_memory_url_secret_and_instance_separate_engine_namespaces(monkeypatch):
    import shutil
    apps = [Flask('memory-worker-a'), Flask('memory-worker-b')]
    registry = service.InstallationJobs()
    monkeypatch.setattr(service, 'manual_jobs', registry)
    managers = []
    job = str(uuid.uuid4())
    try:
        for app in apps:
            app.config.update(SECRET_KEY='identical-memory-secret', SQLALCHEMY_DATABASE_URI='sqlite:///:memory:')
            db.init_app(app)
            with app.app_context():
                manager = registry.manager()
                managers.append(manager)
                manager._prepare()
                if len(managers) == 1:
                    directory = manager.directory(job)
                    directory.mkdir(mode=0o700)
                    manager._write(directory, {'job_id': job, 'user_id': 1, 'speaker_id': 1, 'recording_id': 1,
                        'state': 'ready', 'finished': time.time(), 'start_ms': 0, 'end_ms': 20000,
                        'space': {'space_id': 1}, 'result': {'speech_ms': 20000}})
        assert apps[0].instance_path == apps[1].instance_path
        assert managers[0].root != managers[1].root
        with apps[1].app_context():
            with pytest.raises(service.ManualVoiceError, match='missing'):
                with service.manual_jobs.locked(job): pass
            service.invalidate_profile(1, 1)
        with apps[0].app_context():
            assert registry.manager() is managers[0], 'The same memory engine must retain its namespace'
            with service.manual_jobs.locked(job) as (_, meta):
                assert meta['state'] == 'ready'
    finally:
        for manager in managers:
            assert manager.root.parent == Path(service.tempfile.gettempdir())
            assert manager.root.name.startswith('speakr-manual-voice-ranges-v1-')
            shutil.rmtree(manager.root, ignore_errors=True)


def transient_directory(manager, job_id=None, state='ready', expired=False):
    manager._prepare()
    job_id = job_id or str(uuid.uuid4())
    directory = manager.directory(job_id)
    directory.mkdir(mode=0o700)
    work = directory / 'work'
    work.mkdir(mode=0o700)
    (work / 'clip.wav').write_bytes(b'private clip fixture')
    now = time.time()
    meta = {'job_id': job_id, 'state': state, 'user_id': 1, 'speaker_id': 1, 'recording_id': 1,
        'start_ms': 0, 'end_ms': 20000, 'space': {'space_id': 1}, 'result': {'speech_ms': 20000}}
    if state == 'preparing':
        meta.update(parent_pid=os.getpid(), parent_birth=service.process_identity(os.getpid()))
    else:
        meta.update(finished=now - (901 if expired else 10), expires_at=now + (-1 if expired else 890))
    manager._write(directory, meta)
    return directory


def test_blueprint_registration_purges_restart_without_prepare_or_database_tables(tmp_path, monkeypatch):
    import shutil
    from src.api.manual_voice_samples import manual_voice_samples_bp
    app = Flask('restart-before-db-tables')
    app.config.update(SECRET_KEY='restart-fixture', SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / 'restart.db'))
    db.init_app(app)
    registry = service.InstallationJobs()
    monkeypatch.setattr(service, 'manual_jobs', registry)
    with app.app_context():
        old = service.ManualJobs()
        expired = transient_directory(old, expired=True)
        live = transient_directory(old, state='preparing')
        cancelled = transient_directory(old, state='cancelled')
        snapshot = (cancelled / 'meta.json').read_bytes()
    try:
        with patch('src.services.voice_embedding_check.probe_backend', side_effect=AssertionError('hidden upload')), \
                patch.object(service, 'purge_expired_receipts', side_effect=AssertionError('startup must not access tables')):
            app.register_blueprint(manual_voice_samples_bp)
        assert not expired.exists(), 'Idle startup must physically delete expired WAV and metadata'
        assert (live / 'work' / 'clip.wav').exists()
        assert (cancelled / 'meta.json').read_bytes() == snapshot
        with app.app_context():
            assert db.inspect(db.engine).get_table_names() == []
            assert registry.manager()._janitor_started
    finally:
        assert old.root.parent == Path(service.tempfile.gettempdir())
        shutil.rmtree(old.root, ignore_errors=True)


def test_secret_rotation_sweeps_only_owned_expired_family_preserves_original_ttl(tmp_path):
    import shutil
    app = Flask('secret-rotation')
    app.config.update(SECRET_KEY='first-secret', SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / 'rotation.db'))
    db.init_app(app)
    roots = []
    with app.app_context():
        old = service.ManualJobs()
        roots.append(old.root)
        expired = transient_directory(old, expired=True)
        ready_directory = transient_directory(old)
        tombstone = transient_directory(old, state='cancelled')
        snapshots = {path: (path / 'meta.json').read_bytes() for path in [ready_directory, tombstone]}
        family = old.family
        app.config['SECRET_KEY'] = 'rotated-secret'
        current = service.ManualJobs(ttl=1)
        assert current.family == family and current.root != old.root
        # A namespace-shaped path without this family's exact owner marker
        # does not grant sweep authority, even when its job is expired.
        foreign_root = old.root.parent / ('speakr-manual-voice-ranges-v1-' + family + '-' + uuid.uuid4().hex)
        roots.append(foreign_root)
        foreign = service.ManualJobs(root=foreign_root, family='foreign-family')
        foreign_job = transient_directory(foreign, expired=True)
        try:
            current.sweep_owned()
            assert not expired.exists()
            assert (foreign_job / 'work' / 'clip.wav').exists()
            for path, before in snapshots.items():
                assert (path / 'meta.json').read_bytes() == before
            short_ttl = service.ManualJobs(root=old.root, family=family, ttl=1)
            short_ttl.sweep_owned()
            assert tombstone.exists(), 'A restarted shorter TTL must preserve the original absolute tombstone expiry'
        finally:
            for root in roots:
                assert root.parent == Path(service.tempfile.gettempdir())
                shutil.rmtree(root, ignore_errors=True)


def test_owned_sweep_rejects_metadata_lock_and_root_symlinks(tmp_path):
    manager = service.ManualJobs(root=tmp_path / 'owned')
    directory = transient_directory(manager, expired=True)
    metadata = directory / 'meta.json'
    foreign_meta = tmp_path / 'foreign.json'
    foreign_meta.write_bytes(metadata.read_bytes())
    metadata.unlink()
    metadata.symlink_to(foreign_meta)
    manager.sweep_owned()
    assert directory.exists() and foreign_meta.exists()
    metadata.unlink()
    metadata.write_bytes(foreign_meta.read_bytes())
    foreign_lock = tmp_path / 'foreign-lock'
    foreign_lock.write_bytes(b'foreign untouched')
    lock = manager.root / '.metadata.lock'
    if lock.exists(): lock.unlink()
    lock.symlink_to(foreign_lock)
    manager.sweep_owned()
    assert directory.exists() and foreign_lock.read_bytes() == b'foreign untouched'
    linked = service.ManualJobs(root=tmp_path / 'linked')
    linked.root.symlink_to(manager.root, target_is_directory=True)
    linked.sweep_owned()
    assert directory.exists()


def test_owned_sweep_rotates_bounded_job_window(tmp_path):
    manager = service.ManualJobs(root=tmp_path / 'bounded')
    retained = [transient_directory(manager, str(uuid.UUID(int=number))) for number in range(1, 201)]
    expired = transient_directory(manager, str(uuid.UUID(int=201)), expired=True)
    manager.sweep_owned(job_limit=200)
    assert expired.exists(), 'First bounded window should inspect only the first 200 nonexpired jobs'
    manager.sweep_owned(job_limit=200)
    assert not expired.exists(), 'Periodic cursor must eventually reach jobs beyond a nonexpired prefix'
    assert all(path.exists() for path in retained)


def test_restart_sweep_does_not_renew_orphaned_worker_retention(tmp_path):
    old = service.ManualJobs(root=tmp_path / 'orphaned')
    directory = transient_directory(old, state='preparing')
    with old.locked(directory.name) as (_, metadata):
        metadata.update(parent_pid=999999999, parent_birth='dead-owner', created=time.time() - 1300, deadline=300)
        old._write(directory, metadata)
    restarted = service.ManualJobs(root=old.root)
    restarted.sweep_owned()
    assert not directory.exists(), 'An old orphan must expire at created+deadline+original TTL, not restart+TTL'


def test_cleanup_family_excludes_database_credentials_and_app_secret(monkeypatch):
    monkeypatch.setenv('SQLALCHEMY_DATABASE_URI', 'postgresql://fixture-user:first-password@fixture.invalid/fixture-db')
    monkeypatch.setenv('SECRET_KEY', 'first-app-secret')
    root1, family1 = service.default_job_scope()
    monkeypatch.setenv('SQLALCHEMY_DATABASE_URI', 'postgresql://rotated-user:second-password@fixture.invalid/fixture-db')
    monkeypatch.setenv('SECRET_KEY', 'second-app-secret')
    root2, family2 = service.default_job_scope()
    assert family1 == family2 and root1 != root2
    monkeypatch.setenv('SQLALCHEMY_DATABASE_URI', 'postgresql+psycopg2://third-user@fixture.invalid/fixture-db?password=third-password')
    _, family3 = service.default_job_scope()
    assert family3 == family1, 'Driver/auth query changes must not orphan owned TTL data from the same database'
    assert 'password' not in str(root1) + str(root2)


def test_registration_cleanup_acl_timeout_preserves_files_and_allows_startup(tmp_path, monkeypatch):
    import shutil
    import subprocess
    from src.api.manual_voice_samples import manual_voice_samples_bp
    app = Flask('cleanup-acl-timeout')
    app.config.update(SECRET_KEY='timeout-fixture', SQLALCHEMY_DATABASE_URI='sqlite:///' + str(tmp_path / 'timeout.db'))
    db.init_app(app)
    registry = service.InstallationJobs()
    monkeypatch.setattr(service, 'manual_jobs', registry)
    with app.app_context():
        old = service.ManualJobs()
        expired = transient_directory(old, expired=True)
    try:
        with patch.object(service.ManualJobs, '_prepare', side_effect=subprocess.TimeoutExpired('private-acl', 10)):
            app.register_blueprint(manual_voice_samples_bp)
        assert (expired / 'work' / 'clip.wav').exists(), 'Cleanup timeout must defer deletion and keep the application available'
        with app.app_context():
            assert registry.manager()._janitor_started
    finally:
        assert old.root.parent == Path(service.tempfile.gettempdir())
        shutil.rmtree(old.root, ignore_errors=True)


@pytest.mark.parametrize('purge_phase', ['before_commit', 'after_snapshot'])
def test_commit_after_physical_expiry_purge_returns_410_without_recreation(fixture, purge_phase):
    app, user, speaker, rec, path, jobs = fixture
    job, directory, metadata = ready(fixture)
    def expire_and_purge():
        with jobs.locked(job) as (_, current):
            current['expires_at'] = time.time() - 1
            jobs._write(directory, current)
        jobs.sweep_owned()
        assert not directory.exists()
    if purge_phase == 'before_commit':
        expire_and_purge()
    def authorize(_):
        if purge_phase == 'after_snapshot':
            expire_and_purge()
        return str(path)
    with pytest.raises(service.ManualVoiceError) as error:
        service.commit_sample(user.id, speaker.id, job, authorize)
    assert error.value.code == 'expired' and error.value.status == 410
    db.session.rollback()
    assert ManualVoiceSample.query.count() == 0 and ManualVoiceSampleReceipt.query.count() == 0
