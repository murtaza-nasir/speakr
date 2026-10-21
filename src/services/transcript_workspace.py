"""Unified draft persistence. Legacy speaker save routes keep their contracts.

The transcript commit and best-effort follow-ups have distinct acknowledgements.
Voice training receives provenance deltas; snippet rebuilding receives full coverage.
"""
import copy
import json
import math
from datetime import datetime

from flask import current_app
from src.database import db
from src.models import Speaker, SpeakerVoiceSample
from src.services.speaker import DEFAULT_LABEL, find_user_speaker, participants_from_segments


def validate_segments(segments):
    if not isinstance(segments, list):
        raise ValueError('Invalid transcript data')
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('speaker'), str) or not isinstance(segment.get('sentence'), str):
            raise ValueError('Invalid transcript segment')
        # Legacy JSON speaker edits may have no timing data. Preserve that shape.
        if segment.get('start_time') is None and segment.get('end_time') is None:
            continue
        for field in ('start_time', 'end_time'):
            value = segment.get(field)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError('Invalid transcript bounds')
        if segment['end_time'] < segment['start_time']:
            raise ValueError('Invalid transcript bounds')
    return copy.deepcopy(segments)


def validate_effects(recording, segments, effects):
    if not isinstance(effects, dict):
        raise ValueError('Invalid workspace effects')
    before = json.loads(recording.transcription or '[]')
    if not isinstance(before, list):
        raise ValueError('JSON transcript required')
    names_before = {s.get('speaker') for s in before if isinstance(s, dict)}
    names_after = {s['speaker'] for s in segments}
    labels = set(recording.speaker_embeddings or {}) | set(recording.speaker_label_map or {})
    changes = effects.get('assignment_changes', [])
    training = effects.get('eligible_training_assignments', {})
    invalid = effects.get('invalidated_voice_labels', [])
    if not isinstance(changes, list) or not isinstance(training, dict) or not isinstance(invalid, list):
        raise ValueError('Invalid workspace effects')
    assignments = {}
    for change in changes:
        if not isinstance(change, dict) or change.get('current_before') not in names_before or change.get('final_name') not in names_after:
            raise ValueError('Assignment does not belong to this recording')
        label = change.get('source_label')
        if label is not None:
            if label not in labels or change['current_before'] not in (label, (recording.speaker_label_map or {}).get(label)):
                raise ValueError('Invalid assignment provenance')
            assignments.setdefault(label, set()).add(change['final_name'])
    if any(not isinstance(label, str) or label not in labels for label in invalid):
        raise ValueError('Unknown voice label')
    for label, name in training.items():
        if label not in labels or label in invalid or assignments.get(label) != {name} or name not in names_after or DEFAULT_LABEL.match(name):
            raise ValueError('Invalid training assignment')
    if any(len(names) > 1 and label not in invalid for label, names in assignments.items()):
        raise ValueError('Mixed voice label must be invalidated')
    seconds = effects.get('training_seconds_by_label')
    if seconds is not None:
        total = sum((s.get('end_time') or 0) - (s.get('start_time') or 0) for s in segments)
        if not isinstance(seconds, dict) or set(seconds) != set(training) or any(
                isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0 or value > total + 1e-6
                for value in seconds.values()):
            raise ValueError('Invalid source speech duration')
    else:
        from src.services.voice_profiles import speech_seconds_by_label
        prior_seconds = speech_seconds_by_label(before) or {}
        seconds = {label: prior_seconds.get((recording.speaker_label_map or {}).get(label, label), 0) for label in training}
    return changes, training, set(invalid), seconds


def save_workspace(recording, submitted, effects, user, regenerate_summary,
                   reindex, export, enqueue):
    segments = validate_segments(submitted)
    changes, training, invalid, source_seconds = validate_effects(recording, segments, effects)
    # Normalize every displayed name, independently of the usage/training delta.
    spellings = {}
    def resolve(name):
        name = name.strip()
        key = name.casefold()
        if key not in spellings:
            saved = find_user_speaker(user.id, name)
            spellings[key] = saved.name if saved else name
        return spellings[key]
    for segment in segments:
        segment['speaker'] = resolve(segment['speaker'])
        segment.pop('speaker_id', None)  # Re-link only to the recording owner.
        for field in ('id', 'showSuggestions', 'filteredSpeakers'):
            segment.pop(field, None)
    used = {resolve(c['final_name']) for c in changes if c['current_before'] != c['final_name'] and not DEFAULT_LABEL.match(c['final_name'])}
    prior_names = {s.get('speaker') for s in json.loads(recording.transcription or '[]')}
    used.update(s['speaker'] for s in segments if s['speaker'] not in prior_names and not DEFAULT_LABEL.match(s['speaker']))
    training = {label: resolve(name) for label, name in training.items()}
    for name in used:
        if find_user_speaker(user.id, name) is None:
            db.session.add(Speaker(name=name, user_id=user.id, use_count=0))
    db.session.flush()
    # Once a source has mixed voices, it must not become eligible again on reopen.
    embeddings = dict(recording.speaker_embeddings or {})
    label_map = dict(recording.speaker_label_map or {})
    for label in invalid:
        embeddings.pop(label, None)
        label_map.pop(label, None)
    assignments = {}
    for change in changes:
        label = change.get('source_label')
        if label and label not in invalid:
            assignments.setdefault(label, set()).add(resolve(change['final_name']))
    for label, names in assignments.items():
        if len(names) == 1 and not DEFAULT_LABEL.match(next(iter(names))):
            label_map[label] = next(iter(names))
    recording.speaker_embeddings = embeddings or None
    recording.speaker_label_map = label_map or None
    from src.services.speaker_links import link_list
    link_list(segments, recording)
    recording.transcription = json.dumps(segments, ensure_ascii=False)
    recording.participants = participants_from_segments(segments)
    db.session.commit()

    status = {}
    def follow_up(name, operation, success='done'):
        try:
            operation()
            db.session.commit()
            status[name] = success
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Workspace follow-up %s failed for recording %s', name, recording.id)
            status[name] = 'failed'

    def usage():
        for name in used:
            speaker = find_user_speaker(user.id, name)
            speaker.use_count += 1
            speaker.last_used = datetime.utcnow()
    if used:
        follow_up('usage', usage)
    else:
        status['usage'] = 'skipped'

    def profiles():
        from src.services.voice_profiles import apply_names_to_profiles, refresh_speaker_summary, record_sample, from_bytes
        touched = set()
        for sample in SpeakerVoiceSample.query.filter_by(user_id=user.id, recording_id=recording.id).all():
            if sample.label in invalid:
                touched.add(sample.speaker_id)
                db.session.delete(sample)
            elif sample.label not in training and len(assignments.get(sample.label, set())) == 1:
                # On reopen, several labels may share one stored name. Their
                # existing samples retain known duration/weight; an unambiguous
                # correction moves them without inventing per-label durations.
                target = find_user_speaker(user.id, next(iter(assignments[sample.label])))
                if target and target.id != sample.speaker_id:
                    touched.update((sample.speaker_id, target.id))
                    retained = {field: getattr(sample, field) for field in
                                ('embedding', 'dimension', 'space_id', 'speech_seconds', 'source', 'weight')}
                    outcome = record_sample(target, recording, sample.label,
                                            from_bytes(sample.embedding), sample.speech_seconds, sample.source)
                    if outcome == 'stored':
                        for field, value in retained.items():
                            setattr(sample, field, value)
        apply_names_to_profiles(recording, training, source_seconds, user)
        db.session.flush()
        for sid in touched:
            speaker = db.session.get(Speaker, sid)
            if speaker:
                refresh_speaker_summary(speaker)
    follow_up('training', profiles)
    from src.services.speaker_snippets import create_speaker_snippets
    coverage = {s['speaker']: {'name': s['speaker']} for s in segments if not DEFAULT_LABEL.match(s['speaker'])}
    follow_up('snippets', lambda: create_speaker_snippets(recording.id, coverage))
    follow_up('reindex', lambda: reindex(recording.id), 'queued')
    if regenerate_summary:
        follow_up('summary', lambda: enqueue(user_id=user.id, recording_id=recording.id,
                  job_type='summarize', params={'user_id': user.id}), 'queued')
        status['export'] = 'queued' if status['summary'] == 'queued' else 'skipped'
    else:
        status['summary'] = 'skipped'
        follow_up('export', lambda: export(recording.id))
    return status
