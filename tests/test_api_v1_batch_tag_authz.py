"""Regression tests for the batch tag-update IDOR fix.

PATCH /api/v1/recordings/batch with add_tag_ids / remove_tag_ids used to
trust the tag_ids blindly. An authenticated attacker could attach any
tag_id (including admin-curated or another user's private tags) to
their own recordings. The fix mirrors the folder-validation pattern:
personal tags must be owned by the caller, group tags require caller
membership in the tag's group.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording
from src.models.organization import Tag, Group, GroupMembership, RecordingTag

app.config["WTF_CSRF_ENABLED"] = False


def _setup_user(prefix):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _make_recording(user_id, title='r'):
    rec = Recording(
        user_id=user_id,
        title=title,
        audio_path='/tmp/x.mp3',
        status='COMPLETED',
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def test_batch_update_rejects_adding_other_users_personal_tag():
    """Attacker cannot attach a victim's personal tag to their own
    recording via the batch endpoint. The tag is silently skipped;
    other operations in the same batch still proceed."""
    with app.app_context():
        victim = _setup_user('v_tag_victim')
        attacker = _setup_user('v_tag_attacker')
        victim_tag = Tag(
            name='secret-personal-tag',
            user_id=victim.id,
            group_id=None,
        )
        db.session.add(victim_tag)
        db.session.commit()
        attacker_rec = _make_recording(attacker.id, 'attacker rec')

        client = app.test_client()
        _login(client, attacker)
        resp = client.patch(
            '/api/v1/recordings/batch',
            json={
                'recording_ids': [attacker_rec.id],
                'updates': {'add_tag_ids': [victim_tag.id]},
            },
        )
        assert resp.status_code == 200

        # The RecordingTag row must NOT have been created.
        rt = RecordingTag.query.filter_by(
            recording_id=attacker_rec.id, tag_id=victim_tag.id
        ).first()
        assert rt is None, (
            'Attacker attached victim_tag to their own recording via batch '
            'add_tag_ids. IDOR fix missing.'
        )

        db.session.delete(attacker_rec)
        db.session.delete(victim_tag)
        db.session.delete(victim)
        db.session.delete(attacker)
        db.session.commit()


def test_batch_update_rejects_adding_group_tag_when_not_member():
    """Group-scoped tags require caller membership in the tag's group.
    A non-member can't attach a group tag to their own recordings."""
    with app.app_context():
        owner = _setup_user('grp_owner')
        outsider = _setup_user('grp_outsider')
        group = Group(name=f'g_{uuid.uuid4().hex[:6]}')
        db.session.add(group)
        db.session.commit()
        # Owner is a member; outsider is not.
        db.session.add(GroupMembership(user_id=owner.id, group_id=group.id, role='admin'))
        db.session.commit()
        group_tag = Tag(name='group-tag', user_id=owner.id, group_id=group.id)
        db.session.add(group_tag)
        db.session.commit()
        outsider_rec = _make_recording(outsider.id, 'outsider rec')

        client = app.test_client()
        _login(client, outsider)
        resp = client.patch(
            '/api/v1/recordings/batch',
            json={
                'recording_ids': [outsider_rec.id],
                'updates': {'add_tag_ids': [group_tag.id]},
            },
        )
        assert resp.status_code == 200
        rt = RecordingTag.query.filter_by(
            recording_id=outsider_rec.id, tag_id=group_tag.id
        ).first()
        assert rt is None, 'Non-member attached a group tag via batch IDOR'

        db.session.delete(outsider_rec)
        db.session.delete(group_tag)
        for m in GroupMembership.query.filter_by(group_id=group.id).all():
            db.session.delete(m)
        db.session.delete(group)
        db.session.delete(owner)
        db.session.delete(outsider)
        db.session.commit()


def test_batch_update_accepts_owned_personal_tag():
    """The legitimate path still works: a user can attach their own
    personal tag via the batch endpoint."""
    with app.app_context():
        user = _setup_user('legit_tag')
        my_tag = Tag(name='mine', user_id=user.id, group_id=None)
        db.session.add(my_tag)
        db.session.commit()
        rec = _make_recording(user.id, 'r')

        client = app.test_client()
        _login(client, user)
        resp = client.patch(
            '/api/v1/recordings/batch',
            json={
                'recording_ids': [rec.id],
                'updates': {'add_tag_ids': [my_tag.id]},
            },
        )
        assert resp.status_code == 200
        rt = RecordingTag.query.filter_by(
            recording_id=rec.id, tag_id=my_tag.id
        ).first()
        assert rt is not None, 'Owner could not attach their own tag'

        db.session.delete(rt)
        db.session.delete(rec)
        db.session.delete(my_tag)
        db.session.delete(user)
        db.session.commit()


def test_batch_update_accepts_group_tag_when_member():
    """Group-scoped tags: members can attach them."""
    with app.app_context():
        owner = _setup_user('grp_owner_m')
        member = _setup_user('grp_member')
        group = Group(name=f'g_{uuid.uuid4().hex[:6]}')
        db.session.add(group)
        db.session.commit()
        db.session.add_all([
            GroupMembership(user_id=owner.id, group_id=group.id, role='admin'),
            GroupMembership(user_id=member.id, group_id=group.id, role='member'),
        ])
        db.session.commit()
        group_tag = Tag(name='gt', user_id=owner.id, group_id=group.id)
        db.session.add(group_tag)
        db.session.commit()
        member_rec = _make_recording(member.id, 'r')

        client = app.test_client()
        _login(client, member)
        resp = client.patch(
            '/api/v1/recordings/batch',
            json={
                'recording_ids': [member_rec.id],
                'updates': {'add_tag_ids': [group_tag.id]},
            },
        )
        assert resp.status_code == 200
        rt = RecordingTag.query.filter_by(
            recording_id=member_rec.id, tag_id=group_tag.id
        ).first()
        assert rt is not None

        db.session.delete(rt)
        db.session.delete(member_rec)
        db.session.delete(group_tag)
        for m in GroupMembership.query.filter_by(group_id=group.id).all():
            db.session.delete(m)
        db.session.delete(group)
        db.session.delete(owner)
        db.session.delete(member)
        db.session.commit()
