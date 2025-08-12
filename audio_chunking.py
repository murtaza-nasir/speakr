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
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import mimetypes

# Configure logging
logger = logging.getLogger(__name__)

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
        
    def needs_chunking(self, file_path: str, use_asr_endpoint: bool = False) -> bool:
        """
        Check if a file needs to be chunked based on size and endpoint being used.
        
        NOTE: For duration-based limits, this may return True even if chunking isn't needed,
        because we need to convert the file first to check duration. The actual chunking
        decision is made after conversion in calculate_optimal_chunking().
        
        Args:
            file_path: Path to the audio file
            use_asr_endpoint: Whether ASR endpoint is being used (no chunking needed)
            
        Returns:
            True if file might need chunking, False otherwise
        """
        if use_asr_endpoint:
            return False
            
        try:
            file_size = os.path.getsize(file_path)
            mode, limit_value = self.parse_chunk_limit()
            
            if mode == 'size':
                # For size-based limits, we can determine immediately
                chunk_size_bytes = limit_value * 1024 * 1024
                needs_it = file_size > chunk_size_bytes
                logger.info(f"Size check: {file_size/1024/1024:.1f}MB vs limit {limit_value}MB - needs chunking: {needs_it}")
                return needs_it
            else:
                # For duration-based limits, we need to check the actual duration
                # Try to get duration without conversion first (fast check)
                duration = self.get_audio_duration(file_path)
                if duration:
                    needs_it = duration > limit_value
                    logger.info(f"Duration check: {duration:.1f}s vs limit {limit_value}s - needs chunking: {needs_it}")
                    return needs_it
                else:
                    # Can't determine duration without conversion, assume might need chunking
                    logger.info(f"Duration-based limit set ({limit_value}s) but can't check duration yet - will check after conversion")
                    return True  # Proceed to conversion and check
        except OSError:
            logger.error(f"Could not get file size for {file_path}")
            return False
    
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
            
            # Convert to 64kbps MP3 using better quality settings
            cmd = [
                'ffmpeg', '-i', file_path,
                '-codec:a', 'libmp3lame',  # Use LAME MP3 encoder explicitly
                '-b:a', '64k',  # 64kbps bitrate for better quality
                '-ar', '22050',  # 22.05kHz sample rate (better than 16kHz)
                '-ac', '1',  # Mono (sufficient for speech)
                '-compression_level', '2',  # Better compression
                '-y',  # Overwrite output file
                mp3_path
            ]
            
            logger.info(f"Converting {file_path} to 64kbps MP3 format for accurate chunking...")
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise ValueError(f"ffmpeg conversion failed: {result.stderr}")
            
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
    
    def calculate_optimal_chunking(self, converted_size: float, total_duration: float) -> Tuple[int, float]:
        """
        Calculate optimal number of chunks and chunk duration based on the configured limit.
        
        Args:
            converted_size: Size of the converted audio file in bytes
            total_duration: Total duration of the audio file in seconds
            
        Returns:
            Tuple of (num_chunks, chunk_duration_seconds)
        """
        try:
            mode, limit_value = self.parse_chunk_limit()
            
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
    
    def create_chunks(self, file_path: str, temp_dir: str) -> List[Dict[str, Any]]:
        """
        Split audio file into overlapping chunks.
        
        First converts the file to MP3 format to get accurate size information,
        then calculates optimal chunk duration based on the actual MP3 file size.
        
        Args:
            file_path: Path to the source audio file
            temp_dir: Directory to store temporary chunk files
            
        Returns:
            List of chunk information dictionaries
        """
        chunks = []
        wav_path = None
        
        try:
            # Step 1: Convert to MP3 and get accurate size/duration info
            mp3_path, mp3_duration, mp3_size = self.convert_to_mp3_and_get_info(file_path, temp_dir)
            
            # Step 2: Calculate optimal chunking strategy
            num_chunks, chunk_duration = self.calculate_optimal_chunking(mp3_size, mp3_duration)
            
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

class ChunkProcessingError(Exception):
    """Exception raised when chunk processing fails."""
    pass

class ChunkingNotSupportedError(Exception):
    """Exception raised when chunking is not supported for the current configuration."""
    pass
