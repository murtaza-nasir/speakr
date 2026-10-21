"""Unified saves preserve speaker effects without replaying full-map usage."""
import json
from unittest.mock import patch
import pytest
from tests.test_speaker_save_paths import _user, _recording, _client, EMB, SEGMENTS
from src.app import app, db
from src.models import Recording, Speaker, SpeakerVoiceSample, SpeakerSnippet

@pytest.fixture
def ctx():
    with app.app_context(), patch('src.app.ENABLE_INTERNAL_SHARING', True):
        yield

def body(segments, changes=None, training=None, invalid=None):
    return {'transcript_data': segments, 'workspace_effects': {
        'assignment_changes': changes or [], 'eligible_training_assignments': training or {},
        'invalidated_voice_labels': invalid or []}}

def change(prior, name, label=None):
    return dict(current_before=prior, final_name=name, **({'source_label':label} if label else {}))

def test_rename_autosave_then_correct_preserves_provenance_and_full_snippets(ctx):
    user = _user(); rec = _recording(user, SEGMENTS, {'SPEAKER_00':EMB, 'SPEAKER_01':EMB})
    client = _client(user)
    draft = json.loads(rec.transcription)
    draft[0]['speaker']='Ana'; draft[1]['speaker']='John'
    with patch('src.services.voice_profiles.MIN_SPEECH_SECONDS', 0):
        result = client.post(f'/recording/{rec.id}/update_transcript', json=body(draft,
            [change('SPEAKER_00','Ana','SPEAKER_00'),change('SPEAKER_01','John','SPEAKER_01')],
            {'SPEAKER_00':'Ana','SPEAKER_01':'John'}))
    assert result.status_code == 200
    assert result.json['persistence_status']=='saved'
    ana=Speaker.query.filter_by(user_id=user.id,name='Ana').one()
    john=Speaker.query.filter_by(user_id=user.id,name='John').one()
    assert ana.use_count==john.use_count==1
    draft=json.loads(rec.transcription); draft[0]['speaker']='Boris'
    with patch('src.services.voice_profiles.MIN_SPEECH_SECONDS',0):
        result=client.post(f'/recording/{rec.id}/update_transcript',json=body(draft,
            [change('Ana','Boris','SPEAKER_00'),change('John','John','SPEAKER_01')],{'SPEAKER_00':'Boris'}))
    assert result.status_code==200
    boris=Speaker.query.filter_by(user_id=user.id,name='Boris').one()
    assert boris.use_count==1 and john.use_count==1
    assert rec.speaker_label_map['SPEAKER_00']=='Boris'
    assert SpeakerVoiceSample.query.filter_by(recording_id=rec.id,label='SPEAKER_00').one().speaker_id==boris.id
    assert {s.speaker_id for s in SpeakerSnippet.query.filter_by(recording_id=rec.id)}=={john.id,boris.id}
    draft=json.loads(rec.transcription); draft[0]['sentence']='Only the text changed here.'
    result=client.post(f'/recording/{rec.id}/update_transcript',json=body(draft))
    assert result.status_code==200 and result.json['follow_up']['usage']=='skipped'
    assert boris.use_count==john.use_count==1

@pytest.mark.parametrize('first', ['Ana','SPEAKER_00'])
def test_mixed_split_removes_stale_embedding_map_and_actor_sample(ctx,first):
    user=_user(); rec=_recording(user,SEGMENTS,{'SPEAKER_00':EMB,'SPEAKER_01':EMB})
    client=_client(user); draft=json.loads(rec.transcription); draft[0]['speaker']='Ana'
    with patch('src.services.voice_profiles.MIN_SPEECH_SECONDS',0):
        assert client.post(f'/recording/{rec.id}/update_transcript',json=body(draft,
            [change('SPEAKER_00','Ana','SPEAKER_00')],{'SPEAKER_00':'Ana'})).status_code==200
    ana=Speaker.query.filter_by(user_id=user.id,name='Ana').one()
    draft=json.loads(rec.transcription); original=draft.pop(0)
    draft[0:0]=[{**original,'speaker':first,'end_time':1.5},{**original,'speaker':'Boris','start_time':1.5}]
    result=client.post(f'/recording/{rec.id}/update_transcript',json=body(draft,
        [change('Ana',first,'SPEAKER_00'),change('Ana','Boris','SPEAKER_00')],invalid=['SPEAKER_00']))
    assert result.status_code==200
    assert 'SPEAKER_00' not in (rec.speaker_embeddings or {})
    assert 'SPEAKER_00' not in (rec.speaker_label_map or {})
    assert 'SPEAKER_01' in rec.speaker_embeddings
    assert not SpeakerVoiceSample.query.filter_by(user_id=user.id,recording_id=rec.id,label='SPEAKER_00').first()
    assert db.session.get(Speaker,ana.id) is not None and ana.embedding_count==0
    assert client.get(f'/recording/{rec.id}/workspace_context').json['voice_labels']==['SPEAKER_01']

def test_validation_before_any_effect_and_empty_save(ctx):
    user=_user(); rec=_recording(user,SEGMENTS,{'SPEAKER_00':EMB}); original=rec.transcription
    draft=json.loads(original); draft[0]['speaker']='Ana'
    result=_client(user).post(f'/recording/{rec.id}/update_transcript',json=body(draft,
        [change('Foreign','Ana','SPEAKER_00')],{'SPEAKER_00':'Ana'}))
    assert result.status_code==400 and rec.transcription==original
    assert Speaker.query.filter_by(user_id=user.id).count()==0
    result=_client(user).post(f'/recording/{rec.id}/update_transcript',json=body([]))
    assert result.status_code==200 and rec.transcription=='[]' and rec.participants==''

def test_follow_up_failure_does_not_report_main_save_failed(ctx):
    user=_user(); rec=_recording(user,SEGMENTS); draft=json.loads(rec.transcription); draft[0]['sentence']='Corrected transcript text.'
    with patch('src.api.recordings.export_recording',side_effect=RuntimeError('fixture export failure')):
        result=_client(user).post(f'/recording/{rec.id}/update_transcript',json=body(draft))
    assert result.status_code==200 and result.json['follow_up']['export']=='failed'
    assert json.loads(rec.transcription)[0]['sentence']=='Corrected transcript text.'

def test_llm_receives_transient_draft_without_persisting(ctx):
    user=_user(); rec=_recording(user,SEGMENTS); original=rec.transcription
    draft=json.loads(original); draft[0]['sentence']='Unsaved speaker clue Ana.'
    with patch('src.services.speaker_identification.identify_speakers_from_transcript',return_value={'SPEAKER_00':'Ana'}) as identify:
        result=_client(user).post(f'/recording/{rec.id}/auto_identify_speakers',json={'transcript_data':draft})
    assert result.status_code==200
    assert identify.call_args.args[0][0]['sentence']=='Unsaved speaker clue Ana.'
    assert rec.transcription==original

def test_empty_to_new_cyrillic_speaker_creates_owner_link_and_usage(ctx):
    user=_user(); rec=_recording(user,[])
    result=_client(user).post(f'/recording/{rec.id}/update_transcript',json=body([
        {'speaker':'Борис','sentence':'Новая реплика для нового спикера.', 'start_time':0,'end_time':5}]))
    assert result.status_code==200 and result.json['follow_up']['usage']=='done'
    speaker=Speaker.query.filter_by(user_id=user.id,name='Борис').one()
    assert speaker.use_count==1
    assert json.loads(rec.transcription)[0]['speaker_id']==speaker.id

def test_two_labels_same_person_keep_individual_speech_minimum(ctx):
    user=_user(); segments=[{**SEGMENTS[0],'end_time':1},{**SEGMENTS[1],'start_time':1,'end_time':21}]
    rec=_recording(user,segments,{'SPEAKER_00':EMB,'SPEAKER_01':EMB});draft=[{**s,'speaker':'Ana'} for s in segments]
    request=body(draft,[change('SPEAKER_00','Ana','SPEAKER_00'),change('SPEAKER_01','Ana','SPEAKER_01')],{'SPEAKER_00':'Ana','SPEAKER_01':'Ana'})
    request['workspace_effects']['training_seconds_by_label']={'SPEAKER_00':1,'SPEAKER_01':20}
    with patch('src.services.voice_profiles.MIN_SPEECH_SECONDS',5):
        result=_client(user).post(f'/recording/{rec.id}/update_transcript',json=request)
    assert result.status_code==200
    assert [sample.label for sample in SpeakerVoiceSample.query.filter_by(recording_id=rec.id)]==['SPEAKER_01']

def test_collaborator_links_owner_but_updates_only_own_usage_and_samples(ctx):
    from src.models import InternalShare
    owner=_user(); editor=_user(); rec=_recording(owner,SEGMENTS,{'SPEAKER_00':EMB})
    owner_speaker=Speaker(user_id=owner.id,name='Ana',use_count=5);db.session.add(owner_speaker)
    db.session.add(InternalShare(recording_id=rec.id,owner_id=owner.id,shared_with_user_id=editor.id,can_edit=True));db.session.commit()
    draft=json.loads(rec.transcription);draft[0]['speaker']='Ana';draft[0]['speaker_id']=999999
    with patch('src.services.voice_profiles.MIN_SPEECH_SECONDS',0):
        result=_client(editor).post(f'/recording/{rec.id}/update_transcript',json=body(draft,[change('SPEAKER_00','Ana','SPEAKER_00')],{'SPEAKER_00':'Ana'}))
    assert result.status_code==200 and json.loads(rec.transcription)[0]['speaker_id']==owner_speaker.id
    actor_speaker=Speaker.query.filter_by(user_id=editor.id,name='Ana').one()
    assert actor_speaker.use_count==1 and owner_speaker.use_count==5
    sample=SpeakerVoiceSample.query.filter_by(recording_id=rec.id).one()
    assert sample.user_id==editor.id and sample.speaker_id==actor_speaker.id

def test_readonly_collaborator_cannot_save_context_or_transient_llm(ctx):
    from src.models import InternalShare
    owner=_user(); viewer=_user(); rec=_recording(owner,SEGMENTS,{'SPEAKER_00':EMB})
    db.session.add(InternalShare(recording_id=rec.id,owner_id=owner.id,shared_with_user_id=viewer.id,can_edit=False));db.session.commit()
    client=_client(viewer)
    assert client.get(f'/recording/{rec.id}/workspace_context').status_code==403
    assert client.post(f'/recording/{rec.id}/update_transcript',json=body(SEGMENTS)).status_code==403
    assert client.post(f'/recording/{rec.id}/auto_identify_speakers',json={'transcript_data':SEGMENTS}).status_code==403


def test_reopened_merged_name_moves_existing_samples_without_guessing_durations(ctx):
    user=_user(); rec=_recording(user,SEGMENTS,{'SPEAKER_00':EMB,'SPEAKER_01':EMB})
    other=_recording(user,SEGMENTS,{'SPEAKER_00':EMB})
    client=_client(user)
    with patch('src.services.voice_profiles.MIN_SPEECH_SECONDS',0):
        for recording in (rec,other):
            draft=[{**s,'speaker':'Ana'} for s in json.loads(recording.transcription)]
            labels=list(recording.speaker_embeddings)
            assert client.post(f'/recording/{recording.id}/update_transcript',json=body(draft,
                [change(label,'Ana',label) for label in labels],{label:'Ana' for label in labels})).status_code==200
    ana=Speaker.query.filter_by(user_id=user.id,name='Ana').one()
    samples=SpeakerVoiceSample.query.filter_by(user_id=user.id,recording_id=rec.id).all()
    original={sample.label:(sample.id,sample.speech_seconds,sample.weight,sample.embedding) for sample in samples}
    draft=[{**s,'speaker':'Boris'} for s in json.loads(rec.transcription)]
    result=client.post(f'/recording/{rec.id}/update_transcript',json=body(draft,
        [change('Ana','Boris',label) for label in rec.speaker_embeddings]))
    assert result.status_code==200 and result.json['follow_up']['training']=='done'
    boris=Speaker.query.filter_by(user_id=user.id,name='Boris').one()
    for sample in SpeakerVoiceSample.query.filter_by(user_id=user.id,recording_id=rec.id):
        assert sample.speaker_id==boris.id
        assert (sample.id,sample.speech_seconds,sample.weight,sample.embedding)==original[sample.label]
    assert SpeakerVoiceSample.query.filter_by(recording_id=other.id).one().speaker_id==ana.id
    assert ana.embedding_count==1 and boris.embedding_count==2


def test_legacy_untimed_json_names_and_text_save_without_adding_timestamps(ctx):
    user=_user(); rec=_recording(user,[{'speaker':'SPEAKER_00','sentence':'Legacy text','metadata':{'keep':True}}])
    client=_client(user)
    assert client.get(f'/recording/{rec.id}/workspace_context').status_code==200
    draft=[{'speaker':'Ana','sentence':'Corrected legacy text','metadata':{'keep':True}}]
    result=client.post(f'/recording/{rec.id}/update_transcript',json=body(draft,[change('SPEAKER_00','Ana')]))
    assert result.status_code==200
    stored=json.loads(client.get(f'/api/recordings/{rec.id}').json['transcription'])
    assert stored[0]['speaker']=='Ana' and stored[0]['sentence']=='Corrected legacy text'
    assert 'start_time' not in stored[0] and 'end_time' not in stored[0] and stored[0]['metadata']=={'keep':True}


def test_merged_reopen_correction_preserves_existing_outlier_gate(ctx):
    from src.services.voice_profiles import record_sample, refresh_speaker_summary
    user=_user(); rec=_recording(user,SEGMENTS,{'SPEAKER_00':EMB,'SPEAKER_01':EMB});client=_client(user)
    draft=[{**s,'speaker':'Ana'} for s in SEGMENTS]
    with patch('src.services.voice_profiles.MIN_SPEECH_SECONDS',0):
        assert client.post(f'/recording/{rec.id}/update_transcript',json=body(draft,
            [change(label,'Ana',label) for label in rec.speaker_embeddings],{label:'Ana' for label in rec.speaker_embeddings})).status_code==200
    ana=Speaker.query.filter_by(user_id=user.id,name='Ana').one()
    boris=Speaker(user_id=user.id,name='Boris');db.session.add(boris);db.session.flush()
    for _ in range(3):
        other=_recording(user,SEGMENTS,{'SPEAKER_00':[-.1]*256})
        assert record_sample(boris,other,'SPEAKER_00',[-.1]*256,20)=='stored'
    refresh_speaker_summary(boris);db.session.commit()
    result=client.post(f'/recording/{rec.id}/update_transcript',json=body(
        [{**s,'speaker':'Boris'} for s in json.loads(rec.transcription)],
        [change('Ana','Boris',label) for label in rec.speaker_embeddings]))
    assert result.status_code==200 and result.json['follow_up']['training']=='done'
    assert not SpeakerVoiceSample.query.filter_by(recording_id=rec.id).first()
    assert ana.embedding_count==0 and boris.embedding_count==3
