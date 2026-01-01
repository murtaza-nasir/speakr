"""
Audio conversion utility for handling codec detection and file conversion.

This module provides a single, unified interface for handling ALL audio/video
conversion needs:
- Video to audio extraction
- Unsupported codec conversion
- Lossless audio compression

Callers should ONLY use convert_if_needed() - it handles everything.
"""

import os
import logging
from pathlib import Path
from typing import Optional, Tuple, Set, Dict, Any

from src.utils.ffprobe import get_codec_info, is_lossless_audio, FFProbeError
from src.utils.ffmpeg_utils import compress_audio, extract_audio_from_video, FFmpegError, FFmpegNotFoundError
from src.config.app_config import AUDIO_COMPRESS_UPLOADS, AUDIO_CODEC, AUDIO_BITRATE, AUDIO_UNSUPPORTED_CODECS

logger = logging.getLogger(__name__)


class ConversionResult:
    """Result of a conversion operation."""

    def __init__(
        self,
        output_path: str,
        mime_type: str,
        was_converted: bool,
        was_compressed: bool,
        original_size: int,
        final_size: int,
        original_codec: Optional[str] = None,
        final_codec: Optional[str] = None
    ):
        self.output_path = output_path
        self.mime_type = mime_type
        self.was_converted = was_converted
        self.was_compressed = was_compressed
        self.original_size = original_size
        self.final_size = final_size
        self.original_codec = original_codec
        self.final_codec = final_codec

    @property
    def size_reduction_percent(self) -> float:
        """Calculate size reduction percentage."""
        if self.original_size == 0:
            return 0.0
        return ((self.original_size - self.final_size) / self.original_size) * 100

    @property
    def original_size_mb(self) -> float:
        """Original size in megabytes."""
        return self.original_size / (1024 * 1024)

    @property
    def final_size_mb(self) -> float:
        """Final size in megabytes."""
        return self.final_size / (1024 * 1024)


def get_supported_codecs(needs_chunking: bool = False) -> Set[str]:
    """
    Get the set of supported audio codecs.

    Args:
        needs_chunking: If True, return only codecs that work well with chunking

    Returns:
        Set of supported codec names (minus any excluded via AUDIO_UNSUPPORTED_CODECS)
    """
    if needs_chunking:
        # For chunking: only support codecs that work well with chunking
        base_codecs = {'pcm_s16le', 'pcm_s24le', 'pcm_f32le', 'mp3', 'flac'}
    else:
        # For direct transcription: support more codecs including WebM/Opus
        base_codecs = {'pcm_s16le', 'pcm_s24le', 'pcm_f32le', 'mp3', 'flac', 'opus', 'vorbis', 'aac'}

    # Remove any user-specified unsupported codecs
    if AUDIO_UNSUPPORTED_CODECS:
        excluded = base_codecs & AUDIO_UNSUPPORTED_CODECS
        if excluded:
            logger.info(f"Excluding codecs from supported list (via AUDIO_UNSUPPORTED_CODECS): {excluded}")
        return base_codecs - AUDIO_UNSUPPORTED_CODECS

    return base_codecs


def convert_if_needed(
    filepath: str,
    original_filename: Optional[str] = None,
    codec_info: Optional[Dict[str, Any]] = None,
    needs_chunking: bool = False,
    is_asr_endpoint: bool = False,
    delete_original: bool = True
) -> ConversionResult:
    """
    Handle ALL audio conversion needs in one place.

    This is the ONLY function callers should use. It handles:
    1. Video to audio extraction (if has_video)
    2. Unsupported codec conversion (if codec not supported)
    3. Lossless audio compression (if AUDIO_COMPRESS_UPLOADS enabled)

    The function makes intelligent decisions about what processing is needed
    and performs it in the optimal order.

    Args:
        filepath: Path to the audio/video file
        original_filename: Original filename for logging (defaults to basename)
        codec_info: Optional pre-fetched codec info to avoid redundant probe calls
        needs_chunking: Whether chunking will be used (affects supported codecs)
        is_asr_endpoint: Whether using ASR endpoint (affects AAC handling)
        delete_original: Whether to delete original file after successful conversion

    Returns:
        ConversionResult with output path, mime type, and conversion stats

    Raises:
        FFmpegNotFoundError: If FFmpeg is not available
        FFmpegError: If conversion fails
    """
    if original_filename is None:
        original_filename = os.path.basename(filepath)

    # Get original file size
    original_size = os.path.getsize(filepath)

    # Probe if codec info not provided
    if codec_info is None:
        try:
            codec_info = get_codec_info(filepath, timeout=10)
            logger.info(
                f"Detected codec for {original_filename}: "
                f"audio_codec={codec_info.get('audio_codec')}, "
                f"has_video={codec_info.get('has_video', False)}"
            )
        except FFProbeError as e:
            logger.warning(f"Failed to probe {filepath}: {e}. Will attempt conversion.")
            codec_info = None

    original_codec = codec_info.get('audio_codec') if codec_info else None
    audio_codec = original_codec
    has_video = codec_info.get('has_video', False) if codec_info else False

    # Get supported codecs based on processing mode
    supported_codecs = get_supported_codecs(needs_chunking)

    # Handle video files - extract audio
    if has_video:
        # Check if we can remux (copy) instead of transcode
        can_remux = False
        if audio_codec and audio_codec in supported_codecs:
            try:
                # Remux if audio is lossy, or if lossless but compression is disabled
                is_lossless = is_lossless_audio(filepath, codec_info=codec_info)
                can_remux = not is_lossless or not AUDIO_COMPRESS_UPLOADS
            except Exception as e:
                logger.warning(f"Could not determine if audio is lossless: {e}. Will transcode.")
        
        try:
            if can_remux:
                logger.info(f"Extracting audio from video (remux, no transcoding): {original_filename}")
                output_filepath, mime_type = extract_audio_from_video(
                    filepath,
                    output_format='copy',
                    cleanup_original=delete_original,
                    copy_stream=True
                )
                final_codec = audio_codec
            else:
                logger.info(f"Extracting and converting audio from video to {AUDIO_CODEC.upper()}: {original_filename}")
                output_filepath, mime_type = extract_audio_from_video(
                    filepath,
                    output_format=AUDIO_CODEC,
                    bitrate=AUDIO_BITRATE,
                    cleanup_original=delete_original,
                    copy_stream=False
                )
                final_codec = AUDIO_CODEC
            
            final_size = os.path.getsize(output_filepath)
            reduction = ((original_size - final_size) / original_size * 100) if original_size > 0 else 0
            
            logger.info(
                f"Successfully extracted audio from {original_filename}: "
                f"{original_size/1024/1024:.1f}MB -> {final_size/1024/1024:.1f}MB "
                f"({reduction:.1f}% reduction)"
            )
            
            return ConversionResult(
                output_path=output_filepath,
                mime_type=mime_type,
                was_converted=not can_remux,
                was_compressed=False,
                original_size=original_size,
                final_size=final_size,
                original_codec=original_codec,
                final_codec=final_codec
            )
        except FFmpegNotFoundError:
            logger.error("FFmpeg not found")
            raise
        except FFmpegError as e:
            logger.error(f"Failed to extract audio from video {filepath}: {e}")
            raise
    
    # Handle audio files - check if conversion needed
    needs_conversion = False
    if audio_codec is None:
        needs_conversion = True
        logger.info(f"Unknown codec for {original_filename}, will attempt conversion")
    elif is_asr_endpoint and audio_codec == 'aac':
        needs_conversion = True
        logger.info(f"Converting AAC-encoded file for ASR endpoint compatibility")
    elif audio_codec not in supported_codecs:
        needs_conversion = True
        logger.info(f"Converting {original_filename} (codec: {audio_codec}) - unsupported for processing")
    
    if needs_conversion:
        logger.info(f"Converting {original_filename} to {AUDIO_CODEC.upper()}")
        
        try:
            output_filepath, mime_type, _ = compress_audio(
                filepath,
                codec=AUDIO_CODEC,
                bitrate=AUDIO_BITRATE,
                delete_original=delete_original,
                codec_info=codec_info
            )
            
            final_size = os.path.getsize(output_filepath)
            reduction = ((original_size - final_size) / original_size * 100) if original_size > 0 else 0
            
            logger.info(
                f"Successfully converted {original_filename}: "
                f"{original_size/1024/1024:.1f}MB -> {final_size/1024/1024:.1f}MB "
                f"({reduction:.1f}% reduction)"
            )
            
            return ConversionResult(
                output_path=output_filepath,
                mime_type=mime_type,
                was_converted=True,
                was_compressed=False,
                original_size=original_size,
                final_size=final_size,
                original_codec=original_codec,
                final_codec=AUDIO_CODEC
            )
        except FFmpegNotFoundError:
            logger.error("FFmpeg not found")
            raise
        except FFmpegError as e:
            logger.error(f"FFmpeg conversion failed for {filepath}: {e}")
            raise
    
    # Audio file with supported codec - check if we should compress lossless
    logger.info(f"Codec {audio_codec} is supported, no conversion needed")

    if AUDIO_COMPRESS_UPLOADS:
        try:
            # Check if file is lossless
            if is_lossless_audio(filepath, codec_info=codec_info):
                # Skip if already in target codec (e.g., FLAC to FLAC)
                if audio_codec == AUDIO_CODEC:
                    logger.info(f"File already in target codec {AUDIO_CODEC}, no compression needed")
                    return ConversionResult(
                        output_path=filepath,
                        mime_type=_guess_mime_type(filepath),
                        was_converted=False,
                        was_compressed=False,
                        original_size=original_size,
                        final_size=original_size,
                        original_codec=original_codec,
                        final_codec=audio_codec
                    )

                logger.info(f"Compressing lossless audio ({audio_codec}) to {AUDIO_CODEC.upper()}")

                # Perform compression
                compressed_path, mime_type, _ = compress_audio(
                    filepath,
                    codec=AUDIO_CODEC,
                    bitrate=AUDIO_BITRATE,
                    delete_original=delete_original,
                    codec_info=codec_info
                )

                final_size = os.path.getsize(compressed_path)
                reduction = ((original_size - final_size) / original_size * 100) if original_size > 0 else 0

                logger.info(
                    f"Successfully compressed {original_filename}: "
                    f"{original_size/1024/1024:.1f}MB -> {final_size/1024/1024:.1f}MB "
                    f"({reduction:.1f}% reduction)"
                )

                return ConversionResult(
                    output_path=compressed_path,
                    mime_type=mime_type,
                    was_converted=False,
                    was_compressed=True,
                    original_size=original_size,
                    final_size=final_size,
                    original_codec=original_codec,
                    final_codec=AUDIO_CODEC
                )
        except Exception as e:
            logger.warning(f"Failed to compress lossless audio: {e}. Continuing with original.")
            # Fall through to return original file

    # No processing needed - return original file
    return ConversionResult(
        output_path=filepath,
        mime_type=_guess_mime_type(filepath),
        was_converted=False,
        was_compressed=False,
        original_size=original_size,
        final_size=original_size,
        original_codec=original_codec,
        final_codec=audio_codec
    )


def _guess_mime_type(filepath: str) -> str:
    """
    Guess MIME type from file extension.

    Args:
        filepath: Path to the file

    Returns:
        MIME type string
    """
    import mimetypes
    mime_type, _ = mimetypes.guess_type(filepath)
    return mime_type or 'application/octet-stream'
