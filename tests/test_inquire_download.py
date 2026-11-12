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
from docx.oxml.ns import qn

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
        'Ask Ambrose',
        'Export Date: 2026-03-04 15:06',
        'You:',
        'What is on the roadmap?',
        'Assistant:',
        'The roadmap has two items.',
    ]
    doc = Document(io.BytesIO(resp.data))
    assert [p.style.name for p in doc.paragraphs] == [
        'Title', 'Normal', 'Conversation Role', 'Normal', 'Conversation Role', 'Normal']
    bold = [r.text for p in doc.paragraphs for r in p.runs if r.bold]
    assert bold == ['You:', 'Assistant:', 'roadmap']
    assert all(r._element.rPr is None or r._element.rPr.rFonts is None for p in doc.paragraphs for r in p.runs)
    assert doc.styles['Normal'].element.rPr.rFonts.get(qn('w:ascii')) == 'Arial'


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
        'Ask Ambrose',
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
    assert paragraphs[2] == 'You:'
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
    assert _paragraphs(resp)[2:] == [
        'You:', 'Summarise the budget',
        'You:', 'And the timeline?',
        'Assistant:', 'The timeline moves to',
    ]


def test_missing_or_odd_role_is_unknown(user_id):
    resp = _post(user_id, {'messages': [{'content': 'A'}, {'role': 3, 'content': 'B'}, {'role': 'system', 'content': 'C'}]})
    labels = _paragraphs(resp)[2::2]
    assert labels == ['Unknown:', 'Unknown:', 'System:']


def test_keeps_non_ascii(user_id):
    resp = _post(user_id, {'messages': [{'role': 'assistant', 'content': '预算 **会议**'}],
                           'filters': {'tags': ['会议']}})
    assert resp.status_code == 200
    assert resp.headers['Content-Disposition'] == 'attachment; filename="inquire.docx"'
    paragraphs = _paragraphs(resp)
    assert 'Tags: 会议' in paragraphs
    assert '预算 会议' in paragraphs


def test_build_failure_500(user_id):
    with patch('src.services.document.Document', side_effect=RuntimeError('boom')):
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


_ANSWER = 'Two items [Budget review @ 0:05](/recordings/5?t=5), [Standup](/recordings/9).'


def _hyperlinks(resp):
    return [(h.text, h.url) for p in Document(io.BytesIO(resp.data)).paragraphs for h in p.hyperlinks]


def _sources_turn(sources):
    return {'messages': [
        {'role': 'user', 'content': 'What is on the roadmap?'},
        {'role': 'assistant', 'content': _ANSWER, 'sources': sources},
    ]}


@pytest.mark.parametrize('env, base', [
    ({'APP_BASE_URL': 'https://speakr.example.com/'}, 'https://speakr.example.com'),
    ({'APP_BASE_URL': ''}, 'http://localhost'),
])
def test_citations_and_sources_are_absolute_hyperlinks(user_id, env, base):
    with patch.dict(os.environ, env):
        resp = _post(user_id, _sources_turn([
            {'title': 'Budget review', 'url': '/recordings/5'},
            {'title': 'Standup', 'url': '/recordings/9?t=30'},
        ]))
    assert resp.status_code == 200
    assert _paragraphs(resp)[2:] == [
        'You:', 'What is on the roadmap?',
        'Assistant:', 'Two items 1 @ 0:05, 2.',
        'Sources', '1. Budget review', '2. Standup',
    ]
    assert _hyperlinks(resp) == [
        ('1 @ 0:05', base + '/recordings/5?t=5'),
        ('2', base + '/recordings/9'),
        ('Budget review', base + '/recordings/5'),
        ('Standup', base + '/recordings/9?t=30'),
    ]
    assert '(/recordings' not in '\n'.join(_paragraphs(resp))


def test_invalid_sources_dropped_and_numbers_kept(user_id):
    resp = _post(user_id, _sources_turn([
        {'title': 'Kept one', 'url': '/recordings/5'},
        {'title': 'Script', 'url': 'javascript:alert(1)'},
        {'title': 'Elsewhere', 'url': 'https://evil.example/recordings/5'},
        {'title': 'Host relative', 'url': '//evil.example/recordings/5'},
        {'title': 'Traversal', 'url': '/recordings/5/../../admin'},
        {'title': 'Other param', 'url': '/recordings/5?t=1&next=/x'},
        {'title': 'Not a number', 'url': '/recordings/abc'},
        {'title': 'Other page', 'url': '/admin'},
        {'title': 7, 'url': '/recordings/5'},
        {'title': '   ', 'url': '/recordings/5'},
        {'title': 'No url'},
        'not a dict',
        {'title': 'Kept two', 'url': '/recordings/12?t=40'},
    ]))
    paragraphs = _paragraphs(resp)
    assert paragraphs[paragraphs.index('Sources'):] == ['Sources', '1. Kept one', '13. Kept two']
    assert [url for _, url in _hyperlinks(resp)][2:] == [
        'http://localhost/recordings/5', 'http://localhost/recordings/12?t=40']


@pytest.mark.parametrize('sources', [None, [], 'Budget', {'title': 'x', 'url': '/recordings/5'},
                                     [{'title': 'Script', 'url': 'javascript:alert(1)'}]])
def test_no_sources_label_without_valid_sources(user_id, sources):
    body = _sources_turn(sources)
    if sources is None:
        del body['messages'][1]['sources']
    paragraphs = _paragraphs(_post(user_id, body))
    assert 'Sources' not in paragraphs
    assert paragraphs[-1] == 'Two items Budget review @ 0:05, Standup.'


def test_sources_on_a_user_turn_are_ignored(user_id):
    resp = _post(user_id, {'messages': [
        {'role': 'user', 'content': 'Question', 'sources': [{'title': 'Planted', 'url': '/recordings/5'}]},
    ]})
    assert 'Planted' not in '\n'.join(_paragraphs(resp))


def test_source_title_is_capped_and_whitespace_collapsed(user_id):
    resp = _post(user_id, _sources_turn([{'title': 'A\n  b ' + 'x' * 400, 'url': '/recordings/5'}]))
    line = _paragraphs(resp)[-1]
    assert line.startswith('1. A b x')
    assert len(line) == len('1. ') + 300


def test_sources_capped_per_turn_and_in_total(user_id):
    many = [{'title': f'Recording {i}', 'url': f'/recordings/{i}'} for i in range(1, 61)]
    resp = _post(user_id, _sources_turn(many))
    lines = [p for p in _paragraphs(resp) if p.startswith(tuple(f'{i}. ' for i in range(1, 100)))]
    assert len(lines) == 50 and lines[-1] == '50. Recording 50'

    turns = []
    for _ in range(5):
        turns += [{'role': 'user', 'content': 'Q'}, {'role': 'assistant', 'content': 'A', 'sources': many}]
    paragraphs = _paragraphs(_post(user_id, {'messages': turns}))
    assert paragraphs.count('Sources') == 4
    assert sum(1 for p in paragraphs if p.startswith('50. ')) == 4


def test_citations_numbered_as_on_the_page(user_id):
    body = {'messages': [{'role': 'assistant', 'sources': [
        {'title': 'Budget review', 'url': '/recordings/5'},
        {'title': 'Standup', 'url': '/recordings/9'},
    ], 'content': ('A [Budget review @ 1:02:03](/recordings/5?t=3723), '
                   'B [Budget review @ 2:05](/recordings/5), '
                   'C [Standup @ 1317](/recordings/9), '
                   'D [Other](/recordings/44?t=8), '
                   'E [Standup](/recordings/9).')}]}
    resp = _post(user_id, body)
    assert _paragraphs(resp)[3] == 'A 1 @ 1:02:03, B 1 @ 2:05, C 2 @ 21:57, D Other, E 2.'
    assert [h for h in _hyperlinks(resp)][:5] == [
        ('1 @ 1:02:03', 'http://localhost/recordings/5?t=3723'),
        ('1 @ 2:05', 'http://localhost/recordings/5'),
        ('2 @ 21:57', 'http://localhost/recordings/9'),
        ('Other', 'http://localhost/recordings/44?t=8'),
        ('2', 'http://localhost/recordings/9'),
    ]


def test_citations_keep_their_text_without_sources(user_id):
    resp = _post(user_id, _sources_turn(None))
    assert _paragraphs(resp)[-1] == 'Two items Budget review @ 0:05, Standup.'
