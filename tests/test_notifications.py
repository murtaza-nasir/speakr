"""In-app notifications: raising, deduplicating, resolving and reading.

The store exists because Speakr had a delivery channel (web push) and nowhere
to keep a notice, so nothing could be listed or counted. Most of what is
tested here is the behaviour that makes it safe to raise a notice from a code
path that runs on every startup.
"""

import os

import pytest

from src.app import app
from src.database import db
from src.models import Notification, User, KIND_VOICE_EMBEDDING_CHANGED
from src.services import notifications as ns


@pytest.fixture
def users():
    tag = f'notif_{os.getpid()}'
    with app.app_context():
        made = []
        for name, is_admin in (('admin1', True), ('admin2', True), ('plain', False)):
            u = User(username=f'{tag}_{name}', email=f'{tag}_{name}@example.com',
                     password='x', is_admin=is_admin)
            db.session.add(u)
            made.append(u)
        db.session.commit()
        ids = {'admin1': made[0].id, 'admin2': made[1].id, 'plain': made[2].id,
               'all': [u.id for u in made]}
    yield ids
    with app.app_context():
        Notification.query.filter(Notification.user_id.in_(ids['all'])).delete(
            synchronize_session=False)
        User.query.filter(User.id.in_(ids['all'])).delete(synchronize_session=False)
        db.session.commit()


def _get(user_id):
    return db.session.get(User, user_id)


# --- raising -------------------------------------------------------------

def test_a_notice_reaches_every_admin_and_nobody_else(users):
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', admins=True)
        recipients = {n.user_id for n in Notification.query.filter_by(kind='test.kind').all()}
    assert users['admin1'] in recipients
    assert users['admin2'] in recipients
    assert users['plain'] not in recipients, 'a non-admin received an instance notice'


def test_raising_the_same_condition_twice_does_not_stack_up(users):
    """The property that makes a startup check safe. Startup runs on every
    restart and every deploy; an insert per run would pile up identical rows."""
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])
        assert Notification.query.filter_by(
            user_id=users['admin1'], kind='test.kind').count() == 1


def test_a_second_raise_refreshes_the_existing_notice(users):
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']],
                  params={'detail': 'first'})
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']],
                  params={'detail': 'second'}, level='error')
        n = Notification.query.filter_by(user_id=users['admin1'], kind='test.kind').one()
    assert n.params['detail'] == 'second'
    assert n.level == 'error'


def test_distinct_dedupe_keys_coexist(users):
    """One kind can describe several independent conditions."""
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']],
                  dedupe_key='test.kind:a')
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']],
                  dedupe_key='test.kind:b')
        assert Notification.query.filter_by(user_id=users['admin1']).count() == 2


def test_raising_with_no_recipients_is_a_no_op(users):
    with app.app_context():
        assert ns.notify('test.kind', 'notifications.test', user_ids=[]) == 0
        assert Notification.query.filter_by(kind='test.kind').count() == 0


# --- resolving -----------------------------------------------------------

def test_resolving_hides_the_notice_without_deleting_it(users):
    """Resolution is the producer saying the condition ended. The row stays so
    the history is not lost, but it stops being shown or counted."""
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])
        assert ns.unread_count(_get(users['admin1'])) == 1

        ns.resolve('test.kind')

        assert ns.unread_count(_get(users['admin1'])) == 0
        assert ns.active_for(_get(users['admin1'])) == []
        assert Notification.query.filter_by(kind='test.kind').count() == 1


def test_a_condition_that_comes_back_is_shown_again(users):
    """Restoring a backend resolves the notice; breaking it again must raise
    it, not leave the admin looking at a silent, already-resolved row."""
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])
        ns.mark_read(_get(users['admin1']))
        ns.resolve('test.kind')
        assert ns.unread_count(_get(users['admin1'])) == 0

        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])

        assert ns.unread_count(_get(users['admin1'])) == 1, 'a recurrence stayed silent'
        assert len(ns.active_for(_get(users['admin1']))) == 1


def test_a_dismissed_notice_returns_if_the_condition_recurs(users):
    """Dismissal clears it from view; it is not a permanent mute."""
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])
        n = Notification.query.filter_by(user_id=users['admin1']).one()
        ns.dismiss(_get(users['admin1']), n.id)
        assert ns.active_for(_get(users['admin1'])) == []

        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])
        assert len(ns.active_for(_get(users['admin1']))) == 1


# --- reading -------------------------------------------------------------

def test_unread_count_ignores_read_dismissed_and_resolved(users):
    with app.app_context():
        for i, key in enumerate(('a', 'b', 'c', 'd')):
            ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']],
                      dedupe_key=f'test.kind:{key}')
        admin = _get(users['admin1'])
        assert ns.unread_count(admin) == 4

        rows = Notification.query.filter_by(user_id=users['admin1']).order_by(Notification.id).all()
        ns.mark_read(admin, [rows[0].id])
        ns.dismiss(admin, rows[1].id)
        ns.resolve('test.kind:c')

        assert ns.unread_count(admin) == 1


def test_one_user_cannot_dismiss_another_users_notice(users):
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])
        n = Notification.query.filter_by(user_id=users['admin1']).one()

        assert ns.dismiss(_get(users['plain']), n.id) is False
        assert db.session.get(Notification, n.id).dismissed_at is None


# --- what is stored ------------------------------------------------------

def test_the_message_is_stored_as_a_key_not_as_text(users):
    """Storing rendered English would make notifications the one part of the
    interface that ignores the seven locale files."""
    with app.app_context():
        ns.notify(KIND_VOICE_EMBEDDING_CHANGED, 'notifications.voiceEmbeddingChanged',
                  user_ids=[users['admin1']], params={'detail': 'similarity 0.27'})
        n = Notification.query.filter_by(user_id=users['admin1']).one()

    assert n.message_key == 'notifications.voiceEmbeddingChanged'
    assert ' ' not in n.message_key, 'that looks like a sentence, not a key'
    assert n.params['detail'] == 'similarity 0.27'


def test_every_message_key_the_backend_raises_exists_in_every_locale():
    """A notice whose key is missing renders as the raw key, in the one place
    a user is being told something went wrong."""
    import json
    import re

    keys = set()
    for path in ('src/services/voice_embedding_check.py',):
        src = open(path, encoding='utf-8').read()
        keys |= set(re.findall(r"'(notifications\.[A-Za-z0-9_.]+)'", src))
    assert keys, 'no notification keys found to check'

    for lang in ('en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh'):
        data = json.load(open(f'static/locales/{lang}.json', encoding='utf-8'))
        for key in keys:
            node = data
            for part in key.split('.'):
                assert isinstance(node, dict) and part in node, f'{lang}.json lacks {key}'
                node = node[part]


def test_the_link_is_relative(users):
    """An absolute URL in a notification is a phishing shape."""
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']], link='/admin')
        n = Notification.query.filter_by(user_id=users['admin1']).one()
    assert n.link.startswith('/')
    assert '://' not in n.link


# --- the API -------------------------------------------------------------

@pytest.fixture
def api(users):
    previous = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    client = app.test_client()

    def login(user_id):
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user_id)
            sess['_fresh'] = True
    yield client, login, users
    app.config['WTF_CSRF_ENABLED'] = previous


def test_the_list_endpoint_returns_only_the_callers_notices(api):
    client, login, users = api
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin2']])

    login(users['admin1'])
    body = client.get('/api/notifications').get_json()
    assert len(body['notifications']) == 1
    assert body['unread_count'] == 1


def test_every_endpoint_requires_a_session(api):
    client, _login, users = api
    for method, path in (('get', '/api/notifications'), ('get', '/api/notifications/count'),
                         ('post', '/api/notifications/read'),
                         ('delete', '/api/notifications/1')):
        resp = getattr(client, method)(path)
        assert resp.status_code in (302, 401), f'{method.upper()} {path} was reachable: {resp.status_code}'


def test_dismissing_someone_elses_notice_is_a_404_not_a_403(api):
    """403 would confirm the id exists, which is not something to tell a user
    who does not own it."""
    client, login, users = api
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin2']])
        other = Notification.query.filter_by(user_id=users['admin2']).one().id

    login(users['admin1'])
    assert client.delete(f'/api/notifications/{other}').status_code == 404


def test_marking_read_clears_the_badge(api):
    client, login, users = api
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']])

    login(users['admin1'])
    assert client.get('/api/notifications/count').get_json()['unread_count'] == 1
    body = client.post('/api/notifications/read', json={}).get_json()
    assert body['marked'] == 1
    assert body['unread_count'] == 0


def test_a_bad_ids_payload_is_rejected(api):
    client, login, users = api
    login(users['admin1'])
    assert client.post('/api/notifications/read', json={'ids': 'not-a-list'}).status_code == 400


# --- the link is rendered as an href ------------------------------------

@pytest.mark.parametrize('bad', [
    'javascript:alert(1)',
    'https://evil.example/login',
    '//evil.example/login',          # protocol-relative, still off-site
    'data:text/html,<script>',
])
def test_a_link_that_is_not_a_same_origin_path_is_dropped(users, bad):
    """The link is the one thing in a notice a user is told to click.
    Only a same-origin path is accepted, so a future producer cannot put
    a javascript: or off-site URL there."""
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']], link=bad)
        n = Notification.query.filter_by(user_id=users['admin1']).one()
    assert n.link is None, f'{bad!r} was stored as a link'


def test_a_same_origin_path_is_kept(users):
    with app.app_context():
        ns.notify('test.kind', 'notifications.test', user_ids=[users['admin1']], link='/admin?tab=voice')
        n = Notification.query.filter_by(user_id=users['admin1']).one()
    assert n.link == '/admin?tab=voice'
