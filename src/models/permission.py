"""
Named permissions granted to users and groups (#576).

A row grants one permission either to one user or to every member of one
group. Which rows count depends on the permission's mode setting; see
src/auth/permissions.py, which is the only place that reads this table.
"""

from datetime import datetime

from src.database import db


class PermissionGrant(db.Model):
    """One permission granted to one user or to the members of one group."""

    __tablename__ = 'permission_grant'

    id = db.Column(db.Integer, primary_key=True)
    # The permission name, such as 'create_groups' (src/auth/permissions.py).
    permission = db.Column(db.String(64), nullable=False, index=True)
    # Exactly one of user_id and group_id is set.
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), nullable=True, index=True)
    group_id = db.Column(db.Integer, db.ForeignKey('group.id', ondelete='CASCADE'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # The ORM cascades delete the grants with their user or group on every
    # backend, including SQLite without foreign key enforcement, so a reused
    # id never inherits a grant.
    user = db.relationship('User', backref=db.backref(
        'permission_grants', lazy=True, cascade='all, delete-orphan'))
    group = db.relationship('Group', backref=db.backref(
        'permission_grants', lazy=True, cascade='all, delete-orphan'))

    __table_args__ = (
        db.CheckConstraint(
            '(user_id IS NULL) <> (group_id IS NULL)',
            name='ck_permission_grant_one_target',
        ),
        # NULLs are distinct in a unique index on SQLite and PostgreSQL, so a
        # user grant and a group grant never clash with each other.
        db.UniqueConstraint('permission', 'user_id', name='uq_permission_grant_user'),
        db.UniqueConstraint('permission', 'group_id', name='uq_permission_grant_group'),
    )
