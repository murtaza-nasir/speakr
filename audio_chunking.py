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
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import mimetypes

# Configure logging
logger = logging.getLogger(__name__)

class AudioChunkingService:
    """Service for chunking large audio files and processing them with OpenAI Whisper API."""
    
    def __init__(self, max_chunk_size_mb: int = 20, overlap_seconds: int = 3):
        """
        Initialize the chunking service.
        
        Args:
            max_chunk_size_mb: Maximum size for each chunk in MB (default 20MB for safety margin)
            overlap_seconds: Overlap between chunks in seconds for context continuity
        """
        self.max_chunk_size_mb = max_chunk_size_mb
        self.overlap_seconds = overlap_seconds
        self.max_chunk_size_bytes = max_chunk_size_mb * 1024 * 1024
        self.chunk_stats = []  # Track processing statistics
        
    def needs_chunking(self, file_path: str, use_asr_endpoint: bool = False) -> bool:
        """
        Check if a file needs to be chunked based on size and endpoint being used.
        
        Args:
            file_path: Path to the audio file
            use_asr_endpoint: Whether ASR endpoint is being used (no chunking needed)
            
        Returns:
            True if file needs chunking, False otherwise
        """
        if use_asr_endpoint:
            return False
            
        try:
            file_size = os.path.getsize(file_path)
            # Use configured chunk size limit from environment
            chunk_size_mb = float(os.environ.get('CHUNK_SIZE_MB', '20'))  # Default 20MB
            chunk_size_bytes = chunk_size_mb * 1024 * 1024
            return file_size > chunk_size_bytes
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
    
    def convert_to_wav_and_get_info(self, file_path: str, temp_dir: str) -> Tuple[str, float, float]:
        """
        Convert the input file to WAV format and get its size and duration info.
        
        Args:
            file_path: Path to the source audio file
            temp_dir: Directory to store the temporary WAV file
            
        Returns:
            Tuple of (wav_file_path, duration_seconds, size_bytes)
        """
        try:
            # Generate WAV filename
            base_name = os.path.splitext(os.path.basename(file_path))[0]
            wav_filename = f"{base_name}_converted.wav"
            wav_path = os.path.join(temp_dir, wav_filename)
            
            # Convert to 32kbps MP3 using the same settings we use for chunks
            cmd = [
                'ffmpeg', '-i', file_path,
                '-acodec', 'mp3',
                '-ab', '32k',
                '-ar', '16000',
                '-ac', '1',
                '-y',  # Overwrite output file
                wav_path
            ]
            
            logger.info(f"Converting {file_path} to 32kbps MP3 format for accurate chunking...")
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise ValueError(f"ffmpeg conversion failed: {result.stderr}")
            
            if not os.path.exists(wav_path):
                raise ValueError("WAV file was not created")
            
            # Get the size and duration of the converted WAV file
            wav_size = os.path.getsize(wav_path)
            wav_duration = self.get_audio_duration(wav_path)
            
            if not wav_duration:
                raise ValueError("Could not determine WAV file duration")
            
            logger.info(f"Converted WAV: {wav_size/1024/1024:.1f}MB, {wav_duration:.1f}s")
            return wav_path, wav_duration, wav_size
            
        except Exception as e:
            logger.error(f"Error converting file to WAV: {e}")
            raise

    def calculate_chunk_duration_from_wav(self, wav_size: float, wav_duration: float) -> float:
        """
        Calculate optimal chunk duration based on actual WAV file size.
        
        Args:
            wav_size: Size of the WAV file in bytes
            wav_duration: Duration of the WAV file in seconds
            
        Returns:
            Chunk duration in seconds
        """
        try:
            # Calculate actual bitrate from the WAV file
            bitrate_bytes_per_sec = wav_size / wav_duration
            
            # Use configured chunk size with improved safety factor
            chunk_size_mb = float(os.environ.get('CHUNK_SIZE_MB', '20'))  # Default 20MB
            safety_factor = 0.95  # Use 95% of max size for better target accuracy
            target_chunk_size = chunk_size_mb * 1024 * 1024 * safety_factor
            
            # Calculate duration based on target size, accounting for overlap
            chunk_duration = (target_chunk_size / bitrate_bytes_per_sec) - self.overlap_seconds
            
            # More flexible duration limits to allow reaching target chunk size
            # Minimum 5 minutes, but allow longer chunks if needed to reach target size
            min_duration = 300  # 5 minutes minimum
            max_duration = max(3600, chunk_duration * 1.1)  # At least 1 hour, or 110% of calculated duration
            
            chunk_duration = max(min_duration, min(max_duration, chunk_duration))
            
            # Calculate what the actual chunk size will be with this duration
            actual_chunk_size_mb = (bitrate_bytes_per_sec * (chunk_duration + self.overlap_seconds)) / (1024 * 1024)
            
            logger.info(f"Calculated chunk duration: {chunk_duration:.1f}s (target: {target_chunk_size/1024/1024:.1f}MB, estimated actual: {actual_chunk_size_mb:.1f}MB) based on bitrate {bitrate_bytes_per_sec:.0f} bytes/sec")
            return chunk_duration
            
        except Exception as e:
            logger.error(f"Error calculating chunk duration from WAV: {e}")
            return 600  # Default 10 minutes (increased from 5)
    
    def create_chunks(self, file_path: str, temp_dir: str) -> List[Dict[str, Any]]:
        """
        Split audio file into overlapping chunks.
        
        First converts the file to WAV format to get accurate size information,
        then calculates optimal chunk duration based on the actual WAV file size.
        
        Args:
            file_path: Path to the source audio file
            temp_dir: Directory to store temporary chunk files
            
        Returns:
            List of chunk information dictionaries
        """
        chunks = []
        wav_path = None
        
        try:
            # Step 1: Convert to WAV and get accurate size/duration info
            wav_path, wav_duration, wav_size = self.convert_to_wav_and_get_info(file_path, temp_dir)
            
            # Step 2: Calculate optimal chunk duration based on actual WAV file
            chunk_duration = self.calculate_chunk_duration_from_wav(wav_size, wav_duration)
            
            step_duration = chunk_duration - self.overlap_seconds
            current_start = 0
            chunk_index = 0
            
            logger.info(f"Splitting {file_path} into chunks of {chunk_duration}s with {self.overlap_seconds}s overlap")
            
            while current_start < wav_duration:
                # Calculate end time for this chunk
                chunk_end = min(current_start + chunk_duration, wav_duration)
                actual_duration = chunk_end - current_start
                
                # Skip very short chunks at the end
                if actual_duration < 10:  # Less than 10 seconds
                    break
                
                # Generate chunk filename
                base_name = os.path.splitext(os.path.basename(file_path))[0]
                chunk_filename = f"{base_name}_chunk_{chunk_index:03d}.wav"
                chunk_path = os.path.join(temp_dir, chunk_filename)
                
                # Extract chunk from the converted WAV file (more efficient than re-converting)
                cmd = [
                    'ffmpeg', '-i', wav_path,
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
                else:
                    logger.error(f"Chunk file not created: {chunk_path}")
                
                # Move to next chunk
                current_start += step_duration
                chunk_index += 1
            
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
    
    def cleanup_chunks(self, chunks: List[Dict[str, Any]], temp_wav_path: str = None) -> None:
        """
        Clean up temporary chunk files and WAV file.
        
        Args:
            chunks: List of chunk information dictionaries
            temp_wav_path: Optional path to temporary WAV file to clean up
        """
        for chunk in chunks:
            try:
                chunk_path = chunk.get('path')
                if chunk_path and os.path.exists(chunk_path):
                    os.remove(chunk_path)
                    logger.debug(f"Cleaned up chunk file: {chunk_path}")
            except Exception as e:
                logger.warning(f"Error cleaning up chunk {chunk.get('filename', 'unknown')}: {e}")
        
        # Clean up temporary WAV file if provided
        if temp_wav_path and os.path.exists(temp_wav_path):
            try:
                os.remove(temp_wav_path)
                logger.debug(f"Cleaned up temporary WAV file: {temp_wav_path}")
            except Exception as e:
                logger.warning(f"Error cleaning up temporary WAV file: {e}")

class ChunkProcessingError(Exception):
    """Exception raised when chunk processing fails."""
    pass

class ChunkingNotSupportedError(Exception):
    """Exception raised when chunking is not supported for the current configuration."""
    pass
