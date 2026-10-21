"""Recovery and cross-process duplication tests for paid Tencent ASR tasks."""
import io
import json
import subprocess
import sys
from unittest.mock import Mock

import httpx
import pytest

from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.connectors.tencent_asr import TencentASRConnector
from src.services.transcription.exceptions import TranscriptionError
from src.services.transcription.tencent_state import locked_checkpoint


def connector(tmp_path):
    return TencentASRConnector({'secret_id':'id','secret_key':'secret','state_dir':str(tmp_path)})


def audio():
    return TranscriptionRequest(audio_file=io.BytesIO(b'original bytes'),filename='meeting.webm',diarize=True)


def test_new_worker_resumes_saved_task(tmp_path):
    first=connector(tmp_path)
    first._call=Mock(return_value={'TaskId':123})
    first._poll=Mock(side_effect=TranscriptionError('network interrupted'))
    with pytest.raises(TranscriptionError):
        first.transcribe(audio())
    second=connector(tmp_path)
    second._call=Mock(side_effect=AssertionError('must not resubmit'))
    second._poll=Mock(return_value={'Status':2,'Result':'recovered'})
    assert second.transcribe(audio()).text=='recovered'
    assert second._poll.call_args.args[1]==123
    third=connector(tmp_path)
    third._call=Mock(side_effect=AssertionError('must use saved result'))
    third._poll=Mock(side_effect=AssertionError('must use saved result'))
    assert third.transcribe(audio()).text=='recovered'


def test_uncertain_post_blocks_new_worker(tmp_path):
    first=connector(tmp_path)
    first._call=Mock(side_effect=httpx.ReadTimeout('sensitive URL'))
    with pytest.raises(TranscriptionError,match='uncertain'):
        first.transcribe(audio())
    second=connector(tmp_path)
    second._call=Mock(side_effect=AssertionError('duplicate submission'))
    with pytest.raises(TranscriptionError,match='automatic resubmission is blocked'):
        second.transcribe(audio())


def test_expired_task_never_reused_or_resubmitted(tmp_path):
    first=connector(tmp_path)
    first._call=Mock(return_value={'TaskId':123})
    first._poll=Mock(side_effect=TranscriptionError('interrupted'))
    with pytest.raises(TranscriptionError): first.transcribe(audio())
    path=next(tmp_path.glob('*.json'))
    state=json.loads(path.read_text())
    state['submitted_at']=0
    path.write_text(json.dumps(state))
    with pytest.raises(TranscriptionError,match='expiry'):
        connector(tmp_path).transcribe(audio())


def test_corrupt_checkpoint_does_not_submit(tmp_path):
    path=tmp_path/'fingerprint.json'
    path.write_text('{')
    with pytest.raises(TranscriptionError,match='cannot be read'):
        with locked_checkpoint(tmp_path,'fingerprint'): pass


def test_real_process_lock_and_crash_release(tmp_path):
    program='''import sys,time
from src.services.transcription.tencent_state import locked_checkpoint
with locked_checkpoint(sys.argv[1], 'same-audio') as (state, save):
 state.update(status='submitting')
 save()
 print('LOCKED',flush=True)
 time.sleep(120)
'''
    child=subprocess.Popen([sys.executable,'-c',program,str(tmp_path)],stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='LOCKED'
        with pytest.raises(TranscriptionError,match='already being processed'):
            with locked_checkpoint(tmp_path,'same-audio'): pass
    finally:
        child.kill()
        child.wait(timeout=10)
    with locked_checkpoint(tmp_path,'same-audio') as (state,save):
        assert state['status']=='submitting'


def test_failed_cleanup_preserves_locator_for_retry(tmp_path,monkeypatch):
    import src.services.storage as storage_module
    from types import SimpleNamespace
    storage=Mock(endpoint_url='https://cos.ap-hongkong.myqcloud.com')
    monkeypatch.setattr(storage_module,'get_storage_service',lambda:SimpleNamespace(s3=storage))
    c=connector(tmp_path)
    state={'status':'completed','staging_locator':'s3://test/recordings/audio.webm',
           'staging_endpoint':storage.endpoint_url}
    save=Mock()
    storage.delete.side_effect=RuntimeError('network')
    c._cleanup_checkpoint(state,save)
    assert 'staging_locator' in state
    storage.delete.side_effect=None
    c._cleanup_checkpoint(state,save)
    assert 'staging_locator' not in state
    save.assert_called_once()


def test_cos_client_configuration_and_private_upload(tmp_path):
    from unittest.mock import patch
    from src.services.storage.s3 import S3StorageBackend
    backend=S3StorageBackend(bucket='test-123',region='ap-hongkong',
                             endpoint_url='https://cos.ap-hongkong.myqcloud.com')
    with patch('boto3.client') as factory:
        backend._create_client()
        config=factory.call_args.kwargs['config']
        assert config.s3['addressing_style']=='virtual'
        assert config.request_checksum_calculation=='when_required'
        assert config.response_checksum_validation=='when_required'
    path=tmp_path/'audio.webm'
    path.write_bytes(b'original audio unchanged')
    fake=Mock()
    with patch.object(backend,'_get_client',return_value=fake), patch.object(backend,'stat'):
        backend.upload_local_file(str(path),'recordings/audio.webm',acl='private')
        fake.upload_file.assert_called_once_with(str(path),'test-123','recordings/audio.webm',ExtraArgs={'ACL':'private'})
    assert path.read_bytes()==b'original audio unchanged'


def test_webm_pipeline_preserves_original_even_when_compression_enabled(tmp_path,monkeypatch):
    import src.utils.audio_conversion as conversion
    p=tmp_path/'meeting.webm'
    p.write_bytes(b'original webm bytes')
    monkeypatch.setattr(conversion,'AUDIO_COMPRESS_UPLOADS',True)
    monkeypatch.setattr(conversion,'get_codec_info',Mock(side_effect=AssertionError('must not inspect or convert')))
    result=conversion.convert_if_needed(str(p),connector_specs=TencentASRConnector.SPECIFICATIONS)
    assert result.output_path==str(p)
    assert not result.was_compressed and not result.was_converted
    assert p.read_bytes()==b'original webm bytes'
