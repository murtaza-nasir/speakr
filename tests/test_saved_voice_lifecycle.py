"""Private playback crash/quota/access regression tests, isolated media only."""
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
import wave

import pytest
from flask import Flask
from src.database import db
from src.services.manual_voice_playback import PlaybackJobs, Error
from src.services.segment_transcription import process_identity
from test_manual_voice_playback import saved_fixture, ready
from test_manual_voice_access import access_fixture, login_client


def identity(path, user=1):
    return dict(user_id=user,speaker_id=user,sample_id=str(uuid.uuid4()),recording_id=1,
        start_ms=0,end_ms=20000,digest=hashlib.sha256(path.read_bytes()).hexdigest())


def wait_until(check, timeout=8):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if check():return
        time.sleep(.05)
    assert check()


def test_delayed_supervisor_cannot_spawn_after_verified_rollback(tmp_path,monkeypatch):
    path=tmp_path/'source';path.write_bytes(b'fixture')
    jobs=PlaybackJobs(tmp_path/'jobs');go=threading.Event();finished=threading.Event()
    original=jobs._run
    def delayed(*args):
        go.wait(5)
        original(*args)
        finished.set()
    monkeypatch.setattr(jobs,'_run',delayed)
    job=jobs.start(identity(path),str(path))
    assert jobs.cleanup(all_jobs=True)==0
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('spawn after verified purge'))
    go.set();assert finished.wait(5)
    assert not (jobs.root/job['preview_id']).exists()


def test_actual_active_cancel_and_deadline_purge(tmp_path):
    path=tmp_path/'source';path.write_bytes(b'fixture')
    jobs=PlaybackJobs(tmp_path/'jobs',deadline=.25,ttl=.1)
    jobs.worker_command=[sys.executable,'-c','import sys,time;sys.stdin.buffer.read();time.sleep(30)']
    job=jobs.start(identity(path),str(path));directory=jobs.root/job['preview_id']
    wait_until(lambda:'worker_pid' in json.loads((directory/'meta.json').read_text()))
    meta=json.loads((directory/'meta.json').read_text());pid=meta['worker_pid']
    wait_until(lambda:json.loads((directory/'meta.json').read_text())['state']=='failed')
    assert process_identity(pid) is None
    time.sleep(.11);assert jobs.cleanup()==0 and not directory.exists()
    second=jobs.start(identity(path),str(path));directory=jobs.root/second['preview_id']
    wait_until(lambda:'worker_pid' in json.loads((directory/'meta.json').read_text()))
    assert jobs.cleanup(all_jobs=True)==0
    assert not directory.exists()


@pytest.mark.skipif(os.name=='nt',reason='Linux Docker process-family containment')
def test_dead_leader_owned_decoder_group_is_killed_before_purge(tmp_path):
    path=tmp_path/'source';path.write_bytes(b'fixture')
    jobs=PlaybackJobs(tmp_path/'jobs');jobs._run=lambda *args:None
    job=jobs.start(identity(path),str(path));directory=jobs.root/job['preview_id']
    child_file=tmp_path/'child.pid'
    script='import subprocess,time,pathlib,sys;from src.services.manual_voice_playback_worker import parent_death_options;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],**parent_death_options());pathlib.Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)'
    leader=subprocess.Popen([sys.executable,'-c',script,str(child_file)],start_new_session=True)
    try:
        wait_until(child_file.exists);child=int(child_file.read_text())
        meta=json.loads((directory/'meta.json').read_text())
        meta.update(worker_pid=leader.pid,worker_birth=process_identity(leader.pid))
        with jobs.admission():jobs._write(directory,meta)
        leader.kill();leader.wait()
        assert jobs.cleanup(all_jobs=True)==0
        assert not PlaybackJobs._group_alive(meta)
        assert not directory.exists()
    finally:
        try:os.killpg(leader.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        leader.wait()


@pytest.mark.skipif(os.name=='nt',reason='Linux pidfd containment')
def test_reused_foreign_leader_never_killed_by_stale_metadata(tmp_path,monkeypatch):
    jobs=PlaybackJobs(tmp_path/'jobs')
    foreign=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
    try:
        birth=process_identity(foreign.pid)
        stale={'worker_pid':foreign.pid,'worker_birth':str(int(birth)-1)}
        assert not jobs._group_alive(stale)
        monkeypatch.setattr(os,'killpg',lambda *args:pytest.fail('foreign group kill'))
        jobs._terminate_stored(stale)
        assert foreign.poll() is None
    finally:
        foreign.kill();foreign.wait()


def test_namespace_secret_rotation_and_named_memory_engines(tmp_path,monkeypatch):
    from src.services import manual_voice_playback as playback
    monkeypatch.setattr(PlaybackJobs,'start_maintenance',lambda *args:None)
    def root(uri,secret):
        app=Flask('playback-scope',instance_path=str(tmp_path))
        app.config.update(SQLALCHEMY_DATABASE_URI=uri,SECRET_KEY=secret)
        db.init_app(app)
        with app.app_context():return playback.get_jobs().root
    uri='sqlite:///'+str(tmp_path/'same.db')
    assert root(uri,'first')==root(uri,'rotated')
    memory='sqlite:///file:saved-range?mode=memory&cache=shared&uri=true'
    assert root(memory,'first')!=root(memory,'first')


def test_multiprocess_atomic_reservations_keep_global_budget(tmp_path):
    root=tmp_path/'jobs';path=tmp_path/'source';path.write_bytes(b'fixture')
    script='''import json,sys,time
from src.services.manual_voice_playback import PlaybackJobs,Error
j=PlaybackJobs(sys.argv[1]);j.budget=j.overhead+2*j.reservation;j._run=lambda *a:None
try:j.start(json.loads(sys.argv[3]),sys.argv[2]);print('accepted',flush=True)
except Error as e:print(e.code,flush=True)
time.sleep(1)
'''
    processes=[subprocess.Popen([sys.executable,'-c',script,str(root),str(path),json.dumps(identity(path,n))],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for n in range(1,7)]
    results=[p.communicate(timeout=15) for p in processes]
    assert all(p.returncode==0 for p in processes),results
    assert [out.strip() for out,_ in results].count('accepted')==2,results
    assert all(out.strip() in ('accepted','limit') for out,_ in results),results
    jobs=PlaybackJobs(root);assert jobs.cleanup(all_jobs=True)==0


def test_read_only_source_playback_and_real_share_revocation(access_fixture,tmp_path,monkeypatch):
    from src.models import ManualVoiceSample
    from src.services import manual_voice_playback as playback
    app,user,speaker,record,share,_,path,actual_access=access_fixture
    jobs=PlaybackJobs(tmp_path/'playback');monkeypatch.setattr(playback,'get_jobs',lambda:jobs)
    share.can_edit=False
    sample=ManualVoiceSample(id=str(uuid.uuid4()),user_id=user.id,speaker_id=speaker.id,recording_id=record.id,
        start_ms=2000,end_ms=22000,speech_ms=20000,source_audio_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        preparation_id=str(uuid.uuid4()),space_id=1,dimension=3,embedding=struct.pack('<3f',1,0,0))
    db.session.add(sample);db.session.commit()
    assert actual_access(record,user) and not actual_access(record,user,require_edit=True)
    client=login_client(app,user);base=f'/speakers/{speaker.id}/manual_voice_samples/{sample.id}'
    job,url=ready(client,base)
    assert client.get(base+'/source').status_code==200
    db.session.delete(share);db.session.commit()
    for endpoint in [base+'/source',url,url.replace('?','/audio?')]:
        response=client.get(endpoint);assert response.status_code==403
        assert record.title not in response.get_data(as_text=True) and str(path) not in response.get_data(as_text=True)
    assert client.post(base+'/playback',json={}).status_code==403
    assert jobs.cleanup(all_jobs=True)==0


def test_detach_removes_matching_preserves_receipt_source_and_other_sample(saved_fixture,monkeypatch):
    from src.models import ManualVoiceSample, ManualVoiceSampleReceipt, Speaker
    from src.services import voice_profiles as vp
    from datetime import datetime,timedelta
    app,client,sample,record,other,jobs,path=saved_fixture
    monkeypatch.setattr(vp,'current_space_id',lambda:1);monkeypatch.setattr(vp,'legacy_space_id',lambda:1)
    monkeypatch.setenv('VOICE_PROFILE_MIN_SPEECH_SECONDS','15')
    speaker=db.session.get(Speaker,sample.speaker_id)
    receipt=ManualVoiceSampleReceipt(preparation_id=sample.preparation_id,user_id=sample.user_id,initial_speaker_id=sample.speaker_id,initial_speaker_created_at=speaker.created_at,
        sample_id=sample.id,expires_at=datetime.utcnow()+timedelta(days=1))
    db.session.add(receipt);vp.refresh_speaker_summary(speaker);db.session.commit()
    assert vp.find_matches([1,0,0],sample.user_id,1,threshold=.9)[0]['speaker_id']==speaker.id
    before=path.read_bytes();sid=sample.id;base=f'/speakers/{speaker.id}/manual_voice_samples/{sid}'
    assert client.delete(base).status_code==200
    assert ManualVoiceSample.query.count()==0 and ManualVoiceSampleReceipt.query.count()==1
    assert vp.find_matches([1,0,0],speaker.user_id,1,threshold=.9)==[]
    db.session.expire_all()
    assert db.session.get(ManualVoiceSample,sid) is None
    assert path.read_bytes()==before and db.session.get(type(record),record.id).transcription=='keep original'


def test_preview_post_release_and_detach_require_csrf(saved_fixture):
    from flask_wtf.csrf import CSRFProtect
    app,client,sample,record,other,jobs,path=saved_fixture
    app.config['WTF_CSRF_ENABLED']=True;CSRFProtect(app)
    base=f'/speakers/{sample.speaker_id}/manual_voice_samples/{sample.id}'
    assert client.post(base+'/playback',json={}).status_code==400
    assert client.delete(base).status_code==400
    assert client.delete(base+'/playbacks/'+str(uuid.uuid4())+'?lease='+'a'*32).status_code==400
    assert not list(jobs.root.glob('*/*'))


def test_real_merge_rejects_old_profile_and_keeps_uuid_source_and_playback(saved_fixture,monkeypatch):
    from src.models import Speaker,ManualVoiceSample
    from src.services import voice_profiles as vp
    from src.services.speaker_merge import merge_speakers
    app,client,sample,record,other,jobs,path=saved_fixture
    monkeypatch.setattr(vp,'current_space_id',lambda:1);monkeypatch.setattr(vp,'legacy_space_id',lambda:1)
    target=Speaker(user_id=sample.user_id,name='Target saved person');db.session.add(target);db.session.commit()
    original_id=sample.id;old=f'/speakers/{sample.speaker_id}/manual_voice_samples/{sample.id}'
    _,url=ready(client,old)
    merge_speakers(target.id,[sample.speaker_id],sample.user_id)
    assert client.get(url).status_code in (403,404)
    moved=db.session.get(ManualVoiceSample,original_id)
    assert moved.speaker_id==target.id and moved.recording_id==record.id
    base=f'/speakers/{target.id}/manual_voice_samples/{original_id}'
    _,new_url=ready(client,base)
    assert client.get(new_url.replace('?','/audio?')).status_code==200


@pytest.mark.skipif(os.name=='nt',reason='Linux worker parent watchdog')
def test_worker_lost_parent_before_payload_exits_without_output(tmp_path):
    environment=os.environ.copy();environment['SPEAKR_PLAYBACK_PARENT_PID']='999999999';environment['SPEAKR_PLAYBACK_PARENT_BIRTH']='missing'
    worker=subprocess.Popen([sys.executable,'-m','src.services.manual_voice_playback_worker'],stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True,env=environment)
    try:
        worker.wait(timeout=8)
        assert worker.returncode!=0 and list(tmp_path.iterdir())==[]
    finally:
        if worker.poll() is None:worker.kill();worker.wait()
        worker.stdin.close()


def test_expired_ready_purged_by_restarted_manager_and_corruption_fails_closed(saved_fixture):
    app,client,sample,record,other,jobs,path=saved_fixture
    base=f'/speakers/{sample.speaker_id}/manual_voice_samples/{sample.id}'
    job,url=ready(client,base);directory=jobs.root/job['preview_id']
    with jobs.admission():
        meta=jobs._load(directory);meta['expires_at']=time.time()-1;jobs._write(directory,meta)
    restarted=PlaybackJobs(jobs.root)
    assert restarted.cleanup()==0 and not directory.exists()
    assert client.get(url).status_code==410
    (jobs.root/'.foreign').write_text('foreign')
    with pytest.raises(Error):restarted.cleanup(all_jobs=True)
    assert (jobs.root/'.foreign').read_text()=='foreign'
    (jobs.root/'.foreign').unlink()


@pytest.mark.parametrize('rate',[384000,8000])
def test_high_rate_and_long_decoded_source_stream_without_full_temp(tmp_path,rate):
    from src.services.manual_voice_playback_worker import crop_streaming
    source=tmp_path/'source.wav';duration=15 if rate==384000 else 34000
    # A sparse managed input whose decoded PCM exceeds the entire preview budget.
    frames=rate*duration
    with source.open('wb') as stream:
        stream.write(struct.pack('<4sI4s4sIHHIIHH4sI',b'RIFF',36+frames*2,b'WAVE',b'fmt ',16,1,1,rate,rate*2,2,16,b'data',frames*2))
        stream.seek(44+frames*2-1);stream.write(b'\0')
    output=tmp_path/'clip.wav';crop_streaming(source,output,0,15000)
    with wave.open(str(output),'rb') as clip:
        assert clip.getframerate()==rate and clip.getnframes()==rate*15
        assert clip.readframes(10)==b'\0'*20
    assert output.stat().st_size<20*1024*1024
    assert sorted(p.name for p in tmp_path.iterdir())==['clip.wav','source.wav']
    if rate==8000:assert source.stat().st_size>512*1024*1024
