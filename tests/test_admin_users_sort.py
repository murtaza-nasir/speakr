"""The sort of the admin user list (``GET /admin/users?sort=&order=``).

The list is paged and searched on the server, so every sort key is applied in
SQL, before the page is cut. Seven accounts with one tag in their names are
created for each test and found again with ``search=<tag>``, so the expected
orders do not depend on the other rows of the database:

====  =========  =====  =====  =====  ==========  =======  ===========
user  username   email  admin  share  recordings  storage  last active
====  =========  =====  =====  =====  ==========  =======  ===========
A     delta      m      no     yes    3           300      1 hour ago
B     Alpha      Z      yes    no     1           1000     never
C     charlie    b      NULL   NULL   0           0        200 days ago
D     Bravo      Q      no     yes    2           50       1 hour ago
E     echo       a      yes    yes    1           0        never
F     foxtrot    k      no     no     0           0        3 days ago
G     golf       g      no     yes    0           0        never
====  =========  =====  =====  =====  ==========  =======  ===========

The ids ascend from A to G. A has a recording with deleted audio (5000
bytes) and E has only such a recording (700 bytes): both are counted as
recordings and neither is counted as storage. One recording of D has no size.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, bcrypt  # noqa: E402
from src.models import Group, GroupMembership, Recording, User  # noqa: E402

PASSWORD_HASH = bcrypt.generate_password_hash('Passw0rd!').decode()

# name: (username, email, is_admin, can_share_publicly,
#        sizes of recordings with audio, sizes of recordings with deleted audio,
#        age of the last use or None)
HOUR = timedelta(hours=1)
PEOPLE = {
    'A': ('delta', 'm', False, True, [100, 200], [5000], HOUR),
    'B': ('Alpha', 'Z', True, False, [1000], [], None),
    'C': ('charlie', 'b', None, None, [], [], timedelta(days=200)),
    'D': ('Bravo', 'Q', False, True, [None, 50], [], HOUR),
    'E': ('echo', 'a', True, True, [], [700], None),
    'F': ('foxtrot', 'k', False, False, [], [], timedelta(days=3)),
    'G': ('golf', 'g', False, True, [], [], None),
}
DISPLAYED = {  # name: (recordings_count, storage_used)
    'A': (3, 300), 'B': (1, 1000), 'C': (0, 0), 'D': (2, 50),
    'E': (1, 0), 'F': (0, 0), 'G': (0, 0),
}
# key: (ascending order, descending order). Equal values are in the order of
# the ids in both directions.
ORDERS = {
    'id': ('ABCDEFG', 'GFEDCBA'),
    'username': ('BDCAEFG', 'GFEACDB'),
    'email': ('ECGFADB', 'BDAFGCE'),
    'is_admin': ('ACDFGBE', 'BEACDFG'),
    'can_share_publicly': ('BCFADEG', 'ADEGBCF'),
    'recordings_count': ('CFGBEDA', 'ADBECFG'),
    'storage_used': ('CEFGDAB', 'BADCEFG'),
    'last_active': ('BEGCFAD', 'ADFCBEG'),
}
EVERY_SORT = [(key, order) for key in ORDERS for order in ('asc', 'desc')]


class World:
    """The accounts of one test."""

    def __init__(self, tag, admin, users):
        self.tag = tag
        self.admin = admin
        self.users = users                                    # name -> User
        self.names = {user.id: name for name, user in users.items()}

    def letters(self, rows):
        """The listed rows as a string of names, 'BDCAEFG'."""
        return ''.join(self.names[row['id']] for row in rows)


def _client_for(user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess.clear()
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


@pytest.fixture
def world():
    with app.app_context():
        tag = uuid.uuid4().hex[:6]
        now = datetime.utcnow().replace(microsecond=0)
        admin = User(username=f'sortadm_{uuid.uuid4().hex[:8]}',
                     email=f'sortadm_{uuid.uuid4().hex[:8]}@local.test',
                     password=PASSWORD_HASH, is_admin=True)
        db.session.add(admin)
        db.session.commit()
        users, recording_ids = {}, []
        for name, (username, email, is_admin, share, live, removed, age) in PEOPLE.items():
            user = User(username=f'{username}{tag}', email=f'{email}{tag}@local.test',
                        password=PASSWORD_HASH,
                        last_seen_at=(now - age) if age is not None else None)
            db.session.add(user)
            db.session.commit()
            # Set after the insert: the column defaults would replace a None.
            user.is_admin = is_admin
            user.can_share_publicly = share
            db.session.commit()
            users[name] = user
            for size, deleted_at in [(s, None) for s in live] + [(s, now) for s in removed]:
                recording = Recording(user_id=user.id, title=f'sort {name}', status='COMPLETED',
                                      file_size=size, audio_deleted_at=deleted_at)
                db.session.add(recording)
                db.session.commit()
                recording_ids.append(recording.id)
        assert [u.id for u in users.values()] == sorted(u.id for u in users.values())
        try:
            yield World(tag, admin, users)
        finally:
            db.session.rollback()
            for recording_id in recording_ids:
                row = db.session.get(Recording, recording_id)
                if row is not None:
                    db.session.delete(row)
            db.session.commit()
            for user in [admin, *users.values()]:
                row = db.session.get(User, user.id)
                if row is not None:
                    GroupMembership.query.filter_by(user_id=row.id).delete()
                    db.session.delete(row)
            db.session.commit()


@pytest.fixture
def lead_client(world):
    """G as the admin of a group with A, C and E; B, D and F are out of scope."""
    group = Group(name=f'sort_{world.tag}')
    db.session.add(group)
    db.session.commit()
    db.session.add(GroupMembership(group_id=group.id, user_id=world.users['G'].id, role='admin'))
    for name in 'ACE':
        db.session.add(GroupMembership(group_id=group.id, user_id=world.users[name].id, role='member'))
    db.session.commit()
    try:
        yield _client_for(world.users['G'])
    finally:
        db.session.rollback()
        GroupMembership.query.filter_by(group_id=group.id).delete()
        db.session.delete(db.session.get(Group, group.id))
        db.session.commit()


def _listing(client, world, query='', search=None):
    search = world.tag if search is None else search
    resp = client.get(f'/admin/users?per_page=1000&search={search}&{query}')
    assert resp.status_code == 200, resp.data
    return resp.get_json()


# --- Every key, both directions ---------------------------------------------

@pytest.mark.parametrize('key, order', EVERY_SORT)
def test_the_list_is_ordered_by_each_key_in_both_directions(world, key, order):
    body = _listing(_client_for(world.admin), world, f'sort={key}&order={order}')

    assert world.letters(body['users']) == ORDERS[key][order == 'desc']
    assert (body['sort'], body['order']) == (key, order)
    assert body['pagination']['total'] == len(PEOPLE)


@pytest.mark.parametrize('key', ['username', 'EMAIL', 'Storage_Used'])
def test_ascending_is_the_direction_without_an_order_and_the_key_is_not_case_sensitive(world, key):
    client = _client_for(world.admin)
    expected = ORDERS[key.lower()][0]

    for query in (f'sort={key}', f'sort={key}&order=', f'sort={key}&order=sideways',
                  f'sort={key}&order=ASC'):
        body = _listing(client, world, query)
        assert world.letters(body['users']) == expected, query
        assert (body['sort'], body['order']) == (key.lower(), 'asc'), query
    assert world.letters(_listing(client, world, f'sort={key}&order=DESC')['users']) == (
        ORDERS[key.lower()][1])


def test_equal_values_are_in_the_order_of_the_ids_in_both_directions(world):
    client = _client_for(world.admin)
    ids = {name: user.id for name, user in world.users.items()}

    for key, field, tied in (('recordings_count', 'recordings_count', 'CFG'),
                             ('storage_used', 'storage_used', 'CEFG'),
                             ('is_admin', 'is_admin', 'BE'),
                             ('last_active', 'last_seen_at', 'AD')):
        for order in ('asc', 'desc'):
            rows = _listing(client, world, f'sort={key}&order={order}')['users']
            listed = [row['id'] for row in rows if world.names[row['id']] in tied]
            assert listed == sorted(ids[name] for name in tied), (key, order)
            assert len({row[field] for row in rows if row['id'] in listed}) == 1, (key, order)


# --- Pages -------------------------------------------------------------------

@pytest.mark.parametrize('key, order', EVERY_SORT)
@pytest.mark.parametrize('per_page', [1, 2, 3])
def test_the_pages_follow_the_order_without_a_repeated_or_missing_row(world, key, order, per_page):
    client = _client_for(world.admin)
    paged, page = [], 1
    while True:
        resp = client.get(f'/admin/users?search={world.tag}&per_page={per_page}&page={page}'
                          f'&sort={key}&order={order}')
        body = resp.get_json()
        assert len(body['users']) <= per_page
        assert body['pagination']['total'] == len(PEOPLE)
        assert (body['sort'], body['order']) == (key, order)
        paged.extend(body['users'])
        if not body['pagination']['has_next']:
            break
        page += 1

    assert page == -(-len(PEOPLE) // per_page)
    assert world.letters(paged) == ORDERS[key][order == 'desc']


def test_a_page_beyond_the_last_is_the_last_page_of_the_order(world):
    client = _client_for(world.admin)

    body = client.get(f'/admin/users?search={world.tag}&per_page=3&page=99'
                      '&sort=storage_used&order=desc').get_json()

    assert body['pagination']['page'] == 3
    assert world.letters(body['users']) == ORDERS['storage_used'][1][6:]


# --- Search ------------------------------------------------------------------

@pytest.mark.parametrize('key, order', EVERY_SORT)
def test_a_search_is_ordered_by_the_key(world, key, order):
    # "a<tag>" is the end of the usernames of A (delta) and B (Alpha) and the
    # start of the email of E.
    body = _listing(_client_for(world.admin), world, f'sort={key}&order={order}',
                    search=f'a{world.tag}')

    expected = ''.join(name for name in ORDERS[key][order == 'desc'] if name in 'ABE')
    assert world.letters(body['users']) == expected
    assert body['pagination']['total'] == 3


def test_a_search_without_a_match_is_an_empty_first_page(world):
    body = _listing(_client_for(world.admin), world, 'sort=recordings_count&order=desc',
                    search=f'nobody{world.tag}')

    assert body['users'] == []
    assert body['pagination']['total'] == 0
    assert body['pagination']['page'] == 1
    assert (body['sort'], body['order']) == ('recordings_count', 'desc')


# --- Unknown keys ------------------------------------------------------------

@pytest.mark.parametrize('query, expected_order', [
    ('', 'asc'),
    ('sort=&order=', 'asc'),
    ('sort=password', 'asc'),
    ('sort=storage_quota_mb&order=asc', 'asc'),
    ('sort=id;drop%20table%20user&order=desc', 'desc'),
    ('sort=password&order=desc', 'desc'),
    ('order=desc', 'desc'),
])
def test_an_unknown_or_missing_key_is_the_order_by_id(world, query, expected_order):
    resp = _client_for(world.admin).get(
        f'/admin/users?per_page=1000&search={world.tag}&{query}')

    assert resp.status_code == 200
    body = resp.get_json()
    assert world.letters(body['users']) == ORDERS['id'][expected_order == 'desc']
    assert (body['sort'], body['order']) == ('id', expected_order)


# --- The totals --------------------------------------------------------------

def test_the_totals_in_the_rows_are_the_values_of_the_order(world):
    client = _client_for(world.admin)

    for key, position in (('recordings_count', 0), ('storage_used', 1)):
        for order in ('asc', 'desc'):
            rows = _listing(client, world, f'sort={key}&order={order}')['users']
            shown = {world.names[row['id']]: (row['recordings_count'], row['storage_used'])
                     for row in rows}
            assert shown == DISPLAYED
            # The order of the rows is the order of the displayed numbers,
            # with the id for equal numbers.
            by_value = sorted(rows, key=lambda row: row['id'])
            by_value.sort(key=lambda row: row[key], reverse=(order == 'desc'))
            assert [row['id'] for row in rows] == [row['id'] for row in by_value]
            assert [row[key] for row in rows] == sorted(
                (numbers[position] for numbers in DISPLAYED.values()), reverse=(order == 'desc'))


def test_deleted_audio_counts_as_a_recording_and_not_as_storage(world):
    client = _client_for(world.admin)
    rows = {world.names[row['id']]: row
            for row in _listing(client, world, 'sort=storage_used&order=desc')['users']}

    # E has one recording, whose audio was deleted.
    assert (rows['E']['recordings_count'], rows['E']['storage_used']) == (1, 0)
    # By storage E is with the accounts that have none; by count it is ahead of them.
    assert ORDERS['storage_used'][1].index('E') > ORDERS['storage_used'][1].index('D')
    by_count = world.letters(_listing(client, world, 'sort=recordings_count&order=desc')['users'])
    assert by_count.index('E') < by_count.index('C')


def test_a_user_with_several_recordings_is_listed_once(world):
    """The totals are joined as one row for each user."""
    client = _client_for(world.admin)

    for key in ('recordings_count', 'storage_used'):
        body = client.get(f'/admin/users?search={world.tag}&per_page=2&page=4'
                          f'&sort={key}&order=asc').get_json()
        assert body['pagination']['total'] == len(PEOPLE)
        assert body['pagination']['total_pages'] == 4
        assert world.letters(body['users']) == ORDERS[key][0][6:]


# --- Group admins ------------------------------------------------------------

@pytest.mark.parametrize('key, order', [pair for pair in EVERY_SORT if pair[0] != 'last_active'])
def test_a_group_admin_is_given_the_members_only_in_each_order(world, lead_client, key, order):
    # No search: the scope alone must keep B, D, F and every other account out.
    resp = lead_client.get(f'/admin/users?per_page=1000&sort={key}&order={order}')
    assert resp.status_code == 200
    body = resp.get_json()

    assert {row['id'] for row in body['users']} == {world.users[name].id for name in 'ACEG'}
    expected = ''.join(name for name in ORDERS[key][order == 'desc'] if name in 'ACEG')
    assert world.letters(body['users']) == expected
    assert (body['sort'], body['order']) == (key, order)
    assert body['pagination']['total'] == 4
    assert all('last_seen_at' not in row for row in body['users'])
    shown = {world.names[row['id']]: (row['recordings_count'], row['storage_used'])
             for row in body['users']}
    assert shown == {name: DISPLAYED[name] for name in 'ACEG'}


@pytest.mark.parametrize('key, order', [pair for pair in EVERY_SORT if pair[0] != 'last_active'])
def test_the_pages_of_a_group_admin_follow_the_order(world, lead_client, key, order):
    paged = []
    for page in (1, 2):
        body = lead_client.get(
            f'/admin/users?per_page=2&page={page}&sort={key}&order={order}').get_json()
        assert body['pagination']['total_pages'] == 2
        paged.extend(body['users'])

    expected = ''.join(name for name in ORDERS[key][order == 'desc'] if name in 'ACEG')
    assert world.letters(paged) == expected


@pytest.mark.parametrize('order', ['asc', 'desc'])
def test_last_active_is_not_a_key_for_a_group_admin(world, lead_client, order):
    resp = lead_client.get(f'/admin/users?per_page=1000&sort=last_active&order={order}')
    assert resp.status_code == 200
    body = resp.get_json()

    # By last active A (an hour ago) and C (200 days ago) would not be in the
    # order of their ids in either direction next to E and G (never, and G
    # has just made this request): the order is by id, and it is reported so.
    expected = ''.join(name for name in ORDERS['id'][order == 'desc'] if name in 'ACEG')
    assert world.letters(body['users']) == expected
    assert (body['sort'], body['order']) == ('id', order)
    assert all('last_seen_at' not in row for row in body['users'])


def test_a_search_of_a_group_admin_stays_in_the_scope(world, lead_client):
    # "a<tag>" matches A, B and E; B is not a member of the group.
    body = lead_client.get(
        f'/admin/users?per_page=1000&search=a{world.tag}&sort=storage_used&order=desc').get_json()

    assert world.letters(body['users']) == 'AE'
    assert body['pagination']['total'] == 2


# --- Who is refused ----------------------------------------------------------

def test_an_account_that_administers_nothing_is_refused_with_every_key(world):
    client = _client_for(world.users['F'])

    for key in ORDERS:
        assert client.get(f'/admin/users?sort={key}&order=desc').status_code == 403
