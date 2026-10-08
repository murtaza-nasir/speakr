"""Segment audio/auth/race/process contracts; isolated data and stub providers only."""
import io
import importlib
import json
import os
import subprocess
import sys
import time
import uuid
import wave
from unittest.mock import Mock, patch
import numpy as np
import pytest
from src.app import app
from src.database import db
from src.models import Recording, User, InternalShare
from src.services import segment_transcription as jobs
from src.services.segment_transcription_worker import transcribe_segment, source_signature
from src.services.transcription import TranscriptionResponse, TranscriptionSegment


@pytest.fixture
def media(tmp_path):
    rate = 16000
    time_axis = np.arange(rate * 8) / rate
    signal = np.where((time_axis < 2) | (time_axis >= 3), np.sin(2*np.pi*2000*time_axis), 0) * .6
    path = tmp_path / 'source.wav'
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(rate)
        output.writeframes((signal * 32767).astype('<i2').tobytes())
    return path


def payload(path, tmp_path, start=1, end=5):
    return dict(path=str(path),signature=source_signature(path),start=start,end=end,
        connector='asr_endpoint',config={'base_url':'http://stub.invalid'},
        params={'language':'ru','hotwords':'test','initial_prompt':'context','transcription_model':'large-v3'},work=str(tmp_path),deadline=300)


@pytest.mark.parametrize('extension,codec', [('wav',None),('m4a','aac'),('webm','libopus')])
def test_uploaded_clip_has_exact_nonzero_range_and_preserves_request_params(media,tmp_path,extension,codec):
    path=media
    if codec:
        path=tmp_path / ('encoded.'+extension)
        subprocess.run(['ffmpeg','-v','error','-i',str(media),'-c:a',codec,str(path)],check=True)
    captured={}
    def transcribe(request):
        captured['request']=request
        with wave.open(io.BytesIO(request.audio_file.read()),'rb') as audio:
            captured['frames']=audio.getnframes(); captured['channels']=audio.getnchannels()
            captured['wave']=np.frombuffer(audio.readframes(audio.getnframes()),dtype='<i2')
        return TranscriptionResponse(text='unused',segments=[TranscriptionSegment(text='First',speaker='SPEAKER_0'),TranscriptionSegment(text='Second')])
    connector=Mock(transcribe=transcribe)
    with patch('src.services.transcription.get_registry') as registry:
        registry.return_value.create_connector.return_value=connector
        result=transcribe_segment(payload(path,tmp_path))
    assert result == {'text':'First Second'}
    assert abs(captured['frames']-64000)<80 and captured['channels']==1
    assert np.abs(captured['wave'][18000:29000]).mean() < np.abs(captured['wave'][1000:12000]).mean()*.1
    request=captured['request']
    assert request.diarize is False and request.min_speakers is None and request.max_speakers is None
    assert request.language=='ru' and request.model=='large-v3' and request.hotwords=='test' and request.prompt=='context'


def test_stereo_is_not_silently_mixed_and_current_source_must_match(media,tmp_path):
    stereo=tmp_path/'stereo.wav'
    subprocess.run(['ffmpeg','-v','error','-i',str(media),'-af','pan=stereo|c0=c0|c1=-1*c0',str(stereo)],check=True)
    def transcribe(request):
        with wave.open(io.BytesIO(request.audio_file.read()),'rb') as audio:
            assert audio.getnchannels()==2
            samples=np.frombuffer(audio.readframes(5000),dtype='<i2').reshape(-1,2)
            assert np.abs(samples).max()>1000 and np.array_equal(samples[:,0],-samples[:,1])
        return TranscriptionResponse(text='Stereo')
    with patch('src.services.transcription.get_registry') as registry:
        registry.return_value.create_connector.return_value=Mock(transcribe=transcribe)
        assert transcribe_segment(payload(stereo,tmp_path))=={'text':'Stereo'}
        data=payload(stereo,tmp_path);stereo.write_bytes(b'changed')
        assert transcribe_segment(data)=={'code':'changed'}


@pytest.mark.parametrize('text,code',[('   ','empty'),('a'*65537,'size')])
def test_empty_and_oversized_provider_output_never_replace_text(media,tmp_path,text,code):
    with patch('src.services.transcription.get_registry') as registry:
        registry.return_value.create_connector.return_value.transcribe.return_value=TranscriptionResponse(text=text)
        assert transcribe_segment(payload(media,tmp_path))=={'code':code}


def test_bounds_corrupt_and_private_provider_exception(media,tmp_path):
    assert transcribe_segment(payload(media,tmp_path,end=9))=={'code':'bounds'}
    with patch('src.services.transcription.get_registry') as registry:
        registry.return_value.create_connector.return_value.transcribe.side_effect=RuntimeError('secret-api-key private-path')
        assert transcribe_segment(payload(media,tmp_path))=={'code':'provider'}
    media.write_bytes(b'not-audio')
    assert transcribe_segment(payload(media,tmp_path))=={'code':'media'}


@pytest.mark.parametrize('start,end',[(None,1),(True,3),(0,False),(0,'nan'),(0,'inf'),(-1,2),(1,1),(0,.24),(0,300.01)])
def test_invalid_bounds_never_start_a_process(start,end):
    with pytest.raises(jobs.SegmentTranscriptionError) as error:
        jobs.validate_bounds(start,end)
    assert error.value.status==400


def wait_done(manager,meta,path):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        state=manager.get(1,2,meta['job_id'],str(path))
        if state['status']!='running':return state
        time.sleep(.02)
    raise AssertionError('Disposable worker did not finish')


def manager_start(manager,media,config=None):
    return manager.start(1,2,str(media),1,5,'asr_endpoint',config or {},{})


def cross_worker_status(root,path,job_id,cancel):
    # Spawn only the job service: importing the test module would initialize a second Flask DB.
    command = "import json,sys;from src.services.segment_transcription import SegmentJobs;p=json.load(sys.stdin);print(json.dumps(SegmentJobs(p['root']).get(1,2,p['job'],p['path'],cancel=p['cancel'])))"
    result = subprocess.run([sys.executable,'-c',command],input=json.dumps(dict(root=str(root),path=str(path),job=job_id,cancel=cancel)),capture_output=True,text=True,timeout=30,check=True)
    return json.loads(result.stdout)


def test_job_can_be_polled_and_cancelled_from_another_process_without_config_on_disk(media,tmp_path):
    command=[sys.executable,'-c',"import json,sys,time;json.load(sys.stdin);time.sleep(1);print(json.dumps({'text':'New text'}))"]
    manager=jobs.SegmentJobs(tmp_path/'jobs',worker_command=command,deadline=5)
    meta=manager_start(manager,media,{'api_key':'private-test-secret'})
    for file in manager.root.glob('*/meta.json'):
        assert 'private-test-secret' not in file.read_text()
        assert file.stat().st_mode & 0o077 == 0
    status=cross_worker_status(manager.root,media,meta['job_id'],False)
    assert status['status'] in ('running','done')
    result=wait_done(manager,meta,media);assert result['text']=='New text'
    assert cross_worker_status(manager.root,media,meta['job_id'],True)['status']=='cancelled'
    assert not (manager.root/meta['job_id']/'work').exists()


def test_global_busy_cancel_and_deadline_leave_no_clip_or_children(media,tmp_path):
    command=[sys.executable,'-c',"import json,sys,time;json.load(sys.stdin);time.sleep(30)"]
    manager=jobs.SegmentJobs(tmp_path/'jobs',worker_command=command,deadline=2)
    meta=manager_start(manager,media)
    with pytest.raises(jobs.SegmentTranscriptionError) as error:manager_start(jobs.SegmentJobs(manager.root),media)
    assert error.value.code=='busy'
    jobs.SegmentJobs(manager.root).get(1,2,meta['job_id'],str(media),cancel=True)
    assert wait_done(manager,meta,media)['status']=='cancelled'
    assert not (manager.root/meta['job_id']/'work').exists()
    timed=manager_start(manager,media);assert wait_done(manager,timed,media)['code']=='timeout'
    assert not (manager.root/timed['job_id']/'work').exists()
    with jobs.file_lock(manager.root/'.execution.lock',blocking=False):pass


def test_identity_privacy_source_change_dead_owner_and_ttl(media,tmp_path):
    manager=jobs.SegmentJobs(tmp_path/'jobs',worker_command=[sys.executable,'-c',"import json,sys;json.load(sys.stdin);print(json.dumps({'text':'new'}))"],ttl=900)
    meta=manager_start(manager,media);wait_done(manager,meta,media)
    with pytest.raises(jobs.SegmentTranscriptionError):manager.get(4,2,meta['job_id'],str(media))
    with pytest.raises(jobs.SegmentTranscriptionError):manager.get(1,3,meta['job_id'],str(media))
    with pytest.raises(jobs.SegmentTranscriptionError):manager.get(1,2,'../outside',str(media))
    assert manager.get(1,2,meta['job_id'],str(tmp_path/'other'))['code']=='changed'
    directory=manager.root/meta['job_id'];stored=manager._read(directory)
    stored.update(status='running',birth='different-birth-token');manager._write(directory,stored)
    assert manager.get(1,2,meta['job_id'],str(media))['code']=='worker'
    stored=manager._read(directory);stored['finished']=time.time()-901;manager._write(directory,stored)
    with pytest.raises(jobs.SegmentTranscriptionError) as error:manager.get(1,2,meta['job_id'],str(media))
    assert error.value.status==404 and not directory.exists()


@pytest.fixture
def recording_users(media):
    previous_csrf = app.config['WTF_CSRF_ENABLED']
    app.config['WTF_CSRF_ENABLED'] = False
    with app.app_context():
        suffix=uuid.uuid4().hex
        owner=User(username='segment_'+suffix,email=suffix+'@example.test',password='test')
        other=User(username='segment_other_'+suffix,email='other_'+suffix+'@example.test',password='test')
        db.session.add_all([owner,other]);db.session.flush()
        recording=Recording(user_id=owner.id,title='Segment test',status='COMPLETED',audio_path=str(media),transcription='original')
        db.session.add(recording);db.session.commit();ids=recording.id,owner.id,other.id
    yield ids
    with app.app_context():
        InternalShare.query.filter_by(recording_id=ids[0]).delete()
        db.session.delete(db.session.get(Recording,ids[0]));db.session.delete(db.session.get(User,ids[1]));db.session.delete(db.session.get(User,ids[2]));db.session.commit()
    app.config['WTF_CSRF_ENABLED'] = previous_csrf


def client_for(user_id=None):
    client=app.test_client()
    if user_id:
        with client.session_transaction() as session:session['_user_id']=str(user_id);session['_fresh']=True
    return client


def test_api_edit_access_csrf_and_no_generation_for_denied(recording_users):
    rid,owner,other=recording_users;url=f'/api/recordings/{rid}/segment-transcriptions'
    with patch.object(jobs.segment_jobs,'start') as start:
        assert client_for().post(url,json={'start':0,'end':1}).status_code==401
        assert client_for(other).post(url,json={'start':0,'end':1}).status_code==403
        start.assert_not_called()
        with patch.dict(app.config,{'WTF_CSRF_ENABLED':True}):
            assert client_for(owner).post(url,json={'start':0,'end':1}).status_code==400
        start.assert_not_called()


def test_workplace_save_permissions_csrf_and_export(recording_users):
    rid, owner, other = recording_users
    url = f'/recording/{rid}/update_transcription'
    body = {'transcription': json.dumps([{'speaker': 'Local name', 'sentence': 'Edited.', 'start_time': 0, 'end_time': 8}])}
    with app.app_context():
        db.session.add(InternalShare(recording_id=rid, owner_id=owner, shared_with_user_id=other, can_edit=False))
        db.session.commit()
    with patch('src.api.recordings.export_recording') as export:
        assert client_for(other).post(url, json=body).status_code == 403
        with patch.dict(app.config, {'WTF_CSRF_ENABLED': True}):
            assert client_for(owner).post(url, json=body).status_code == 400
        export.assert_not_called()
        with app.app_context():
            assert db.session.get(Recording, rid).transcription == 'original'
            InternalShare.query.filter_by(recording_id=rid).one().can_edit = True
            db.session.commit()
        with patch.object(importlib.import_module('src.app'), 'ENABLE_INTERNAL_SHARING', True):
            response = client_for(other).post(url, json=body)
        assert response.status_code == 200, response.get_json()
        assert json.loads(response.json['recording']['transcription'])[0]['sentence'] == 'Edited.'
        export.assert_called_once()


def test_api_start_uses_draft_bounds_does_not_write_recording_and_poll_rechecks_permission(recording_users):
    rid,owner,other=recording_users;url=f'/api/recordings/{rid}/segment-transcriptions'
    with patch.object(jobs.segment_jobs,'start',return_value={'job_id':'job','status':'running'}) as start:
        response=client_for(owner).post(url,json={'start':2,'end':7})
        assert response.status_code==202 and response.headers['Cache-Control']=='private, no-store'
        assert start.call_args.args[3:5]==(2.,7.)
        assert client_for(owner).post(url,json={'start':0,'end':301}).status_code==400
    with app.app_context():
        record=db.session.get(Recording,rid);assert record.transcription=='original' and record.status=='COMPLETED'
        share=InternalShare(recording_id=rid,owner_id=owner,shared_with_user_id=other,can_edit=False)
        db.session.add(share);db.session.commit()
    with patch.object(jobs.segment_jobs,'get',return_value={'status':'running'}) as get:
        assert client_for(other).get(url+'/job').status_code==403;get.assert_not_called()
        assert client_for(owner).get(url+'/job').status_code==200
    with app.app_context():
        InternalShare.query.filter_by(recording_id=rid).first().can_edit=True;db.session.commit()
    with patch.object(importlib.import_module('src.app'), 'ENABLE_INTERNAL_SHARING', True), patch.object(jobs.segment_jobs,'start',return_value={'job_id':'job','status':'running'}) as start:
        assert client_for(other).post(url,json={'start':0,'end':1}).status_code==202
    with patch.object(jobs.segment_jobs,'get',return_value={'status':'cancelled'}) as get:
        assert client_for(owner).delete(url+'/job').status_code==200
        assert get.call_args.kwargs['cancel'] is True


def test_api_remote_missing_busy_and_safe_failure(recording_users):
    rid,owner,_=recording_users;client=client_for(owner);url=f'/api/recordings/{rid}/segment-transcriptions'
    with patch.object(jobs.segment_jobs,'start',side_effect=jobs.SegmentTranscriptionError('busy',429)):
        response=client.post(url,json={'start':0,'end':1});assert response.status_code==429 and response.headers['Retry-After']=='2'
    with patch.object(jobs.segment_jobs,'start',side_effect=RuntimeError('private-secret')):
        response=client.post(url,json={'start':0,'end':1});assert response.status_code==503 and response.json=={'code':'unavailable'}
    with app.app_context():db.session.get(Recording,rid).audio_path='s3://bucket/file';db.session.commit()
    assert client.post(url,json={'start':0,'end':1}).status_code==501
    with app.app_context():db.session.get(Recording,rid).audio_path=None;db.session.commit()
    assert client.post(url,json={'start':0,'end':1}).status_code==404


@pytest.mark.parametrize("historical", [(None, None, None), ("ru", "Old hints", "Old context")])
def test_segment_asr_historical_hints_do_not_inherit_new_defaults(recording_users, historical):
    rid, owner, _ = recording_users
    with app.app_context():
        recording = db.session.get(Recording, rid)
        recording.transcription_language, recording.resolved_hotwords, recording.resolved_initial_prompt = historical
        db.session.commit()
    with patch("src.api.recordings.resolve_transcription_params", return_value={"language": "en", "hotwords": "NEW", "initial_prompt": "NEW", "transcription_model": "base"}), patch.object(jobs.segment_jobs, "start", return_value={"job_id":"fixture","status":"running"}) as start:
        response = client_for(owner).post(f"/api/recordings/{rid}/segment-transcriptions", json={"start":0,"end":1})
        assert response.status_code == 202
        params = start.call_args.args[-1]
        assert (params["language"], params["hotwords"], params["initial_prompt"]) == historical
        assert params["transcription_model"] == "base"
