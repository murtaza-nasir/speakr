"""Real clipped spectra, access control and resource-bound regressions."""
import io
import json
import subprocess
import uuid
import wave
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image
from src.services import segment_spectrogram as spectra
from src.app import app, db
from src.models import User, Recording, InternalShare


@pytest.mark.parametrize('value', [None, True, False, -5, 45, 1, float('nan'), float('inf'), '20;evil', ''])
def test_invalid_visual_gain_is_rejected(value):
    with pytest.raises(spectra.SpectrogramError) as error:
        spectra.validate_gain(value)
    assert error.value.code == 'gain' and error.value.status == 400


def test_quiet_signal_gain_preserves_silence_coordinates_and_audio(tmp_path):
    import hashlib
    rate = 16000
    t = np.arange(rate * 4) / rate
    signal = np.where(t < 2, np.sin(2 * np.pi * 1000 * t) * .001, 0)
    path = tmp_path / 'quiet.wav'
    with wave.open(str(path), 'wb') as file:
        file.setnchannels(1); file.setsampwidth(2); file.setframerate(rate)
        file.writeframes((signal * 32767).astype('<i2').tobytes())
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    pictures = [np.asarray(Image.open(io.BytesIO(spectra.render_spectrogram(str(path), 0, 4, gain_db=gain).png)).convert('RGB')) for gain in (0, 20, 40)]
    # The 1kHz band stays at the same frequency; black digital silence is not lifted.
    levels = [image[215:231, 50:450].mean() for image in pictures]
    assert levels[1] > levels[0] * 1.3 and levels[2] > levels[1]
    for image in pictures:
        assert image[:, 650:950].max() == 0
        assert abs(image[:, 200].max(axis=1).argmax() - 224) < 6
    assert hashlib.sha256(path.read_bytes()).hexdigest() == fingerprint


@pytest.fixture
def audio_file(tmp_path):
    rate = 16000
    t = np.arange(rate * 8) / rate
    signal = np.where(t < 2, np.sin(2 * np.pi * 400 * t),
             np.where((t >= 3) & (t < 5), np.sin(2 * np.pi * 2000 * t), 0)) * 0.6
    path = tmp_path / 'tones.wav'
    with wave.open(str(path), 'wb') as file:
        file.setnchannels(1); file.setsampwidth(2); file.setframerate(rate)
        file.writeframes((signal * 32767).astype('<i2').tobytes())
    return path


@pytest.mark.parametrize('extension,codec', [('wav', None), ('m4a', 'aac'), ('webm', 'libopus')])
def test_clipped_seek_time_and_frequency_are_real(audio_file, tmp_path, extension, codec):
    path = audio_file
    if codec:
        path = tmp_path / f'encoded.{extension}'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(audio_file), '-c:a', codec, str(path)], check=True)
    result = spectra.render_spectrogram(str(path), 1, 5)
    assert result.start == 1 and result.end == 5 and result.channels == 1
    image = np.asarray(Image.open(io.BytesIO(result.png)).convert('RGB'))
    assert image.shape == (256, 1024, 3)
    intensity = image.max(axis=2).max(axis=0)
    # The gap at absolute 2–3 s lies at 25–50% of this nonzero-seek window.
    assert np.median(intensity[320:450]) < np.median(intensity[50:150]) * 0.5
    assert np.median(intensity[320:450]) < np.median(intensity[600:800]) * 0.5
    # At 4 s the 2 kHz tone is ~1/4 of the 8 kHz range above the bottom.
    peak = image[:, 700, :].max(axis=1).argmax()
    assert abs(peak - 192) < 15


def test_eof_clipping_and_stereo_do_not_mix_channels(audio_file, tmp_path):
    stereo = tmp_path / 'stereo.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(audio_file), '-af',
                    'pan=stereo|c0=c0|c1=-1*c0', str(stereo)], check=True)
    result = spectra.render_spectrogram(str(stereo), 1, 9)
    assert result.end == 8 and result.channels == 2
    image = np.asarray(Image.open(io.BytesIO(result.png)).convert('RGB'))
    assert image.shape[:2] == (384, 1024)
    assert image[:192, :100].max() > 50 and image[192:, :100].max() > 50


@pytest.mark.parametrize('frequency,maximum', [('2000',2000),('4000',4000),('8000',8000),('full',24000)])
def test_frequency_presets_render_real_axis_and_filter_upper_tones(tmp_path,frequency,maximum):
    rate=48000;t=np.arange(rate*2)/rate
    signal=(np.sin(2*np.pi*1000*t)+np.sin(2*np.pi*6000*t)+np.sin(2*np.pi*12000*t))/4
    path=tmp_path/'wide.wav'
    with wave.open(str(path),'wb') as file:
        file.setnchannels(1);file.setsampwidth(2);file.setframerate(rate);file.writeframes((signal*32767).astype('<i2').tobytes())
    result=spectra.render_spectrogram(str(path),0,2,frequency)
    assert result.max_frequency==maximum and result.sample_rate==48000
    image=np.asarray(Image.open(io.BytesIO(result.png)).convert('RGB'))
    energy=image[:,500,:].max(axis=1)
    for tone in [1000,6000,12000]:
        if tone >= maximum:continue
        row=int(256*(1-tone/maximum));assert energy[max(0,row-8):min(256,row+8)].max()>60


def test_low_native_rate_uses_native_ceiling_and_invalid_presets_do_not_spawn(tmp_path):
    path=tmp_path/'low.wav'
    with wave.open(str(path),'wb') as file:
        file.setnchannels(1);file.setsampwidth(2);file.setframerate(8000);file.writeframes(b'\0'*16000)
    assert spectra.render_spectrogram(str(path),0,1).max_frequency==4000
    with patch.object(spectra,'_run') as run:
        with pytest.raises(spectra.SpectrogramError):spectra.render_spectrogram(str(path),0,1,'arbitrary=filter')
        run.assert_not_called()


@pytest.mark.parametrize('start,end', [(None, 1), ('nan', 2), (0, 'inf'), (-1, 1), (1, 1), (2, 1), (0, 60.1)])
def test_invalid_window_never_spawns_child(start, end):
    with patch.object(spectra.subprocess, 'run') as run:
        with pytest.raises(spectra.SpectrogramError) as error:
            spectra.render_spectrogram('/not/read', start, end)
        assert error.value.status == 400
        run.assert_not_called()


def test_busy_is_nonblocking_and_lock_is_retained_by_owner(audio_file):
    spectra._generation.acquire()
    try:
        with pytest.raises(spectra.SpectrogramError) as error:
            spectra.render_spectrogram(str(audio_file), 0, 1)
        assert error.value.status == 429
        assert not spectra._generation.acquire(blocking=False)
    finally:
        spectra._generation.release()


def test_timeout_releases_slot_and_run_uses_timeout(audio_file):
    with patch.object(spectra.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffprobe', 5)) as run:
        with pytest.raises(spectra.SpectrogramError) as error:
            spectra.render_spectrogram(str(audio_file), 0, 1)
        assert error.value.status == 504
        assert run.call_args.kwargs['timeout'] == 5
    assert spectra._generation.acquire(blocking=False)
    spectra._generation.release()


def test_corrupt_and_missing_media(audio_file, tmp_path):
    missing = tmp_path / 'missing'
    with pytest.raises(spectra.SpectrogramError) as error:
        spectra.render_spectrogram(str(missing), 0, 1)
    assert error.value.status == 404
    audio_file.write_bytes(b'not audio')
    with pytest.raises(spectra.SpectrogramError) as error:
        spectra.render_spectrogram(str(audio_file), 0, 1)
    assert error.value.code == 'media'


def test_source_change_is_rejected(audio_file):
    original = spectra._run
    def run(command, timeout):
        result = original(command, timeout)
        if command[0] == 'ffmpeg':
            with audio_file.open('ab') as file:
                file.write(b'changed')
        return result
    with patch.object(spectra, '_run', side_effect=run):
        with pytest.raises(spectra.SpectrogramError) as error:
            spectra.render_spectrogram(str(audio_file), 0, 1)
    assert error.value.status == 409


@pytest.fixture
def recording_users(audio_file):
    with app.app_context():
        suffix = uuid.uuid4().hex
        owner = User(username='spectra_owner_' + suffix, email=suffix + '@example.test', password='test')
        other = User(username='spectra_other_' + suffix, email='other_' + suffix + '@example.test', password='test')
        db.session.add_all([owner, other]); db.session.flush()
        recording = Recording(user_id=owner.id, title='Synthetic spectra', status='COMPLETED', audio_path=str(audio_file))
        db.session.add(recording); db.session.commit()
        ids = recording.id, owner.id, other.id
    yield ids
    with app.app_context():
        InternalShare.query.filter_by(recording_id=ids[0]).delete()
        db.session.delete(db.session.get(Recording, ids[0]))
        db.session.delete(db.session.get(User, ids[1])); db.session.delete(db.session.get(User, ids[2]))
        db.session.commit()


def client_for(user_id=None):
    client = app.test_client()
    if user_id:
        with client.session_transaction() as session:
            session['_user_id'] = str(user_id); session['_fresh'] = True
    return client


@pytest.mark.parametrize('gain', [None, True, False, -5, 45, 1, 'evil', '20', 20.0])
def test_prepare_rejects_invalid_gain(recording_users, gain):
    recording, owner, _ = recording_users
    with patch.dict(app.config, WTF_CSRF_ENABLED=False):
        response = client_for(owner).post(f'/api/recordings/{recording}/spectrogram/prepare', json={'start': 0, 'end': 2, 'span': 2, 'gainDb': gain})
    assert response.status_code == 400 and response.json['code'] == 'gain'


def test_legacy_spectrum_optional_gain_and_default_are_reported(recording_users):
    recording, owner, _ = recording_users
    client = client_for(owner)
    base = f'/api/recordings/{recording}/spectrogram?start=0&end=2'
    normal = client.get(base)
    bright = client.get(base + '&gainDb=20')
    assert normal.status_code == bright.status_code == 200
    assert normal.headers['X-Spectrogram-Gain-Db'] == '0'
    assert bright.headers['X-Spectrogram-Gain-Db'] == '20'
    assert normal.data != bright.data
    invalid = client.get(base + '&gainDb=999')
    assert invalid.status_code == 400 and invalid.json['code'] == 'gain'


def test_endpoint_owner_and_no_read_for_forbidden(recording_users):
    rid, owner, other = recording_users
    url = f'/api/recordings/{rid}/spectrogram?start=1&end=5'
    with patch.object(spectra, 'render_spectrogram') as render:
        assert client_for().get(url).status_code == 401
        assert client_for(other).get(url).status_code == 403
        render.assert_not_called()
    response = client_for(owner).get(url)
    assert response.status_code == 200 and response.mimetype == 'image/png'
    assert response.headers['X-Spectrogram-Start'] == '1.0'
    assert response.headers['Cache-Control'] == 'private, no-store'


def test_endpoint_deleted_remote_and_invalid_bounds(recording_users):
    rid, owner, _ = recording_users
    client = client_for(owner)
    url = f'/api/recordings/{rid}/spectrogram'
    with patch.object(spectra, 'render_spectrogram') as render:
        assert client.get(url + '?start=0&end=61').status_code == 400
        with app.app_context():
            db.session.get(Recording, rid).audio_path = 's3://bucket/object.wav'; db.session.commit()
        assert client.get(url + '?start=0&end=1').status_code == 501
        with app.app_context():
            db.session.get(Recording, rid).audio_path = None; db.session.commit()
        assert client.get(url + '?start=0&end=1').status_code == 404
        render.assert_not_called()


def test_internal_share_obeys_feature_flag(recording_users):
    import src.api.recordings as routes
    rid, owner, other = recording_users
    with app.app_context():
        share = InternalShare(recording_id=rid, shared_with_user_id=other, owner_id=owner)
        db.session.add(share); db.session.commit()
    url = f'/api/recordings/{rid}/spectrogram?start=0&end=1'
    with patch.object(routes, 'ENABLE_INTERNAL_SHARING', False):
        assert client_for(other).get(url).status_code == 403
    with patch.object(routes, 'ENABLE_INTERNAL_SHARING', True):
        assert client_for(other).get(url).status_code == 200


def test_http_busy_header_and_error_privacy(recording_users):
    rid, owner, _ = recording_users
    url = f'/api/recordings/{rid}/spectrogram?start=0&end=1'
    with patch.object(spectra, 'render_spectrogram', side_effect=spectra.SpectrogramError('busy', 429)):
        response = client_for(owner).get(url)
        assert response.status_code == 429 and response.headers['Retry-After'] == '2'
    with patch.object(spectra, 'render_spectrogram', side_effect=RuntimeError('secret path')):
        response = client_for(owner).get(url)
        assert response.status_code == 500 and 'secret path' not in response.get_data(as_text=True)


def test_prepared_api_cache_reads_recheck_share_and_do_not_write(recording_users, tmp_path):
    import time
    import src.api.recordings as routes
    from src.services import spectrogram_cache as module
    rid, owner, other = recording_users
    base = f'/api/recordings/{rid}/spectrogram'
    cache = module.SpectrogramCache(tmp_path / 'api-cache')
    with app.app_context():
        db.session.add(InternalShare(recording_id=rid, shared_with_user_id=other, owner_id=owner))
        db.session.commit()
        original = db.session.get(Recording, rid).to_dict()
    with patch.object(module, 'spectrogram_cache', cache), patch.object(routes, 'ENABLE_INTERNAL_SHARING', True), patch.dict(app.config, WTF_CSRF_ENABLED=False):
        assert client_for().post(base + '/prepare', json={'start': 1, 'end': 5, 'span': 1}).status_code == 401
        client = client_for(other)
        response = client.post(base + '/prepare', json={'start': 1, 'end': 5, 'frequency': '8000', 'span': 1})
        assert response.status_code == 202
        result = response.json
        url = base + '/preparations/' + result['id']
        query = {'lease': result['lease']}
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get(url, query_string=query).json
            if result['status'] == 'ready':
                break
            time.sleep(.02)
        assert result['status'] == 'ready'
        assert result['manifest']['tiles'][0]['url'] == url + '/tiles/0'
        with patch.object(spectra, '_run') as run:
            assert client.get(url + '/tiles/0', query_string=query).mimetype == 'image/png'
            assert client.post(url + '/lease', json=query).status_code == 200
            assert client.get(base + '/preparations/missing', query_string=query).status_code == 410
            with patch.object(module.threading.Thread, 'start') as start:
                cached = client.post(base + '/prepare', json={'start': 1, 'end': 5, 'frequency': '8000', 'span': 1, 'existing_id': result['id']})
                assert cached.status_code == 200 and cached.json['status'] == 'ready'
                for identifier in ('missing', '', None):
                    assert client.post(base + '/prepare', json={'start': 1, 'end': 5, 'frequency': '8000', 'span': 1, 'existing_id': identifier}).status_code == 410
                start.assert_not_called()
            with app.app_context():
                assert db.session.get(Recording, rid).to_dict() == original
                InternalShare.query.filter_by(recording_id=rid).delete(); db.session.commit()
                original = db.session.get(Recording, rid).to_dict()
            assert client.get(url, query_string=query).status_code == 403
            assert client.get(url + '/tiles/0', query_string=query).status_code == 403
            run.assert_not_called()
        with app.app_context():
            assert db.session.get(Recording, rid).to_dict() == original


def test_prepared_api_requires_csrf_for_prepare_and_lease(recording_users):
    rid, owner, _ = recording_users
    base = f'/api/recordings/{rid}/spectrogram'
    with patch.dict(app.config, WTF_CSRF_ENABLED=True):
        client = client_for(owner)
        assert client.post(base + '/prepare', json={'start': 1, 'end': 5, 'span': 1}).status_code == 400
        assert client.post(base + '/preparations/opaque/lease', json={'lease': 'token'}).status_code == 400
        assert client.delete(base + '/preparations/opaque/lease', json={'lease': 'token'}).status_code == 400
