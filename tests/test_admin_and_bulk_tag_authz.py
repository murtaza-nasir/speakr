"""Authorization fixes:

1. GET /admin/users must not leak the whole user directory to a group admin
   (previously returned User.query.all() to anyone who administered any group).
2. POST /api/recordings/bulk-tags must enforce group membership for group tags
   (previously group tags skipped the membership check its sibling endpoints do).
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, bcrypt
from src.models import User, Recording
from src.models.organization import Group, GroupMembership, Tag

app.config['WTF_CSRF_ENABLED'] = False


def _user(prefix):
    s = uuid.uuid4().hex[:8]
    u = User(username=f'{prefix}_{s}', email=f'{prefix}_{s}@local.test',
             password=bcrypt.generate_password_hash('Passw0rd!').decode())
    db.session.add(u)
    db.session.commit()
    return u


def _login(client, user):
    from flask import g
    with client.session_transaction() as sess:
        sess.clear()
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    try:
        g.pop('_login_user', None)
    except RuntimeError:
        pass


def test_group_admin_only_sees_own_group_members():
    with app.app_context():
        group_admin = _user('gadmin')
        member = _user('gmember')
        outsider = _user('outsider')  # in NO shared group with group_admin

        g = Group(name=f'grp_{uuid.uuid4().hex[:6]}')
        db.session.add(g)
        db.session.commit()
        db.session.add_all([
            GroupMembership(group_id=g.id, user_id=group_admin.id, role='admin'),
            GroupMembership(group_id=g.id, user_id=member.id, role='member'),
        ])
        db.session.commit()

        client = app.test_client()
        _login(client, group_admin)
        resp = client.get('/admin/users')
        assert resp.status_code == 200
        returned_ids = {u['id'] for u in resp.get_json()}

        assert group_admin.id in returned_ids   # self
        assert member.id in returned_ids         # their group member
        assert outsider.id not in returned_ids   # NOT the whole directory

        for u in (group_admin, member, outsider):
            db.session.delete(u)
        db.session.delete(g)
        db.session.commit()


def test_site_admin_still_sees_all_users():
    with app.app_context():
        site_admin = _user('siteadmin')
        site_admin.is_admin = True
        other = _user('other')
        db.session.commit()

        client = app.test_client()
        _login(client, site_admin)
        resp = client.get('/admin/users')
        assert resp.status_code == 200
        ids = {u['id'] for u in resp.get_json()}
        assert site_admin.id in ids and other.id in ids

        db.session.delete(site_admin)
        db.session.delete(other)
        db.session.commit()


def test_bulk_tags_rejects_group_tag_for_non_member():
    with app.app_context():
        owner = _user('owner')          # group owner/admin, creates a group tag
        attacker = _user('attacker')    # NOT a member of the group

        g = Group(name=f'grp_{uuid.uuid4().hex[:6]}')
        db.session.add(g)
        db.session.commit()
        db.session.add(GroupMembership(group_id=g.id, user_id=owner.id, role='admin'))
        group_tag = Tag(name='confidential', user_id=owner.id, group_id=g.id)
        db.session.add(group_tag)
        # Attacker has a recording of their own to tag.
        rec = Recording(user_id=attacker.id, title='mine', status='COMPLETED')
        db.session.add(rec)
        db.session.commit()

        client = app.test_client()
        _login(client, attacker)
        resp = client.post('/api/recordings/bulk-tags', json={
            'recording_ids': [rec.id], 'tag_id': group_tag.id, 'action': 'add',
        })
        assert resp.status_code == 403, resp.data

        db.session.delete(rec)
        db.session.delete(group_tag)
        db.session.delete(GroupMembership.query.filter_by(group_id=g.id).first())
        db.session.delete(g)
        db.session.delete(owner)
        db.session.delete(attacker)
        db.session.commit()


def test_bulk_tags_allows_group_member():
    with app.app_context():
        member = _user('member')
        g = Group(name=f'grp_{uuid.uuid4().hex[:6]}')
        db.session.add(g)
        db.session.commit()
        db.session.add(GroupMembership(group_id=g.id, user_id=member.id, role='member'))
        group_tag = Tag(name='team', user_id=member.id, group_id=g.id)
        rec = Recording(user_id=member.id, title='mine', status='COMPLETED')
        db.session.add_all([group_tag, rec])
        db.session.commit()

        client = app.test_client()
        _login(client, member)
        resp = client.post('/api/recordings/bulk-tags', json={
            'recording_ids': [rec.id], 'tag_id': group_tag.id, 'action': 'add',
        })
        assert resp.status_code == 200, resp.data

        db.session.delete(rec)
        db.session.delete(group_tag)
        db.session.delete(GroupMembership.query.filter_by(group_id=g.id).first())
        db.session.delete(g)
        db.session.delete(member)
        db.session.commit()
