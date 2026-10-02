"""Extracted events in API v1 and ICS (mailr spec G5).

The events route returns attendees (as {name, email}) and the reminder, the
same objects as the detail; event times are floating wall-clock times. The
ICS output escapes text, folds long lines, lists real attendee addresses and
carries the reminder.

SHARED-DB: the user, token, recording and events are removed afterwards.
"""

import json
import os
import re
import secrets
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Event, Recording, User
from src.utils.token_auth import hash_token

ATTENDEES = ["Dana Lee", "omar@example.org", "Priya Shah <priya@example.org>",
             {"name": "Lee", "email": "lee@example.org"}, {"name": "Kim"}, 42, "", {"phone": "1"}]


@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f"ev_{s}", email=f"ev_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        rec = Recording(user_id=u.id, title="Sync", status="COMPLETED", original_filename="s.wav")
        db.session.add(rec)
        db.session.commit()
        ev = Event(recording_id=rec.id, title="Budget review, Q4; draft",
                   description="Bring the numbers.\nAnd the slides; all of them. " + "Long text " * 12,
                   start_datetime=datetime(2026, 10, 9, 15, 0), end_datetime=datetime(2026, 10, 9, 16, 0),
                   location="Room 4, Building B", attendees=json.dumps(ATTENDEES), reminder_minutes=30)
        plain = f"tok-{secrets.token_urlsafe(16)}"
        db.session.add_all([ev, APIToken(user_id=u.id, token_hash=hash_token(plain), name="ev")])
        db.session.commit()
        ids = dict(user=u.id, rec=rec.id, event=ev.id, token=plain)
    yield ids
    with app.app_context():
        Event.query.filter_by(recording_id=ids["rec"]).delete()
        Recording.query.filter_by(id=ids["rec"]).delete()
        APIToken.query.filter_by(user_id=ids["user"]).delete()
        User.query.filter_by(id=ids["user"]).delete()
        db.session.commit()


def _get(world, path):
    with app.test_client() as c:
        return c.get(path, headers={"Authorization": f"Bearer {world['token']}"})


def test_the_events_route_returns_attendees_and_reminder(world):
    events = _get(world, f"/api/v1/recordings/{world['rec']}/events").get_json()["events"]
    ev = events[0]
    assert ev["reminder_minutes"] == 30 and ev["floating"] is True and ev["recording_id"] == world["rec"]
    assert ev["start_datetime"] == "2026-10-09T15:00:00"
    assert ev["attendees"] == [
        {"name": "Dana Lee", "email": None},
        {"name": None, "email": "omar@example.org"},
        {"name": "Priya Shah", "email": "priya@example.org"},
        {"name": "Lee", "email": "lee@example.org"},
        {"name": "Kim", "email": None},
    ]


def test_the_detail_embeds_identical_events(world):
    events = _get(world, f"/api/v1/recordings/{world['rec']}/events").get_json()["events"]
    detail = _get(world, f"/api/v1/recordings/{world['rec']}").get_json()["events"]
    assert detail == events


def _unfold(text):
    assert "\r\n" in text
    for raw in text.split("\r\n"):
        assert len(raw.encode("utf-8")) <= 75, raw
    return re.sub(r"\r\n[ \t]", "", text).split("\r\n")


def _unescape(value):
    return re.sub(r"\\([\\,;nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def test_ics_escapes_folds_and_lists_attendees(world):
    resp = _get(world, f"/api/v1/recordings/{world['rec']}/events/ics")
    assert resp.status_code == 200
    lines = _unfold(resp.get_data(as_text=True))
    props = {}
    for line in lines:
        name_params, _, value = line.partition(":")
        props.setdefault(name_params.split(";")[0], []).append((name_params, value))
    assert _unescape(props["SUMMARY"][0][1]) == "Budget review, Q4; draft"
    assert _unescape(props["LOCATION"][0][1]) == "Room 4, Building B"
    description = _unescape(props["DESCRIPTION"][0][1])
    assert description.startswith("Bring the numbers.\nAnd the slides; all of them.")
    assert description.endswith("Attendees: Dana Lee, Kim")
    attendees = sorted(v for k, v in props["ATTENDEE"])
    assert attendees == ["mailto:lee@example.org", "mailto:omar@example.org", "mailto:priya@example.org"]
    assert not any("example.com" in v for _, v in props["ATTENDEE"])
    assert any(k.startswith('ATTENDEE;CN="Priya Shah"') for k, _ in props["ATTENDEE"])
    assert props["DTSTAMP"][0][1].endswith("Z") and props["DTSTART"][0][1] == "20261009T150000"
    assert props["TRIGGER"][0][1] == "-PT30M"


def test_extraction_keeps_the_wall_clock_time(world):
    from unittest.mock import MagicMock, patch
    import src.tasks.processing as proc
    reply = json.dumps({"events": [{"title": "Call", "start_datetime": "2026-11-02T09:30:00-05:00",
                                    "end_datetime": "2026-11-02T10:00:00Z"}]})
    with app.app_context():
        Event.query.filter_by(recording_id=world["rec"]).delete()
        r = db.session.get(Recording, world["rec"])
        r.transcription = "Speaker 1: let us meet on Monday at nine thirty."
        db.session.get(User, world["user"]).extract_events = True
        db.session.commit()
        completion = MagicMock(choices=[MagicMock(message=MagicMock(content=reply, reasoning=None))])
        with patch.object(proc, "client", MagicMock()), patch.object(proc, "call_llm_completion", return_value=completion), \
                patch("src.services.webhook_dispatch.emit_webhook_event"):
            proc.extract_events_from_transcript(world["rec"], r.transcription, r.summary or "")
        ev = Event.query.filter_by(recording_id=world["rec"]).one()
        assert ev.start_datetime == datetime(2026, 11, 2, 9, 30) and ev.end_datetime == datetime(2026, 11, 2, 10, 0)
