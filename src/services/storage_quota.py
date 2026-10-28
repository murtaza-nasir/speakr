"""Per-user storage quotas (#413).

A user's storage is the size of their own recordings whose audio is still
kept, the same figure the admin pages show: recordings with their audio
removed do not count, archived and deletion-exempt ones do, and a recording
shared with someone counts for its owner only. `user.storage_quota_mb` is the
limit in megabytes (MiB); NULL means no limit.

Uploads are refused when they would go over the limit. A recording made in
the app is checked when it starts, never when it ends, so a recording in
progress is never lost to the quota. Code that stores counted bytes calls
reserve_room in the transaction that inserts them, so two overlapping
uploads cannot both fit into the same free space (#509).
"""

import logging

from src.database import db

logger = logging.getLogger(__name__)

MB = 1024 * 1024
KIND_STORAGE_QUOTA_REACHED = 'storage.quota_reached'


def _mb(n):
    return f'{n / MB:,.1f} MB'


class StorageQuotaExceeded(Exception):
    def __init__(self, used_bytes, quota_bytes, file_bytes=0):
        self.used_bytes, self.quota_bytes, self.file_bytes = used_bytes, quota_bytes, file_bytes
        super().__init__(f"Storage quota exceeded: {used_bytes} used + {file_bytes} new > {quota_bytes}")

    def to_dict(self):
        return {
            'error': (f'This upload would exceed your storage quota: {_mb(self.used_bytes)} of '
                      f'{_mb(self.quota_bytes)} used, and the file is {_mb(self.file_bytes)}.'
                      if self.file_bytes else
                      f'Your storage quota is full: {_mb(self.used_bytes)} of {_mb(self.quota_bytes)} used.'),
            'code': 'storage_quota_exceeded',
            'used_bytes': self.used_bytes,
            'quota_bytes': self.quota_bytes,
            'file_bytes': self.file_bytes,
            'available_bytes': max(self.quota_bytes - self.used_bytes, 0),
        }

    def response(self):
        from flask import jsonify
        # 507 Insufficient Storage, as recording sessions return for their
        # in-progress cap.
        return jsonify(self.to_dict()), 507


def storage_used(user_id):
    from src.models import Recording
    return int(db.session.query(db.func.coalesce(db.func.sum(Recording.file_size), 0))
               .filter(Recording.user_id == user_id, Recording.audio_deleted_at.is_(None)).scalar() or 0)


def quota_bytes(user):
    mb = getattr(user, 'storage_quota_mb', None)
    return int(mb) * MB if mb else None


def usage(user):
    """Used bytes, quota and percentage, for the account page and the API."""
    used = storage_used(user.id)
    quota = quota_bytes(user)
    return {
        'used_bytes': used,
        'quota_bytes': quota,
        'quota_mb': getattr(user, 'storage_quota_mb', None),
        'available_bytes': max(quota - used, 0) if quota else None,
        'percentage': round(used / quota * 100, 1) if quota else None,
    }


def check_room(user, file_bytes=0):
    """Raise StorageQuotaExceeded when `file_bytes` more would go over the quota.

    With file_bytes=0 it asks whether anything new may start at all, which is
    the check for a recording about to begin: refused at or over the quota.
    """
    quota = quota_bytes(user)
    if quota is None:
        return
    used = storage_used(user.id)
    over = used >= quota if not file_bytes else used + int(file_bytes) > quota
    if over:
        raise StorageQuotaExceeded(used, quota, int(file_bytes or 0))
    resolve_notice(user)


def reserve_room(user, file_bytes):
    """check_room, with the user's quota held until the transaction ends (#509).

    The caller then adds the recording that holds file_bytes and commits;
    another reserve_room for the same user waits for that commit and counts
    the new recording. On SQLite the lock is the database write lock, taken
    by an UPDATE that changes nothing; elsewhere it is a row lock on the
    user (SELECT ... FOR UPDATE). Raises StorageQuotaExceeded with the lock
    still held: the caller rolls back.
    """
    if quota_bytes(user) is None:
        return
    from src.models import User
    if db.engine.dialect.name == 'sqlite':
        table = User.__table__
        db.session.execute(table.update().where(table.c.id == user.id)
                           .values(storage_quota_mb=table.c.storage_quota_mb))
    else:
        db.session.query(User.id).filter(User.id == user.id).with_for_update().scalar()
    check_room(user, file_bytes)


def raise_notice(user, exc, filename=None):
    """Tell the user a file was left out, once: the notice is a condition,
    resolved when there is room again."""
    try:
        from src.services.notifications import notify
        notify(KIND_STORAGE_QUOTA_REACHED, 'notifications.storageQuotaReached',
               user_ids=[user.id], level='warning', link='/account',
               params={'used_mb': exc.used_bytes // MB, 'quota_mb': exc.quota_bytes // MB,
                       'filename': filename or ''},
               dedupe_key=f'{KIND_STORAGE_QUOTA_REACHED}:{user.id}')
    except Exception as e:
        logger.warning('Could not raise the storage quota notification for user %s: %s', user.id, e)


def resolve_notice(user):
    try:
        from src.models import Notification
        if not Notification.query.filter_by(user_id=user.id, dedupe_key=f'{KIND_STORAGE_QUOTA_REACHED}:{user.id}',
                                            resolved_at=None).first():
            return
        from src.services.notifications import resolve
        resolve(f'{KIND_STORAGE_QUOTA_REACHED}:{user.id}', user_ids=[user.id])
    except Exception as e:
        logger.debug('Could not resolve the storage quota notification for user %s: %s', user.id, e)


def default_quota_mb():
    """The admin's default for new accounts (System Settings), or None."""
    try:
        from src.models import SystemSetting
        value = int(SystemSetting.get_setting('default_storage_quota_mb', 0) or 0)
        return value if value > 0 else None
    except Exception:
        return None
