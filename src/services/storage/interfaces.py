"""Storage interfaces and shared dataclasses for file storage backends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class StorageLocator:
    """Parsed locator for local/s3/legacy paths."""

    scheme: str  # local | s3 | legacy_local_abs | legacy_local_rel
    raw: str
    key: Optional[str] = None
    bucket: Optional[str] = None
    path: Optional[str] = None  # absolute local path for legacy abs

    @property
    def is_legacy(self) -> bool:
        return self.scheme.startswith('legacy_')

    @property
    def is_local(self) -> bool:
        return self.scheme in ('local', 'legacy_local_abs', 'legacy_local_rel')

    @property
    def is_s3(self) -> bool:
        return self.scheme == 's3'


@dataclass
class StoredObject:
    """Result of storing an object."""

    locator: str
    key: str
    size: Optional[int] = None
    content_type: Optional[str] = None
    etag: Optional[str] = None


@dataclass
class ObjectStat:
    """Storage object metadata."""

    size: Optional[int] = None
    last_modified: Optional[datetime] = None
    etag: Optional[str] = None
    content_type: Optional[str] = None


@dataclass
class MaterializedFile:
    """Local filesystem path prepared for processing/read."""

    local_path: str
    cleanup_required: bool = False


@dataclass
class AudioDeliveryResult:
    """How API should deliver audio to client."""

    mode: str  # local_file | redirect_url
    mimetype: Optional[str] = None
    local_path: Optional[str] = None
    url: Optional[str] = None
