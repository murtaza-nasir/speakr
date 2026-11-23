"""Validation and storage of external references (mailr spec G8)."""

import re

from src.database import db

MAX_PER_RECORDING = 50
_SLUG = re.compile(r'^[a-z0-9_.-]{1,40}$')


class RefError(ValueError):
    """A reference that cannot be stored. `key` names the message
    server.apiError.<key>, said in the interface language; `code` is the
    stable code of the answer (invalid_parameter, or conflict for the
    limit) and `status` its HTTP status."""

    def __init__(self, key, code='invalid_parameter', status=400, **params):
        from src.utils.localization import translate
        super().__init__(translate(f'server.apiError.{key}', **params))
        self.key = key
        self.code = code
        self.status = status


def validate(raw):
    """A clean ref dict from client input, or RefError."""
    if not isinstance(raw, dict):
        raise RefError('external_ref_not_object')
    system = raw.get('system')
    kind = raw.get('kind')
    ref = raw.get('ref')
    url = raw.get('url')
    label = raw.get('label')
    if not isinstance(system, str) or not _SLUG.match(system):
        raise RefError('external_ref_system_invalid')
    if not isinstance(kind, str) or not _SLUG.match(kind):
        raise RefError('external_ref_kind_invalid')
    if not isinstance(ref, str) or not 1 <= len(ref) <= 500 or not ref.isprintable():
        raise RefError('external_ref_ref_invalid')
    if url is not None:
        if not isinstance(url, str) or len(url) > 1000 or not re.match(r'^https?://\S+$', url, re.I):
            raise RefError('external_ref_url_invalid')
    if label is not None and (not isinstance(label, str) or len(label) > 200):
        raise RefError('external_ref_label_invalid')
    return {'system': system, 'kind': kind, 'ref': ref, 'url': url or None, 'label': label or None}


def validate_list(raw_list):
    if not isinstance(raw_list, list):
        raise RefError('external_refs_not_list')
    if len(raw_list) > MAX_PER_RECORDING:
        raise RefError('external_refs_too_many', code='conflict', status=409, max=MAX_PER_RECORDING)
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
        raise RefError('external_refs_too_many', code='conflict', status=409, max=MAX_PER_RECORDING)
    row = RecordingExternalRef(recording_id=recording.id, user_id=user_id, **clean)
    db.session.add(row)
    return row, True


def replace_system(recording, user_id, system, cleans):
    """Replace this user's refs of one system on the recording."""
    from src.models import RecordingExternalRef
    if any(c['system'] != system for c in cleans):
        raise RefError('external_ref_system_mismatch')
    others = RecordingExternalRef.query.filter(
        RecordingExternalRef.recording_id == recording.id, RecordingExternalRef.user_id == user_id,
        RecordingExternalRef.system != system).count()
    unique = {(c['kind'], c['ref']): c for c in cleans}
    if others + len(unique) > MAX_PER_RECORDING:
        raise RefError('external_refs_too_many', code='conflict', status=409, max=MAX_PER_RECORDING)
    for row in RecordingExternalRef.query.filter_by(recording_id=recording.id, user_id=user_id, system=system):
        db.session.delete(row)
    db.session.flush()
    for c in unique.values():
        db.session.add(RecordingExternalRef(recording_id=recording.id, user_id=user_id, **c))
