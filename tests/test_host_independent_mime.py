"""Stored MIME types do not depend on the host's /etc/mime.types.

``mimetypes.guess_type`` reads the host's type files, so ``.mkv`` was
video/matroska on Ubuntu 26.04, video/x-matroska on older hosts and unknown in
the Docker image, and ``.ts`` was a Qt translation file wherever the
media-types package is installed. Speakr guesses from Python's built-in table
plus its own registrations, which is what the Docker image uses.
"""
import mimetypes

import pytest

import src.app  # noqa: F401  (registers Speakr's types in the process-wide table)
from src.utils import mime
from src.utils.mime import SPEAKR_MEDIA_TYPES, guess_media_type


@pytest.fixture
def host_types(monkeypatch):
    """A host whose /etc/mime.types disagrees with the Docker image."""
    host = {".mkv": "video/matroska", ".ts": "text/vnd.trolltech.linguist",
            ".opus": "audio/ogg", ".m4v": "video/mp4"}
    real = mimetypes.guess_type

    def fake(url, strict=True):
        for ext, kind in host.items():
            if str(url).lower().endswith(ext):
                return kind, None
        return real(url, strict)

    monkeypatch.setattr(mimetypes, "guess_type", fake)
    return host


@pytest.mark.parametrize("name, expected", [
    ("a.mp3", "audio/mpeg"), ("a.wav", "audio/x-wav"), ("a.m4a", "audio/x-m4a"),
    ("a.aac", "audio/aac"), ("a.flac", "audio/flac"), ("a.ogg", "audio/ogg"),
    ("a.webm", "audio/webm"), ("a.opus", "audio/opus"), ("a.mp4", "video/mp4"),
    ("a.mov", "video/quicktime"), ("a.MP3", "audio/mpeg"),
    ("a.mkv", None), ("a.ts", None), ("a.m4v", None), ("a", None),
])
def test_the_guess_ignores_the_host_type_files(host_types, name, expected):
    assert guess_media_type(name) == expected


def test_speakr_registrations_match_the_process_wide_table():
    for kind, ext in SPEAKR_MEDIA_TYPES:
        if kind == "audio/mp4":  # .m4a is registered twice; the later audio/x-m4a wins
            continue
        assert guess_media_type("x" + ext) == kind


def test_an_mkv_without_a_probe_falls_back_to_the_fixed_video_type(host_types):
    assert mime.resolve_media_mime("missing.mkv", codec_info={}, has_video=True) == "video/x-matroska"


def test_a_ts_file_is_never_served_as_a_text_type(host_types):
    assert mime.served_media_mime(None, "clip.ts") == "application/octet-stream"
