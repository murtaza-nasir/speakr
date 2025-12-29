#!/usr/bin/env python3
"""
Test script for ffprobe codec detection functionality.

This script tests the new codec-based detection system to ensure it correctly
identifies audio codecs, video files, and lossless formats.
"""

import os
import sys
import tempfile
import subprocess
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.ffprobe import (
    get_codec_info,
    is_video_file,
    is_audio_file,
    get_audio_codec,
    needs_audio_conversion,
    is_lossless_audio,
    get_duration,
    FFProbeError
)


def create_test_audio_file(codec, output_path, duration=1.0):
    """Create a test audio file with specific codec."""
    codec_map = {
        'mp3': ['ffmpeg', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}', '-acodec', 'libmp3lame', '-b:a', '128k', output_path],
        'aac': ['ffmpeg', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}', '-acodec', 'aac', '-b:a', '128k', output_path],
        'opus': ['ffmpeg', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}', '-acodec', 'libopus', '-b:a', '64k', output_path],
        'flac': ['ffmpeg', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}', '-acodec', 'flac', output_path],
        'pcm_s16le': ['ffmpeg', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}', '-acodec', 'pcm_s16le', '-ar', '44100', output_path],
        'vorbis': ['ffmpeg', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}', '-acodec', 'libvorbis', '-b:a', '128k', output_path],
    }
    
    if codec not in codec_map:
        raise ValueError(f"Unknown codec: {codec}")
    
    subprocess.run(codec_map[codec], check=True, capture_output=True)


def create_test_video_file(output_path, duration=1.0):
    """Create a test video file with audio."""
    subprocess.run([
        'ffmpeg', '-f', 'lavfi', '-i', f'testsrc=duration={duration}:size=320x240:rate=1',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}',
        '-acodec', 'aac', '-vcodec', 'libx264', '-pix_fmt', 'yuv420p',
        output_path
    ], check=True, capture_output=True)


def test_codec_detection():
    """Test basic codec detection."""
    print("\n=== Testing Codec Detection ===\n")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        test_files = {
            'mp3': 'test.mp3',
            'aac': 'test.m4a',
            'opus': 'test.opus',
            'flac': 'test.flac',
            'pcm_s16le': 'test.wav',
            'vorbis': 'test.ogg',
        }
        
        for codec, filename in test_files.items():
            filepath = os.path.join(tmpdir, filename)
            try:
                print(f"Creating test file: {filename} with codec {codec}...")
                create_test_audio_file(codec, filepath)
                
                print(f"  Probing {filename}...")
                codec_info = get_codec_info(filepath)
                
                detected_codec = codec_info['audio_codec']
                print(f"  ✓ Detected codec: {detected_codec}")
                print(f"    Has audio: {codec_info['has_audio']}")
                print(f"    Has video: {codec_info['has_video']}")
                print(f"    Format: {codec_info['format_name']}")
                print(f"    Duration: {codec_info['duration']:.2f}s" if codec_info['duration'] else "    Duration: N/A")
                
                if detected_codec != codec:
                    print(f"  ⚠️  Warning: Expected {codec}, got {detected_codec}")
                
                print()
                
            except Exception as e:
                print(f"  ✗ Failed to test {codec}: {e}\n")


def test_video_detection():
    """Test video file detection."""
    print("\n=== Testing Video Detection ===\n")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, 'test_video.mp4')
        audio_path = os.path.join(tmpdir, 'test_audio.mp3')
        
        try:
            print("Creating test video file...")
            create_test_video_file(video_path)
            
            print("Creating test audio file...")
            create_test_audio_file('mp3', audio_path)
            
            print(f"\nProbing video file...")
            codec_info = get_codec_info(video_path)
            print(f"  Audio codec: {codec_info['audio_codec']}")
            print(f"  Video codec: {codec_info['video_codec']}")
            print(f"  Has audio: {codec_info['has_audio']}")
            print(f"  Has video: {codec_info['has_video']}")
            
            is_video = is_video_file(video_path)
            print(f"  is_video_file(): {is_video}")
            
            if not is_video:
                print("  ✗ Video file not detected as video!")
            else:
                print("  ✓ Video file correctly detected")
            
            print(f"\nProbing audio file...")
            codec_info = get_codec_info(audio_path)
            print(f"  Audio codec: {codec_info['audio_codec']}")
            print(f"  Video codec: {codec_info['video_codec']}")
            print(f"  Has audio: {codec_info['has_audio']}")
            print(f"  Has video: {codec_info['has_video']}")
            
            is_video = is_video_file(audio_path)
            print(f"  is_video_file(): {is_video}")
            
            if is_video:
                print("  ✗ Audio file incorrectly detected as video!")
            else:
                print("  ✓ Audio file correctly identified as audio-only")
            
            print()
            
        except Exception as e:
            print(f"✗ Failed to test video detection: {e}\n")


def test_lossless_detection():
    """Test lossless audio detection."""
    print("\n=== Testing Lossless Detection ===\n")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        test_cases = {
            'pcm_s16le': ('test.wav', True),
            'flac': ('test.flac', True),
            'mp3': ('test.mp3', False),
            'aac': ('test.m4a', False),
            'opus': ('test.opus', False),
        }
        
        for codec, (filename, expected_lossless) in test_cases.items():
            filepath = os.path.join(tmpdir, filename)
            try:
                print(f"Creating {filename} with codec {codec}...")
                create_test_audio_file(codec, filepath)
                
                is_lossless = is_lossless_audio(filepath)
                status = "✓" if is_lossless == expected_lossless else "✗"
                
                print(f"  {status} {codec}: is_lossless={is_lossless} (expected {expected_lossless})")
                
            except Exception as e:
                print(f"  ✗ Failed to test {codec}: {e}")
        
        print()


def test_conversion_check():
    """Test conversion requirement detection."""
    print("\n=== Testing Conversion Check ===\n")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Supported codecs for direct transcription
        supported_codecs = ['pcm_s16le', 'mp3', 'flac', 'opus', 'aac']
        
        test_cases = {
            'mp3': ('test.mp3', False),  # Supported, no conversion needed
            'aac': ('test.m4a', False),  # Supported, no conversion needed
            'opus': ('test.opus', False),  # Supported, no conversion needed
            'vorbis': ('test.ogg', True),  # Not in supported list, needs conversion
        }
        
        for codec, (filename, should_convert) in test_cases.items():
            filepath = os.path.join(tmpdir, filename)
            try:
                print(f"Creating {filename} with codec {codec}...")
                create_test_audio_file(codec, filepath)
                
                needs_conversion, detected_codec = needs_audio_conversion(filepath, supported_codecs)
                status = "✓" if needs_conversion == should_convert else "✗"
                
                print(f"  {status} {codec}: needs_conversion={needs_conversion} (expected {should_convert})")
                print(f"     Detected codec: {detected_codec}")
                
            except Exception as e:
                print(f"  ✗ Failed to test {codec}: {e}")
        
        print()


def test_misnamed_file():
    """Test detection of files with wrong extensions."""
    print("\n=== Testing Misnamed File Detection ===\n")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create an MP3 file but name it .wav
        wrong_name_path = os.path.join(tmpdir, 'actually_mp3.wav')
        
        try:
            print("Creating MP3 file with .wav extension...")
            create_test_audio_file('mp3', wrong_name_path)
            
            codec_info = get_codec_info(wrong_name_path)
            detected_codec = codec_info['audio_codec']
            
            print(f"  Filename: actually_mp3.wav")
            print(f"  Detected codec: {detected_codec}")
            
            if detected_codec == 'mp3':
                print("  ✓ Correctly detected MP3 codec despite .wav extension")
            else:
                print(f"  ✗ Incorrectly detected as {detected_codec}")
            
            # Create a FLAC file but name it .mp3
            wrong_name_path2 = os.path.join(tmpdir, 'actually_flac.mp3')
            print("\nCreating FLAC file with .mp3 extension...")
            create_test_audio_file('flac', wrong_name_path2)
            
            codec_info = get_codec_info(wrong_name_path2)
            detected_codec = codec_info['audio_codec']
            
            print(f"  Filename: actually_flac.mp3")
            print(f"  Detected codec: {detected_codec}")
            
            if detected_codec == 'flac':
                print("  ✓ Correctly detected FLAC codec despite .mp3 extension")
            else:
                print(f"  ✗ Incorrectly detected as {detected_codec}")
            
            print()
            
        except Exception as e:
            print(f"✗ Failed to test misnamed files: {e}\n")


def test_duration():
    """Test duration extraction."""
    print("\n=== Testing Duration Extraction ===\n")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        durations = [1.0, 2.5, 5.0]
        
        for expected_duration in durations:
            filepath = os.path.join(tmpdir, f'test_{expected_duration}s.mp3')
            try:
                print(f"Creating {expected_duration}s audio file...")
                create_test_audio_file('mp3', filepath, duration=expected_duration)
                
                detected_duration = get_duration(filepath)
                
                # Allow 0.1s tolerance for encoding variations
                if detected_duration and abs(detected_duration - expected_duration) < 0.1:
                    print(f"  ✓ Duration: {detected_duration:.2f}s (expected {expected_duration}s)")
                else:
                    print(f"  ✗ Duration: {detected_duration:.2f}s (expected {expected_duration}s)")
                
            except Exception as e:
                print(f"  ✗ Failed to test duration: {e}")
        
        print()


def main():
    """Run all tests."""
    print("=" * 60)
    print("FFProbe Codec Detection Test Suite")
    print("=" * 60)
    
    # Check if ffmpeg/ffprobe are available
    try:
        subprocess.run(['ffprobe', '-version'], capture_output=True, check=True)
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("\n✗ Error: ffmpeg/ffprobe not found. Please install ffmpeg to run tests.\n")
        return 1
    
    try:
        test_codec_detection()
        test_video_detection()
        test_lossless_detection()
        test_conversion_check()
        test_misnamed_file()
        test_duration()
        
        print("=" * 60)
        print("All tests completed!")
        print("=" * 60)
        print()
        
        return 0
        
    except Exception as e:
        print(f"\n✗ Test suite failed with error: {e}\n")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())