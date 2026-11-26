"""Temporary files of the transcription connectors.

No connector writes the reply of the transcription service to a file. A
connector that copies the audio to a temporary file of its own (VibeVoice for
ffprobe, Tencent for the upload) writes the copy of an incognito file into the
folder of its request, and removes it before it returns.
"""

import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services import incognito_temp
from src.services.transcription.base import TranscriptionRequest
from src.services.transcription.connectors import audiocpp, tencent_asr, vibevoice

ROOT = Path(__file__).resolve().parent.parent
CONNECTORS = ROOT / "src" / "services" / "transcription"
SENTINEL = "ZZ transcript sentinel ZZ"
REAL_CLIENT = httpx.Client        # the Tencent tests replace httpx.Client
REPLY = json.dumps([{"Start": 0.0, "End": 1.5, "Speaker": 0, "Content": SENTINEL}])


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """A system temporary folder and an incognito folder of this test only."""
    system = tmp_path / "system-tmp"
    system.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(system))
    monkeypatch.setenv("INCOGNITO_TEMP_DIR", str(tmp_path / "incognito"))
    request_dir = incognito_temp.new_request_dir()
    return SimpleNamespace(system=system, request_dir=Path(request_dir), other=tmp_path)


def _files(folder):
    return sorted(str(p.relative_to(folder)) for p in Path(folder).rglob("*"))


def _audio(folder, name="tmpabc123.wav"):
    path = Path(folder) / name
    path.write_bytes(b"RIFF" + b"\x00" * 64)
    return path


def _vibevoice(monkeypatch, seen):
    connector = vibevoice.VibeVoiceTranscriptionConnector({"base_url": "http://vibevoice.test/v1"})

    def post(url, **kwargs):
        return httpx.Response(200, json={"choices": [{"message": {"content": REPLY}, "finish_reason": "stop"}]},
                              request=httpx.Request("POST", "http://vibevoice.test/v1/chat/completions"))

    def ffprobe(cmd, **kwargs):
        seen.append(cmd[-1])
        assert os.path.exists(cmd[-1])
        return SimpleNamespace(returncode=0, stdout="1.50\n", stderr="")

    monkeypatch.setattr(connector.client, "post", post)
    monkeypatch.setattr("subprocess.run", ffprobe)
    return connector


def test_vibevoice_writes_no_copy_of_the_reply(folders, monkeypatch):
    seen = []
    connector = _vibevoice(monkeypatch, seen)
    with open(_audio(folders.other), "rb") as audio:
        result = connector.transcribe(TranscriptionRequest(audio_file=audio, filename="visit.wav"))
    assert SENTINEL in result.text
    # Nothing is left in the temporary folder: no reply, no copy of the audio.
    assert _files(folders.system) == []
    assert not (folders.system / "vibevoice_last_response.json").exists()
    assert len(seen) == 1 and Path(seen[0]).parent == folders.system
    for path in folders.other.rglob("*"):
        if path.is_file():
            assert SENTINEL.encode() not in path.read_bytes(), path


def test_vibevoice_copy_of_an_incognito_file_is_in_its_request_folder(folders, monkeypatch):
    seen = []
    connector = _vibevoice(monkeypatch, seen)
    upload = _audio(folders.request_dir)
    with open(upload, "rb") as audio:
        result = connector.transcribe(TranscriptionRequest(audio_file=audio, filename="visit.wav"))
    assert SENTINEL in result.text
    assert len(seen) == 1 and Path(seen[0]).parent == folders.request_dir
    assert _files(folders.request_dir) == [upload.name]       # the copy is removed again
    assert _files(folders.system) == []


def test_vibevoice_copy_is_removed_when_ffprobe_fails(folders, monkeypatch):
    connector = vibevoice.VibeVoiceTranscriptionConnector({"base_url": "http://vibevoice.test/v1"})

    def ffprobe(cmd, **kwargs):
        raise OSError("ffprobe is missing")

    monkeypatch.setattr("subprocess.run", ffprobe)
    assert connector._get_audio_duration(b"audio", "visit.wav", temp_dir=str(folders.request_dir)) is None
    assert connector._get_audio_duration(b"audio", "visit.wav") is None
    assert _files(folders.request_dir) == [] and _files(folders.system) == []


def test_vibevoice_malformed_reply_is_parsed_without_a_file(folders):
    connector = vibevoice.VibeVoiceTranscriptionConnector({"base_url": "http://vibevoice.test/v1"})
    segments = connector._parse_json_segments(REPLY[:-1] + ', {"Start": 2.0, "Con')
    assert segments and segments[0]["Content"] == SENTINEL
    assert _files(folders.system) == []


def _tencent(monkeypatch, seen):
    connector = tencent_asr.TencentASRConnector({"secret_id": "test-id", "secret_key": "test-secret",
                                                 "poll_interval": .001, "timeout": .5})
    replies = [{"Data": {"TaskId": 42}},
               {"Data": {"Status": 2, "AudioDuration": 1.5, "ResultDetail": [
                   {"FinalSentence": SENTINEL, "StartMs": 0, "EndMs": 1500, "SpeakerId": 0}]}}]
    client = REAL_CLIENT(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"Response": replies.pop(0)})))
    monkeypatch.setattr(tencent_asr.httpx, "Client", lambda **kwargs: client)
    real = tempfile.TemporaryDirectory

    class Recorded(real):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen.append(self.name)

    monkeypatch.setattr(tencent_asr.tempfile, "TemporaryDirectory", Recorded)
    return connector


def test_tencent_copy_of_an_incognito_file_is_in_its_request_folder(folders, monkeypatch):
    seen = []
    connector = _tencent(monkeypatch, seen)
    upload = _audio(folders.request_dir)
    with open(upload, "rb") as audio:
        result = connector.transcribe(TranscriptionRequest(audio_file=audio, filename="visit.wav"))
    assert result.text == SENTINEL
    assert len(seen) == 1 and Path(seen[0]).parent == folders.request_dir
    assert _files(folders.request_dir) == [upload.name]
    assert _files(folders.system) == []


def test_tencent_copy_of_an_ordinary_file_is_removed_from_the_temporary_folder(folders, monkeypatch):
    seen = []
    for audio in (io.BytesIO(b"audio"), open(_audio(folders.other), "rb")):
        with audio:
            _tencent(monkeypatch, seen).transcribe(TranscriptionRequest(audio_file=audio, filename="visit.wav"))
    assert len(set(seen)) == 2 and {Path(p).parent for p in seen} == {folders.system}
    assert _files(folders.system) == []


def _audiocpp(monkeypatch, seen):
    connector = audiocpp.AudioCppTranscriptionConnector(
        {"base_url": "http://audiocpp.test:8080", "model": "vibevoice-asr"})

    def ffmpeg(cmd, **kwargs):
        source, target = cmd[cmd.index("-i") + 1], cmd[-1]
        seen.extend([source, target])
        assert os.path.exists(source)
        Path(target).write_bytes(b"RIFF converted")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, **kwargs):
            return httpx.Response(200, json={"text": SENTINEL, "timing": {}},
                                  request=httpx.Request("POST", url))

    monkeypatch.setattr(audiocpp.subprocess, "run", ffmpeg)
    monkeypatch.setattr(audiocpp.httpx, "Client", Client)
    return connector


def test_audiocpp_copies_of_an_incognito_file_are_in_its_request_folder(folders, monkeypatch):
    seen = []
    connector = _audiocpp(monkeypatch, seen)
    upload = _audio(folders.request_dir, "tmpabc123.m4a")
    with open(upload, "rb") as audio:
        result = connector.transcribe(TranscriptionRequest(audio_file=audio, filename="visit.m4a"))
    assert result.text == SENTINEL
    assert len(seen) == 2 and {Path(p).parent for p in seen} == {folders.request_dir}
    assert _files(folders.request_dir) == [upload.name]       # both copies are removed again
    assert _files(folders.system) == []


def test_audiocpp_copies_of_an_ordinary_file_are_removed(folders, monkeypatch):
    seen = []
    connector = _audiocpp(monkeypatch, seen)
    connector.transcribe(TranscriptionRequest(audio_file=io.BytesIO(b"not a wav"), filename="visit.mp3"))
    assert len(seen) == 2 and {Path(p).parent for p in seen} == {folders.system}
    assert _files(folders.system) == []


def test_dir_for_stream(folders):
    upload = _audio(folders.request_dir)
    with open(upload, "rb") as audio:
        assert incognito_temp.dir_for_stream(audio) == str(folders.request_dir)
    with open(_audio(folders.other), "rb") as audio:
        assert incognito_temp.dir_for_stream(audio) is None
    assert incognito_temp.dir_for_stream(io.BytesIO(b"x")) is None
    with open(upload, "rb") as audio, os.fdopen(os.dup(audio.fileno()), "rb") as by_descriptor:
        assert incognito_temp.dir_for_stream(by_descriptor) is None     # its name is a number


def test_no_connector_writes_a_file_outside_these_two():
    """A new temporary file or debug dump in a connector is routed through
    incognito_temp.dir_for_stream, or this list is changed on purpose."""
    writes = re.compile(r"tempfile\.|gettempdir|mkstemp|mkdtemp|open\([^)]*[\x27\"][wax]b?\+?[\x27\"]"
                        r"|\.write_text\(|\.write_bytes\(|json\.dump\(")
    found = {}
    for path in sorted(CONNECTORS.rglob("*.py")):
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                 if writes.search(line) and not line.strip().startswith("#")]
        if lines:
            found[path.name] = lines
    assert set(found) == {"audiocpp.py", "tencent_asr.py", "vibevoice.py"}, found
    for name, lines in found.items():
        source = (CONNECTORS / "connectors" / name).read_text(encoding="utf-8")
        assert "dir_for_stream(request.audio_file)" in source, name
        assert "vibevoice_last_response" not in source and "gettempdir" not in source, name
