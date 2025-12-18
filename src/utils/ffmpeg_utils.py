"""Centralized FFmpeg utilities for consistent audio/video processing."""

import os
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Optional, Tuple
from flask import current_app


# Configuration constants
DEFAULT_MP3_BITRATE = os.getenv('AUDIO_BITRATE', '128k')
DEFAULT_SAMPLE_RATE = os.getenv('AUDIO_SAMPLE_RATE', '44100')
DEFAULT_CHANNELS = int(os.getenv('AUDIO_CHANNELS', '1'))  # Mono for speech
DEFAULT_COMPRESSION_LEVEL = int(os.getenv('AUDIO_COMPRESSION_LEVEL', '2'))


class FFmpegError(Exception):
    """Custom exception for FFmpeg-related errors."""
    pass


class FFmpegNotFoundError(FFmpegError):
    """Raised when FFmpeg executable is not found."""
    pass


def convert_to_mp3(
    input_path: str,
    output_path: Optional[str] = None,
    bitrate: str = DEFAULT_MP3_BITRATE,
    sample_rate: str = DEFAULT_SAMPLE_RATE,
    channels: int = DEFAULT_CHANNELS,
    compression_level: int = DEFAULT_COMPRESSION_LEVEL
) -> str:
    """
    Convert audio/video file to MP3 format using FFmpeg.
    
    Args:
        input_path: Path to input audio/video file
        output_path: Path for output MP3 file (auto-generated if None)
        bitrate: MP3 bitrate (e.g., '128k', '192k')
        sample_rate: Sample rate in Hz (e.g., '44100', '48000')
        channels: Number of audio channels (1=mono, 2=stereo)
        compression_level: MP3 compression level (0-9, higher=better compression)
    
    Returns:
        Path to the created MP3 file
        
    Raises:
        FFmpegNotFoundError: If FFmpeg is not installed
        FFmpegError: If conversion fails
    """
    if output_path is None:
        base = os.path.splitext(input_path)[0]
        output_path = f"{base}.mp3"
    
    cmd = [
        'ffmpeg',
        '-i', input_path,
        '-y',  # Overwrite output
        '-acodec', 'libmp3lame',
        '-b:a', bitrate,
        '-ar', sample_rate,
        '-ac', str(channels),
        '-compression_level', str(compression_level),
        output_path
    ]
    
    _run_ffmpeg_command(cmd, f"MP3 conversion of {os.path.basename(input_path)}")
    return output_path


def extract_audio_from_video(
    video_path: str,
    output_format: str = 'mp3',
    bitrate: str = DEFAULT_MP3_BITRATE,
    cleanup_original: bool = True,
    copy_stream: bool = False
) -> Tuple[str, str]:
    """
    Extract audio track from video file.
    
    Args:
        video_path: Path to video file
        output_format: Audio format ('mp3', 'wav', 'flac', 'copy')
        bitrate: Audio bitrate for lossy formats (ignored if copy_stream=True)
        cleanup_original: Whether to delete the original video file
        copy_stream: If True, copy audio stream without re-encoding (fast, preserves quality)
                    If False, re-encode to specified format
    
    Returns:
        Tuple of (audio_filepath, mime_type)
        
    Raises:
        FFmpegNotFoundError: If FFmpeg is not installed
        FFmpegError: If extraction fails
    """
    base_path = os.path.splitext(video_path)[0]
    
    try:
        if copy_stream or output_format == 'copy':
            # Copy audio stream without re-encoding - need to detect the format first
            from src.utils.ffprobe import get_codec_info
            
            try:
                codec_info = get_codec_info(video_path, timeout=10)
                audio_codec = codec_info.get('audio_codec', 'unknown')
                
                # Map codec to extension and MIME type
                codec_map = {
                    'aac': {'ext': 'm4a', 'mime': 'audio/mp4'},
                    'mp3': {'ext': 'mp3', 'mime': 'audio/mpeg'},
                    'opus': {'ext': 'opus', 'mime': 'audio/opus'},
                    'vorbis': {'ext': 'ogg', 'mime': 'audio/ogg'},
                    'flac': {'ext': 'flac', 'mime': 'audio/flac'},
                }
                
                if audio_codec in codec_map:
                    output_ext = codec_map[audio_codec]['ext']
                    mime_type = codec_map[audio_codec]['mime']
                else:
                    # Default to m4a for unknown codecs
                    current_app.logger.warning(f"Unknown audio codec '{audio_codec}', defaulting to m4a container")
                    output_ext = 'm4a'
                    mime_type = 'audio/mp4'
                
                temp_audio_path = f"{base_path}_audio_temp.{output_ext}"
                final_audio_path = f"{base_path}_audio.{output_ext}"
                
                cmd = [
                    'ffmpeg',
                    '-i', video_path,
                    '-y',
                    '-vn',  # No video
                    '-acodec', 'copy',  # Copy audio stream without re-encoding
                    temp_audio_path
                ]
                
                current_app.logger.info(f"Copying audio stream (codec: {audio_codec}) without re-encoding")
                
            except Exception as probe_error:
                current_app.logger.warning(f"Failed to detect audio codec: {probe_error}. Falling back to MP3 encoding.")
                # Fallback to MP3 encoding if we can't detect the codec
                output_ext = 'mp3'
                mime_type = 'audio/mpeg'
                temp_audio_path = f"{base_path}_audio_temp.{output_ext}"
                final_audio_path = f"{base_path}_audio.{output_ext}"
                
                cmd = [
                    'ffmpeg',
                    '-i', video_path,
                    '-y',
                    '-vn',
                    '-acodec', 'libmp3lame',
                    '-b:a', bitrate,
                    '-ar', DEFAULT_SAMPLE_RATE,
                    '-ac', str(DEFAULT_CHANNELS),
                    '-compression_level', str(DEFAULT_COMPRESSION_LEVEL),
                    temp_audio_path
                ]
        
        elif output_format == 'mp3':
            temp_audio_path = f"{base_path}_audio_temp.mp3"
            final_audio_path = f"{base_path}_audio.mp3"
            cmd = [
                'ffmpeg',
                '-i', video_path,
                '-y',
                '-vn',  # No video
                '-acodec', 'libmp3lame',
                '-b:a', bitrate,
                '-ar', DEFAULT_SAMPLE_RATE,
                '-ac', str(DEFAULT_CHANNELS),
                '-compression_level', str(DEFAULT_COMPRESSION_LEVEL),
                temp_audio_path
            ]
            mime_type = 'audio/mpeg'
        elif output_format == 'wav':
            temp_audio_path = f"{base_path}_audio_temp.wav"
            final_audio_path = f"{base_path}_audio.wav"
            cmd = [
                'ffmpeg',
                '-i', video_path,
                '-y',
                '-vn',
                '-acodec', 'pcm_s16le',
                '-ar', DEFAULT_SAMPLE_RATE,
                temp_audio_path
            ]
            mime_type = 'audio/wav'
        elif output_format == 'flac':
            temp_audio_path = f"{base_path}_audio_temp.flac"
            final_audio_path = f"{base_path}_audio.flac"
            cmd = [
                'ffmpeg',
                '-i', video_path,
                '-y',
                '-vn',
                '-acodec', 'flac',
                '-compression_level', '12',
                temp_audio_path
            ]
            mime_type = 'audio/flac'
        elif output_format == 'opus':
            temp_audio_path = f"{base_path}_audio_temp.opus"
            final_audio_path = f"{base_path}_audio.opus"
            cmd = [
                'ffmpeg',
                '-i', video_path,
                '-y',
                '-vn',
                '-acodec', 'libopus',
                '-b:a', bitrate,
                temp_audio_path
            ]
            mime_type = 'audio/opus'
        else:
            raise ValueError(f"Unsupported output format: {output_format}")
        
        _run_ffmpeg_command(cmd, f"Audio extraction from {os.path.basename(video_path)}")
        
        # Optionally preserve temp file for debugging
        if os.getenv('PRESERVE_TEMP_AUDIO', 'false').lower() == 'true':
            import shutil
            debug_path = temp_audio_path.replace('_temp', '_debug')
            shutil.copy2(temp_audio_path, debug_path)
            current_app.logger.info(f"Debug: Preserved temp audio file as {debug_path}")
        
        # Rename temp file to final filename
        os.rename(temp_audio_path, final_audio_path)
        
        if cleanup_original:
            try:
                os.remove(video_path)
                current_app.logger.info(f"Cleaned up original video: {os.path.basename(video_path)}")
            except Exception as e:
                current_app.logger.warning(f"Failed to cleanup video {video_path}: {e}")
        
        return final_audio_path, mime_type
        
    except Exception as e:
        # Clean up temp file on error
        if os.path.exists(temp_audio_path):
            try:
                os.remove(temp_audio_path)
            except:
                pass
        raise


def compress_audio(
    input_path: str,
    codec: str = 'mp3',
    bitrate: str = DEFAULT_MP3_BITRATE,
    delete_original: bool = True,
    codec_info: Optional[dict] = None
) -> Tuple[str, str, Optional[dict]]:
    """
    Compress audio file to specified codec.
    
    Args:
        input_path: Path to input audio file
        codec: Target codec ('mp3', 'flac', 'opus')
        bitrate: Bitrate for lossy codecs (ignored for FLAC)
        delete_original: Whether to delete the original file after compression
        codec_info: Optional pre-fetched codec info (returned as-is, not updated)
    
    Returns:
        Tuple of (output_path, mime_type, codec_info)
        Note: codec_info is returned unchanged (None after compression)
        
    Raises:
        FFmpegNotFoundError: If FFmpeg is not installed
        FFmpegError: If compression fails
    """
    codec_config = {
        'mp3': {
            'ext': '.mp3',
            'mime': 'audio/mpeg',
            'cmd_args': [
                '-acodec', 'libmp3lame',
                '-b:a', bitrate,
                '-ar', DEFAULT_SAMPLE_RATE,
                '-ac', str(DEFAULT_CHANNELS)
            ]
        },
        'flac': {
            'ext': '.flac',
            'mime': 'audio/flac',
            'cmd_args': ['-acodec', 'flac', '-compression_level', '12']
        },
        'opus': {
            'ext': '.opus',
            'mime': 'audio/opus',
            'cmd_args': ['-acodec', 'libopus', '-b:a', bitrate]
        }
    }
    
    if codec not in codec_config:
        raise ValueError(f"Unsupported codec: {codec}. Supported: {list(codec_config.keys())}")
    
    config = codec_config[codec]
    base_path = os.path.splitext(input_path)[0]
    temp_output_path = f"{base_path}_compressed_temp{config['ext']}"
    final_output_path = f"{base_path}{config['ext']}"
    
    try:
        # Get original file size for logging
        original_size = os.path.getsize(input_path)
        
        cmd = ['ffmpeg', '-i', input_path, '-y'] + config['cmd_args'] + [temp_output_path]
        
        _run_ffmpeg_command(cmd, f"Compression of {os.path.basename(input_path)} to {codec}")
        
        # Get compressed file size
        compressed_size = os.path.getsize(temp_output_path)
        ratio = (1 - compressed_size / original_size) * 100 if original_size > 0 else 0
        
        current_app.logger.info(
            f"Compressed {os.path.basename(input_path)}: "
            f"{original_size / 1024 / 1024:.1f}MB -> "
            f"{compressed_size / 1024 / 1024:.1f}MB ({ratio:.1f}% reduction)"
        )
        
        # Remove original and rename temp to final
        if delete_original:
            os.remove(input_path)
            current_app.logger.debug(f"Deleted original file: {input_path}")
        os.rename(temp_output_path, final_output_path)
        
        # Return codec_info as None since file was converted (codec changed)
        return final_output_path, config['mime'], None
        
    except Exception as e:
        # Clean up temp file if it exists
        if os.path.exists(temp_output_path):
            try:
                os.remove(temp_output_path)
            except:
                pass
        # Re-raise with codec_info preservation
        raise


def extract_audio_segment(
    input_path: str,
    output_path: str,
    start_time: float,
    duration: float
) -> None:
    """
    Extract a segment from an audio file.
    
    Args:
        input_path: Path to input audio file
        output_path: Path for output segment
        start_time: Start time in seconds
        duration: Duration in seconds
        
    Raises:
        FFmpegNotFoundError: If FFmpeg is not installed
        FFmpegError: If extraction fails
    """
    cmd = [
        'ffmpeg',
        '-i', input_path,
        '-ss', str(start_time),
        '-t', str(duration),
        '-c', 'copy',  # Copy codec (no re-encoding)
        '-y',
        output_path
    ]
    
    _run_ffmpeg_command(cmd, f"Segment extraction from {os.path.basename(input_path)}")


@contextmanager
def temp_audio_conversion(input_path: str, target_format: str = 'mp3'):
    """
    Context manager for temporary audio conversion.
    Automatically cleans up temp file on exit.
    
    Example:
        with temp_audio_conversion(input_path, 'mp3') as mp3_path:
            # Use mp3_path
            process_audio(mp3_path)
        # mp3_path is automatically deleted
        
    Args:
        input_path: Path to input audio file
        target_format: Target format ('mp3', 'wav', etc.)
        
    Yields:
        Path to temporary converted file
    """
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f'.{target_format}', delete=False) as temp_file:
            temp_path = temp_file.name
        
        if target_format == 'mp3':
            convert_to_mp3(input_path, temp_path)
        else:
            raise ValueError(f"Unsupported target format: {target_format}")
        
        yield temp_path
        
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception as e:
                current_app.logger.warning(f"Failed to cleanup temp file {temp_path}: {e}")


def _run_ffmpeg_command(cmd: list, operation_description: str) -> None:
    """
    Execute FFmpeg command with consistent error handling.
    
    Args:
        cmd: FFmpeg command as list of strings
        operation_description: Human-readable description for error messages
        
    Raises:
        FFmpegNotFoundError: If FFmpeg is not installed
        FFmpegError: If FFmpeg command fails
    """
    try:
        current_app.logger.debug(f"Running FFmpeg command: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True
        )
        current_app.logger.debug(f"FFmpeg {operation_description} completed successfully")
        
    except FileNotFoundError:
        error_msg = "FFmpeg not found. Please ensure FFmpeg is installed and in the system's PATH."
        current_app.logger.error(error_msg)
        raise FFmpegNotFoundError(error_msg)
        
    except subprocess.CalledProcessError as e:
        error_msg = f"{operation_description} failed: {e.stderr}"
        current_app.logger.error(f"FFmpeg error: {error_msg}")
        raise FFmpegError(error_msg)