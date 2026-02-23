"""Locator parsing/serialization helpers for local and S3 storage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .interfaces import StorageLocator

LOCAL_SCHEME = 'local://'
S3_SCHEME = 's3://'


def _normalize_key(key: str) -> str:
    key = (key or '').replace('\\', '/').strip()
    while '//' in key:
        key = key.replace('//', '/')
    return key.lstrip('/')


def build_local_locator(key: str) -> str:
    return f"{LOCAL_SCHEME}{_normalize_key(key)}"


def build_s3_locator(bucket: str, key: str) -> str:
    return f"{S3_SCHEME}{bucket}/{_normalize_key(key)}"


def is_probably_windows_abs(path: str) -> bool:
    if not path or len(path) < 3:
        return False
    return path[1] == ':' and path[2] in ('\\', '/')


def is_absolute_local_path(value: str) -> bool:
    if not value:
        return False
    if is_probably_windows_abs(value):
        return True
    return os.path.isabs(value)


def parse_locator(value: Optional[str]) -> Optional[StorageLocator]:
    """Parse locator string into a typed structure."""
    if value is None:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    if raw.startswith(LOCAL_SCHEME):
        key = _normalize_key(raw[len(LOCAL_SCHEME):])
        return StorageLocator(scheme='local', raw=raw, key=key)

    if raw.startswith(S3_SCHEME):
        tail = raw[len(S3_SCHEME):]
        if '/' not in tail:
            raise ValueError(f"Invalid s3 locator (missing key): {raw}")
        bucket, key = tail.split('/', 1)
        bucket = bucket.strip()
        key = _normalize_key(key)
        if not bucket or not key:
            raise ValueError(f"Invalid s3 locator: {raw}")
        return StorageLocator(scheme='s3', raw=raw, bucket=bucket, key=key)

    if is_absolute_local_path(raw):
        return StorageLocator(scheme='legacy_local_abs', raw=raw, path=raw)

    return StorageLocator(scheme='legacy_local_rel', raw=raw, key=_normalize_key(raw))


def local_path_from_key(local_root: str, key: str) -> str:
    """Resolve a storage key under local_root and prevent path traversal."""
    safe_key = _normalize_key(key)
    root = Path(local_root).resolve()
    candidate = (root / Path(*safe_key.split('/'))).resolve()
    try:
        candidate.relative_to(root)
    except Exception as exc:
        raise ValueError(f"Local storage key resolves outside root: {key}") from exc
    return str(candidate)


def relative_key_from_local_path(abs_path: str, local_root: str) -> str:
    """Convert absolute local path to a storage key relative to local root."""
    root = Path(local_root).resolve()
    path = Path(abs_path).resolve()
    try:
        rel = path.relative_to(root)
    except Exception as exc:
        raise ValueError(f"Path '{abs_path}' is outside local storage root '{local_root}'") from exc
    rel_key = _normalize_key(rel.as_posix())
    if not rel_key:
        raise ValueError(f"Cannot build key from path '{abs_path}'")
    return rel_key
