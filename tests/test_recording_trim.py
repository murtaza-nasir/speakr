"""Trimming a recording: the audio and the transcript together (#660).

src/services/recording_trim.py cuts the audio with ffmpeg (a short tone made
by ffmpeg here) and the transcript with it, so nothing is transcribed again.

Covered here:
  - the transcript rules: dropped, clipped and moved segments, both key sets,
    word times;
  - the job on real audio (WAV, MP3, M4A): the length, the codec kept, the old
    file removed, and what depends on the audio or the transcript (peaks,
    Inquire chunks, speaker snippets, voice embeddings, the translation);
  - the job through the queue: a failure leaves the recording as it was and
    COMPLETED; a job that runs again after its commit cuts nothing twice;
  - the route: who may trim (owner, edit permission), the checks of the range,
    a recording that is busy, the audit event, the state of the trim;
  - the summary: regenerated only when asked.

SHARED-DB: users, recordings, jobs and files are removed afterwards.
"""

import json
import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from tests.audit_log_support import (  # noqa: E402
    app, client_for, db, make_user, max_id, remove_users, rows_after,
)
import src.app as app_module
from src.models import InternalShare, ProcessingJob, Recording, SpeakerSnippet, TranscriptChunk
from src.services import recording_trim as trim
from src.services.job_queue import BACKGROUND_JOBS, CONVERT_JOBS, JOB_RUNNERS, is_background, job_queue
from src.services.storage.service import get_storage_service

app.config['WTF_CSRF_ENABLED'] = False
app.config['TESTING'] = True

needs_ffmpeg = pytest.mark.skipif(shutil.which('ffmpeg') is None or shutil.which('ffprobe') is None,
                                  reason='ffmpeg is not installed')

SECONDS = 10.0
SEGMENTS = [
    {'speaker': 'SPEAKER_00', 'sentence': 'Before the meeting starts.', 'start_time': 0.0, 'end_time': 1.5},
    {'speaker': 'SPEAKER_01', 'sentence': 'Across the start.', 'start_time': 1.5, 'end_time': 3.0},
    {'speaker': 'SPEAKER_01', 'sentence': 'Fully inside.', 'start_time': 3.0, 'end_time': 5.0},
    {'speaker': 'SPEAKER_01', 'sentence': 'Across the end.', 'start_time': 5.5, 'end_time': 7.5},
    {'speaker': 'SPEAKER_02', 'sentence': 'After the meeting.', 'start_time': 8.0, 'end_time': 10.0},
]
START, END = 2.0, 7.0


# ---- the transcript rules ---------------------------------------------------------

def test_segments_are_dropped_clipped_and_moved_to_start_at_zero():
    kept, index_map = trim.trim_segments(SEGMENTS, START, END)
    assert [(s['sentence'], s['start_time'], s['end_time']) for s in kept] == [
        ('Across the start.', 0.0, 1.0),
        ('Fully inside.', 1.0, 3.0),
        ('Across the end.', 3.5, 5.0),
    ]
    assert index_map == {1: 0, 2: 1, 3: 2}
    # The stored segments are not changed
    assert SEGMENTS[1]['start_time'] == 1.5


def test_the_older_key_set_and_word_times_are_cut_too():
    segments = [{'speaker': 'A', 'text': 'one two three', 'start': '0:01', 'end': 4,
                 'words': [{'word': 'one', 'start': 1.0, 'end': 1.8}, {'word': 'two', 'start': 2.0, 'end': 2.9},
                           {'word': 'three', 'start': 3.1, 'end': 3.9}]}]
    # A time as text is not a time this code reads: refused rather than guessed
    with pytest.raises(trim.TrimError) as refused:
        trim.trim_segments(segments, 2.0, 3.0)
    assert refused.value.code == 'trim_needs_times'
    segments[0]['start'] = 1.0
    [seg], _ = trim.trim_segments(segments, 2.0, 3.5)
    assert (seg['start'], seg['end']) == (0.0, 1.5) and 'start_time' not in seg
    assert [(w['word'], w['start'], w['end']) for w in seg['words']] == [('two', 0.0, 0.9), ('three', 1.1, 1.5)]


def test_a_sliver_at_a_cut_is_dropped():
    kept, _ = trim.trim_segments([{'start_time': 0.0, 'end_time': 2.02, 'sentence': 'x'}], 2.0, 5.0)
    assert kept == []


# ---- fixtures ---------------------------------------------------------------------

def _tone(path, codec_args):
    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', f'sine=frequency=440:duration={SECONDS}:sample_rate=16000', *codec_args, path],
                   check=True, capture_output=True)


FORMATS = {
    'wav': ([], 'audio/wav', 'pcm_s16le'),
    'mp3': (['-c:a', 'libmp3lame', '-b:a', '64k'], 'audio/mpeg', 'mp3'),
    'm4a': (['-c:a', 'aac', '-b:a', '64k'], 'audio/mp4', 'aac'),
}


@pytest.fixture
def world(tmp_path):
    created = []
    stored = []
    with app.app_context():
        owner = make_user(created, prefix='trim')
        editor = make_user(created, prefix='trimed')
        viewer = make_user(created, prefix='trimvw')
        stranger = make_user(created, prefix='trimx')

        def recording(fmt='wav', **extra):
            codec_args, mime, _ = FORMATS[fmt]
            local = str(tmp_path / f'tone-{uuid.uuid4().hex[:6]}.{fmt}')
            _tone(local, codec_args)
            storage = get_storage_service()
            key = storage.build_recording_key(f'meeting.{fmt}', None)
            locator = storage.upload_local_file(local, key, content_type=mime).locator
            stored.append(locator)
            values = dict(user_id=owner.id, title='Trim me', status='COMPLETED', original_filename=f'meeting.{fmt}',
                          transcription=json.dumps(SEGMENTS), summary='A summary.', audio_path=locator,
                          mime_type=mime, audio_duration_seconds=SECONDS, file_size=os.path.getsize(local))
            values.update(extra)
            rec = Recording(**values)
            db.session.add(rec)
            db.session.commit()
            return rec

        yield dict(owner=owner, editor=editor, viewer=viewer, stranger=stranger, recording=recording,
                   stored=stored)

        db.session.rollback()
        for rec in Recording.query.filter_by(user_id=owner.id).all():
            stored.append(rec.audio_path)
            ProcessingJob.query.filter_by(recording_id=rec.id).delete()
            SpeakerSnippet.query.filter_by(recording_id=rec.id).delete()
            TranscriptChunk.query.filter_by(recording_id=rec.id).delete()
            InternalShare.query.filter_by(recording_id=rec.id).delete()
        from src.models import Speaker
        Speaker.query.filter_by(user_id=owner.id).delete()
        db.session.commit()
        storage = get_storage_service()
        for locator in stored:
            try:
                storage.delete(locator, missing_ok=True)
            except Exception:
                pass
        remove_users(created)


@contextmanager
def _internal_sharing():
    missing = object()
    saved = app_module.__dict__.get('ENABLE_INTERNAL_SHARING', missing)
    app_module.ENABLE_INTERNAL_SHARING = True
    try:
        yield
    finally:
        if saved is missing:
            app_module.__dict__.pop('ENABLE_INTERNAL_SHARING', None)
        else:
            app_module.ENABLE_INTERNAL_SHARING = saved


def _speaker(owner):
    from src.models import Speaker
    speaker = Speaker(name=f'Pat {uuid.uuid4().hex[:4]}', user_id=owner.id)
    db.session.add(speaker)
    db.session.commit()
    return speaker


# ---- the job on real audio ----------------------------------------------------------

@needs_ffmpeg
@pytest.mark.parametrize('fmt', sorted(FORMATS))
def test_the_job_cuts_the_audio_and_keeps_its_codec(world, fmt):
    from src.utils.ffprobe import get_codec_info, get_duration
    rec = world['recording'](fmt)
    old_locator = rec.audio_path
    storage = get_storage_service()

    result = trim.run_trim_job(rec, {'start': START, 'end': END, 'source_audio_path': old_locator})

    db.session.refresh(rec)
    assert result == {'summary_queued': False}
    assert rec.audio_path != old_locator
    assert not storage.exists(old_locator), 'the old audio file is removed'
    assert storage.exists(rec.audio_path)
    with storage.materialize(rec.audio_path) as local:
        info = get_codec_info(local.local_path)
        measured = get_duration(local.local_path)
        assert os.path.splitext(local.local_path)[1] == f'.{fmt}'
    assert info['audio_codec'] == FORMATS[fmt][2]
    # Encoders add a few milliseconds of padding (AAC more than the others)
    assert measured == pytest.approx(END - START, abs=0.1)
    assert rec.audio_duration_seconds == pytest.approx(END - START, abs=0.1)
    assert rec.file_size > 0
    stored = json.loads(rec.transcription)
    assert [s['sentence'] for s in stored] == ['Across the start.', 'Fully inside.', 'Across the end.']
    assert stored[0]['start_time'] == 0.0 and stored[-1]['end_time'] == 5.0
    assert rec.status == 'COMPLETED'
    assert rec.summary == 'A summary.', 'the summary is left alone unless asked'


@needs_ffmpeg
def test_what_depends_on_the_audio_and_the_transcript_follows(world):
    from src.services import waveform_peaks
    rec = world['recording']('wav')
    owner = world['owner']
    rec.transcription_translation = json.dumps([{**seg, 'sentence': f'T: {seg["sentence"]}'} for seg in SEGMENTS])
    rec.transcription_translation_language = 'de'
    rec.speaker_embeddings = {'SPEAKER_00': [0.1] * 4, 'SPEAKER_01': [0.2] * 4, 'SPEAKER_02': [0.3] * 4,
                              'SPEAKER_09': [0.9] * 4}
    rec.speaker_label_map = {'SPEAKER_01': 'SPEAKER_01'}
    speaker = _speaker(owner)
    db.session.add_all([
        SpeakerSnippet(speaker_id=speaker.id, recording_id=rec.id, segment_index=0, text_snippet='Before', timestamp=0.0),
        SpeakerSnippet(speaker_id=speaker.id, recording_id=rec.id, segment_index=2, text_snippet='Fully', timestamp=3.0),
        SpeakerSnippet(speaker_id=speaker.id, recording_id=rec.id, segment_index=4, text_snippet='After', timestamp=8.0),
        TranscriptChunk(recording_id=rec.id, user_id=owner.id, chunk_index=0, content='Before the meeting starts.',
                        start_time=0.0, end_time=1.5),
    ])
    db.session.commit()
    upload_folder = app.config['UPLOAD_FOLDER']
    os.makedirs(waveform_peaks.cache_dir(upload_folder), exist_ok=True)
    peaks_file = os.path.join(waveform_peaks.cache_dir(upload_folder), f'{rec.id}-stale.bin')
    with open(peaks_file, 'wb') as f:
        f.write(b'SPK1')
    reindexed = []

    with patch('src.api.recordings.reindex_recording_chunks_async', side_effect=reindexed.append):
        trim.run_trim_job(rec, {'start': START, 'end': END, 'source_audio_path': rec.audio_path})

    db.session.refresh(rec)
    assert not os.path.exists(peaks_file), 'the waveform peaks are computed again'
    assert TranscriptChunk.query.filter_by(recording_id=rec.id).count() == 0
    assert reindexed == [rec.id], 'the Inquire chunks are built again'
    snippets = SpeakerSnippet.query.filter_by(recording_id=rec.id).all()
    assert [(s.segment_index, s.timestamp, s.text_snippet) for s in snippets] == [(1, 1.0, 'Fully')]
    # SPEAKER_00 and SPEAKER_02 have no segment left; SPEAKER_09 was never in the transcript
    assert sorted(rec.speaker_embeddings) == ['SPEAKER_01', 'SPEAKER_09']
    translation = json.loads(rec.transcription_translation)
    assert [(s['sentence'], s['start_time'], s['end_time']) for s in translation] == [
        ('T: Across the start.', 0.0, 1.0), ('T: Fully inside.', 1.0, 3.0), ('T: Across the end.', 3.5, 5.0)]
    assert rec.transcription_translation_language == 'de'


@needs_ffmpeg
def test_the_summary_is_regenerated_when_asked(world):
    rec = world['recording']('wav')
    queued = []
    with patch.object(job_queue, 'enqueue', side_effect=lambda **kw: queued.append(kw) or 1):
        result = trim.run_trim_job(rec, {'start': START, 'end': END, 'source_audio_path': rec.audio_path,
                                         'regenerate_summary': True, 'user_id': world['owner'].id})
    assert result == {'summary_queued': True}
    assert [(q['job_type'], q['recording_id']) for q in queued] == [('summarize', rec.id)]


@needs_ffmpeg
def test_a_job_that_runs_again_after_its_commit_cuts_nothing_twice(world):
    rec = world['recording']('wav')
    params = {'start': START, 'end': END, 'source_audio_path': rec.audio_path}
    trim.run_trim_job(rec, params)
    db.session.refresh(rec)
    once = (rec.audio_path, rec.transcription, rec.audio_duration_seconds)
    assert trim.run_trim_job(rec, params) == {'skipped': True}
    db.session.refresh(rec)
    assert (rec.audio_path, rec.transcription, rec.audio_duration_seconds) == once


@needs_ffmpeg
def test_an_ffmpeg_failure_leaves_the_recording_as_it_was(world):
    from src.utils.ffmpeg_utils import FFmpegError
    rec = world['recording']('wav')
    before = (rec.audio_path, rec.transcription, rec.audio_duration_seconds)
    upload_folder = app.config['UPLOAD_FOLDER']
    with patch('src.utils.ffmpeg_utils._run_ffmpeg_command', side_effect=FFmpegError('boom')):
        with pytest.raises(trim.TrimError) as failed:
            trim.run_trim_job(rec, {'start': START, 'end': END, 'source_audio_path': rec.audio_path})
    assert failed.value.code == 'trim_failed' and failed.value.permanent
    db.session.refresh(rec)
    assert (rec.audio_path, rec.transcription, rec.audio_duration_seconds) == before
    assert get_storage_service().exists(rec.audio_path)
    assert not [n for n in os.listdir(upload_folder) if n.startswith('trim_') and n.endswith(f'_{rec.id}.wav')]


@needs_ffmpeg
def test_a_failed_trim_through_the_queue_keeps_the_recording_completed(world):
    """A trim is a background job: its failure is not the recording's."""
    from src.utils.ffmpeg_utils import FFmpegError
    rec = world['recording']('wav')
    before = rec.transcription
    job_id = job_queue.enqueue(world['owner'].id, rec.id, 'trim',
                               params={'start': START, 'end': END, 'source_audio_path': rec.audio_path})
    db.session.refresh(rec)
    assert rec.status == 'COMPLETED', 'queueing a trim does not change the status'
    job = job_queue._claim_next_job(['trim'], 'convert')
    assert job is not None and job.id == job_id
    with patch('src.utils.ffmpeg_utils._run_ffmpeg_command', side_effect=FFmpegError('boom')):
        job_queue._process_job(job)
    db.session.rollback()
    job = db.session.get(ProcessingJob, job_id)
    rec = db.session.get(Recording, rec.id)
    assert job.status == 'failed' and job.retry_count == 1, 'an ffmpeg failure is not retried'
    assert rec.status == 'COMPLETED' and rec.transcription == before
    with app.test_request_context():
        assert trim.trim_state(rec.id)['state'] == 'failed'


@needs_ffmpeg
def test_a_trim_through_the_queue_completes(world):
    rec = world['recording']('mp3')
    job_id = job_queue.enqueue(world['owner'].id, rec.id, 'trim',
                               params={'start': START, 'end': END, 'source_audio_path': rec.audio_path})
    job = job_queue._claim_next_job(['trim'], 'convert')
    assert job.id == job_id
    job_queue._process_job(job)
    db.session.rollback()
    assert db.session.get(ProcessingJob, job_id).status == 'completed'
    assert db.session.get(Recording, rec.id).audio_duration_seconds == pytest.approx(END - START, abs=0.1)


def test_the_job_type_is_registered_as_a_background_ffmpeg_job():
    assert JOB_RUNNERS['trim'] == '_run_trim'
    assert 'trim' in CONVERT_JOBS and 'trim' in BACKGROUND_JOBS and is_background('trim')


# ---- the route ----------------------------------------------------------------------

def _post(client, rec_id, body):
    return client.post(f'/api/recordings/{rec_id}/trim', json=body)


@needs_ffmpeg
def test_the_owner_queues_a_trim_and_it_is_audited(world):
    rec = world['recording']('wav')
    start_id = max_id()
    queued = []
    with patch.object(job_queue, 'enqueue', side_effect=lambda **kw: queued.append(kw) or 77):
        resp = _post(client_for(world['owner']), rec.id, {'start': START, 'end': END, 'regenerate_summary': True})
    assert resp.status_code == 202, resp.get_json()
    assert resp.get_json() == {'success': True, 'job_id': 77, 'state': 'queued'}
    [job] = queued
    assert job['job_type'] == 'trim' and job['recording_id'] == rec.id
    assert job['params']['source_audio_path'] == rec.audio_path and job['params']['regenerate_summary'] is True
    [row] = rows_after(start_id, 'recording.trimmed')
    assert row.recording_id == rec.id and row.actor_user_id == world['owner'].id
    details = row.details if isinstance(row.details, dict) else json.loads(row.details)
    assert details == {'start': START, 'end': END, 'removed_segments': 2}


@needs_ffmpeg
def test_only_the_owner_and_users_with_edit_permission_may_trim(world):
    rec = world['recording']('wav')
    db.session.add_all([
        InternalShare(recording_id=rec.id, owner_id=world['owner'].id, shared_with_user_id=world['editor'].id,
                      can_edit=True),
        InternalShare(recording_id=rec.id, owner_id=world['owner'].id, shared_with_user_id=world['viewer'].id,
                      can_edit=False),
    ])
    db.session.commit()
    body = {'start': START, 'end': END}
    with _internal_sharing(), patch.object(job_queue, 'enqueue', return_value=5):
        assert _post(client_for(world['stranger']), rec.id, body).status_code == 404
        assert _post(client_for(world['viewer']), rec.id, body).status_code == 403
        assert client_for(world['viewer']).get(f'/api/recordings/{rec.id}/trim').status_code == 404
        assert _post(client_for(world['editor']), rec.id, body).status_code == 202


@needs_ffmpeg
@pytest.mark.parametrize('body, code', [
    ({'start': 7, 'end': 2}, 'trim_invalid_range'),
    ({'start': -1, 'end': 5}, 'trim_invalid_range'),
    ({'start': 0, 'end': 99}, 'trim_invalid_range'),
    ({'start': '1', 'end': 5}, 'trim_invalid_range'),
    ({'start': True, 'end': 5}, 'trim_invalid_range'),
    ({'start': 2, 'end': 2.5}, 'trim_too_short'),
    ({'start': 0, 'end': 10}, 'trim_nothing_removed'),
    ({'start': 2, 'end': 7, 'regenerate_summary': 'yes'}, 'trim_invalid_range'),
])
def test_a_bad_range_is_refused(world, body, code):
    rec = world['recording']('wav')
    with patch.object(job_queue, 'enqueue', return_value=5) as enqueue:
        resp = _post(client_for(world['owner']), rec.id, body)
    assert resp.status_code == 400 and resp.get_json()['code'] == code, resp.get_json()
    enqueue.assert_not_called()


@needs_ffmpeg
def test_a_recording_that_cannot_be_trimmed_is_refused(world):
    client = client_for(world['owner'])
    body = {'start': START, 'end': END}
    cases = {
        'trim_not_completed': world['recording']('wav', status='PROCESSING'),
        'trim_needs_segments': world['recording']('wav', transcription='Plain text transcript.'),
        'trim_no_audio': world['recording']('wav', audio_path=None),
        'trim_unknown_duration': world['recording']('wav', audio_duration_seconds=None),
        'trim_needs_times': world['recording']('wav', transcription=json.dumps([{'sentence': 'No times'}])),
    }
    with patch.object(job_queue, 'enqueue', return_value=5):
        for code, rec in cases.items():
            resp = _post(client, rec.id, body)
            assert resp.status_code == 400 and resp.get_json()['code'] == code, (code, resp.get_json())
        busy = world['recording']('wav')
        db.session.add(ProcessingJob(user_id=world['owner'].id, recording_id=busy.id, job_type='summarize',
                                     status='queued'))
        db.session.commit()
        resp = _post(client, busy.id, body)
        assert resp.status_code == 409 and resp.get_json()['code'] == 'trim_busy'
    assert _post(client, busy.id, []).status_code == 400


@needs_ffmpeg
def test_the_state_of_a_trim(world):
    rec = world['recording']('wav')
    client = client_for(world['owner'])
    assert client.get(f'/api/recordings/{rec.id}/trim').get_json() == {'state': 'none'}
    db.session.add(ProcessingJob(user_id=world['owner'].id, recording_id=rec.id, job_type='trim', status='failed',
                                 error_message='trim_store_failed', params=json.dumps({'regenerate_summary': True})))
    db.session.commit()
    failed = client.get(f'/api/recordings/{rec.id}/trim').get_json()
    assert failed['state'] == 'failed' and failed['code'] == 'trim_store_failed'
    assert failed['error'] == 'The trimmed audio could not be stored.'
    db.session.add(ProcessingJob(user_id=world['owner'].id, recording_id=rec.id, job_type='trim', status='failed',
                                 error_message='Some provider text that is not a code'))
    db.session.commit()
    assert client.get(f'/api/recordings/{rec.id}/trim').get_json()['code'] == 'trim_failed'
    db.session.add(ProcessingJob(user_id=world['owner'].id, recording_id=rec.id, job_type='trim', status='completed',
                                 params=json.dumps({'regenerate_summary': True})))
    db.session.commit()
    done = client.get(f'/api/recordings/{rec.id}/trim').get_json()
    assert done['state'] == 'completed' and done['summary_queued'] is True


@needs_ffmpeg
def test_an_end_a_little_past_the_stored_length_is_the_end_of_the_audio(world):
    """A player can measure the file a little longer than ffprobe did."""
    rec = world['recording']('wav')
    queued = []
    with patch.object(job_queue, 'enqueue', side_effect=lambda **kw: queued.append(kw) or 3):
        resp = _post(client_for(world['owner']), rec.id, {'start': 2, 'end': SECONDS + 0.6})
        assert resp.status_code == 202, resp.get_json()
        assert _post(client_for(world['owner']), rec.id, {'start': 2, 'end': SECONDS + 1.5}).status_code == 400
    assert queued[0]['params']['end'] == SECONDS
