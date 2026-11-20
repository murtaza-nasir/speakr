#!/usr/bin/env python3
"""The tag dialog offers Default Language for connectors that accept a language.

The speaker-count controls stay limited to connectors that support diarization.

Run:
    python -m pytest tests/test_tag_language_field.py -q
"""

import contextlib
import os
import sys
import uuid
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import User  # noqa: E402
from src.services.transcription.base import TranscriptionCapability  # noqa: E402
from src.services.transcription.connectors.asr_endpoint import ASREndpointConnector  # noqa: E402
from src.services.transcription.connectors.assemblyai import AssemblyAITranscriptionConnector  # noqa: E402
from src.services.transcription.connectors.azure_openai_transcribe import AzureOpenAITranscribeConnector  # noqa: E402
from src.services.transcription.connectors.mistral import MistralTranscriptionConnector  # noqa: E402
from src.services.transcription.connectors.openai_transcribe import OpenAITranscribeConnector  # noqa: E402
from src.services.transcription.connectors.openai_whisper import OpenAIWhisperConnector  # noqa: E402
from src.services.transcription.connectors.openasr import OpenASRTranscriptionConnector  # noqa: E402


# Connectors that read request.language and pass it to the provider.
FORWARDS_LANGUAGE = [
    ASREndpointConnector,
    AssemblyAITranscriptionConnector,
    AzureOpenAITranscribeConnector,
    MistralTranscriptionConnector,
    OpenAITranscribeConnector,
    OpenAIWhisperConnector,
    OpenASRTranscriptionConnector,
]


@pytest.mark.parametrize("cls", FORWARDS_LANGUAGE)
def test_connectors_that_forward_language_declare_it(cls):
    assert TranscriptionCapability.LANGUAGE_SELECTION in cls.CAPABILITIES


class _StubConnector:
    """Minimal registry connector; only the account page flags are read."""

    def __init__(self, language, diarization=False):
        self.supports_language_selection = language
        self.supports_diarization = diarization
        self.supports_speaker_count_control = diarization
        self.supports_exact_speaker_count = False
        self.supports_hotwords = False
        self.supports_initial_prompt = False

    def supports(self, capability):
        return False


@pytest.fixture(autouse=True)
def _disable_csrf():
    prev = app.config.get('WTF_CSRF_ENABLED')
    app.config['WTF_CSRF_ENABLED'] = False
    yield
    app.config['WTF_CSRF_ENABLED'] = prev


@pytest.fixture
def user():
    with app.app_context():
        u = User(username="u_" + uuid.uuid4().hex[:14],
                 email=uuid.uuid4().hex[:10] + "@example.com",
                 password=None, email_verified=True)
        db.session.add(u)
        db.session.commit()
        uid = u.id
        yield u
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


PAGES = ['/account', '/']


@contextlib.contextmanager
def _page(user, connector, path):
    client = app.test_client()
    with client.session_transaction() as s:
        s['_user_id'] = str(user.id)
        s['_fresh'] = True
    with mock.patch('src.services.transcription.get_registry') as reg:
        reg.return_value.get_active_connector.return_value = connector
        reg.return_value.get_active_connector_name.return_value = 'stub'
        resp = client.get(path)
    assert resp.status_code == 200
    yield resp.get_data(as_text=True)


@pytest.mark.parametrize('path', PAGES)
def test_language_only_connector_shows_language_but_not_speakers(user, path):
    with _page(user, _StubConnector(language=True), path) as page:
        assert 'id="tagLanguage"' in page
        assert 'id="tagMinSpeakers"' not in page
        assert 'id="tagMaxSpeakers"' not in page


@pytest.mark.parametrize('path', PAGES)
def test_diarizing_connector_shows_language_and_speakers(user, path):
    with _page(user, _StubConnector(language=False, diarization=True), path) as page:
        assert 'id="tagLanguage"' in page
        assert 'id="tagMinSpeakers"' in page
        assert 'id="tagMaxSpeakers"' in page


@pytest.mark.parametrize('path', PAGES)
def test_connector_without_language_hides_language_field(user, path):
    with _page(user, _StubConnector(language=False), path) as page:
        assert 'id="tagLanguage"' not in page
        assert 'id="tagMinSpeakers"' not in page

def _connectors_ignoring_language():
    from src.services.transcription.connectors.alibaba_funasr import AlibabaFunASRConnector
    from src.services.transcription.connectors.mossland import MosslandTranscriptionConnector
    from src.services.transcription.connectors.tencent_asr import TencentASRConnector
    from src.services.transcription.connectors.vibevoice import VibeVoiceTranscriptionConnector
    return [AlibabaFunASRConnector, MosslandTranscriptionConnector, TencentASRConnector,
            VibeVoiceTranscriptionConnector]


@pytest.mark.parametrize("cls", _connectors_ignoring_language())
def test_connectors_that_ignore_language_do_not_declare_it(cls):
    assert TranscriptionCapability.LANGUAGE_SELECTION not in cls.CAPABILITIES


@pytest.mark.parametrize('path', PAGES)
def test_the_folder_dialog_follows_the_same_rule(user, path):
    with _page(user, _StubConnector(language=True), path) as page:
        assert 'id="folderLanguage"' in page and 'id="folderMinSpeakers"' not in page
    with _page(user, _StubConnector(language=False), path) as page:
        assert 'id="folderLanguage"' not in page


@pytest.mark.parametrize('language', [True, False])
def test_the_main_page_tells_the_app_whether_a_language_is_used(user, language):
    with _page(user, _StubConnector(language=language), '/') as page:
        assert f'data-connector-supports-language="{language}"' in page
