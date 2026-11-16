"""Audit log: the internal routes of the admin page.

GET /admin/audit-log (filters, keyset paging) and GET /admin/audit-log/export
(CSV and JSON lines, the rows of the table unchanged). Site admins only: a
group admin, an ordinary account and a scoped token are refused.
"""

import csv
import io
import json
from datetime import datetime, timedelta

import pytest

from tests.audit_log_support import (  # noqa: E402
    anonymous_client, api_token, app, audit, bearer, client_for, db, make_user, max_id,
    remove_users, reset_audit_settings, rows_after, set_setting,
)
from src.models import AuditEvent, Group, GroupMembership, SystemSetting, User  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

# Ids no other test uses, so that the filters select exactly these rows.
REC = 7400000


@pytest.fixture(autouse=True)
def created():
    with app.app_context():
        reset_audit_settings()
        audit.flush()
        audit.reset_memory()
        users = []
        yield users
        audit.flush()
        db.session.rollback()
        SystemSetting.set_setting('audit_log_notice_dismissed', 'false', None, 'boolean')
        for group in Group.query.filter(Group.name.like('audrt%')).all():
            GroupMembership.query.filter_by(group_id=group.id).delete()
            db.session.delete(group)
        db.session.commit()
        remove_users(users)


def _seed(actor, target, recording_id, count=12):
    """Rows of several kinds for one recording id, a minute apart, oldest first."""
    base = datetime.utcnow() - timedelta(days=3)
    ids = []
    for i in range(count):
        kind = i % 4
        if kind == 0:
            row = audit.record_in_tx('recording.edited', recording_id=recording_id, recording_owner_id=actor.id,
                                     actor_kind='session', actor_user_id=actor.id, details={'fields': ['title']},
                                     created_at=base + timedelta(minutes=i))
        elif kind == 1:
            row = audit.record_in_tx('recording.downloaded', recording_id=recording_id, recording_owner_id=actor.id,
                                     actor_kind='api_token', actor_user_id=actor.id, actor_token_id=77,
                                     outcome='denied', details={'part': 'summary', 'format': 'md'},
                                     created_at=base + timedelta(minutes=i))
        elif kind == 2:
            row = audit.record_in_tx('share.internal.created', recording_id=recording_id, recording_owner_id=actor.id,
                                     actor_kind='session', actor_user_id=actor.id, target_user_id=target.id,
                                     details={'share_id': 5, 'can_edit': True, 'can_reshare': False},
                                     created_at=base + timedelta(minutes=i))
        else:
            row = audit.record_in_tx('share.public.opened', recording_id=recording_id, recording_owner_id=actor.id,
                                     actor_kind='public_link', details={'share_id': 9},
                                     created_at=base + timedelta(minutes=i))
        db.session.flush()
        ids.append(row.id)
    db.session.commit()
    return base, ids


def _get(client, query='', **kwargs):
    return client.get('/admin/audit-log' + (('?' + query) if query else ''), **kwargs)


def test_access_is_for_site_admins_only(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    user = make_user(created)
    group_admin = make_user(created, prefix='gad')
    group = Group(name='audrt' + str(max_id()), created_by=admin.id)
    db.session.add(group)
    db.session.flush()
    db.session.add(GroupMembership(group_id=group.id, user_id=group_admin.id, role='admin'))
    db.session.commit()

    for path in ('/admin/audit-log', '/admin/audit-log/export?format=csv'):
        assert anonymous_client().get(path).status_code in (302, 401)
        assert client_for(user).get(path).status_code == 403
        assert client_for(group_admin).get(path).status_code == 403
        assert client_for(admin).get(path).status_code == 200

        full, _ = api_token(admin)
        assert anonymous_client().get(path, headers=bearer(full)).status_code == 200
        user_full, _ = api_token(user)
        assert anonymous_client().get(path, headers=bearer(user_full)).status_code == 403
        for scopes in ('["read"]', '["read", "write", "account"]'):
            scoped, _ = api_token(admin, scopes=scopes)
            resp = anonymous_client().get(path, headers=bearer(scoped))
            assert resp.status_code == 403 and resp.get_json()['code'] == 'insufficient_scope'


def test_only_get_is_routed():
    rules = [rule for rule in app.url_map.iter_rules() if rule.rule.startswith('/admin/audit-log')]
    assert sorted(rule.rule for rule in rules) == ['/admin/audit-log', '/admin/audit-log/export']
    for rule in rules:
        assert rule.methods - {'HEAD', 'OPTIONS'} == {'GET'}


def test_filters(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    target = make_user(created, prefix='tgt')
    rec = REC + 1
    base, ids = _seed(admin, target, rec)
    client = client_for(admin)

    def listed(query):
        resp = _get(client, f'recording_id={rec}&limit=500&' + query)
        assert resp.status_code == 200, resp.data[:300]
        return [row['id'] for row in resp.get_json()['events']]

    newest_first = list(reversed(ids))
    assert listed('') == newest_first
    assert listed('group=share') == [i for n, i in reversed(list(enumerate(ids))) if n % 4 in (2, 3)]
    assert listed('event=recording.edited') == [i for n, i in reversed(list(enumerate(ids))) if n % 4 == 0]
    assert listed('outcome=denied') == [i for n, i in reversed(list(enumerate(ids))) if n % 4 == 1]
    assert listed('actor_kind=public_link') == [i for n, i in reversed(list(enumerate(ids))) if n % 4 == 3]
    # User: the actor or the target, by id or by name in any case.
    assert listed(f'user={target.id}') == [i for n, i in reversed(list(enumerate(ids))) if n % 4 == 2]
    assert listed(f'user={target.username.upper()}') == listed(f'user={target.id}')
    assert listed(f'user={admin.id}') == [i for n, i in reversed(list(enumerate(ids))) if n % 4 != 3]
    assert listed('user=nobody-with-this-name') == []
    # From is inclusive, To is exclusive.
    start = (base + timedelta(minutes=3)).isoformat() + 'Z'
    end = (base + timedelta(minutes=6)).isoformat() + 'Z'
    assert listed(f'from={start}&to={end}') == [ids[5], ids[4], ids[3]]
    assert listed(f'from={(base + timedelta(days=1)).date().isoformat()}') == []
    assert listed(f'to={(base + timedelta(days=1)).date().isoformat()}') == newest_first

    for bad in ('from=yesterday', 'recording_id=abc', 'group=everything', 'outcome=ok', 'actor_kind=robot',
                'event=recording.opened', 'limit=0', 'limit=501', 'limit=many'):
        assert _get(client, bad).status_code == 400, bad


def test_keyset_paging_and_names(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    target = make_user(created, prefix='tgt')
    rec = REC + 2
    base, ids = _seed(admin, target, rec, count=11)
    client = client_for(admin)
    newest_first = list(reversed(ids))

    first = _get(client, f'recording_id={rec}&limit=4').get_json()
    assert [row['id'] for row in first['events']] == newest_first[:4]
    assert first['has_older'] is True and first['has_newer'] is False
    second = _get(client, f'recording_id={rec}&limit=4&before_id={first["events"][-1]["id"]}').get_json()
    assert [row['id'] for row in second['events']] == newest_first[4:8]
    assert second['has_older'] is True and second['has_newer'] is True
    third = _get(client, f'recording_id={rec}&limit=4&before_id={second["events"][-1]["id"]}').get_json()
    assert [row['id'] for row in third['events']] == newest_first[8:]
    assert third['has_older'] is False
    back = _get(client, f'recording_id={rec}&limit=4&after_id={third["events"][0]["id"]}').get_json()
    assert [row['id'] for row in back['events']] == newest_first[4:8]
    top = _get(client, f'recording_id={rec}&limit=4&after_id={newest_first[0]}').get_json()
    assert top['events'] == [] and top['has_newer'] is False

    # Names are resolved for the accounts that exist; a deleted one is absent.
    assert first['users'][str(admin.id)] == admin.username
    row = AuditEvent.query.filter_by(id=ids[2]).first()
    page = _get(client, f'recording_id={rec}&event=share.internal.created').get_json()
    assert page['users'][str(target.id)] == target.username
    assert page['events'][0]['details'] == row.details
    target_id = target.id
    db.session.delete(db.session.get(User, target_id))
    db.session.commit()
    created.remove(target_id)
    page = _get(client, f'recording_id={rec}&event=share.internal.created').get_json()
    assert str(target_id) not in page['users'] and page['events'][0]['target_user_id'] == target_id
    # Every column of the table, and nothing else.
    assert set(page['events'][0]) == set(audit.COLUMNS)


def test_reading_the_log_is_recorded_once_per_half_hour_also_while_it_is_off(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    client = client_for(admin)
    set_setting('audit_log_enabled', 'false')
    start = max_id()
    # A time bucket of its own: the id of a deleted account is given out again
    # on SQLite, and another test read the log with the same id.
    from unittest import mock
    import random
    later = audit.time.time() + 86400 * random.randint(400, 4000)
    with mock.patch.object(audit, '_epoch', return_value=later):
        for _ in range(3):
            resp = _get(client, 'group=auth&outcome=failed')
            assert resp.status_code == 200 and resp.get_json()['enabled'] is False
    rows = rows_after(start)
    assert [(r.event, r.actor_user_id, r.details) for r in rows] == [
        ('audit.viewed', admin.id, {'filters': ['group', 'outcome']})]


def test_export_gives_the_rows_of_the_table(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    target = make_user(created, prefix='tgt')
    rec = REC + 3
    base, ids = _seed(admin, target, rec, count=6)
    odd = audit.record_in_tx('recording.viewed', recording_id=rec, actor_kind='session', actor_user_id=admin.id)
    odd.user_agent = '=HYPERLINK("http://example.test")'
    db.session.commit()
    ids.append(odd.id)
    client = client_for(admin)
    start = max_id()

    resp = client.get(f'/admin/audit-log/export?format=jsonl&recording_id={rec}')
    assert resp.status_code == 200 and resp.mimetype == 'application/x-ndjson'
    assert 'attachment; filename="audit-log-' in resp.headers['Content-Disposition']
    lines = [json.loads(line) for line in resp.get_data(as_text=True).splitlines()]
    stored = {row.id: row.to_dict() for row in AuditEvent.query.filter(AuditEvent.id.in_(ids))}
    assert [line['id'] for line in lines] == ids  # oldest first
    for line in lines:
        assert line == stored[line['id']]

    resp = client.get(f'/admin/audit-log/export?format=csv&recording_id={rec}&outcome=denied')
    assert resp.status_code == 200 and resp.mimetype == 'text/csv'
    table = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
    assert table[0] == list(audit.COLUMNS)
    assert [int(line[0]) for line in table[1:]] == [ids[1], ids[5]]
    first = dict(zip(table[0], table[1]))
    assert first['event'] == 'recording.downloaded' and first['actor_token_id'] == '77'
    assert json.loads(first['details']) == {'part': 'summary', 'format': 'md'} and first['ip'] == ''

    resp = client.get(f'/admin/audit-log/export?format=csv&recording_id={rec}&event=recording.viewed')
    cell = dict(zip(*list(csv.reader(io.StringIO(resp.get_data(as_text=True))))[:2]))['user_agent']
    assert cell.startswith("'=")

    assert client.get('/admin/audit-log/export?format=xlsx').status_code == 400
    rows = rows_after(start, 'audit.exported')
    assert [r.details for r in rows] == [
        {'filters': ['recording_id'], 'count': 7, 'format': 'jsonl'},
        {'filters': ['recording_id', 'outcome'], 'count': 2, 'format': 'csv'},
        {'filters': ['recording_id', 'event'], 'count': 1, 'format': 'csv'}]
    assert all(r.actor_user_id == admin.id and r.event_group == 'audit' for r in rows)


def test_the_notice_is_shown_until_an_admin_dismisses_it(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    client = client_for(admin)
    data = _get(client).get_json()
    assert data['notice'] is True and data['retention_days'] == 365 and data['store_ip'] == 'full'
    start = max_id()
    resp = client.post('/admin/settings', json={'key': 'audit_log_notice_dismissed', 'value': 'true',
                                                'setting_type': 'boolean'})
    assert resp.status_code == 200
    assert _get(client).get_json()['notice'] is False
    rows = rows_after(start, 'admin.setting.changed')
    assert [r.details for r in rows] == [{'key': 'audit_log_notice_dismissed', 'old': 'false', 'new': 'true'}]


def test_the_tab_is_in_the_admin_page_for_site_admins_only(created):
    admin = make_user(created, prefix='adm', is_admin=True)
    page = client_for(admin).get('/admin').get_data(as_text=True)
    assert 'data-audit-log' in page and "activeTab = 'audit'" in page
    assert 'v-if="!isTeamAdminOnly" data-audit-log' in page
    assert 'data-audit-view-activity' in page
