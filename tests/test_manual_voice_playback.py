"""Saved range PCM parity and isolated preview lifecycle, no provider."""
import hashlib
import wave
from pathlib import Path
import pytest
import json
import struct
import time
import uuid
from flask import Flask
from flask_login import LoginManager
from src.database import db
from src.models import User, Speaker, Recording, ManualVoiceSample, VoiceEmbeddingSpace


@pytest.fixture
def saved_fixture(tmp_path, monkeypatch):
    app=Flask(__name__)
    app.config.update(TESTING=True,SECRET_KEY='saved-playback-fixture',WTF_CSRF_ENABLED=False,
        SQLALCHEMY_DATABASE_URI='sqlite:///'+str(tmp_path/'fixture.db'))
    db.init_app(app)
    login=LoginManager(app)
    login.user_loader(lambda uid: db.session.get(User,int(uid)))
    from src.api.manual_voice_samples import manual_voice_samples_bp
    from src.services.manual_voice_playback import PlaybackJobs
    from src.services import manual_voice_playback as playback
    app.register_blueprint(manual_voice_samples_bp)
    jobs=PlaybackJobs(tmp_path/'playback')
    monkeypatch.setattr(playback,'get_jobs',lambda:jobs)
    path=tmp_path/'source.wav'
    with wave.open(str(path),'wb') as audio:
        audio.setparams((1,2,8000,0,'NONE','not compressed'))
        audio.writeframes(bytes(range(256))*4000)
    monkeypatch.setattr('src.api.manual_voice_samples.local_path',lambda _:str(path))
    with app.app_context():
        db.create_all()
        user=User(username='preview',email='preview@test.example',password='x')
        other=User(username='other',email='other@test.example',password='x')
        db.session.add_all([user,other,VoiceEmbeddingSpace(id=1,dimension=3)])
        db.session.flush()
        speaker=Speaker(user_id=user.id,name='Saved person')
        record=Recording(user_id=user.id,title='Saved source',audio_path=str(path),transcription='keep original')
        db.session.add_all([speaker,record]);db.session.flush()
        sample=ManualVoiceSample(id=str(uuid.uuid4()),user_id=user.id,speaker_id=speaker.id,recording_id=record.id,
            start_ms=123,end_ms=20123,speech_ms=19000,source_audio_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            preparation_id=str(uuid.uuid4()),space_id=1,dimension=3,embedding=struct.pack('<3f',1,0,0))
        db.session.add(sample);db.session.commit()
        client=app.test_client()
        with client.session_transaction() as session:session['_user_id']=str(user.id);session['_fresh']=True
        yield app,client,sample,record,other,jobs,path
        deadline=time.time()+5
        while time.time()<deadline:
            try:jobs.cleanup(all_jobs=True);break
            except Exception:time.sleep(.1)
        db.session.remove()


def ready(client,base):
    response=client.post(base+'/playback',json={})
    assert response.status_code==202,response.json
    job=response.json
    url=base+'/playbacks/'+job['preview_id']+'?lease='+job['lease']
    for _ in range(100):
        status=client.get(url)
        if status.json.get('state')!='preparing':break
        time.sleep(.1)
    assert status.status_code==200 and status.json['state']=='ready',status.json
    return job,url


def test_saved_clip_full_range_read_range_and_lease_isolation(saved_fixture,monkeypatch):
    app,client,sample,record,other,jobs,path=saved_fixture
    monkeypatch.setattr('src.services.manual_voice_samples.space_snapshot',lambda:pytest.fail('No model check for playback'))
    base=f'/speakers/{sample.speaker_id}/manual_voice_samples/{sample.id}'
    job,url=ready(client,base)
    second=client.post(base+'/playback',json={}).json
    assert second['preview_id']==job['preview_id'] and second['lease']!=job['lease']
    audio=url.replace('?','/audio?')
    response=client.get(audio)
    assert response.status_code==200 and response.headers['Cache-Control']=='private, no-store'
    import io
    with wave.open(io.BytesIO(response.data),'rb') as clip,wave.open(str(path),'rb') as source:
        assert clip.getnframes()==160000
        source.setpos(984)
        assert clip.readframes(160000)==source.readframes(160000)
    assert client.get(audio,headers={'Range':'bytes=0-43'}).status_code==206
    assert client.get(audio,headers={'Range':'bytes=9999999-'}).status_code==416
    assert client.delete(url).status_code==200
    second_url=url.split('?')[0]+'?lease='+second['lease']
    assert client.get(second_url).json['state']=='ready'
    assert client.get(audio).status_code==404
    assert record.transcription=='keep original' and ManualVoiceSample.query.count()==1


@pytest.mark.parametrize('kind',['other','deleted','changed','merge','wrong-lease','client-bounds'])
def test_saved_playback_rechecks_identity_and_source(saved_fixture,kind):
    app,client,sample,record,other,jobs,path=saved_fixture
    base=f'/speakers/{sample.speaker_id}/manual_voice_samples/{sample.id}'
    job,url=ready(client,base)
    audio=url.replace('?','/audio?')
    if kind=='other':
        with client.session_transaction() as session:session['_user_id']=str(other.id)
        # This fixture deliberately holds an app context for direct DB checks;
        # clear Flask-Login's per-request user cached on that reused g object.
        from flask import g
        g.pop('_login_user',None)
    elif kind=='deleted':db.session.delete(sample);db.session.commit()
    elif kind=='changed':path.write_bytes(path.read_bytes()+b'changed')
    elif kind=='merge':sample.speaker_id+=999;db.session.commit()
    elif kind=='wrong-lease':audio=audio.split('?')[0]+'?lease=bad'
    else:
        assert client.post(base+'/playback',json={'start_ms':0}).status_code==400
        return
    response=client.get(audio)
    assert response.status_code in (403,404,409)
    assert not any(key in response.json for key in ('recording_title','digest','path','embedding'))


def test_quota_is_shared_atomic_reservation_and_rollback_purges(saved_fixture,monkeypatch):
    app,client,sample,record,other,jobs,path=saved_fixture
    monkeypatch.setattr(jobs,'_run',lambda *args:None)
    jobs.budget=jobs.overhead+2*jobs.reservation
    identity=dict(user_id=1,speaker_id=1,sample_id=sample.id,recording_id=record.id,
        start_ms=123,end_ms=20123,digest=sample.source_audio_sha256)
    jobs.start(identity,str(path))
    jobs.start(identity|{'user_id':2},str(path))
    with pytest.raises(Exception) as error:jobs.start(identity|{'user_id':3},str(path))
    assert error.value.code=='limit'
    assert jobs.cleanup(all_jobs=True)==0


def test_saved_range_streaming_matches_reference(tmp_path):
    from src.services.manual_voice_playback_worker import crop_streaming
    from src.services.manual_voice_worker import crop_pcm
    source = tmp_path / 'source.wav'
    with wave.open(str(source), 'wb') as w:
        w.setparams((2, 2, 8000, 0, 'NONE', 'not compressed'))
        w.writeframes(bytes(range(256)) * 2000)
    ref = tmp_path / 'ref'
    ref.mkdir()
    reference, _ = crop_pcm(source, ref, 123, 7123)
    output = tmp_path / 'clip.wav'
    crop_streaming(source, output, 123, 7123)
    with wave.open(str(reference), 'rb') as a, wave.open(str(output), 'rb') as b:
        assert a.getparams() == b.getparams()
        assert a.readframes(a.getnframes()) == b.readframes(b.getnframes())
    assert not (tmp_path / 'decoded.wav').exists()


def test_playback_does_not_depend_on_provider_configuration(tmp_path):
    from src.services.manual_voice_playback import PlaybackJobs
    jobs = PlaybackJobs(tmp_path / 'playback', ttl=.05)
    assert jobs.root != tmp_path / 'embedding'
    assert jobs.reservation <= 21 * 1024 * 1024
