"""A .weba file (WebM audio, as browsers save an audio-only WebM) is handled as a .webm is.

The watch folder's extension list had no .weba, so such a file stayed in the
folder for good without a log line. Uploads took it, but kept the browser's
WebM as it was, without the conversion a .webm gets for seeking. A file the
watch folder skips for its extension is now logged once, at INFO.
"""
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording
from src.file_monitor import FileMonitor
from src.utils.audio_conversion import convert_if_needed
from src.utils.mime import guess_media_type

from tests.test_upload_conversion_job import _jobs, _local_path, _run_convert_job, _upload, user  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="ffmpeg/ffprobe not installed")


def _opus_weba(path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-c:a", "libopus", "-f", "webm", str(path)], check=True, capture_output=True)
    return path


# --- MIME type --------------------------------------------------------------

def test_weba_is_webm_audio():
    assert guess_media_type("clip.weba") == "audio/webm"
    assert guess_media_type("CLIP.WEBA") == "audio/webm"
    # The process-wide table Flask serves files with
    assert mimetypes.guess_type("clip.weba")[0] == "audio/webm"
    # A WebM audio file still gets .webm as its extension
    assert mimetypes.guess_extension("audio/webm") == ".webm"


def test_the_installed_app_opens_weba_files():
    manifest = json.loads((Path(__file__).parent.parent / "static" / "manifest.json").read_text())
    handled = [ext for handler in manifest["file_handlers"] for exts in handler["accept"].values() for ext in exts]
    assert ".weba" in handled and ".webm" in handled


# --- conversion ---------------------------------------------------------------

@needs_ffmpeg
def test_an_opus_weba_is_converted_as_a_webm_is(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    path = str(_opus_weba(tmp_path / "clip.weba"))
    with app.app_context():
        result = convert_if_needed(path, original_filename="clip.weba", delete_original=False)
    try:
        assert result.was_converted is True
        assert result.output_path.endswith(".mp3")
        assert any("lacks seek support" in r.getMessage() for r in caplog.records)
    finally:
        if result.output_path != path and os.path.exists(result.output_path):
            os.remove(result.output_path)


def test_a_weba_whose_header_is_not_at_the_front_is_repaired(tmp_path):
    from src.services import recording_ingestion
    from src.utils.ffprobe import FFProbeError
    path = tmp_path / "clip.weba"
    path.write_bytes(b"\x00" * 64)
    info = {"audio_codec": "opus", "has_video": False, "format_name": "matroska,webm"}
    with app.app_context(), \
            patch.object(recording_ingestion, "get_codec_info", side_effect=FFProbeError("bad header")), \
            patch.object(recording_ingestion, "try_repair_malformed_webm", return_value=info) as repair:
        codec_info, probe_error = recording_ingestion.probe_upload(filepath=str(path), original_filename="clip.weba")[:2]
    repair.assert_called_once()
    assert codec_info == info and probe_error is None


@needs_ffmpeg
def test_an_uploaded_weba_is_stored_and_converted(user, tmp_path):  # noqa: F811
    payload = _opus_weba(tmp_path / "in.weba").read_bytes()
    with app.app_context(), patch("src.services.recording_ingestion.CONVERT_WORKERS", 1):
        res = _upload(user, payload, "voice-note.weba")
        assert res.status_code == 202, res.get_json()
        rid = res.get_json()["id"]
        assert _jobs(rid) == [("convert", "queued")]
        assert _run_convert_job().status == "completed"
        rec = db.session.get(Recording, rid)
        assert not rec.audio_path.endswith(".weba")
        assert rec.mime_type.startswith("audio/") and rec.mime_type != "audio/webm"
        assert os.path.exists(_local_path(rec.audio_path))
        assert _jobs(rid) == [("convert", "completed"), ("transcribe", "queued")]


# --- watch folder ---------------------------------------------------------------

@pytest.fixture
def watch(tmp_path):
    monitor = FileMonitor(str(tmp_path), check_interval=30, mode="admin_only")
    monitor._is_file_stable = lambda *a, **kw: True
    processed = []
    monitor._process_file = lambda path, uid, tag_id=None: processed.append(str(path))

    def scan():
        monitor._scan_directory_for_user(monitor.base_watch_directory, 1)
        return processed

    return tmp_path, monitor, scan


def test_the_watch_folder_takes_a_weba(watch):
    folder, _, scan = watch
    (folder / "voice-note.weba").write_bytes(b"x" * 16)
    (folder / "LOUD.WEBA").write_bytes(b"x" * 16)
    assert sorted(Path(p).name for p in scan()) == ["LOUD.WEBA.processing", "voice-note.weba.processing"]


def _skip_lines(caplog):
    return [r for r in caplog.records if r.name == "file_monitor" and "Skipping" in r.getMessage()]


def test_a_skipped_extension_is_logged_once_at_info(watch, caplog):
    caplog.set_level(logging.INFO, logger="file_monitor")
    folder, _, scan = watch
    (folder / "meeting.xyz").write_bytes(b"x")
    (folder / "noextension").write_bytes(b"x")
    for _ in range(3):
        assert scan() == []
    lines = _skip_lines(caplog)
    assert sorted(r.getMessage() for r in lines) == sorted([
        f"Skipping {folder / 'meeting.xyz'} in the watch folder: the extension '.xyz' is not a supported audio or video type",
        f"Skipping {folder / 'noextension'} in the watch folder: the extension '(none)' is not a supported audio or video type",
    ])
    assert all(r.levelno == logging.INFO for r in lines)


def test_a_skipped_file_that_returns_is_logged_again(watch, caplog):
    caplog.set_level(logging.INFO, logger="file_monitor")
    folder, _, scan = watch
    path = folder / "meeting.xyz"
    path.write_bytes(b"x")
    scan()
    path.unlink()
    scan()
    path.write_bytes(b"x")
    scan()
    scan()
    assert len(_skip_lines(caplog)) == 2


def test_hidden_set_aside_and_locked_files_are_not_logged(watch, caplog):
    caplog.set_level(logging.INFO, logger="file_monitor")
    folder, _, scan = watch
    for name in (".hidden.txt", "broken.mp3.failed", "busy.mp3.processing"):
        (folder / name).write_bytes(b"x")
    assert scan() == []
    assert _skip_lines(caplog) == []


def test_the_documented_watch_folder_extensions_are_the_ones_taken(tmp_path):
    import re
    doc = (Path(__file__).parent.parent / "docs" / "getting-started" / "installation.md").read_text()
    line = next(l for l in doc.splitlines() if l.startswith("Only files with one of these extensions"))
    documented = {f".{ext}" for ext in re.findall(r"`([a-z0-9]+)`", line)}
    assert documented == FileMonitor(str(tmp_path)).potential_audio_extensions
