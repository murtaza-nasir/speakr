"""The access audit log: who signed in, who opened, changed, shared or deleted
which recording, and what an administrator changed.

One table, ``audit_event`` (``src/models/audit.py``), and two ways into it.

``record_in_tx(event, ...)``
    For an action that already writes to the database (a change, a share, an
    administrative action). The row is added to the session of the request and
    is committed or rolled back together with the action, so the row exists
    exactly when the action took effect. The caller commits.

``record(event, ...)``
    For a read and for a sign-in event. Actor, address and user agent are
    taken in the request thread and the event is put on a bounded queue. A
    writer thread per process inserts the queued events once per second, as
    one transaction on a connection of its own. The request never waits for
    the database and never fails because of the audit log.

What is never recorded
    Titles, file names, transcript text, search queries, token names, the
    ``public_id`` of a share link, e-mail addresses, passwords and exception
    text. ``EVENTS`` lists, per event, the only keys the ``details`` column may
    hold. A key that is not listed raises in tests and is dropped with a
    warning otherwise.

Repeated reads
    The front end requests the same recording many times, and a player sends
    a range request per seek. An event with a ``dedup`` window is written once
    per fixed time bucket: a map in memory drops the repeats of one process,
    and the unique ``dedup_key`` column makes it exact across processes.

When the database cannot be written
    A batch is tried again after 1, 5, 15 and 60 seconds while new events keep
    queuing. After that every event of the batch is logged as one line
    ``AUDIT-FALLBACK {json}`` at ERROR on the logger ``speakr.audit``, and the
    next batch that is written adds an ``audit.write_failed`` row with the
    count. An event that finds the queue full is logged the same way.

Incognito requests
    Inside an incognito request only ``incognito.used`` is recorded (actor,
    time and outcome). Every other event is dropped there, and the fallback
    line is written outside the incognito log filter because it holds no
    content.
"""

import atexit
import contextvars
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.pool import NullPool

from src.database import db
from src.models.audit import AuditEvent

logger = logging.getLogger('speakr.audit')

FALLBACK_PREFIX = 'AUDIT-FALLBACK'

# --- Events -----------------------------------------------------------------
# event -> (group, keys allowed in details)
_NONE = frozenset()
EVENTS = {
    # Sign-in and session
    'auth.login.succeeded': ('auth', frozenset({'method', 'remember'})),
    'auth.login.failed': ('auth', frozenset({'reason', 'method'})),
    'auth.login.locked': ('auth', frozenset({'minutes'})),
    'auth.logout': ('auth', _NONE),
    'auth.registered': ('auth', _NONE),
    'auth.password.changed': ('auth', _NONE),
    'auth.password.reset_requested': ('auth', _NONE),
    'auth.password.reset': ('auth', _NONE),
    'auth.sso.linked': ('auth', _NONE),
    'auth.sso.unlinked': ('auth', _NONE),
    'token.created': ('auth', frozenset({'token_id', 'scopes', 'expires', 'sso_client_id'})),
    'token.changed': ('auth', frozenset({'token_id', 'scopes'})),
    'token.revoked': ('auth', frozenset({'token_id'})),
    'token.first_used': ('auth', frozenset({'token_id'})),
    # Access to a recording
    'recording.viewed': ('access', _NONE),
    'recording.content.read': ('access', frozenset({'part'})),
    'recording.media.played': ('access', _NONE),
    'recording.media.downloaded': ('access', _NONE),
    'recording.downloaded': ('access', frozenset({'part', 'format'})),
    'recording.exported': ('access', frozenset({'count'})),
    # Changes
    'recording.edited': ('change', frozenset({'fields'})),
    'recording.deleted': ('change', frozenset({'reason'})),
    'recording.audio.deleted': ('change', frozenset({'reason'})),
    'recording.reprocessed': ('change', frozenset({'kind'})),
    'recording.job.canceled': ('change', frozenset({'kind'})),
    'recording.trimmed': ('change', frozenset({'start', 'end', 'removed_segments'})),
    # Sharing
    'share.internal.created': ('share', frozenset({'share_id', 'can_edit', 'can_reshare', 'imported'})),
    'share.internal.changed': ('share', frozenset({
        'share_id', 'can_edit', 'can_reshare', 'old_can_edit', 'old_can_reshare', 'imported'})),
    'share.internal.revoked': ('share', frozenset({'share_id', 'cascade', 'imported'})),
    'share.public.created': ('share', frozenset({'share_id', 'share_summary', 'share_notes', 'expires'})),
    'share.public.changed': ('share', frozenset({'share_id', 'share_summary', 'share_notes', 'expires'})),
    'share.public.revoked': ('share', frozenset({'share_id'})),
    'share.public.opened': ('share', frozenset({'share_id'})),
    'share.public.media.played': ('share', frozenset({'share_id'})),
    # Administration
    'admin.user.created': ('admin', frozenset({'username', 'is_admin'})),
    'admin.user.changed': ('admin', frozenset({'fields'})),
    'admin.user.password_set': ('admin', frozenset({'self'})),
    'admin.user.unlocked': ('admin', _NONE),
    'admin.user.role_changed': ('admin', frozenset({'old', 'new'})),
    'admin.user.deleted': ('admin', frozenset({'username', 'recordings_deleted'})),
    'admin.setting.changed': ('admin', frozenset({'key', 'old', 'new', 'users_updated'})),
    'admin.llm_model.created': ('admin', frozenset({'model', 'fields'})),
    'admin.llm_model.changed': ('admin', frozenset({'model', 'fields'})),
    'admin.llm_model.deleted': ('admin', frozenset({'model'})),
    'admin.llm_role.assigned': ('admin', frozenset({'role', 'model', 'old'})),
    'admin.llm_role.reverted': ('admin', frozenset({'role', 'old'})),
    'admin.retention.run': ('admin', frozenset({'checked', 'deleted_audio_only', 'deleted_full', 'errors'})),
    'admin.inquire.processed': ('admin', frozenset({'processed', 'failed', 'total', 'force'})),
    'group.created': ('admin', _NONE),
    'group.changed': ('admin', frozenset({'fields'})),
    'group.deleted': ('admin', _NONE),
    'group.member.added': ('admin', frozenset({'role'})),
    'group.member.role_changed': ('admin', frozenset({'role', 'old_role'})),
    'group.member.removed': ('admin', _NONE),
    # The log itself
    'audit.viewed': ('audit', frozenset({'filters'})),
    'audit.exported': ('audit', frozenset({'filters', 'count', 'format'})),
    'audit.pruned': ('audit', frozenset({'count', 'cutoff'})),
    'audit.write_failed': ('audit', frozenset({'count'})),
    # Incognito
    'incognito.used': ('incognito', _NONE),
}

EVENT_GROUPS = ('auth', 'access', 'change', 'share', 'admin', 'incognito', 'audit')
ACTOR_KINDS = ('session', 'api_token', 'sso_token', 'public_link', 'system', 'anonymous')
OUTCOMES = ('success', 'denied', 'failed')

# Events whose row never holds the client address, the user agent or a token id.
NO_CLIENT_EVENTS = frozenset({'incognito.used'})

# Dropped while audit_log_record_views is off.
VIEW_EVENTS = frozenset({'recording.viewed', 'recording.media.played'})

# De-duplication windows, in seconds.
DEDUP_VIEW = 30 * 60
DEDUP_DOWNLOAD = 5 * 60
DEDUP_DENIED = 60
DEDUP_LOGIN_FAILED = 60
DEDUP_RESET_REQUESTED = 5 * 60
# One bucket for all time: the event is written once.
DEDUP_ONCE = 10 ** 11

# System settings whose old and new value are stored with a change: booleans,
# integers and enumerations. For every other key only the name is stored.
SETTING_VALUE_KEYS = frozenset({
    'audit_log_enabled', 'audit_log_retention_days', 'audit_log_store_ip', 'audit_log_record_views',
    'audit_log_notice_dismissed',
    'allow_registration', 'users_can_delete', 'enable_public_sharing', 'enable_internal_sharing',
    'show_usernames_in_ui', 'user_search_scope', 'enable_incognito_mode', 'incognito_mode_default',
    'incognito_temp_max_age_hours', 'enable_auto_deletion', 'global_retention_days', 'deletion_mode',
    'recording_tombstone_days', 'webhook_delivery_retention_days', 'webhook_omit_titles',
    'auto_export_delete_with_recording', 'video_retention', 'video_passthrough_asr', 'asr_async_jobs',
    'max_concurrent_uploads', 'enable_email_verification', 'require_email_verification',
    'session_idle_timeout_minutes', 'api_query_tokens_enabled', 'sso_api_tokens_enabled',
    'login_max_failed_attempts', 'login_lockout_minutes', 'max_file_size_mb',
    'default_storage_quota_mb', 'transcript_length_limit',
})

MAX_DETAILS_CHARS = 512
MAX_USER_AGENT_CHARS = 160
MAX_DEDUP_KEY_CHARS = 80

# --- Writer -----------------------------------------------------------------
QUEUE_SIZE = 10000
FLUSH_INTERVAL_SECONDS = 1.0
BATCH_SIZE = 200
# Waits between the attempts to write one batch; after the last one the batch
# goes to the fallback log line.
RETRY_DELAYS = (1, 5, 15, 60)
# The busy timeout of the writer's own connection.
LOCK_WAIT_SECONDS = 5
PRUNE_BATCH = 5000

_queue = queue.Queue(QUEUE_SIZE)
_lock = threading.Lock()
_writer = None
_writer_pid = None
_wake = threading.Event()
_idle = threading.Event()
_idle.set()
_engines = {}
# Events that went to the fallback line since the last written batch, per
# main engine, so that the gap is recorded in the table itself.
_lost = {}
_lost_lock = threading.Lock()
# dedup_key -> time.time() after which the key is forgotten
_seen = {}
_seen_lock = threading.Lock()
_SEEN_LIMIT = 20000
_atexit_registered = False


# --- Settings ---------------------------------------------------------------

def _setting(key):
    from src.models import SystemSetting
    from src.utils.system_settings import ENV_BACKED_SETTINGS, env_fallback
    env_var, default = ENV_BACKED_SETTINGS[key][:2]
    fallback = env_fallback(env_var, default)
    try:
        value = SystemSetting.get_setting(key, fallback)
    except Exception:
        return fallback
    return fallback if value is None else value


def is_enabled():
    """audit_log_enabled (AUDIT_LOG_ENABLED); on unless turned off."""
    return bool(_setting('audit_log_enabled'))


def retention_days():
    """audit_log_retention_days; 0 (or less) keeps every row."""
    try:
        return max(0, int(_setting('audit_log_retention_days')))
    except (TypeError, ValueError):
        return 365


def ip_mode():
    """audit_log_store_ip: full, truncated or off. An unknown value counts as full."""
    value = str(_setting('audit_log_store_ip') or '').strip().lower()
    return value if value in ('full', 'truncated', 'off') else 'full'


def views_recorded():
    """audit_log_record_views; off drops recording.viewed and recording.media.played."""
    return bool(_setting('audit_log_record_views'))


def stored_ip(remote_addr, mode=None):
    """The address as the log stores it under the given mode."""
    mode = mode or ip_mode()
    if mode == 'off' or not remote_addr:
        return None
    raw = str(remote_addr).split('%', 1)[0].strip()
    if mode == 'full':
        return raw[:45]
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f'{address}/{prefix}', strict=False).network_address)


# --- Building a row ---------------------------------------------------------

def _strict():
    """Whether a forbidden details key raises (tests) or is dropped (production)."""
    try:
        from flask import current_app
        return bool(current_app.config.get('TESTING'))
    except Exception:
        return False


def _clean_details(event, details):
    if not details:
        return None
    allowed = EVENTS[event][1]
    clean = {}
    for key, value in details.items():
        if key not in allowed:
            if _strict():
                raise ValueError(f'Audit event {event} does not allow the details key {key!r}')
            logger.warning('Audit event %s: details key %s is not allowed and was dropped', event, key)
            continue
        if value is None:
            continue
        clean[key] = value
    if not clean:
        return None
    if len(json.dumps(clean, default=str)) > MAX_DETAILS_CHARS:
        if _strict():
            raise ValueError(f'Audit event {event}: details are longer than {MAX_DETAILS_CHARS} characters')
        logger.warning('Audit event %s: details were too long and were dropped', event)
        return {'truncated': True}
    return clean


def _request_actor():
    """(actor_kind, actor_user_id, actor_token_id) of the current request."""
    from flask import has_request_context
    if not has_request_context():
        return 'system', None, None
    from flask_login import current_user
    try:
        authenticated = bool(current_user.is_authenticated)
    except Exception:
        authenticated = False
    if not authenticated:
        return 'anonymous', None, None
    user_id = current_user.id
    try:
        from src.utils.token_auth import current_api_token, is_sso_token
        api_token = current_api_token()
    except Exception:
        api_token = None
    if api_token is None:
        return 'session', user_id, None
    if is_sso_token(api_token):
        return 'sso_token', user_id, None
    return 'api_token', user_id, getattr(api_token, 'id', None)


def _visitor_hash(with_agent=True):
    """A short keyed hash of the address (and the user agent), for the
    de-duplication of events without an account. It is never stored."""
    from flask import current_app, request
    secret = str(current_app.config.get('SECRET_KEY') or '').encode()
    agent = request.headers.get('User-Agent', '') if with_agent else ''
    material = f"{request.remote_addr}|{agent}".encode()
    return hmac.new(secret, material, hashlib.sha256).hexdigest()[:12]


def stamp(moment):
    """A creation time as a short code for a de-duplication key.

    The id of a deleted recording, token or share is given out again on
    SQLite. With the creation time in the key, an event of the new object is
    never taken for a repeat of an event of the deleted one."""
    if moment is None:
        return ''
    value = int(moment.timestamp() * 1000)
    digits = '0123456789abcdefghijklmnopqrstuvwxyz'
    code = ''
    while value:
        value, rest = divmod(value, 36)
        code = digits[rest] + code
    return code or '0'


def _epoch():
    """The time the de-duplication buckets are counted from (tests replace it)."""
    return time.time()


def _dedup_key(event, parts, window, now_epoch):
    bucket = int(now_epoch // window)
    key = ':'.join([event] + [str(p) for p in parts if p is not None] + [str(bucket)])
    if len(key) > MAX_DEDUP_KEY_CHARS:
        key = event[:15] + ':' + hashlib.sha256(key.encode()).hexdigest()[:48]
    return key


def _build(event, *, recording=None, recording_id=None, recording_owner_id=None,
           target_user_id=None, group_id=None, outcome='success', details=None,
           dedup=None, dedup_extra=None, actor_kind=None, actor_user_id=None,
           actor_token_id=None, force=False, created_at=None):
    """The column values of one event, or None when it is not to be recorded."""
    if event not in EVENTS:
        raise ValueError(f'Unknown audit event {event!r}')
    if outcome not in OUTCOMES:
        raise ValueError(f'Unknown audit outcome {outcome!r}')
    group = EVENTS[event][0]

    from src.utils.incognito_logging import incognito_active
    if incognito_active() and event != 'incognito.used':
        return None
    # The switch cannot hide itself: audit.* events and changes of the
    # audit_log_* settings are written while the log is off.
    if not force and group != 'audit' and not is_enabled():
        return None
    if event in VIEW_EVENTS and not views_recorded():
        return None

    from flask import has_request_context, request
    in_request = has_request_context()
    if actor_kind is None:
        actor_kind, req_user_id, req_token_id = _request_actor()
        if actor_user_id is None:
            actor_user_id = req_user_id
        if actor_token_id is None:
            actor_token_id = req_token_id
    elif actor_kind not in ACTOR_KINDS:
        raise ValueError(f'Unknown audit actor kind {actor_kind!r}')

    recording_stamp = ''
    if recording is not None:
        recording_id = recording.id
        if recording_owner_id is None:
            recording_owner_id = recording.user_id
        recording_stamp = stamp(getattr(recording, 'created_at', None))

    ip = user_agent = None
    # An incognito upload is recorded as account, time and outcome only:
    # no address and no user agent, whatever audit_log_store_ip says.
    if in_request and actor_kind != 'system' and event not in NO_CLIENT_EVENTS:
        ip = stored_ip(request.remote_addr)
        agent = request.headers.get('User-Agent')
        user_agent = agent[:MAX_USER_AGENT_CHARS] if agent else None

    if event in NO_CLIENT_EVENTS:
        actor_token_id = None

    now = created_at or datetime.utcnow()
    dedup_key = None
    if dedup:
        if actor_kind == 'public_link' and in_request:
            who = 'p' + _visitor_hash()
        elif actor_token_id is not None:
            who = f't{actor_token_id}'
        elif actor_user_id is not None:
            who = f'u{actor_user_id}'
        elif in_request:
            who = 'a' + _visitor_hash(with_agent=False)
        else:
            who = 'system'
        parts = [who, f'r{recording_id}{recording_stamp}' if recording_id is not None else None,
                 f'x{target_user_id}' if target_user_id is not None else None,
                 outcome if outcome != 'success' else None, dedup_extra]
        dedup_key = _dedup_key(event, parts, dedup, _epoch())

    return {
        'created_at': now,
        'event': event,
        'event_group': group,
        'actor_kind': actor_kind,
        'actor_user_id': actor_user_id,
        'actor_token_id': actor_token_id,
        'ip': ip,
        'user_agent': user_agent,
        'recording_id': recording_id,
        'recording_owner_id': recording_owner_id,
        'target_user_id': target_user_id,
        'group_id': group_id,
        'outcome': outcome,
        'details': _clean_details(event, details),
        'dedup_key': dedup_key,
    }


def _seen_recently(dedup_key, window):
    """True when this process already recorded the key in its time bucket."""
    now = _epoch()
    with _seen_lock:
        expires = _seen.get(dedup_key)
        if expires is not None and expires > now:
            return True
        if len(_seen) >= _SEEN_LIMIT:
            for key in [k for k, v in _seen.items() if v <= now]:
                del _seen[key]
            if len(_seen) >= _SEEN_LIMIT:
                _seen.clear()
        # The key holds the bucket, so it cannot match after the bucket ends.
        _seen[dedup_key] = now + window
        return False


# --- The two entry points ---------------------------------------------------

def record_in_tx(event, **fields):
    """Add the event to the session of the request. The caller commits.

    Returns the AuditEvent, or None when the event is not recorded (the log is
    off). An error in the arguments raises in tests; otherwise it is logged
    and the action goes on without the row.
    """
    try:
        values = _build(event, **fields)
        if values is None:
            return None
        row = AuditEvent(**values)
        db.session.add(row)
        return row
    except Exception as exc:
        if _strict():
            raise
        _log(logging.ERROR, 'Audit event %s was not added (%s)', event, type(exc).__name__)
        return None


def record(event, **fields):
    """Queue the event for the writer thread. Never blocks and never raises
    outside tests. Returns True when the event was queued."""
    try:
        values = _build(event, **fields)
        if values is None:
            return False
        if values['dedup_key'] and _seen_recently(values['dedup_key'], fields.get('dedup') or 0):
            return False
        main = db.engine
    except Exception as exc:
        if _strict():
            raise
        _log(logging.ERROR, 'Audit event %s was not queued (%s)', event, type(exc).__name__)
        return False
    try:
        _queue.put_nowait((main, values))
    except queue.Full:
        _fallback(main, [values])
        return False
    _ensure_writer()
    return True


# --- Logging outside the incognito filter -----------------------------------

def _log(level, message, *args):
    """Log on speakr.audit in an empty context.

    Inside an incognito request every record of another logger is replaced by
    a withheld-line. A line of this module holds ids, codes and counts only,
    so it is written as it is, also from an incognito request.
    """
    contextvars.Context().run(logger.log, level, message, *args)


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _fallback(main, rows):
    """One AUDIT-FALLBACK line per event that could not be stored."""
    for values in rows:
        line = {k: v for k, v in values.items() if v is not None and k != 'dedup_key'}
        _log(logging.ERROR, '%s %s', FALLBACK_PREFIX,
             json.dumps(line, default=_json_default, sort_keys=True))
    with _lost_lock:
        _lost[main] = _lost.get(main, 0) + len(rows)


# --- Writer thread ----------------------------------------------------------

def _write_engine(main):
    """A separate engine without pooled connections for the writer.

    The connection is opened for a batch and closed after it, so nothing is
    held between batches and a forked worker inherits nothing.
    """
    engine = _engines.get(main)
    if engine is None:
        connect_args = {}
        if main.dialect.name == 'sqlite':
            connect_args['timeout'] = LOCK_WAIT_SECONDS
        engine = create_engine(main.url, poolclass=NullPool, connect_args=connect_args)
        _engines[main] = engine
    return engine


def _insert_statement(engine):
    table = AuditEvent.__table__
    if engine.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table).on_conflict_do_nothing(index_elements=['dedup_key'])


def _write_batch(main, rows):
    engine = _write_engine(main)
    with _lost_lock:
        lost = _lost.get(main, 0)
    extra = []
    if lost:
        extra.append({
            'created_at': datetime.utcnow(), 'event': 'audit.write_failed', 'event_group': 'audit',
            'actor_kind': 'system', 'actor_user_id': None, 'actor_token_id': None, 'ip': None,
            'user_agent': None, 'recording_id': None, 'recording_owner_id': None,
            'target_user_id': None, 'group_id': None, 'outcome': 'failed',
            'details': {'count': lost}, 'dedup_key': None,
        })
    with engine.begin() as conn:
        if engine.dialect.name == 'postgresql':
            conn.execute(text(f"SET LOCAL lock_timeout = '{int(LOCK_WAIT_SECONDS * 1000)}ms'"))
        conn.execute(_insert_statement(engine), rows + extra)
    if lost:
        with _lost_lock:
            _lost[main] = max(0, _lost.get(main, 0) - lost)


def _write_with_retries(main, rows, delays=None):
    delays = RETRY_DELAYS if delays is None else delays
    attempt = 0
    while True:
        try:
            _write_batch(main, rows)
            return True
        except Exception as exc:
            if attempt >= len(delays):
                _log(logging.ERROR, 'Audit batch of %d event(s) was not stored (%s)',
                     len(rows), type(exc).__name__)
                _fallback(main, rows)
                return False
            time.sleep(delays[attempt])
            attempt += 1


def _drain(limit=BATCH_SIZE):
    """Up to `limit` queued events, grouped by the database they belong to."""
    batches = {}
    taken = 0
    while taken < limit:
        try:
            main, values = _queue.get_nowait()
        except queue.Empty:
            break
        batches.setdefault(main, []).append(values)
        taken += 1
    return batches


def _writer_loop():
    while True:
        _wake.wait(FLUSH_INTERVAL_SECONDS)
        _wake.clear()
        while True:
            # Cleared before the queue is read, so that flush() never sees an
            # empty queue while its events are still being written.
            _idle.clear()
            batches = _drain()
            if not batches:
                _idle.set()
                break
            try:
                for main, rows in batches.items():
                    _write_with_retries(main, rows)
            except Exception as exc:  # never let the thread die
                _log(logging.ERROR, 'Audit writer error (%s)', type(exc).__name__)


def _ensure_writer():
    """Start the writer thread of this process on the first event.

    Lazily, so that a process that records nothing starts no thread, and after
    a fork the child starts its own.
    """
    global _writer, _writer_pid, _atexit_registered
    pid = os.getpid()
    if _writer is not None and _writer_pid == pid and _writer.is_alive():
        if _queue.qsize() >= BATCH_SIZE:
            _wake.set()
        return
    with _lock:
        if _writer is not None and _writer_pid == pid and _writer.is_alive():
            return
        thread = threading.Thread(target=_writer_loop, daemon=True, name='AuditLogWriter')
        # An empty context: the thread must not inherit the incognito mark of
        # the request that happens to start it.
        contextvars.Context().run(thread.start)
        _writer, _writer_pid = thread, pid
        if not _atexit_registered:
            atexit.register(_flush_at_exit)
            _atexit_registered = True


def _flush_at_exit():
    """Write what is still queued when the process ends. One attempt only."""
    try:
        while True:
            batches = _drain()
            if not batches:
                return
            for main, rows in batches.items():
                _write_with_retries(main, rows, delays=())
    except Exception:
        pass


def flush(timeout=10.0):
    """Wait until every queued event was written (or went to the fallback).

    For tests, the read routes of the admin page and shutdown code. Returns
    True when the queue is empty and the writer is idle, False when the
    timeout ended first. Only the queue of this process is covered.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _queue.empty() and _idle.is_set():
            return True
        _wake.set()
        time.sleep(0.01)
    return _queue.empty() and _idle.is_set()


def reset_memory():
    """Forget the in-memory de-duplication map. For tests."""
    with _seen_lock:
        _seen.clear()


# --- Pruning ----------------------------------------------------------------

def prune_audit_events(now=None):
    """Delete the rows older than audit_log_retention_days, in batches, then
    record the run. Returns the number of rows removed.

    This is the only statement in the application that deletes from
    audit_event (tests/test_audit_log_core.py asserts it).
    """
    days = retention_days()
    if days <= 0:
        return 0
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=days)
    table = AuditEvent.__table__
    removed = 0
    while True:
        ids = select(table.c.id).where(table.c.created_at < cutoff).order_by(table.c.id).limit(PRUNE_BATCH)
        result = db.session.execute(delete(table).where(table.c.id.in_(ids)))
        db.session.commit()
        count = result.rowcount or 0
        removed += count
        if count < PRUNE_BATCH:
            break
    if removed:
        record_in_tx('audit.pruned', actor_kind='system',
                     details={'count': removed, 'cutoff': cutoff.strftime('%Y-%m-%dT%H:%M:%S')})
        db.session.commit()
    return removed


# --- Reading (admin routes) -------------------------------------------------

def filtered_query(filters):
    """A select of audit_event for the filters of the admin page.

    filters: date_from, date_to (datetime), user_id (actor or target),
    event_group, event, recording_id, outcome, actor_kind.
    """
    table = AuditEvent.__table__
    query = select(table)
    if filters.get('date_from') is not None:
        query = query.where(table.c.created_at >= filters['date_from'])
    if filters.get('date_to') is not None:
        query = query.where(table.c.created_at < filters['date_to'])
    if filters.get('user_id') is not None:
        user_id = filters['user_id']
        query = query.where((table.c.actor_user_id == user_id) | (table.c.target_user_id == user_id))
    if filters.get('event_group'):
        query = query.where(table.c.event_group == filters['event_group'])
    if filters.get('event'):
        query = query.where(table.c.event == filters['event'])
    if filters.get('recording_id') is not None:
        query = query.where(table.c.recording_id == filters['recording_id'])
    if filters.get('outcome'):
        query = query.where(table.c.outcome == filters['outcome'])
    if filters.get('actor_kind'):
        query = query.where(table.c.actor_kind == filters['actor_kind'])
    return query


COLUMNS = ('id', 'created_at', 'event', 'event_group', 'actor_kind', 'actor_user_id',
           'actor_token_id', 'ip', 'user_agent', 'recording_id', 'recording_owner_id',
           'target_user_id', 'group_id', 'outcome', 'details')


def row_dict(row):
    """One row as the routes return it: the columns of the table, unchanged."""
    mapping = row._mapping
    data = {name: mapping[name] for name in COLUMNS}
    if data['created_at'] is not None:
        data['created_at'] = data['created_at'].isoformat()
    return data


def page(filters, before_id=None, after_id=None, limit=50):
    """One page, newest first, by keyset.

    Returns (rows, has_older, has_newer). before_id gives the rows older than
    that id; after_id gives the rows newer than it (the page before).
    """
    table = AuditEvent.__table__
    limit = max(1, min(int(limit), 500))
    query = filtered_query(filters)
    if after_id is not None:
        rows = db.session.execute(
            query.where(table.c.id > after_id).order_by(table.c.id.asc()).limit(limit + 1)).all()
        has_newer = len(rows) > limit
        rows = list(reversed(rows[:limit]))
        has_older = True
    else:
        if before_id is not None:
            query = query.where(table.c.id < before_id)
        rows = db.session.execute(query.order_by(table.c.id.desc()).limit(limit + 1)).all()
        has_older = len(rows) > limit
        rows = rows[:limit]
        has_newer = before_id is not None
    return [row_dict(r) for r in rows], has_older, has_newer


def highest_id(filters):
    """The highest id of the filtered result, or None when it has no row.

    Built from filtered_query, so the bound of an export and its rows are
    selected by one predicate.
    """
    table = AuditEvent.__table__
    return db.session.execute(
        filtered_query(filters).with_only_columns(func.max(table.c.id))).scalar()


def stream(filters, chunk=1000, max_id=None):
    """Every row of the filtered result, oldest first, read in chunks by id.

    With max_id, rows with a higher id are left out: the result is then the
    same whatever is written while it is read.
    """
    table = AuditEvent.__table__
    query = filtered_query(filters)
    if max_id is not None:
        query = query.where(table.c.id <= max_id)
    last_id = 0
    while True:
        rows = db.session.execute(
            query.where(table.c.id > last_id).order_by(table.c.id.asc()).limit(chunk)).all()
        if not rows:
            return
        for row in rows:
            yield row_dict(row)
        last_id = rows[-1]._mapping['id']


# --- Route helpers ----------------------------------------------------------

# The only values of recording.edited "fields".
EDIT_FIELDS = frozenset({'title', 'participants', 'notes', 'summary', 'transcript', 'translation',
                         'speakers', 'meeting_date', 'deletion_exempt'})


def code(value, allowed, default):
    """A request value as a details value: one of the allowed codes, else the
    default. A value typed by the user is never stored as it is."""
    value = str(value or '').strip().lower()
    return value if value in allowed else default


def edited(recording, fields):
    """recording.edited in the transaction of the edit, when a field of the
    fixed set changed. The caller commits."""
    fields = [f for f in fields if f]
    unknown = [f for f in fields if f not in EDIT_FIELDS]
    if unknown and _strict():
        raise ValueError(f'recording.edited: unknown field(s) {unknown}')
    fields = sorted(set(fields) & EDIT_FIELDS)
    if not fields:
        return None
    return record_in_tx('recording.edited', recording=recording, details={'fields': fields})


def emits(*events):
    """Mark a view whose body (or a service it calls) records these events.

    The mark is read by tests/test_audit_log_coverage.py, which requires
    every route that names a recording to be audited or listed as not
    audited. It records nothing itself."""
    for name in events:
        if name not in EVENTS:
            raise ValueError(f'Unknown audit event {name!r}')

    def decorator(view):
        view._audit_events = tuple(getattr(view, '_audit_events', ())) + tuple(events)
        return view
    return decorator


_has_recording_access = None


def init_access_check(has_recording_access):
    """The access rule of the application (src/app.py), given at start."""
    global _has_recording_access
    _has_recording_access = has_recording_access


def media_event():
    """Play or download, by the download parameter of the audio routes."""
    from flask import request
    if request.args.get('download', 'false').lower() == 'true':
        return 'recording.media.downloaded', DEDUP_DOWNLOAD
    return 'recording.media.played', DEDUP_VIEW


MEDIA_EVENTS = ('recording.media.played', 'recording.media.downloaded')


def access(event, *, dedup=None, details=None, recording_of=None, also=()):
    """Record a read of a recording after the view answered.

    event: the event name, or a callable returning (name, dedup) for a view
    that serves two kinds of access (play and download). details: a dict or
    a callable returning one. recording_of: a callable (**view_args) that
    returns the recording for a route without recording_id.

    A successful answer (2xx, or a redirect to the storage) records the event.
    A 403 or 404 for a recording that exists and that the caller cannot
    access records it with outcome denied, once per minute. Placed directly
    under login_required, above a decorator that can turn the answer into 304.
    """
    names = [event] if isinstance(event, str) else list(also)
    for name in names:
        if name not in EVENTS:
            raise ValueError(f'Unknown audit event {name!r}')

    def decorator(view):
        import functools

        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            from flask import current_app
            response = current_app.make_response(view(*args, **kwargs))
            try:
                _record_access(event, dedup, details, recording_of, kwargs, response.status_code)
            except Exception as exc:
                if _strict():
                    raise
                _log(logging.ERROR, 'Audit access event was not recorded (%s)', type(exc).__name__)
            return response
        wrapper._audit_events = tuple(getattr(view, '_audit_events', ())) + tuple(names)
        return wrapper
    return decorator


def _record_access(event, dedup, details, recording_of, view_args, status):
    if status >= 500 or status == 304 or status in (400, 401, 409, 429):
        return
    ok = status < 400
    if not ok and status not in (403, 404):
        return
    if callable(event):
        event, dedup = event()
    if not ok and not is_enabled():
        return
    from src.models import Recording
    if recording_of is not None:
        recording = recording_of(**view_args)
    else:
        recording = db.session.get(Recording, view_args.get('recording_id'))
    if recording is None:
        return
    values = details() if callable(details) else details
    extra = (values or {}).get('part')
    if ok:
        record(event, recording=recording, details=values, dedup=dedup, dedup_extra=extra)
        return
    from flask_login import current_user
    if _has_recording_access is not None and not _has_recording_access(recording, current_user):
        record(event, recording=recording, outcome='denied', details=values,
               dedup=DEDUP_DENIED, dedup_extra=extra)


# --- System settings --------------------------------------------------------

_SETTING_ENUMS = {
    'user_search_scope': ('everyone', 'groups', 'exact'),
    'deletion_mode': ('audio_only', 'full_recording'),
    'audit_log_store_ip': ('full', 'truncated', 'off'),
}
_BOOLEAN_WORDS = {'true': 'true', '1': 'true', 'yes': 'true', 'false': 'false', '0': 'false', 'no': 'false'}


def _user_default(key):
    from src.services import user_defaults
    return user_defaults.get(key)


def setting_value(key, raw):
    """A setting value as a details value: a boolean word, an integer or a
    code of the setting's enumeration. Anything else is 'other'; an empty or
    cleared value is None."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        return 'true' if raw else 'false'
    user_default = _user_default(key)
    if user_default is not None:
        # A default for user settings: a value of its list (a theme id, a
        # language, a time zone, a switch).
        try:
            return user_default.clean(raw)
        except ValueError:
            return 'other'
    text_value = str(raw).strip().lower()
    if key in _SETTING_ENUMS:
        return text_value if text_value in _SETTING_ENUMS[key] else 'other'
    if isinstance(raw, int):
        return raw
    if text_value in ('true', 'false', 'yes', 'no'):
        return _BOOLEAN_WORDS[text_value]
    try:
        return int(text_value)
    except ValueError:
        return 'other'


# Settings created at the first start (src/init_db.py) and by the admin page.
# tests/test_audit_log_admin.py checks that every key of init_db is here.
_DEFINED_SETTING_KEYS = frozenset({
    'admin_default_contextual_speaker_prompt', 'admin_default_hotwords', 'admin_default_initial_prompt',
    'admin_default_summary_prompt', 'admin_default_title_prompt', 'asr_timeout_seconds',
    'audit_log_notice_dismissed', 'custom_banner', 'default_storage_quota_mb', 'default_timezone',
    'disable_auto_summarization', 'disable_auto_translation', 'embedding_identifier', 'enable_folders',
    'login_lockout_minutes', 'login_max_failed_attempts', 'max_audio_only_video_size_mb',
    'max_file_size_mb', 'recording_disclaimer', 'show_whats_new', 'transcript_length_limit',
    'upload_disclaimer', 'transcription_models_visible_json', 'transcription_default_model',
})


def known_setting_key(key):
    """Whether the key names a setting the application itself defines.

    POST /admin/settings accepts any key, so a key is typed text until it is
    found in a list of the code. A key that was merely saved before is not
    known."""
    from src.utils.system_settings import ENV_BACKED_SETTINGS
    if not isinstance(key, str):
        return False
    if key in ENV_BACKED_SETTINGS or key in SETTING_VALUE_KEYS or key in _DEFINED_SETTING_KEYS:
        return True
    try:
        from src.auth.permissions import permission_for_setting
        if permission_for_setting(key):
            return True
        from src.services.llm_settings import TEMPERATURES, setting_key
        return key in {setting_key(kind) for kind in TEMPERATURES}
    except Exception:
        return False


def setting_changed(key, old_raw, new_raw, users_updated=None):
    """admin.setting.changed in the transaction of the change.

    users_updated: for a default for user settings, the number of existing
    accounts that got the new default (0 when only new accounts get it).

    The key is stored when it is a setting the application defines, else as
    'other'. Old and new value are stored for the keys in SETTING_VALUE_KEYS
    only. A change of an audit_log_* setting is recorded also while the log
    is off.
    """
    known = known_setting_key(key)
    details = {'key': key if known else 'other'}
    if known and (key in SETTING_VALUE_KEYS or _user_default(key) is not None):
        details['old'] = setting_value(key, old_raw)
        details['new'] = setting_value(key, new_raw)
    if known and users_updated is not None:
        details['users_updated'] = int(users_updated)
    return record_in_tx('admin.setting.changed', details=details,
                        force=known and key.startswith('audit_log_'))


def llm_model_created(model_id, fields):
    """admin.llm_model.created in the transaction of the change."""
    return record_in_tx('admin.llm_model.created',
                        details={'model': model_id, 'fields': list(fields)})


def llm_model_changed(model_id, fields):
    """admin.llm_model.changed in the transaction of the change."""
    return record_in_tx('admin.llm_model.changed',
                        details={'model': model_id, 'fields': list(fields)})


def llm_model_deleted(model_id):
    """admin.llm_model.deleted in the transaction of the change."""
    return record_in_tx('admin.llm_model.deleted', details={'model': model_id})


def llm_role_assigned(role, model_id, old=None):
    """admin.llm_role.assigned in the transaction of the change."""
    return record_in_tx('admin.llm_role.assigned',
                        details={'role': role, 'model': model_id, 'old': old})


def llm_role_reverted(role, old):
    """admin.llm_role.reverted in the transaction of the change."""
    return record_in_tx('admin.llm_role.reverted',
                        details={'role': role, 'old': old})
