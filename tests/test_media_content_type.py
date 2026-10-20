"""Content type of served recordings (#438).

The stored MIME type came from the uploaded file's extension, so an audio file
named ``.html`` was stored as ``text/html`` and opened as a page on the app's
own origin from a public share link. Only audio and video types are served as
such; anything else is served as ``application/octet-stream``, and every media
response carries ``Content-Security-Policy: sandbox``.
"""

import os
import tempfile
import uuid

import pytest

from src.app import app, db
from src.models import User, Recording, Share
from src.utils.mime import served_media_mime


@pytest.mark.parametrize('stored,expected', [
    ('audio/mpeg', 'audio/mpeg'),
    ('video/webm', 'video/webm'),
    ('audio/webm;codecs=opus', 'audio/webm;codecs=opus'),
    ('text/html', 'application/octet-stream'),
    ('image/svg+xml', 'application/octet-stream'),
    ('application/xhtml+xml', 'application/octet-stream'),
    ('', 'application/octet-stream'),
])
def test_served_media_mime(stored, expected):
    assert served_media_mime(stored) == expected


def test_without_a_stored_type_the_path_is_checked_under_the_same_rule():
    assert served_media_mime(None, '/x/a.mp3') == 'audio/mpeg'
    assert served_media_mime(None, '/x/a.html') == 'application/octet-stream'


@pytest.fixture
def html_named_share():
    folder = tempfile.mkdtemp(prefix='speakr_mime_')
    path = os.path.join(folder, 'evil.html')
    with open(path, 'wb') as f:
        f.write(b'ID3\x00' + b'<script>alert(1)</script>')
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f'mime_{suffix}', email=f'mime_{suffix}@local.test', password='x')
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title='t', audio_path=path, status='COMPLETED',
                        mime_type='text/html', original_filename='evil.html')
        db.session.add(rec)
        db.session.commit()
        share = Share(recording_id=rec.id, user_id=user.id)
        db.session.add(share)
        db.session.commit()
        ids = (user.id, rec.id, share.public_id)
    yield ids
    with app.app_context():
        Share.query.filter_by(recording_id=ids[1]).delete()
        db.session.delete(db.session.get(Recording, ids[1]))
        db.session.delete(db.session.get(User, ids[0]))
        db.session.commit()
    os.remove(path)
    os.rmdir(folder)


def test_public_share_never_serves_html(html_named_share):
    _, _, public_id = html_named_share
    resp = app.test_client().get(f'/share/audio/{public_id}')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/octet-stream'
    assert resp.headers['Content-Security-Policy'] == 'sandbox'
    resp.close()


def test_owner_audio_route_never_serves_html(html_named_share):
    user_id, rec_id, _ = html_named_share
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True
    resp = client.get(f'/audio/{rec_id}')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/octet-stream'
    assert resp.headers['Content-Security-Policy'] == 'sandbox'
    resp.close()
