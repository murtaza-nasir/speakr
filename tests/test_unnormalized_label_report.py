"""The count of stored transcripts whose labels are not SPEAKER_NN.

src/utils/unnormalized_label_report.py only reads. The tests build a small
SQLite file of their own, so the counts are exact, and check that the report
and the dry-run script leave the file as it was.
"""

import hashlib
import json
import os
import sys

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.unnormalized_label_report import report, summary_lines


def _transcript(*speakers, speaker_id=None):
    segments = [{'speaker': s, 'sentence': 'words', 'start_time': float(n), 'end_time': n + 1.0}
                for n, s in enumerate(speakers)]
    if speaker_id is not None:
        segments[0]['speaker_id'] = speaker_id
    return json.dumps(segments)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'labels.db'
    engine = create_engine(f'sqlite:///{path}')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE recording (id INTEGER PRIMARY KEY, user_id INTEGER, transcription TEXT, '
                          'participants TEXT, speaker_embeddings JSON)'))
        conn.execute(text('CREATE TABLE speaker (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, average_embedding BLOB)'))
        conn.execute(text('CREATE TABLE transcript_chunk (id INTEGER PRIMARY KEY, recording_id INTEGER, '
                          'speaker_name TEXT)'))
        rows = [
            # 1: as AssemblyAI left it, nothing derived from it
            (1, 1, _transcript('A', 'B', 'A'), None, None),
            # 2: one voice named, the other still a letter that reached every store
            (2, 1, _transcript('Dana Lee', 'B', speaker_id=None), 'B, Dana Lee', json.dumps({'A': [0.1], 'B': [0.2]})),
            # 3: normalized already
            (3, 1, _transcript('SPEAKER_00', 'SPEAKER_01', 'Unknown Speaker'), None, None),
            # 4: FunASR form and a Mossland form in one transcript
            (4, 2, _transcript('SPEAKER_0', 'S02'), None, None),
            # 5: names only; 6: plain text; 7: an error card
            (5, 2, _transcript('Dana Lee', 'Kim'), 'Dana Lee, Kim', None),
            (6, 2, '[A]: plain text, not segments', None, None),
            (7, 2, 'ERROR_JSON:{"t": 1}', None, None),
            # 8: a bare number, linked to a saved speaker of another spelling
            (8, 2, _transcript('1', 'Kim', speaker_id=5), None, None),
        ]
        for row in rows:
            conn.execute(text('INSERT INTO recording VALUES (:a, :b, :c, :d, :e)'),
                         dict(zip('abcde', row)))
        conn.execute(text("INSERT INTO speaker VALUES (1, 1, 'b', NULL), (2, 2, 'B', NULL), (5, 2, '1', NULL)"))
        conn.execute(text("INSERT INTO transcript_chunk VALUES (1, 2, 'B'), (2, 2, 'Dana Lee'), (3, 1, NULL)"))
    engine.dispose()
    return path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_counts_by_scheme_and_by_present_treatment(database):
    before = _digest(database)
    engine = create_engine(f'sqlite:///{database}')
    try:
        result = report(engine)
    finally:
        engine.dispose()
    assert _digest(database) == before
    assert result['scanned'] == 6          # 6 and 7 are not segment transcripts
    assert result['ids'] == {'letter': [1, 2], 'number': [8], 'prefixed_number': [4], 'generic_number': [4]}
    assert result['schemes']['letter'] == {
        'recordings': 2, 'only_labels': 1, 'in_participants': 1, 'chunk_speaker': 1,
        'saved_speaker': 2, 'linked_segments': 0, 'embedding_keys': 1}
    assert result['schemes']['number'] == {
        'recordings': 1, 'only_labels': 0, 'in_participants': 0, 'chunk_speaker': 0,
        'saved_speaker': 1, 'linked_segments': 1, 'embedding_keys': 0}
    assert result['schemes']['prefixed_number']['recordings'] == 1
    assert result['schemes']['generic_number'] == {
        'recordings': 1, 'only_labels': 1, 'in_participants': 0, 'chunk_speaker': 0,
        'saved_speaker': 0, 'linked_segments': 0, 'embedding_keys': 0}


def test_summary_holds_counts_and_ids_but_no_label_or_name(database):
    engine = create_engine(f'sqlite:///{database}')
    try:
        result = report(engine)
    finally:
        engine.dispose()
    lines = summary_lines(result, with_ids=True)
    assert lines[0] == 'Recordings with a segment transcript: 6'
    assert any(line.startswith('letter (') and line.endswith(': 2 recording(s)') for line in lines)
    assert '    recording ids: [1, 2]' in lines
    assert not any('Dana' in line or 'Kim' in line for line in lines)
    assert not any('recording ids' in line for line in summary_lines(result))


def test_an_empty_database_is_reported_as_zero(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "empty.db"}')
    try:
        result = report(engine)
    finally:
        engine.dispose()
    assert result['scanned'] == 0 and all(c['recordings'] == 0 for c in result['schemes'].values())


def test_the_dry_run_script_prints_the_second_part_without_writing(database, capsys):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
    import generic_speakers_dry_run
    before = _digest(database)
    assert generic_speakers_dry_run.main(['--database-uri', f'sqlite:///{database}', '--ids']) == 0
    out = capsys.readouterr().out
    assert 'Stored transcripts with diarization labels that are not in the normalized form. Nothing was changed.' in out
    assert 'SPEAKER_' not in out
    assert 'recording ids: [1, 2]' in out
    assert _digest(database) == before
