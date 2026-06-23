"""Factory for configuring file storage backends from environment variables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .local import LocalStorageBackend
from .s3 import S3StorageBackend


@dataclass
class StorageSettings:
    backend: str
    local_root: str
    key_prefix: str
    staging_dir: str
    presign_ttl_seconds: int
    presign_public_ttl_seconds: int
    s3_bucket_name: Optional[str] = None
    s3_region: Optional[str] = None
    s3_endpoint_url: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_session_token: Optional[str] = None
    s3_use_path_style: bool = False
    s3_verify_ssl: bool = True


def load_storage_settings_from_env() -> StorageSettings:
    # Kept for backward compatibility of the factory API, but values now come
    # from app_config to avoid duplicated env parsing / drift.
    from src.config import app_config

    return StorageSettings(
        backend=(app_config.FILE_STORAGE_BACKEND or 'local').strip().lower() or 'local',
        local_root=app_config.UPLOAD_FOLDER,
        key_prefix=(app_config.FILE_STORAGE_KEY_PREFIX or 'recordings').strip().strip('/').replace('\\', '/'),
        staging_dir=app_config.FILE_STORAGE_STAGING_DIR,
        presign_ttl_seconds=int(app_config.S3_PRESIGN_TTL_SECONDS),
        presign_public_ttl_seconds=int(app_config.S3_PRESIGN_PUBLIC_TTL_SECONDS),
        s3_bucket_name=app_config.S3_BUCKET_NAME,
        s3_region=app_config.S3_REGION,
        s3_endpoint_url=app_config.S3_ENDPOINT_URL,
        s3_access_key_id=app_config.S3_ACCESS_KEY_ID,
        s3_secret_access_key=app_config.S3_SECRET_ACCESS_KEY,
        s3_session_token=app_config.S3_SESSION_TOKEN,
        s3_use_path_style=bool(app_config.S3_USE_PATH_STYLE),
        s3_verify_ssl=bool(app_config.S3_VERIFY_SSL),
    )


def build_local_backend(settings: StorageSettings) -> LocalStorageBackend:
    return LocalStorageBackend(settings.local_root)


def build_s3_backend(settings: StorageSettings) -> Optional[S3StorageBackend]:
    if not settings.s3_bucket_name:
        return None
    return S3StorageBackend(
        bucket=settings.s3_bucket_name,
        region=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
        session_token=settings.s3_session_token,
        use_path_style=settings.s3_use_path_style,
        verify_ssl=settings.s3_verify_ssl,
    )
