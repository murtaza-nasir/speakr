"""Diarization labels (SPEAKER_00, "Speaker 1") are never stored or listed as people.

The rule is src/utils/speaker_labels.py, with a JavaScript twin tested against
the same table (tests/fixtures/speaker_label_cases.json). The transcript
keeps its labels; every store and list that holds people leaves them out:
saved speakers, voice samples, snippets, participants, the Speakers filters,
speaker search, API v1 and the speaker name of Inquire chunks.

SHARED-DB: every assertion is scoped to the user and recordings the test made.
"""

import json
import os
import sys
import uuid
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.speaker_labels import (SPEAKER_WORDS, clean_participants, is_generic_speaker_label,
                                      is_real_speaker_name, normalize_label, real_speaker_names)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, 'tests', 'fixtures', 'speaker_label_cases.json'), encoding='utf-8') as _f:
    CASES = json.load(_f)


# ------------------------------------------------------------------ the rule

def test_the_table_is_not_empty():
    assert len(CASES['generic']) > 40 and len(CASES['names']) > 40


@pytest.mark.parametrize('label', CASES['generic'])
def test_generic_labels(label):
    assert is_generic_speaker_label(label) is True
    assert is_real_speaker_name(label) is False


@pytest.mark.parametrize('name', CASES['names'])
def test_names_are_kept(name):
    assert is_generic_speaker_label(name) is False


@pytest.mark.parametrize('value', [None, 0, 7, 1.5, [], {}, b'SPEAKER_00'])
def test_values_that_are_not_text(value):
    assert is_generic_speaker_label(value) is False
    assert is_real_speaker_name(value) is False


def test_normalization():
    assert normalize_label('  Speaker   1 ') == 'speaker 1'
    assert normalize_label('ＳＰＥＡＫＥＲ＿００') == 'speaker_00'


def test_the_default_speaker_name_of_every_language_is_generic():
    locales = os.path.join(ROOT, 'static', 'locales')
    files = [f for f in os.listdir(locales) if f.endswith('.json')]
    assert len(files) >= 7
    for filename in files:
        with open(os.path.join(locales, filename), encoding='utf-8') as f:
            template = json.load(f)['transcription']['speaker']
        assert template.replace('{{number}}', '').strip().lower() in SPEAKER_WORDS, filename
        assert is_generic_speaker_label(template.replace('{{number}}', '1')), filename
        assert is_generic_speaker_label(template.replace('{{number}}', '12')), filename


def test_the_labels_the_connectors_and_the_editor_produce_are_generic():
    for label in ('SPEAKER_00', 'SPEAKER_7', 'UNKNOWN_SPEAKER', 'Unknown', 'Unknown Speaker',
                  'SPEAKER_%02d' % 3, f'SPEAKER_{12}'):
        assert is_generic_speaker_label(label), label


def test_real_speaker_names_and_participants():
    assert real_speaker_names([' Dana ', 'SPEAKER_00', '', None, 'Speaker 1', 'Kim', 3]) == ['Dana', 'Kim']
    assert real_speaker_names(None) == []
    assert real_speaker_names('Dana') == ['Dana']
    assert clean_participants('Dana, SPEAKER_01,  Kim, UNKNOWN_SPEAKER') == 'Dana, Kim'
    assert clean_participants('SPEAKER_00, Speaker 2') is None
    # A field without a label is returned as typed.
    assert clean_participants('Dana;  Kim  (finance)') == 'Dana;  Kim  (finance)'
    assert clean_participants(None) is None
    assert clean_participants('') == ''


def test_the_library_helper_follows_the_rule():
    from src.services.library_labels import is_generic_speaker
    for label in ('SPEAKER_00', 'Speaker 1', 'unknown', 'spk_2', '', None):
        assert is_generic_speaker(label)
    for name in ('Dana', 'Speaker Johnson', 'A'):
        assert not is_generic_speaker(name)


# ---------------------------------------------------------- stores and lists

from src.app import app, db  # noqa: E402
from src.models import Recording, Speaker, SpeakerVoiceSample, TranscriptChunk, User  # noqa: E402
from src.models.speaker_snippet import SpeakerSnippet  # noqa: E402

app.config['WTF_CSRF_ENABLED'] = False

SEGMENTS = [
    {'speaker': 'SPEAKER_00', 'sentence': 'Good morning everyone, let us begin the review.', 'start_time': 0.0, 'end_time': 20.0},
    {'speaker': 'SPEAKER_01', 'sentence': 'Thanks, I have the quarterly numbers ready here.', 'start_time': 20.0, 'end_time': 40.0},
    {'speaker': 'SPEAKER_00', 'sentence': 'Then we start with the budget for next year.', 'start_time': 40.0, 'end_time': 60.0},
    {'speaker': 'SPEAKER_01', 'sentence': 'The vendor figures arrived late on Friday.', 'start_time': 60.0, 'end_time': 80.0},
]


def _embeddings():
    a = [0.0] * 256
    b = [0.0] * 256
    a[0] = 1.0
    b[1] = 1.0
    return json.dumps({'SPEAKER_00': a, 'SPEAKER_01': b})


def _client(user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True
    return client


@pytest.fixture
def world():
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f'gsl_{suffix}', email=f'gsl_{suffix}@local.test', password='x', name='Owner Person')
        db.session.add(user)
        db.session.commit()
        made = {'user': user, 'recordings': []}

        def recording(segments=None, embeddings=True, **fields):
            rec = Recording(user_id=user.id, title='r', status='COMPLETED',
                            audio_path='local://recordings/x.mp3', original_filename='x.mp3',
                            transcription=json.dumps(segments if segments is not None else SEGMENTS),
                            speaker_embeddings=_embeddings() if embeddings else None, **fields)
            db.session.add(rec)
            db.session.commit()
            made['recordings'].append(rec.id)
            return rec

        made['recording'] = recording
        yield made
        db.session.rollback()
        ids = made['recordings']
        if ids:
            TranscriptChunk.query.filter(TranscriptChunk.recording_id.in_(ids)).delete(synchronize_session=False)
            SpeakerSnippet.query.filter(SpeakerSnippet.recording_id.in_(ids)).delete(synchronize_session=False)
        SpeakerVoiceSample.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        from src.models import ProcessingJob
        if ids:
            ProcessingJob.query.filter(ProcessingJob.recording_id.in_(ids)).delete(synchronize_session=False)
        Speaker.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        if ids:
            Recording.query.filter(Recording.id.in_(ids)).delete(synchronize_session=False)
        from src.models import InquireSession
        InquireSession.query.filter_by(user_id=user.id).delete(synchronize_session=False)
        User.query.filter_by(id=user.id).delete(synchronize_session=False)
        db.session.commit()


def _names(user):
    return sorted(s.name for s in Speaker.query.filter_by(user_id=user.id).all())


def _segments(rec):
    db.session.expire_all()
    return json.loads(db.session.get(Recording, rec.id).transcription)


def _save(client, rec, speaker_map, route='update_speakers', transcript=None):
    with patch('src.api.recordings.export_recording', lambda *a, **k: None, create=True):
        if route == 'update_speakers':
            return client.post(f'/recording/{rec.id}/update_speakers',
                               json={'speaker_map': speaker_map, 'regenerate_summary': False})
        return client.post(f'/recording/{rec.id}/update_transcript',
                           json={'transcript_data': transcript if transcript is not None else _segments(rec),
                                 'speaker_map': speaker_map, 'regenerate_summary': False})


@pytest.mark.parametrize('route', ['update_speakers', 'update_transcript'])
def test_only_the_named_speaker_is_stored(world, route):
    user, rec = world['user'], world['recording']()
    client = _client(user)
    resp = _save(client, rec, {'SPEAKER_00': {'name': 'Dana Lee', 'isMe': False}}, route)
    assert resp.status_code == 200, resp.data

    # One saved speaker: the named one. The other turns keep their label.
    assert _names(user) == ['Dana Lee']
    dana = Speaker.query.filter_by(user_id=user.id).one()
    segments = _segments(rec)
    assert [s['speaker'] for s in segments] == ['Dana Lee', 'SPEAKER_01', 'Dana Lee', 'SPEAKER_01']
    assert [s.get('speaker_id') for s in segments] == [dana.id, None, dana.id, None]

    # Voice samples and snippets: the named speaker only.
    samples = SpeakerVoiceSample.query.filter_by(user_id=user.id).all()
    assert [(s.speaker_id, s.label) for s in samples] == [(dana.id, 'SPEAKER_00')]
    assert {s.speaker_id for s in SpeakerSnippet.query.filter_by(recording_id=rec.id)} == {dana.id}

    # Participants, the Filters panel source, speaker search, API v1.
    assert db.session.get(Recording, rec.id).participants == 'Dana Lee'
    assert [s['name'] for s in client.get('/speakers').get_json()] == ['Dana Lee']
    assert [s['name'] for s in client.get('/speakers/search?top=1').get_json()] == ['Dana Lee']
    assert client.get('/speakers/search?q=speaker').get_json() == []
    listed = client.get('/api/v1/speakers').get_json()['speakers']
    assert [s['name'] for s in listed] == ['Dana Lee']
    in_recording = client.get(f'/api/v1/recordings/{rec.id}/speakers').get_json()['speakers']
    by_label = {s['label']: s for s in in_recording}
    assert by_label['SPEAKER_00']['identified_name'] == 'Dana Lee' and by_label['SPEAKER_00']['speaker_id'] == dana.id
    # The unnamed turn is still shown, as a label without a person.
    assert by_label['SPEAKER_01']['identified_name'] is None and by_label['SPEAKER_01']['speaker_id'] is None


def test_inquire_chunks_carry_names_only(world):
    import src.services.embeddings as emb
    user, rec = world['user'], world['recording']([
        {'speaker': 'Dana Lee', 'sentence': 'word ' * 200, 'start_time': 0.0, 'end_time': 60.0},
        {'speaker': 'SPEAKER_01', 'sentence': 'other ' * 200, 'start_time': 60.0, 'end_time': 120.0},
        {'speaker': 'UNKNOWN_SPEAKER', 'sentence': 'third ' * 200, 'start_time': 120.0, 'end_time': 180.0},
        {'speaker': 'Speaker 2', 'sentence': 'fourth ' * 200, 'start_time': 180.0, 'end_time': 240.0},
    ], embeddings=False)
    with patch.object(emb, 'EMBEDDINGS_AVAILABLE', False), patch.object(emb, 'generate_embeddings', return_value=[]):
        assert emb.process_recording_chunks(rec.id) is True
    chunks = TranscriptChunk.query.filter_by(recording_id=rec.id).order_by(TranscriptChunk.chunk_index).all()
    assert len(chunks) >= 4
    assert {c.speaker_name for c in chunks} == {'Dana Lee', None}
    # The text of the chunk keeps the label: it is the transcript.
    assert any('SPEAKER_01:' in c.content for c in chunks)
    assert all(c.speaker_name is None for c in chunks if c.content.startswith(('SPEAKER_01', 'UNKNOWN_SPEAKER', 'Speaker 2')))


def test_chunk_metadata_rule():
    from src.transcript_chunking import _finalize_segment_chunk
    seg = lambda speaker: {'speaker': speaker, 'text': 'hello there', 'start': 0.0, 'end': 1.0}
    assert _finalize_segment_chunk([seg('Dana'), seg('Dana'), seg('SPEAKER_01')])['speaker_name'] == 'Dana'
    # The dominant voice is unnamed: no person is credited with the chunk.
    assert _finalize_segment_chunk([seg('SPEAKER_01'), seg('SPEAKER_01'), seg('Dana')])['speaker_name'] is None
    assert _finalize_segment_chunk([seg('Speaker 1')])['speaker_name'] is None
    assert 'SPEAKER_01: hello there' in _finalize_segment_chunk([seg('SPEAKER_01')])['text']


@pytest.mark.parametrize('typed', ['Speaker 1', 'SPEAKER_07', 'unknown speaker', 'Sprecher 2'])
@pytest.mark.parametrize('route', ['update_speakers', 'update_transcript'])
def test_a_label_typed_as_a_name_is_not_a_speaker(world, route, typed):
    """A user who names someone "Speaker 1": the turns show it, nothing is stored."""
    user, rec = world['user'], world['recording']()
    client = _client(user)
    resp = _save(client, rec, {'SPEAKER_00': {'name': 'Dana Lee'}, 'SPEAKER_01': {'name': typed}}, route)
    assert resp.status_code == 200, resp.data
    assert 'error' not in resp.get_json()

    assert _names(user) == ['Dana Lee']
    segments = _segments(rec)
    assert [s['speaker'] for s in segments] == ['Dana Lee', typed, 'Dana Lee', typed]
    assert segments[1].get('speaker_id') is None
    assert db.session.get(Recording, rec.id).participants == 'Dana Lee'
    dana = Speaker.query.filter_by(user_id=user.id).one()
    assert {s.speaker_id for s in SpeakerVoiceSample.query.filter_by(user_id=user.id)} == {dana.id}
    assert {s.speaker_id for s in SpeakerSnippet.query.filter_by(recording_id=rec.id)} == {dana.id}
    assert [s['name'] for s in client.get('/speakers').get_json()] == ['Dana Lee']


def test_a_label_typed_as_a_name_in_a_plain_text_transcript(world):
    user = world['user']
    rec = world['recording'](embeddings=False)
    rec.transcription = '[SPEAKER_00]: Good morning.\n[SPEAKER_01]: Thanks.'
    db.session.commit()
    resp = _save(_client(user), rec, {'SPEAKER_00': {'name': 'Dana Lee'}, 'SPEAKER_01': {'name': 'Speaker 1'}})
    assert resp.status_code == 200, resp.data
    db.session.expire_all()
    stored = db.session.get(Recording, rec.id)
    assert stored.transcription == '[Dana Lee]: Good morning.\n[Speaker 1]: Thanks.'
    assert stored.participants == 'Dana Lee'
    assert _names(user) == ['Dana Lee']


def test_a_speaker_added_in_the_editor_is_not_stored(world):
    """The editor's Add speaker creates SPEAKER_NN; saving it stores no person."""
    user, rec = world['user'], world['recording']()
    transcript = SEGMENTS + [{'speaker': 'SPEAKER_02', 'sentence': 'A late remark from the back of the room.',
                              'start_time': 80.0, 'end_time': 100.0, 'speaker_id': 999999}]
    resp = _save(_client(user), rec, {}, 'update_transcript', transcript)
    assert resp.status_code == 200, resp.data
    assert _names(user) == []
    assert all('speaker_id' not in s for s in _segments(rec))
    assert not db.session.get(Recording, rec.id).participants
    assert SpeakerVoiceSample.query.filter_by(user_id=user.id).count() == 0


@pytest.mark.parametrize('back_to', ['SPEAKER_00', 'Speaker 1'])
def test_renaming_a_person_back_to_a_label_takes_the_voice_back(world, back_to):
    user, rec = world['user'], world['recording']()
    client = _client(user)
    assert _save(client, rec, {'SPEAKER_00': {'name': 'Dana Lee'}}).status_code == 200
    dana = Speaker.query.filter_by(user_id=user.id).one()
    assert SpeakerVoiceSample.query.filter_by(speaker_id=dana.id).count() == 1

    resp = _save(client, rec, {'Dana Lee': {'name': back_to}})
    assert resp.status_code == 200, resp.data

    segments = _segments(rec)
    assert [s['speaker'] for s in segments] == [back_to, 'SPEAKER_01', back_to, 'SPEAKER_01']
    assert all('speaker_id' not in s for s in segments)
    stored = db.session.get(Recording, rec.id)
    assert not stored.participants
    # The voice of this recording is no longer Dana's; the saved speaker stays.
    assert SpeakerVoiceSample.query.filter_by(user_id=user.id).count() == 0
    assert 'Dana Lee' not in (stored.speaker_label_map or {}).values()
    assert _names(user) == ['Dana Lee']
    assert SpeakerSnippet.query.filter_by(recording_id=rec.id).count() == 0 or \
        all(s.speaker_id == dana.id for s in SpeakerSnippet.query.filter_by(recording_id=rec.id))


def test_api_v1_assign_stores_real_names_only(world):
    user, rec = world['user'], world['recording']()
    client = _client(user)
    with patch('src.file_exporter.ENABLE_AUTO_EXPORT', False):
        resp = client.put(f'/api/v1/recordings/{rec.id}/speakers/assign',
                          json={'speaker_map': {'SPEAKER_00': 'Dana Lee', 'SPEAKER_01': 'SPEAKER_01'}})
    assert resp.status_code == 200, resp.data
    assert resp.get_json()['recording']['participants'] == 'Dana Lee'
    assert _names(user) == ['Dana Lee']
    assert SpeakerVoiceSample.query.filter_by(user_id=user.id).count() == 1


@pytest.mark.parametrize('name', ['SPEAKER_00', 'Speaker 1', 'unknown', 'spk_3'])
def test_a_label_cannot_be_created_or_given_as_a_saved_speaker(world, name):
    user = world['user']
    client = _client(user)
    resp = client.post('/speakers', json={'name': name})
    assert resp.status_code == 400 and resp.get_json()['code'] == 'speaker_name_generic'
    resp = client.post('/api/v1/speakers', json={'name': name})
    assert resp.status_code == 400 and resp.get_json()['code'] == 'invalid_parameter'
    assert _names(user) == []

    real = Speaker(name='Dana Lee', user_id=user.id)
    db.session.add(real)
    db.session.commit()
    resp = client.put(f'/speakers/{real.id}', json={'name': name})
    assert resp.status_code == 400 and resp.get_json()['code'] == 'speaker_name_generic'
    resp = client.put(f'/api/v1/speakers/{real.id}', json={'name': name})
    assert resp.status_code == 400 and resp.get_json()['code'] == 'invalid_parameter'
    db.session.expire_all()
    assert _names(user) == ['Dana Lee']


def test_real_names_are_still_created_and_renamed(world):
    user = world['user']
    client = _client(user)
    assert client.post('/speakers', json={'name': 'Speaker Johnson'}).status_code == 201
    created = client.post('/api/v1/speakers', json={'name': 'A'})
    assert created.status_code == 201
    assert client.put(f"/api/v1/speakers/{created.get_json()['id']}", json={'name': 'Guest 1'}).status_code == 200
    assert _names(user) == ['Guest 1', 'Speaker Johnson']


def test_a_stored_label_row_is_never_listed_matched_or_linked(world):
    """A row written by an earlier release, before the startup cleanup removed it."""
    from src.services import voice_profiles as vp
    from src.services.speaker import find_user_speaker, update_speaker_usage
    from src.services.speaker_links import owner_speakers
    user = world['user']
    rec = world['recording']()
    label = Speaker(name='SPEAKER_00', user_id=user.id, use_count=9)
    real = Speaker(name='Dana Lee', user_id=user.id, use_count=1)
    db.session.add_all([label, real])
    db.session.commit()
    vec = vp.to_bytes(vp.normalize([1.0] + [0.0] * 255))
    db.session.add_all([
        SpeakerVoiceSample(user_id=user.id, speaker_id=label.id, embedding=vec, dimension=256, label='SPEAKER_00'),
        SpeakerVoiceSample(user_id=user.id, speaker_id=real.id, embedding=vec, dimension=256, label='SPEAKER_00'),
    ])
    db.session.commit()
    client = _client(user)

    assert [s['name'] for s in client.get('/speakers').get_json()] == ['Dana Lee']
    assert [s['name'] for s in client.get('/speakers/search?top=1').get_json()] == ['Dana Lee']
    assert client.get('/speakers/search?q=SPEAKER').get_json() == []
    assert [s['name'] for s in client.get('/api/v1/speakers').get_json()['speakers']] == ['Dana Lee']

    # Not a voice-match target, not a saved speaker to find, link or count.
    assert [sp.name for sp, _, _ in vp._user_profiles(user.id, None)] == ['Dana Lee']
    assert find_user_speaker(user.id, 'speaker_00') is None
    by_name, by_id = owner_speakers(user.id)
    assert list(by_name) == ['dana lee'] and list(by_id) == [real.id]
    assert vp.record_sample(label, rec, 'SPEAKER_01', [0.0, 1.0] + [0.0] * 254) == 'invalid'
    with app.test_request_context():
        from flask_login import login_user
        login_user(user)
        update_speaker_usage(['SPEAKER_00', 'Speaker 3'])
    db.session.expire_all()
    assert db.session.get(Speaker, label.id).use_count == 9
    assert _names(user) == ['Dana Lee', 'SPEAKER_00']

    # A segment that points at the row is shown without a person.
    rec.transcription = json.dumps([dict(SEGMENTS[0], speaker_id=label.id)])
    db.session.commit()
    shown = client.get(f'/api/v1/recordings/{rec.id}/speakers').get_json()['speakers']
    assert shown[0]['speaker_id'] is None and shown[0]['identified_name'] is None and shown[0]['label'] == 'SPEAKER_00'


def test_auto_labelling_never_offers_or_accepts_a_label(world):
    from src.services.speaker_identification import _sanitize_identified_map, apply_contextual_auto_labels
    from src.services.speaker_embedding_matcher import apply_speaker_names_to_transcription
    user = world['user']
    labels = ['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_02', 'SPEAKER_03']
    with app.test_request_context():
        cleaned = _sanitize_identified_map(
            {'SPEAKER_00': 'Dana Lee', 'SPEAKER_01': 'Speaker 1', 'SPEAKER_02': 'Unknown Speaker',
             'SPEAKER_03': 'speaker b'}, labels)
    assert cleaned == {'SPEAKER_00': 'Dana Lee', 'SPEAKER_01': '', 'SPEAKER_02': '', 'SPEAKER_03': ''}

    # The candidates of contextual labelling are the real saved names.
    db.session.add_all([Speaker(name='SPEAKER_01', user_id=user.id), Speaker(name='Dana Lee', user_id=user.id)])
    user.auto_speaker_labelling = True
    db.session.commit()
    rec = world['recording'](embeddings=False)
    seen = {}

    def fake_identify(data, user_id, candidate_names=None):
        seen['candidates'] = list(candidate_names)
        return {'SPEAKER_00': 'Dana Lee', 'SPEAKER_01': ''}

    with patch('src.services.speaker_identification.identify_speakers_from_transcript', fake_identify):
        applied = apply_contextual_auto_labels(rec, user)
    assert seen['candidates'] == ['Dana Lee']
    assert applied == {'SPEAKER_00': 'Dana Lee'}

    # A label is never applied as a name by the pipeline.
    rec2 = world['recording'](embeddings=False)
    assert apply_speaker_names_to_transcription(rec2, {'SPEAKER_00': 'Speaker 9'}) is False
    assert [s['speaker'] for s in _segments(rec2)][0] == 'SPEAKER_00'


# ------------------------------------------------------------------ filters

def test_participants_never_hold_a_label(world):
    from src.services.speaker import participants_from_segments
    from src.services.recording_merge import _union_participants
    user = world['user']
    assert participants_from_segments([
        {'speaker': 'Dana'}, {'speaker': 'SPEAKER_01'}, {'speaker': 'UNKNOWN_SPEAKER'}, {'speaker': 'Speaker 2'},
        {'speaker': ' Kim '}, {'speaker': None}, {'speaker': 'Unknown'}, 'not a segment']) == 'Dana, Kim'

    a = world['recording'](participants='Dana, SPEAKER_01')
    b = world['recording'](participants='Kim, Speaker 2, dana')
    assert _union_participants([a, b]) == 'Dana, Kim'

    client = _client(user)
    resp = client.post('/save', json={'id': a.id, 'participants': 'Dana, SPEAKER_03, Kim'})
    if resp.status_code == 404:
        pytest.skip('no /save route in this build')
    assert resp.status_code == 200, resp.data
    db.session.expire_all()
    assert db.session.get(Recording, a.id).participants == 'Dana, Kim'


def test_api_v1_participants_are_cleaned(world):
    user, rec = world['user'], world['recording']()
    client = _client(user)
    resp = client.patch(f'/api/v1/recordings/{rec.id}', json={'participants': 'Dana, SPEAKER_01, Unknown Speaker'})
    assert resp.status_code == 200, resp.data
    db.session.expire_all()
    assert db.session.get(Recording, rec.id).participants == 'Dana'
    resp = client.patch(f'/api/v1/recordings/{rec.id}', json={'participants': 'Dana;  Kim'})
    assert resp.status_code == 200
    db.session.expire_all()
    assert db.session.get(Recording, rec.id).participants == 'Dana;  Kim'


def test_the_recording_list_ignores_a_label_filter(world):
    user = world['user']
    named = world['recording'](participants='Dana Lee')
    unnamed = world['recording']()
    # A value an earlier release stored: it is not matched by a label filter.
    stale = world['recording'](participants='SPEAKER_00')
    client = _client(user)

    def ids(q):
        body = client.get('/api/recordings', query_string={'q': q, 'per_page': 100}).get_json()
        return {r['id'] for r in body['recordings']}

    everything = {named.id, unnamed.id, stale.id}
    assert ids('speaker:Dana_Lee') == {named.id}
    # Ignored, as if the filter was not given: no error and no narrowing.
    assert ids('speaker:SPEAKER_00') == everything
    assert ids('speaker:speaker_1') == everything
    # Beside a real name, only the real name filters.
    assert ids('speaker:SPEAKER_00 speaker:Dana_Lee') == {named.id}


def test_inquire_filters_list_people_only(world):
    import src.api.inquire as inquire_api
    from src.models import InquireSession
    from src.services.assistant_events import filters_from_request
    user = world['user']
    world['recording'](participants='Dana Lee, SPEAKER_01, UNKNOWN_SPEAKER')
    world['recording'](participants='Speaker 2, Kim')
    client = _client(user)
    with patch.object(inquire_api, 'ENABLE_INQUIRE_MODE', True):
        body = client.get('/api/inquire/available_filters').get_json()
        assert body['speakers'] == ['Dana Lee', 'Kim']
        created = client.post('/api/inquire/sessions', json={'filter_speakers': ['SPEAKER_00', 'Dana Lee']})
    assert created.status_code in (200, 201), created.data
    session = InquireSession.query.filter_by(user_id=user.id).order_by(InquireSession.id.desc()).first()
    assert json.loads(session.filter_speakers) == ['Dana Lee']

    assert filters_from_request({'filter_speakers': ['SPEAKER_00', 'Dana Lee', 'speaker 4']}) == {'speaker_names': ['Dana Lee']}
    assert filters_from_request({'filter_speakers': ['SPEAKER_00']}) == {}
    assert filters_from_request({'filter_speakers': ['Dana Lee'], 'filter_tags': [3]}) == \
        {'tag_ids': [3], 'speaker_names': ['Dana Lee']}


def test_api_v1_search_ignores_a_label_as_speaker(world):
    user = world['user']
    rec = world['recording']([
        {'speaker': 'Dana Lee', 'sentence': 'The zebrafish budget is approved.', 'start_time': 0.0, 'end_time': 3.0},
        {'speaker': 'SPEAKER_01', 'sentence': 'The zebrafish contract is late.', 'start_time': 3.0, 'end_time': 6.0},
    ], embeddings=False)
    client = _client(user)

    def hits(**params):
        resp = client.get('/api/v1/search', query_string={'q': 'zebrafish', 'mode': 'keyword',
                                                         'recording_ids': str(rec.id), **params})
        assert resp.status_code == 200, resp.data
        return [h for h in resp.get_json()['results'] if h['field'] == 'transcript']

    assert len(hits()) == 2
    assert [h['speaker'] for h in hits(speaker='Dana Lee')] == ['Dana Lee']
    # A label names nobody: the parameter is ignored, the request succeeds.
    assert len(hits(speaker='SPEAKER_01')) == 2
    assert len(hits(speaker='Speaker 1')) == 2


def test_a_label_for_a_value_the_transcript_does_not_show_changes_nothing(world):
    """API clients send {"SPEAKER_00": "SPEAKER_00"} for speakers they leave
    alone: a person named earlier keeps the voice sample."""
    user, rec = world['user'], world['recording']()
    client = _client(user)
    assert _save(client, rec, {'SPEAKER_00': {'name': 'Dana Lee'}}).status_code == 200
    assert SpeakerVoiceSample.query.filter_by(user_id=user.id).count() == 1
    resp = _save(client, rec, {'SPEAKER_00': {'name': 'SPEAKER_00'}, 'SPEAKER_01': {'name': 'SPEAKER_01'}})
    assert resp.status_code == 200, resp.data
    assert [s['speaker'] for s in _segments(rec)] == ['Dana Lee', 'SPEAKER_01', 'Dana Lee', 'SPEAKER_01']
    assert SpeakerVoiceSample.query.filter_by(user_id=user.id).count() == 1
    assert db.session.get(Recording, rec.id).speaker_label_map == {'SPEAKER_00': 'Dana Lee'}
    assert db.session.get(Recording, rec.id).participants == 'Dana Lee'
    assert _names(user) == ['Dana Lee']
