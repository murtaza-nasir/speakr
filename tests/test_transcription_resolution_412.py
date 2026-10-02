"""Transcription settings resolver (#412 audit S6, S9).

S6: the admin default hotwords and initial prompt are the resolver's last
level, so every caller that reads the resolved values sees them.
S9: language "auto" forces auto-detection past tag, folder and account
defaults; an empty value still falls through to them (the rule since f37469d6).

SHARED-DB: the user and admin settings are restored afterwards.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import SystemSetting, User
from src.services.transcription_defaults import resolve_transcription_params

KEYS = ("admin_default_hotwords", "admin_default_initial_prompt")


@pytest.fixture
def owner():
    with app.app_context():
        old = {k: SystemSetting.get_setting(k, None) for k in KEYS}
        SystemSetting.set_setting("admin_default_hotwords", "Speakr, WhisperX", setting_type="string")
        SystemSetting.set_setting("admin_default_initial_prompt", "A business meeting.", setting_type="string")
        s = uuid.uuid4().hex[:8]
        u = User(username=f"tr_{s}", email=f"tr_{s}@local.test", password="x", transcription_language="en")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        for k, v in old.items():
            if v is None:
                SystemSetting.query.filter_by(key=k).delete()
            else:
                SystemSetting.set_setting(k, v, setting_type="string")
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _resolve(uid, **overrides):
    return resolve_transcription_params(None, overrides, tags=[], folder=None, owner=db.session.get(User, uid))


def test_admin_defaults_are_the_last_level(owner):
    with app.app_context():
        p = _resolve(owner)
        assert p["hotwords"] == "Speakr, WhisperX"
        assert p["initial_prompt"] == "A business meeting."


def test_user_values_beat_admin_defaults(owner):
    with app.app_context():
        u = db.session.get(User, owner)
        u.transcription_hotwords = "Quarterly"
        db.session.commit()
        assert _resolve(owner)["hotwords"] == "Quarterly"
        assert _resolve(owner, hotwords="Override")["hotwords"] == "Override"


def test_auto_forces_auto_detection_and_empty_falls_through(owner):
    with app.app_context():
        assert _resolve(owner, language="auto")["language"] is None
        assert _resolve(owner, language="")["language"] == "en"
        assert _resolve(owner)["language"] == "en"
        assert _resolve(owner, language="de")["language"] == "de"
