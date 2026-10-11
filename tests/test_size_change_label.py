"""Conversion log lines say "larger" when the file grew (#opus-to-mp3 doubling)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.audio_conversion import ConversionResult
from src.utils.ffmpeg_utils import describe_size_change, describe_size_reduction


def test_labels_name_the_direction():
    assert describe_size_change(1000, 598) == "40.2% smaller"
    assert describe_size_change(226_900_000, 439_000_000) == "93.5% larger"
    assert describe_size_change(1000, 1000) == "same size"
    assert describe_size_change(0, 10) == "same size"
    assert describe_size_reduction(-93.5) == "93.5% larger"


def test_conversion_result_exposes_the_label():
    r = ConversionResult(output_path="x", mime_type="audio/mpeg", was_converted=True,
                         was_compressed=False, original_size=1000, final_size=2000,
                         original_codec="opus", final_codec="mp3")
    assert r.size_change_label == "100.0% larger"
