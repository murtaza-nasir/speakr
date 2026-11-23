"""Review fixes for the translation feature (#538): chunk size, save errors,
job retry, webhook contract, and translations that follow their transcript."""
import json
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError

from src.app import app, db
from src.api import auth as auth_mod
from src.models import ProcessingJob, Recording, User
from src.services import recording_changes
from src.services import transcription_translation as tt


@pytest.fixture(autouse=True)
def _disable_csrf():
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    yield
    app.config['WTF_CSRF_ENABLED'] = prev


@pytest.fixture
def user():
    with app.app_context():
        u = User(username="u_" + uuid.uuid4().hex[:14], email=uuid.uuid4().hex[:10] + "@example.com",
                 password=auth_mod.bcrypt.generate_password_hash("Sup3rSecret!").decode('utf-8'))
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        ProcessingJob.query.filter_by(user_id=uid).delete()
        Recording.query.filter_by(user_id=uid).delete()
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _segments(*rows):
    return json.dumps([{"speaker": s, "start_time": a, "end_time": b, "sentence": t} for s, a, b, t in rows])


ORIGINAL = _segments(("SPEAKER_00", 0.0, 2.0, "Hello there"), ("SPEAKER_01", 2.0, 4.0, "Good morning"))
SPANISH = _segments(("SPEAKER_00", 0.0, 2.0, "Hola"), ("SPEAKER_01", 2.0, 4.0, "Buenos días"))


def _recording(uid, **extra):
    rec = Recording(user_id=uid, status='COMPLETED', transcription=ORIGINAL,
                    transcription_translation=SPANISH, transcription_translation_language='Spanish',
                    translation_duration_seconds=3, **extra)
    db.session.add(rec)
    db.session.commit()
    return rec


def _echo(calls):
    def fake_call(messages, user_id):
        content = messages[-1]['content']
        payload = json.loads(content[content.find('{'):content.rfind('}') + 1])
        calls.append(payload)
        return json.dumps({k: v + " [es]" for k, v in payload.items()}, ensure_ascii=False)
    return fake_call


# 1. Chunk size -------------------------------------------------------------

def test_no_limit_setting_still_sends_one_call_for_a_short_transcript(monkeypatch):
    """transcript_length_limit=-1 ("no limit") used to make every segment its
    own call; the chunk size no longer depends on that setting."""
    calls = []
    monkeypatch.setattr(tt, '_call_llm', _echo(calls))
    text = json.dumps([{"speaker": "A", "sentence": f"Sentence number {i}."} for i in range(20)])

    def setting(key, default=None):
        # The length limit is what this test is about; every other setting,
        # such as the prefix-cache flags, must keep its default.
        return -1 if key == 'transcript_length_limit' else default

    with app.app_context(), patch('src.models.SystemSetting.get_setting', side_effect=setting):
        out = json.loads(tt.translate_transcription_text(text, target_language="Spanish"))
    assert len(calls) == 1
    assert len(out) == 20


def test_a_long_transcript_is_split_into_output_sized_chunks(monkeypatch):
    calls = []
    monkeypatch.setattr(tt, '_call_llm', _echo(calls))
    sentence = "x" * 190
    text = json.dumps([{"speaker": "A", "sentence": sentence} for _ in range(100)])
    with app.app_context():
        out = json.loads(tt.translate_transcription_text(text, target_language="Spanish"))
    assert len(out) == 100
    assert len(calls) > 1
    assert all(sum(len(v) for v in c.values()) <= tt.TRANSLATION_CHUNK_CHARS for c in calls)



def test_a_chunk_holds_at_most_the_segment_cap(monkeypatch):
    calls = []
    monkeypatch.setattr(tt, '_call_llm', _echo(calls))
    text = json.dumps([{"speaker": "A", "sentence": f"Short {i}."} for i in range(60)])
    with app.app_context():
        out = json.loads(tt.translate_transcription_text(text, target_language="Spanish"))
    assert [len(c) for c in calls] == [25, 25, 10]
    assert [o["sentence"] for o in out] == [f"Short {i}. [es]" for i in range(60)]


def test_a_dropped_key_splits_the_chunk_and_keeps_each_sentence_in_place(monkeypatch):
    """A model that skips a sentence shifts every later translation up one key;
    the response is discarded and each half is sent again."""
    calls = []

    def skipping_model(messages, user_id):
        content = messages[-1]['content']
        payload = json.loads(content[content.find('{'):content.rfind('}') + 1])
        calls.append(list(payload))
        keys = list(payload)
        if len(keys) > 4:
            # Skip the third sentence and move the rest up one key.
            values = [payload[k] for k in keys if k != keys[2]]
            return json.dumps({k: v + " [es]" for k, v in zip(keys, values)})
        return json.dumps({k: v + " [es]" for k, v in payload.items()})

    monkeypatch.setattr(tt, '_call_llm', skipping_model)
    text = json.dumps([{"speaker": f"S{i}", "start_time": i, "sentence": f"Line {i}."} for i in range(10)])
    with app.app_context():
        out = json.loads(tt.translate_transcription_text(text, target_language="Spanish"))
    assert [o["sentence"] for o in out] == [f"Line {i}. [es]" for i in range(10)]
    assert [o["speaker"] for o in out] == [f"S{i}" for i in range(10)]
    assert len(calls[0]) == 10 and len(calls) > 1

# 2. A failed save reaches the job -------------------------------------------

def test_a_failed_commit_is_raised(monkeypatch, user):
    monkeypatch.setattr(tt, '_call_llm', _echo([]))
    with app.app_context():
        rec = Recording(user_id=user, status='COMPLETED', transcription=ORIGINAL)
        db.session.add(rec)
        db.session.commit()
        with patch.object(db.session, 'commit', side_effect=RuntimeError("disk full")):
            with pytest.raises(RuntimeError):
                tt.translate_transcription(rec, {'user_id': user, 'target_language': 'Spanish'})
        db.session.rollback()


# 3. Retry when a newer job of the same type is active ------------------------

def _login(client, uid):
    with client.session_transaction() as s:
        s['_user_id'] = str(uid)
        s['_fresh'] = True


def test_retry_is_refused_while_a_job_of_the_same_type_is_active(user):
    with app.app_context():
        rec = _recording(user)
        failed = ProcessingJob(user_id=user, recording_id=rec.id, job_type='reprocess_summary', status='failed')
        active = ProcessingJob(user_id=user, recording_id=rec.id, job_type='reprocess_summary', status='queued')
        db.session.add_all([failed, active])
        db.session.commit()
        failed_id = failed.id
    client = app.test_client()
    _login(client, user)
    resp = client.post(f'/api/recordings/jobs/{failed_id}/retry')
    assert resp.status_code == 409
    assert resp.get_json()['code'] == 'recording_already_processing'
    with app.app_context():
        assert db.session.get(ProcessingJob, failed_id).status == 'failed'


def test_retry_still_works_without_an_active_job(user):
    with app.app_context():
        rec = _recording(user)
        failed = ProcessingJob(user_id=user, recording_id=rec.id, job_type='reprocess_summary', status='failed')
        db.session.add(failed)
        db.session.commit()
        failed_id = failed.id
    client = app.test_client()
    _login(client, user)
    with patch('src.services.job_queue.job_queue.start'):
        resp = client.post(f'/api/recordings/jobs/{failed_id}/retry')
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(ProcessingJob, failed_id).status == 'queued'


def test_the_database_allows_one_active_job_per_recording_and_type(user):
    """The partial unique index exists on SQLite and PostgreSQL alike."""
    with app.app_context():
        rec = _recording(user)
        db.session.add(ProcessingJob(user_id=user, recording_id=rec.id, job_type='summarize', status='queued'))
        db.session.commit()
        db.session.add(ProcessingJob(user_id=user, recording_id=rec.id, job_type='summarize', status='processing'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        # Finished jobs and other job types are not restricted.
        db.session.add_all([
            ProcessingJob(user_id=user, recording_id=rec.id, job_type='summarize', status='completed'),
            ProcessingJob(user_id=user, recording_id=rec.id, job_type='summarize', status='failed'),
            ProcessingJob(user_id=user, recording_id=rec.id, job_type='translate_transcription', status='queued'),
        ])
        db.session.commit()


# 4. Webhook contract ------------------------------------------------------

def test_a_translation_moves_updated_at_but_is_not_a_webhook_field():
    assert 'transcription_translation' in recording_changes.TRACKED_FIELDS
    assert 'transcription_translation' not in recording_changes.WEBHOOK_FIELD_NAMES
    assert 'translation' not in recording_changes.WEBHOOK_FIELD_NAMES.values()


# 5. The translation follows its transcript ----------------------------------

def test_a_speaker_rename_is_copied_into_the_translation(user):
    with app.app_context():
        rec = _recording(user)
        rec.transcription = _segments(("Alice", 0.0, 2.0, "Hello there"), ("Bob", 2.5, 4.0, "Good morning"))
        db.session.commit()
        db.session.refresh(rec)
        translated = json.loads(rec.transcription_translation)
        assert [s['speaker'] for s in translated] == ["Alice", "Bob"]
        assert translated[1]['start_time'] == 2.5
        assert [s['sentence'] for s in translated] == ["Hola", "Buenos días"]
        assert rec.transcription_translation_language == 'Spanish'
        assert rec.translation_duration_seconds == 3


def test_a_text_edit_clears_the_translation(user):
    with app.app_context():
        rec = _recording(user)
        rec.transcription = _segments(("SPEAKER_00", 0.0, 2.0, "Hello there, friend"),
                                      ("SPEAKER_01", 2.0, 4.0, "Good morning"))
        db.session.commit()
        db.session.refresh(rec)
        assert rec.transcription_translation is None
        assert rec.transcription_translation_language is None
        assert rec.translation_duration_seconds is None


def test_a_text_edit_clears_a_translation_of_text_keyed_segments(user):
    def rows(first):
        return json.dumps([{"speaker": "A", "text": first}, {"speaker": "B", "text": "Good morning"}])
    with app.app_context():
        rec = Recording(user_id=user, status='COMPLETED', transcription=rows("Hello there"),
                        transcription_translation=json.dumps([{"speaker": "A", "text": "Hola"},
                                                              {"speaker": "B", "text": "Buenos días"}]),
                        transcription_translation_language='Spanish')
        db.session.add(rec)
        db.session.commit()
        rec.transcription = rows("Hello there, friend")
        db.session.commit()
        db.session.refresh(rec)
        assert rec.transcription_translation is None


def test_reprocessing_clears_the_translation(user):
    with app.app_context():
        rec = _recording(user)
        rec.transcription = None
        db.session.commit()
        db.session.refresh(rec)
        assert rec.transcription_translation is None


def test_an_expired_transcript_is_compared_with_the_stored_one(user):
    """The speaker routes commit before writing the transcript, which expires
    the old value; it is read back from the row instead."""
    with app.app_context():
        rec = _recording(user)
        db.session.expire(rec)
        rec.transcription = _segments(("Alice", 0.0, 2.0, "Hello there"), ("Bob", 2.0, 4.0, "Good morning"))
        db.session.commit()
        db.session.refresh(rec)
        assert [s['speaker'] for s in json.loads(rec.transcription_translation)] == ["Alice", "Bob"]
        db.session.expire(rec)
        rec.transcription = _segments(("Alice", 0.0, 2.0, "Hello"), ("Bob", 2.0, 4.0, "Good morning"))
        db.session.commit()
        db.session.refresh(rec)
        assert rec.transcription_translation is None


def test_an_edit_of_the_translation_itself_is_kept(user):
    with app.app_context():
        rec = _recording(user)
        rid = rec.id
    client = app.test_client()
    _login(client, user)
    edited = _segments(("SPEAKER_00", 0.0, 2.0, "Hola a todos"), ("SPEAKER_01", 2.0, 4.0, "Buenos días"))
    with patch('src.api.recordings.export_recording'):
        resp = client.post(f'/recording/{rid}/update_transcription', json={'transcription_translation': edited})
    assert resp.status_code == 200
    with app.app_context():
        rec = db.session.get(Recording, rid)
        assert rec.transcription_translation == edited
        assert rec.transcription == ORIGINAL


def test_an_unrelated_change_leaves_the_translation_alone(user):
    with app.app_context():
        rec = _recording(user)
        rec.title = "Renamed"
        db.session.commit()
        db.session.refresh(rec)
        assert rec.transcription_translation == SPANISH
