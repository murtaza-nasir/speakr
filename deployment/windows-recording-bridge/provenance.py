"""Immutable source facts for local Speakr bridge jobs."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import re
import json
import subprocess

from common import path_key, run, sha256, signature


UTC = timezone.utc
EARLIEST_SOURCE_DATE = datetime(2000, 1, 1, tzinfo=UTC)
ACR_NAME = re.compile(r'.+-[01]-(\d{13})\.m4a$', re.IGNORECASE)


def iso_utc(timestamp):
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def under_root(path, root):
    child = path_key(path)
    parent = path_key(root)
    try:
        return os.path.commonpath((child, parent)) == parent
    except ValueError:
        return False


def is_acr(path, cfg):
    return any(under_root(path, root) for root in cfg.get('exact_two_speaker_roots', ()))


def category_and_relative(path, cfg):
    roots = [(root, 'ACRPhone') for root in cfg.get('acr_timestamp_roots', ())]
    roots += [(root, Path(root).name) for root in cfg.get('sources', ())]
    matches = [(len(path_key(root)), root, label) for root, label in roots if under_root(path, root)]
    if not matches:
        return 'Ручная загрузка', None
    _, root, label = max(matches, key=lambda item: item[0])
    return label, os.path.relpath(path, root)


def acr_filename_time(path, cfg, now=None):
    if not any(under_root(path, root) for root in cfg.get('acr_timestamp_roots', ())):
        return None
    match = ACR_NAME.fullmatch(Path(path).name)
    if not match:
        return None
    try:
        timestamp = int(match.group(1)) / 1000
        parsed = datetime.fromtimestamp(timestamp, UTC)
    except (OSError, OverflowError, ValueError):
        return None
    if EARLIEST_SOURCE_DATE <= parsed <= (now or datetime.now(UTC)) + timedelta(days=1):
        return iso_utc(timestamp)
    return None


def media_creation(probe):
    fmt = probe.get('format') or {}
    value = (fmt.get('tags') or {}).get('creation_time')
    if value:
        return value, 'format.tags.creation_time'
    for index, stream in enumerate(probe.get('streams') or []):
        value = (stream.get('tags') or {}).get('creation_time')
        if value:
            return value, f'streams[{index}].tags.creation_time'
    return None, None


def snapshot(path, mtime, cfg, probe=None, status='verified'):
    path = Path(path)
    category, relative = category_and_relative(path, cfg)
    result = {
        'normalized_source_path': path_key(path),
        'source_path': str(path),
        'original_filename': path.name,
        'source_category': category,
        'relative_path': relative,
        'source_mtime_utc': iso_utc(mtime),
        'filesystem_created_utc': None,
        'media_creation_raw': None,
        'media_creation_source': None,
        'filename_epoch_utc': None,
        'selected_date_utc': iso_utc(mtime),
        'selected_date_basis': 'source_mtime_fallback',
        'source_status': status,
        'captured_at_utc': iso_utc(datetime.now(UTC).timestamp()),
    }
    if status != 'verified':
        return result
    stat = path.stat()
    created = getattr(stat, 'st_birthtime', None)
    if created is None and os.name == 'nt':
        created = stat.st_ctime
    result['filesystem_created_utc'] = iso_utc(created)
    result['media_creation_raw'], result['media_creation_source'] = media_creation(probe or {})
    result['filename_epoch_utc'] = acr_filename_time(path, cfg)
    if result['filename_epoch_utc']:
        result['selected_date_utc'] = result['filename_epoch_utc']
        result['selected_date_basis'] = 'acr_filename_epoch_inferred_call_start'
    return result


def verified_snapshot(path, mtime, digest, cfg, media_run=run):
    """Never borrow present-day metadata from a replaced historical source."""
    path = Path(path)
    if not path.is_file():
        return snapshot(path, mtime, cfg, status='missing')
    try:
        before = signature(path)
        if sha256(path) != digest or signature(path) != before:
            return snapshot(path, mtime, cfg, status='source_changed')
        try:
            probe = json.loads(media_run([cfg['ffprobe'], '-v', 'error', '-show_streams',
                                          '-show_format', '-of', 'json', path], timeout=90).stdout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            probe = {}
        if signature(path) != before:
            return snapshot(path, mtime, cfg, status='source_changed')
        return snapshot(path, path.stat().st_mtime if mtime is None else mtime, cfg, probe)
    except FileNotFoundError:
        return snapshot(path, mtime, cfg, status='missing')
    except OSError:
        return snapshot(path, mtime, cfg, status='source_changed')


def speakr_notes(source):
    lines = [
        'Источник записи',
        f'Исходное имя: {source["original_filename"]}',
        f'Категория: {source["source_category"]}',
    ]
    if source.get('relative_path'):
        lines.append(f'Путь внутри категории: {source["relative_path"]}')
    lines += [
        f'Дата для сортировки: {source["selected_date_utc"]} ({source["selected_date_basis"]})',
        f'Изменение исходного файла: {source["source_mtime_utc"]}',
    ]
    for key, label in (
        ('filesystem_created_utc', 'Создание локальной копии'),
        ('media_creation_raw', 'Дата внутри медиа'),
        ('filename_epoch_utc', 'Метка в имени ACRPhone'),
    ):
        if source.get(key):
            lines.append(f'{label}: {source[key]}')
    if source['source_status'] != 'verified':
        lines.append(f'Проверка исходника: {source["source_status"]}')
    if source.get('filename_epoch_utc'):
        lines.append('Метка в имени ACRPhone — предполагаемое начало звонка, не подтверждённое время записи.')
    return '\n'.join(line.replace('\r', ' ').replace('\n', ' ') for line in lines)
