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
            # Use 25MB limit for OpenAI Whisper API
            return file_size > (25 * 1024 * 1024)
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
    
    def calculate_chunk_duration(self, file_path: str) -> Optional[float]:
        """
        Calculate optimal chunk duration based on file size and bitrate.
        
        Args:
            file_path: Path to the audio file
            
        Returns:
            Chunk duration in seconds, or None if unable to calculate
        """
        try:
            file_size = os.path.getsize(file_path)
            duration = self.get_audio_duration(file_path)
            
            if not duration:
                # Fallback: assume reasonable bitrate for chunk calculation
                # For 16kHz mono PCM: ~32KB/s, for MP3: ~128kbps = ~16KB/s
                estimated_bitrate_bytes_per_sec = 32000  # Conservative estimate
                chunk_duration = (self.max_chunk_size_bytes / estimated_bitrate_bytes_per_sec) - self.overlap_seconds
                return max(60, chunk_duration)  # Minimum 60 seconds per chunk
            
            # Calculate actual bitrate
            bitrate_bytes_per_sec = file_size / duration
            
            # Calculate chunk duration to stay under size limit
            chunk_duration = (self.max_chunk_size_bytes / bitrate_bytes_per_sec) - self.overlap_seconds
            
            # Ensure reasonable chunk size (minimum 60 seconds, maximum 30 minutes)
            chunk_duration = max(60, min(1800, chunk_duration))
            
            logger.info(f"Calculated chunk duration: {chunk_duration:.1f}s for file {file_path}")
            return chunk_duration
            
        except Exception as e:
            logger.error(f"Error calculating chunk duration for {file_path}: {e}")
            return 300  # Default 5 minutes
    
    def create_chunks(self, file_path: str, temp_dir: str) -> List[Dict[str, Any]]:
        """
        Split audio file into overlapping chunks.
        
        Args:
            file_path: Path to the source audio file
            temp_dir: Directory to store temporary chunk files
            
        Returns:
            List of chunk information dictionaries
        """
        chunks = []
        
        try:
            duration = self.get_audio_duration(file_path)
            if not duration:
                raise ValueError("Could not determine audio duration")
            
            chunk_duration = self.calculate_chunk_duration(file_path)
            if not chunk_duration:
                raise ValueError("Could not calculate chunk duration")
            
            step_duration = chunk_duration - self.overlap_seconds
            current_start = 0
            chunk_index = 0
            
            logger.info(f"Splitting {file_path} into chunks of {chunk_duration}s with {self.overlap_seconds}s overlap")
            
            while current_start < duration:
                # Calculate end time for this chunk
                chunk_end = min(current_start + chunk_duration, duration)
                actual_duration = chunk_end - current_start
                
                # Skip very short chunks at the end
                if actual_duration < 10:  # Less than 10 seconds
                    break
                
                # Generate chunk filename
                base_name = os.path.splitext(os.path.basename(file_path))[0]
                chunk_filename = f"{base_name}_chunk_{chunk_index:03d}.wav"
                chunk_path = os.path.join(temp_dir, chunk_filename)
                
                # Extract chunk using ffmpeg
                cmd = [
                    'ffmpeg', '-i', file_path,
                    '-ss', str(current_start),
                    '-t', str(actual_duration),
                    '-acodec', 'pcm_s16le',
                    '-ar', '16000',
                    '-ac', '1',
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
    
    def cleanup_chunks(self, chunks: List[Dict[str, Any]]) -> None:
        """
        Clean up temporary chunk files.
        
        Args:
            chunks: List of chunk information dictionaries
        """
        for chunk in chunks:
            try:
                chunk_path = chunk.get('path')
                if chunk_path and os.path.exists(chunk_path):
                    os.remove(chunk_path)
                    logger.debug(f"Cleaned up chunk file: {chunk_path}")
            except Exception as e:
                logger.warning(f"Error cleaning up chunk {chunk.get('filename', 'unknown')}: {e}")

class ChunkProcessingError(Exception):
    """Exception raised when chunk processing fails."""
    pass

class ChunkingNotSupportedError(Exception):
    """Exception raised when chunking is not supported for the current configuration."""
    pass
