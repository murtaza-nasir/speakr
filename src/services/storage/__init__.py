"""Unified file storage service supporting local and S3 backends."""

from .interfaces import AudioDeliveryResult, MaterializedFile, ObjectStat, StorageLocator, StoredObject
from .locator import build_local_locator, build_s3_locator, parse_locator, relative_key_from_local_path
from .service import StorageService, get_storage_service, reset_storage_service_singleton

__all__ = [
    'AudioDeliveryResult',
    'MaterializedFile',
    'ObjectStat',
    'StorageLocator',
    'StoredObject',
    'build_local_locator',
    'build_s3_locator',
    'parse_locator',
    'relative_key_from_local_path',
    'StorageService',
    'get_storage_service',
    'reset_storage_service_singleton',
]
