"""
FFprobe utility for detecting audio/video codecs and format information.

This module provides functions to inspect media files using ffprobe and return
structured information about their codecs, streams, and formats.
"""

import json
import logging
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)


class FFProbeError(Exception):
    """Raised when ffprobe fails to analyze a file."""
    pass


def probe(filename: str, cmd: str = 'ffprobe', timeout: Optional[int] = None) -> Dict[str, Any]:
    """
    Run ffprobe on the specified file and return a JSON representation of the output.

    Args:
        filename: Path to the media file to probe
        cmd: Command to use (default: 'ffprobe')
        timeout: Optional timeout in seconds

    Returns:
        Dictionary containing streams and format information

    Raises:
        FFProbeError: if ffprobe returns a non-zero exit code
    """
    args = [cmd, '-show_format', '-show_streams', '-of', 'json', filename]
    p = None

    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        communicate_kwargs = {}
        if timeout is not None:
            communicate_kwargs['timeout'] = timeout
        out, err = p.communicate(**communicate_kwargs)
        
        if p.returncode != 0:
            error_msg = err.decode('utf-8', errors='ignore')
            raise FFProbeError(f'ffprobe failed: {error_msg}')
        
        return json.loads(out.decode('utf-8'))
    except subprocess.TimeoutExpired:
        if p:
            p.kill()
        raise FFProbeError(f'ffprobe timed out after {timeout} seconds')
    except FileNotFoundError:
        raise FFProbeError('ffprobe command not found. Please ensure ffmpeg is installed.')
    except json.JSONDecodeError as e:
        raise FFProbeError(f'Failed to parse ffprobe output: {e}')


def get_codec_info(filename: str, timeout: Optional[int] = None) -> Dict[str, Any]:
    """
    Get codec information for a media file.

    Args:
        filename: Path to the media file
        timeout: Optional timeout in seconds

    Returns:
        Dictionary with keys:
        - audio_codec: Audio codec name (e.g., 'pcm_s16le', 'aac', 'mp3')
        - video_codec: Video codec name if present, or None
        - has_video: Boolean indicating if file contains video stream
        - has_audio: Boolean indicating if file contains audio stream
        - format_name: Container format name (e.g., 'wav', 'mov,mp4,m4a')
        - duration: Duration in seconds (float)
        - sample_rate: Audio sample rate if available
        - channels: Number of audio channels if available
        - bit_rate: Bit rate if available

    Raises:
        FFProbeError: if ffprobe fails to analyze the file
    """
    try:
        probe_data = probe(filename, timeout=timeout)
    except FFProbeError:
        raise

    result = {
        'audio_codec': None,
        'video_codec': None,
        'has_video': False,
        'has_audio': False,
        'format_name': None,
        'duration': None,
        'sample_rate': None,
        'channels': None,
        'bit_rate': None
    }

    # Extract format information
    if 'format' in probe_data:
        fmt = probe_data['format']
        result['format_name'] = fmt.get('format_name')
        
        if 'duration' in fmt:
            try:
                result['duration'] = float(fmt['duration'])
            except (ValueError, TypeError):
                pass
        
        if 'bit_rate' in fmt:
            try:
                result['bit_rate'] = int(fmt['bit_rate'])
            except (ValueError, TypeError):
                pass

    # Extract stream information
    if 'streams' in probe_data:
        for stream in probe_data['streams']:
            codec_type = stream.get('codec_type')
            codec_name = stream.get('codec_name')
            
            if codec_type == 'audio':
                result['has_audio'] = True
                if result['audio_codec'] is None:  # Use first audio stream
                    result['audio_codec'] = codec_name
                    result['sample_rate'] = stream.get('sample_rate')
                    result['channels'] = stream.get('channels')
            
            elif codec_type == 'video':
                result['has_video'] = True
                if result['video_codec'] is None:  # Use first video stream
                    result['video_codec'] = codec_name

    return result


def is_video_file(filename: str, timeout: Optional[int] = None, codec_info: Optional[Dict[str, Any]] = None) -> bool:
    """
    Check if a file contains video streams.

    Args:
        filename: Path to the media file
        timeout: Optional timeout in seconds
        codec_info: Optional pre-fetched codec info to avoid redundant probe calls

    Returns:
        True if file contains video streams, False otherwise
    """
    try:
        if codec_info is None:
            codec_info = get_codec_info(filename, timeout=timeout)
        return codec_info['has_video']
    except FFProbeError as e:
        logger.warning(f"Failed to probe {filename}: {e}")
        return False


def is_audio_file(filename: str, timeout: Optional[int] = None, codec_info: Optional[Dict[str, Any]] = None) -> bool:
    """
    Check if a file contains audio streams.

    Args:
        filename: Path to the media file
        timeout: Optional timeout in seconds
        codec_info: Optional pre-fetched codec info to avoid redundant probe calls

    Returns:
        True if file contains audio streams, False otherwise
    """
    try:
        if codec_info is None:
            codec_info = get_codec_info(filename, timeout=timeout)
        return codec_info['has_audio']
    except FFProbeError as e:
        logger.warning(f"Failed to probe {filename}: {e}")
        return False


def get_audio_codec(filename: str, timeout: Optional[int] = None, codec_info: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Get the audio codec name for a file.

    Args:
        filename: Path to the media file
        timeout: Optional timeout in seconds
        codec_info: Optional pre-fetched codec info to avoid redundant probe calls

    Returns:
        Audio codec name (e.g., 'pcm_s16le', 'aac', 'mp3', 'opus'), or None if no audio
    """
    try:
        if codec_info is None:
            codec_info = get_codec_info(filename, timeout=timeout)
        return codec_info['audio_codec']
    except FFProbeError as e:
        logger.warning(f"Failed to probe {filename}: {e}")
        return None


def needs_audio_conversion(filename: str, supported_codecs: list, timeout: Optional[int] = None, codec_info: Optional[Dict[str, Any]] = None) -> Tuple[bool, Optional[str]]:
    """
    Check if a file needs audio conversion based on its codec.

    Args:
        filename: Path to the media file
        supported_codecs: List of supported audio codec names
        timeout: Optional timeout in seconds
        codec_info: Optional pre-fetched codec info to avoid redundant probe calls

    Returns:
        Tuple of (needs_conversion: bool, current_codec: str or None)
    """
    try:
        if codec_info is None:
            codec_info = get_codec_info(filename, timeout=timeout)
        
        # If it has video, it likely needs conversion
        if codec_info['has_video']:
            return True, codec_info.get('audio_codec')
        
        # If no audio at all, cannot convert
        if not codec_info['has_audio']:
            logger.warning(f"File {filename} has no audio streams")
            return False, None
        
        audio_codec = codec_info['audio_codec']
        
        # Check if codec is in supported list
        if audio_codec in supported_codecs:
            return False, audio_codec
        
        return True, audio_codec
        
    except FFProbeError as e:
        logger.warning(f"Failed to probe {filename}: {e}")
        # Default to attempting conversion on error
        return True, None


def is_lossless_audio(filename: str, timeout: Optional[int] = None, codec_info: Optional[Dict[str, Any]] = None) -> bool:
    """
    Check if a file uses a lossless audio codec.

    Args:
        filename: Path to the media file
        timeout: Optional timeout in seconds
        codec_info: Optional pre-fetched codec info to avoid redundant probe calls

    Returns:
        True if file uses lossless audio codec, False otherwise
    """
    lossless_codecs = {
        'pcm_s16le', 'pcm_s24le', 'pcm_s32le',
        'pcm_f32le', 'pcm_f64le',
        'pcm_u8', 'pcm_u16le', 'pcm_u24le', 'pcm_u32le',
        'flac', 'alac', 'ape', 'wavpack', 'tta',
        'mlp', 'truehd'
    }
    
    try:
        if codec_info is None:
            codec_info = get_codec_info(filename, timeout=timeout)
        audio_codec = codec_info['audio_codec']
        return audio_codec in lossless_codecs if audio_codec else False
    except FFProbeError as e:
        logger.warning(f"Failed to probe {filename}: {e}")
        return False


def get_duration(filename: str, timeout: Optional[int] = None, codec_info: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """
    Get the duration of a media file in seconds.

    Args:
        filename: Path to the media file
        timeout: Optional timeout in seconds
        codec_info: Optional pre-fetched codec info to avoid redundant probe calls

    Returns:
        Duration in seconds, or None if unable to determine
    """
    try:
        if codec_info is None:
            codec_info = get_codec_info(filename, timeout=timeout)
        return codec_info['duration']
    except FFProbeError as e:
        logger.warning(f"Failed to probe {filename}: {e}")
        return None


def get_creation_date(filename: str, timeout: Optional[int] = None, use_file_mtime: bool = True) -> Optional[datetime]:
    """
    Extract the creation/recording date from a media file's metadata.

    Checks various metadata tags commonly used by recorders and devices:
    - creation_time (MP4, M4A, MOV)
    - date (various formats)
    - encoded_date (some encoders)

    Falls back to file modification time if no metadata found and use_file_mtime is True.

    Args:
        filename: Path to the media file
        timeout: Optional timeout in seconds
        use_file_mtime: If True, fall back to file modification time when no metadata found

    Returns:
        datetime object if creation date found, None otherwise
    """
    import os

    try:
        probe_data = probe(filename, timeout=timeout)
    except FFProbeError as e:
        logger.warning(f"Failed to probe {filename} for creation date: {e}")
        # Even if probe fails, we can still try file mtime
        if use_file_mtime:
            return _get_file_mtime(filename)
        return None

    # Tags to check for creation date (in order of preference)
    date_tags = ['creation_time', 'date', 'encoded_date', 'date_recorded', 'recording_time']

    # Check format-level tags first
    if 'format' in probe_data and 'tags' in probe_data['format']:
        tags = probe_data['format']['tags']
        for tag in date_tags:
            # Check both lowercase and original case
            value = tags.get(tag) or tags.get(tag.upper())
            if value:
                parsed = _parse_date_string(value)
                if parsed:
                    logger.debug(f"Found creation date from format tag '{tag}': {parsed}")
                    return parsed

    # Check stream-level tags
    if 'streams' in probe_data:
        for stream in probe_data['streams']:
            if 'tags' in stream:
                tags = stream['tags']
                for tag in date_tags:
                    value = tags.get(tag) or tags.get(tag.upper())
                    if value:
                        parsed = _parse_date_string(value)
                        if parsed:
                            logger.debug(f"Found creation date from stream tag '{tag}': {parsed}")
                            return parsed

    # Fall back to file modification time
    if use_file_mtime:
        mtime = _get_file_mtime(filename)
        if mtime:
            logger.debug(f"Using file modification time as creation date: {mtime}")
            return mtime

    logger.debug(f"No creation date found for {filename}")
    return None


def _get_file_mtime(filename: str) -> Optional[datetime]:
    """
    Get the file's modification time as a datetime.

    Args:
        filename: Path to the file

    Returns:
        datetime object or None if unable to get mtime
    """
    import os

    try:
        stat_info = os.stat(filename)
        return datetime.fromtimestamp(stat_info.st_mtime)
    except (OSError, ValueError) as e:
        logger.warning(f"Failed to get file mtime for {filename}: {e}")
        return None


def _parse_date_string(date_str: str) -> Optional[datetime]:
    """
    Parse various date string formats commonly found in media metadata.

    Args:
        date_str: Date string to parse

    Returns:
        datetime object if parsing successful, None otherwise
    """
    if not date_str:
        return None

    # Common formats in media files
    formats = [
        '%Y-%m-%dT%H:%M:%S.%fZ',      # ISO 8601 with microseconds and Z
        '%Y-%m-%dT%H:%M:%SZ',          # ISO 8601 with Z
        '%Y-%m-%dT%H:%M:%S.%f%z',      # ISO 8601 with microseconds and timezone
        '%Y-%m-%dT%H:%M:%S%z',         # ISO 8601 with timezone
        '%Y-%m-%dT%H:%M:%S.%f',        # ISO 8601 with microseconds
        '%Y-%m-%dT%H:%M:%S',           # ISO 8601 basic
        '%Y-%m-%d %H:%M:%S',           # Common datetime
        '%Y/%m/%d %H:%M:%S',           # Alternate datetime
        '%Y-%m-%d',                     # Date only
        '%Y/%m/%d',                     # Alternate date only
        '%d-%m-%Y %H:%M:%S',           # European format
        '%d/%m/%Y %H:%M:%S',           # European format alternate
    ]

    # Clean up the string
    date_str = date_str.strip()

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    # Try fromisoformat as a fallback (handles many ISO variants)
    try:
        # Replace Z with +00:00 for fromisoformat compatibility
        clean_str = date_str.replace('Z', '+00:00')
        return datetime.fromisoformat(clean_str)
    except ValueError:
        pass

    logger.debug(f"Could not parse date string: {date_str}")
    return None