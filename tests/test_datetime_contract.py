"""Characterization tests: the machine-facing datetime formats (#412 safety net).

Datetimes are stored as naive UTC. The API returns them as naive ISO strings
without a zone suffix (the browser appends Z before converting to local time),
and webhook envelopes carry an explicit Z. The per-user timezone work for #412
changes only human-facing text, so every format checked here must stay exactly
as it is, also when the container's own timezone (TZ) is not UTC.

SHARED-DB: the test user, recording and token are removed afterwards.
"""

import os
import secrets
import sys
import time
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, Recording, User
from src.services.webhook_dispatch import _build_envelope
from src.utils.token_auth import hash_token

MEETING = datetime(2026, 3, 4, 23, 30, 15)      # late evening UTC: a different day east of UTC
CREATED = datetime(2026, 3, 5, 6, 13, 46, 123456)
COMPLETED = datetime(2026, 3, 5, 6, 20, 1)


@pytest.fixture
def rec():
    with app.app_context():
        name = f"dtcontract_{secrets.token_hex(4)}"
        user = User(username=name, email=f"{name}@local.test")
        db.session.add(user)
        db.session.commit()
        r = Recording(user_id=user.id, title="Contract", status="COMPLETED", original_filename="c.wav",
                      meeting_date=MEETING, created_at=CREATED, completed_at=COMPLETED)
        db.session.add(r)
        db.session.commit()
        plain = f"test-token-{secrets.token_urlsafe(16)}"
        token = APIToken(user_id=user.id, token_hash=hash_token(plain), name="dtcontract")
        db.session.add(token)
        db.session.commit()
        ids = (token.id, r.id, user.id)
    yield ids, plain
    with app.app_context():
        for model, oid in zip((APIToken, Recording, User), ids):
            obj = db.session.get(model, oid)
            if obj is not None:
                db.session.delete(obj)
        db.session.commit()


@pytest.fixture(params=["UTC", "Asia/Tokyo", "America/Los_Angeles"])
def container_tz(request):
    """Run with the process timezone set as a container TZ would set it."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_recording_to_dict_formats(rec, container_tz):
    (_, rid, _), _ = rec
    with app.app_context():
        d = db.session.get(Recording, rid).to_dict()
        assert d["meeting_date"] == "2026-03-04T23:30:15"
        assert d["created_at"] == "2026-03-05T06:13:46.123456"
        assert d["completed_at"] == "2026-03-05T06:20:01"


def test_recording_to_list_dict_formats(rec, container_tz):
    (_, rid, _), _ = rec
    with app.app_context():
        d = db.session.get(Recording, rid).to_list_dict()
        assert d["meeting_date"] == "2026-03-04T23:30:15"
        assert d["created_at"] == "2026-03-05T06:13:46.123456"


def test_api_v1_recording_formats(rec, container_tz):
    (_, rid, _), plain = rec
    with app.test_client() as c:
        resp = c.get(f"/api/v1/recordings/{rid}", headers={"Authorization": f"Bearer {plain}"})
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        body = resp.get_json()
        body = body.get("recording", body)
        assert body["meeting_date"] == "2026-03-04T23:30:15"
        assert body["created_at"] == "2026-03-05T06:13:46.123456"


def test_webhook_envelope_timestamp_is_utc_with_z(container_tz):
    before = datetime.utcnow().replace(microsecond=0)
    env = _build_envelope("evt", "recording.completed", 1, {"id": 1})
    ts = env["timestamp"]
    assert ts.endswith("Z") and "+" not in ts
    stamped = datetime.fromisoformat(ts[:-1])
    assert abs((stamped - before).total_seconds()) < 60
