"""Incognito uses the same settings chain as an upload (#412 audit P14).

It used to pass the form's empty language straight through (so the user's
language never applied), and it skipped the account and admin hotwords and
initial prompt and the ASR speaker-count defaults. Nothing is stored either way.

SHARED-DB: the user and admin setting are restored afterwards.
"""

import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import SystemSetting, User
import src.tasks.processing as proc


@pytest.fixture
def user():
    with app.app_context():
        old = SystemSetting.get_setting("admin_default_initial_prompt", None)
        SystemSetting.set_setting("admin_default_initial_prompt", "A planning meeting.", setting_type="string")
        s = uuid.uuid4().hex[:8]
        u = User(username=f"inc_{s}", email=f"inc_{s}@local.test", password="x",
                 transcription_language="de", transcription_hotwords="Speakr")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        if old is None:
            SystemSetting.query.filter_by(key="admin_default_initial_prompt").delete()
        else:
            SystemSetting.set_setting("admin_default_initial_prompt", old, setting_type="string")
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def test_incognito_resolves_language_hotwords_and_admin_prompt(user, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 2048)
    connector = MagicMock(PROVIDER_NAME="test", supports_diarization=False, default_diarize=False)
    connector.specifications = MagicMock(max_duration_seconds=1800, handles_chunking_internally=False,
                                         recommended_chunk_seconds=600, unsupported_codecs=[])
    response = MagicMock(text="hello world transcript", segments=None, speakers=None, speaker_embeddings=None)
    response.has_diarization.return_value = False
    connector.transcribe.return_value = response
    with app.app_context():
        u = db.session.get(User, user)
        registry = MagicMock()
        registry.get_active_connector.return_value = connector
        with patch("src.services.transcription.get_registry", return_value=registry), \
             patch.object(proc, "is_video_file", return_value=False), \
             patch.object(proc, "convert_if_needed", return_value=MagicMock(was_converted=False)), \
             patch.object(proc, "chunking_service") as svc, patch.object(proc, "client", None):
            svc.needs_chunking.return_value = False
            svc.get_audio_duration.return_value = 30.0
            proc.transcribe_incognito(str(audio), "a.mp3", language="", user=u)
    call = connector.transcribe.call_args
    request = call.args[0] if call.args else next(iter(call.kwargs.values()))
    assert request.language == "de"
    assert request.hotwords == "Speakr"
    assert request.prompt == "A planning meeting."
