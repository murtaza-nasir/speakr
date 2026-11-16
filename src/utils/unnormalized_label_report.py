"""Count stored transcripts whose diarization labels are not SPEAKER_NN.

Since labels are normalized at ingestion (diarization_label_map in
src/utils/speaker_labels.py), a new transcript holds SPEAKER_00, SPEAKER_01
for its unnamed voices. Transcripts stored before that keep what the service
returned: ``A`` and ``B`` (AssemblyAI, OpenAI and Azure OpenAI diarization),
``S01`` (Mossland), ``SPEAKER_0`` (FunASR), ``speaker_0``. Nothing rewrites
them: in a stored transcript a single letter or a number may be a name that
a person typed, and the two cannot be told apart from the text alone.

This module only reads. ``report()`` counts the recordings concerned and how
each is treated today, as evidence for a decision about them:

- by scheme (raw_label_scheme): letter, number, prefixed_number,
  generic_number. One recording can count under several schemes;
- ``only_labels``: every speaker value of the transcript is a label of some
  kind, none is a name. Such a transcript looks as the service left it;
- ``in_participants``: one of the labels is listed in
  ``recording.participants``, the field the speaker filter of the recording
  list matches;
- ``chunk_speaker``: one of the labels is the ``speaker_name`` of an Inquire
  chunk of the recording, so the label is offered and matched as a speaker
  in Ask;
- ``saved_speaker``: the owner has a saved speaker (speaker library) whose
  name is one of the labels, ignoring case;
- ``linked_segments``: a segment with such a label carries a ``speaker_id``;
- ``embedding_keys``: ``recording.speaker_embeddings`` is keyed by one of
  the labels.

Letters, numbers and prefixed numbers are names under the rule of
speaker_labels.py, so the last five counts can be above zero for them. A
generic_number label (``SPEAKER_0``, ``Speaker 1``) is already kept out of
those stores; only its form differs. ``Speaker 1`` is also the default name
of the segment editor, so that scheme includes labels a person accepted.

The transcription service is not stored with a recording, so the counts are
by scheme, not by connector. Plain SQL through the engine, no models.
Nothing here returns a label or a name, only counts and ids.
"""

import json

from sqlalchemy import inspect, text

from src.utils.speaker_labels import is_real_speaker_name, raw_label_scheme

BATCH = 200
SCHEMES = ('letter', 'number', 'prefixed_number', 'generic_number')
FACTS = ('recordings', 'only_labels', 'in_participants', 'chunk_speaker', 'saved_speaker',
         'linked_segments', 'embedding_keys')
# Printed by the dry run, whose output holds no label: described in words.
TYPICAL_SOURCE = {
    'letter': 'one letter: AssemblyAI, OpenAI and Azure OpenAI diarization',
    'number': 'a number alone',
    'prefixed_number': 'a short prefix and a number: Mossland',
    'generic_number': 'a speaker word and a number in another form: FunASR, Mistral, the segment editor default',
}


def _segments(transcription):
    try:
        data = json.loads(transcription)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _lower(value):
    return value.strip().lower() if isinstance(value, str) else None


def report(engine):
    """{'scanned': n, 'schemes': {scheme: {fact: count}}, 'ids': {scheme: [recording ids]}}.

    `scanned` is the number of recordings with a JSON transcript. Reads only.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    out = {'scanned': 0, 'schemes': {s: dict.fromkeys(FACTS, 0) for s in SCHEMES},
           'ids': {s: [] for s in SCHEMES}}
    if 'recording' not in tables:
        return out
    columns = {c['name'] for c in inspector.get_columns('recording')}
    with_embeddings = 'speaker_embeddings' in columns
    select = ('SELECT id, user_id, transcription, participants'
              + (', speaker_embeddings' if with_embeddings else ', NULL')
              + ' FROM recording WHERE id > :last AND transcription LIKE :json ORDER BY id LIMIT :n')

    with engine.connect() as conn:
        saved = {}   # user id -> lowercased names of the saved speakers
        if 'speaker' in tables:
            for user_id, name in conn.execute(text('SELECT user_id, name FROM speaker')):
                if _lower(name):
                    saved.setdefault(user_id, set()).add(_lower(name))

        last_id = 0
        while True:
            rows = conn.execute(text(select), {'last': last_id, 'json': '[%', 'n': BATCH}).fetchall()
            if not rows:
                break
            for rec_id, user_id, transcription, participants, embeddings in rows:
                last_id = rec_id
                segments = _segments(transcription)
                if segments is None:
                    continue
                out['scanned'] += 1
                by_scheme, linked, has_name = {}, set(), False
                for segment in segments:
                    label = segment.get('speaker') if isinstance(segment, dict) else None
                    scheme = raw_label_scheme(label)[0]
                    if scheme in SCHEMES:
                        by_scheme.setdefault(scheme, set()).add(str(label))
                        if segment.get('speaker_id') is not None:
                            linked.add(scheme)
                    elif scheme is None and is_real_speaker_name(label):
                        has_name = True
                if not by_scheme:
                    continue

                listed = {_lower(p) for p in (participants or '').split(',')} - {None, ''}
                chunk_names = set()
                if 'transcript_chunk' in tables:
                    chunk_names = {_lower(n) for n, in conn.execute(text(
                        'SELECT DISTINCT speaker_name FROM transcript_chunk '
                        'WHERE recording_id = :id AND speaker_name IS NOT NULL'), {'id': rec_id})}
                if isinstance(embeddings, str):
                    try:
                        embeddings = json.loads(embeddings)
                    except ValueError:
                        embeddings = None
                keys = {_lower(k) for k in embeddings} if isinstance(embeddings, dict) else set()

                for scheme, labels in by_scheme.items():
                    lowered = {_lower(label) for label in labels}
                    counts = out['schemes'][scheme]
                    counts['recordings'] += 1
                    counts['only_labels'] += 0 if has_name else 1
                    counts['in_participants'] += 1 if lowered & listed else 0
                    counts['chunk_speaker'] += 1 if lowered & chunk_names else 0
                    counts['saved_speaker'] += 1 if lowered & saved.get(user_id, set()) else 0
                    counts['linked_segments'] += 1 if scheme in linked else 0
                    counts['embedding_keys'] += 1 if lowered & keys else 0
                    out['ids'][scheme].append(rec_id)
    return out


def summary_lines(result, with_ids=False):
    """The report as text lines: counts and ids, never a label or a name."""
    lines = [f"Recordings with a segment transcript: {result['scanned']}"]
    for scheme in SCHEMES:
        c = result['schemes'][scheme]
        lines.append(f"{scheme} ({TYPICAL_SOURCE[scheme]}): {c['recordings']} recording(s)")
        if not c['recordings']:
            continue
        lines += [
            f"    with no named speaker at all (as the service left them): {c['only_labels']}",
            f"    label listed in participants (matched by the speaker filter): {c['in_participants']}",
            f"    label stored as the speaker of an Inquire chunk (offered in Ask): {c['chunk_speaker']}",
            f"    label is a saved speaker of the owner (speaker library): {c['saved_speaker']}",
            f"    label on a segment linked to a saved speaker: {c['linked_segments']}",
            f"    label is a key of the stored voice embeddings: {c['embedding_keys']}",
        ]
        if with_ids:
            lines.append(f"    recording ids: {result['ids'][scheme]}")
    return lines
