"""Uploads with long file names (#430).

Speakr adds a timestamp and an id to an uploaded file's name when it stores
the file, so a name of 208 or more characters exceeded the file system's
255-byte limit and the upload failed with a 500.
"""

import os
from datetime import datetime

from src.services.storage import get_storage_service
from src.utils.filenames import shorten_filename
from src.utils.titles import placeholder_title, placeholder_titles, resolve_upload_title


def test_short_names_are_unchanged():
    assert shorten_filename("meeting.mp3") == "meeting.mp3"


def test_long_names_keep_their_extension():
    name = shorten_filename("a" * 300 + ".m4a")
    assert name.endswith(".m4a") and len(name.encode("utf-8")) == 150


def test_multibyte_names_are_cut_on_a_character():
    name = shorten_filename("会议" * 200 + ".mp3")
    assert name.endswith(".mp3") and len(name.encode("utf-8")) <= 150
    name.encode("utf-8").decode("utf-8")


def test_storage_key_name_fits_the_file_system():
    key = get_storage_service().build_recording_key("会议" * 200 + ".mp3", 12345, now=datetime(2026, 10, 19))
    assert len(os.path.basename(key).encode("utf-8")) <= 255


def test_titles_fit_the_title_column():
    long_name = "x" * 400 + ".mp3"
    assert len(placeholder_title(long_name)) == 200
    assert all(len(t) == 200 for t in placeholder_titles(long_name))
    assert placeholder_title(long_name) in placeholder_titles(long_name)
    assert len(resolve_upload_title("y" * 300, long_name)) == 200
