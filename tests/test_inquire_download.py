"""POST /api/inquire/download: the Inquire conversation as a Word document (#555)."""
import io
import os
import sys
import uuid
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from docx import Document

from src.app import app, db
from src.models import User, InquireSession

app.config['WTF_CSRF_ENABLED'] = False

_URL = '/api/inquire/download'
_TURNS = [
    {'role': 'user', 'content': 'What is on the roadmap?'},
    {'role': 'assistant', 'content': 'The **roadmap** has two items.'},
]


@pytest.fixture(scope='module')
def user_id():
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f'inqdl_{suffix}', email=f'inqdl_{suffix}@example.com', password='x')
        db.session.add(user)
        db.session.commit()
        uid = user.id
    yield uid
    with app.app_context():
        User.query.filter_by(id=uid).delete()
        db.session.commit()


def _post(user_id, body, enabled=True, **kwargs):
    client = app.test_client()
    if user_id is not None:
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
    with patch('src.api.inquire.ENABLE_INQUIRE_MODE', enabled), \
         patch('src.utils.timezones.now_local', return_value=datetime(2026, 3, 4, 15, 6)):
        if body is None:
            return client.post(_URL, **kwargs)
        return client.post(_URL, json=body, **kwargs)


def _paragraphs(resp):
    return [p.text for p in Document(io.BytesIO(resp.data)).paragraphs]


def test_requires_login():
    resp = _post(None, {'messages': _TURNS})
    assert resp.status_code == 401
    assert resp.get_json()['code'] == 'authentication_required'


@pytest.mark.parametrize('body', [{'messages': _TURNS}, {}])
def test_disabled_403_before_body_checks(user_id, body):
    resp = _post(user_id, body, enabled=False)
    assert resp.status_code == 403
    assert resp.get_json()['code'] == 'inquire_disabled'


@pytest.mark.parametrize('body', [None, {}, {'filters': {'tags': ['Planning']}}])
def test_missing_body_or_messages_400(user_id, body):
    resp = _post(user_id, body)
    assert resp.status_code == 400
    assert resp.get_json()['code'] == 'no_messages_provided'


def test_non_json_body_400(user_id):
    resp = _post(user_id, None, data='not json', content_type='text/plain')
    assert resp.status_code == 400
    assert resp.get_json()['code'] == 'no_messages_provided'


@pytest.mark.parametrize('messages', [
    [],
    'not a list',
    [{'role': 'assistant', 'content': ''}, 'not a dict', {'role': 'user'}, {'role': 'user', 'content': 5}],
])
def test_nothing_to_download_400(user_id, messages):
    resp = _post(user_id, {'messages': messages})
    assert resp.status_code == 400
    assert resp.get_json()['code'] == 'no_messages_to_download'


def test_returns_docx(user_id):
    resp = _post(user_id, {'messages': _TURNS})
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    assert resp.headers['Content-Disposition'] == 'attachment; filename="inquire.docx"'
    assert _paragraphs(resp) == [
        'Inquire',
        'Export Date: 2026-03-04 15:06',
        '',
        'You: ',
        'What is on the roadmap?',
        '',
        'Assistant: ',
        'The roadmap has two items.',
        '',
    ]
    doc = Document(io.BytesIO(resp.data))
    assert doc.paragraphs[0].style.name == 'Title'
    bold = [r.text for p in doc.paragraphs for r in p.runs if r.bold]
    assert bold == ['You: ', 'Assistant: ', 'roadmap']


def test_filter_paragraph_lists_names_in_order(user_id):
    resp = _post(user_id, {'messages': _TURNS, 'filters': {
        'dates': ['2026-01-01', '2026-01-31'],
        'recordings': ['Standup'],
        'speakers': ['Ada'],
        'folders': ['Client'],
        'tags': ['Planning', 'Budget'],
    }})
    assert resp.status_code == 200
    assert _paragraphs(resp)[:3] == [
        'Inquire',
        'Export Date: 2026-03-04 15:06',
        'Tags: Planning, Budget; Folders: Client; Speakers: Ada; Recordings: Standup; Dates: 2026-01-01, 2026-01-31',
    ]


def test_filter_paragraph_dates_only_set_ends(user_id):
    resp = _post(user_id, {'messages': _TURNS, 'filters': {'dates': ['2026-01-31']}})
    assert _paragraphs(resp)[2] == 'Dates: 2026-01-31'


@pytest.mark.parametrize('filters', [
    None,
    {},
    {'tags': [], 'dates': ''},
    {'tags': ['  ', 7], 'speakers': None},
    'Tags: Planning',
])
def test_no_filter_paragraph_without_filters(user_id, filters):
    body = {'messages': _TURNS}
    if filters is not None:
        body['filters'] = filters
    resp = _post(user_id, body)
    assert resp.status_code == 200
    paragraphs = _paragraphs(resp)
    assert paragraphs[2] == ''
    assert not any(p.startswith(('Tags:', 'Dates:', 'Speakers:')) for p in paragraphs)


def test_keeps_only_role_and_content(user_id):
    resp = _post(user_id, {
        'messages': [
            {'role': 'user', 'content': 'Question?', 'html': '<p>secret-html</p>'},
            {'role': 'assistant', 'content': 'Answer.', 'steps': [{'label': 'secret-step'}],
             'sources': [{'title': 'secret-source'}], 'activitySummary': 'secret-activity',
             'thinking': 'secret-thinking'},
        ],
        'conversation_memory': 'secret-memory',
    })
    text = '\n'.join(_paragraphs(resp))
    assert 'Question?' in text and 'Answer.' in text
    assert 'secret' not in text
    assert '[Model Reasoning]' not in text


def test_partial_turns_are_kept_and_blank_turns_skipped(user_id):
    resp = _post(user_id, {'messages': [
        {'role': 'user', 'content': 'Summarise the budget'},
        {'role': 'assistant', 'content': ''},
        {'role': 'user', 'content': 'And the timeline?'},
        {'role': 'assistant', 'content': 'The timeline moves to'},
    ]})
    assert _paragraphs(resp)[3:] == [
        'You: ', 'Summarise the budget', '',
        'You: ', 'And the timeline?', '',
        'Assistant: ', 'The timeline moves to', '',
    ]


def test_missing_or_odd_role_is_unknown(user_id):
    resp = _post(user_id, {'messages': [{'content': 'A'}, {'role': 3, 'content': 'B'}, {'role': 'system', 'content': 'C'}]})
    labels = _paragraphs(resp)[3::3]
    assert labels == ['Unknown: ', 'Unknown: ', 'System: ']


def test_keeps_non_ascii(user_id):
    resp = _post(user_id, {'messages': [{'role': 'assistant', 'content': '预算 **会议**'}],
                           'filters': {'tags': ['会议']}})
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'] == 'attachment; filename="inquire.docx"'
    paragraphs = _paragraphs(resp)
    assert 'Tags: 会议' in paragraphs
    assert '预算 会议' in paragraphs


def test_build_failure_500(user_id):
    with patch('docx.Document', side_effect=RuntimeError('boom')):
        resp = _post(user_id, {'messages': _TURNS})
    assert resp.status_code == 500
    assert resp.get_json()['code'] == 'word_document_failed'


def test_no_inquire_session_created(user_id):
    with app.app_context():
        before = InquireSession.query.filter_by(user_id=user_id).count()
    assert _post(user_id, {'messages': _TURNS, 'filters': {'tags': ['Planning']}}).status_code == 200
    with app.app_context():
        assert InquireSession.query.filter_by(user_id=user_id).count() == before


def test_route_is_csrf_protected(user_id):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        with patch('src.api.inquire.ENABLE_INQUIRE_MODE', True):
            missing = client.post(_URL, json={'messages': _TURNS})
            token = client.get('/api/csrf-token').get_json()['csrf_token']
            sent = client.post(_URL, json={'messages': _TURNS}, headers={'X-CSRFToken': token})
    finally:
        app.config['WTF_CSRF_ENABLED'] = False
    assert missing.status_code == 400
    assert 'token' in missing.get_json()['csrf_reason'].lower()
    assert sent.status_code == 200
    assert sent.headers['Content-Disposition'] == 'attachment; filename="inquire.docx"'
