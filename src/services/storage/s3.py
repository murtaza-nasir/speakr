"""S3-compatible storage backend (AWS S3 / MinIO)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Optional

from .interfaces import MaterializedFile, ObjectStat, StorageLocator, StoredObject
from .locator import build_s3_locator


class S3StorageBackend:
    """S3 storage backend with lazy boto3 initialization."""

    def __init__(self, *, bucket: str, region: Optional[str] = None, endpoint_url: Optional[str] = None,
                 access_key_id: Optional[str] = None, secret_access_key: Optional[str] = None,
                 session_token: Optional[str] = None, use_path_style: bool = False,
                 verify_ssl: bool = True):
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.session_token = session_token
        self.use_path_style = use_path_style
        self.verify_ssl = verify_ssl
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            import boto3
            from botocore.config import Config
        except Exception as exc:
            raise RuntimeError('S3 backend requires boto3 and botocore installed') from exc

        client_kwargs = {
            'service_name': 's3',
            'verify': self.verify_ssl,
        }
        if self.region:
            client_kwargs['region_name'] = self.region
        if self.endpoint_url:
            client_kwargs['endpoint_url'] = self.endpoint_url
        if self.access_key_id:
            client_kwargs['aws_access_key_id'] = self.access_key_id
        if self.secret_access_key:
            client_kwargs['aws_secret_access_key'] = self.secret_access_key
        if self.session_token:
            client_kwargs['aws_session_token'] = self.session_token

        addressing_style = 'path' if self.use_path_style else 'auto'
        client_kwargs['config'] = Config(signature_version='s3v4', s3={'addressing_style': addressing_style})

        self._client = boto3.client(**client_kwargs)
        return self._client

    def _bucket_key(self, locator: StorageLocator):
        if locator.scheme != 's3':
            raise ValueError(f"Unsupported locator for S3 backend: {locator.scheme}")
        bucket = locator.bucket or self.bucket
        key = locator.key
        if not bucket or not key:
            raise ValueError('S3 locator missing bucket or key')
        return bucket, key

    def build_locator(self, key: str) -> str:
        return build_s3_locator(self.bucket, key)

    def save_fileobj(self, fileobj: BinaryIO, key: str, content_type: Optional[str] = None, metadata: Optional[dict] = None) -> StoredObject:
        client = self._get_client()
        extra = {}
        if content_type:
            extra['ContentType'] = content_type
        if metadata:
            extra['Metadata'] = metadata
        if extra:
            client.upload_fileobj(fileobj, self.bucket, key, ExtraArgs=extra)
        else:
            client.upload_fileobj(fileobj, self.bucket, key)
        stat = self.stat(StorageLocator(scheme='s3', raw=self.build_locator(key), bucket=self.bucket, key=key))
        return StoredObject(locator=self.build_locator(key), key=key, size=stat.size, content_type=stat.content_type, etag=stat.etag)

    def upload_local_file(self, local_path: str, key: str, content_type: Optional[str] = None, metadata: Optional[dict] = None, delete_source: bool = False) -> StoredObject:
        client = self._get_client()
        extra = {}
        if content_type:
            extra['ContentType'] = content_type
        if metadata:
            extra['Metadata'] = metadata
        if extra:
            client.upload_file(local_path, self.bucket, key, ExtraArgs=extra)
        else:
            client.upload_file(local_path, self.bucket, key)
        if delete_source:
            try:
                os.remove(local_path)
            except FileNotFoundError:
                pass
        stat = self.stat(StorageLocator(scheme='s3', raw=self.build_locator(key), bucket=self.bucket, key=key))
        return StoredObject(locator=self.build_locator(key), key=key, size=stat.size, content_type=stat.content_type, etag=stat.etag)

    def exists(self, locator: StorageLocator) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.stat(locator)
            return True
        except ClientError as exc:
            response = getattr(exc, 'response', {}) or {}
            status_code = (response.get('ResponseMetadata') or {}).get('HTTPStatusCode')
            error_code = str((response.get('Error') or {}).get('Code') or '')
            if status_code == 404 or error_code in ('404', 'NoSuchKey', 'NotFound'):
                return False
            raise

    def delete(self, locator: StorageLocator, missing_ok: bool = True) -> bool:
        client = self._get_client()
        bucket, key = self._bucket_key(locator)
        client.delete_object(Bucket=bucket, Key=key)
        return True

    def stat(self, locator: StorageLocator) -> ObjectStat:
        client = self._get_client()
        bucket, key = self._bucket_key(locator)
        data = client.head_object(Bucket=bucket, Key=key)
        return ObjectStat(
            size=data.get('ContentLength'),
            last_modified=data.get('LastModified'),
            etag=(data.get('ETag') or '').strip('"') or None,
            content_type=data.get('ContentType'),
        )

    def materialize(self, locator: StorageLocator) -> MaterializedFile:
        client = self._get_client()
        bucket, key = self._bucket_key(locator)
        suffix = Path(key).suffix
        fd, tmp_path = tempfile.mkstemp(prefix='speakr_s3_', suffix=suffix)
        os.close(fd)
        client.download_file(bucket, key, tmp_path)
        return MaterializedFile(local_path=tmp_path, cleanup_required=True)

    def presign_get_url(self, locator: StorageLocator, expires_seconds: int, response_content_type: Optional[str] = None,
                        response_content_disposition: Optional[str] = None) -> str:
        client = self._get_client()
        bucket, key = self._bucket_key(locator)
        params = {'Bucket': bucket, 'Key': key}
        if response_content_type:
            params['ResponseContentType'] = response_content_type
        if response_content_disposition:
            params['ResponseContentDisposition'] = response_content_disposition
        return client.generate_presigned_url(
            'get_object',
            Params=params,
            ExpiresIn=int(expires_seconds),
        )
