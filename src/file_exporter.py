#!/usr/bin/env python3
"""
File Exporter for Automated Recording Export

Exports transcriptions and summaries as markdown files to a configured directory.
Supports per-user subdirectories based on username.
Supports customizable export templates with localized labels.
"""

import os
import re
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from werkzeug.utils import secure_filename

# Configuration from environment
ENABLE_AUTO_EXPORT = os.environ.get('ENABLE_AUTO_EXPORT', 'false').lower() == 'true'
AUTO_EXPORT_DIR = os.environ.get('AUTO_EXPORT_DIR', '/data/exports')
AUTO_EXPORT_TRANSCRIPTION = os.environ.get('AUTO_EXPORT_TRANSCRIPTION', 'true').lower() == 'true'
AUTO_EXPORT_SUMMARY = os.environ.get('AUTO_EXPORT_SUMMARY', 'true').lower() == 'true'

# Setup logging
logger = logging.getLogger('file_exporter')
logger.setLevel(logging.INFO)


def format_transcription_with_template(transcription_text, user):
    """
    Format transcription using the user's default template.

    Args:
        transcription_text: Raw transcription (JSON or plain text)
        user: User object to get template from

    Returns:
        Formatted transcription string
    """
    # Import here to avoid circular imports
    from src.models import TranscriptTemplate

    # Try to parse as JSON
    try:
        transcription_data = json.loads(transcription_text)
        if not isinstance(transcription_data, list):
            # Not our expected format, return as-is
            return transcription_text
    except (json.JSONDecodeError, TypeError):
        # Not JSON, return as-is
        return transcription_text

    # Get user's default template
    template = TranscriptTemplate.query.filter_by(
        user_id=user.id,
        is_default=True
    ).first()

    # Default format if no template set
    if not template:
        template_format = "[{{speaker}}]: {{text}}"
    else:
        template_format = template.template

    # Helper functions for formatting
    def format_time(seconds):
        """Format seconds to HH:MM:SS"""
        if seconds is None:
            return "00:00:00"
        td = timedelta(seconds=seconds)
        hours = int(td.total_seconds() // 3600)
        minutes = int((td.total_seconds() % 3600) // 60)
        secs = int(td.total_seconds() % 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def format_srt_time(seconds):
        """Format seconds to SRT format HH:MM:SS,mmm"""
        if seconds is None:
            return "00:00:00,000"
        td = timedelta(seconds=seconds)
        hours = int(td.total_seconds() // 3600)
        minutes = int((td.total_seconds() % 3600) // 60)
        secs = int(td.total_seconds() % 60)
        millis = int((td.total_seconds() % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    # Generate formatted transcript
    output_lines = []
    for index, segment in enumerate(transcription_data, 1):
        line = template_format

        # Replace variables
        replacements = {
            '{{index}}': str(index),
            '{{speaker}}': segment.get('speaker', 'Unknown'),
            '{{text}}': segment.get('sentence', ''),
            '{{start_time}}': format_time(segment.get('start_time')),
            '{{end_time}}': format_time(segment.get('end_time')),
        }

        for key, value in replacements.items():
            line = line.replace(key, value)

        # Handle filters
        # Upper case filter
        line = re.sub(r'{{(.*?)\|upper}}', lambda m: replacements.get('{{' + m.group(1) + '}}', '').upper(), line)
        # SRT time filter
        line = re.sub(r'{{start_time\|srt}}', format_srt_time(segment.get('start_time')), line)
        line = re.sub(r'{{end_time\|srt}}', format_srt_time(segment.get('end_time')), line)

        output_lines.append(line)

    return '\n'.join(output_lines)


def get_export_directory(user):
    """Get the export directory for a user, creating if needed."""
    base_dir = Path(AUTO_EXPORT_DIR)

    # Create per-user subdirectory based on username
    user_dir = base_dir / secure_filename(user.username)
    user_dir.mkdir(parents=True, exist_ok=True)

    return user_dir


def generate_safe_filename(recording):
    """Generate a safe filename for the export based on recording ID only."""
    # Use only recording ID for consistent filename that doesn't change
    return f"recording_{recording.id}"


# Default filename template — reproduces the legacy "recording_{id}" naming
# exactly, so users who never touch the setting see no behavior change (#348).
DEFAULT_EXPORT_FILENAME_TEMPLATE = 'recording_{{id}}'

# Maximum length of a rendered export filename (without the .md extension).
MAX_EXPORT_FILENAME_LENGTH = 150

# Characters illegal on common filesystems (plus path separators).
_UNSAFE_FILENAME_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def sanitize_export_filename(name):
    """
    Make a rendered filename safe for common filesystems.

    Strips path separators and characters illegal on Windows/macOS/Linux,
    collapses whitespace runs to single spaces, trims dots/spaces at the ends
    and caps the length. Unicode letters are preserved (deliberately NOT
    secure_filename(), which would mangle non-ASCII titles). Returns '' if
    nothing safe remains — callers must fall back to the legacy name.
    """
    if not name:
        return ''
    result = _UNSAFE_FILENAME_CHARS.sub('', name)
    result = re.sub(r'\s+', ' ', result)
    result = result.strip(' .')
    if len(result) > MAX_EXPORT_FILENAME_LENGTH:
        result = result[:MAX_EXPORT_FILENAME_LENGTH].strip(' .')
    return result


def render_export_filename(recording, user):
    """
    Render the export filename (WITHOUT the .md extension) for a recording
    using the user's filename template.

    Supported variables: {{id}}, {{title}}, {{filename}} (original filename
    stem), and {{date}}/{{datetime}}/{{time}}/{{year}}/{{month}}/{{day}} from
    recording.meeting_date (falling back to created_at). The result is
    sanitized for filesystem safety; an empty result falls back to the legacy
    "recording_{id}". If the rendered name collides (case-insensitively) with
    a DIFFERENT recording's stored export filename for the same user, "_{id}"
    is appended to keep names unique.
    """
    from src.models import Recording
    from sqlalchemy import func

    template = (getattr(user, 'export_filename_template', None) or '').strip()
    if not template:
        template = DEFAULT_EXPORT_FILENAME_TEMPLATE

    dt = recording.meeting_date or recording.created_at
    filename_stem = os.path.splitext(recording.original_filename)[0] if recording.original_filename else ''

    variables = {
        'id': str(recording.id),
        'title': recording.title or '',
        'filename': filename_stem,
        'date': dt.strftime('%Y-%m-%d') if dt else '',
        'datetime': dt.strftime('%Y-%m-%d %H-%M') if dt else '',
        'time': dt.strftime('%H-%M') if dt else '',
        'year': dt.strftime('%Y') if dt else '',
        'month': dt.strftime('%m') if dt else '',
        'day': dt.strftime('%d') if dt else '',
    }

    result = template
    for var_name, value in variables.items():
        result = result.replace('{{' + var_name + '}}', value)

    result = sanitize_export_filename(result)
    if not result:
        result = f"recording_{recording.id}"

    # Uniqueness: a different recording of the same user already owns this
    # name (case-insensitive) -> append the id to disambiguate.
    try:
        collision = Recording.query.filter(
            Recording.user_id == recording.user_id,
            Recording.id != recording.id,
            func.lower(Recording.export_filename) == result.lower()
        ).first()
    except Exception:
        collision = None
    if collision is not None:
        suffix = f"_{recording.id}"
        if len(result) + len(suffix) > MAX_EXPORT_FILENAME_LENGTH:
            result = result[:MAX_EXPORT_FILENAME_LENGTH - len(suffix)].strip(' .')
        result = f"{result}{suffix}"

    return result


def get_stored_export_filename(recording):
    """The authoritative on-disk name (no extension) for a recording's export.

    Recordings exported before this feature have no stored name and keep
    using the legacy "recording_{id}" so existing files remain addressable.
    """
    return recording.export_filename or f"recording_{recording.id}"


def get_export_filepath(user, recording):
    """Get the full export filepath for a recording."""
    export_dir = get_export_directory(user)
    return export_dir / f"{get_stored_export_filename(recording)}.md"


# The delete endpoint removes the Recording row BEFORE calling
# mark_export_as_deleted(), so with custom filenames the stored name can no
# longer be read from the database at that point. This ORM listener captures
# (user_id, export_filename) at delete time so the rename still finds the
# right file. Best-effort, in-process only; the legacy "recording_{id}" scan
# remains as fallback.
_deleted_export_names = {}


def _remember_deleted_export_name(mapper, connection, target):
    try:
        if len(_deleted_export_names) > 1000:
            _deleted_export_names.clear()
        _deleted_export_names[target.id] = (target.user_id, target.export_filename)
    except Exception:
        pass


def _register_delete_listener():
    try:
        from sqlalchemy import event
        from src.models import Recording
        if not event.contains(Recording, 'after_delete', _remember_deleted_export_name):
            event.listen(Recording, 'after_delete', _remember_deleted_export_name)
    except Exception:
        logger.debug("Could not register export-filename delete listener", exc_info=True)


_register_delete_listener()


def mark_export_as_deleted(recording_id):
    """
    Rename the export file to indicate the recording was deleted.

    Uses the recording's stored export filename when available (from the DB
    row if it still exists, or captured at ORM delete time otherwise) and
    falls back to the legacy "recording_{id}" name.

    Args:
        recording_id: ID of the deleted recording

    Returns:
        New filepath if renamed, None otherwise
    """
    if not ENABLE_AUTO_EXPORT:
        return None

    # Import here to avoid circular imports
    from src.app import app, db
    from src.models import Recording, User

    with app.app_context():
        try:
            base_dir = Path(AUTO_EXPORT_DIR)
            if not base_dir.exists():
                return None

            stored_name = None
            user_dirs = None

            def _dir_for(user_id):
                user = db.session.get(User, user_id) if user_id else None
                if user:
                    d = base_dir / secure_filename(user.username)
                    return [d] if d.is_dir() else []
                return None

            captured = _deleted_export_names.pop(recording_id, None)
            if captured is not None:
                user_id, stored_name = captured
                user_dirs = _dir_for(user_id)
            else:
                recording = db.session.get(Recording, recording_id)
                if recording:
                    stored_name = recording.export_filename
                    user_dirs = _dir_for(recording.user_id)

            if user_dirs is None:
                # Owner unknown — check all user directories.
                user_dirs = [d for d in base_dir.iterdir() if d.is_dir()]

            # Stored name first (authoritative), then the legacy id-based name.
            candidates = []
            if stored_name and '/' not in stored_name and '\\' not in stored_name:
                candidates.append(stored_name)
            legacy = f"recording_{recording_id}"
            if legacy not in candidates:
                candidates.append(legacy)

            for user_dir in user_dirs:
                for name in candidates:
                    old_filepath = user_dir / f"{name}.md"
                    if old_filepath.exists():
                        new_filepath = user_dir / f"[deleted]_{name}.md"
                        old_filepath.rename(new_filepath)
                        logger.info(f"Marked export as deleted: {new_filepath}")
                        return str(new_filepath)

            return None

        except Exception as e:
            logger.error(f"Failed to mark export as deleted for recording {recording_id}: {e}")
            return None


def apply_filename_template_for_user(user_id):
    """
    Re-render every exported recording's filename for a user under their
    current template and rename the files on disk (the "[deleted]_"-prefixed
    files of removed exports keep their prefix). Stored export_filename
    values are updated to match. Missing files on disk are skipped.

    Returns:
        dict with counts: {'renamed': n, 'skipped': n, 'errors': n}
    """
    # Import here to avoid circular imports
    from src.app import app, db
    from src.models import Recording, User

    with app.app_context():
        renamed = 0
        skipped = 0
        errors = 0

        user = db.session.get(User, user_id)
        if not user:
            return {'renamed': 0, 'skipped': 0, 'errors': 0}

        user_dir = Path(AUTO_EXPORT_DIR) / secure_filename(user.username)

        recordings = Recording.query.filter(Recording.user_id == user_id).all()
        for recording in recordings:
            try:
                old_name = get_stored_export_filename(recording)
                old_file = user_dir / f"{old_name}.md"
                old_deleted = user_dir / f"[deleted]_{old_name}.md"

                if not user_dir.is_dir() or not (old_file.exists() or old_deleted.exists()):
                    # Never exported (or file gone) — nothing to rename.
                    skipped += 1
                    continue

                new_name = render_export_filename(recording, user)
                if new_name == old_name:
                    # Persist the (unchanged) name so it becomes authoritative.
                    if recording.export_filename != new_name:
                        recording.export_filename = new_name
                    skipped += 1
                    continue

                # Disk-level guard: if the target already exists (e.g. stale
                # file, case-insensitive filesystem), disambiguate with the id.
                if (user_dir / f"{new_name}.md").exists() or (user_dir / f"[deleted]_{new_name}.md").exists():
                    suffix = f"_{recording.id}"
                    if not new_name.endswith(suffix):
                        new_name = f"{new_name}{suffix}"

                moved = False
                if old_file.exists():
                    old_file.rename(user_dir / f"{new_name}.md")
                    moved = True
                if old_deleted.exists():
                    old_deleted.rename(user_dir / f"[deleted]_{new_name}.md")
                    moved = True

                recording.export_filename = new_name
                if moved:
                    renamed += 1
                else:
                    skipped += 1
            except Exception as e:
                logger.error(f"Failed to apply filename template to recording {recording.id}: {e}")
                errors += 1

        db.session.commit()
        return {'renamed': renamed, 'skipped': skipped, 'errors': errors}


def format_duration(seconds):
    """Format duration in seconds to human-readable string."""
    if not seconds:
        return ""

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


def format_file_size(bytes_size):
    """Format file size in bytes to human-readable string."""
    if not bytes_size:
        return ""

    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


def get_user_export_template(user, recording=None):
    """
    Get the export template to use for a recording.

    Resolution order:
    1. Folder's export_template_id (if recording is in a folder)
    2. Tag's export_template_id (first matching tag with an export template)
    3. User's default export template (is_default=True)

    Args:
        user: User object
        recording: Optional Recording object (for folder/tag lookup)

    Returns:
        ExportTemplate object or None
    """
    from src.models import ExportTemplate
    from src.database import db

    # 1. Check folder's export template
    if recording and recording.folder and recording.folder.export_template_id:
        template = db.session.get(ExportTemplate, recording.folder.export_template_id)
        if template:
            return template

    # 2. Check tags' export templates
    if recording and recording.tags:
        for tag in recording.tags:
            if tag.export_template_id:
                template = db.session.get(ExportTemplate, tag.export_template_id)
                if template:
                    return template

    # 3. Fall back to user's default
    return ExportTemplate.query.filter_by(
        user_id=user.id,
        is_default=True
    ).first()


def render_export_template(template_str, context, labels):
    """
    Render an export template with variable substitution and conditionals.

    Args:
        template_str: Template string with {{variables}} and {{#if var}}...{{/if}} blocks
        context: Dictionary of variable values
        labels: Dictionary of localized labels

    Returns:
        Rendered string
    """
    result = template_str

    # Process conditionals first: {{#if variable}}content{{/if}}
    def replace_conditional(match):
        var_name = match.group(1)
        content = match.group(2)
        # Check if the variable exists and is truthy
        value = context.get(var_name, '')
        if value:
            return content
        return ''

    # Match {{#if var}}...{{/if}} blocks (non-greedy)
    conditional_pattern = r'\{\{#if\s+(\w+)\}\}(.*?)\{\{/if\}\}'
    result = re.sub(conditional_pattern, replace_conditional, result, flags=re.DOTALL)

    # Replace label variables: {{label.key}}
    def replace_label(match):
        key = match.group(1)
        return labels.get(key, key)

    result = re.sub(r'\{\{label\.(\w+)\}\}', replace_label, result)

    # Replace context variables: {{variable}}
    for key, value in context.items():
        placeholder = '{{' + key + '}}'
        result = result.replace(placeholder, str(value) if value else '')

    return result


def generate_markdown_content(recording, user, include_transcription=True, include_summary=True):
    """Generate markdown content for a recording export.

    Args:
        recording: Recording object to export
        user: User object for getting template preferences
        include_transcription: Whether to include transcription
        include_summary: Whether to include summary
    """
    from src.utils.localization import get_export_labels, format_date_localized, format_datetime_localized

    # Get user's language preference (default to English)
    user_language = getattr(user, 'ui_language', 'en') or 'en'

    # Get localized labels
    labels = get_export_labels(user_language)

    # Get export template (checks folder, tags, then user default)
    export_template = get_user_export_template(user, recording)

    if export_template:
        # Use custom template
        return generate_from_template(
            recording, user, export_template.template, labels, user_language,
            include_transcription, include_summary
        )
    else:
        # Use default (backwards compatible) behavior
        return generate_default_markdown(
            recording, user, labels, user_language,
            include_transcription, include_summary
        )


def generate_from_template(recording, user, template_str, labels, user_language,
                           include_transcription=True, include_summary=True):
    """
    Generate markdown content using a custom template.

    Args:
        recording: Recording object
        user: User object
        template_str: Template string
        labels: Localized labels dictionary
        user_language: User's language code
        include_transcription: Whether to include transcription
        include_summary: Whether to include summary

    Returns:
        Rendered markdown string
    """
    from src.utils.localization import format_date_localized, format_datetime_localized

    # Build context with all available variables
    context = {
        'title': recording.title or f"Recording {recording.id}",
        'meeting_date': format_date_localized(recording.meeting_date, user_language) if recording.meeting_date else '',
        'created_at': format_datetime_localized(recording.created_at, user_language) if recording.created_at else '',
        'original_filename': recording.original_filename or '',
        'file_size': format_file_size(recording.file_size) if recording.file_size else '',
        'participants': recording.participants or '',
        'tags': ', '.join([tag.name for tag in recording.tags]) if recording.tags else '',
        'transcription_duration': format_duration(recording.transcription_duration_seconds) if recording.transcription_duration_seconds else '',
        'summarization_duration': format_duration(recording.summarization_duration_seconds) if recording.summarization_duration_seconds else '',
        'notes': recording.notes or '' if include_summary else '',  # Notes included with summary setting
        'summary': recording.summary or '' if include_summary else '',
        'transcription': '',  # Will be set below
    }

    # Format transcription if included
    if include_transcription and recording.transcription:
        context['transcription'] = format_transcription_with_template(recording.transcription, user)

    # Render template
    rendered = render_export_template(template_str, context, labels)

    # Always append hardcoded footer
    footer = labels.get('footer', 'Generated with [Speakr](https://github.com/learnedmachine/speakr)')
    rendered += f"\n\n---\n\n*{footer}*\n"

    return rendered


def generate_default_markdown(recording, user, labels, user_language,
                              include_transcription=True, include_summary=True):
    """
    Generate markdown using the default (backwards compatible) format.

    Args:
        recording: Recording object
        user: User object
        labels: Localized labels dictionary
        user_language: User's language code
        include_transcription: Whether to include transcription
        include_summary: Whether to include summary

    Returns:
        Rendered markdown string
    """
    from src.utils.localization import format_date_localized, format_datetime_localized

    lines = []

    # Header with title
    title = recording.title or f"Recording {recording.id}"
    lines.append(f"# {title}")
    lines.append("")

    # Metadata section
    lines.append(f"## {labels.get('metadata', 'Metadata')}")
    lines.append("")

    if recording.meeting_date:
        date_str = format_date_localized(recording.meeting_date, user_language)
        lines.append(f"- **{labels.get('date', 'Date')}:** {date_str}")

    if recording.created_at:
        created_str = format_datetime_localized(recording.created_at, user_language)
        lines.append(f"- **{labels.get('created', 'Created')}:** {created_str}")

    if recording.original_filename:
        lines.append(f"- **{labels.get('originalFile', 'Original File')}:** {recording.original_filename}")

    if recording.file_size:
        lines.append(f"- **{labels.get('fileSize', 'File Size')}:** {format_file_size(recording.file_size)}")

    if recording.participants:
        lines.append(f"- **{labels.get('participants', 'Participants')}:** {recording.participants}")

    if recording.tags:
        tag_names = [tag.name for tag in recording.tags]
        lines.append(f"- **{labels.get('tags', 'Tags')}:** {', '.join(tag_names)}")

    if recording.transcription_duration_seconds:
        lines.append(f"- **{labels.get('transcriptionTime', 'Transcription Time')}:** {format_duration(recording.transcription_duration_seconds)}")

    if recording.summarization_duration_seconds:
        lines.append(f"- **{labels.get('summarizationTime', 'Summarization Time')}:** {format_duration(recording.summarization_duration_seconds)}")

    lines.append("")

    # Notes section (if available)
    if recording.notes:
        lines.append(f"## {labels.get('notes', 'Notes')}")
        lines.append("")
        lines.append(recording.notes)
        lines.append("")

    # Summary section
    if include_summary and recording.summary:
        lines.append(f"## {labels.get('summary', 'Summary')}")
        lines.append("")
        lines.append(recording.summary)
        lines.append("")

    # Transcription section
    if include_transcription and recording.transcription:
        lines.append(f"## {labels.get('transcription', 'Transcription')}")
        lines.append("")
        # Format transcription using user's template
        formatted_transcription = format_transcription_with_template(recording.transcription, user)
        lines.append(formatted_transcription)
        lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    footer = labels.get('footer', 'Generated with [Speakr](https://github.com/learnedmachine/speakr)')
    lines.append(f"*{footer}*")
    lines.append("")

    return "\n".join(lines)


def export_recording(recording_id):
    """
    Export a recording to markdown file.

    Args:
        recording_id: ID of the recording to export

    Returns:
        Path to the exported file, or None if export failed/disabled
    """
    if not ENABLE_AUTO_EXPORT:
        return None

    # Check if we should export anything
    if not AUTO_EXPORT_TRANSCRIPTION and not AUTO_EXPORT_SUMMARY:
        logger.warning("Auto-export is enabled but both transcription and summary export are disabled")
        return None

    # Import here to avoid circular imports
    from src.app import app, db
    from src.models import Recording, User

    with app.app_context():
        try:
            recording = db.session.get(Recording, recording_id)
            if not recording:
                logger.error(f"Recording {recording_id} not found for export")
                return None

            # Get the owner
            user = db.session.get(User, recording.user_id)
            if not user:
                logger.error(f"User not found for recording {recording_id}")
                return None

            # Check if we have content to export
            has_transcription = bool(recording.transcription) and AUTO_EXPORT_TRANSCRIPTION
            has_summary = bool(recording.summary) and AUTO_EXPORT_SUMMARY

            if not has_transcription and not has_summary:
                logger.debug(f"Recording {recording_id} has no content to export")
                return None

            # Get export directory for user
            export_dir = get_export_directory(user)

            # Render and store the filename on first export (or when NULL from
            # a legacy row that predates stored names). Once stored, the name
            # is authoritative: re-exports overwrite that same file.
            if not recording.export_filename:
                new_name = render_export_filename(recording, user)
                # A legacy export may already exist under "recording_{id}.md";
                # migrate it to the new name so no orphan file is left behind.
                legacy_file = export_dir / f"recording_{recording.id}.md"
                if new_name != f"recording_{recording.id}" and legacy_file.exists():
                    try:
                        legacy_file.rename(export_dir / f"{new_name}.md")
                    except OSError:
                        pass
                recording.export_filename = new_name
                db.session.commit()

            filepath = export_dir / f"{recording.export_filename}.md"

            # Generate content
            content = generate_markdown_content(
                recording,
                user,
                include_transcription=AUTO_EXPORT_TRANSCRIPTION,
                include_summary=AUTO_EXPORT_SUMMARY
            )

            # Write to file (overwrites if exists)
            filepath.write_text(content, encoding='utf-8')

            logger.info(f"Exported recording {recording_id} to {filepath}")
            return str(filepath)

        except Exception as e:
            logger.error(f"Failed to export recording {recording_id}: {e}")
            return None


def initialize_export_directory():
    """Initialize the export directory on startup."""
    if not ENABLE_AUTO_EXPORT:
        return

    try:
        export_dir = Path(AUTO_EXPORT_DIR)
        export_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Auto-export enabled, directory: {AUTO_EXPORT_DIR}")

        if AUTO_EXPORT_TRANSCRIPTION and AUTO_EXPORT_SUMMARY:
            logger.info("Exporting: transcription and summary")
        elif AUTO_EXPORT_TRANSCRIPTION:
            logger.info("Exporting: transcription only")
        elif AUTO_EXPORT_SUMMARY:
            logger.info("Exporting: summary only")
        else:
            logger.warning("Auto-export enabled but no content types selected")

    except Exception as e:
        logger.error(f"Failed to initialize export directory: {e}")
