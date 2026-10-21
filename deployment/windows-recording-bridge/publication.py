"""One owned Obsidian note per recording, with durable off-vault revisions."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import html
import json
import os
from pathlib import Path
import re
import uuid

from common import is_reparse, safe_name

MOSCOW = timezone(timedelta(hours=3))


class NoteConflict(ValueError):
    """The actual replaced bytes differed from the reviewed note snapshot."""


def version_hash(detail, transcript, provenance=None):
    data = {'title': detail.get('title'), 'participants': detail.get('participants'),
            'transcript': transcript}
    if '_summary_status' in detail:
        data.update(format_version=2, summary_status=detail['_summary_status'],
                    summary=detail.get('summary') if detail['_summary_status'] == 'ready' else None)
    if provenance:
        data['provenance'] = sorted(provenance, key=lambda item: item['normalized_source_path'])
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def stamp(value):
    try:
        seconds = max(0, int(float(value)))
        return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'
    except (ValueError, TypeError, OverflowError):
        return '??:??:??'


def escape(text):
    # Protect YAML/HTML/wiki-link structure; words remain ordinary text.
    text = html.escape(str(text), quote=False)
    for char in '\\`*_{}[]()#!|>':
        text = text.replace(char, '\\' + char)
    return text.replace('\r', '').replace('\n', ' ')


def render(job, detail, transcript, version, url, previous=None, provenance=None):
    source = Path(job['source_path'])
    date = datetime.fromtimestamp(job['mtime'], MOSCOW)
    primary = provenance[0] if provenance else None
    metadata = {'type': 'transcription', 'bridge_id': job['id'], 'result_version': version,
                'source_path': str(source), 'source_sha256': job['sha256'],
                'source_timestamp': date.isoformat(),
                'imported_at': datetime.now(MOSCOW).isoformat(),
                'speakr_id': job['recording_id'], 'status': 'completed',
                'review_status': 'unreviewed'}
    if '_summary_status' in detail:
        metadata.update(transcription_status='completed', summary_status=detail['_summary_status'], publication_format=2)
    if primary:
        metadata.update(source_original_filename=primary['original_filename'],
                        source_category=primary['source_category'],
                        source_relative_path=primary['relative_path'],
                        source_file_modified_at=primary['source_mtime_utc'],
                        source_file_created_at=primary['filesystem_created_utc'],
                        source_media_creation_raw=primary['media_creation_raw'],
                        source_media_creation_source=primary['media_creation_source'],
                        source_filename_epoch_utc=primary['filename_epoch_utc'],
                        recording_time_utc=primary['selected_date_utc'],
                        recording_time_basis=primary['selected_date_basis'],
                        source_status=primary['source_status'],
                        source_aliases=[{
                            'path': s['source_path'], 'original_filename': s['original_filename'],
                            'category': s['source_category'], 'relative_path': s['relative_path'],
                            'file_modified_at': s['source_mtime_utc'],
                            'file_created_at': s['filesystem_created_utc'],
                            'media_creation_raw': s['media_creation_raw'],
                            'media_creation_source': s['media_creation_source'],
                            'filename_epoch_utc': s['filename_epoch_utc'],
                            'recording_time_utc': s['selected_date_utc'],
                            'recording_time_basis': s['selected_date_basis'],
                            'status': s['source_status']
                        } for s in provenance[1:]])
    lines = ['---'] + [f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in metadata.items()]
    lines += ['---', '', '# ' + escape(source.stem), '',
              '> Автоматическая расшифровка. Текст и разделение говорящих не проверены.', '',
              f'- Дата файла: {date:%d.%m.%Y %H:%M:%S} (Москва; дата самой записи не подтверждена).',
              f'- [Открыть в Speakr]({url}/recordings/{job["recording_id"]})']
    if primary:
        lines += ['', '## Источник и даты', '',
                  f'- Исходное имя: {escape(primary["original_filename"])}',
                  f'- Категория: {escape(primary["source_category"])}',
                  f'- Исходный путь: {escape(primary["source_path"])}',
                  f'- Дата для сортировки: {escape(primary["selected_date_utc"])} ({escape(primary["selected_date_basis"])}).',
                  f'- Изменение файла: {escape(primary["source_mtime_utc"])}.',
                  f'- Проверка исходника: {escape(primary["source_status"])}.']
        for key, label in (('filesystem_created_utc', 'Создание локальной копии'),
                           ('media_creation_raw', 'Дата внутри медиа'),
                           ('filename_epoch_utc', 'Метка времени в имени ACRPhone')):
            if primary.get(key):
                lines.append(f'- {label}: {escape(primary[key])}.')
        if primary['selected_date_basis'] == 'acr_filename_epoch_inferred_call_start':
            lines.append('- Дата из имени ACRPhone — предполагаемое начало звонка; время записи не подтверждено.')
        if primary['source_status'] == 'verified':
            lines.append(f'- [Оригинал](<{source.as_uri()}>)')
        for alias in provenance[1:]:
            lines.append(f'- Ещё один источник: {escape(alias["source_path"])} ({escape(alias["source_status"])}).')
            lines.append(f'  - Исходное имя: {escape(alias["original_filename"])}; категория: {escape(alias["source_category"])}.')
            lines.append(f'  - Дата для сортировки: {escape(alias["selected_date_utc"])} ({escape(alias["selected_date_basis"])}); изменение файла: {escape(alias["source_mtime_utc"])}.')
            for key, label in (('filesystem_created_utc', 'Создание локальной копии'),
                               ('media_creation_raw', 'Дата внутри медиа'),
                               ('filename_epoch_utc', 'Метка времени в имени ACRPhone')):
                if alias.get(key):
                    lines.append(f'  - {label}: {escape(alias[key])}.')
            if alias['source_status'] == 'verified':
                lines.append(f'  - [Открыть файл](<{Path(alias["source_path"]).as_uri()}>)')
    else:
        lines.append(f'- [Оригинал](<{source.as_uri()}>)')
    if '_summary_status' in detail:
        state = detail['_summary_status']
        lines += ['', '## Резюме', '']
        if state == 'ready':
            # Plain paragraphs preserve the meaning without executable HTML/wiki links.
            lines += [escape(line) for line in str(detail.get('summary') or '').splitlines()]
        else:
            lines += [{'pending': 'Расшифровка готова. Резюме ожидается.',
                       'manual_required': 'Расшифровка готова. Резюме требует проверки.',
                       'skipped': 'Резюме не запрашивалось или было отключено.'}.get(state, 'Резюме ожидается.')]
    lines += ['', '## Транскрипция', '']
    segments = transcript.get('segments') or []
    if segments:
        for seg in segments:
            text = seg.get('sentence', seg.get('text', ''))
            start = seg.get('start_time', seg.get('start'))
            end = seg.get('end_time', seg.get('end'))
            times = f'`{stamp(start)}–{stamp(end)}` ' if start is not None else ''
            lines.append(f'{times}**{escape(seg.get("speaker") or "Говорящий не определён")}:** {escape(text)}')
            lines.append('')
    elif transcript.get('raw'):
        lines += ['Временные метки для этого результата недоступны.', '', escape(transcript['raw']), '']
    else:
        lines += ['Речь не обнаружена.', '']
    lines += ['## Мои заметки', '', '']
    return '\n'.join(lines)


def choose_path(vault, job, version, previous=None, provenance=None):
    selected = provenance[0]['selected_date_utc'] if provenance else None
    date = datetime.fromisoformat(selected.replace('Z', '+00:00')).astimezone(MOSCOW) if selected else datetime.fromtimestamp(job['mtime'], MOSCOW)
    directory = Path(vault) / date.strftime('%Y/%m')
    base = f'{date:%Y-%m-%d %H-%M-%S} — {safe_name(Path(job["source_path"]).stem)} — {job["id"][:8]}'
    return directory / (base + '.md')


def owned_note(path, job_id):
    """Return a note's bytes and user section only when its owner is unambiguous."""
    path = Path(path)
    if is_reparse(path) or not path.is_file():
        raise ValueError('Published note is not a regular file')
    data = path.read_bytes()
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Published note is not UTF-8') from exc
    if not text.startswith(('---\n', '---\r\n')):
        raise ValueError('Published note has no frontmatter')
    first_line_end = text.index('\n') + 1
    boundary = re.search(r'(?m)^---\r?$', text[first_line_end:])
    if not boundary:
        raise ValueError('Published note has incomplete frontmatter')
    frontmatter = text[first_line_end:first_line_end + boundary.start()]
    owners = re.findall(r'(?m)^bridge_id:[ \t]*(.+?)[ \t]*\r?$', frontmatter)
    if len(owners) != 1:
        raise ValueError('Published note has ambiguous bridge_id')
    try:
        owner = json.loads(owners[0])
    except ValueError as exc:
        raise ValueError('Published note has invalid bridge_id') from exc
    if owner != job_id:
        raise ValueError('Published note belongs to another job')
    markers = list(re.finditer(r'(?m)^## Мои заметки[ \t]*\r?$', text))
    if len(markers) != 1 or text.find('## Транскрипция') < 0 or markers[0].start() < text.find('## Транскрипция'):
        raise ValueError('Published note has ambiguous user section')
    return data, text[markers[0].start():]


def with_user_notes(generated, user_section):
    marker = '\n## Мои заметки\n'
    if generated.count(marker) != 1 or not user_section.startswith('## Мои заметки'):
        raise ValueError('Generated note has no unique user section')
    return generated[:generated.index(marker) + 1] + user_section


def backup_original(path, root, job_id, data=None):
    path = Path(path)
    if data is None:
        data, _ = owned_note(path, job_id)
    digest = hashlib.sha256(data).hexdigest()
    backup = Path(root) / 'backups' / 'note-revisions' / job_id / (digest + '.md')
    if backup.exists():
        if backup.read_bytes() != data:
            raise ValueError('Existing note backup differs from source')
    else:
        write_new(backup, data)
    return backup


def _replace_with_backup(path, temp, replaced):
    if os.name == 'nt':
        # ReplaceFileW backs up the bytes actually replaced, even if an
        # editor writes between the expected-byte check and the swap.
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        replace = kernel.ReplaceFileW
        replace.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p,
                            ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
        replace.restype = ctypes.c_int
        if not replace(str(path), str(temp), str(replaced), 0, None, None):
            raise OSError(ctypes.get_last_error(),
                          'ReplaceFileW failed; inspect note, temporary file and replacement backup')
    else:
        os.link(path, replaced)
        os.replace(temp, path)


def write_replace(path, text, expected=None, backup_dir=None):
    path = Path(path)
    data = text.encode('utf-8') if isinstance(text, str) else text
    temp = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    replaced = path.with_name('.' + uuid.uuid4().hex + '.replaced')
    succeeded = False
    try:
        with temp.open('xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if expected is not None and path.read_bytes() != expected:
            raise ValueError('Published note changed during replacement')
        _replace_with_backup(path, temp, replaced)
        succeeded = True
        actual = replaced.read_bytes()
        destination = Path(backup_dir) if backup_dir else path.parent / '.replacement-backups'
        destination.mkdir(parents=True, exist_ok=True)
        saved = destination / (hashlib.sha256(actual).hexdigest() + '.replaced')
        if saved.exists():
            if saved.read_bytes() != actual:
                raise OSError('Replacement backup hash collision')
        else:
            write_new(saved, actual)
        if saved.read_bytes() != actual:
            raise OSError('Replacement backup verification failed')
        replaced.unlink()
        if expected is not None and actual != expected:
            raise NoteConflict(f'Concurrent note edit preserved at {saved}; reconcile before re-export')
    finally:
        # A partial ReplaceFileW failure may leave either recovery artifact.
        if succeeded or not replaced.exists():
            temp.unlink(missing_ok=True)


def write_new(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    data = text.encode('utf-8') if isinstance(text, str) else text
    try:
        with temp.open('xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # Windows rename does not overwrite. link provides the same guarantee on POSIX.
        if os.name == 'nt':
            os.rename(temp, path)
        else:
            os.link(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def is_our_complete_file(path, expected_hash):
    p = Path(path)
    return p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == expected_hash
