"""Validation and storage of external references (mailr spec G8)."""

import re

from src.database import db

MAX_PER_RECORDING = 50
_SLUG = re.compile(r'^[a-z0-9_.-]{1,40}$')


class RefError(ValueError):
    def __init__(self, message, code='invalid_parameter', status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def validate(raw):
    """A clean ref dict from client input, or RefError."""
    if not isinstance(raw, dict):
        raise RefError('Each external reference must be an object')
    system = raw.get('system')
    kind = raw.get('kind')
    ref = raw.get('ref')
    url = raw.get('url')
    label = raw.get('label')
    if not isinstance(system, str) or not _SLUG.match(system):
        raise RefError('system must be 1 to 40 characters of a-z, 0-9, _ . -')
    if not isinstance(kind, str) or not _SLUG.match(kind):
        raise RefError('kind must be 1 to 40 characters of a-z, 0-9, _ . -')
    if not isinstance(ref, str) or not 1 <= len(ref) <= 500 or not ref.isprintable():
        raise RefError('ref must be 1 to 500 printable characters')
    if url is not None:
        if not isinstance(url, str) or len(url) > 1000 or not re.match(r'^https?://\S+$', url, re.I):
            raise RefError('url must be an http:// or https:// address of at most 1000 characters')
    if label is not None and (not isinstance(label, str) or len(label) > 200):
        raise RefError('label must be at most 200 characters')
    return {'system': system, 'kind': kind, 'ref': ref, 'url': url or None, 'label': label or None}


def validate_list(raw_list):
    if not isinstance(raw_list, list):
        raise RefError('external_refs must be a list')
    if len(raw_list) > MAX_PER_RECORDING:
        raise RefError(f'At most {MAX_PER_RECORDING} external references per recording', code='conflict', status=409)
    return [validate(r) for r in raw_list]


def refs_for(recording_id, user_id):
    from src.models import RecordingExternalRef
    return (RecordingExternalRef.query.filter_by(recording_id=recording_id, user_id=user_id)
            .order_by(RecordingExternalRef.id).all())


def refs_map(recording_ids, user_id):
    """{recording id: [ref dict]} for one page of recordings, in one query."""
    from src.models import RecordingExternalRef
    result = {rid: [] for rid in recording_ids}
    if not recording_ids:
        return result
    for ref in (RecordingExternalRef.query
                .filter(RecordingExternalRef.recording_id.in_(list(recording_ids)),
                        RecordingExternalRef.user_id == user_id)
                .order_by(RecordingExternalRef.id)):
        result.setdefault(ref.recording_id, []).append(ref.to_dict())
    return result


def add(recording, user_id, clean):
    """(ref, created). An existing (system, kind, ref) is returned unchanged."""
    from src.models import RecordingExternalRef
    existing = RecordingExternalRef.query.filter_by(
        recording_id=recording.id, user_id=user_id, system=clean['system'], kind=clean['kind'],
        ref=clean['ref']).first()
    if existing is not None:
        return existing, False
    if RecordingExternalRef.query.filter_by(recording_id=recording.id, user_id=user_id).count() >= MAX_PER_RECORDING:
        raise RefError(f'At most {MAX_PER_RECORDING} external references per recording', code='conflict', status=409)
    row = RecordingExternalRef(recording_id=recording.id, user_id=user_id, **clean)
    db.session.add(row)
    return row, True


def replace_system(recording, user_id, system, cleans):
    """Replace this user's refs of one system on the recording."""
    from src.models import RecordingExternalRef
    if any(c['system'] != system for c in cleans):
        raise RefError('Every reference must have the system given in ?system=')
    others = RecordingExternalRef.query.filter(
        RecordingExternalRef.recording_id == recording.id, RecordingExternalRef.user_id == user_id,
        RecordingExternalRef.system != system).count()
    unique = {(c['kind'], c['ref']): c for c in cleans}
    if others + len(unique) > MAX_PER_RECORDING:
        raise RefError(f'At most {MAX_PER_RECORDING} external references per recording', code='conflict', status=409)
    for row in RecordingExternalRef.query.filter_by(recording_id=recording.id, user_id=user_id, system=system):
        db.session.delete(row)
    db.session.flush()
    for c in unique.values():
        db.session.add(RecordingExternalRef(recording_id=recording.id, user_id=user_id, **c))
