"""Remove diarization labels from the stores that hold people (run_once 0008).

Before the rule of src/utils/speaker_labels.py was applied everywhere, a
label such as SPEAKER_00 could become a saved speaker, a participant, the
speaker name of an Inquire chunk or a stored Ask filter. This module removes
what those releases left behind:

- ``speaker`` rows whose name is a label, with their voice samples
  (``speaker_voice_sample``) and snippets (``speaker_snippet``);
- the ``speaker_id`` of transcript segments that pointed at a removed row
  (the visible speaker label of the segment is not touched);
- labels in ``recording.participants``;
- labels in ``transcript_chunk.speaker_name`` (set to NULL; the chunk text
  keeps the label inline);
- labels in ``inquire_session.filter_speakers``.

A row is kept when a person entered contact details for it (email or
aliases): that is data somebody typed, and deleting it is their decision.
Such a row stays out of every list, and its id is reported.

It works in two steps. ``plan()`` only reads and returns what would change;
``apply()`` writes that plan. Every write names the value it expects to
replace, so a change made by a person (or by another worker running the same
cleanup) between the two steps is left alone. Running it again finds nothing.

Plain SQL through the engine, no models: the dry-run script can point it at
a copy of a database without starting the application. Nothing here logs a
name, only counts and ids.
"""

import json
from datetime import datetime

from sqlalchemy import inspect, text

from src.utils.speaker_labels import clean_participants, is_generic_speaker_label

BATCH = 200

COUNT_KEYS = (
    'speakers_removed', 'speakers_with_voice_profile', 'voice_samples_removed', 'snippets_removed',
    'recordings_unlinked', 'segments_unlinked',
    'recordings_participants_cleaned', 'participant_labels_removed',
    'chunks_cleared', 'chunk_recordings', 'inquire_sessions_cleaned',
)


def _tables(engine):
    return set(inspect(engine).get_table_names())


def _has_columns(engine, table, *columns):
    names = {c['name'] for c in inspect(engine).get_columns(table)}
    return all(c in names for c in columns)


def _in_clause(ids, prefix='i'):
    """('(:i0, :i1)', {'i0': .., 'i1': ..}) for a non-empty list of ids."""
    params = {f'{prefix}{n}': value for n, value in enumerate(ids)}
    return '(' + ', '.join(f':{k}' for k in params) + ')', params


def _chunks(items, size=BATCH):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _unlink(transcription, removed_ids):
    """(new text, segments changed) with speaker_id dropped where it points at
    a removed row; (None, 0) when nothing changes or it is not a JSON transcript."""
    try:
        segments = json.loads(transcription)
    except (TypeError, ValueError):
        return None, 0
    if not isinstance(segments, list):
        return None, 0
    changed = 0
    for segment in segments:
        if isinstance(segment, dict) and segment.get('speaker_id') in removed_ids \
                and not isinstance(segment.get('speaker_id'), bool):
            del segment['speaker_id']
            changed += 1
    return (json.dumps(segments, ensure_ascii=False), changed) if changed else (None, 0)


def plan(engine):
    """Read the database and return what apply() would change. Writes nothing.

    {'counts': {...}, 'kept_speaker_ids': [...], and the work lists}.
    """
    tables = _tables(engine)
    counts = dict.fromkeys(COUNT_KEYS, 0)
    out = {'counts': counts, 'kept_speaker_ids': [], 'speakers': [], 'transcripts': [],
           'participants': [], 'chunk_names': [], 'sessions': [], 'users_affected': 0}

    with engine.connect() as conn:
        removable = {}
        users = set()
        if 'speaker' in tables:
            contact = _has_columns(engine, 'speaker', 'email', 'aliases')
            columns = 'id, name, user_id, average_embedding IS NOT NULL' + (', email, aliases' if contact else '')
            for row in conn.execute(text(f'SELECT {columns} FROM speaker ORDER BY id')):
                if not is_generic_speaker_label(row[1]):
                    continue
                if contact and ((row[4] or '').strip() or (row[5] or '').strip() not in ('', '[]')):
                    out['kept_speaker_ids'].append(row[0])
                    continue
                removable[row[0]] = row[1]
                users.add(row[2])
                out['speakers'].append({'id': row[0], 'name': row[1]})
                counts['speakers_removed'] += 1
                counts['speakers_with_voice_profile'] += 1 if row[3] else 0
            out['users_affected'] = len(users)

        for ids in _chunks(removable):
            clause, params = _in_clause(ids)
            if 'speaker_voice_sample' in tables:
                counts['voice_samples_removed'] += conn.execute(text(
                    f'SELECT COUNT(*) FROM speaker_voice_sample WHERE speaker_id IN {clause}'), params).scalar() or 0
            if 'speaker_snippet' in tables:
                counts['snippets_removed'] += conn.execute(text(
                    f'SELECT COUNT(*) FROM speaker_snippet WHERE speaker_id IN {clause}'), params).scalar() or 0

        if 'recording' in tables:
            last_id = 0
            while True:
                rows = conn.execute(text(
                    'SELECT id, participants FROM recording WHERE id > :last AND participants IS NOT NULL '
                    'ORDER BY id LIMIT :n'), {'last': last_id, 'n': BATCH * 5}).fetchall()
                if not rows:
                    break
                for rec_id, participants in rows:
                    last_id = rec_id
                    cleaned = clean_participants(participants)
                    if cleaned != participants:
                        out['participants'].append({'id': rec_id, 'old': participants, 'new': cleaned})
                        counts['recordings_participants_cleaned'] += 1
                        counts['participant_labels_removed'] += sum(
                            1 for p in participants.split(',') if is_generic_speaker_label(p))

            # Transcripts are read only when a row is removed, and only those
            # of the users who own such a row: a segment links to a saved
            # speaker of the recording's owner.
            removed_ids = set(removable)
            for owner_ids in _chunks(sorted(u for u in users if u is not None)):
                clause, params = _in_clause(owner_ids, 'u')
                last_id = 0
                while True:
                    rows = conn.execute(text(
                        f'SELECT id, transcription FROM recording WHERE id > :last AND user_id IN {clause} '
                        'AND transcription LIKE :needle ORDER BY id LIMIT :n'),
                        dict(params, last=last_id, needle='%"speaker_id"%', n=BATCH)).fetchall()
                    if not rows:
                        break
                    for rec_id, transcription in rows:
                        last_id = rec_id
                        new_text, changed = _unlink(transcription, removed_ids)
                        if new_text is not None:
                            out['transcripts'].append(rec_id)
                            counts['recordings_unlinked'] += 1
                            counts['segments_unlinked'] += changed

        if 'transcript_chunk' in tables:
            recordings = set()
            for name, in conn.execute(text(
                    'SELECT DISTINCT speaker_name FROM transcript_chunk WHERE speaker_name IS NOT NULL')).fetchall():
                if not is_generic_speaker_label(name):
                    continue
                out['chunk_names'].append(name)
                counts['chunks_cleared'] += conn.execute(text(
                    'SELECT COUNT(*) FROM transcript_chunk WHERE speaker_name = :n'), {'n': name}).scalar() or 0
                recordings.update(r for r, in conn.execute(text(
                    'SELECT DISTINCT recording_id FROM transcript_chunk WHERE speaker_name = :n'), {'n': name}))
            counts['chunk_recordings'] = len(recordings)

        if 'inquire_session' in tables and _has_columns(engine, 'inquire_session', 'filter_speakers'):
            for sid, stored in conn.execute(text(
                    'SELECT id, filter_speakers FROM inquire_session WHERE filter_speakers IS NOT NULL')).fetchall():
                try:
                    names = json.loads(stored)
                except (TypeError, ValueError):
                    continue
                if not isinstance(names, list):
                    continue
                kept = [n for n in names if not is_generic_speaker_label(n)]
                if len(kept) != len(names):
                    out['sessions'].append({'id': sid, 'old': stored, 'new': json.dumps(kept)})
                    counts['inquire_sessions_cleaned'] += 1
    return out


def apply(engine, planned):
    """Write a plan. Returns the counts of what this call changed."""
    done = dict.fromkeys(COUNT_KEYS, 0)
    tables = _tables(engine)

    # Segment links first: when a run stops halfway, the rows are still there
    # and the next run finds the same work. The transcript is read again here
    # and written only if it is still the text that was read.
    removable = {speaker['id'] for speaker in planned['speakers']}
    for rec_id in planned['transcripts']:
        with engine.begin() as conn:
            current = conn.execute(text('SELECT transcription FROM recording WHERE id = :id'), {'id': rec_id}).scalar()
            new_text, changed = _unlink(current, removable) if current else (None, 0)
            if new_text is None:
                continue
            if conn.execute(text('UPDATE recording SET transcription = :new WHERE id = :id AND transcription = :old'),
                            {'new': new_text, 'id': rec_id, 'old': current}).rowcount:
                done['recordings_unlinked'] += 1
                done['segments_unlinked'] += changed

    for speaker in planned['speakers']:
        with engine.begin() as conn:
            # Takes the row (PostgreSQL) or the database (SQLite) for writing
            # and confirms the name is still the label that was planned: a
            # row renamed or already removed in the meantime is left alone.
            held = conn.execute(text('UPDATE speaker SET name = name WHERE id = :id AND name = :name'),
                                {'id': speaker['id'], 'name': speaker['name']}).rowcount
            if not held:
                continue
            if 'speaker_snippet' in tables:
                done['snippets_removed'] += conn.execute(
                    text('DELETE FROM speaker_snippet WHERE speaker_id = :id'), {'id': speaker['id']}).rowcount
            if 'speaker_voice_sample' in tables:
                done['voice_samples_removed'] += conn.execute(
                    text('DELETE FROM speaker_voice_sample WHERE speaker_id = :id'), {'id': speaker['id']}).rowcount
            done['speakers_removed'] += conn.execute(
                text('DELETE FROM speaker WHERE id = :id'), {'id': speaker['id']}).rowcount

    has_updated_at = 'recording' in tables and _has_columns(engine, 'recording', 'updated_at')
    for batch in _chunks(planned['participants']):
        with engine.begin() as conn:
            for item in batch:
                # participants is a field clients read: updated_at moves so a
                # client that syncs by it sees the change.
                statement = ('UPDATE recording SET participants = :new, updated_at = :now '
                             if has_updated_at else 'UPDATE recording SET participants = :new ')
                params = {'new': item['new'], 'id': item['id'], 'old': item['old']}
                if has_updated_at:
                    params['now'] = datetime.utcnow()
                changed = conn.execute(text(statement + 'WHERE id = :id AND participants = :old'), params).rowcount
                if changed:
                    done['recordings_participants_cleaned'] += 1
                    done['participant_labels_removed'] += sum(
                        1 for p in item['old'].split(',') if is_generic_speaker_label(p))

    for name in planned['chunk_names']:
        with engine.begin() as conn:
            done['chunks_cleared'] += conn.execute(
                text('UPDATE transcript_chunk SET speaker_name = NULL WHERE speaker_name = :n'), {'n': name}).rowcount

    for item in planned['sessions']:
        with engine.begin() as conn:
            done['inquire_sessions_cleaned'] += conn.execute(
                text('UPDATE inquire_session SET filter_speakers = :new WHERE id = :id AND filter_speakers = :old'),
                {'new': item['new'], 'id': item['id'], 'old': item['old']}).rowcount
    return done


def summary_lines(counts, kept_speaker_ids=(), users_affected=None, dry_run=False):
    """The report as text lines: counts and ids, never a name."""
    verb = 'that would be removed' if dry_run else 'removed'
    first = f"Saved speakers named like a diarization label, {verb}: {counts['speakers_removed']}"
    if dry_run:
        first += (f" (of {users_affected} user(s); {counts['speakers_with_voice_profile']} "
                  f"with a voice profile)")
    lines = [
        first,
        f"Voice samples {verb} with them: {counts['voice_samples_removed']}",
        f"Speaker snippets {verb} with them: {counts['snippets_removed']}",
        f"Transcript segments unlinked from a removed speaker (the visible label stays): "
        f"{counts['segments_unlinked']} in {counts['recordings_unlinked']} recording(s)",
        f"Recordings whose participants lose a label: {counts['recordings_participants_cleaned']} "
        f"({counts['participant_labels_removed']} label(s))",
        f"Inquire chunks whose speaker name is cleared: {counts['chunks_cleared']}"
        + (f" (in {counts['chunk_recordings']} recording(s))" if dry_run else ''),
        f"Stored Ask sessions whose speaker filter loses a label: {counts['inquire_sessions_cleaned']}",
    ]
    if kept_speaker_ids:
        lines.append(
            f"Kept, because a person entered an email or aliases for them: {len(kept_speaker_ids)} "
            f"saved speaker(s), ids {sorted(kept_speaker_ids)}. Rename or delete them by id.")
    return lines


def remove_generic_speakers(engine, logger=None):
    """Plan and apply (run_once 0008). Returns the counts of what changed."""
    planned = plan(engine)
    done = apply(engine, planned)
    if logger and (any(done.values()) or planned['kept_speaker_ids']):
        logger.info(
            "Diarization labels removed from the speaker stores: %s saved speaker(s), %s voice sample(s), "
            "%s snippet(s); %s segment link(s) in %s recording(s); participants of %s recording(s); "
            "%s Inquire chunk(s); %s stored Ask filter(s)",
            done['speakers_removed'], done['voice_samples_removed'], done['snippets_removed'],
            done['segments_unlinked'], done['recordings_unlinked'], done['recordings_participants_cleaned'],
            done['chunks_cleared'], done['inquire_sessions_cleaned'])
        if planned['kept_speaker_ids']:
            logger.warning(
                "%s saved speaker(s) are named like a diarization label and were kept because they have an "
                "email or aliases (ids %s). Rename or delete them.",
                len(planned['kept_speaker_ids']), sorted(planned['kept_speaker_ids']))
    return done
