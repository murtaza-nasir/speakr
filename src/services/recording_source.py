"""Source adapters for Speakr's recording ingestion pipeline.

Speakr ingests recordings from more than one origin: an HTTP upload (a
Werkzeug ``FileStorage``) and a file dropped into the auto-process watch
directory (a path on disk).  Both feed the same :func:`ingest_uploaded_recording`
pipeline, but that function historically spoke only ``FileStorage``: it called
``.filename``, ``.seek``, ``.tell`` and ``.save`` on its input.

This module introduces the seam the ingestion pipeline actually needs: a
small :class:`RecordingSource` protocol that describes "something we can read
bytes from and copy to a staging path", plus one adapter per origin.  Two real
adapters justify the seam; a single one would be a speculative abstraction.

Adapters
--------
* :class:`FileStorageSource` - wraps a Werkzeug ``FileStorage`` (HTTP upload).
* :class:`PathSource` - wraps a path on disk (auto-process monitor).

The protocol is structural, so callers may pass either an adapter or a raw
``FileStorage``; :func:`coerce_source` normalises the latter.
"""

import os
import shutil
from typing import Protocol, runtime_checkable


@runtime_checkable
class RecordingSource(Protocol):
    """The minimal read/copy contract the ingestion pipeline relies on."""

    @property
    def filename(self) -> str:
        """Original filename of the source (used for MIME/extension hints)."""

    def seek(self, offset: int, whence: int = 0) -> int:
        ...

    def tell(self) -> int:
        ...

    def save(self, destination: str) -> None:
        """Copy the full source bytes into ``destination`` (a filesystem path)."""


class FileStorageSource:
    """Adapter around a Werkzeug ``FileStorage`` (the HTTP upload case)."""

    def __init__(self, file_storage):
        self._file = file_storage

    @property
    def filename(self) -> str:
        return getattr(self._file, 'filename', '') or ''

    def seek(self, offset, whence=0):
        return self._file.seek(offset, whence)

    def tell(self):
        return self._file.tell()

    def save(self, destination):
        self._file.save(destination)


class PathSource:
    """Adapter around a path on disk (the auto-process monitor case)."""

    def __init__(self, path):
        self._path = str(path)
        self._handle = None

    @property
    def filename(self) -> str:
        return os.path.basename(self._path)

    def _open(self):
        if self._handle is None or self._handle.closed:
            self._handle = open(self._path, 'rb')
        return self._handle

    def seek(self, offset, whence=0):
        return self._open().seek(offset, whence)

    def tell(self):
        return self._open().tell()

    def save(self, destination):
        # Copy the whole file; the ingestion pipeline then owns the staging copy.
        shutil.copyfile(self._path, destination)

    def close(self):
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
            self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def coerce_source(uploaded_file=None, source=None):
    """Return a :class:`RecordingSource` for either input form.

    ``source`` (an adapter) wins when given; otherwise a raw ``FileStorage``
    is wrapped in :class:`FileStorageSource`.  Returns ``None`` if neither is
    provided, so callers can keep their existing "no file" handling.
    """
    if source is not None:
        if isinstance(source, RecordingSource):
            return source
        # Duck-typed: anything exposing the contract is accepted as-is.
        if all(hasattr(source, attr) for attr in ('filename', 'seek', 'tell', 'save')):
            return source
        raise TypeError(f'unsupported source type: {type(source)!r}')
    if uploaded_file is None:
        return None
    if isinstance(uploaded_file, RecordingSource):
        return uploaded_file
    if all(hasattr(uploaded_file, attr) for attr in ('filename', 'seek', 'tell', 'save')):
        # Already FileStorage-like: wrap for uniformity only when it is not
        # already an adapter.  Wrapping is cheap and keeps the seam honest.
        return FileStorageSource(uploaded_file)
    raise TypeError(f'unsupported uploaded_file type: {type(uploaded_file)!r}')
