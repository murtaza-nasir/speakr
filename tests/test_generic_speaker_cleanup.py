"""The startup cleanup of diarization labels (run_once 0008).

Earlier releases could store a label such as SPEAKER_00 as a saved speaker,
a participant, the speaker name of an Inquire chunk or a stored Ask filter.
src/utils/generic_speaker_cleanup.py removes those once. The tests run
against the database of the suite: SQLite by default, PostgreSQL when
PYTEST_DATABASE_URI points at one.

SHARED-DB: assertions are scoped to the rows the test made; the plan of a
shared database may list rows of other tests, so counts are compared for the
test's own ids.
"""

import io
import json
import logging
import os
import sys
import threading
import uuid

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import InquireSession, Recording, Speaker, SpeakerVoiceSample, TranscriptChunk, User
from src.models.speaker_snippet import SpeakerSnippet
from src.utils.database import migration_lock, run_once
from src.utils.generic_speaker_cleanup import apply, plan, remove_generic_speakers, summary_lines


def _speaker(user, name, samples=0, snippets=0, recording=None, **fields):
    speaker = Speaker(name=name, user_id=user.id, **fields)
    db.session.add(speaker)
    db.session.commit()
    for n in range(samples):
        db.session.add(SpeakerVoiceSample(user_id=user.id, speaker_id=speaker.id, embedding=b'\0' * 16,
                                          dimension=4, recording_id=recording.id if recording else None,
                                          label=f'SPEAKER_{n:02d}'))
    for n in range(snippets):
        db.session.add(SpeakerSnippet(speaker_id=speaker.id, recording_id=recording.id,
                                      segment_index=n, text_snippet='a quote of some length'))
    db.session.commit()
    return speaker.id


@pytest.fixture
def world():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f'gsc_{s}', email=f'gsc_{s}@local.test', password='x')
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title='t', status='COMPLETED')
        plain = Recording(user_id=user.id, title='plain', status='COMPLETED',
                          transcription='[SPEAKER_00]: hello', participants='Dana Lee;  Kim')
        untouched = Recording(user_id=user.id, title='clean', status='COMPLETED', participants='Dana Lee, Kim')
        db.session.add_all([rec, plain, untouched])
        db.session.commit()

        ids = {
            'user': user.id, 'rec': rec.id, 'plain': plain.id, 'untouched': untouched.id,
            'label': _speaker(user, 'SPEAKER_00', samples=2, snippets=2, recording=rec, average_embedding=b'\1' * 16),
            'label2': _speaker(user, 'Speaker 1', samples=1, recording=rec),
            'unknown': _speaker(user, 'Unknown Speaker'),
            'real': _speaker(user, 'Dana Lee', samples=1, snippets=1, recording=rec),
            'role': _speaker(user, 'Guest Speaker'),
            'contact': _speaker(user, 'SPEAKER_05', samples=1, recording=rec, email='someone@local.test'),
            'aliases': _speaker(user, 'spk_2', aliases=json.dumps(['Sam'])),
        }
        rec.transcription = json.dumps([
            {'speaker': 'SPEAKER_00', 'sentence': 'one', 'start_time': 0.0, 'end_time': 1.0, 'speaker_id': ids['label']},
            {'speaker': 'Dana Lee', 'sentence': 'two', 'start_time': 1.0, 'end_time': 2.0, 'speaker_id': ids['real']},
            {'speaker': 'Speaker 1', 'sentence': 'three', 'start_time': 2.0, 'end_time': 3.0, 'speaker_id': ids['label2']},
            {'speaker': 'SPEAKER_05', 'sentence': 'four', 'start_time': 3.0, 'end_time': 4.0, 'speaker_id': ids['contact']},
            {'speaker': 'SPEAKER_03', 'sentence': 'five', 'start_time': 4.0, 'end_time': 5.0},
        ])
        rec.participants = 'Dana Lee, SPEAKER_01, UNKNOWN_SPEAKER, Kim, Speaker 2'
        db.session.add_all([
            TranscriptChunk(recording_id=rec.id, user_id=user.id, chunk_index=0, content='SPEAKER_00: one',
                            speaker_name='SPEAKER_00'),
            TranscriptChunk(recording_id=rec.id, user_id=user.id, chunk_index=1, content='Dana Lee: two',
                            speaker_name='Dana Lee'),
            TranscriptChunk(recording_id=rec.id, user_id=user.id, chunk_index=2, content='UNKNOWN_SPEAKER: x',
                            speaker_name='UNKNOWN_SPEAKER'),
            TranscriptChunk(recording_id=rec.id, user_id=user.id, chunk_index=3, content='no speaker'),
        ])
        session = InquireSession(user_id=user.id, filter_speakers=json.dumps(['SPEAKER_00', 'Dana Lee']))
        keep = InquireSession(user_id=user.id, filter_speakers=json.dumps(['Dana Lee']))
        db.session.add_all([session, keep])
        db.session.commit()
        ids['session'], ids['keep_session'] = session.id, keep.id
        ids['updated_at'] = db.session.get(Recording, rec.id).updated_at
        ids['untouched_updated_at'] = db.session.get(Recording, untouched.id).updated_at
        db.session.remove()
        yield ids
        db.session.rollback()
        recs = [ids['rec'], ids['plain'], ids['untouched']]
        TranscriptChunk.query.filter(TranscriptChunk.recording_id.in_(recs)).delete(synchronize_session=False)
        SpeakerSnippet.query.filter(SpeakerSnippet.recording_id.in_(recs)).delete(synchronize_session=False)
        SpeakerVoiceSample.query.filter_by(user_id=ids['user']).delete(synchronize_session=False)
        Speaker.query.filter_by(user_id=ids['user']).delete(synchronize_session=False)
        InquireSession.query.filter_by(user_id=ids['user']).delete(synchronize_session=False)
        Recording.query.filter(Recording.id.in_(recs)).delete(synchronize_session=False)
        User.query.filter_by(id=ids['user']).delete(synchronize_session=False)
        db.session.commit()
        with db.engine.begin() as conn:
            conn.execute(text("DELETE FROM schema_migrations WHERE migration_id LIKE 'test_generic_speakers_%'"))


def _mine(planned, ids):
    """The part of a plan that concerns this test's rows."""
    return {
        'speakers': sorted(s['id'] for s in planned['speakers']
                           if s['id'] in (ids['label'], ids['label2'], ids['unknown'], ids['real'], ids['role'],
                                          ids['contact'], ids['aliases'])),
        'kept': sorted(i for i in planned['kept_speaker_ids'] if i in (ids['contact'], ids['aliases'])),
        'transcripts': [i for i in planned['transcripts'] if i in (ids['rec'], ids['plain'], ids['untouched'])],
        'participants': [p['id'] for p in planned['participants'] if p['id'] in (ids['rec'], ids['plain'], ids['untouched'])],
        'sessions': [s['id'] for s in planned['sessions'] if s['id'] in (ids['session'], ids['keep_session'])],
    }


def _check_cleaned(ids):
    db.session.expire_all()
    # The label rows are gone with their samples and snippets.
    for key in ('label', 'label2', 'unknown'):
        assert db.session.get(Speaker, ids[key]) is None
        assert SpeakerVoiceSample.query.filter_by(speaker_id=ids[key]).count() == 0
        assert SpeakerSnippet.query.filter_by(speaker_id=ids[key]).count() == 0
    # Real speakers are untouched, a name that only contains "Speaker" too.
    assert db.session.get(Speaker, ids['real']).name == 'Dana Lee'
    assert SpeakerVoiceSample.query.filter_by(speaker_id=ids['real']).count() == 1
    assert SpeakerSnippet.query.filter_by(speaker_id=ids['real']).count() == 1
    assert db.session.get(Speaker, ids['role']).name == 'Guest Speaker'
    # Rows a person gave contact details are kept as they are.
    assert db.session.get(Speaker, ids['contact']).email == 'someone@local.test'
    assert SpeakerVoiceSample.query.filter_by(speaker_id=ids['contact']).count() == 1
    assert db.session.get(Speaker, ids['aliases']) is not None

    # Segment links to removed rows are cleared; the visible label, the text
    # and every other link are as they were.
    rec = db.session.get(Recording, ids['rec'])
    segments = json.loads(rec.transcription)
    assert [s['speaker'] for s in segments] == ['SPEAKER_00', 'Dana Lee', 'Speaker 1', 'SPEAKER_05', 'SPEAKER_03']
    assert [s.get('speaker_id') for s in segments] == [None, ids['real'], None, ids['contact'], None]
    assert 'speaker_id' not in segments[0] and 'speaker_id' not in segments[2]
    assert [s['sentence'] for s in segments] == ['one', 'two', 'three', 'four', 'five']
    assert [s['start_time'] for s in segments] == [0.0, 1.0, 2.0, 3.0, 4.0]

    # Participants hold people only; a field without a label is not rewritten.
    assert rec.participants == 'Dana Lee, Kim'
    assert rec.updated_at is not None and rec.updated_at != ids['updated_at']
    assert db.session.get(Recording, ids['plain']).participants == 'Dana Lee;  Kim'
    assert db.session.get(Recording, ids['plain']).transcription == '[SPEAKER_00]: hello'
    untouched = db.session.get(Recording, ids['untouched'])
    assert untouched.participants == 'Dana Lee, Kim' and untouched.updated_at == ids['untouched_updated_at']

    # Inquire chunks: the speaker name is cleared, the text keeps the label.
    chunks = TranscriptChunk.query.filter_by(recording_id=ids['rec']).order_by(TranscriptChunk.chunk_index).all()
    assert [c.speaker_name for c in chunks] == [None, 'Dana Lee', None, None]
    assert chunks[0].content == 'SPEAKER_00: one'

    assert json.loads(db.session.get(InquireSession, ids['session']).filter_speakers) == ['Dana Lee']
    assert json.loads(db.session.get(InquireSession, ids['keep_session']).filter_speakers) == ['Dana Lee']


def test_the_plan_reports_and_changes_nothing(world):
    with app.app_context():
        before = db.session.get(Recording, world['rec']).transcription
        planned = plan(db.engine)
        mine = _mine(planned, world)
        assert mine['speakers'] == sorted([world['label'], world['label2'], world['unknown']])
        assert mine['kept'] == sorted([world['contact'], world['aliases']])
        assert mine['transcripts'] == [world['rec']]
        assert mine['participants'] == [world['rec']]
        assert mine['sessions'] == [world['session']]
        counts = planned['counts']
        assert counts['speakers_removed'] >= 3 and counts['speakers_with_voice_profile'] >= 1
        assert counts['voice_samples_removed'] >= 3 and counts['snippets_removed'] >= 2
        assert counts['segments_unlinked'] >= 2 and counts['participant_labels_removed'] >= 3
        assert counts['chunks_cleared'] >= 2 and counts['chunk_recordings'] >= 1
        assert planned['users_affected'] >= 1

        db.session.expire_all()
        assert db.session.get(Speaker, world['label']) is not None
        assert SpeakerVoiceSample.query.filter_by(speaker_id=world['label']).count() == 2
        assert db.session.get(Recording, world['rec']).transcription == before
        assert db.session.get(Recording, world['rec']).participants.count('SPEAKER') == 2


def test_the_report_holds_counts_and_ids_only(world):
    with app.app_context():
        planned = plan(db.engine)
        report = '\n'.join(summary_lines(planned['counts'], planned['kept_speaker_ids'],
                                         users_affected=planned['users_affected'], dry_run=True))
        for name in ('Dana', 'Kim', 'SPEAKER_', 'Speaker 1', 'someone@', 'Guest'):
            assert name not in report
        assert str(world['contact']) in report

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger('test_generic_speaker_cleanup')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            remove_generic_speakers(db.engine, logger=logger)
        finally:
            logger.removeHandler(handler)
        logged = stream.getvalue()
        assert 'saved speaker(s)' in logged and str(world['contact']) in logged
        for name in ('Dana', 'Kim', 'SPEAKER_', 'Speaker 1', 'someone@', 'Guest', 'Unknown'):
            assert name not in logged
        _check_cleaned(world)


def test_cleanup_removes_labels_and_keeps_people(world):
    with app.app_context():
        done = remove_generic_speakers(db.engine)
        assert done['speakers_removed'] >= 3 and done['voice_samples_removed'] >= 3
        assert done['snippets_removed'] >= 2 and done['segments_unlinked'] >= 2
        assert done['recordings_participants_cleaned'] >= 1 and done['chunks_cleared'] >= 2
        assert done['inquire_sessions_cleaned'] >= 1
        _check_cleaned(world)


def test_cleanup_is_idempotent(world):
    with app.app_context():
        remove_generic_speakers(db.engine)
        _check_cleaned(world)
        state = (db.session.get(Recording, world['rec']).transcription,
                 db.session.get(Recording, world['rec']).updated_at)
        db.session.remove()

        again = plan(db.engine)
        assert _mine(again, world) == {'speakers': [], 'kept': sorted([world['contact'], world['aliases']]),
                                       'transcripts': [], 'participants': [], 'sessions': []}
        done = remove_generic_speakers(db.engine)
        _check_cleaned(world)
        assert (db.session.get(Recording, world['rec']).transcription,
                db.session.get(Recording, world['rec']).updated_at) == state
        if not _others_present(again, world):
            assert not any(done.values())


def _others_present(planned, ids):
    """True when the shared database holds label rows of other tests."""
    return bool(planned['speakers'] or planned['participants'] or planned['chunk_names'] or planned['sessions'])


def test_two_workers_with_the_same_plan_change_each_row_once(world):
    """Several workers start at once and the lock has failed open: both read
    the same state, then both write."""
    with app.app_context():
        first, second = plan(db.engine), plan(db.engine)
        done_first = apply(db.engine, first)
        done_second = apply(db.engine, second)
        assert done_first['speakers_removed'] >= 3
        # The second worker finds every row already handled.
        assert done_second['speakers_removed'] == 0
        assert done_second['voice_samples_removed'] == 0 and done_second['snippets_removed'] == 0
        assert done_second['recordings_unlinked'] == 0 and done_second['segments_unlinked'] == 0
        assert done_second['recordings_participants_cleaned'] == 0
        assert done_second['chunks_cleared'] == 0 and done_second['inquire_sessions_cleaned'] == 0
        _check_cleaned(world)


def test_a_change_made_after_the_plan_is_left_alone(world):
    """A person renames the row and edits the transcript between the read and
    the write of the cleanup: their change wins."""
    with app.app_context():
        planned = plan(db.engine)
        edited = json.dumps([{'speaker': 'Kim', 'sentence': 'rewritten', 'start_time': 0.0, 'end_time': 1.0}])
        with db.engine.begin() as conn:
            conn.execute(text('UPDATE speaker SET name = :n WHERE id = :id'), {'n': 'Robin', 'id': world['label']})
            conn.execute(text('UPDATE recording SET transcription = :t, participants = :p WHERE id = :id'),
                         {'t': edited, 'p': 'Kim', 'id': world['rec']})
        apply(db.engine, planned)
        db.session.expire_all()
        renamed = db.session.get(Speaker, world['label'])
        assert renamed is not None and renamed.name == 'Robin'
        assert SpeakerVoiceSample.query.filter_by(speaker_id=world['label']).count() == 2
        rec = db.session.get(Recording, world['rec'])
        assert rec.transcription == edited and rec.participants == 'Kim'
        # The rows nobody touched were still cleaned.
        assert db.session.get(Speaker, world['label2']) is None


def test_concurrent_start_runs_the_cleanup_once(world):
    """Workers starting together: the migration lock and the ledger let one
    of them do the work; the others find it done."""
    migration_id = f'test_generic_speakers_{uuid.uuid4().hex[:8]}'
    runs, errors, results = [], [], []
    with app.app_context():
        engine = db.engine

        def cleanup(eng):
            runs.append(remove_generic_speakers(eng))

        def worker():
            try:
                with migration_lock(engine, timeout=60):
                    results.append(run_once(engine, migration_id, cleanup))
            except Exception as e:  # pragma: no cover - reported below
                errors.append(repr(e))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        assert errors == []
        assert sorted(results) == [False, False, False, True]
        assert len(runs) == 1 and runs[0]['speakers_removed'] >= 3
        with engine.connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM schema_migrations WHERE migration_id = :id'),
                                {'id': migration_id}).scalar() == 1
        _check_cleaned(world)


def test_the_startup_migration_is_registered():
    """init_db runs the cleanup through run_once, inside a migration section."""
    import inspect
    import src.init_db as init_db
    source = inspect.getsource(init_db)
    assert "run_once(engine, '0008_remove_generic_speaker_labels'" in source
    assert source.count("'0008_") == 1
    with app.app_context():
        with db.engine.connect() as conn:
            applied = conn.execute(text(
                "SELECT COUNT(*) FROM schema_migrations WHERE migration_id = '0008_remove_generic_speaker_labels'"
            )).scalar()
        assert applied == 1


def test_the_dry_run_script_reads_a_sqlite_copy_without_writing(tmp_path, capsys):
    """scripts/generic_speakers_dry_run.py against a file: counts printed, file unchanged."""
    import hashlib
    import importlib.util
    import sqlite3
    path = tmp_path / 'copy.db'
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE speaker (id INTEGER PRIMARY KEY, name TEXT, user_id INTEGER, average_embedding BLOB,
                              email TEXT, aliases TEXT);
        CREATE TABLE speaker_voice_sample (id INTEGER PRIMARY KEY, speaker_id INTEGER);
        CREATE TABLE speaker_snippet (id INTEGER PRIMARY KEY, speaker_id INTEGER);
        CREATE TABLE recording (id INTEGER PRIMARY KEY, user_id INTEGER, participants TEXT, transcription TEXT,
                                updated_at TEXT);
        CREATE TABLE transcript_chunk (id INTEGER PRIMARY KEY, recording_id INTEGER, speaker_name TEXT);
        INSERT INTO speaker VALUES (1, 'SPEAKER_00', 1, NULL, NULL, NULL), (2, 'Dana Lee', 1, NULL, NULL, NULL),
                                   (3, 'Speaker 2', 2, x'00', NULL, '[]'), (4, 'spk_1', 2, NULL, 'a@local.test', NULL);
        INSERT INTO speaker_voice_sample VALUES (1, 1), (2, 1), (3, 2);
        INSERT INTO speaker_snippet VALUES (1, 3), (2, 2);
        INSERT INTO recording VALUES
            (1, 1, 'Dana Lee, SPEAKER_01', '[{"speaker": "SPEAKER_00", "sentence": "x", "speaker_id": 1}]', NULL),
            (2, 1, 'Dana Lee', '[{"speaker": "Dana Lee", "sentence": "y", "speaker_id": 2}]', NULL),
            (3, 3, NULL, '[{"speaker": "SPEAKER_00", "sentence": "z", "speaker_id": 1}]', NULL);
        INSERT INTO transcript_chunk VALUES (1, 1, 'SPEAKER_00'), (2, 1, 'SPEAKER_00'), (3, 2, 'Dana Lee');
    ''')
    conn.commit()
    conn.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location('generic_speakers_dry_run',
                                                  os.path.join(root, 'scripts', 'generic_speakers_dry_run.py'))
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main(['--database-uri', f'sqlite:///{path}']) == 0
    out = capsys.readouterr().out
    assert 'Nothing was changed' in out
    assert 'would be removed: 2 (of 2 user(s); 1 with a voice profile)' in out
    assert 'Voice samples that would be removed with them: 2' in out
    assert 'Speaker snippets that would be removed with them: 1' in out
    assert '1 in 1 recording(s)' in out
    assert 'participants lose a label: 1 (1 label(s))' in out
    assert 'speaker name is cleared: 2 (in 1 recording(s))' in out
    assert 'ids [4]' in out
    for name in ('Dana', 'SPEAKER_', 'Speaker 2', 'spk_1', 'a@local.test'):
        assert name not in out
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    # The engine of the script cannot write to the file.
    engine = script.read_only_engine(f'sqlite:///{path}')
    with pytest.raises(Exception):
        with engine.begin() as c:
            c.execute(text("DELETE FROM speaker"))
    engine.dispose()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
