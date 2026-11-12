"""Media files in storage that no database row refers to.

Older releases deleted a recording's row without its file in some paths, so
an upload folder can hold media nobody can open. This module finds such
files. Two callers use it: the nightly ownerless cleanup only counts them for
its log line, and the admin command ``scripts/clean_unreferenced_files.py``
lists them and, when asked, deletes them or moves them to a quarantine
folder.

What is looked at (local storage):

- plain files directly in UPLOAD_FOLDER, the layout of older releases;
- every file below UPLOAD_FOLDER/<key prefix> (``recordings`` by default).

Other folders at the top of UPLOAD_FOLDER (``_sessions``, ``_staging``,
``debug``, ``*.parts`` work folders and anything else) are never looked at.
Inside the prefix tree, folders whose name starts with ``_`` or ``.`` and the
configured staging, session, export and watch folders are pruned. Dot files
and symbolic links are skipped, and so is any path that resolves outside
UPLOAD_FOLDER.

On S3 storage the objects under ``<key prefix>/`` in the configured bucket are
listed in the same way.

A file counts as referenced when a row points to it through
``recording.audio_path``, ``upload_join_part.audio_path`` or (defensively,
since it holds the client's file name) ``recording_session.upload_filename``.
Locators are resolved with the storage service, so ``local://`` keys,
legacy absolute paths and legacy relative paths all map to the same file.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional, Set, Tuple

from src.database import db

DEFAULT_MIN_AGE = timedelta(hours=48)

# Top-level folders of UPLOAD_FOLDER the app writes in-progress work to. Only
# the prefix tree is walked, so these are never scanned; the names are listed
# so that a prefix configured to one of them is refused.
IN_PROGRESS_FOLDERS = ('_sessions', '_staging')


@dataclass
class Candidate:
    """One unreferenced file or object."""
    backend: str          # 'local' | 's3'
    path: str             # absolute path (local) or object key (s3)
    size: int
    modified: datetime    # UTC, naive
    bucket: Optional[str] = None

    @property
    def display(self) -> str:
        return f"s3://{self.bucket}/{self.path}" if self.backend == 's3' else self.path


@dataclass
class MissingRow:
    """A row that points to a file that does not exist."""
    table: str
    row_id: object
    locator: str


@dataclass
class ScanResult:
    root: Optional[str] = None
    scanned_dirs: List[str] = field(default_factory=list)
    not_scanned: List[str] = field(default_factory=list)
    s3_bucket: Optional[str] = None
    s3_prefix: Optional[str] = None
    scanned: int = 0
    referenced: int = 0
    candidates: List[Candidate] = field(default_factory=list)
    recent: int = 0
    recent_bytes: int = 0
    skipped_links: List[str] = field(default_factory=list)
    kept_name_matches: List[str] = field(default_factory=list)
    missing_rows: List[MissingRow] = field(default_factory=list)
    truncated: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(c.size for c in self.candidates)


@dataclass
class References:
    local_paths: Set[str] = field(default_factory=set)        # real paths
    s3_objects: Set[Tuple[str, str]] = field(default_factory=set)  # (bucket, key)
    missing_rows: List[MissingRow] = field(default_factory=list)
    missing_names: Set[str] = field(default_factory=set)


def _settings(storage):
    return getattr(storage, 'settings', None)


def local_root(storage) -> Optional[str]:
    root = getattr(_settings(storage), 'local_root', None)
    if not root or not os.path.isdir(root):
        return None
    return os.path.realpath(root)


def key_prefix(storage) -> str:
    prefix = getattr(_settings(storage), 'key_prefix', '') or 'recordings'
    return prefix.strip().strip('/').replace('\\', '/') or 'recordings'


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _rows():
    """Yield (table, row id, locator, audio deleted) for every stored path."""
    from src.models import Recording, RecordingSession
    from src.models.upload_join import UploadJoinPart

    for rid, locator, deleted_at in (db.session.query(Recording.id, Recording.audio_path,
                                                      Recording.audio_deleted_at)
                                     .filter(Recording.audio_path.isnot(None))):
        yield 'recording', rid, locator, deleted_at is not None
    for pid, locator in (db.session.query(UploadJoinPart.id, UploadJoinPart.audio_path)
                         .filter(UploadJoinPart.audio_path.isnot(None))):
        yield 'upload_join_part', pid, locator, False
    # Not a stored path: the session's audio lives in _sessions/<id>/, which
    # is never scanned. Resolved anyway so that a file of that name in the
    # scanned area is kept.
    for sid, name in (db.session.query(RecordingSession.id, RecordingSession.upload_filename)
                      .filter(RecordingSession.upload_filename.isnot(None))):
        yield 'recording_session', sid, name, None


def collect_references(storage) -> References:
    """Every file or object a row refers to, and the rows whose file is missing.

    Missing-file detection covers recording and upload_join_part rows with a
    local locator; a recording whose audio was deleted on purpose (audio
    deleted by retention) is not reported.
    """
    refs = References()
    for table, row_id, locator, audio_deleted in _rows():
        locator = (locator or '').strip()
        if not locator:
            continue
        try:
            parsed = storage.parse_locator(locator)
        except Exception:
            parsed = None
        scheme = getattr(parsed, 'scheme', None)
        if scheme == 's3':
            bucket = parsed.bucket or getattr(_settings(storage), 's3_bucket_name', None)
            refs.s3_objects.add((bucket, parsed.key))
            continue
        try:
            path = storage.resolve_local_filesystem_path(locator)
        except Exception:
            continue  # unreadable locator, or one resolving outside the root
        if not isinstance(path, str):
            continue
        real = os.path.realpath(path)
        refs.local_paths.add(real)
        if audio_deleted is None or audio_deleted:
            continue
        if not os.path.exists(real):
            refs.missing_rows.append(MissingRow(table, row_id, locator))
            refs.missing_names.add(os.path.basename(real))
    return refs


def _protected_dirs(storage, root: str) -> Set[str]:
    """Real paths of folders that must never be walked into."""
    from src.config import app_config
    candidates = [
        getattr(_settings(storage), 'staging_dir', None),
        getattr(app_config, 'FILE_STORAGE_STAGING_DIR', None),
        os.environ.get('AUTO_EXPORT_DIR', '/data/exports'),
        os.environ.get('AUTO_PROCESS_WATCH_DIR', '/data/auto-process'),
    ]
    candidates += [os.path.join(root, name) for name in IN_PROGRESS_FOLDERS]
    return {os.path.realpath(p) for p in candidates if p}


def iter_local_files(storage, result: ScanResult) -> Iterator[str]:
    """Yield the files in the scanned area of local storage, as real paths.

    Fills result.root, scanned_dirs, not_scanned and skipped_links. Raises
    ValueError when the key prefix resolves outside the root or into an
    in-progress folder.
    """
    root = local_root(storage)
    if root is None:
        return
    result.root = root
    prefix = key_prefix(storage)
    prefix_dir = os.path.realpath(os.path.join(root, *prefix.split('/')))
    if not _within(prefix_dir, root) or prefix_dir == root:
        raise ValueError(f"Key prefix '{prefix}' does not resolve to a folder inside {root}")
    protected = _protected_dirs(storage, root)
    if any(_within(prefix_dir, p) for p in protected):
        raise ValueError(f"Key prefix '{prefix}' lies inside an in-progress folder")

    result.scanned_dirs = [f"{root} (top level only)"]
    if os.path.isdir(prefix_dir):
        result.scanned_dirs.append(prefix_dir)
    top_prefix = prefix.split('/')[0]

    with os.scandir(root) as entries:
        for entry in sorted(entries, key=lambda e: e.name):
            if entry.is_dir(follow_symlinks=False):
                if entry.name != top_prefix:
                    result.not_scanned.append(entry.name)
                continue
            if entry.name.startswith('.'):
                continue
            if entry.is_symlink():
                result.skipped_links.append(entry.path)
                continue
            if entry.is_file(follow_symlinks=False):
                yield entry.path

    if not os.path.isdir(prefix_dir):
        return
    for dirpath, dirnames, filenames in os.walk(prefix_dir):
        kept = []
        for name in sorted(dirnames):
            full = os.path.join(dirpath, name)
            if name.startswith(('_', '.')) or os.path.islink(full) or os.path.realpath(full) in protected:
                if os.path.islink(full):
                    result.skipped_links.append(full)
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            if name.startswith('.'):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                result.skipped_links.append(full)
                continue
            yield full


def _changed_at(st) -> datetime:
    # The later of modification and inode change: a file copied with
    # copy2 keeps an old mtime, but its ctime is the time of the copy.
    return datetime.utcfromtimestamp(max(st.st_mtime, st.st_ctime))


def scan(storage=None, *, min_age: Optional[timedelta] = DEFAULT_MIN_AGE, now: Optional[datetime] = None,
         limit: Optional[int] = None, include_s3: bool = True) -> ScanResult:
    """Find unreferenced files. Changes nothing.

    Files changed within min_age are counted in result.recent and are not
    candidates; min_age None applies no age filter. limit caps the number of
    files looked at (result.truncated is then set).
    """
    if storage is None:
        from src.services.storage import get_storage_service
        storage = get_storage_service()
    now = now or datetime.utcnow()
    cutoff = None if min_age is None else now - min_age
    result = ScanResult()
    refs = collect_references(storage)
    result.missing_rows = refs.missing_rows

    for path in iter_local_files(storage, result):
        result.scanned += 1
        if limit is not None and result.scanned > limit:
            result.truncated = True
            result.scanned -= 1
            break
        real = os.path.realpath(path)
        if not _within(real, result.root):
            result.skipped_links.append(path)
            continue
        if real in refs.local_paths:
            result.referenced += 1
            continue
        try:
            st = os.stat(real, follow_symlinks=False)
        except FileNotFoundError:
            continue
        changed = _changed_at(st)
        if cutoff is not None and changed > cutoff:
            result.recent += 1
            result.recent_bytes += st.st_size
            continue
        if os.path.basename(real) in refs.missing_names:
            result.kept_name_matches.append(real)
            continue
        result.candidates.append(Candidate('local', real, st.st_size, changed))

    if include_s3:
        _scan_s3(storage, refs, result, cutoff)
    return result


def s3_in_use(storage) -> bool:
    settings = _settings(storage)
    return bool(getattr(storage, 's3', None)) and getattr(settings, 'backend', 'local') == 's3'


def _scan_s3(storage, refs: References, result: ScanResult, cutoff: Optional[datetime]):
    if not s3_in_use(storage):
        return
    backend = storage.s3
    bucket = backend.bucket
    prefix = key_prefix(storage) + '/'
    result.s3_bucket, result.s3_prefix = bucket, prefix
    paginator = backend._get_client().get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []) or []:
            key = obj['Key']
            if key.endswith('/'):
                continue  # folder placeholder
            parts = key[len(prefix):].split('/')
            if any(p.startswith(('_', '.')) for p in parts):
                continue
            result.scanned += 1
            if (bucket, key) in refs.s3_objects:
                result.referenced += 1
                continue
            modified = obj.get('LastModified')
            if isinstance(modified, datetime) and modified.tzinfo is not None:
                modified = modified.astimezone(timezone.utc).replace(tzinfo=None)
            size = int(obj.get('Size') or 0)
            if cutoff is not None and (not isinstance(modified, datetime) or modified > cutoff):
                result.recent += 1
                result.recent_bytes += size
                continue
            if os.path.basename(key) in refs.missing_names:
                result.kept_name_matches.append(f"s3://{bucket}/{key}")
                continue
            if not isinstance(modified, datetime):
                modified = datetime.utcnow()
            result.candidates.append(Candidate('s3', key, size, modified, bucket=bucket))


@dataclass
class ApplyResult:
    removed: int = 0
    removed_bytes: int = 0
    skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def validate_move_target(target: str, storage) -> str:
    """Return the real path of a quarantine folder, or raise ValueError."""
    real = os.path.realpath(target)
    root = local_root(storage)
    if root is not None:
        prefix_dir = os.path.realpath(os.path.join(root, *key_prefix(storage).split('/')))
        if real == root or _within(real, prefix_dir):
            raise ValueError(f"{target} lies in the scanned area; choose a folder outside {prefix_dir}")
    if os.path.exists(real) and not os.path.isdir(real):
        raise ValueError(f"{target} exists and is not a folder")
    return real


def apply(candidates: List[Candidate], storage, *, min_age: timedelta = DEFAULT_MIN_AGE,
          now: Optional[datetime] = None, move_to: Optional[str] = None) -> ApplyResult:
    """Delete the candidates, or move them below move_to.

    The references are collected again and every file is checked again just
    before it is removed, so a row created since the scan keeps its file.
    """
    now = now or datetime.utcnow()
    cutoff = now - min_age
    out = ApplyResult()
    db.session.expire_all()
    refs = collect_references(storage)
    root = local_root(storage)
    target = validate_move_target(move_to, storage) if move_to else None

    for cand in candidates:
        try:
            if cand.backend == 's3':
                if target:
                    out.skipped.append(f"{cand.display}: --move-to supports local files only")
                    continue
                if (cand.bucket, cand.path) in refs.s3_objects:
                    out.skipped.append(f"{cand.display}: now referenced")
                    continue
                from src.services.storage.interfaces import StorageLocator
                storage.s3.delete(StorageLocator(scheme='s3', raw=cand.display,
                                                 bucket=cand.bucket, key=cand.path))
                out.removed += 1
                out.removed_bytes += cand.size
                continue

            real = os.path.realpath(cand.path)
            if root is None or not _within(real, root) or real != cand.path:
                out.skipped.append(f"{cand.path}: outside the uploads folder")
                continue
            if os.path.islink(cand.path) or not os.path.isfile(real):
                out.skipped.append(f"{cand.path}: no longer a plain file")
                continue
            if real in refs.local_paths or os.path.basename(real) in refs.missing_names:
                out.skipped.append(f"{cand.path}: now referenced")
                continue
            st = os.stat(real, follow_symlinks=False)
            if _changed_at(st) > cutoff:
                out.skipped.append(f"{cand.path}: changed since the scan")
                continue
            if target:
                dest = os.path.join(target, os.path.relpath(real, root))
                if os.path.lexists(dest):
                    out.skipped.append(f"{cand.path}: {dest} already exists")
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(real, dest)
            else:
                os.remove(real)
            out.removed += 1
            out.removed_bytes += st.st_size
        except Exception as e:
            out.errors.append(f"{cand.display}: {e}")
    return out


def format_size(n: int) -> str:
    size = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f"{int(size)} {unit}" if unit == 'B' else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"
