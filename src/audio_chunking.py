"""
Audio Chunking Service for Large File Processing with OpenAI Whisper API

This module provides functionality to split large audio files into smaller chunks
that comply with OpenAI's 25MB file size limit, process them individually,
and reassemble the transcriptions while maintaining accuracy and speaker continuity.
"""

import os
import json
import subprocess
import tempfile
import logging
import math
import re
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple, TYPE_CHECKING
from datetime import datetime
import mimetypes

from src.utils.ffmpeg_utils import convert_to_mp3, FFmpegError, FFmpegNotFoundError

if TYPE_CHECKING:
    from src.services.transcription.base import ConnectorSpecifications

# Configure logging
logger = logging.getLogger(__name__)


@dataclass
class EffectiveChunkingConfig:
    """Effective chunking configuration after resolving connector specs and ENV settings."""
    enabled: bool
    mode: str  # 'size' or 'duration'
    limit_value: float  # MB for size, seconds for duration
    overlap_seconds: int
    source: str  # 'disabled', 'connector_internal', 'env', 'connector_default', 'app_default'


def get_effective_chunking_config(
    connector_specs: Optional['ConnectorSpecifications'] = None
) -> EffectiveChunkingConfig:
    """
    Determine effective chunking configuration based on connector specs and ENV settings.

    Logic:
    1. Gather connector constraints (max_duration_seconds, max_file_size_bytes)
    2. Gather user settings (CHUNK_LIMIT, CHUNK_SIZE_MB, ENABLE_CHUNKING)
    3. If connector has hard limits:
       - Chunking is REQUIRED (can't disable)
       - Use MIN(connector_limit, user_limit) - user can go smaller but not larger
    4. If connector has no hard limits:
       - If handles_chunking_internally=True → no app chunking
       - If ENABLE_CHUNKING=false → no chunking
       - Otherwise use user settings or app defaults

    Args:
        connector_specs: Optional ConnectorSpecifications from the active connector

    Returns:
        EffectiveChunkingConfig with resolved settings
    """
    overlap_seconds = int(os.environ.get('CHUNK_OVERLAP_SECONDS', '3'))
    enable_chunking_env = os.environ.get('ENABLE_CHUNKING', '').lower()

    # --- Step 1: Determine connector's hard limits ---
    connector_duration_limit = None
    connector_size_limit_mb = None

    if connector_specs:
        if connector_specs.max_duration_seconds:
            # Use recommended if available, otherwise 85% of max for safety
            if connector_specs.recommended_chunk_seconds:
                connector_duration_limit = connector_specs.recommended_chunk_seconds
            else:
                connector_duration_limit = int(connector_specs.max_duration_seconds * 0.85)

        if connector_specs.max_file_size_bytes:
            # Use 80% of max for safety margin
            connector_size_limit_mb = (connector_specs.max_file_size_bytes / (1024 * 1024)) * 0.8

    has_hard_limits = connector_duration_limit is not None or connector_size_limit_mb is not None

    # --- Step 2: Parse user settings ---
    user_duration_limit = None
    user_size_limit_mb = None

    chunk_limit = os.environ.get('CHUNK_LIMIT', '').strip()
    chunk_size_mb_env = os.environ.get('CHUNK_SIZE_MB', '').strip()

    if chunk_limit:
        chunk_limit_upper = chunk_limit.upper()
        try:
            if chunk_limit_upper.endswith('MB'):
                user_size_limit_mb = float(re.sub(r'[^0-9.]', '', chunk_limit_upper))
            elif chunk_limit_upper.endswith('S'):
                user_duration_limit = float(re.sub(r'[^0-9.]', '', chunk_limit_upper))
            elif chunk_limit_upper.endswith('M') and not chunk_limit_upper.endswith('MB'):
                user_duration_limit = float(re.sub(r'[^0-9.]', '', chunk_limit_upper)) * 60
        except ValueError:
            logger.warning(f"Invalid CHUNK_LIMIT format: {chunk_limit}")
    elif chunk_size_mb_env:
        try:
            user_size_limit_mb = float(chunk_size_mb_env)
        except ValueError:
            logger.warning(f"Invalid CHUNK_SIZE_MB format: {chunk_size_mb_env}")

    # --- Step 3: If connector has hard limits, chunking is REQUIRED ---
    if has_hard_limits:
        # Prefer duration-based if connector has duration limit
        if connector_duration_limit is not None:
            # Use minimum of connector limit and user limit (if user set one)
            if user_duration_limit is not None:
                effective_limit = min(connector_duration_limit, user_duration_limit)
                source = 'user_and_connector'
                logger.info(f"Chunking: Using MIN(connector={connector_duration_limit}s, user={user_duration_limit}s) = {effective_limit}s")
            else:
                effective_limit = connector_duration_limit
                source = 'connector_limit'
                logger.info(f"Chunking: Connector requires duration limit {effective_limit}s (max_duration={connector_specs.max_duration_seconds}s)")

            return EffectiveChunkingConfig(
                enabled=True,
                mode='duration',
                limit_value=effective_limit,
                overlap_seconds=overlap_seconds,
                source=source
            )

        # Fall back to size-based if only size limit exists
        elif connector_size_limit_mb is not None:
            if user_size_limit_mb is not None:
                effective_limit = min(connector_size_limit_mb, user_size_limit_mb)
                source = 'user_and_connector'
                logger.info(f"Chunking: Using MIN(connector={connector_size_limit_mb:.1f}MB, user={user_size_limit_mb}MB) = {effective_limit:.1f}MB")
            else:
                effective_limit = connector_size_limit_mb
                source = 'connector_limit'
                logger.info(f"Chunking: Connector requires size limit {effective_limit:.1f}MB (max_size={connector_specs.max_file_size_bytes/(1024*1024):.1f}MB)")

            return EffectiveChunkingConfig(
                enabled=True,
                mode='size',
                limit_value=effective_limit,
                overlap_seconds=overlap_seconds,
                source=source
            )

    # --- Step 4: No hard limits - chunking is optional ---

    # Connector handles chunking internally
    if connector_specs and connector_specs.handles_chunking_internally:
        logger.info("Chunking: Connector handles chunking internally, no app-level chunking needed")
        return EffectiveChunkingConfig(
            enabled=False,
            mode='none',
            limit_value=0,
            overlap_seconds=overlap_seconds,
            source='connector_internal'
        )

    # User explicitly disabled chunking
    if enable_chunking_env == 'false':
        logger.info("Chunking: Disabled via ENABLE_CHUNKING=false")
        return EffectiveChunkingConfig(
            enabled=False,
            mode='none',
            limit_value=0,
            overlap_seconds=overlap_seconds,
            source='disabled'
        )

    # User set explicit limits - use them
    if user_duration_limit is not None:
        logger.info(f"Chunking: Using user CHUNK_LIMIT={user_duration_limit}s")
        return EffectiveChunkingConfig(
            enabled=True,
            mode='duration',
            limit_value=user_duration_limit,
            overlap_seconds=overlap_seconds,
            source='env'
        )

    if user_size_limit_mb is not None:
        logger.info(f"Chunking: Using user CHUNK_LIMIT={user_size_limit_mb}MB")
        return EffectiveChunkingConfig(
            enabled=True,
            mode='size',
            limit_value=user_size_limit_mb,
            overlap_seconds=overlap_seconds,
            source='env'
        )

    # Connector has recommended settings (but no hard limits)
    if connector_specs and connector_specs.recommended_chunk_seconds:
        logger.info(f"Chunking: Using connector recommended={connector_specs.recommended_chunk_seconds}s")
        return EffectiveChunkingConfig(
            enabled=True,
            mode='duration',
            limit_value=connector_specs.recommended_chunk_seconds,
            overlap_seconds=overlap_seconds,
            source='connector_recommended'
        )

    # App defaults
    if enable_chunking_env != 'false':
        logger.info("Chunking: Using app defaults (20MB size-based)")
        return EffectiveChunkingConfig(
            enabled=True,
            mode='size',
            limit_value=20.0,
            overlap_seconds=overlap_seconds,
            source='app_default'
        )

    # Final fallback: disabled
    return EffectiveChunkingConfig(
        enabled=False,
        mode='none',
        limit_value=0,
        overlap_seconds=overlap_seconds,
        source='disabled'
    )

class AudioChunkingService:
    """Service for chunking large audio files and processing them with OpenAI Whisper API."""
    
    def __init__(self, max_chunk_size_mb: int = 20, overlap_seconds: int = 3, max_chunk_duration_seconds: int = None):
        """
        Initialize the chunking service.
        
        Args:
            max_chunk_size_mb: Maximum size for each chunk in MB (default 20MB for safety margin)
            overlap_seconds: Overlap between chunks in seconds for context continuity
            max_chunk_duration_seconds: Maximum duration for each chunk in seconds (optional)
        """
        self.max_chunk_size_mb = max_chunk_size_mb
        self.overlap_seconds = overlap_seconds
        self.max_chunk_size_bytes = max_chunk_size_mb * 1024 * 1024
        self.max_chunk_duration_seconds = max_chunk_duration_seconds
        self.chunk_stats = []  # Track processing statistics
        
    def needs_chunking(
        self,
        file_path: str,
        use_asr_endpoint: bool = False,
        connector_specs: Optional['ConnectorSpecifications'] = None
    ) -> bool:
        """
        Check if a file needs to be chunked based on connector specs, ENV settings, and file size.

        Priority order for chunking configuration:
        1. If connector handles_chunking_internally=True → no app-level chunking
        2. If ENABLE_CHUNKING=false → no chunking
        3. If CHUNK_LIMIT or CHUNK_SIZE_MB is explicitly set → use ENV settings
        4. If connector has specs → use connector defaults
        5. Fallback: 20MB size-based chunking

        NOTE: For duration-based limits, this may return True even if chunking isn't needed,
        because we need to convert the file first to check duration. The actual chunking
        decision is made after conversion in calculate_optimal_chunking().

        Args:
            file_path: Path to the audio file
            use_asr_endpoint: DEPRECATED - use connector_specs instead
            connector_specs: Optional ConnectorSpecifications from the active connector

        Returns:
            True if file might need chunking, False otherwise
        """
        # Get effective chunking configuration
        chunking_config = get_effective_chunking_config(connector_specs)

        # If chunking is disabled (by connector or user), return False
        if not chunking_config.enabled:
            logger.info(f"Chunking disabled (source: {chunking_config.source})")
            return False

        # Legacy fallback: if no connector_specs provided, check use_asr_endpoint
        if connector_specs is None and use_asr_endpoint:
            logger.info("Chunking: ASR endpoint detected (legacy), no chunking needed")
            return False

        try:
            file_size = os.path.getsize(file_path)

            if chunking_config.mode == 'size':
                # For size-based limits, we can determine immediately
                chunk_size_bytes = chunking_config.limit_value * 1024 * 1024
                needs_it = file_size > chunk_size_bytes
                logger.info(f"Chunking check (size, source={chunking_config.source}): "
                           f"{file_size/1024/1024:.1f}MB vs limit {chunking_config.limit_value}MB - needs chunking: {needs_it}")
                return needs_it
            elif chunking_config.mode == 'duration':
                # For duration-based limits, we need to check the actual duration
                # Try to get duration without conversion first (fast check)
                duration = self.get_audio_duration(file_path)
                if duration:
                    needs_it = duration > chunking_config.limit_value
                    logger.info(f"Chunking check (duration, source={chunking_config.source}): "
                               f"{duration:.1f}s vs limit {chunking_config.limit_value}s - needs chunking: {needs_it}")
                    return needs_it
                else:
                    # Can't determine duration without conversion, assume might need chunking
                    logger.info(f"Duration-based limit set ({chunking_config.limit_value}s) but can't check duration yet - will check after conversion")
                    return True  # Proceed to conversion and check
            else:
                # Mode is 'none', shouldn't reach here but handle gracefully
                return False

        except OSError:
            logger.error(f"Could not get file size for {file_path}")
            return False

    def needs_chunking_with_config(
        self,
        file_path: str,
        connector_specs: Optional['ConnectorSpecifications'] = None
    ) -> Tuple[bool, EffectiveChunkingConfig]:
        """
        Check if a file needs chunking and return the effective configuration.

        This is useful when you need both the decision and the configuration
        for subsequent processing.

        Args:
            file_path: Path to the audio file
            connector_specs: Optional ConnectorSpecifications from the active connector

        Returns:
            Tuple of (needs_chunking, EffectiveChunkingConfig)
        """
        chunking_config = get_effective_chunking_config(connector_specs)
        needs_it = self.needs_chunking(file_path, connector_specs=connector_specs)
        return needs_it, chunking_config
    
    def get_audio_duration(self, file_path: str) -> Optional[float]:
        """
        Get the duration of an audio file in seconds using ffprobe.
        
        Args:
            file_path: Path to the audio file
            
        Returns:
            Duration in seconds, or None if unable to determine
        """
        try:
            result = subprocess.run([
                'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', file_path
            ], capture_output=True, text=True, check=True)
            
            duration = float(result.stdout.strip())
            return duration
        except (subprocess.CalledProcessError, ValueError, FileNotFoundError) as e:
            logger.error(f"Error getting audio duration for {file_path}: {e}")
            return None
    
    def convert_to_mp3_and_get_info(self, file_path: str, temp_dir: str) -> Tuple[str, float, float]:
        """
        Convert the input file to MP3 format for consistency and get its size and duration info.
        
        Args:
            file_path: Path to the source audio file
            temp_dir: Directory to store the temporary MP3 file
            
        Returns:
            Tuple of (mp3_file_path, duration_seconds, size_bytes)
        """
        try:
            # Generate MP3 filename
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            mp3_filename = f"{base_name}_converted.mp3"
            mp3_path = os.path.join(temp_dir, mp3_filename)
            
            logger.info(f"Converting {file_path} to 128kbps MP3 format for accurate chunking...")
            
            # Use centralized FFmpeg utility for conversion
            convert_to_mp3(file_path, mp3_path)
            
            if not os.path.exists(mp3_path):
                raise ValueError("MP3 file was not created")
            
            # Get the size and duration of the converted MP3 file
            mp3_size = os.path.getsize(mp3_path)
            mp3_duration = self.get_audio_duration(mp3_path)
            
            if not mp3_duration:
                raise ValueError("Could not determine MP3 file duration")
            
            logger.info(f"Converted MP3: {mp3_size/1024/1024:.1f}MB, {mp3_duration:.1f}s")
            
            # Optionally preserve converted file for debugging (set PRESERVE_CHUNK_DEBUG=true in env)
            if os.getenv('PRESERVE_CHUNK_DEBUG', 'false').lower() == 'true':
                import shutil
                # Save debug files in /data/uploads/debug/ directory
                debug_dir = '/data/uploads/debug'
                os.makedirs(debug_dir, exist_ok=True)
                debug_filename = os.path.basename(mp3_path).replace('_converted', '_converted_debug')
                debug_path = os.path.join(debug_dir, debug_filename)
                shutil.copy2(mp3_path, debug_path)
                logger.info(f"Debug: Preserved converted file as {debug_path}")
            
            return mp3_path, mp3_duration, mp3_size
            
        except (FFmpegError, FFmpegNotFoundError) as e:
            logger.error(f"Error converting file to MP3: {e}")
            raise
        except Exception as e:
            logger.error(f"Error converting file to MP3: {e}")
            raise

    def parse_chunk_limit(self) -> Tuple[str, float]:
        """
        Parse the CHUNK_LIMIT environment variable to determine chunking mode and value.
        
        Supports formats:
        - Size-based: "20MB", "10MB" 
        - Duration-based: "1200s", "20m"
        - Legacy: CHUNK_SIZE_MB environment variable (for backwards compatibility)
        
        Returns:
            Tuple of (mode, value) where mode is 'size' or 'duration'
        """
        chunk_limit = os.environ.get('CHUNK_LIMIT', '').strip().upper()
        
        # Check for new CHUNK_LIMIT format
        if chunk_limit:
            # Size-based: ends with MB
            if chunk_limit.endswith('MB'):
                try:
                    size_mb = float(re.sub(r'[^0-9.]', '', chunk_limit))
                    return 'size', size_mb
                except ValueError:
                    logger.warning(f"Invalid CHUNK_LIMIT format: {chunk_limit}")
            
            # Duration-based: ends with s or m
            elif chunk_limit.endswith('S'):
                try:
                    seconds = float(re.sub(r'[^0-9.]', '', chunk_limit))
                    return 'duration', seconds
                except ValueError:
                    logger.warning(f"Invalid CHUNK_LIMIT format: {chunk_limit}")
            
            elif chunk_limit.endswith('M'):
                try:
                    minutes = float(re.sub(r'[^0-9.]', '', chunk_limit))
                    return 'duration', minutes * 60
                except ValueError:
                    logger.warning(f"Invalid CHUNK_LIMIT format: {chunk_limit}")
        
        # Fallback to legacy CHUNK_SIZE_MB for backwards compatibility
        legacy_size = os.environ.get('CHUNK_SIZE_MB', '20')
        try:
            size_mb = float(legacy_size)
            logger.info(f"Using legacy CHUNK_SIZE_MB: {size_mb}MB")
            return 'size', size_mb
        except ValueError:
            logger.warning(f"Invalid CHUNK_SIZE_MB format: {legacy_size}")
            return 'size', 20.0  # Ultimate fallback
    
    def calculate_optimal_chunking(self, converted_size: float, total_duration: float, connector_specs=None) -> Tuple[int, float]:
        """
        Calculate optimal number of chunks and chunk duration based on the configured limit.

        Args:
            converted_size: Size of the converted audio file in bytes
            total_duration: Total duration of the audio file in seconds
            connector_specs: Optional ConnectorSpecifications with hard limits

        Returns:
            Tuple of (num_chunks, chunk_duration_seconds)
        """
        try:
            # Use effective chunking config which respects connector hard limits
            chunking_config = get_effective_chunking_config(connector_specs)
            mode = chunking_config.mode
            limit_value = chunking_config.limit_value

            logger.info(f"Chunking config: mode={mode}, limit={limit_value}, source={chunking_config.source}")
            
            if mode == 'size':
                # Size-based chunking
                max_size_bytes = limit_value * 1024 * 1024 * 0.95  # 95% safety factor
                num_chunks = max(1, math.ceil(converted_size / max_size_bytes))
                
                logger.info(f"Size-based chunking: {limit_value}MB limit")
                logger.info(f"File size {converted_size/1024/1024:.1f}MB requires {num_chunks} chunks")
                
            else:  # duration-based
                # Duration-based chunking with API safety limit
                effective_limit = min(limit_value, 1400)  # Cap at OpenAI safe limit
                num_chunks = max(1, math.ceil(total_duration / effective_limit))
                
                logger.info(f"Duration-based chunking: {limit_value}s limit (effective: {effective_limit}s)")
                logger.info(f"File duration {total_duration:.1f}s requires {num_chunks} chunks")
            
            # Calculate chunk duration
            chunk_duration = total_duration / num_chunks
            
            # Apply minimum duration (5 minutes) but don't exceed file duration
            chunk_duration = min(max(300, chunk_duration), total_duration)
            
            # Log final chunking plan
            expected_chunk_size_mb = (converted_size / num_chunks) / (1024 * 1024)
            logger.info(f"Chunking plan: {num_chunks} chunks of ~{chunk_duration:.1f}s each (~{expected_chunk_size_mb:.1f}MB each)")
            
            return num_chunks, chunk_duration
            
        except Exception as e:
            logger.error(f"Error calculating optimal chunking: {e}")
            # Conservative fallback
            fallback_chunks = max(2, math.ceil(total_duration / 600))  # 10-minute chunks
            fallback_duration = total_duration / fallback_chunks
            return fallback_chunks, fallback_duration
    
    def create_chunks(self, file_path: str, temp_dir: str, connector_specs=None) -> List[Dict[str, Any]]:
        """
        Split audio file into overlapping chunks.

        First converts the file to MP3 format to get accurate size information,
        then calculates optimal chunk duration based on the actual MP3 file size.

        Args:
            file_path: Path to the source audio file
            temp_dir: Directory to store temporary chunk files
            connector_specs: Optional ConnectorSpecifications with hard limits

        Returns:
            List of chunk information dictionaries
        """
        chunks = []
        wav_path = None

        try:
            # Step 1: Convert to MP3 and get accurate size/duration info
            mp3_path, mp3_duration, mp3_size = self.convert_to_mp3_and_get_info(file_path, temp_dir)

            # Step 2: Calculate optimal chunking strategy (respects connector hard limits)
            num_chunks, chunk_duration = self.calculate_optimal_chunking(mp3_size, mp3_duration, connector_specs)
            
            # If only 1 chunk needed, no actual chunking required
            if num_chunks == 1:
                logger.info(f"File duration {mp3_duration:.1f}s is within limit - no chunking needed")
                # Return the single "chunk" as the whole file
                base_name = os.path.splitext(os.path.basename(file_path))[0]
                chunk_filename = f"{base_name}_chunk_000.mp3"
                chunk_path = os.path.join(temp_dir, chunk_filename)
                
                # Copy the converted file as the single chunk
                import shutil
                shutil.copy2(mp3_path, chunk_path)
                
                chunk_info = {
                    'index': 0,
                    'path': chunk_path,
                    'filename': chunk_filename,
                    'start_time': 0,
                    'end_time': mp3_duration,
                    'duration': mp3_duration,
                    'size_bytes': mp3_size,
                    'size_mb': mp3_size / (1024 * 1024)
                }
                chunks.append(chunk_info)
                logger.info(f"Created single chunk for entire file: {mp3_duration:.1f}s")
                return chunks
            
            # Calculate step size to create exactly num_chunks with overlap
            # Total coverage needed: mp3_duration + (overlap * (num_chunks - 1))
            # Each chunk covers: chunk_duration
            # Step between chunks to get exactly num_chunks
            if num_chunks > 1:
                step_duration = (mp3_duration - chunk_duration) / (num_chunks - 1)
            else:
                step_duration = mp3_duration
            
            current_start = 0
            chunk_index = 0
            
            logger.info(f"Splitting {file_path} into {num_chunks} chunks of ~{chunk_duration:.1f}s with {self.overlap_seconds}s overlap")
            
            for chunk_index in range(num_chunks):
                # Calculate start position for this chunk
                if chunk_index > 0:
                    current_start = chunk_index * step_duration
                
                # Calculate end time for this chunk
                chunk_end = min(current_start + chunk_duration, mp3_duration)
                actual_duration = chunk_end - current_start
                
                # Skip very short chunks at the end (shouldn't happen with proper calculation)
                if actual_duration < 10:  # Less than 10 seconds
                    logger.warning(f"Skipping short chunk {chunk_index}: {actual_duration:.1f}s")
                    break
                
                # Generate chunk filename
                base_name = os.path.splitext(os.path.basename(file_path))[0]
                chunk_filename = f"{base_name}_chunk_{chunk_index:03d}.mp3"
                chunk_path = os.path.join(temp_dir, chunk_filename)
                
                # Extract chunk from the converted MP3 file (more efficient than re-converting)
                cmd = [
                    'ffmpeg', '-i', mp3_path,
                    '-ss', str(current_start),
                    '-t', str(actual_duration),
                    '-acodec', 'copy',  # Copy codec since it's already in the right format
                    '-y',  # Overwrite output file
                    chunk_path
                ]
                
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    logger.error(f"ffmpeg failed for chunk {chunk_index}: {result.stderr}")
                    continue
                
                # Verify chunk was created and get its size
                if os.path.exists(chunk_path):
                    chunk_size = os.path.getsize(chunk_path)
                    
                    # Verify chunk size is within limits
                    if chunk_size > self.max_chunk_size_bytes:
                        logger.warning(f"Chunk {chunk_index} is {chunk_size/1024/1024:.1f}MB, exceeds {self.max_chunk_size_mb}MB limit")
                    
                    chunk_info = {
                        'index': chunk_index,
                        'path': chunk_path,
                        'filename': chunk_filename,
                        'start_time': current_start,
                        'end_time': chunk_end,
                        'duration': actual_duration,
                        'size_bytes': chunk_size,
                        'size_mb': chunk_size / (1024 * 1024)
                    }
                    
                    chunks.append(chunk_info)
                    logger.info(f"Created chunk {chunk_index}: {current_start:.1f}s-{chunk_end:.1f}s ({chunk_size/1024/1024:.1f}MB)")
                    
                    # Optionally preserve chunks for debugging (set PRESERVE_CHUNK_DEBUG=true in env)
                    if os.getenv('PRESERVE_CHUNK_DEBUG', 'false').lower() == 'true':
                        import shutil
                        # Save debug chunks in /data/uploads/debug/ directory
                        debug_dir = '/data/uploads/debug'
                        os.makedirs(debug_dir, exist_ok=True)
                        debug_filename = os.path.basename(chunk_path).replace('.mp3', '_debug.mp3')
                        debug_path = os.path.join(debug_dir, debug_filename)
                        shutil.copy2(chunk_path, debug_path)
                        logger.info(f"Debug: Preserved chunk as {debug_path}")
                else:
                    logger.error(f"Chunk file not created: {chunk_path}")
            
            logger.info(f"Created {len(chunks)} chunks for {file_path}")
            return chunks
            
        except Exception as e:
            logger.error(f"Error creating chunks for {file_path}: {e}")
            # Clean up any partial chunks
            for chunk in chunks:
                try:
                    if os.path.exists(chunk['path']):
                        os.remove(chunk['path'])
                except Exception:
                    pass
            raise
        finally:
            # Clean up the temporary WAV file
            if wav_path and os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                    logger.debug(f"Cleaned up temporary WAV file: {wav_path}")
                except Exception as e:
                    logger.warning(f"Error cleaning up temporary WAV file: {e}")
    
    def merge_transcriptions(self, chunk_results: List[Dict[str, Any]]) -> str:
        """
        Merge transcription results from multiple chunks, handling overlaps.
        
        Args:
            chunk_results: List of transcription results from chunks
            
        Returns:
            Merged transcription text
        """
        if not chunk_results:
            return ""
        
        if len(chunk_results) == 1:
            return chunk_results[0].get('transcription', '')
        
        # Sort chunks by start time to ensure correct order
        sorted_chunks = sorted(chunk_results, key=lambda x: x.get('start_time', 0))
        
        merged_text = ""
        
        for i, chunk in enumerate(sorted_chunks):
            chunk_text = chunk.get('transcription', '').strip()
            
            if not chunk_text:
                continue
            
            if i == 0:
                # First chunk: use entire transcription
                merged_text = chunk_text
            else:
                # Subsequent chunks: try to handle overlap
                merged_text = self._merge_overlapping_text(
                    merged_text, 
                    chunk_text, 
                    chunk.get('start_time', 0),
                    sorted_chunks[i-1].get('end_time', 0)
                )
        
        return merged_text
    
    def _merge_overlapping_text(self, existing_text: str, new_text: str, 
                               new_start_time: float, prev_end_time: float) -> str:
        """
        Merge overlapping transcription text, attempting to remove duplicates.
        
        Args:
            existing_text: Previously merged text
            new_text: New chunk text to merge
            new_start_time: Start time of new chunk
            prev_end_time: End time of previous chunk
            
        Returns:
            Merged text with overlaps handled
        """
        # If there's no overlap, just concatenate
        overlap_duration = prev_end_time - new_start_time
        if overlap_duration <= 0:
            return f"{existing_text}\n{new_text}"
        
        # For overlapping chunks, try to find common text and merge intelligently
        # This is a simplified approach - in practice, you might want more sophisticated
        # text similarity matching
        
        # Split texts into sentences/phrases
        existing_sentences = self._split_into_sentences(existing_text)
        new_sentences = self._split_into_sentences(new_text)
        
        if not existing_sentences or not new_sentences:
            return f"{existing_text}\n{new_text}"
        
        # Try to find overlap by comparing last few sentences of existing text
        # with first few sentences of new text
        overlap_found = False
        merge_point = len(existing_sentences)
        
        # Look for common sentences (simple approach)
        for i in range(min(3, len(existing_sentences))):  # Check last 3 sentences
            last_sentence = existing_sentences[-(i+1)].strip().lower()
            
            for j in range(min(3, len(new_sentences))):  # Check first 3 sentences
                first_sentence = new_sentences[j].strip().lower()
                
                # If sentences are similar enough, consider it an overlap
                if last_sentence and first_sentence and self._sentences_similar(last_sentence, first_sentence):
                    merge_point = len(existing_sentences) - i
                    new_start_index = j + 1
                    overlap_found = True
                    break
            
            if overlap_found:
                break
        
        if overlap_found:
            # Merge at the found overlap point
            merged_sentences = existing_sentences[:merge_point] + new_sentences[new_start_index:]
            return ' '.join(merged_sentences)
        else:
            # No clear overlap found, concatenate with a separator
            return f"{existing_text}\n{new_text}"
    
    def _split_into_sentences(self, text: str) -> List[str]:
        """Split text into sentences for overlap detection."""
        import re
        # Simple sentence splitting - could be improved with more sophisticated NLP
        sentences = re.split(r'[.!?]+', text)
        return [s.strip() for s in sentences if s.strip()]
    
    def _sentences_similar(self, sent1: str, sent2: str, threshold: float = 0.8) -> bool:
        """Check if two sentences are similar enough to be considered the same."""
        # Simple similarity check based on common words
        words1 = set(sent1.split())
        words2 = set(sent2.split())
        
        if not words1 or not words2:
            return False
        
        intersection = len(words1.intersection(words2))
        union = len(words1.union(words2))
        
        similarity = intersection / union if union > 0 else 0
        return similarity >= threshold
    
    def analyze_chunk_audio_properties(self, chunk_path: str) -> Dict[str, Any]:
        """
        Analyze audio properties of a chunk that might affect processing time.
        
        Args:
            chunk_path: Path to the chunk file
            
        Returns:
            Dictionary with audio analysis results
        """
        try:
            # Get detailed audio information using ffprobe
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', '-show_streams', chunk_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            probe_data = json.loads(result.stdout)
            
            audio_stream = None
            for stream in probe_data.get('streams', []):
                if stream.get('codec_type') == 'audio':
                    audio_stream = stream
                    break
            
            if not audio_stream:
                return {'error': 'No audio stream found'}
            
            format_info = probe_data.get('format', {})
            
            analysis = {
                'duration': float(format_info.get('duration', 0)),
                'size_bytes': int(format_info.get('size', 0)),
                'bitrate': int(format_info.get('bit_rate', 0)),
                'sample_rate': int(audio_stream.get('sample_rate', 0)),
                'channels': int(audio_stream.get('channels', 0)),
                'codec': audio_stream.get('codec_name', 'unknown'),
                'bits_per_sample': int(audio_stream.get('bits_per_raw_sample', 0)),
            }
            
            # Calculate some derived metrics
            if analysis['duration'] > 0:
                analysis['effective_bitrate'] = (analysis['size_bytes'] * 8) / analysis['duration']
                analysis['compression_ratio'] = analysis['bitrate'] / analysis['effective_bitrate'] if analysis['effective_bitrate'] > 0 else 0
            
            return analysis
            
        except Exception as e:
            logger.warning(f"Error analyzing chunk audio properties: {e}")
            return {'error': str(e)}
    
    def log_processing_statistics(self, chunk_results: List[Dict[str, Any]]) -> None:
        """
        Log detailed statistics about chunk processing performance.
        
        Args:
            chunk_results: List of chunk processing results with timing info
        """
        if not chunk_results:
            return
        
        logger.info("=== CHUNK PROCESSING STATISTICS ===")
        
        total_chunks = len(chunk_results)
        processing_times = []
        sizes = []
        durations = []
        
        for i, result in enumerate(chunk_results):
            processing_time = result.get('processing_time', 0)
            chunk_size = result.get('size_mb', 0)
            chunk_duration = result.get('duration', 0)
            
            processing_times.append(processing_time)
            sizes.append(chunk_size)
            durations.append(chunk_duration)
            
            # Log individual chunk stats
            rate = chunk_duration / processing_time if processing_time > 0 else 0
            logger.info(f"Chunk {i+1}: {processing_time:.1f}s processing, {chunk_size:.1f}MB, {chunk_duration:.1f}s audio (rate: {rate:.2f}x)")
        
        # Calculate summary statistics
        if processing_times:
            avg_time = sum(processing_times) / len(processing_times)
            min_time = min(processing_times)
            max_time = max(processing_times)
            
            avg_size = sum(sizes) / len(sizes)
            avg_duration = sum(durations) / len(durations)
            
            total_audio_time = sum(durations)
            total_processing_time = sum(processing_times)
            overall_rate = total_audio_time / total_processing_time if total_processing_time > 0 else 0
            
            logger.info(f"Summary: {total_chunks} chunks, {total_audio_time:.1f}s audio in {total_processing_time:.1f}s")
            logger.info(f"Average: {avg_time:.1f}s processing, {avg_size:.1f}MB, {avg_duration:.1f}s audio")
            logger.info(f"Range: {min_time:.1f}s - {max_time:.1f}s processing time")
            logger.info(f"Overall rate: {overall_rate:.2f}x realtime")
            
            # Identify performance outliers
            if max_time > avg_time * 2:
                slow_chunks = [i for i, t in enumerate(processing_times) if t > avg_time * 1.5]
                logger.warning(f"Performance outliers detected: chunks {[i+1 for i in slow_chunks]} took significantly longer")
                
                # Suggest possible causes
                logger.info("Possible causes for slow processing:")
                logger.info("- OpenAI API server load/performance variations")
                logger.info("- Network latency or connection issues")
                logger.info("- Audio content complexity (silence, noise, multiple speakers)")
                logger.info("- Temporary API rate limiting or throttling")
        
        logger.info("=== END STATISTICS ===")
    
    def get_performance_recommendations(self, chunk_results: List[Dict[str, Any]]) -> List[str]:
        """
        Generate performance recommendations based on processing results.
        
        Args:
            chunk_results: List of chunk processing results
            
        Returns:
            List of recommendation strings
        """
        recommendations = []
        
        if not chunk_results:
            return recommendations
        
        processing_times = [r.get('processing_time', 0) for r in chunk_results]
        
        if processing_times:
            avg_time = sum(processing_times) / len(processing_times)
            max_time = max(processing_times)
            
            # Check for high variance in processing times
            if max_time > avg_time * 3:
                recommendations.append("High variance in processing times detected. Consider implementing retry logic with exponential backoff.")
            
            # Check for overall slow processing
            total_audio = sum(r.get('duration', 0) for r in chunk_results)
            total_processing = sum(processing_times)
            rate = total_audio / total_processing if total_processing > 0 else 0
            
            if rate < 0.5:  # Less than 0.5x realtime
                recommendations.append("Overall processing is slow. Consider using smaller chunks or a different transcription service.")
            
            # Check for timeout issues
            if any(t > 300 for t in processing_times):  # 5+ minutes
                recommendations.append("Some chunks took over 5 minutes. Consider implementing timeout handling and chunk retry logic.")
            
            # Check chunk size optimization
            avg_size = sum(r.get('size_mb', 0) for r in chunk_results) / len(chunk_results)
            if avg_size < 10:
                recommendations.append("Chunks are relatively small. Consider increasing chunk size for better efficiency.")
            elif avg_size > 22:
                recommendations.append("Chunks are close to size limit. Consider reducing chunk size for more reliable processing.")
        
        return recommendations
    
    def cleanup_chunks(self, chunks: List[Dict[str, Any]], temp_mp3_path: str = None) -> None:
        """
        Clean up temporary chunk files and MP3 file.
        
        Args:
            chunks: List of chunk information dictionaries
            temp_mp3_path: Optional path to temporary MP3 file to clean up
        """
        for chunk in chunks:
            try:
                chunk_path = chunk.get('path')
                if chunk_path and os.path.exists(chunk_path):
                    os.remove(chunk_path)
                    logger.debug(f"Cleaned up chunk file: {chunk_path}")
            except Exception as e:
                logger.warning(f"Error cleaning up chunk {chunk.get('filename', 'unknown')}: {e}")
        
        # Clean up temporary MP3 file if provided
        if temp_mp3_path and os.path.exists(temp_mp3_path):
            try:
                os.remove(temp_mp3_path)
                logger.debug(f"Cleaned up temporary MP3 file: {temp_mp3_path}")
            except Exception as e:
                logger.warning(f"Error cleaning up temporary MP3 file: {e}")

def get_audio_duration_ffprobe(file_path: str) -> Optional[float]:
    """Get actual audio duration using ffprobe."""
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', file_path
        ], capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception:
        return None


def extract_speaker_samples(
    audio_path: str,
    segments: List[Dict[str, Any]],
    output_dir: str,
    min_duration: float = 1.5,  # OpenAI minimum is 1.2s, use 1.5s for safety
    max_duration: float = 9.0,  # OpenAI maximum is 10.0s, use 9.0s for safety
    max_speakers: int = 4
) -> Dict[str, str]:
    """
    Extract audio samples for each unique speaker from diarized segments.

    This is used to maintain speaker identity across chunks when processing
    long audio files with the gpt-4o-transcribe-diarize model.

    Args:
        audio_path: Path to the source audio file (should be the converted chunk MP3)
        segments: List of diarized segments with speaker, start_time, end_time
        output_dir: Directory to store extracted speaker samples
        min_duration: Minimum duration for a speaker sample (OpenAI requires 1.2-10s)
        max_duration: Maximum duration for a speaker sample
        max_speakers: Maximum number of speakers to extract (OpenAI supports up to 4)

    Returns:
        Dict mapping speaker label (e.g., "A", "B") to path of extracted audio sample
    """
    # OpenAI's actual limits
    OPENAI_MIN_DURATION = 1.2
    OPENAI_MAX_DURATION = 10.0

    # Group segments by speaker
    speaker_segments: Dict[str, List[Dict]] = {}
    for seg in segments:
        # Handle both dict and object segments
        if isinstance(seg, dict):
            speaker = seg.get('speaker', 'Unknown')
            start = seg.get('start_time') or seg.get('start')
            end = seg.get('end_time') or seg.get('end')
        else:
            speaker = getattr(seg, 'speaker', 'Unknown')
            start = getattr(seg, 'start_time', None) or getattr(seg, 'start', None)
            end = getattr(seg, 'end_time', None) or getattr(seg, 'end', None)

        if speaker == 'Unknown' or start is None or end is None:
            continue

        if speaker not in speaker_segments:
            speaker_segments[speaker] = []
        speaker_segments[speaker].append({'start': start, 'end': end})

    if not speaker_segments:
        logger.warning("No valid speaker segments found for sample extraction")
        return {}

    # Sort speakers to get consistent ordering (A, B, C, D...)
    sorted_speakers = sorted(speaker_segments.keys())[:max_speakers]
    logger.info(f"Extracting samples for {len(sorted_speakers)} speakers: {sorted_speakers}")

    speaker_samples = {}

    for speaker in sorted_speakers:
        segs = speaker_segments[speaker]

        # Find the best segment for this speaker (ideally 1.5-9 seconds)
        best_segment = None
        best_duration = 0

        for seg in segs:
            duration = seg['end'] - seg['start']

            # Prefer segments in the ideal range
            if min_duration <= duration <= max_duration:
                if duration > best_duration:
                    best_segment = seg
                    best_duration = duration

        # If no segment in ideal range, try to find one we can trim
        if not best_segment:
            for seg in segs:
                duration = seg['end'] - seg['start']
                if duration >= min_duration:
                    # Trim to max_duration if needed
                    best_segment = {
                        'start': seg['start'],
                        'end': min(seg['end'], seg['start'] + max_duration)
                    }
                    best_duration = best_segment['end'] - best_segment['start']
                    break

        # Still no segment? Try combining multiple short segments
        if not best_segment and len(segs) > 1:
            # Sort by start time and try to find consecutive segments
            sorted_segs = sorted(segs, key=lambda x: x['start'])
            combined_start = sorted_segs[0]['start']
            combined_end = sorted_segs[0]['end']

            for i in range(1, len(sorted_segs)):
                # If segments are close (within 1 second), combine them
                if sorted_segs[i]['start'] - combined_end < 1.0:
                    combined_end = sorted_segs[i]['end']
                    if combined_end - combined_start >= min_duration:
                        break

            combined_duration = combined_end - combined_start
            if combined_duration >= min_duration:
                best_segment = {
                    'start': combined_start,
                    'end': min(combined_end, combined_start + max_duration)
                }
                best_duration = best_segment['end'] - best_segment['start']

        if not best_segment:
            logger.warning(f"Could not find suitable segment for speaker {speaker}")
            continue

        # Extract the audio sample using ffmpeg
        sample_filename = f"speaker_{speaker}_sample.mp3"
        sample_path = os.path.join(output_dir, sample_filename)

        try:
            cmd = [
                'ffmpeg', '-i', audio_path,
                '-ss', str(best_segment['start']),
                '-t', str(best_duration),
                '-acodec', 'libmp3lame',
                '-b:a', '128k',
                '-y',
                sample_path
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"Failed to extract sample for speaker {speaker}: {result.stderr}")
                continue

            if os.path.exists(sample_path) and os.path.getsize(sample_path) > 0:
                # Verify actual duration meets OpenAI requirements
                actual_duration = get_audio_duration_ffprobe(sample_path)
                if actual_duration:
                    logger.info(f"Speaker {speaker} sample: expected {best_duration:.2f}s, actual {actual_duration:.2f}s")

                    if actual_duration < OPENAI_MIN_DURATION:
                        logger.warning(f"Sample for speaker {speaker} too short ({actual_duration:.2f}s < {OPENAI_MIN_DURATION}s), skipping")
                        os.remove(sample_path)
                        continue
                    elif actual_duration > OPENAI_MAX_DURATION:
                        logger.warning(f"Sample for speaker {speaker} too long ({actual_duration:.2f}s > {OPENAI_MAX_DURATION}s), skipping")
                        os.remove(sample_path)
                        continue

                speaker_samples[speaker] = sample_path
                logger.info(f"Extracted {actual_duration:.1f}s sample for speaker {speaker} "
                           f"(from {best_segment['start']:.1f}s to {best_segment['end']:.1f}s)")
            else:
                logger.warning(f"Sample file not created for speaker {speaker}")

        except Exception as e:
            logger.error(f"Error extracting sample for speaker {speaker}: {e}")

    return speaker_samples


def samples_to_data_urls(speaker_samples: Dict[str, str]) -> Dict[str, str]:
    """
    Convert speaker sample file paths to base64-encoded data URLs.

    OpenAI's known_speaker_references requires audio samples as data URLs
    when using multipart form data.

    Args:
        speaker_samples: Dict mapping speaker label to file path

    Returns:
        Dict mapping speaker label to data URL
    """
    import base64

    data_urls = {}

    for speaker, path in speaker_samples.items():
        try:
            with open(path, 'rb') as f:
                audio_data = f.read()

            # Encode as base64 data URL
            b64_data = base64.b64encode(audio_data).decode('utf-8')
            data_url = f"data:audio/mpeg;base64,{b64_data}"
            data_urls[speaker] = data_url

            logger.debug(f"Converted speaker {speaker} sample to data URL ({len(b64_data)} bytes)")

        except Exception as e:
            logger.error(f"Error converting speaker {speaker} sample to data URL: {e}")

    return data_urls


class ChunkProcessingError(Exception):
    """Exception raised when chunk processing fails."""
    pass

class ChunkingNotSupportedError(Exception):
    """Exception raised when chunking is not supported for the current configuration."""
    pass
