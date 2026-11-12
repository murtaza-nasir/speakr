"""The old chat timestamp settings are gone from the Account page.

Segment citations replaced them. The columns stay, and a saved value is kept
for the non-segment fallback of the recording chat.
"""
import pytest

from src.app import app, db
from src.models import User


@pytest.fixture
def user():
    with app.app_context():
        u = User(username="chatts_user", email="chatts@local.test", password="x",
                 chat_include_timestamps=True)
        db.session.add(u)
        db.session.commit()
        yield u
        db.session.delete(u)
        db.session.commit()


def _client(user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return client


def test_account_page_has_no_chat_timestamp_controls(user):
    page = _client(user).get("/account").get_data(as_text=True)
    assert 'name="summary_include_timestamps"' in page
    assert "chat_include_timestamps" not in page
    assert "chat_timestamp_template_id" not in page


def test_saving_prompt_options_keeps_the_stored_chat_setting(user):
    _client(user).post("/account", data={"summary_prompt": "", "summary_include_timestamps": "on"})
    db.session.expire_all()
    saved = db.session.get(User, user.id)
    assert saved.summary_include_timestamps is True
    assert saved.chat_include_timestamps is True
