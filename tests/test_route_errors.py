"""Routes answer an unexpected exception with a fixed message and a
reference, never with the exception's text (the text can quote SQL, paths
or connection settings). The cause is logged with the same reference.

SHARED-DB: the users made here are removed afterwards.
"""

import logging
import os
import re
import secrets
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
from tests.test_recording_sessions import _login

app.config["WTF_CSRF_ENABLED"] = False
SECRET = "postgresql://speakr:hunter2@db.internal:5432/speakr /srv/app/data/x.db"
API = Path(__file__).resolve().parent.parent / "src" / "api"


def test_no_route_answers_with_the_text_of_an_exception():
    pattern = re.compile(r"""jsonify\(\{\s*['"]error['"]\s*:\s*str\(e\)\s*\}\)\s*,\s*5\d\d""")
    found = [f"{p.name}:{n}" for p in sorted(API.glob("*.py"))
             for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
             if pattern.search(line)]
    assert found == []


@pytest.fixture
def user():
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f"re_{s}", email=f"re_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        Recording.query.filter_by(user_id=uid).delete()
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _check(resp, caplog):
    assert resp.status_code == 500
    body = resp.get_json()
    text = resp.get_data(as_text=True)
    assert "hunter2" not in text and "db.internal" not in text and "/srv/app" not in text
    assert body["code"] == "unexpected_error_ref"
    assert re.fullmatch(r"[0-9a-f]{8}", body["error_id"])
    assert body["error_id"] in body["error"]
    logged = [r for r in caplog.records if body["error_id"] in r.getMessage()]
    assert logged and "hunter2" in logged[0].getMessage() and logged[0].exc_info


def test_the_token_budget_route_hides_the_exception(user, caplog):
    caplog.set_level(logging.ERROR)
    client = app.test_client()
    with app.app_context():
        u = db.session.get(User, user)
        u.monthly_token_budget = 1000
        db.session.commit()
        _login(client, u)
    with patch("src.api.system.token_tracker.get_monthly_usage", side_effect=RuntimeError(SECRET)):
        resp = client.get("/api/user/token-budget")
    _check(resp, caplog)


def test_the_events_route_hides_the_exception(user, caplog):
    caplog.set_level(logging.ERROR)
    client = app.test_client()
    with app.app_context():
        u = db.session.get(User, user)
        rec = Recording(user_id=u.id, title="r", status="COMPLETED")
        db.session.add(rec)
        db.session.commit()
        rid = rec.id
        _login(client, u)

    class Broken:
        class query:
            @staticmethod
            def filter_by(**kwargs):
                raise RuntimeError(SECRET)

    with patch("src.api.events.Event", Broken):
        resp = client.get(f"/api/recording/{rid}/events")
    _check(resp, caplog)


def test_the_message_is_translated():
    from src.utils.localization import api_error
    for lang in ("en", "de", "es", "fr", "pt-BR", "ru", "zh"):
        text = api_error("unexpected_error_ref", lang, ref="abc12345")["error"]
        assert "abc12345" in text and "{{" not in text, lang
