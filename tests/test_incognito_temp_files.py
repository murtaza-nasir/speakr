"""No audio of an incognito recording stays in the temporary folder.

A lossless upload (WAV, FLAC) is compressed before it is sent to the
transcription service, and a file in a container the service cannot read is
remuxed. Neither counts as "converted", and the file written for it was left
in the temporary folder after the request.
"""

import os
import sys
import wave
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app
import src.tasks.processing as proc
from src.services.transcription.base import ConnectorSpecifications, TranscriptionResponse
from src.utils.audio_conversion import ConversionResult


class _Connector:
    specifications = ConnectorSpecifications()
    supports_diarization = False
    default_diarize = False

    def __init__(self):
        self.sent = []

    def transcribe(self, request):
        self.sent.append(request.audio_file.name)
        return TranscriptionResponse(text="a transcript of more than ten characters")


def _run(tmp_path, was_converted, was_compressed):
    upload = tmp_path / "tmpabc123.wav"
    with wave.open(str(upload), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 16000)
    derived = tmp_path / "tmpabc123.mp3"

    def convert(filepath, **kwargs):
        derived.write_bytes(b"derived audio")
        return ConversionResult(output_path=str(derived), mime_type="audio/mpeg", was_converted=was_converted,
                                was_compressed=was_compressed, original_size=1, final_size=1,
                                original_codec="pcm_s16le", final_codec="mp3")

    connector = _Connector()
    registry = SimpleNamespace(get_active_connector=lambda: connector)
    with app.app_context(), \
         patch("src.services.transcription.get_registry", return_value=registry), \
         patch.object(proc, "convert_if_needed", side_effect=convert), \
         patch.object(proc.chunking_service, "needs_chunking", return_value=False), \
         patch.object(proc.llm_config, "is_configured", return_value=False):
        result = proc.transcribe_incognito(str(upload), "visit.wav", language="en")
    assert result["error"] is None
    assert connector.sent == [str(derived)]
    return upload, derived


def test_compressed_copy_of_a_lossless_upload_is_removed(tmp_path):
    upload, derived = _run(tmp_path, was_converted=False, was_compressed=True)
    assert not derived.exists()
    assert upload.exists()          # the upload itself is removed by the route


def test_remuxed_copy_is_removed(tmp_path):
    _, derived = _run(tmp_path, was_converted=False, was_compressed=False)
    assert not derived.exists()


def test_converted_copy_is_removed(tmp_path):
    _, derived = _run(tmp_path, was_converted=True, was_compressed=False)
    assert not derived.exists()
