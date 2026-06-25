"""Unit tests for the S3/MinIO storage backend (src/services/storage/s3.py).

All tests run fully offline using a mocked boto3 client. No real network/AWS.
"""

import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.services.storage.s3 import S3StorageBackend
from src.services.storage.local import LocalStorageBackend
from src.services.storage.locator import local_path_from_key
from src.services.storage.service import StorageService
from src.services.storage.factory import StorageSettings
from src.services.storage.interfaces import (
    MaterializedFile,
    ObjectStat,
    StorageLocator,
    StoredObject,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_backend(**overrides):
    kwargs = dict(bucket='mybucket', region='us-east-1')
    kwargs.update(overrides)
    return S3StorageBackend(**kwargs)


def patch_client(backend, fake_client):
    """Patch _get_client so it returns the provided fake client."""
    return patch.object(
        S3StorageBackend, '_get_client', return_value=fake_client
    )


def make_head_response(size=123, etag='"abc123"', content_type='audio/mpeg',
                       last_modified=None):
    return {
        'ContentLength': size,
        'ETag': etag,
        'ContentType': content_type,
        'LastModified': last_modified or datetime(2024, 1, 1, 12, 0, 0),
    }


def make_client_error(code='404', http_status=404, operation='HeadObject'):
    response = {
        'Error': {'Code': str(code), 'Message': 'boom'},
        'ResponseMetadata': {'HTTPStatusCode': http_status},
    }
    return ClientError(response, operation)


def s3_locator(bucket='mybucket', key='audio/file.mp3'):
    return StorageLocator(
        scheme='s3',
        raw=f's3://{bucket}/{key}',
        bucket=bucket,
        key=key,
    )


# ---------------------------------------------------------------------------
# build_locator
# ---------------------------------------------------------------------------

def test_build_locator():
    backend = make_backend()
    assert backend.build_locator('audio/file.mp3') == 's3://mybucket/audio/file.mp3'


def test_build_locator_normalizes_key():
    backend = make_backend()
    # leading slashes + doubled slashes get normalized away
    assert backend.build_locator('/audio//file.mp3') == 's3://mybucket/audio/file.mp3'


# ---------------------------------------------------------------------------
# _bucket_key
# ---------------------------------------------------------------------------

def test_bucket_key_uses_locator_bucket():
    backend = make_backend()
    bucket, key = backend._bucket_key(s3_locator(bucket='otherbucket', key='k/v.mp3'))
    assert bucket == 'otherbucket'
    assert key == 'k/v.mp3'


def test_bucket_key_falls_back_to_backend_bucket():
    backend = make_backend()
    loc = StorageLocator(scheme='s3', raw='s3:///k', bucket=None, key='k')
    bucket, key = backend._bucket_key(loc)
    assert bucket == 'mybucket'
    assert key == 'k'


def test_bucket_key_bad_scheme_raises():
    backend = make_backend()
    loc = StorageLocator(scheme='local', raw='local://k', key='k')
    with pytest.raises(ValueError, match='Unsupported locator'):
        backend._bucket_key(loc)


def test_bucket_key_missing_key_raises():
    backend = make_backend()
    loc = StorageLocator(scheme='s3', raw='s3://mybucket/', bucket='mybucket', key=None)
    with pytest.raises(ValueError, match='missing bucket or key'):
        backend._bucket_key(loc)


def test_bucket_key_missing_bucket_raises():
    backend = make_backend(bucket='')
    loc = StorageLocator(scheme='s3', raw='s3:///k', bucket=None, key='k')
    with pytest.raises(ValueError, match='missing bucket or key'):
        backend._bucket_key(loc)


# ---------------------------------------------------------------------------
# save_fileobj
# ---------------------------------------------------------------------------

def test_save_fileobj_with_content_type_and_metadata():
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.return_value = make_head_response(size=42, etag='"deadbeef"',
                                                       content_type='audio/wav')
    fileobj = MagicMock()
    with patch_client(backend, fake):
        result = backend.save_fileobj(
            fileobj, 'audio/x.wav', content_type='audio/wav',
            metadata={'owner': 'u1'},
        )
    fake.upload_fileobj.assert_called_once_with(
        fileobj, 'mybucket', 'audio/x.wav',
        ExtraArgs={'ContentType': 'audio/wav', 'Metadata': {'owner': 'u1'}},
    )
    assert isinstance(result, StoredObject)
    assert result.locator == 's3://mybucket/audio/x.wav'
    assert result.key == 'audio/x.wav'
    assert result.size == 42
    assert result.etag == 'deadbeef'
    assert result.content_type == 'audio/wav'


def test_save_fileobj_without_extra_args():
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.return_value = make_head_response()
    fileobj = MagicMock()
    with patch_client(backend, fake):
        backend.save_fileobj(fileobj, 'audio/y.mp3')
    fake.upload_fileobj.assert_called_once_with(fileobj, 'mybucket', 'audio/y.mp3')


# ---------------------------------------------------------------------------
# upload_local_file
# ---------------------------------------------------------------------------

def test_upload_local_file_with_content_type():
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.return_value = make_head_response(size=10)
    with patch_client(backend, fake):
        result = backend.upload_local_file(
            '/tmp/src.mp3', 'audio/z.mp3', content_type='audio/mpeg',
            metadata={'a': 'b'},
        )
    fake.upload_file.assert_called_once_with(
        '/tmp/src.mp3', 'mybucket', 'audio/z.mp3',
        ExtraArgs={'ContentType': 'audio/mpeg', 'Metadata': {'a': 'b'}},
    )
    assert result.key == 'audio/z.mp3'
    assert result.size == 10


def test_upload_local_file_without_extra_args():
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.return_value = make_head_response()
    with patch_client(backend, fake):
        backend.upload_local_file('/tmp/src.mp3', 'audio/z.mp3')
    fake.upload_file.assert_called_once_with('/tmp/src.mp3', 'mybucket', 'audio/z.mp3')


def test_upload_local_file_delete_source_removes_file():
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.return_value = make_head_response()
    fd, tmp_path = tempfile.mkstemp(prefix='speakr_test_src_')
    os.close(fd)
    assert os.path.exists(tmp_path)
    with patch_client(backend, fake):
        backend.upload_local_file(tmp_path, 'audio/del.mp3', delete_source=True)
    assert not os.path.exists(tmp_path)


def test_upload_local_file_delete_source_missing_file_ignored():
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.return_value = make_head_response()
    missing = '/tmp/definitely_not_here_speakr_xyz.mp3'
    with patch_client(backend, fake):
        # should not raise even though the file doesn't exist
        result = backend.upload_local_file(missing, 'audio/del.mp3', delete_source=True)
    # The missing-source deletion is swallowed; the upload still completes and
    # returns a valid StoredObject pointing at the uploaded key.
    assert isinstance(result, StoredObject)
    assert result.locator == 's3://mybucket/audio/del.mp3'
    assert result.key == 'audio/del.mp3'
    fake.upload_file.assert_called_once_with(missing, 'mybucket', 'audio/del.mp3')


# ---------------------------------------------------------------------------
# exists
# ---------------------------------------------------------------------------

def test_exists_true_when_head_succeeds():
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.return_value = make_head_response()
    with patch_client(backend, fake):
        assert backend.exists(s3_locator()) is True


@pytest.mark.parametrize('code,http_status', [
    ('404', 404),
    ('NoSuchKey', 200),
    ('NotFound', 200),
    ('SomethingElse', 404),  # 404 status code path
])
def test_exists_false_on_not_found(code, http_status):
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.side_effect = make_client_error(code=code, http_status=http_status)
    with patch_client(backend, fake):
        assert backend.exists(s3_locator()) is False


def test_exists_reraises_other_client_errors():
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.side_effect = make_client_error(code='AccessDenied', http_status=403)
    with patch_client(backend, fake):
        with pytest.raises(ClientError):
            backend.exists(s3_locator())


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_calls_delete_object():
    backend = make_backend()
    fake = MagicMock()
    with patch_client(backend, fake):
        result = backend.delete(s3_locator(key='audio/del.mp3'))
    fake.delete_object.assert_called_once_with(Bucket='mybucket', Key='audio/del.mp3')
    assert result is True


# ---------------------------------------------------------------------------
# stat
# ---------------------------------------------------------------------------

def test_stat_maps_fields():
    backend = make_backend()
    fake = MagicMock()
    lm = datetime(2023, 5, 6, 7, 8, 9)
    fake.head_object.return_value = make_head_response(
        size=999, etag='"quoted-etag"', content_type='audio/ogg', last_modified=lm,
    )
    with patch_client(backend, fake):
        stat = backend.stat(s3_locator(key='audio/s.ogg'))
    fake.head_object.assert_called_once_with(Bucket='mybucket', Key='audio/s.ogg')
    assert isinstance(stat, ObjectStat)
    assert stat.size == 999
    assert stat.etag == 'quoted-etag'  # surrounding quotes stripped
    assert stat.content_type == 'audio/ogg'
    assert stat.last_modified == lm


def test_stat_handles_missing_etag():
    backend = make_backend()
    fake = MagicMock()
    fake.head_object.return_value = {
        'ContentLength': 5,
        'ContentType': 'application/octet-stream',
        'LastModified': datetime(2024, 1, 1),
    }
    with patch_client(backend, fake):
        stat = backend.stat(s3_locator())
    assert stat.etag is None
    assert stat.size == 5


# ---------------------------------------------------------------------------
# materialize
# ---------------------------------------------------------------------------

def test_materialize_downloads_to_temp_and_flags_cleanup():
    backend = make_backend()
    fake = MagicMock()
    with patch_client(backend, fake):
        result = backend.materialize(s3_locator(key='audio/m.flac'))
    assert isinstance(result, MaterializedFile)
    assert result.cleanup_required is True
    assert result.local_path.endswith('.flac')
    fake.download_file.assert_called_once()
    args, kwargs = fake.download_file.call_args
    assert args[0] == 'mybucket'
    assert args[1] == 'audio/m.flac'
    assert args[2] == result.local_path
    # the temp file was created by mkstemp; clean up after the test
    if os.path.exists(result.local_path):
        os.remove(result.local_path)


# ---------------------------------------------------------------------------
# presign_get_url
# ---------------------------------------------------------------------------

def test_presign_get_url_basic():
    backend = make_backend()
    fake = MagicMock()
    fake.generate_presigned_url.return_value = 'https://signed.example/url'
    with patch_client(backend, fake):
        url = backend.presign_get_url(s3_locator(key='audio/p.mp3'), 600)
    assert url == 'https://signed.example/url'
    fake.generate_presigned_url.assert_called_once_with(
        'get_object',
        Params={'Bucket': 'mybucket', 'Key': 'audio/p.mp3'},
        ExpiresIn=600,
    )


def test_presign_get_url_with_response_overrides():
    backend = make_backend()
    fake = MagicMock()
    fake.generate_presigned_url.return_value = 'https://signed.example/url2'
    with patch_client(backend, fake):
        backend.presign_get_url(
            s3_locator(key='audio/p.mp3'),
            expires_seconds=120,
            response_content_type='audio/mpeg',
            response_content_disposition='attachment; filename="p.mp3"',
        )
    fake.generate_presigned_url.assert_called_once_with(
        'get_object',
        Params={
            'Bucket': 'mybucket',
            'Key': 'audio/p.mp3',
            'ResponseContentType': 'audio/mpeg',
            'ResponseContentDisposition': 'attachment; filename="p.mp3"',
        },
        ExpiresIn=120,
    )


# ---------------------------------------------------------------------------
# _get_client config wiring (patch boto3.client and inspect kwargs)
# ---------------------------------------------------------------------------

def test_get_client_path_style_addressing():
    backend = make_backend(
        endpoint_url='http://minio:9000',
        access_key_id='AK',
        secret_access_key='SK',
        session_token='ST',
        use_path_style=True,
        verify_ssl=False,
    )
    fake_boto_client = MagicMock()
    fake_config_instance = MagicMock()
    with patch('boto3.client', return_value=fake_boto_client) as mock_client, \
            patch('botocore.config.Config', return_value=fake_config_instance) as mock_config:
        client = backend._get_client()

    assert client is fake_boto_client
    kwargs = mock_client.call_args.kwargs
    assert kwargs['service_name'] == 's3'
    assert kwargs['verify'] is False
    assert kwargs['region_name'] == 'us-east-1'
    assert kwargs['endpoint_url'] == 'http://minio:9000'
    assert kwargs['aws_access_key_id'] == 'AK'
    assert kwargs['aws_secret_access_key'] == 'SK'
    assert kwargs['aws_session_token'] == 'ST'
    assert kwargs['config'] is fake_config_instance
    config_kwargs = mock_config.call_args.kwargs
    assert config_kwargs['signature_version'] == 's3v4'
    assert config_kwargs['s3'] == {'addressing_style': 'path'}


def test_get_client_virtual_addressing_and_minimal_kwargs():
    backend = S3StorageBackend(bucket='b')  # no region/endpoint/creds
    fake_boto_client = MagicMock()
    fake_config_instance = MagicMock()
    with patch('boto3.client', return_value=fake_boto_client) as mock_client, \
            patch('botocore.config.Config', return_value=fake_config_instance) as mock_config:
        backend._get_client()

    kwargs = mock_client.call_args.kwargs
    assert kwargs['verify'] is True
    assert 'region_name' not in kwargs
    assert 'endpoint_url' not in kwargs
    assert 'aws_access_key_id' not in kwargs
    config_kwargs = mock_config.call_args.kwargs
    assert config_kwargs['s3'] == {'addressing_style': 'auto'}


def test_get_client_is_cached():
    backend = make_backend()
    fake_boto_client = MagicMock()
    with patch('boto3.client', return_value=fake_boto_client) as mock_client, \
            patch('botocore.config.Config', return_value=MagicMock()):
        first = backend._get_client()
        second = backend._get_client()
    assert first is second
    mock_client.assert_called_once()


# ===========================================================================
# LocalStorageBackend (src/services/storage/local.py)
# Mutation survivors (2026-06-25): the empty key/path guards in resolve_path,
# the same-path no-op in upload_local_file, the missing-file delete branch,
# and the cleanup_required=False flag in materialize.
# ===========================================================================

def make_local_backend():
    root = tempfile.mkdtemp(prefix='speakr_local_')
    return LocalStorageBackend(root), root


def _write(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def test_local_resolve_path_local_scheme_missing_key_raises():
    backend, _ = make_local_backend()
    loc = StorageLocator(scheme='local', raw='local://', key='')
    with pytest.raises(ValueError):
        backend.resolve_path(loc)


def test_local_resolve_path_legacy_abs_missing_path_raises():
    backend, _ = make_local_backend()
    loc = StorageLocator(scheme='legacy_local_abs', raw='', path='')
    with pytest.raises(ValueError):
        backend.resolve_path(loc)


def test_local_resolve_path_legacy_rel_missing_key_raises():
    backend, _ = make_local_backend()
    loc = StorageLocator(scheme='legacy_local_rel', raw='', key='')
    with pytest.raises(ValueError):
        backend.resolve_path(loc)


def test_local_upload_same_path_is_noop():
    # The source IS the destination. The `src_resolved != dst_resolved` guard
    # must skip the copy/move (a `!=`->`==` mutation would attempt copy2 on an
    # identical path and raise SameFileError).
    backend, root = make_local_backend()
    key = 'recordings/same.mp3'
    dst = local_path_from_key(root, key)
    _write(dst, b'hello')
    result = backend.upload_local_file(dst, key, delete_source=False)
    assert os.path.exists(dst)
    with open(dst, 'rb') as f:
        assert f.read() == b'hello'
    assert result.size == 5


def test_local_upload_different_path_copies():
    backend, root = make_local_backend()
    fd, src = tempfile.mkstemp(prefix='speakr_src_')
    os.write(fd, b'data123')
    os.close(fd)
    try:
        result = backend.upload_local_file(src, 'recordings/copied.mp3', delete_source=False)
        assert os.path.exists(src)  # copy, not move -> source survives
        dst = local_path_from_key(root, 'recordings/copied.mp3')
        assert os.path.exists(dst)
        assert result.size == 7
    finally:
        if os.path.exists(src):
            os.remove(src)


def test_local_delete_missing_returns_missing_ok():
    backend, root = make_local_backend()
    loc = StorageLocator(scheme='local', raw='local://nope.mp3', key='nope.mp3')
    assert backend.delete(loc, missing_ok=True) is True
    assert backend.delete(loc, missing_ok=False) is False


def test_local_delete_existing_returns_true_and_removes():
    backend, root = make_local_backend()
    key = 'recordings/del.mp3'
    dst = local_path_from_key(root, key)
    _write(dst)
    loc = StorageLocator(scheme='local', raw=f'local://{key}', key=key)
    assert backend.delete(loc) is True
    assert not os.path.exists(dst)


def test_local_materialize_does_not_request_cleanup():
    # A local file is already on disk; materialize must NOT flag it for
    # deletion (cleanup_required must stay False, else the real file is removed).
    backend, root = make_local_backend()
    key = 'recordings/m.mp3'
    dst = local_path_from_key(root, key)
    _write(dst)
    loc = StorageLocator(scheme='local', raw=f'local://{key}', key=key)
    mat = backend.materialize(loc)
    assert mat.cleanup_required is False
    assert mat.local_path == dst


# ===========================================================================
# StorageService (src/services/storage/service.py) without an S3 backend
# Mutation survivors (2026-06-25): empty-locator guards and the
# "S3 not configured" RuntimeErrors.
# ===========================================================================

def make_no_s3_service(backend='local'):
    root = tempfile.mkdtemp(prefix='speakr_svc_')
    settings = StorageSettings(
        backend=backend,
        local_root=root,
        key_prefix='recordings',
        staging_dir=os.path.join(root, 'staging'),
        presign_ttl_seconds=300,
        presign_public_ttl_seconds=600,
        s3_bucket_name=None,  # -> build_s3_backend returns None
    )
    return StorageService(settings=settings), root


def test_service_no_s3_backend_is_none():
    svc, _ = make_no_s3_service()
    assert svc.s3 is None


def test_service_parse_empty_raises():
    svc, _ = make_no_s3_service()
    with pytest.raises(ValueError, match='Empty storage locator'):
        svc.exists('')


def test_service_delete_empty_returns_missing_ok():
    svc, _ = make_no_s3_service()
    assert svc.delete('', missing_ok=True) is True
    assert svc.delete(None, missing_ok=False) is False


def test_service_normalize_empty_returns_input_unchanged():
    svc, _ = make_no_s3_service()
    assert svc.maybe_normalize_local_legacy_locator('') == ''
    assert svc.maybe_normalize_local_legacy_locator(None) is None


def test_service_normalize_unparseable_returns_input():
    # whitespace-only -> parse_locator returns None -> the `if not locator`
    # guard returns the input rather than dereferencing None.
    svc, _ = make_no_s3_service()
    assert svc.maybe_normalize_local_legacy_locator('   ') == '   '


def test_service_s3_locator_without_backend_raises():
    # _resolve_backend_for_locator: s3 scheme + no s3 client -> RuntimeError.
    svc, _ = make_no_s3_service()
    with pytest.raises(RuntimeError, match='not configured'):
        svc.exists('s3://bucket/key.mp3')


def test_service_build_default_locator_s3_backend_no_client_raises():
    svc, _ = make_no_s3_service(backend='s3')
    with pytest.raises(RuntimeError, match='not configured'):
        svc.build_default_locator('recordings/x.mp3')


def test_service_upload_local_file_s3_backend_no_client_raises():
    svc, _ = make_no_s3_service(backend='s3')
    with pytest.raises(RuntimeError, match='not configured'):
        svc.upload_local_file('/tmp/x.mp3', 'recordings/x.mp3')
