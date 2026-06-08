#!/usr/bin/env python3
"""
Test script for ffprobe codec detection functionality.

This script tests the codec-based detection system to ensure it correctly
identifies audio codecs, video files, and lossless formats. It generates real
media files with ffmpeg and probes them with the production ffprobe helpers in
src/utils/ffprobe.py — nothing is mocked. If ffmpeg/ffprobe are unavailable the
tests skip rather than passing vacuously.
"""

import os
import sys
import tempfile
import subprocess
from pathlib import Path

import pytest

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
    FFProbeError,
)


def _ffmpeg_available():
    """Return True if both ffmpeg and ffprobe are runnable."""
    try:
        subprocess.run(['ffprobe', '-version'], capture_output=True, check=True)
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# Skip the whole module when ffmpeg/ffprobe are missing instead of passing silently.
pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="ffmpeg/ffprobe not found; required for codec-detection tests",
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
    """The detected audio codec must match the codec the file was encoded with."""
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
            create_test_audio_file(codec, filepath)

            codec_info = get_codec_info(filepath)

            assert codec_info['has_audio'] is True, f"{filename}: audio stream not detected"
            assert codec_info['has_video'] is False, f"{filename}: unexpected video stream"
            assert codec_info['audio_codec'] == codec, (
                f"{filename}: expected codec {codec}, got {codec_info['audio_codec']}"
            )
            assert codec_info['duration'] is not None and codec_info['duration'] > 0, (
                f"{filename}: duration not detected"
            )
            # get_audio_codec is a thin wrapper and must agree.
            assert get_audio_codec(filepath) == codec
            assert is_audio_file(filepath) is True


def test_video_detection():
    """A video file must be flagged as video; an audio-only file must not."""
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, 'test_video.mp4')
        audio_path = os.path.join(tmpdir, 'test_audio.mp3')

        create_test_video_file(video_path)
        create_test_audio_file('mp3', audio_path)

        video_info = get_codec_info(video_path)
        assert video_info['has_video'] is True, "video stream not detected in video file"
        assert video_info['has_audio'] is True, "audio stream not detected in video file"
        assert video_info['video_codec'] is not None
        assert is_video_file(video_path) is True, "video file not detected as video"

        audio_info = get_codec_info(audio_path)
        assert audio_info['has_video'] is False, "unexpected video stream in audio file"
        assert audio_info['has_audio'] is True
        assert audio_info['video_codec'] is None
        assert is_video_file(audio_path) is False, "audio file incorrectly detected as video"


def test_lossless_detection():
    """Lossless codecs must be reported as lossless; lossy codecs must not."""
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
            create_test_audio_file(codec, filepath)

            assert is_lossless_audio(filepath) is expected_lossless, (
                f"{codec}: is_lossless_audio returned {not expected_lossless}, "
                f"expected {expected_lossless}"
            )


def test_conversion_check():
    """needs_audio_conversion must flag only codecs outside the supported list."""
    with tempfile.TemporaryDirectory() as tmpdir:
        supported_codecs = ['pcm_s16le', 'mp3', 'flac', 'opus', 'aac']

        test_cases = {
            'mp3': ('test.mp3', False),    # Supported, no conversion needed
            'aac': ('test.m4a', False),    # Supported, no conversion needed
            'opus': ('test.opus', False),  # Supported, no conversion needed
            'vorbis': ('test.ogg', True),  # Not supported, needs conversion
        }

        for codec, (filename, should_convert) in test_cases.items():
            filepath = os.path.join(tmpdir, filename)
            create_test_audio_file(codec, filepath)

            needs_conversion, detected_codec = needs_audio_conversion(filepath, supported_codecs)

            assert needs_conversion is should_convert, (
                f"{codec}: needs_conversion={needs_conversion}, expected {should_convert}"
            )
            assert detected_codec == codec, (
                f"{codec}: detected codec {detected_codec}"
            )


def test_misnamed_file():
    """Codec detection must rely on stream contents, not the file extension."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # An MP3 file deliberately named .wav must still be detected as mp3.
        wrong_name_path = os.path.join(tmpdir, 'actually_mp3.wav')
        create_test_audio_file('mp3', wrong_name_path)
        assert get_codec_info(wrong_name_path)['audio_codec'] == 'mp3', (
            "MP3 file misdetected because of .wav extension"
        )

        # A FLAC file deliberately named .mp3 must still be detected as flac.
        # ffmpeg would normally infer the muxer from the .mp3 extension, so
        # force the FLAC container explicitly to produce the misnamed file.
        wrong_name_path2 = os.path.join(tmpdir, 'actually_flac.mp3')
        subprocess.run([
            'ffmpeg', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1.0',
            '-acodec', 'flac', '-f', 'flac', wrong_name_path2
        ], check=True, capture_output=True)
        assert get_codec_info(wrong_name_path2)['audio_codec'] == 'flac', (
            "FLAC file misdetected because of .mp3 extension"
        )


def test_duration():
    """Extracted duration must match the encoded duration within tolerance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        durations = [1.0, 2.5, 5.0]

        for expected_duration in durations:
            filepath = os.path.join(tmpdir, f'test_{expected_duration}s.mp3')
            create_test_audio_file('mp3', filepath, duration=expected_duration)

            detected_duration = get_duration(filepath)

            assert detected_duration is not None, (
                f"{expected_duration}s file: no duration detected"
            )
            # Allow 0.1s tolerance for encoder padding/priming variations.
            assert abs(detected_duration - expected_duration) < 0.1, (
                f"duration {detected_duration:.2f}s, expected {expected_duration}s"
            )


def main():
    """Run all tests standalone (without pytest)."""
    print("=" * 60)
    print("FFProbe Codec Detection Test Suite")
    print("=" * 60)

    if not _ffmpeg_available():
        print("\nffmpeg/ffprobe not found. Please install ffmpeg to run tests.\n")
        return 1

    tests = [
        test_codec_detection,
        test_video_detection,
        test_lossless_detection,
        test_conversion_check,
        test_misnamed_file,
        test_duration,
    ]

    failed = False
    for test in tests:
        try:
            test()
            print(f"PASS - {test.__name__}")
        except AssertionError as e:
            print(f"FAIL - {test.__name__}: {e}")
            failed = True
        except Exception as e:
            print(f"ERROR - {test.__name__}: {e}")
            failed = True

    print("=" * 60)
    print("Some tests failed." if failed else "All tests completed!")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
