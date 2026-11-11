"""Per-response Word downloads: "scope": "response" on the Inquire and recording chat routes.

The scope only chooses the filename. "response" gives inquire-response.docx
or chat-<title>-response.docx; an absent scope, any other string and any
non-string give the usual names. The document holds the turns the page sends
(one answer and its question) with the same layout, links and Sources list.
"""
import email.utils
import io
import os
import sys
import uuid
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from docx import Document

import src.services.document as document_module
from src.app import app, db
from src.models import User, Recording

app.config['WTF_CSRF_ENABLED'] = False

_QUESTION = {'role': 'user', 'content': 'What is on the roadmap?'}
_ANSWER = {
    'role': 'assistant',
    'content': 'Two items, see [Budget review @ 0:30](/recordings/5?t=30).',
    'sources': [{'title': 'Budget review', 'url': '/recordings/5'}],
}
_NOT_RESPONSE = [None, 'conversation', 'Response', 'response ', '', 5, True, ['response'], {'scope': 'response'}]


@pytest.fixture(scope='module')
def owner():
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f'respdl_{suffix}', email=f'respdl_{suffix}@example.com',
                    password='x', timezone='UTC')
        db.session.add(user)
        db.session.commit()
        ids = {}
        for key, title in {'ascii': 'Weekly Sync', 'japanese': '週次会議', 'blank': '???'}.items():
            rec = Recording(user_id=user.id, title=title, status='COMPLETED',
                            audio_path='local://respdl/a.mp3', original_filename='a.mp3',
                            transcription='hello', created_at=datetime(2026, 3, 4, 9, 30),
                            meeting_date=datetime(2026, 3, 3, 14, 0))
            db.session.add(rec)
            db.session.commit()
            ids[key] = rec.id
        uid = user.id
    yield uid, ids
    with app.app_context():
        for rid in ids.values():
            Recording.query.filter_by(id=rid).delete()
        User.query.filter_by(id=uid).delete()
        db.session.commit()


def _client(user_id):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True
    return client


def _body(messages, scope, **extra):
    body = {'messages': messages, **extra}
    if scope is not None:
        body['scope'] = scope
    return body


def _inquire(user_id, body):
    with patch('src.api.inquire.ENABLE_INQUIRE_MODE', True), \
         patch('src.utils.timezones.now_local', return_value=datetime(2026, 3, 4, 15, 6)), \
         patch.dict(os.environ, {'APP_BASE_URL': 'https://speakr.example.com'}):
        return _client(user_id).post('/api/inquire/download', json=body)


def _chat(user_id, rid, body):
    with patch('src.utils.timezones.now_local', return_value=datetime(2026, 3, 5, 16, 7)), \
         patch.dict(os.environ, {'APP_BASE_URL': 'https://speakr.example.com'}):
        return _client(user_id).post(f'/recording/{rid}/download/chat', json=body)


def _paragraphs(resp):
    return [p.text for p in Document(io.BytesIO(resp.data)).paragraphs]


def _hyperlinks(resp):
    return [(h.text, h.url) for p in Document(io.BytesIO(resp.data)).paragraphs for h in p.hyperlinks]


# --- Inquire -------------------------------------------------------------------

def test_inquire_response_scope_names_the_file(owner):
    user_id, _ = owner
    resp = _inquire(user_id, _body([_QUESTION, _ANSWER], 'response', filters={'tags': ['Planning']}))
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'] == 'attachment; filename="inquire-response.docx"'


def test_inquire_response_holds_the_question_and_answer_with_sources(owner):
    user_id, _ = owner
    resp = _inquire(user_id, _body([_QUESTION, _ANSWER], 'response', filters={'tags': ['Planning']}))
    assert _paragraphs(resp) == [
        'Ask Ambrose',
        'Export Date: 2026-03-04 15:06',
        'Tags: Planning',
        'You:', 'What is on the roadmap?',
        'Assistant:', 'Two items, see 1 @ 0:30.',
        'Sources', '1. Budget review',
    ]
    assert _hyperlinks(resp) == [
        ('1 @ 0:30', 'https://speakr.example.com/recordings/5?t=30'),
        ('Budget review', 'https://speakr.example.com/recordings/5'),
    ]


def test_inquire_scope_changes_only_the_filename(owner):
    user_id, _ = owner
    with_scope = _inquire(user_id, _body([_QUESTION, _ANSWER], 'response'))
    without = _inquire(user_id, _body([_QUESTION, _ANSWER], None))
    assert _paragraphs(with_scope) == _paragraphs(without)
    assert _hyperlinks(with_scope) == _hyperlinks(without)


def test_inquire_answer_without_question(owner):
    user_id, _ = owner
    resp = _inquire(user_id, _body([_ANSWER], 'response'))
    assert resp.headers['Content-Disposition'] == 'attachment; filename="inquire-response.docx"'
    assert _paragraphs(resp)[2:4] == ['Assistant:', 'Two items, see 1 @ 0:30.']


@pytest.mark.parametrize('scope', _NOT_RESPONSE)
def test_inquire_other_scope_values_keep_inquire_docx(owner, scope):
    user_id, _ = owner
    resp = _inquire(user_id, _body([_QUESTION, _ANSWER], scope))
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'] == 'attachment; filename="inquire.docx"'


def test_inquire_response_scope_still_validates_messages(owner):
    user_id, _ = owner
    resp = _inquire(user_id, {'scope': 'response'})
    assert resp.status_code == 400 and resp.get_json()['code'] == 'no_messages_provided'
    resp = _inquire(user_id, _body([{'role': 'assistant', 'content': ''}], 'response'))
    assert resp.status_code == 400 and resp.get_json()['code'] == 'no_messages_to_download'


# --- Recording chat ------------------------------------------------------------

_CHAT_ANSWER = {'role': 'assistant', 'content': 'See [Standup @ 0:09](/recordings/3?t=9) for the **roadmap**.',
                'thinking': 'reasoning'}


def test_chat_response_scope_names_the_file(owner):
    user_id, ids = owner
    resp = _chat(user_id, ids['ascii'], _body([_QUESTION, _CHAT_ANSWER], 'response'))
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'] == 'attachment; filename="chat-Weekly-Sync-response.docx"'


def test_chat_response_holds_the_question_answer_and_thinking(owner):
    user_id, ids = owner
    resp = _chat(user_id, ids['ascii'], _body([_QUESTION, _CHAT_ANSWER], 'response'))
    assert _paragraphs(resp) == [
        'Chat Conversation: Weekly Sync',
        'Recording Date: 2026-03-03 14:00',
        'Chat Export Date: 2026-03-05 16:07',
        'You:', 'What is on the roadmap?',
        'Assistant:', '[Model Reasoning]\nreasoning',
        'See Standup @ 0:09 for the roadmap.',
    ]
    assert _hyperlinks(resp) == [('Standup @ 0:09', 'https://speakr.example.com/recordings/3?t=9')]


def test_chat_scope_changes_only_the_filename(owner):
    user_id, ids = owner
    with_scope = _chat(user_id, ids['ascii'], _body([_QUESTION, _CHAT_ANSWER], 'response'))
    without = _chat(user_id, ids['ascii'], _body([_QUESTION, _CHAT_ANSWER], None))
    assert _paragraphs(with_scope) == _paragraphs(without)
    assert _hyperlinks(with_scope) == _hyperlinks(without)


@pytest.mark.parametrize('scope', _NOT_RESPONSE)
def test_chat_other_scope_values_keep_the_chat_name(owner, scope):
    user_id, ids = owner
    resp = _chat(user_id, ids['ascii'], _body([_QUESTION, _CHAT_ANSWER], scope))
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'] == 'attachment; filename="chat-Weekly-Sync.docx"'


def test_chat_response_non_ascii_title_uses_rfc2231(owner):
    user_id, ids = owner
    resp = _chat(user_id, ids['japanese'], _body([_QUESTION, _CHAT_ANSWER], 'response'))
    expected = email.utils.encode_rfc2231('chat-週次会議-response.docx', charset='utf-8')
    assert resp.headers['Content-Disposition'] == f'attachment; filename*={expected}'


def test_chat_response_rfc2231_failure_falls_back(owner):
    user_id, ids = owner
    rid = ids['japanese']
    with patch.object(document_module, 'encode_rfc2231', side_effect=ValueError('boom')):
        resp = _chat(user_id, rid, _body([_QUESTION, _CHAT_ANSWER], 'response'))
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'] == f'attachment; filename="download-{rid}-response.docx"'


def test_chat_response_title_without_safe_characters(owner):
    user_id, ids = owner
    rid = ids['blank']
    resp = _chat(user_id, rid, _body([_QUESTION, _CHAT_ANSWER], 'response'))
    assert resp.headers['Content-Disposition'] == f'attachment; filename="chat-recording-{rid}-response.docx"'
    resp = _chat(user_id, rid, _body([_QUESTION, _CHAT_ANSWER], None))
    assert resp.headers['Content-Disposition'] == f'attachment; filename="chat-recording-{rid}.docx"'
