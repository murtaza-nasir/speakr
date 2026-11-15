"""Who a share request may name: POST /api/recordings/<id>/share-internal.

The endpoint takes user ids. A recording owner could otherwise run through
the numbers and read the usernames from GET .../shares-internal, whatever the
user_search_scope setting says. So an id is accepted only for an account the
requester could find in the user search (src/services/user_lookup.py):

- everyone: any account
- groups:   an account in a shared group, or one named by an accompanying
            identifier (the full username or the full email address)
- exact:    only an account named by an accompanying identifier

A site admin is not restricted. An account that does not exist and one that
may not be named by id are answered the same way. The share window sends the
id and the username of each person it selected.

SHARED-DB: every row made here is removed afterwards.
"""

import os
import sys
import uuid
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.app as app_module
import src.api.shares as shares_module
from src.app import app, db
from src.models import (Group, GroupMembership, InternalShare, Recording, ShareAuditLog, SharedRecordingState,
                        SystemSetting, User)

app.config['WTF_CSRF_ENABLED'] = False

LEVELS = ('everyone', 'groups', 'exact')
UNKNOWN_ID = 2_000_000_000


@contextmanager
def _internal_sharing():
    modules = (app_module, shares_module)
    saved = [(m, m.__dict__.get('ENABLE_INTERNAL_SHARING'), 'ENABLE_INTERNAL_SHARING' in m.__dict__) for m in modules]
    for m in modules:
        setattr(m, 'ENABLE_INTERNAL_SHARING', True)
    try:
        yield
    finally:
        for m, old, had in saved:
            if had:
                setattr(m, 'ENABLE_INTERNAL_SHARING', old)
            else:
                delattr(m, 'ENABLE_INTERNAL_SHARING')


def _level(value):
    with app.app_context():
        SystemSetting.set_setting('user_search_scope', value, None, 'string')


class World:
    """An owner with a recording, a colleague in a shared group, a stranger and a site admin."""

    def __init__(self):
        self.tok = uuid.uuid4().hex[:8]
        self.user_ids, self.group_ids, self.recording_ids = [], [], []

    def user(self, name, is_admin=False):
        user = User(username=f"{self.tok}{name}", email=f"{self.tok}{name}@Corp.local.test", password='x',
                    is_admin=is_admin)
        db.session.add(user)
        db.session.commit()
        self.user_ids.append(user.id)
        return user.id

    def recording(self, owner_id):
        recording = Recording(user_id=owner_id, title='scope', audio_path='/tmp/x.mp3', status='COMPLETED')
        db.session.add(recording)
        db.session.commit()
        self.recording_ids.append(recording.id)
        return recording.id

    def name(self, who):
        return f"{self.tok}{who}"

    def email(self, who):
        return f"{self.tok}{who}@corp.local.test"       # stored with another case

    def cleanup(self):
        with app.app_context():
            for model, column in ((ShareAuditLog, ShareAuditLog.recording_id),
                                  (SharedRecordingState, SharedRecordingState.recording_id),
                                  (InternalShare, InternalShare.recording_id)):
                model.query.filter(column.in_(self.recording_ids)).delete(synchronize_session=False)
            Recording.query.filter(Recording.id.in_(self.recording_ids)).delete(synchronize_session=False)
            GroupMembership.query.filter(GroupMembership.user_id.in_(self.user_ids)).delete(synchronize_session=False)
            Group.query.filter(Group.id.in_(self.group_ids)).delete(synchronize_session=False)
            User.query.filter(User.id.in_(self.user_ids)).delete(synchronize_session=False)
            db.session.commit()


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv('USER_SEARCH_SCOPE', raising=False)
    with app.app_context():
        row = SystemSetting.query.filter_by(key='user_search_scope').first()
        saved = None if row is None else (row.value, row.setting_type)
    w = World()
    with app.app_context():
        w.owner, w.mate, w.stranger = w.user('owner'), w.user('mate'), w.user('stranger')
        w.admin = w.user('root', is_admin=True)
        group = Group(name=f"{w.tok} team")
        db.session.add(group)
        db.session.commit()
        w.group_ids.append(group.id)
        for user_id in (w.owner, w.mate):
            db.session.add(GroupMembership(group_id=group.id, user_id=user_id, role='member'))
        db.session.commit()
        w.rec = w.recording(w.owner)
        w.admin_rec = w.recording(w.admin)
    try:
        yield w
    finally:
        w.cleanup()
        with app.app_context():
            if saved is None:
                row = SystemSetting.query.filter_by(key='user_search_scope').first()
                if row is not None:
                    db.session.delete(row)
                    db.session.commit()
            else:
                SystemSetting.set_setting('user_search_scope', saved[0], None, saved[1])


def _post(user_id, rec, body):
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True
    with _internal_sharing():
        return client.post(f"/api/recordings/{rec}/share-internal", json=body)


def _shared_with(rec):
    with app.app_context():
        return {s.shared_with_user_id for s in InternalShare.query.filter_by(recording_id=rec).all()}


def _unshare(rec):
    with app.app_context():
        InternalShare.query.filter_by(recording_id=rec).delete()
        db.session.commit()


def _statuses(resp):
    assert resp.status_code == 200, resp.get_json()
    return [(row.get('user_id'), row.get('identifier'), row['status']) for row in resp.get_json()['results']]


# --- one person (user_id, identifier) ---------------------------------------------

def test_everyone_accepts_any_account_by_id(world):
    _level('everyone')
    assert _post(world.owner, world.rec, {'user_id': world.stranger}).status_code == 201
    assert _shared_with(world.rec) == {world.stranger}


def test_groups_accepts_an_id_only_from_a_shared_group(world):
    _level('groups')
    assert _post(world.owner, world.rec, {'user_id': world.mate}).status_code == 201
    refused = _post(world.owner, world.rec, {'user_id': world.stranger})
    assert refused.status_code == 404 and refused.get_json()['code'] == 'target_user_not_found'
    assert _shared_with(world.rec) == {world.mate}


@pytest.mark.parametrize('by', ['name', 'email'])
def test_groups_accepts_a_stranger_named_in_full(world, by):
    _level('groups')
    identifier = world.name('stranger') if by == 'name' else world.email('stranger')
    assert _post(world.owner, world.rec, {'user_id': world.stranger, 'identifier': identifier}).status_code == 201
    assert _shared_with(world.rec) == {world.stranger}


def test_exact_refuses_every_bare_id(world):
    _level('exact')
    for target in (world.mate, world.stranger, world.admin):
        refused = _post(world.owner, world.rec, {'user_id': target})
        assert refused.status_code == 404 and refused.get_json()['code'] == 'target_user_not_found'
    assert _shared_with(world.rec) == set()


@pytest.mark.parametrize('by', ['name', 'email'])
def test_exact_accepts_an_id_with_its_full_identifier(world, by):
    _level('exact')
    identifier = world.name('mate') if by == 'name' else world.email('mate')
    resp = _post(world.owner, world.rec, {'user_id': world.mate, 'identifier': f"  {identifier} ", 'can_edit': True})
    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()['share']['can_edit'] is True
    assert _shared_with(world.rec) == {world.mate}


@pytest.mark.parametrize('level', LEVELS)
def test_an_identifier_alone_names_its_account_at_every_level(world, level):
    _level(level)
    assert _post(world.owner, world.rec, {'identifier': world.name('stranger')}).status_code == 201
    assert _shared_with(world.rec) == {world.stranger}


@pytest.mark.parametrize('level', ['groups', 'exact'])
def test_an_identifier_of_someone_else_does_not_open_another_id(world, level):
    """The lookup is run again on the server: a name that resolves to another
    account, part of a name and a made-up name give the id nothing."""
    _level(level)
    for identifier in (world.name('mate'), world.name('strang'), world.email('stranger')[:-2], 'nobody-at-all'):
        refused = _post(world.owner, world.rec, {'user_id': world.stranger, 'identifier': identifier})
        assert refused.status_code == 404, identifier
    assert _shared_with(world.rec) == set()


@pytest.mark.parametrize('level', ['groups', 'exact'])
def test_an_unknown_id_and_a_hidden_account_get_the_same_answer(world, level):
    _level(level)
    hidden = _post(world.owner, world.rec, {'user_id': world.stranger})
    unknown = _post(world.owner, world.rec, {'user_id': UNKNOWN_ID})
    assert hidden.status_code == unknown.status_code == 404
    assert hidden.get_json() == unknown.get_json()
    unknown_name = _post(world.owner, world.rec, {'identifier': 'nobody-at-all'})
    assert unknown_name.status_code == 404 and unknown_name.get_json() == hidden.get_json()


def test_an_existing_share_does_not_show_through_a_bare_id(world):
    """already_shared (409) would tell that the id exists."""
    _level('everyone')
    assert _post(world.owner, world.rec, {'user_id': world.stranger}).status_code == 201
    _level('exact')
    again = _post(world.owner, world.rec, {'user_id': world.stranger})
    assert again.status_code == 404
    named = _post(world.owner, world.rec, {'user_id': world.stranger, 'identifier': world.name('stranger')})
    assert named.status_code == 409 and named.get_json()['code'] == 'already_shared'


@pytest.mark.parametrize('level', LEVELS)
def test_a_site_admin_is_not_restricted(world, level):
    _level(level)
    assert _post(world.admin, world.admin_rec, {'user_id': world.stranger}).status_code == 201
    resp = _post(world.admin, world.admin_rec, {'user_ids': [world.mate, UNKNOWN_ID]})
    assert _statuses(resp) == [(world.mate, None, 'shared'), (UNKNOWN_ID, None, 'not_found')]
    assert _shared_with(world.admin_rec) == {world.stranger, world.mate}


def test_one_person_needs_an_id_or_an_identifier(world):
    _level('everyone')
    for body in ({}, {'can_edit': True}, {'identifier': ''}, {'identifier': '   '}, {'identifier': 7},
                 {'identifier': ['x']}, {'identifier': 'x' * 321}, {'user_id': True},
                 {'user_id': world.mate, 'identifier': 5}):
        resp = _post(world.owner, world.rec, body)
        assert resp.status_code == 400 and resp.get_json()['code'] == 'user_id_required', body
    assert _shared_with(world.rec) == set()


# --- several people (user_ids, identifiers) ----------------------------------------

def test_the_share_window_payload_passes_at_every_level(world):
    """Each selected person as id plus the username the search returned."""
    for level in LEVELS:
        _level(level)
        _unshare(world.rec)
        resp = _post(world.owner, world.rec, {
            'user_ids': [world.mate, world.stranger],
            'identifiers': [world.name('mate'), world.name('stranger')]})
        assert _statuses(resp) == [(world.mate, None, 'shared'), (world.stranger, None, 'shared')], level
        assert resp.get_json()['shared_count'] == 2
        assert _shared_with(world.rec) == {world.mate, world.stranger}


def test_bare_ids_follow_the_level(world):
    ids = [world.mate, world.stranger, UNKNOWN_ID]
    expected = {
        'everyone': ['shared', 'shared', 'not_found'],
        'groups': ['shared', 'not_found', 'not_found'],
        'exact': ['not_found', 'not_found', 'not_found'],
    }
    for level in LEVELS:
        _level(level)
        _unshare(world.rec)
        resp = _post(world.owner, world.rec, {'user_ids': ids})
        assert [status for _, _, status in _statuses(resp)] == expected[level], level
        # A hidden account and an unknown id give the same row but for the id sent.
        rows = resp.get_json()['results']
        assert {k: v for k, v in rows[2].items() if k != 'user_id'} == {'status': 'not_found'}
        if level != 'everyone':
            assert {k: v for k, v in rows[1].items() if k != 'user_id'} == {'status': 'not_found'}


def test_identifiers_alone_share_by_name_and_email(world):
    _level('exact')
    resp = _post(world.owner, world.rec, {
        'identifiers': [world.name('mate'), world.email('stranger'), 'nobody-at-all', world.name('stra')]})
    assert _statuses(resp) == [
        (world.mate, world.name('mate'), 'shared'),
        (world.stranger, world.email('stranger'), 'shared'),
        (None, 'nobody-at-all', 'not_found'),
        (None, world.name('stra'), 'not_found'),
    ]
    assert _shared_with(world.rec) == {world.mate, world.stranger}


def test_an_identifier_covers_only_its_own_id(world):
    _level('exact')
    resp = _post(world.owner, world.rec, {'user_ids': [world.stranger, world.mate],
                                          'identifiers': [world.name('mate')]})
    assert _statuses(resp) == [(world.stranger, None, 'not_found'), (world.mate, None, 'shared')]
    assert _shared_with(world.rec) == {world.mate}


def test_one_person_named_twice_is_shared_with_once(world):
    _level('exact')
    resp = _post(world.owner, world.rec, {
        'user_ids': [world.mate], 'identifiers': [world.name('mate'), world.email('mate')]})
    assert _statuses(resp) == [(world.mate, None, 'shared')]
    resp = _post(world.owner, world.rec, {'identifiers': [world.email('mate'), world.name('owner')]})
    assert _statuses(resp) == [(world.mate, world.email('mate'), 'already_shared'),
                               (world.owner, world.name('owner'), 'share_with_owner')]


def test_an_existing_share_does_not_show_through_a_bare_id_in_a_list(world):
    _level('everyone')
    assert _post(world.owner, world.rec, {'user_id': world.stranger}).status_code == 201
    _level('exact')
    assert _statuses(_post(world.owner, world.rec, {'user_ids': [world.stranger]})) == [
        (world.stranger, None, 'not_found')]


def test_bad_lists_are_refused_without_writing(world):
    _level('everyone')
    names = [f"{world.tok}n{i}" for i in range(26)]
    bad = [
        ({'identifiers': []}, 'share_recipients_invalid'),
        ({'user_ids': [], 'identifiers': []}, 'share_recipients_invalid'),
        ({'identifiers': 'someone'}, 'share_recipients_invalid'),
        ({'identifiers': [world.name('mate'), '']}, 'share_recipients_invalid'),
        ({'identifiers': [world.name('mate'), 7]}, 'share_recipients_invalid'),
        ({'identifiers': ['x' * 321]}, 'share_recipients_invalid'),
        ({'identifiers': names}, 'share_recipients_invalid'),
        ({'user_ids': [world.mate], 'identifiers': None}, 'share_recipients_invalid'),
        ({'identifiers': [world.name('mate')], 'user_id': world.mate}, 'share_recipients_conflict'),
        ({'user_ids': [world.mate], 'identifier': world.name('mate')}, 'share_recipients_conflict'),
    ]
    for body, code in bad:
        resp = _post(world.owner, world.rec, body)
        assert resp.status_code == 400 and resp.get_json()['code'] == code, body
    # 25 ids and 25 names are each within the limit; together they name too many.
    resp = _post(world.owner, world.rec, {'user_ids': list(range(UNKNOWN_ID, UNKNOWN_ID + 25)),
                                          'identifiers': [world.name('mate')]})
    assert resp.status_code == 400 and resp.get_json()['code'] == 'share_recipients_invalid'
    assert _shared_with(world.rec) == set()


def test_the_list_of_shares_cannot_be_used_to_read_usernames(world):
    """The gap this closes: run through ids, then read the names back."""
    _level('exact')
    for user_id in (world.mate, world.stranger, world.admin):
        _post(world.owner, world.rec, {'user_id': user_id})
        _post(world.owner, world.rec, {'user_ids': [user_id]})
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(world.owner)
        s['_fresh'] = True
    with _internal_sharing():
        listing = client.get(f"/api/recordings/{world.rec}/shares-internal")
    assert listing.status_code == 200
    text = listing.get_data(as_text=True)
    for who in ('mate', 'stranger', 'root'):
        assert world.name(who) not in text
