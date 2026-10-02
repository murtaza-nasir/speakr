"""API v1 chat and /users/me follow the same settings as the web app (#412 audit).

S8: API v1 chat used a fixed 0.7 temperature and ignored the user's output
language; web chat uses the admin chat temperature and the output language.
S10: /users/me reported auto_summarization None (never set, which processing
treats as enabled) as False.

SHARED-DB: the user, recording, token and setting are removed afterwards.
"""

import os
import secrets
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, SystemSetting, User
from src.services import llm_settings as ls
from src.utils.token_auth import hash_token


@pytest.fixture
def setup():
    with app.app_context():
        name = f"v1chat_{secrets.token_hex(4)}"
        u = User(username=name, email=f"{name}@local.test", password="x", output_language="German")
        db.session.add(u)
        db.session.commit()
        r = Recording(user_id=u.id, title="Sync", status="COMPLETED", original_filename="s.wav",
                      transcription="Speaker 1: We move the launch to next quarter.")
        plain = f"test-token-{secrets.token_urlsafe(16)}"
        t = APIToken(user_id=u.id, token_hash=hash_token(plain), name="v1chat")
        db.session.add_all([r, t])
        db.session.commit()
        old = SystemSetting.get_setting(ls.setting_key("chat"), None)
        SystemSetting.set_setting(ls.setting_key("chat"), "0.2", setting_type="string")
        db.session.commit()
        ids = (u.id, r.id, t.id)
    yield ids, plain
    with app.app_context():
        if old is None:
            SystemSetting.query.filter_by(key=ls.setting_key("chat")).delete()
        else:
            SystemSetting.set_setting(ls.setting_key("chat"), old, setting_type="string")
        for model, oid in zip((APIToken, Recording, User), reversed(ids)):
            obj = db.session.get(model, oid)
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def test_v1_chat_uses_admin_temperature_and_output_language(setup):
    (uid, rid, _), plain = setup
    reply = MagicMock()
    reply.choices = [MagicMock(message=MagicMock(content="Ja."))]
    with patch("src.services.llm.chat_client", object()), \
         patch("src.services.llm.call_chat_completion", return_value=reply) as call:
        with app.test_client() as c:
            resp = c.post(f"/api/v1/recordings/{rid}/chat", json={"message": "When is the launch?"},
                          headers={"Authorization": f"Bearer {plain}"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    messages = call.call_args.args[0]
    assert call.call_args.kwargs["temperature"] == 0.2
    assert "Please provide all your responses in German." in messages[0]["content"]


def test_users_me_reports_unset_auto_summarization_as_enabled(setup):
    (uid, _, _), plain = setup
    with app.app_context():
        u = db.session.get(User, uid)
        u.auto_summarization = None
        db.session.commit()
    with app.test_client() as c:
        body = c.get("/api/v1/users/me", headers={"Authorization": f"Bearer {plain}"}).get_json()
    prefs = body.get("preferences", body)
    assert prefs["auto_summarization"] is True
