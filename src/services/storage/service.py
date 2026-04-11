"""Storage service facade supporting local, S3, and temporary legacy path compatibility."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4
import threading

from .factory import StorageSettings, build_local_backend, build_s3_backend, load_storage_settings_from_env
from .interfaces import AudioDeliveryResult, MaterializedFile, StorageLocator, StoredObject
from .locator import build_local_locator, parse_locator, relative_key_from_local_path


@dataclass
class _ResolvedBackend:
    kind: str
    backend: object
    locator: StorageLocator


class StorageService:
    """Facade to hide storage backend details from business logic."""

    def __init__(self, settings: Optional[StorageSettings] = None):
        self.settings = settings or load_storage_settings_from_env()
        self.local = build_local_backend(self.settings)
        self.s3 = build_s3_backend(self.settings)

    def _parse(self, locator_value: str) -> StorageLocator:
        locator = parse_locator(locator_value)
        if not locator:
            raise ValueError('Empty storage locator')
        return locator

    def parse_locator(self, locator_value: str) -> Optional[StorageLocator]:
        return parse_locator(locator_value)

    def _resolve_backend_for_locator(self, locator_value: str) -> _ResolvedBackend:
        locator = self._parse(locator_value)
        if locator.scheme == 's3':
            if not self.s3:
                raise RuntimeError('S3 locator encountered but S3 backend is not configured')
            return _ResolvedBackend(kind='s3', backend=self.s3, locator=locator)
        return _ResolvedBackend(kind='local', backend=self.local, locator=locator)

    def default_backend_kind(self) -> str:
        return self.settings.backend

    def get_staging_dir(self) -> str:
        Path(self.settings.staging_dir).mkdir(parents=True, exist_ok=True)
        return self.settings.staging_dir

    def build_recording_key(self, original_filename: Optional[str], recording_id: Optional[int] = None, *, now: Optional[datetime] = None) -> str:
        now = now or datetime.utcnow()
        base_name = os.path.basename(original_filename or 'recording.bin').replace('\\', '_').replace('/', '_')
        safe_name = ''.join(c for c in base_name if c.isalnum() or c in (' ', '.', '-', '_')).strip() or 'recording.bin'
        safe_name = safe_name.replace(' ', '_')
        ts = now.strftime('%Y%m%d%H%M%S')
        rec_part = str(recording_id) if recording_id is not None else f"tmp-{uuid4().hex[:8]}"
        prefix = (self.settings.key_prefix or 'recordings').strip('/').replace('\\', '/')
        return f"{prefix}/{now.strftime('%Y/%m')}/{rec_part}/{ts}_{safe_name}"

    def build_default_locator(self, key: str) -> str:
        if self.settings.backend == 's3':
            if not self.s3:
                raise RuntimeError('FILE_STORAGE_BACKEND=s3 but S3 backend is not configured')
            return self.s3.build_locator(key)
        return self.local.build_locator(key)

    def build_local_locator_from_path(self, abs_path: str) -> str:
        key = relative_key_from_local_path(abs_path, self.settings.local_root)
        return build_local_locator(key)

    def maybe_normalize_local_legacy_locator(self, locator_value: Optional[str]) -> Optional[str]:
        if not locator_value:
            return locator_value
        locator = parse_locator(locator_value)
        if not locator:
            return locator_value
        if locator.scheme == 'legacy_local_abs':
            return self.build_local_locator_from_path(locator.path)
        if locator.scheme == 'legacy_local_rel':
            return build_local_locator(locator.key or '')
        return locator_value

    def resolve_local_filesystem_path(self, locator_value: str) -> str:
        resolved = self._resolve_backend_for_locator(locator_value)
        if resolved.kind != 'local':
            raise ValueError('Locator is not local')
        return self.local.resolve_path(resolved.locator)

    def exists(self, locator_value: str) -> bool:
        resolved = self._resolve_backend_for_locator(locator_value)
        return resolved.backend.exists(resolved.locator)

    def delete(self, locator_value: Optional[str], missing_ok: bool = True) -> bool:
        if not locator_value:
            return bool(missing_ok)
        resolved = self._resolve_backend_for_locator(locator_value)
        return resolved.backend.delete(resolved.locator, missing_ok=missing_ok)

    def upload_local_file(self, local_path: str, key: str, *, content_type: Optional[str] = None, delete_source: bool = False) -> StoredObject:
        if self.settings.backend == 's3':
            if not self.s3:
                raise RuntimeError('FILE_STORAGE_BACKEND=s3 but S3 backend is not configured')
            return self.s3.upload_local_file(local_path, key, content_type=content_type, delete_source=delete_source)
        return self.local.upload_local_file(local_path, key, content_type=content_type, delete_source=delete_source)

    @contextmanager
    def materialize(self, locator_value: str) -> Iterator[MaterializedFile]:
        resolved = self._resolve_backend_for_locator(locator_value)
        materialized = resolved.backend.materialize(resolved.locator)
        try:
            yield materialized
        finally:
            if materialized.cleanup_required:
                try:
                    os.remove(materialized.local_path)
                except FileNotFoundError:
                    pass

    def get_audio_delivery(self, locator_value: str, *, download: bool = False, mime_type: Optional[str] = None,
                           download_name: Optional[str] = None, is_public: bool = False) -> AudioDeliveryResult:
        resolved = self._resolve_backend_for_locator(locator_value)
        if resolved.kind == 'local':
            local_path = self.local.resolve_path(resolved.locator)
            return AudioDeliveryResult(mode='local_file', local_path=local_path, mimetype=mime_type)

        ttl = self.settings.presign_public_ttl_seconds if is_public else self.settings.presign_ttl_seconds
        disposition = None
        if download:
            safe_name = (download_name or 'recording').replace('"', '')
            disposition = f'attachment; filename="{safe_name}"'
        url = self.s3.presign_get_url(
            resolved.locator,
            expires_seconds=ttl,
            response_content_type=mime_type,
            response_content_disposition=disposition,
        )
        return AudioDeliveryResult(mode='redirect_url', url=url, mimetype=mime_type)


_storage_service_singleton: Optional[StorageService] = None
_storage_service_singleton_lock = threading.Lock()


def get_storage_service() -> StorageService:
    global _storage_service_singleton
    if _storage_service_singleton is None:
        with _storage_service_singleton_lock:
            if _storage_service_singleton is None:
                _storage_service_singleton = StorageService()
    return _storage_service_singleton


def reset_storage_service_singleton() -> None:
    global _storage_service_singleton
    with _storage_service_singleton_lock:
        _storage_service_singleton = None
