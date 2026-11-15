"""Who a signed-in person can find: the user_search_scope setting.

GET /api/users/search and the group page's add-member both go through
src/services/user_lookup.py. Three levels:

- everyone: part of a username finds any account
- groups:   part of a username finds only people in a shared group; anyone
            else needs the full username or the full email address
- exact:    the full username or the full email address only

At every level an email address matches only in full (ignoring case), a
result has no email address unless a site admin searches, the person
searching is left out, a query needs two characters and at most ten accounts
come back. Unset, the level follows show_usernames_in_ui.

SHARED-DB: every row made here is removed afterwards, and every assertion
is about names carrying the test's own random token.
"""

import os
import sys
import uuid
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.app as app_module
import src.api.groups as groups_module
import src.api.shares as shares_module
import src.models.system as system_models
from src.app import app, db
from src.models import Group, GroupMembership, SystemSetting, User
from src.services import user_lookup

app.config['WTF_CSRF_ENABLED'] = False

LEVELS = ('everyone', 'groups', 'exact')


@contextmanager
def _internal_sharing(value=True):
    modules = (app_module, shares_module, groups_module)
    saved = [(m, m.__dict__.get('ENABLE_INTERNAL_SHARING', None), 'ENABLE_INTERNAL_SHARING' in m.__dict__)
             for m in modules]
    for m in modules:
        setattr(m, 'ENABLE_INTERNAL_SHARING', value)
    try:
        yield
    finally:
        for m, old, had in saved:
            if had:
                setattr(m, 'ENABLE_INTERNAL_SHARING', old)
            else:
                delattr(m, 'ENABLE_INTERNAL_SHARING')


def _save(key, value, setting_type='string'):
    with app.app_context():
        SystemSetting.set_setting(key, value, None, setting_type)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """Both settings start unsaved and are put back afterwards."""
    monkeypatch.delenv('USER_SEARCH_SCOPE', raising=False)
    monkeypatch.delenv('SHOW_USERNAMES_IN_UI', raising=False)
    keys = ('user_search_scope', 'show_usernames_in_ui')
    with app.app_context():
        saved = {}
        for key in keys:
            row = SystemSetting.query.filter_by(key=key).first()
            saved[key] = None if row is None else (row.value, row.setting_type)
    _save('user_search_scope', None)
    _save('show_usernames_in_ui', None, 'boolean')
    yield
    with app.app_context():
        for key in keys:
            if saved[key] is None:
                row = SystemSetting.query.filter_by(key=key).first()
                if row is not None:
                    db.session.delete(row)
                    db.session.commit()
            else:
                SystemSetting.set_setting(key, saved[key][0], None, saved[key][1])


class World:
    """me, a colleague in a shared group, a stranger, and a site admin."""

    def __init__(self):
        self.tok = uuid.uuid4().hex[:8]
        self.user_ids = []
        self.group_ids = []

    def user(self, name, email=None, is_admin=False):
        user = User(username=name, email=email or f"{name}@local.test", password="x", is_admin=is_admin)
        db.session.add(user)
        db.session.commit()
        self.user_ids.append(user.id)
        return user.id

    def group(self, name, members, admins=()):
        group = Group(name=name)
        db.session.add(group)
        db.session.commit()
        for user_id in members:
            db.session.add(GroupMembership(group_id=group.id, user_id=user_id,
                                           role='admin' if user_id in admins else 'member'))
        db.session.commit()
        self.group_ids.append(group.id)
        return group.id

    def cleanup(self):
        with app.app_context():
            GroupMembership.query.filter(GroupMembership.user_id.in_(self.user_ids)).delete(synchronize_session=False)
            Group.query.filter(Group.id.in_(self.group_ids)).delete(synchronize_session=False)
            User.query.filter(User.id.in_(self.user_ids)).delete(synchronize_session=False)
            db.session.commit()


@pytest.fixture
def world():
    w = World()
    t = w.tok
    with app.app_context():
        w.me = w.user(f"{t}me", f"{t}me@Corp.local.test")
        w.mate = w.user(f"{t}mate", f"{t}Mate@Corp.local.test")
        w.stranger = w.user(f"{t}stranger", f"{t}stranger@corp.local.test")
        w.admin = w.user(f"{t}root", f"{t}root@corp.local.test", is_admin=True)
        w.shared = w.group(f"{t} shared", [w.me, w.mate], admins=[w.me])
        w.other = w.group(f"{t} other", [w.stranger])
    try:
        yield w
    finally:
        w.cleanup()


def _client(user_id):
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(user_id)
        s['_fresh'] = True
    return client


def _search(user_id, q):
    with _internal_sharing():
        resp = _client(user_id).get('/api/users/search', query_string={'q': q})
    assert resp.status_code == 200, resp.data
    return resp.get_json()


def _names(user_id, q):
    return [u['username'] for u in _search(user_id, q)]


# --- the three levels ------------------------------------------------------------

def test_everyone_finds_any_account_by_part_of_a_username(world):
    _save('user_search_scope', 'everyone')
    t = world.tok
    assert _names(world.me, t) == [f"{t}mate", f"{t}root", f"{t}stranger"]
    assert _names(world.me, f"{t[2:]}stran") == [f"{t}stranger"]
    assert _names(world.me, f"{t}STRAN") == [f"{t}stranger"]


def test_groups_finds_only_people_in_a_shared_group_by_part_of_a_username(world):
    _save('user_search_scope', 'groups')
    t = world.tok
    assert _names(world.me, t) == [f"{t}mate"]
    assert _names(world.me, f"{t}stran") == []
    # The stranger is in a group, but not in one of mine
    assert _names(world.stranger, t) == []


def test_groups_finds_anyone_by_full_username_or_full_email(world):
    _save('user_search_scope', 'groups')
    t = world.tok
    assert _names(world.me, f"{t}stranger") == [f"{t}stranger"]
    assert _names(world.me, f"{t}STRANGER@corp.local.test") == [f"{t}stranger"]
    # A full username that is also part of a colleague's name lists both, the exact one first
    with app.app_context():
        world.user(f"{t}mat")
    assert _names(world.me, f"{t}mat") == [f"{t}mat", f"{t}mate"]


def test_groups_without_any_group_is_exact_only(world):
    _save('user_search_scope', 'groups')
    t = world.tok
    with app.app_context():
        loner = world.user(f"{t}loner")
    assert _names(loner, t) == []
    assert _names(loner, f"{t}mate") == [f"{t}mate"]


def test_exact_finds_only_the_full_username_or_the_full_email(world):
    _save('user_search_scope', 'exact')
    t = world.tok
    assert _names(world.me, t) == []
    assert _names(world.me, f"{t}mat") == []
    assert _names(world.me, f"{t}mate") == [f"{t}mate"]
    assert _names(world.me, f"{t}mate@corp.local.test") == [f"{t}mate"]


# --- rules of every level ----------------------------------------------------------

@pytest.mark.parametrize('level', LEVELS)
def test_a_full_email_matches_whatever_its_case(world, level):
    _save('user_search_scope', level)
    t = world.tok
    for typed in (f"{t}Mate@Corp.local.test", f"{t}mate@corp.local.test", f"{t.upper()}MATE@CORP.LOCAL.TEST"):
        assert _names(world.me, typed) == [f"{t}mate"], typed


@pytest.mark.parametrize('level', LEVELS)
def test_part_of_an_email_finds_nobody(world, level):
    _save('user_search_scope', level)
    t = world.tok
    for typed in ('corp.local.test', '@corp.local.test', f"{t}mate@corp", f"{t}mate@corp.local.tes",
                  f"{t}mate@corp.local.test.", f"mate@corp.local.test", '%@corp.local.test',
                  f"{t}mate@corp.local.tes_"):
        assert _search(world.me, typed) == [], typed
    # The colleague is found by name, and then by nothing of the address
    assert _names(world.mate, 'Corp') == []


@pytest.mark.parametrize('level', LEVELS)
def test_a_result_has_no_email_for_a_non_admin(world, level):
    _save('user_search_scope', level)
    t = world.tok
    for typed in (f"{t}mate", f"{t}mate@corp.local.test"):
        found = _search(world.me, typed)
        assert found == [{'id': world.mate, 'username': f"{t}mate"}]
        assert 'local.test' not in str(found)


@pytest.mark.parametrize('level', LEVELS)
def test_a_site_admin_sees_the_email_and_follows_the_same_level(world, level):
    _save('user_search_scope', level)
    t = world.tok
    found = _search(world.admin, f"{t}mate")
    assert found == [{'id': world.mate, 'username': f"{t}mate", 'email': f"{t}Mate@Corp.local.test"}]
    assert _search(world.admin, f"{t}mate@corp") == []
    by_part = _names(world.admin, f"{t}ma")
    assert by_part == ([f"{t}mate"] if level == 'everyone' else [])


@pytest.mark.parametrize('level', LEVELS)
def test_the_person_searching_is_left_out(world, level):
    _save('user_search_scope', level)
    t = world.tok
    assert _names(world.me, f"{t}me") == []
    assert _names(world.me, f"{t}me@corp.local.test") == []


@pytest.mark.parametrize('level', LEVELS)
def test_a_query_needs_two_characters(world, level):
    _save('user_search_scope', level)
    with app.app_context():
        one = world.user(uuid.uuid4().hex[:1] + 'é')  # a two-character name, to prove the bound
        name = db.session.get(User, one).username
    assert _search(world.me, '') == []
    assert _search(world.me, name[0]) == []
    assert _search(world.me, f" {name[0]} ") == []
    assert _names(world.me, name) == [name]


def test_at_most_ten_accounts_with_the_exact_one_first(world):
    _save('user_search_scope', 'everyone')
    t = world.tok
    with app.app_context():
        for i in range(12):
            world.user(f"{t}z{i:02d}")
        world.user(f"{t}z")
    names = _names(world.me, f"{t}z")
    assert len(names) == 10
    assert names == [f"{t}z"] + [f"{t}z{i:02d}" for i in range(9)]


def test_internal_sharing_off_refuses_the_search(world):
    _save('user_search_scope', 'everyone')
    with _internal_sharing(False):
        resp = _client(world.me).get('/api/users/search', query_string={'q': world.tok})
    assert resp.status_code == 403


def test_the_search_needs_a_sign_in(world):
    resp = app.test_client().get('/api/users/search', query_string={'q': world.tok})
    assert resp.status_code in (302, 401)


# --- the search limit ------------------------------------------------------------

def test_searches_are_limited_per_person(world):
    """30 a minute for each signed-in person; then 429 with Retry-After.

    RATELIMIT_ENABLED is false under test, so an enabled limiter that counts
    in memory stands in for the app's own (as in test_api_token_scopes.py).
    One test only: the route keeps the limiter it was first called with.
    """
    from unittest.mock import patch
    from flask import Flask
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    assert shares_module.USER_SEARCH_RATE_LIMIT == '30 per minute'
    assert shares_module.limiter is app_module.limiter      # injected by the app
    live = Limiter(get_remote_address, storage_uri='memory://', enabled=True)
    live.init_app(Flask('limiter-host'))
    _save('user_search_scope', 'exact')
    with patch.object(app_module, 'limiter', live), patch.object(shares_module, 'limiter', live), \
         _internal_sharing():
        me = _client(world.me)
        codes = [me.get('/api/users/search', query_string={'q': f"{world.tok}{i}@corp.local.test"}).status_code
                 for i in range(30)]
        assert codes == [200] * 30
        refused = me.get('/api/users/search', query_string={'q': f"{world.tok}mate@corp.local.test"})
        assert refused.status_code == 429
        assert int(refused.headers['Retry-After']) >= 1
        assert world.tok not in refused.get_data(as_text=True)
        # The limit is counted per person, not per address: a colleague on
        # the same address still searches.
        other = _client(world.mate).get('/api/users/search', query_string={'q': f"{world.tok}me"})
        assert other.status_code == 200 and [u['id'] for u in other.get_json()] == [world.me]
        # No sign-in: refused before the limit is counted.
        assert app.test_client().get('/api/users/search', query_string={'q': 'xx'}).status_code in (302, 401)


def test_the_share_window_has_a_message_for_the_search_limit():
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = open(os.path.join(root, 'static', 'js', 'modules', 'composables', 'sharing.js'), encoding='utf-8').read()
    assert script.count("t('sharing.searchRateLimited')") == 2      # the search and Share by name
    assert script.count('status === 429') == 2
    for lang in ('en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh'):
        data = json.load(open(os.path.join(root, 'static', 'locales', f'{lang}.json'), encoding='utf-8'))
        assert data['sharing']['searchRateLimited'].strip(), lang


# --- the level in force ----------------------------------------------------------

def _scope():
    with app.app_context():
        return user_lookup.search_scope()


def test_unset_the_level_follows_the_username_setting(world, monkeypatch):
    assert _scope() == 'exact'                       # show_usernames_in_ui is off by default
    monkeypatch.setenv('SHOW_USERNAMES_IN_UI', 'true')
    assert _scope() == 'everyone'
    monkeypatch.setenv('SHOW_USERNAMES_IN_UI', 'false')
    assert _scope() == 'exact'
    _save('show_usernames_in_ui', 'true', 'boolean')  # saved in System Settings, over .env
    assert _scope() == 'everyone'
    t = world.tok
    assert _names(world.me, f"{t}stran") == [f"{t}stranger"]
    _save('show_usernames_in_ui', 'false', 'boolean')
    assert _scope() == 'exact'
    assert _names(world.me, f"{t}stran") == []


def test_the_env_value_is_used_over_the_username_setting(monkeypatch):
    monkeypatch.setenv('SHOW_USERNAMES_IN_UI', 'true')
    monkeypatch.setenv('USER_SEARCH_SCOPE', ' Groups ')
    assert _scope() == 'groups'
    monkeypatch.setenv('USER_SEARCH_SCOPE', 'nonsense')
    assert _scope() == 'everyone'
    monkeypatch.setenv('USER_SEARCH_SCOPE', '')
    assert _scope() == 'everyone'


def test_the_saved_setting_is_used_over_env(monkeypatch):
    monkeypatch.setenv('USER_SEARCH_SCOPE', 'everyone')
    monkeypatch.setenv('SHOW_USERNAMES_IN_UI', 'true')
    _save('user_search_scope', 'exact')
    assert _scope() == 'exact'


def test_the_username_setting_keeps_its_own_meaning(world):
    """A saved level does not change whether owner usernames are displayed."""
    _save('user_search_scope', 'everyone')
    with app.app_context():
        assert shares_module.SHOW_USERNAMES_IN_UI is False
    _save('user_search_scope', 'exact')
    _save('show_usernames_in_ui', 'true', 'boolean')
    with app.app_context():
        assert shares_module.SHOW_USERNAMES_IN_UI is True
        assert user_lookup.search_scope() == 'exact'


def test_the_setting_is_env_backed():
    from src.utils.system_settings import ENV_BACKED_SETTINGS
    assert ENV_BACKED_SETTINGS['user_search_scope'][:3] == ('USER_SEARCH_SCOPE', '', 'string')


def test_admin_settings_lists_the_level_in_force_and_validates(world, monkeypatch):
    admin = _client(world.admin)
    row = next(r for r in admin.get('/admin/settings').get_json() if r['key'] == 'user_search_scope')
    assert (row['value'], row['source'], row['env_backed']) == ('exact', 'env', True)
    monkeypatch.setenv('SHOW_USERNAMES_IN_UI', 'true')
    row = next(r for r in admin.get('/admin/settings').get_json() if r['key'] == 'user_search_scope')
    assert row['value'] == 'everyone'

    resp = admin.post('/admin/settings', json={'key': 'user_search_scope', 'value': 'friends'})
    assert resp.status_code == 400 and resp.get_json()['code'] == 'invalid_user_search_scope'
    resp = admin.post('/admin/settings', json={'key': 'user_search_scope', 'value': ' Groups '})
    assert resp.status_code == 200
    assert _scope() == 'groups'
    row = next(r for r in admin.get('/admin/settings').get_json() if r['key'] == 'user_search_scope')
    assert row['value'] == 'groups' and 'source' not in row

    # Cleared, the level follows .env and the username setting again
    assert admin.post('/admin/settings', json={'key': 'user_search_scope', 'value': ''}).status_code == 200
    assert _scope() == 'everyone'
    assert _client(world.me).post('/admin/settings',
                                  json={'key': 'user_search_scope', 'value': 'everyone'}).status_code == 403


def test_the_frontend_config_carries_the_level(world):
    _save('user_search_scope', 'groups')
    config = _client(world.me).get('/api/config').get_json()
    assert config['user_search_scope'] == 'groups'
    _save('user_search_scope', None)
    assert _client(world.me).get('/api/config').get_json()['user_search_scope'] == 'exact'


# --- adding a member to a group ----------------------------------------------------

def _add(actor_id, group_id, **body):
    with _internal_sharing():
        return _client(actor_id).post(f"/api/admin/groups/{group_id}/members", json=body)


def _member_ids(group_id):
    with app.app_context():
        return {m.user_id for m in GroupMembership.query.filter_by(group_id=group_id).all()}


@pytest.mark.parametrize('level', LEVELS)
def test_a_group_admin_adds_by_full_username_or_email_at_every_level(world, level):
    _save('user_search_scope', level)
    t = world.tok
    with app.app_context():
        second = world.user(f"{t}second", f"{t}Second@corp.local.test")
    resp = _add(world.me, world.shared, identifier=f"{t}stranger")
    assert resp.status_code == 201, resp.data
    assert 'email' not in resp.get_json()
    assert _add(world.me, world.shared, identifier=f" {t}SECOND@CORP.local.test ").status_code == 201
    assert {world.stranger, second} <= _member_ids(world.shared)


@pytest.mark.parametrize('level', LEVELS)
def test_a_group_admin_cannot_add_by_part_of_a_name_or_by_id(world, level):
    _save('user_search_scope', level)
    t = world.tok
    for body in ({'identifier': f"{t}stran"}, {'identifier': f"{t}stranger@corp"},
                 {'identifier': '@corp.local.test'}, {'identifier': f"{t}STRANGER"}):
        resp = _add(world.me, world.shared, **body)
        assert resp.status_code == 404 and resp.get_json()['code'] == 'user_not_found', body
    resp = _add(world.me, world.shared, user_id=world.stranger)
    assert resp.status_code == 400 and resp.get_json()['code'] == 'member_identifier_required'
    assert world.stranger not in _member_ids(world.shared)


def test_a_group_member_who_is_not_its_admin_cannot_add(world):
    resp = _add(world.mate, world.shared, identifier=f"{world.tok}stranger")
    assert resp.status_code == 403


def test_a_site_admin_still_adds_by_user_id(world):
    _save('user_search_scope', 'exact')
    assert _add(world.admin, world.shared, user_id=world.stranger).status_code == 201
    assert world.stranger in _member_ids(world.shared)


def test_the_group_route_and_the_search_use_one_lookup(world):
    assert groups_module._find_user_exactly.__module__ == groups_module.__name__
    t = world.tok
    with app.app_context():
        assert groups_module._find_user_exactly(f"{t}mate").id == world.mate
        assert user_lookup.find_user_exactly(f"{t}MATE@corp.local.test").id == world.mate
        assert user_lookup.find_user_exactly(f"{t}mat") is None
        assert user_lookup.find_user_exactly('') is None
        assert user_lookup.find_user_exactly(None) is None


def test_the_member_list_of_a_group_has_no_email(world):
    with _internal_sharing():
        resp = _client(world.me).get(f'/api/admin/groups/{world.shared}')
    assert resp.status_code == 200
    body = resp.get_json()
    assert {m['username'] for m in body['members']} == {f"{world.tok}me", f"{world.tok}mate"}
    assert 'local.test' not in resp.get_data(as_text=True)
    # A member who is not an admin of the group does not get the list
    with _internal_sharing():
        assert _client(world.mate).get(f'/api/admin/groups/{world.shared}').status_code == 403
