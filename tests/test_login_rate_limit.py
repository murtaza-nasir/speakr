"""The sign-in form is limited to ten requests a minute per address.

The suite runs with RATELIMIT_ENABLED=false (conftest.py), and so does the
instance of the browser tests (tests/e2e/run_speakr.py), so this test puts an
enabled limiter in place of the disabled one and checks that /login answers
429 after ten requests from one address while another address still gets the
form. The production limit itself is the decorator's string on the view.
"""

from unittest.mock import patch

import pytest
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import src.api.auth as auth_mod
from src.app import app


@pytest.fixture
def live_limiter():
    live = Limiter(get_remote_address, storage_uri="memory://", enabled=True)
    live.init_app(Flask("login-limiter-host"))
    view = app.view_functions["auth.login"]
    # The decorator applies the limiter on the first limited request and
    # keeps it: start this test from a view that has not been limited yet.
    state = next(cell.cell_contents for cell in view.__closure__
                 if isinstance(cell.cell_contents, dict) and "limited" in cell.cell_contents)
    saved = state["limited"]
    state["limited"] = None
    try:
        with patch.object(auth_mod, "limiter", live):
            yield live
    finally:
        state["limited"] = saved


def test_the_login_view_carries_the_production_limit():
    assert app.view_functions["auth.login"]._rate_limit == "10 per minute"


def test_the_eleventh_request_in_a_minute_from_one_address_is_refused(live_limiter):
    client = app.test_client()
    here = {"REMOTE_ADDR": "203.0.113.7"}
    codes = [client.get("/login", environ_overrides=here).status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]
    # Another address has a budget of its own
    assert app.test_client().get("/login", environ_overrides={"REMOTE_ADDR": "203.0.113.8"}).status_code == 200
