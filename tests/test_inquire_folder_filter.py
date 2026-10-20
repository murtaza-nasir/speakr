"""Folder filter in Inquire (#428).

Recordings can be limited to one or more folders, alone or together with
tags: a recording is in scope when it is in one of the selected folders and,
if tags are selected too, carries one of the selected tags.
"""

import uuid
from unittest.mock import patch

import pytest

from src.app import app, db
from src.models import Folder, Recording, RecordingTag, Tag, TranscriptChunk, User
from src.services.embeddings import basic_text_search_chunks
from src.services.inquire_agent import resolve_allowed_recording_ids

app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture
def library():
    """A user with folders A and B, a tag, and three recordings."""
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f'inqf_{s}', email=f'inqf_{s}@local.test', password='x')
        db.session.add(user)
        db.session.flush()
        fa = Folder(name=f'Client A {s}', user_id=user.id)
        fb = Folder(name=f'Client B {s}', user_id=user.id)
        tag = Tag(name=f'P-100 {s}', user_id=user.id)
        db.session.add_all([fa, fb, tag])
        db.session.flush()
        recs = {}
        for key, folder in (('a_tagged', fa), ('a_plain', fa), ('b_tagged', fb)):
            r = Recording(user_id=user.id, title=key, status='COMPLETED', folder_id=folder.id,
                          transcription='budget review')
            db.session.add(r)
            db.session.flush()
            db.session.add(TranscriptChunk(recording_id=r.id, user_id=user.id, chunk_index=0,
                                           content='We discussed the budget review.'))
            recs[key] = r.id
        for key in ('a_tagged', 'b_tagged'):
            db.session.add(RecordingTag(recording_id=recs[key], tag_id=tag.id))
        db.session.commit()
        ids = {'user': user.id, 'a': fa.id, 'b': fb.id, 'tag': tag.id, **recs}
    yield ids
    with app.app_context():
        for key in ('a_tagged', 'a_plain', 'b_tagged'):
            TranscriptChunk.query.filter_by(recording_id=ids[key]).delete()
            RecordingTag.query.filter_by(recording_id=ids[key]).delete()
            db.session.delete(db.session.get(Recording, ids[key]))
        for key, model in (('tag', Tag), ('a', Folder), ('b', Folder), ('user', User)):
            db.session.delete(db.session.get(model, ids[key]))
        db.session.commit()


def test_agent_scope_by_folder(library):
    with app.app_context():
        assert resolve_allowed_recording_ids(library['user'], {'folder_ids': [library['a']]}) == {
            library['a_tagged'], library['a_plain']}


def test_agent_scope_by_folder_and_tag(library):
    with app.app_context():
        assert resolve_allowed_recording_ids(
            library['user'], {'folder_ids': [library['a']], 'tag_ids': [library['tag']]}
        ) == {library['a_tagged']}


def test_text_search_by_folder_and_tag(library):
    with app.app_context():
        results = basic_text_search_chunks(
            library['user'], 'budget', {'folder_ids': [library['a'], library['b']], 'tag_ids': [library['tag']]},
            top_k=10)
        assert {chunk.recording_id for chunk, _ in results} == {library['a_tagged'], library['b_tagged']}


def test_available_filters_list_folders_when_enabled(library):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(library['user'])
        sess['_fresh'] = True
    real_get = __import__('src.models', fromlist=['SystemSetting']).SystemSetting.get_setting

    def get_setting(key, default=None):
        return True if key == 'enable_folders' else real_get(key, default)

    with patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.SystemSetting.get_setting', side_effect=get_setting):
        data = client.get('/api/inquire/available_filters').get_json()
    names = {f['id']: f['recording_count'] for f in data['folders']}
    assert names == {library['a']: 2, library['b']: 1}


def test_search_passes_the_folder_filter(library):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(library['user'])
        sess['_fresh'] = True
    captured = {}
    with patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.api.inquire.semantic_search_chunks',
               side_effect=lambda uid, q, filters, k: captured.update(filters) or []):
        resp = client.post('/api/inquire/search', json={'query': 'q', 'filter_folders': [library['a']]})
    assert resp.status_code == 200
    assert captured['folder_ids'] == [library['a']]
