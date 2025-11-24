#!/usr/bin/env python3
"""
File Exporter for Automated Recording Export

Exports transcriptions and summaries as markdown files to a configured directory.
Supports per-user subdirectories based on username.
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


def get_export_filepath(user, recording):
    """Get the full export filepath for a recording."""
    export_dir = get_export_directory(user)
    filename = generate_safe_filename(recording)
    return export_dir / f"{filename}.md"


def mark_export_as_deleted(recording_id):
    """
    Rename the export file to indicate the recording was deleted.

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
            # We need to find the file - check all user directories
            base_dir = Path(AUTO_EXPORT_DIR)
            if not base_dir.exists():
                return None

            # Look for the file in all user subdirectories
            for user_dir in base_dir.iterdir():
                if user_dir.is_dir():
                    old_filepath = user_dir / f"recording_{recording_id}.md"
                    if old_filepath.exists():
                        new_filepath = user_dir / f"[deleted]_recording_{recording_id}.md"
                        old_filepath.rename(new_filepath)
                        logger.info(f"Marked export as deleted: {new_filepath}")
                        return str(new_filepath)

            return None

        except Exception as e:
            logger.error(f"Failed to mark export as deleted for recording {recording_id}: {e}")
            return None


def format_duration(seconds):
    """Format duration in seconds to human-readable string."""
    if not seconds:
        return "Unknown"

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
        return "Unknown"

    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024
    return f"{bytes_size:.1f} TB"


def generate_markdown_content(recording, user, include_transcription=True, include_summary=True):
    """Generate markdown content for a recording export.

    Args:
        recording: Recording object to export
        user: User object for getting template preferences
        include_transcription: Whether to include transcription
        include_summary: Whether to include summary
    """
    lines = []

    # Header with title
    title = recording.title or f"Recording {recording.id}"
    lines.append(f"# {title}")
    lines.append("")

    # Metadata section
    lines.append("## Metadata")
    lines.append("")

    if recording.meeting_date:
        lines.append(f"- **Date:** {recording.meeting_date.strftime('%B %d, %Y')}")

    if recording.created_at:
        lines.append(f"- **Created:** {recording.created_at.strftime('%B %d, %Y at %I:%M %p')}")

    if recording.original_filename:
        lines.append(f"- **Original File:** {recording.original_filename}")

    if recording.file_size:
        lines.append(f"- **File Size:** {format_file_size(recording.file_size)}")

    if recording.participants:
        lines.append(f"- **Participants:** {recording.participants}")

    if recording.tags:
        tag_names = [tag.name for tag in recording.tags]
        lines.append(f"- **Tags:** {', '.join(tag_names)}")

    if recording.transcription_duration_seconds:
        lines.append(f"- **Transcription Time:** {format_duration(recording.transcription_duration_seconds)}")

    if recording.summarization_duration_seconds:
        lines.append(f"- **Summarization Time:** {format_duration(recording.summarization_duration_seconds)}")

    lines.append("")

    # Notes section (if available)
    if recording.notes:
        lines.append("## Notes")
        lines.append("")
        lines.append(recording.notes)
        lines.append("")

    # Summary section
    if include_summary and recording.summary:
        lines.append("## Summary")
        lines.append("")
        lines.append(recording.summary)
        lines.append("")

    # Transcription section
    if include_transcription and recording.transcription:
        lines.append("## Transcription")
        lines.append("")
        # Format transcription using user's template
        formatted_transcription = format_transcription_with_template(recording.transcription, user)
        lines.append(formatted_transcription)
        lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append("*Generated with [Speakr](https://github.com/learnedmachine/speakr)*")
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

            # Generate filename and path
            filename = generate_safe_filename(recording)
            filepath = export_dir / f"{filename}.md"

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
