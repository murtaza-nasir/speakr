"""System Statistics overview for the admin dashboard.

One read for the whole tab: headline numbers for a range of days with the
range before it for comparison, recordings per day by source, processing
time and failure rate, model usage per day by operation, cost per model,
twelve months of cost, the latest failed recordings and one row per user.

Days are UTC calendar days, like the usage tables they read.
"""

import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median

from sqlalchemy import case, extract

from src.database import db
from src.models import Recording, TokenUsage, TranscriptionUsage, User
from src.services.token_tracking import token_tracker

RANGES = (7, 30, 90)
SOURCES = ('upload', 'recording', 'merge', 'other')
_SOURCE_MAP = {'upload': 'upload', 'auto_process': 'upload', 'recording': 'recording',
               'recording_session': 'recording', 'merge': 'merge'}


def _source(value):
    return _SOURCE_MAP.get(value or 'upload', 'other')


def _days(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _usage_window(start, end):
    """Tokens, cost and transcription seconds between two dates, inclusive."""
    tok = db.session.query(TokenUsage.operation_type, db.func.sum(TokenUsage.total_tokens),
                           db.func.sum(TokenUsage.cost), db.func.sum(TokenUsage.request_count)) \
        .filter(TokenUsage.date >= start, TokenUsage.date <= end).group_by(TokenUsage.operation_type).all()
    tr = db.session.query(db.func.sum(TranscriptionUsage.audio_duration_seconds),
                          db.func.sum(TranscriptionUsage.estimated_cost)) \
        .filter(TranscriptionUsage.date >= start, TranscriptionUsage.date <= end).first()
    out = {'llm_cost': 0.0, 'embedding_cost': 0.0, 'ai_requests': 0,
           'transcription_seconds': int(tr[0] or 0), 'transcription_cost': float(tr[1] or 0)}
    for op, _tokens, cost, requests in tok:
        if token_tracker.is_embedding_op(op):
            out['embedding_cost'] += float(cost or 0)
        else:
            out['llm_cost'] += float(cost or 0)
            out['ai_requests'] += int(requests or 0)
    out['cost'] = round(out['llm_cost'] + out['embedding_cost'] + out['transcription_cost'], 6)
    return out


def _recordings_window(start, end):
    """Recordings created between two dates: count and active users."""
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time())
    count, users = db.session.query(db.func.count(Recording.id), db.func.count(db.distinct(Recording.user_id))) \
        .filter(Recording.created_at >= lo, Recording.created_at < hi).first()
    return int(count or 0), int(users or 0)


def _monthly_cost(months, today):
    y, m = today.year, today.month
    keys = []
    for _ in range(months):
        keys.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    keys.reverse()
    start = date(*keys[0], 1)
    rows = {k: {'year': k[0], 'month': k[1], 'llm_cost': 0.0, 'embedding_cost': 0.0,
                'transcription_cost': 0.0, 'llm_tokens': 0, 'embedding_tokens': 0, 'minutes': 0}
            for k in keys}
    y_, m_ = extract('year', TokenUsage.date), extract('month', TokenUsage.date)
    for yy, mm, op, tokens, cost in db.session.query(
            y_, m_, TokenUsage.operation_type, db.func.sum(TokenUsage.total_tokens), db.func.sum(TokenUsage.cost)) \
            .filter(TokenUsage.date >= start).group_by(y_, m_, TokenUsage.operation_type):
        row = rows.get((int(yy), int(mm)))
        if row is None:
            continue
        kind = 'embedding' if token_tracker.is_embedding_op(op) else 'llm'
        row[f'{kind}_cost'] += float(cost or 0)
        row[f'{kind}_tokens'] += int(tokens or 0)
    ty, tm = extract('year', TranscriptionUsage.date), extract('month', TranscriptionUsage.date)
    for yy, mm, seconds, cost in db.session.query(
            ty, tm, db.func.sum(TranscriptionUsage.audio_duration_seconds), db.func.sum(TranscriptionUsage.estimated_cost)) \
            .filter(TranscriptionUsage.date >= start).group_by(ty, tm):
        row = rows.get((int(yy), int(mm)))
        if row is not None:
            row['transcription_cost'] += float(cost or 0)
            row['minutes'] += int(seconds or 0) // 60
    return [rows[k] for k in keys]


def build_overview(days=30, today=None):
    days = days if days in RANGES else 30
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(today + timedelta(days=1), datetime.min.time())

    # ---- headline numbers, this range against the one before it
    cur, prev = _usage_window(start, today), _usage_window(prev_start, prev_end)
    rec_cur, users_cur = _recordings_window(start, today)
    rec_prev, users_prev = _recordings_window(prev_start, prev_end)
    storage = db.session.query(db.func.coalesce(db.func.sum(Recording.file_size), 0)) \
        .filter(Recording.audio_deleted_at.is_(None)).scalar() or 0

    # ---- recordings per day by source, processing time, failure rate
    daily_rec = {d.isoformat(): {'date': d.isoformat(), **{s: 0 for s in SOURCES}, 'failed': 0}
                 for d in _days(start, today)}
    durations = []
    failed = 0
    rows = db.session.query(Recording.created_at, Recording.processing_source, Recording.status,
                            Recording.processing_time_seconds) \
        .filter(Recording.created_at >= lo, Recording.created_at < hi).all()
    for created, source, status, seconds in rows:
        day = daily_rec.get(created.date().isoformat())
        if day is None:
            continue
        day[_source(source)] += 1
        if status == 'FAILED':
            day['failed'] += 1
            failed += 1
        if status == 'COMPLETED' and seconds:
            durations.append(seconds)

    # ---- model usage per day by operation, transcription minutes per day
    # Embedding tokens run an order of magnitude above the rest, so they are
    # a series of their own; 'operations' holds the language-model calls.
    daily_use = {d.isoformat(): {'date': d.isoformat(), 'operations': {}, 'embedding_tokens': 0, 'minutes': 0}
                 for d in _days(start, today)}
    operations = defaultdict(int)
    for d, op, tokens in db.session.query(TokenUsage.date, TokenUsage.operation_type, db.func.sum(TokenUsage.total_tokens)) \
            .filter(TokenUsage.date >= start, TokenUsage.date <= today) \
            .group_by(TokenUsage.date, TokenUsage.operation_type):
        row = daily_use.get(d.isoformat())
        if row is None:
            continue
        if token_tracker.is_embedding_op(op):
            row['embedding_tokens'] += int(tokens or 0)
        else:
            row['operations'][op] = row['operations'].get(op, 0) + int(tokens or 0)
            operations[op] += int(tokens or 0)
    for d, seconds in db.session.query(TranscriptionUsage.date, db.func.sum(TranscriptionUsage.audio_duration_seconds)) \
            .filter(TranscriptionUsage.date >= start, TranscriptionUsage.date <= today).group_by(TranscriptionUsage.date):
        row = daily_use.get(d.isoformat())
        if row is not None:
            row['minutes'] += round((seconds or 0) / 60, 1)

    # ---- cost per model in the range
    by_model = []
    for model, op, tokens, requests, cost in db.session.query(
            TokenUsage.model_name, TokenUsage.operation_type, db.func.sum(TokenUsage.total_tokens),
            db.func.sum(TokenUsage.request_count), db.func.sum(TokenUsage.cost)) \
            .filter(TokenUsage.date >= start, TokenUsage.date <= today) \
            .group_by(TokenUsage.model_name, TokenUsage.operation_type):
        by_model.append(('embedding' if token_tracker.is_embedding_op(op) else 'llm', model or '', op,
                         int(tokens or 0), int(requests or 0), float(cost or 0)))
    models = {}
    for kind, model, _op, tokens, requests, cost in by_model:
        row = models.setdefault((kind, model), {'kind': kind, 'model': model, 'tokens': 0, 'minutes': 0,
                                                'requests': 0, 'cost': 0.0})
        row['tokens'] += tokens
        row['requests'] += requests
        row['cost'] += cost
    for model, connector, seconds, requests, cost in db.session.query(
            TranscriptionUsage.model_name, TranscriptionUsage.connector_type,
            db.func.sum(TranscriptionUsage.audio_duration_seconds), db.func.sum(TranscriptionUsage.request_count),
            db.func.sum(TranscriptionUsage.estimated_cost)) \
            .filter(TranscriptionUsage.date >= start, TranscriptionUsage.date <= today) \
            .group_by(TranscriptionUsage.model_name, TranscriptionUsage.connector_type):
        name = model or connector or ''
        row = models.setdefault(('transcription', name), {'kind': 'transcription', 'model': name, 'tokens': 0,
                                                          'minutes': 0, 'requests': 0, 'cost': 0.0})
        row['minutes'] += round((seconds or 0) / 60, 1)
        row['requests'] += int(requests or 0)
        row['cost'] += float(cost or 0)
    model_rows = sorted(models.values(), key=lambda r: (-r['cost'], -r['tokens'], -r['minutes'], r['model']))

    # ---- latest failed recordings
    failed_recent = [
        {'id': r.id, 'title': r.title or r.original_filename or f'#{r.id}', 'username': username,
         'created_at': r.created_at.isoformat() + 'Z' if r.created_at else None,
         'error': (r.error_message or '')[:300]}
        for r, username in db.session.query(Recording, User.username).join(User, User.id == Recording.user_id)
        .filter(Recording.status == 'FAILED').order_by(Recording.created_at.desc()).limit(5)
    ]

    # ---- one row per user
    month_start = today.replace(day=1)
    rec_by_user = {uid: (int(n or 0), int(size or 0), last) for uid, n, size, last in db.session.query(
        Recording.user_id, db.func.count(Recording.id),
        db.func.coalesce(db.func.sum(case((Recording.audio_deleted_at.is_(None), Recording.file_size), else_=0)), 0),
        db.func.max(Recording.created_at)).group_by(Recording.user_id)}
    tok_by_user = dict(db.session.query(TokenUsage.user_id, db.func.sum(TokenUsage.total_tokens))
                       .filter(TokenUsage.date >= month_start, TokenUsage.date <= today).group_by(TokenUsage.user_id).all())
    cost_by_user = defaultdict(float)
    for uid, cost in db.session.query(TokenUsage.user_id, db.func.sum(TokenUsage.cost)) \
            .filter(TokenUsage.date >= month_start, TokenUsage.date <= today).group_by(TokenUsage.user_id):
        cost_by_user[uid] += float(cost or 0)
    sec_by_user = {}
    for uid, seconds, cost in db.session.query(TranscriptionUsage.user_id, db.func.sum(TranscriptionUsage.audio_duration_seconds),
                                               db.func.sum(TranscriptionUsage.estimated_cost)) \
            .filter(TranscriptionUsage.date >= month_start, TranscriptionUsage.date <= today) \
            .group_by(TranscriptionUsage.user_id):
        sec_by_user[uid] = int(seconds or 0)
        cost_by_user[uid] += float(cost or 0)
    users = []
    for u in User.query.order_by(User.username).all():
        n, size, last = rec_by_user.get(u.id, (0, 0, None))
        tokens = int(tok_by_user.get(u.id) or 0)
        seconds = sec_by_user.get(u.id, 0)
        tb, sb = u.monthly_token_budget, u.monthly_transcription_budget
        users.append({
            'id': u.id, 'username': u.username, 'recordings': n, 'storage': size,
            'last_recording_at': last.isoformat() + 'Z' if last else None,
            'tokens_month': tokens, 'token_budget': tb,
            'token_pct': round(tokens / tb * 100, 1) if tb else None,
            'minutes_month': seconds // 60, 'minutes_budget': (sb // 60) if sb else None,
            'minutes_pct': round(seconds / sb * 100, 1) if sb else None,
            'cost_month': round(cost_by_user.get(u.id, 0.0), 6),
        })

    from src.services.embeddings import embeddings_are_local
    return {
        'days': days,
        'start': start.isoformat(),
        'end': today.isoformat(),
        'overview': {
            'recordings': {'current': rec_cur, 'previous': rec_prev},
            'audio_minutes': {'current': cur['transcription_seconds'] // 60, 'previous': prev['transcription_seconds'] // 60},
            'ai_cost': {'current': cur['cost'], 'previous': prev['cost']},
            'ai_requests': {'current': cur['ai_requests'], 'previous': prev['ai_requests']},
            'active_users': {'current': users_cur, 'previous': users_prev, 'total': len(users)},
            'storage': int(storage),
        },
        'activity': {
            'daily': list(daily_rec.values()),
            'created': len(rows),
            'failed': failed,
            'failure_rate': round(failed / len(rows) * 100, 1) if rows else None,
            'median_processing_seconds': int(median(durations)) if durations else None,
        },
        'usage': {
            'daily': list(daily_use.values()),
            'operations': [op for op, _ in sorted(operations.items(), key=lambda kv: -kv[1])],
            'by_model': model_rows,
            'monthly': _monthly_cost(12, today),
            'embeddings_local': embeddings_are_local(),
            'models_local': os.environ.get('MODELS_ARE_LOCAL', 'false').lower() == 'true',
        },
        'failed_recent': failed_recent,
        'users': users,
    }
