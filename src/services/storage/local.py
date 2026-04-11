"""Local filesystem storage backend."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import BinaryIO, Optional

from .interfaces import MaterializedFile, ObjectStat, StorageLocator, StoredObject
from .locator import build_local_locator, local_path_from_key


class LocalStorageBackend:
    """Local filesystem implementation for the storage contract."""

    def __init__(self, root: str):
        self.root = str(Path(root))
        Path(self.root).mkdir(parents=True, exist_ok=True)

    def build_locator(self, key: str) -> str:
        return build_local_locator(key)

    def resolve_path(self, locator: StorageLocator) -> str:
        if locator.scheme == 'local':
            if not locator.key:
                raise ValueError('local locator missing key')
            return local_path_from_key(self.root, locator.key)
        if locator.scheme == 'legacy_local_abs':
            if not locator.path:
                raise ValueError('legacy absolute local path is empty')
            return locator.path
        if locator.scheme == 'legacy_local_rel':
            if not locator.key:
                raise ValueError('legacy relative local path is empty')
            return local_path_from_key(self.root, locator.key)
        raise ValueError(f"Unsupported locator for local backend: {locator.scheme}")

    def save_fileobj(self, fileobj: BinaryIO, key: str, content_type: Optional[str] = None, metadata: Optional[dict] = None) -> StoredObject:
        dst = local_path_from_key(self.root, key)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        with open(dst, 'wb') as out_f:
            shutil.copyfileobj(fileobj, out_f)
        size = os.path.getsize(dst)
        return StoredObject(locator=self.build_locator(key), key=key, size=size, content_type=content_type)

    def upload_local_file(self, local_path: str, key: str, content_type: Optional[str] = None, metadata: Optional[dict] = None, delete_source: bool = False) -> StoredObject:
        dst = local_path_from_key(self.root, key)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)

        src_resolved = str(Path(local_path).resolve())
        dst_resolved = str(Path(dst).resolve())
        if src_resolved != dst_resolved:
            if delete_source:
                shutil.move(local_path, dst)
            else:
                shutil.copy2(local_path, dst)
        size = os.path.getsize(dst)
        return StoredObject(locator=self.build_locator(key), key=key, size=size, content_type=content_type)

    def exists(self, locator: StorageLocator) -> bool:
        return os.path.exists(self.resolve_path(locator))

    def delete(self, locator: StorageLocator, missing_ok: bool = True) -> bool:
        path = self.resolve_path(locator)
        if not os.path.exists(path):
            return bool(missing_ok)
        os.remove(path)
        return True

    def stat(self, locator: StorageLocator) -> ObjectStat:
        path = self.resolve_path(locator)
        st = os.stat(path)
        return ObjectStat(size=st.st_size)

    def materialize(self, locator: StorageLocator) -> MaterializedFile:
        path = self.resolve_path(locator)
        return MaterializedFile(local_path=path, cleanup_required=False)

    def presign_get_url(self, locator: StorageLocator, *args, **kwargs) -> str:
        raise NotImplementedError('Local backend does not support presigned URLs')
