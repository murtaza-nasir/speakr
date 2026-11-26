"""Byte ranges and versions of the media routes (#703, #707).

A range past the end of a file is a 416 with `Content-Range: bytes */<length>`
on every route that serves a recording's audio, never a 500. A range inside
the file stays a 206.

The players address the audio as <route>?v=<audio_revision>. A range request
under an earlier revision whose If-Range names an earlier file is a 410, so
that no player receives a 200 of the whole new file in answer to a range of
the old one. Any other request is served the current file.
"""

import shutil
import struct
import uuid
import wave
from dataclasses import dataclass
from typing import Optional
from unittest import mock

import pytest

from tests.audit_log_support import (  # noqa: E402
    anonymous_client, api_token, app, audit, bearer, client_for, db, make_recording, make_user,
    remove_users, reset_audit_settings, set_setting,
)
from src.models import Recording, Share, SystemSetting  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True


@pytest.fixture(autouse=True)
def created():
    with app.app_context():
        reset_audit_settings()
        audit.flush()
        audit.reset_memory()
        users = []
        yield users
        audit.flush()
        db.session.rollback()
        SystemSetting.set_setting('enable_public_sharing', None, None, 'boolean')
        remove_users(users)


def _wav(tmp_path):
    path = tmp_path / f'{uuid.uuid4().hex}.wav'
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(struct.pack('<h', 0) * 8000)
    return str(path)


def _recording(created, tmp_path):
    owner = make_user(created)
    path = _wav(tmp_path)
    recording = make_recording(owner, audio_path=path, mime_type='audio/wav', file_size=_size(path))
    return owner, recording, _size(path)


def _size(path):
    import os
    return os.path.getsize(path)


def _past_end(size):
    return {'Range': f'bytes={size}-{size}'}


def _assert_416(resp, size):
    assert resp.status_code == 416, (resp.status_code, resp.get_data(as_text=True)[:200])
    assert resp.headers.get('Content-Range') == f'bytes */{size}'
    assert b'unexpected_error' not in resp.get_data()


def _share(owner, recording):
    set_setting('enable_public_sharing', 'true')
    share = Share(recording_id=recording.id, user_id=owner.id)
    db.session.add(share)
    db.session.commit()
    return share.public_id


# --- #703: a range past the end ------------------------------------------------

def test_a_range_past_the_audio_file_is_a_416_and_a_range_inside_it_a_206(created, tmp_path):
    owner, recording, size = _recording(created, tmp_path)
    client = client_for(owner)
    resp = client.get(f'/audio/{recording.id}', headers={'Range': 'bytes=0-99'})
    assert resp.status_code == 206
    assert resp.headers['Content-Range'] == f'bytes 0-99/{size}'
    resp.close()
    _assert_416(client.get(f'/audio/{recording.id}', headers=_past_end(size)), size)
    _assert_416(client.get(f'/audio/{recording.id}?download=true', headers=_past_end(size)), size)
    resp = client.get(f'/audio/{recording.id}?download=true')
    assert resp.status_code == 200
    resp.close()


def test_a_range_past_the_peaks_file_is_a_416(created, tmp_path):
    from src.services.waveform_peaks import PeaksResult
    owner, recording, size = _recording(created, tmp_path)
    peaks = PeaksResult(status='ready', path=recording.audio_path, etag='t')
    with mock.patch('src.services.waveform_peaks.get_peaks', return_value=peaks):
        _assert_416(client_for(owner).get(f'/audio/{recording.id}/peaks', headers=_past_end(size)), size)


def test_a_range_past_the_speaker_snippet_is_a_416_and_the_snippet_file_is_removed(created, tmp_path):
    owner, recording, size = _recording(created, tmp_path)
    written = []

    def extract(source, output, start, duration):
        shutil.copyfile(source, output)
        written.append(output)

    with mock.patch('src.api.speakers.extract_audio_segment', side_effect=extract), \
            mock.patch('src.api.speakers.get_codec_info', return_value={'audio_codec': 'pcm_s16le'}):
        client = client_for(owner)
        url = f'/speakers/snippet-audio/{recording.id}?start=0&duration=1'
        resp = client.get(url, headers={'Range': 'bytes=0-9'})
        assert resp.status_code == 206
        resp.close()
        _assert_416(client.get(url, headers=_past_end(size)), size)
    import os
    assert written and not os.path.exists(written[-1])


def test_a_range_past_the_audio_file_is_a_416_on_api_v1(created, tmp_path):
    owner, recording, size = _recording(created, tmp_path)
    plain, _ = api_token(owner, scopes='["read"]')
    resp = anonymous_client().get(f'/api/v1/recordings/{recording.id}/audio',
                                  headers={**bearer(plain), **_past_end(size)})
    _assert_416(resp, size)


def test_a_range_past_the_audio_file_is_a_416_on_a_public_link(created, tmp_path):
    owner, recording, size = _recording(created, tmp_path)
    public_id = _share(owner, recording)
    resp = anonymous_client().get(f'/share/audio/{public_id}', headers={'Range': 'bytes=0-9'})
    assert resp.status_code == 206
    resp.close()
    _assert_416(anonymous_client().get(f'/share/audio/{public_id}', headers=_past_end(size)), size)


@dataclass
class _Redirect:
    mode: str = 'redirect_url'
    url: Optional[str] = 'https://s3.example/signed'
    local_path: Optional[str] = None


class _S3:
    def get_audio_delivery(self, *args, **kwargs):
        return _Redirect()


def test_audio_on_s3_is_a_redirect_whatever_the_range(created, tmp_path):
    owner = make_user(created)
    recording = make_recording(owner, audio_path='s3://bucket/a.mp3', mime_type='audio/mpeg', file_size=1000)
    with mock.patch('src.api.recordings.get_storage_service', return_value=_S3()):
        resp = client_for(owner).get(f'/audio/{recording.id}', headers=_past_end(1000))
    assert resp.status_code == 302
    assert resp.headers['Location'] == 'https://s3.example/signed'


# --- #707: the version of the stored file ----------------------------------------

def test_the_audio_revision_changes_with_the_stored_file(created, tmp_path):
    owner, recording, _ = _recording(created, tmp_path)
    first = recording.audio_revision
    assert first and recording.to_dict()['audio_revision'] == first
    assert recording.to_list_dict()['audio_revision'] == first
    status = client_for(owner).get(f'/recording/{recording.id}/status').get_json()
    assert status['audio_revision'] == first
    # The convert job: another locator, size and type
    recording.audio_path = _wav(tmp_path)
    recording.mime_type = 'audio/mpeg'
    db.session.commit()
    second = recording.audio_revision
    assert second != first
    # A remux in place: the same locator, another size
    recording.file_size = (recording.file_size or 0) + 1
    db.session.commit()
    assert recording.audio_revision not in (first, second)
    assert make_recording(owner, audio_path=None).audio_revision is None


def test_the_player_address_of_the_current_file_is_served(created, tmp_path):
    owner, recording, size = _recording(created, tmp_path)
    client = client_for(owner)
    url = f'/audio/{recording.id}?v={recording.audio_revision}'
    resp = client.get(url, headers={'Range': 'bytes=0-9'})
    assert resp.status_code == 206
    assert resp.headers['X-Audio-Revision'] == recording.audio_revision
    etag = resp.headers['ETag']
    resp.close()
    resp = client.get(url, headers={'Range': 'bytes=10-19', 'If-Range': etag})
    assert resp.status_code == 206 and resp.headers['Content-Range'] == f'bytes 10-19/{size}'
    resp.close()


def test_a_stale_version_that_continues_the_old_file_is_a_410(created, tmp_path):
    owner, recording, size = _recording(created, tmp_path)
    client = client_for(owner)
    old_url = f'/audio/{recording.id}?v={recording.audio_revision}'
    resp = client.get(old_url, headers={'Range': 'bytes=0-9'})
    etag = resp.headers['ETag']
    resp.close()
    # The convert job stores another file under the same recording id
    recording.audio_path = _wav(tmp_path)
    recording.file_size = size
    recording.mime_type = 'audio/mpeg'
    db.session.commit()
    current = recording.audio_revision
    for headers in ({'Range': f'bytes=100-{size - 1}', 'If-Range': etag},
                    {'Range': f'bytes=100-{size - 1}', 'If-Range': '"stale"'}):
        resp = client.get(old_url, headers=headers)
        assert resp.status_code == 410, (headers, resp.status_code)
        assert resp.headers['X-Audio-Revision'] == current
        assert resp.get_json() == {'audio_revision': current}
        assert 'Content-Range' not in resp.headers
    # A fresh load of the old address is the current file
    resp = client.get(old_url, headers={'Range': 'bytes=0-'})
    assert resp.status_code == 206 and resp.headers['X-Audio-Revision'] == current
    current_etag = resp.headers['ETag']
    resp.close()
    # A player that loaded the current file under the old address continues it
    for headers in ({'Range': 'bytes=100-199', 'If-Range': current_etag}, {'Range': 'bytes=100-199'}):
        resp = client.get(old_url, headers=headers)
        assert resp.status_code == 206 and resp.headers['Content-Range'] == f'bytes 100-199/{size}'
        resp.close()
    # Without a version the stale If-Range is answered as HTTP says: the whole file
    resp = client.get(f'/audio/{recording.id}', headers={'Range': 'bytes=100-199', 'If-Range': '"stale"'})
    assert resp.status_code == 200 and 'Content-Range' not in resp.headers
    resp.close()
    # A download is never refused for a version
    resp = client.get(f'/audio/{recording.id}?download=true&v=old', headers={'Range': 'bytes=100-199'})
    assert resp.status_code == 206
    resp.close()


def test_a_stale_version_is_a_410_before_the_redirect_to_s3(created):
    owner = make_user(created)
    recording = make_recording(owner, audio_path='s3://bucket/a.mp3', mime_type='audio/mpeg', file_size=1000)
    with mock.patch('src.api.recordings.get_storage_service', return_value=_S3()):
        client = client_for(owner)
        resp = client.get(f'/audio/{recording.id}?v=old', headers={'Range': 'bytes=500-', 'If-Range': '"x"'})
        assert resp.status_code == 410
        current = client.get(f'/audio/{recording.id}?v={recording.audio_revision}',
                             headers={'Range': 'bytes=500-', 'If-Range': '"x"'})
        assert current.status_code == 302


def test_a_stale_version_on_a_public_link_is_a_410(created, tmp_path):
    owner, recording, size = _recording(created, tmp_path)
    public_id = _share(owner, recording)
    old = recording.audio_revision
    resp = anonymous_client().get(f'/share/audio/{public_id}?v={old}', headers={'Range': 'bytes=0-9'})
    assert resp.status_code == 206 and resp.headers['X-Audio-Revision'] == old
    resp.close()
    recording.audio_path = _wav(tmp_path)
    db.session.commit()
    resp = anonymous_client().get(f'/share/audio/{public_id}?v={old}',
                                  headers={'Range': 'bytes=100-199', 'If-Range': '"stale"'})
    assert resp.status_code == 410
    assert resp.headers['X-Audio-Revision'] == recording.audio_revision != old
    # The page gives its player the current version
    page = anonymous_client().get(f'/share/{public_id}').get_data(as_text=True)
    assert f'"audio_revision": "{recording.audio_revision}"' in page
