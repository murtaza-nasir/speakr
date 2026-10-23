"""Deleting all speakers removes their snippets and voice samples (#475).

SHARED-DB: users, speakers, snippets and recordings are removed afterwards.
"""

import os
import secrets
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.init_db import _remove_orphaned_speaker_rows
from src.models import Recording, Speaker, SpeakerVoiceSample, User
from src.models.speaker_snippet import SpeakerSnippet


def _make_speaker_with_rows(user, rec, name):
    speaker = Speaker(name=name, user_id=user.id)
    db.session.add(speaker)
    db.session.commit()
    db.session.add(SpeakerSnippet(speaker_id=speaker.id, recording_id=rec.id,
                                  segment_index=0, text_snippet='hello there'))
    db.session.add(SpeakerVoiceSample(user_id=user.id, speaker_id=speaker.id,
                                      embedding=b'\0' * 16, dimension=4))
    db.session.commit()
    return speaker.id


@pytest.fixture
def world():
    saved = app.config.get('WTF_CSRF_ENABLED', True)
    app.config['WTF_CSRF_ENABLED'] = False
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"sbd_{s}", email=f"sbd_{s}@local.test", password="x")
        other = User(username=f"sbdo_{s}", email=f"sbdo_{s}@local.test", password="x")
        db.session.add_all([owner, other])
        db.session.commit()
        rec = Recording(user_id=owner.id, title='t', status='COMPLETED')
        other_rec = Recording(user_id=other.id, title='t', status='COMPLETED')
        db.session.add_all([rec, other_rec])
        db.session.commit()
        ids = dict(owner=owner.id, other=other.id, recs=[rec.id, other_rec.id],
                   mine=_make_speaker_with_rows(owner, rec, 'Dana'),
                   theirs=_make_speaker_with_rows(other, other_rec, 'Lee'))
    yield ids
    app.config['WTF_CSRF_ENABLED'] = saved
    with app.app_context():
        db.session.rollback()
        users = [ids['owner'], ids['other']]
        SpeakerSnippet.query.filter(SpeakerSnippet.recording_id.in_(ids['recs'])).delete(synchronize_session=False)
        SpeakerVoiceSample.query.filter(SpeakerVoiceSample.user_id.in_(users)).delete(synchronize_session=False)
        Speaker.query.filter(Speaker.user_id.in_(users)).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(ids['recs'])).delete(synchronize_session=False)
        User.query.filter(User.id.in_(users)).delete(synchronize_session=False)
        db.session.commit()


def _client_for(user_id):
    from flask import g
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = db.session.get(User, user_id).get_id()
        sess['_fresh'] = True
    g.pop('_login_user', None)
    return client


def test_delete_all_removes_snippets_and_voice_samples(world):
    with app.app_context():
        resp = _client_for(world['owner']).delete('/speakers/delete_all')
        assert resp.status_code == 200, resp.data
        assert resp.get_json()['deleted_count'] == 1
        db.session.expire_all()
        assert SpeakerSnippet.query.filter_by(speaker_id=world['mine']).count() == 0
        assert SpeakerVoiceSample.query.filter_by(speaker_id=world['mine']).count() == 0
        # Another user's speaker and rows are untouched.
        assert db.session.get(Speaker, world['theirs']) is not None
        assert SpeakerSnippet.query.filter_by(speaker_id=world['theirs']).count() == 1
        assert SpeakerVoiceSample.query.filter_by(speaker_id=world['theirs']).count() == 1


def test_cleanup_removes_rows_left_by_the_old_bulk_delete(world):
    with app.app_context():
        if db.engine.dialect.name != 'sqlite':
            pytest.skip('Only SQLite can hold these orphans; PostgreSQL enforces the cascade')
        # Reproduce the old bulk delete: the speaker row goes, its rows stay.
        with db.engine.begin() as conn:
            conn.execute(text('DELETE FROM speaker WHERE id = :id'), {'id': world['mine']})
        assert SpeakerSnippet.query.filter_by(speaker_id=world['mine']).count() == 1

        _remove_orphaned_speaker_rows(db.engine, app)

        db.session.expire_all()
        assert SpeakerSnippet.query.filter_by(speaker_id=world['mine']).count() == 0
        assert SpeakerVoiceSample.query.filter_by(speaker_id=world['mine']).count() == 0
        assert SpeakerSnippet.query.filter_by(speaker_id=world['theirs']).count() == 1
        assert SpeakerVoiceSample.query.filter_by(speaker_id=world['theirs']).count() == 1
