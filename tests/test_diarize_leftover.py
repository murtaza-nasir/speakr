"""The per-user diarize preference is a leftover (#412 audit, item S2).

Its Account-page checkbox was removed in Aug 2025, but the save handler kept
writing the field from the missing checkbox (always False) and API v1 reported
it, while transcription never read it. Saves no longer touch it, and API v1
reports what transcription actually does: the active connector's default.
"""

import os
import re
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app
from src.api import api_v1

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_account_save_no_longer_writes_the_diarize_field():
    src = open(os.path.join(ROOT, "src", "api", "auth.py"), encoding="utf-8").read()
    assert not re.search(r"current_user\.diarize\s*=", src)


def test_api_reports_the_connector_default():
    for supports, default, expected in [(True, True, True), (True, False, False), (False, True, False)]:
        connector = MagicMock(supports_diarization=supports, default_diarize=default)
        registry = MagicMock()
        registry.get_active_connector.return_value = connector
        with app.app_context(), patch("src.services.transcription.get_registry", return_value=registry):
            assert api_v1._effective_diarize() is expected


def test_api_reports_false_when_no_connector_is_available():
    with app.app_context(), patch("src.services.transcription.get_registry", side_effect=RuntimeError("none")):
        assert api_v1._effective_diarize() is False
