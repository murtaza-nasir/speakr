#!/usr/bin/env python3
"""
Tests for the MP3 Xing/VBR header repair (issue #325).

MP3s missing a Xing/Info header force ffmpeg (and Chromium's player) to
estimate duration from bitrate, which causes stuttering playback in
Chromium-based browsers. Speakr repairs such files with a lossless remux
when they would otherwise be stored untouched.

These tests exercise real ffmpeg/ffprobe binaries (available in the dev
container and CI), generating a header-less MP3 via -write_xing 0.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app
from src.utils.ffprobe import mp3_duration_is_estimated, get_codec_info
from src.utils.ffmpeg_utils import remux_mp3_in_place
from src.utils.audio_conversion import convert_if_needed


FFMPEG_AVAILABLE = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None


def _generate_mp3(path, write_xing=True, duration=2):
    """Generate a small sine-wave MP3, optionally without a Xing header."""
    cmd = [
        'ffmpeg', '-v', 'error',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={duration}',
        '-c:a', 'libmp3lame', '-b:a', '64k',
    ]
    if not write_xing:
        cmd += ['-write_xing', '0']
    cmd += ['-y', path]
    subprocess.run(cmd, check=True, capture_output=True, timeout=60)


@unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg/ffprobe not installed")
class TestMp3XingRepair(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.tmpdir = tempfile.mkdtemp(prefix='xing_test_')
        self.broken = os.path.join(self.tmpdir, 'broken.mp3')
        self.good = os.path.join(self.tmpdir, 'good.mp3')
        _generate_mp3(self.broken, write_xing=False)
        _generate_mp3(self.good, write_xing=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.ctx.pop()

    def test_detects_missing_xing_header(self):
        self.assertTrue(mp3_duration_is_estimated(self.broken))

    def test_healthy_mp3_not_flagged(self):
        self.assertFalse(mp3_duration_is_estimated(self.good))

    def test_missing_file_returns_false(self):
        self.assertFalse(mp3_duration_is_estimated(os.path.join(self.tmpdir, 'nope.mp3')))

    def test_remux_repairs_header_and_preserves_duration(self):
        before = get_codec_info(self.broken)
        remux_mp3_in_place(self.broken)
        self.assertFalse(mp3_duration_is_estimated(self.broken))
        after = get_codec_info(self.broken)
        # Still an mp3, duration within a frame or two of the original
        self.assertEqual(after.get('audio_codec'), 'mp3')
        self.assertAlmostEqual(
            float(after.get('duration') or 0),
            float(before.get('duration') or 0),
            delta=0.5,
        )
        self.assertGreater(os.path.getsize(self.broken), 0)

    def test_remux_leaves_no_temp_files(self):
        remux_mp3_in_place(self.broken)
        leftovers = [f for f in os.listdir(self.tmpdir) if f not in ('broken.mp3', 'good.mp3')]
        self.assertEqual(leftovers, [])

    def test_convert_if_needed_repairs_passthrough_mp3(self):
        # An mp3 with a supported codec is stored as-is; the passthrough
        # branch must repair the missing header on the way out.
        result = convert_if_needed(self.broken, needs_chunking=False, is_asr_endpoint=False)
        self.assertEqual(result.output_path, self.broken)
        self.assertFalse(result.was_converted)
        self.assertFalse(mp3_duration_is_estimated(self.broken))

    def test_convert_if_needed_leaves_healthy_mp3_untouched(self):
        size_before = os.path.getsize(self.good)
        mtime_before = os.path.getmtime(self.good)
        result = convert_if_needed(self.good, needs_chunking=False, is_asr_endpoint=False)
        self.assertEqual(result.output_path, self.good)
        self.assertEqual(os.path.getsize(self.good), size_before)
        self.assertEqual(os.path.getmtime(self.good), mtime_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
