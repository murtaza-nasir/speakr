"""One-shot copy of ``share_audit_log`` into the audit log (ledger id 0009).

Until this release, changes of internal shares were written to
``share_audit_log``. Those rows were deleted with the recording and with the
acting account, and nothing read them. They are copied once into
``audit_event`` as ``share.internal.*`` rows, which have no foreign key and
remain. The old table is not changed and is dropped in a later release.

What is copied: time, actor, target account, recording, address and the
permission flags. The free-text ``notes`` column is not copied.

The copy is safe to run twice and from several processes at once: every
copied row has the key ``share_audit_log:<id>`` in the unique ``dedup_key``
column, and the insert skips a key that exists.
"""

import json

from sqlalchemy import inspect, select

from src.models.audit import AuditEvent, ShareAuditLog

BATCH = 500

_EVENTS = {
    'created': 'share.internal.created',
    'modified': 'share.internal.changed',
    'revoked': 'share.internal.revoked',
    'cascade_revoked': 'share.internal.revoked',
}


def _flags(value):
    """A stored permission snapshot as a dict (JSON text on an old SQLite row)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _details(action, permissions, share_id):
    details = {'imported': True}
    if share_id is not None:
        details['share_id'] = share_id
    granted = _flags(permissions)
    if action == 'created':
        for key in ('can_edit', 'can_reshare'):
            if key in granted:
                details[key] = bool(granted[key])
    elif action == 'modified':
        old, new = _flags(granted.get('old')), _flags(granted.get('new'))
        for key in ('can_edit', 'can_reshare'):
            if key in new:
                details[key] = bool(new[key])
            if key in old:
                details[f'old_{key}'] = bool(old[key])
    elif action == 'cascade_revoked':
        details['cascade'] = True
    return details


def copy_share_audit_log(engine, logger=None):
    """Copy every row of share_audit_log. Returns the number of rows read."""
    inspector = inspect(engine)
    if not inspector.has_table('share_audit_log') or not inspector.has_table('audit_event'):
        return 0
    from src.models import Recording
    old = ShareAuditLog.__table__
    recording = Recording.__table__
    target = AuditEvent.__table__
    if engine.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(target).on_conflict_do_nothing(index_elements=['dedup_key'])

    copied = 0
    last_id = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(
                select(old.c.id, old.c.action, old.c.recording_id, old.c.actor_id, old.c.target_user_id,
                       old.c.permissions_granted, old.c.timestamp, old.c.share_id, old.c.ip_address,
                       recording.c.user_id.label('owner_id'))
                .select_from(old.outerjoin(recording, recording.c.id == old.c.recording_id))
                .where(old.c.id > last_id).order_by(old.c.id).limit(BATCH)).all()
            if not rows:
                break
            values = []
            for row in rows:
                event = _EVENTS.get(row.action)
                if event is None:
                    continue
                values.append({
                    'created_at': row.timestamp, 'event': event, 'event_group': 'share',
                    'actor_kind': 'session', 'actor_user_id': row.actor_id, 'actor_token_id': None,
                    'ip': row.ip_address, 'user_agent': None, 'recording_id': row.recording_id,
                    'recording_owner_id': row.owner_id, 'target_user_id': row.target_user_id,
                    'group_id': None, 'outcome': 'success',
                    'details': _details(row.action, row.permissions_granted, row.share_id),
                    'dedup_key': f'share_audit_log:{row.id}',
                })
            if values:
                conn.execute(statement, values)
            copied += len(rows)
            last_id = rows[-1].id
    if logger and copied:
        logger.info(f"Copied {copied} share_audit_log row(s) into the audit log")
    return copied
