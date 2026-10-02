"""Per-user timezone for text the server writes (#412).

The issue: with a naming template such as "{{datetime}} <label>", a recording
made at 09:13 local time (UTC+3, stored as 06:13 UTC) was titled 06:13. Titles
now use the owner's zone: the browser's zone in 'auto' mode, a zone chosen in
Account settings in 'fixed' mode, else the admin default. Stored data and API
output stay UTC (tests/test_datetime_contract.py).

SHARED-DB: users, recordings and the admin setting are restored afterwards.
"""

import os
import sys
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from flask import g, has_app_context
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import NamingTemplate, Recording, SystemSetting, User
from src.services.titling import compute_title
from src.tasks import processing
from src.utils import timezones as tz

app.config["WTF_CSRF_ENABLED"] = False


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        if has_app_context():
            g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def admin_default():
    """Pin the admin default for the test, restore it afterwards."""
    with app.app_context():
        old = SystemSetting.get_setting(tz.DEFAULT_SETTING_KEY, None)

    def set_(value):
        with app.app_context():
            SystemSetting.set_setting(tz.DEFAULT_SETTING_KEY, value, setting_type='string')
            db.session.commit()
    set_('UTC')
    yield set_
    with app.app_context():
        if old is None:
            SystemSetting.query.filter_by(key=tz.DEFAULT_SETTING_KEY).delete()
            db.session.commit()
        else:
            set_(old)


@pytest.fixture
def made():
    ids = []
    yield ids
    with app.app_context():
        db.session.rollback()
        for model, oid in reversed(ids):
            obj = db.session.get(model, oid)
            if obj is not None:
                if isinstance(obj, User):
                    obj.default_naming_template_id = None
                    db.session.commit()
                db.session.delete(obj)
        db.session.commit()


def _user(made, **kw):
    s = uuid.uuid4().hex[:8]
    u = User(username=f"tz_{s}", email=f"tz_{s}@local.test", password="x", **kw)
    db.session.add(u)
    db.session.commit()
    made.append((User, u.id))
    return u


def _client(user_id):
    c = _Client(app, app.response_class, use_cookies=True)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
    return c


# ------------------------------------------------------------------ helpers

def test_to_local_converts_naive_utc_and_handles_dst():
    assert tz.to_local(datetime(2026, 10, 1, 6, 13), "Europe/Istanbul") == datetime(2026, 10, 1, 9, 13)
    assert tz.to_local(datetime(2026, 1, 15, 17, 0), "America/New_York") == datetime(2026, 1, 15, 12, 0)   # EST
    assert tz.to_local(datetime(2026, 7, 15, 17, 0), "America/New_York") == datetime(2026, 7, 15, 13, 0)   # EDT
    assert tz.to_local(None, "UTC") is None


@pytest.mark.parametrize("name,ok", [("Europe/Istanbul", True), ("UTC", True), ("Mars/Olympus", False),
                                     ("", False), (None, False), ("../../etc/passwd", False)])
def test_is_valid_timezone(name, ok):
    assert tz.is_valid_timezone(name) is ok


def test_user_zone_falls_back_to_the_admin_default(made, admin_default):
    admin_default("Asia/Tokyo")
    with app.app_context():
        assert tz.user_timezone(_user(made)) == "Asia/Tokyo"
        assert tz.user_timezone(_user(made, timezone="Europe/Istanbul")) == "Europe/Istanbul"
        assert tz.user_timezone(_user(made, timezone="Bogus/Zone")) == "Asia/Tokyo"


def test_browser_zone_is_saved_in_auto_mode_only(made):
    with app.app_context():
        auto = _user(made, timezone_mode="auto")
        fixed = _user(made, timezone="Europe/Berlin", timezone_mode="fixed")
        assert tz.record_browser_timezone(auto, "Europe/Istanbul") is True
        assert tz.record_browser_timezone(fixed, "Europe/Istanbul") is False
        assert tz.record_browser_timezone(auto, "Not/AZone") is False
        assert (auto.timezone, fixed.timezone) == ("Europe/Istanbul", "Europe/Berlin")


# -------------------------------------------------------------- titles (#412)

def _templated(made, user, template):
    t = NamingTemplate(user_id=user.id, name="tz", template=template)
    db.session.add(t)
    db.session.commit()
    made.append((NamingTemplate, t.id))
    user.default_naming_template_id = t.id
    db.session.commit()
    r = Recording(user_id=user.id, title="Recording - a.wav", original_filename="a.wav", status="PROCESSING",
                  transcription="Speaker 1: hello there everyone, let us start the meeting.",
                  meeting_date=datetime(2026, 10, 1, 6, 13, 46))
    db.session.add(r)
    db.session.commit()
    made.append((Recording, r.id))
    return r


def test_issue_412_title_uses_the_owner_local_time(made, admin_default):
    with app.app_context():
        u = _user(made, timezone="Europe/Istanbul")
        r = _templated(made, u, "{{datetime}} standup")
        with patch.object(processing, "client", None):
            assert compute_title(r) == "2026-10-01 09:13 standup"


def test_without_any_zone_titles_stay_utc_as_before(made, admin_default):
    with app.app_context():
        u = _user(made)
        r = _templated(made, u, "{{date}} {{time}}")
        with patch.object(processing, "client", None):
            assert compute_title(r) == "2026-10-01 06:13"


def test_admin_default_applies_to_users_without_a_zone(made, admin_default):
    admin_default("America/Los_Angeles")
    with app.app_context():
        u = _user(made)
        r = _templated(made, u, "{{datetime}}")
        with patch.object(processing, "client", None):
            assert compute_title(r) == "2026-09-30 23:13"


def test_template_preview_default_is_now_in_the_user_zone(made, admin_default):
    with app.app_context():
        u = _user(made, timezone="Asia/Kolkata")
        t = NamingTemplate(user_id=u.id, name="p", template="{{time}}")
        db.session.add(t)
        db.session.commit()
        made.append((NamingTemplate, t.id))
        c = _client(u.id)
        expected = tz.now_local("Asia/Kolkata").strftime("%H:%M")
        resp = c.post(f"/api/naming-templates/{t.id}/test", json={})
        assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
        assert resp.get_json()["result"] in (expected, tz.now_local("Asia/Kolkata").strftime("%H:%M"))


# --------------------------------------------------------------- endpoints

def test_timezone_endpoint_saves_the_browser_zone(made):
    with app.app_context():
        u = _user(made)
        uid = u.id
    c = _client(uid)
    resp = c.post("/api/user/timezone", json={"timezone": "Europe/Istanbul"})
    assert resp.status_code == 200 and resp.get_json() == {"timezone": "Europe/Istanbul", "mode": "auto"}
    resp = c.post("/api/user/timezone", json={"timezone": "No/Such"})
    assert resp.get_json()["timezone"] == "Europe/Istanbul"
    with app.app_context():
        assert db.session.get(User, uid).timezone == "Europe/Istanbul"


def test_account_form_fixes_or_releases_the_zone(made):
    with app.app_context():
        uid = _user(made).id
    c = _client(uid)
    base = {"preferences_form": "1", "ui_language": "en"}
    c.post("/account", data={**base, "timezone": "America/Chicago", "detected_timezone": "Europe/Istanbul"})
    with app.app_context():
        u = db.session.get(User, uid)
        assert (u.timezone, u.timezone_mode) == ("America/Chicago", "fixed")
    # a fixed zone is not replaced by the browser
    c.post("/api/user/timezone", json={"timezone": "Europe/Istanbul"})
    with app.app_context():
        assert db.session.get(User, uid).timezone == "America/Chicago"
    c.post("/account", data={**base, "timezone": "auto", "detected_timezone": "Europe/Istanbul"})
    with app.app_context():
        u = db.session.get(User, uid)
        assert (u.timezone, u.timezone_mode) == ("Europe/Istanbul", "auto")


def test_admin_default_timezone_is_validated(made, admin_default):
    with app.app_context():
        uid = _user(made, is_admin=True).id
    c = _client(uid)
    bad = c.post("/admin/settings", json={"key": "default_timezone", "value": "Mars/Olympus", "setting_type": "string"})
    assert bad.status_code == 400
    ok = c.post("/admin/settings", json={"key": "default_timezone", "value": "Europe/Berlin", "setting_type": "string"})
    assert ok.status_code == 200, ok.get_data(as_text=True)[:200]
    with app.app_context():
        assert tz.deployment_timezone() == "Europe/Berlin"
