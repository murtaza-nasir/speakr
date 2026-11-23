"""Diarization labels of a transcription service are stored as SPEAKER_NN.

Services number voices in different ways ("A", "S01", "SPEAKER_0").
diarization_label_map() in src/utils/speaker_labels.py brings them to one
form, and TranscriptionResponse.to_storage_format() applies it to every
result before it is stored. The tests cover the mapping, each connector's
own labels through the stored result, and the paths that store a result:
upload and reprocessing (one function), chunked files and incognito mode.

SHARED-DB: assertions are scoped to the rows the test made.
"""

import json
import os
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
import src.tasks.processing as proc
from src.services.transcription.base import (
    ConnectorSpecifications, TranscriptionResponse, TranscriptionSegment,
)
from src.utils.speaker_labels import diarization_label_map, is_generic_speaker_label, raw_label_scheme


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------

def test_letters_take_the_number_of_their_position():
    assert diarization_label_map(['A', 'B', 'A', 'C']) == {
        'A': 'SPEAKER_00', 'B': 'SPEAKER_01', 'C': 'SPEAKER_02'}
    # The order of the turns does not matter, and a gap stays a gap.
    assert diarization_label_map(['C', 'A']) == {'C': 'SPEAKER_02', 'A': 'SPEAKER_00'}
    assert diarization_label_map(['b', 'Speaker A']) == {'b': 'SPEAKER_01', 'Speaker A': 'SPEAKER_00'}


def test_normalized_labels_are_left_alone():
    assert diarization_label_map(['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_12', 'SPEAKER_100']) == {}


def test_a_label_that_carries_a_number_keeps_it():
    # SPEAKER_10 is normalized already and is not part of the mapping.
    assert diarization_label_map(['SPEAKER_0', 'SPEAKER_1', 'SPEAKER_10']) == {
        'SPEAKER_0': 'SPEAKER_00', 'SPEAKER_1': 'SPEAKER_01'}
    assert diarization_label_map(['S01', 'S02']) == {'S01': 'SPEAKER_01', 'S02': 'SPEAKER_02'}
    assert diarization_label_map(['speaker_0', 'Speaker 2', 'spk_3']) == {
        'speaker_0': 'SPEAKER_00', 'Speaker 2': 'SPEAKER_02', 'spk_3': 'SPEAKER_03'}
    assert diarization_label_map([0, 1, '2']) == {0: 'SPEAKER_00', 1: 'SPEAKER_01', '2': 'SPEAKER_02'}


def test_mixed_schemes_never_share_a_number():
    # SPEAKER_01 exists: B cannot have it and receives the lowest free number.
    assert diarization_label_map(['SPEAKER_01', 'A', 'B', 'Dana', None, 'Unknown']) == {
        'A': 'SPEAKER_00', 'B': 'SPEAKER_02'}
    # The first label to ask for a number keeps it; a number another label
    # asked for is not handed out.
    assert diarization_label_map(['0', 'A', 'B']) == {'0': 'SPEAKER_00', 'B': 'SPEAKER_01', 'A': 'SPEAKER_02'}
    mapping = diarization_label_map(['S01', 'SPEAKER_01', 'speaker_1', 'B', 1])
    assert 'SPEAKER_01' not in mapping
    assert len(set(mapping.values())) == len(mapping) == 4
    assert 'SPEAKER_01' not in mapping.values()


def test_more_than_26_speakers():
    letters = [chr(ord('A') + n) for n in range(26)] + ['AA', 'AB']
    mapping = diarization_label_map(letters)
    assert mapping['Z'] == 'SPEAKER_25' and mapping['AA'] == 'SPEAKER_26' and mapping['AB'] == 'SPEAKER_27'
    assert len(set(mapping.values())) == 28
    numbered = diarization_label_map([f'SPEAKER_{n}' for n in range(120)])
    assert numbered == {f'SPEAKER_{n}': f'SPEAKER_0{n}' for n in range(10)}
    spoken = diarization_label_map([f'Speaker {n}' for n in range(1, 121)])
    assert spoken['Speaker 9'] == 'SPEAKER_09' and spoken['Speaker 120'] == 'SPEAKER_120'
    assert len(set(spoken.values())) == 120


def test_empty_and_unknown_values_are_not_labels_to_map():
    assert diarization_label_map(None) == {}
    assert diarization_label_map([]) == {}
    assert diarization_label_map([None, '', '   ', True, 1.5, ['A'], {'a': 1}]) == {}
    assert diarization_label_map(['Unknown', 'UNKNOWN_SPEAKER', 'Unknown Speaker', 'Unknown Speaker 2',
                                  'speaker', 'spk']) == {}


def test_text_without_a_number_or_letter_position_is_taken_for_a_name():
    names = ['Dana', 'Dana Lee', 'Jo', 'Ed', 'Interviewer', 'Keynote Speaker', 'Speaker Johnson',
             'Agent 007 of 9', 'R2D2', '12345', 'Ünal']
    assert diarization_label_map(names) == {}
    for name in names:
        assert raw_label_scheme(name) == (None, None)


def test_the_mapping_is_deterministic_and_its_results_are_generic_labels():
    labels = ['B', 'S03', 'SPEAKER_0', 'A', 'Dana', 2, 'SPEAKER_04']
    first = diarization_label_map(labels)
    assert all(diarization_label_map(labels) == first for _ in range(5))
    assert all(is_generic_speaker_label(target) for target in first.values())
    assert all(raw_label_scheme(target)[0] == 'canonical' for target in first.values())
    # A second pass over the result finds nothing to change.
    assert diarization_label_map([first.get(label, label) for label in labels]) == {}


def test_schemes():
    assert raw_label_scheme('SPEAKER_07') == ('canonical', 7)
    assert raw_label_scheme('speaker_07') == ('generic_number', 7)
    assert raw_label_scheme('SPEAKER_7') == ('generic_number', 7)
    assert raw_label_scheme('A') == ('letter', 0)
    assert raw_label_scheme('AA') == ('letter', 26)
    assert raw_label_scheme('Aa') == (None, None)
    assert raw_label_scheme('3') == ('number', 3)
    assert raw_label_scheme('S01') == ('prefixed_number', 1)
    assert raw_label_scheme('Guest 1') == ('prefixed_number', 1)


# ---------------------------------------------------------------------------
# One result: everything keyed by a label changes together
# ---------------------------------------------------------------------------

def _response(labels, **fields):
    segments = [TranscriptionSegment(text=f'turn {n}', speaker=label, start_time=float(n), end_time=n + 1.0)
                for n, label in enumerate(labels)]
    return TranscriptionResponse(text='\n'.join(f'[{label}]: turn {n}' for n, label in enumerate(labels)),
                                 segments=segments, **fields)


def _stored_speakers(response):
    return [segment['speaker'] for segment in json.loads(response.to_storage_format())]


def test_segments_speakers_embeddings_and_text_agree_after_storage():
    raw = {'speaker_embeddings': {'B': [0.2], 'A': [0.1]}}
    response = _response(['B', 'A', 'B'], speakers=['A', 'B'],
                         speaker_embeddings=dict(raw['speaker_embeddings']), raw_response=raw)
    assert _stored_speakers(response) == ['SPEAKER_01', 'SPEAKER_00', 'SPEAKER_01']
    assert [s.speaker for s in response.segments] == ['SPEAKER_01', 'SPEAKER_00', 'SPEAKER_01']
    assert response.speakers == ['SPEAKER_00', 'SPEAKER_01']
    assert response.speaker_embeddings == {'SPEAKER_01': [0.2], 'SPEAKER_00': [0.1]}
    assert response.text == '[SPEAKER_01]: turn 0\n[SPEAKER_00]: turn 1\n[SPEAKER_01]: turn 2'
    assert response.raw_response == {'speaker_embeddings': {'B': [0.2], 'A': [0.1]}}
    # A second call changes nothing.
    assert response.normalize_speaker_labels() == {}
    assert _stored_speakers(response) == ['SPEAKER_01', 'SPEAKER_00', 'SPEAKER_01']


def test_a_normalized_result_is_stored_unchanged():
    response = _response(['SPEAKER_00', 'SPEAKER_01'], speakers=['SPEAKER_00', 'SPEAKER_01'],
                         speaker_embeddings={'SPEAKER_00': [1.0], 'SPEAKER_01': [2.0]})
    text = response.text
    assert response.normalize_speaker_labels() == {}
    assert _stored_speakers(response) == ['SPEAKER_00', 'SPEAKER_01']
    assert response.text == text and list(response.speaker_embeddings) == ['SPEAKER_00', 'SPEAKER_01']


def test_text_is_changed_at_line_prefixes_only():
    response = TranscriptionResponse(
        text='[A]: Plan A is ready [A]: so\n[B]: A says so',
        segments=[TranscriptionSegment(text='x', speaker='A'), TranscriptionSegment(text='y', speaker='B')])
    response.normalize_speaker_labels()
    assert response.text == '[SPEAKER_00]: Plan A is ready [A]: so\n[SPEAKER_01]: A says so'


def test_a_segment_without_a_speaker_and_a_name_keep_their_value():
    response = _response(['A', None, 'Dana', 'Unknown'])
    assert _stored_speakers(response) == ['SPEAKER_00', 'Unknown Speaker', 'Dana', 'Unknown']


def test_a_result_without_segments_is_stored_as_its_text():
    assert TranscriptionResponse(text='[A]: said by nobody').to_storage_format() == '[A]: said by nobody'


# ---------------------------------------------------------------------------
# Each connector's own labels, through the stored result
# ---------------------------------------------------------------------------

def test_assemblyai_letters_are_stored_normalized():
    from src.services.transcription.connectors.assemblyai import AssemblyAITranscriptionConnector
    connector = AssemblyAITranscriptionConnector({'api_key': 'fake', 'poll_interval': 0})
    response = connector._parse_result({'text': 'Hello Hi', 'utterances': [
        {'speaker': 'A', 'text': 'Hello', 'start': 0, 'end': 900},
        {'speaker': 'B', 'text': 'Hi', 'start': 1000, 'end': 1500},
        {'speaker': 'A', 'text': 'Bye', 'start': 1600, 'end': 2000}]})
    assert response.speakers == ['A', 'B']       # what the service returned
    assert _stored_speakers(response) == ['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_00']
    assert response.speakers == ['SPEAKER_00', 'SPEAKER_01']


def test_openai_diarize_letters_are_stored_normalized():
    from src.services.transcription.connectors.openai_transcribe import OpenAITranscribeConnector
    connector = OpenAITranscribeConnector({'api_key': 'fake', 'model': 'gpt-4o-transcribe-diarize'})
    as_dicts = connector._parse_diarized_response({'segments': [
        {'speaker': 'B', 'text': 'Hello', 'start': 0.0, 'end': 1.0},
        {'speaker': 'A', 'text': 'Hi', 'start': 1.0, 'end': 2.0}]})
    assert _stored_speakers(as_dicts) == ['SPEAKER_01', 'SPEAKER_00']
    assert as_dicts.text == '[SPEAKER_01]: Hello\n[SPEAKER_00]: Hi'
    as_objects = connector._parse_diarized_response(SimpleNamespace(segments=[
        SimpleNamespace(speaker='A', text='Hello', start=0.0, end=1.0),
        SimpleNamespace(speaker='B', text='Hi', start=1.0, end=2.0)]))
    assert _stored_speakers(as_objects) == ['SPEAKER_00', 'SPEAKER_01']


def test_azure_openai_diarize_letters_are_stored_normalized():
    from src.services.transcription.connectors.azure_openai_transcribe import AzureOpenAITranscribeConnector
    connector = AzureOpenAITranscribeConnector({
        'api_key': 'fake', 'endpoint': 'https://example.invalid', 'deployment_name': 'diarize'})
    response = connector._parse_diarized_response({'segments': [
        {'speaker': 'A', 'text': 'Hello', 'start': 0.0, 'end': 1.0},
        {'speaker': 'B', 'text': 'Hi', 'start': 1.0, 'end': 2.0}]})
    assert _stored_speakers(response) == ['SPEAKER_00', 'SPEAKER_01']


def test_mossland_labels_are_stored_normalized():
    from src.services.transcription.connectors.mossland import MosslandTranscriptionConnector
    segments = MosslandTranscriptionConnector._segments([
        {'speaker': 'S01', 'text': 'One', 'start': 0, 'end': 1},
        {'speaker': 'S02', 'text': 'two', 'start': 1, 'end': 2}])
    response = MosslandTranscriptionConnector._result('One two', segments, 'moss-transcribe-diarize')
    assert response.speakers == ['S01', 'S02']
    assert _stored_speakers(response) == ['SPEAKER_01', 'SPEAKER_02']


def test_funasr_labels_are_stored_with_two_digits():
    from src.services.transcription.connectors.alibaba_funasr import AlibabaFunASRConnector
    connector = AlibabaFunASRConnector({'base_url': 'https://example.invalid/api/v1', 'api_key': 'fake'})
    response = connector._parse_transcription_data({'transcripts': [{'sentences': [
        {'text': 'One', 'speaker_id': 0, 'begin_time': 0, 'end_time': 900},
        {'text': 'two', 'speaker_id': 1, 'begin_time': 1000, 'end_time': 1900},
        {'text': 'ten', 'speaker_id': 10, 'begin_time': 2000, 'end_time': 2900}]}]})
    assert response.speakers == ['SPEAKER_0', 'SPEAKER_1', 'SPEAKER_10']
    assert _stored_speakers(response) == ['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_10']
    assert response.text.splitlines()[0] == '[SPEAKER_00]: One'


def test_mistral_labels_are_stored_normalized():
    from src.services.transcription.connectors.mistral import MistralTranscriptionConnector
    connector = MistralTranscriptionConnector({'api_key': 'fake'})
    segments = connector._parse_segments([
        {'text': 'One', 'speaker_id': 'speaker_0', 'start': 0.0, 'end': 1.0},
        {'text': 'two', 'speaker_id': 'speaker_1', 'start': 1.0, 'end': 2.0}])
    response = TranscriptionResponse(text='One two', segments=segments, speakers=['speaker_0', 'speaker_1'])
    assert _stored_speakers(response) == ['SPEAKER_00', 'SPEAKER_01']


def test_vibevoice_labels_are_stored_normalized():
    from src.services.transcription.connectors.vibevoice import VibeVoiceTranscriptionConnector
    connector = VibeVoiceTranscriptionConnector({'base_url': 'http://127.0.0.1:8000'})
    numbered = connector._parse_segments([
        {'Speaker': 0, 'Content': 'One', 'Start': 0.0, 'End': 1.0},
        {'Content': '[Silence]', 'Start': 1.0, 'End': 1.5},
        {'Speaker': 1, 'Content': 'two', 'Start': 1.5, 'End': 2.0}])
    assert [s.speaker for s in numbered] == ['SPEAKER_00', 'SPEAKER_00', 'SPEAKER_01']
    assert _stored_speakers(TranscriptionResponse(text='', segments=numbered)) == [
        'SPEAKER_00', 'SPEAKER_00', 'SPEAKER_01']
    # A speaker value that is text passes through the connector as it is.
    as_text = connector._parse_segments([
        {'Speaker': '0', 'Content': 'One', 'Start': 0.0, 'End': 1.0},
        {'Speaker': 'Speaker 1', 'Content': 'two', 'Start': 1.0, 'End': 2.0},
        {'Speaker': 'Dana', 'Content': 'three', 'Start': 2.0, 'End': 3.0}])
    assert _stored_speakers(TranscriptionResponse(text='', segments=as_text)) == [
        'SPEAKER_00', 'SPEAKER_01', 'Dana']


def test_tencent_labels_are_already_normalized():
    from src.services.transcription.connectors.tencent_asr import TencentASRConnector
    connector = TencentASRConnector({'secret_id': 'test-id', 'secret_key': 'test-secret'})
    response = connector._parse_result({'ResultDetail': [
        {'FinalSentence': 'One', 'SpeakerId': 0, 'StartMs': 0, 'EndMs': 900},
        {'FinalSentence': 'two', 'SpeakerId': 1, 'StartMs': 1000, 'EndMs': 1900}]}, 'model', True)
    assert response.normalize_speaker_labels() == {}
    assert _stored_speakers(response) == ['SPEAKER_00', 'SPEAKER_01']


def test_whisperx_endpoint_labels_and_embeddings_are_unchanged():
    from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
    connector = ASREndpointConnector({'base_url': 'http://127.0.0.1:9000'})
    response = connector._parse_response({
        'text': 'One two', 'language': 'en',
        'segments': [{'speaker': 'SPEAKER_01', 'text': 'One', 'start': 0.0, 'end': 1.0},
                     {'text': 'and', 'start': 1.0, 'end': 1.2},
                     {'speaker': 'SPEAKER_00', 'text': 'two', 'start': 1.2, 'end': 2.0}],
        'speaker_embeddings': {'SPEAKER_00': [0.1, 0.2], 'SPEAKER_01': [0.3, 0.4]}})
    assert response.normalize_speaker_labels() == {}
    assert _stored_speakers(response) == ['SPEAKER_01', 'SPEAKER_01', 'SPEAKER_00']
    assert response.speaker_embeddings == {'SPEAKER_00': [0.1, 0.2], 'SPEAKER_01': [0.3, 0.4]}


def test_asr_endpoint_with_other_labels_keeps_embeddings_on_the_same_voice():
    """A custom ASR webservice may use its own labels for segments and embeddings."""
    from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector
    connector = ASREndpointConnector({'base_url': 'http://127.0.0.1:9000'})
    response = connector._parse_response({
        'segments': [{'speaker': 'spk_1', 'text': 'One', 'start': 0.0, 'end': 1.0},
                     {'speaker': 'spk_0', 'text': 'two', 'start': 1.0, 'end': 2.0}],
        'speaker_embeddings': {'spk_0': [0.1], 'spk_1': [0.3]}})
    assert _stored_speakers(response) == ['SPEAKER_01', 'SPEAKER_00']
    assert response.speaker_embeddings == {'SPEAKER_00': [0.1], 'SPEAKER_01': [0.3]}


def test_openasr_labels_and_embeddings_stay_in_step():
    from src.services.transcription.connectors.openasr import OpenASRTranscriptionConnector
    connector = OpenASRTranscriptionConnector({'base_url': 'http://127.0.0.1:8080', 'model': 'm'})
    data = {'text': 'One two',
            'segments': [{'speaker': 'SPEAKER_2', 'text': 'One', 'start': 0.0, 'end': 1.0},
                         {'speaker': 'SPEAKER_10', 'text': 'two', 'start': 1.0, 'end': 2.0}],
            'speaker_embeddings': {'SPEAKER_2': [0.1], 'SPEAKER_10': [0.3]}}
    response = connector._parse_response(data, 'm', True)
    assert _stored_speakers(response) == ['SPEAKER_02', 'SPEAKER_10']
    assert response.speaker_embeddings == {'SPEAKER_02': [0.1], 'SPEAKER_10': [0.3]}
    assert response.speakers == ['SPEAKER_02', 'SPEAKER_10']


# ---------------------------------------------------------------------------
# The paths that store a result
# ---------------------------------------------------------------------------

class _FakeConnector:
    """Returns a prepared response per call and records the requests."""
    PROVIDER_NAME = 'label-test'
    SUPPORTS_TASK_RESUME = False
    specifications = ConnectorSpecifications()
    supports_diarization = True
    default_diarize = True
    model = 'label-test-model'

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def supports(self, capability):
        return False

    def transcribe(self, request):
        self.requests.append(SimpleNamespace(known_speaker_names=request.known_speaker_names,
                                             known_speaker_references=request.known_speaker_references))
        return self.responses.pop(0)


def _letters_response(labels=('A', 'B', 'A'), embeddings=None):
    return _response(list(labels), speakers=sorted(set(labels)), speaker_embeddings=embeddings)


@pytest.fixture
def recording():
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f'dln_{suffix}', email=f'dln_{suffix}@local.test', password='x')
        db.session.add(user)
        db.session.commit()
        rec = Recording(user_id=user.id, title='labels', original_filename='labels.mp3',
                        audio_path='local://labels.mp3', status='PENDING')
        db.session.add(rec)
        db.session.commit()
        path = os.path.join(app.config['UPLOAD_FOLDER'], f'dln_{rec.id}.mp3')
        with open(path, 'wb') as handle:
            handle.write(b'\x00' * 1024)
        yield SimpleNamespace(id=rec.id, path=path)
        if os.path.exists(path):
            os.remove(path)


def _ingest(recording, connector, needs_chunking=False, task=None):
    converted = MagicMock()
    converted.was_converted = False
    with patch('src.services.transcription.get_connector', return_value=connector), \
         patch.object(proc, 'is_video_file', return_value=False), \
         patch.object(proc, 'convert_if_needed', return_value=converted), \
         patch.object(proc.llm_config, 'is_configured', return_value=True), \
         patch.object(proc, 'ENABLE_INQUIRE_MODE', False), \
         patch.object(proc, 'generate_title_task'), \
         patch.object(proc, 'generate_summary_only_task'), \
         patch.object(proc, 'chunking_service') as chunker:
        chunker.needs_chunking.return_value = needs_chunking
        chunker.get_audio_duration.return_value = 120.0
        if task is not None:
            return task(chunker)
        return proc.transcribe_with_connector(
            app.app_context(), recording.id, recording.path, 'labels.mp3', time.time(), mime_type='audio/mpeg')


def _stored(recording_id):
    db.session.expire_all()
    rec = db.session.get(Recording, recording_id)
    embeddings = rec.speaker_embeddings
    if isinstance(embeddings, str):
        embeddings = json.loads(embeddings)
    return [s['speaker'] for s in json.loads(rec.transcription)], embeddings


def test_upload_stores_normalized_labels_and_embedding_keys(recording):
    with app.app_context():
        connector = _FakeConnector(_letters_response(embeddings={'A': [0.1, 0.2], 'B': [0.3, 0.4]}))
        _ingest(recording, connector)
        speakers, embeddings = _stored(recording.id)
        assert speakers == ['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_00']
        assert embeddings == {'SPEAKER_00': [0.1, 0.2], 'SPEAKER_01': [0.3, 0.4]}


def test_reprocessing_stores_normalized_labels(recording):
    """A reprocess job runs transcribe_audio_task, as the first transcription does."""
    with app.app_context():
        rec = db.session.get(Recording, recording.id)
        rec.transcription = json.dumps([{'speaker': 'A', 'sentence': 'old', 'start_time': 0.0, 'end_time': 1.0}])
        rec.speaker_embeddings = {'A': [9.0]}
        db.session.commit()
        connector = _FakeConnector(_letters_response(('B', 'A'), embeddings={'A': [0.1], 'B': [0.3]}))
        from datetime import datetime
        with patch('src.services.transcription.get_connector', return_value=connector), \
             patch.object(proc, 'is_video_file', return_value=False), \
             patch.object(proc, 'convert_if_needed', return_value=MagicMock(was_converted=False)), \
             patch.object(proc.llm_config, 'is_configured', return_value=True), \
             patch.object(proc, 'ENABLE_INQUIRE_MODE', False), \
             patch.object(proc, 'generate_title_task'), \
             patch.object(proc, 'generate_summary_only_task'), \
             patch.object(proc, 'chunking_service') as chunker:
            chunker.needs_chunking.return_value = False
            chunker.get_audio_duration.return_value = 120.0
            proc.transcribe_audio_task(app.app_context(), recording.id, recording.path, 'labels.mp3',
                                       datetime.utcnow())
        speakers, embeddings = _stored(recording.id)
        assert speakers == ['SPEAKER_01', 'SPEAKER_00']
        assert embeddings == {'SPEAKER_00': [0.1], 'SPEAKER_01': [0.3]}


def _chunks(tmp_path, count):
    chunks = []
    for index in range(count):
        path = tmp_path / f'chunk{index}.mp3'
        path.write_bytes(b'chunk audio %d' % index)
        chunks.append({'index': index, 'path': str(path), 'filename': path.name, 'size_mb': 0.1,
                       'start_time': index * 10.0, 'end_time': (index + 1) * 10.0, 'duration': 10.0})
    return chunks


def test_chunked_file_is_merged_with_normalized_labels(recording, tmp_path, monkeypatch):
    monkeypatch.setattr(proc.time, 'sleep', lambda seconds: None)
    with app.app_context():
        connector = _FakeConnector(_letters_response(('A', 'B')), _letters_response(('B', 'A')))

        def run(chunker):
            chunker.create_chunks.return_value = _chunks(tmp_path, 2)
            with patch('src.audio_chunking.extract_speaker_samples', return_value={'A': 'a.mp3', 'B': 'b.mp3'}), \
                 patch('src.audio_chunking.samples_to_data_urls',
                       return_value={'A': 'data:audio/mpeg;base64,AA', 'B': 'data:audio/mpeg;base64,BB'}):
                return proc.transcribe_with_connector(
                    app.app_context(), recording.id, recording.path, 'labels.mp3', time.time(),
                    mime_type='audio/mpeg')

        _ingest(recording, connector, needs_chunking=True, task=run)
        # The service is sent the labels it returned for the first chunk.
        assert connector.requests[0].known_speaker_names is None
        assert connector.requests[1].known_speaker_names == ['A', 'B']
        assert set(connector.requests[1].known_speaker_references) == {'A', 'B'}
        speakers, _ = _stored(recording.id)
        # Chunk 1 keeps the numbers of its letters; chunk 2 is numbered after it.
        assert speakers == ['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_03', 'SPEAKER_02']


def test_chunk_merge_returns_normalized_text_and_speakers(tmp_path, monkeypatch):
    monkeypatch.setattr(proc.time, 'sleep', lambda seconds: None)
    chunker = MagicMock()
    chunker.create_chunks.return_value = _chunks(tmp_path, 2)
    monkeypatch.setattr(proc, 'chunking_service', chunker)
    connector = _FakeConnector(_letters_response(('S01', 'S02')), _letters_response(('S01',)))
    with app.app_context(), patch('src.audio_chunking.extract_speaker_samples', return_value={}):
        merged = proc.transcribe_chunks_with_connector(
            connector, str(tmp_path / 'long.mp3'), 'long.mp3', 'audio/mpeg', 'en', diarize=True)
    assert merged.speakers == ['SPEAKER_01', 'SPEAKER_02', 'SPEAKER_03']
    assert merged.text == '[SPEAKER_01]: turn 0\n[SPEAKER_02]: turn 1\n[SPEAKER_03]: turn 0'
    assert [s['speaker'] for s in json.loads(merged.to_storage_format())] == [
        'SPEAKER_01', 'SPEAKER_02', 'SPEAKER_03']


def _incognito(tmp_path, connector, needs_chunking=False, chunker=None):
    upload = tmp_path / 'visit.mp3'
    upload.write_bytes(b'\x00' * 2048)
    registry = SimpleNamespace(get_active_connector=lambda: connector)
    from src.utils.audio_conversion import ConversionResult
    same = ConversionResult(output_path=str(upload), mime_type='audio/mpeg', was_converted=False,
                            was_compressed=False, original_size=1, final_size=1,
                            original_codec='mp3', final_codec='mp3')
    with app.app_context(), \
         patch('src.services.transcription.get_registry', return_value=registry), \
         patch.object(proc, 'convert_if_needed', return_value=same), \
         patch.object(proc.llm_config, 'is_configured', return_value=False), \
         patch.object(proc, 'chunking_service', chunker or MagicMock()) as service:
        service.needs_chunking.return_value = needs_chunking
        return proc.transcribe_incognito(str(upload), 'visit.mp3', language='en')


def test_incognito_result_carries_normalized_labels(tmp_path):
    result = _incognito(tmp_path, _FakeConnector(_letters_response(('B', 'A', 'B'))))
    assert result['error'] is None
    assert [s['speaker'] for s in json.loads(result['transcription'])] == [
        'SPEAKER_01', 'SPEAKER_00', 'SPEAKER_01']


def test_incognito_chunked_result_carries_normalized_labels(tmp_path, monkeypatch):
    monkeypatch.setattr(proc.time, 'sleep', lambda seconds: None)
    chunker = MagicMock()
    chunker.create_chunks.return_value = _chunks(tmp_path, 2)
    connector = _FakeConnector(_letters_response(('A', 'B')), _letters_response(('A',)))
    with patch('src.audio_chunking.extract_speaker_samples', return_value={}):
        result = _incognito(tmp_path, connector, needs_chunking=True, chunker=chunker)
    assert result['error'] is None
    assert [s['speaker'] for s in json.loads(result['transcription'])] == [
        'SPEAKER_00', 'SPEAKER_01', 'SPEAKER_02']
