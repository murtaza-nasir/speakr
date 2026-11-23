"""A write with an API token that is not valid answers 401, not 400 CSRF (#672).

A token in the Authorization, X-API-Token or API-Token header marks a
programmatic client; when it is unknown the answer is 401 with
WWW-Authenticate. CSRF is not weakened: a request without credentials (a
login form among them) and a request with a browser session still get
the CSRF check. Also the startup line about in-memory rate limits (#678).
"""

import logging
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import app as app_module
from src.app import app


@pytest.fixture
def csrf_on():
    saved = app.config.get("WTF_CSRF_ENABLED", True)
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        yield
    finally:
        app.config["WTF_CSRF_ENABLED"] = saved


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer not-a-real-token"},
    {"X-API-Token": "not-a-real-token"},
    {"API-Token": "not-a-real-token"},
], ids=["bearer", "x-api-token", "api-token"])
@pytest.mark.parametrize("method,path", [("POST", "/api/tags"), ("POST", "/speakers"),
                                         ("PATCH", "/api/v1/recordings/1"), ("DELETE", "/api/recordings/bulk")])
def test_an_invalid_header_token_gets_401(csrf_on, headers, method, path):
    resp = app.test_client().open(path, method=method, headers=headers, json={"name": "x"})
    assert resp.status_code == 401, resp.get_data(as_text=True)[:200]
    assert resp.is_json and resp.get_json()["code"] == "authentication_required"
    assert resp.headers["WWW-Authenticate"].startswith("Bearer")


def test_a_login_form_without_credentials_keeps_csrf(csrf_on):
    resp = app.test_client().post("/login", data={"email": "a@b.c", "password": "x"})
    assert resp.status_code == 400


def test_a_post_without_credentials_keeps_csrf(csrf_on):
    resp = app.test_client().post("/api/tags", json={"name": "x"})
    assert resp.status_code == 400


# ------------------------------------------------------------ #678

@pytest.mark.parametrize("argv,env,expected", [
    (["/usr/local/bin/gunicorn", "--workers", "3", "src.app:app"], {}, 3),
    (["gunicorn", "-w", "4", "src.app:app"], {}, 4),
    (["gunicorn", "--workers=2", "src.app:app"], {}, 2),
    (["gunicorn", "-w5", "src.app:app"], {}, 5),
    (["gunicorn", "src.app:app"], {"GUNICORN_CMD_ARGS": "--workers 6"}, 6),
    (["gunicorn", "src.app:app"], {"WEB_CONCURRENCY": "7"}, 7),
    (["gunicorn", "src.app:app"], {}, None),
    (["python", "-m", "pytest"], {"WEB_CONCURRENCY": "7"}, None),
])
def test_the_worker_count_is_read_from_the_command_line(argv, env, expected):
    clean = {k: v for k, v in os.environ.items() if k not in ("GUNICORN_CMD_ARGS", "WEB_CONCURRENCY")}
    with patch.object(sys, "argv", argv), patch.dict(os.environ, {**clean, **env}, clear=True):
        assert app_module._gunicorn_worker_count() == expected


def _log(argv, storage=None, enabled=True):
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Grab()
    app.logger.addHandler(handler)
    saved_level = app.logger.level
    app.logger.setLevel(logging.DEBUG)
    saved = app.config.get("RATELIMIT_STORAGE_URI")
    try:
        if storage:
            app.config["RATELIMIT_STORAGE_URI"] = storage
        else:
            app.config.pop("RATELIMIT_STORAGE_URI", None)
        with patch.object(sys, "argv", argv), patch.object(app_module, "_RATELIMIT_ENABLED", enabled), \
                patch.dict(os.environ, {"GUNICORN_CMD_ARGS": "", "WEB_CONCURRENCY": ""}):
            app_module._log_rate_limit_storage()
    finally:
        app.logger.removeHandler(handler)
        app.logger.setLevel(saved_level)
        if saved is None:
            app.config.pop("RATELIMIT_STORAGE_URI", None)
        else:
            app.config["RATELIMIT_STORAGE_URI"] = saved
    return [(r.levelno, r.getMessage()) for r in records if "Rate limits" in r.getMessage()]


def test_several_workers_without_shared_storage_log_a_warning():
    logged = _log(["gunicorn", "--workers", "3", "src.app:app"])
    assert len(logged) == 1 and logged[0][0] == logging.WARNING
    assert "3 gunicorn workers" in logged[0][1] and "RATELIMIT_STORAGE_URI" in logged[0][1]


def test_no_line_with_shared_storage_one_worker_or_limits_off():
    assert _log(["gunicorn", "--workers", "3", "src.app:app"], storage="redis://r:6379") == []
    assert _log(["gunicorn", "--workers", "1", "src.app:app"]) == []
    assert _log(["gunicorn", "--workers", "3", "src.app:app"], enabled=False) == []
    assert _log(["python", "-m", "pytest"]) == []


def test_an_unknown_worker_count_logs_info():
    logged = _log(["gunicorn", "-c", "gunicorn.conf.py", "src.app:app"])
    assert len(logged) == 1 and logged[0][0] == logging.INFO
