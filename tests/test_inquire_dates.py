"""Inquire answers date questions on the user's calendar days (#412 T8).

The agent and both search functions compared stored UTC datetimes with bare
dates, so the last day of a range was dropped and days were UTC days; the
agent's "today" and the dates it shows were UTC too.

SHARED-DB: the user and recordings are removed afterwards.
"""

import os
import sys
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User
from src.services import inquire_agent as ia


@pytest.fixture
def world():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        u = User(username=f"iqd_{s}", email=f"iqd_{s}@local.test", password="x",
                 timezone="Asia/Tokyo", timezone_mode="fixed")
        db.session.add(u)
        db.session.commit()
        # 2026-03-04 23:30 UTC = 2026-03-05 08:30 Tokyo; 2026-03-05 18:00 UTC = 2026-03-06 03:00 Tokyo
        a = Recording(user_id=u.id, title="Morning", original_filename="a.wav", status="COMPLETED",
                      meeting_date=datetime(2026, 3, 4, 23, 30))
        b = Recording(user_id=u.id, title="Next day", original_filename="b.wav", status="COMPLETED",
                      meeting_date=datetime(2026, 3, 5, 18, 0))
        db.session.add_all([a, b])
        db.session.commit()
        ids = dict(user=u.id, a=a.id, b=b.id)
    yield ids
    with app.app_context():
        for model, key in ((Recording, "a"), (Recording, "b"), (User, "user")):
            obj = db.session.get(model, ids[key])
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


def test_list_tool_uses_local_days_and_keeps_the_last_day(world):
    with app.app_context():
        ctx = ia.ToolContext(world["user"], {}, {world["a"], world["b"]}, {"summaries": True, "notes": True})
        out = ia.tool_list_recordings(ctx, {"date_from": "2026-03-05", "date_to": "2026-03-05"})
        titles = {r["title"] for r in out["recordings"]}
        assert titles == {"Morning"}
        brief = next(r for r in out["recordings"] if r["title"] == "Morning")
        assert brief["meeting_date"] == "2026-03-05 08:30"


def test_scope_filter_uses_local_days(world):
    with app.app_context():
        allowed = ia.resolve_allowed_recording_ids(world["user"], {"date_from": datetime(2026, 3, 6).date(),
                                                                   "date_to": datetime(2026, 3, 6).date()})
        assert world["b"] in allowed and world["a"] not in allowed


def test_today_is_the_users_day():
    with patch("src.utils.timezones.now_local", return_value=datetime(2026, 3, 6, 1, 0)):
        prompt = ia._system_prompt({"name": "A", "title": "x", "company": "y", "timezone": "Asia/Tokyo"},
                                   "", {"summaries": True, "notes": True}, "")
    assert "Today's date is 2026-03-06." in prompt
