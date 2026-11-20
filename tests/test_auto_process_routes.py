"""The auto-process admin routes call the real file monitor factory (#652).

tests/test_cov_admin.py replaces the factory with a mock, which is how a
call without its app argument went unnoticed."""
import pytest

import src.file_monitor as file_monitor
from tests.test_cov_admin import admin_client, admin_user, ctx  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _monitor_off(monkeypatch):
    monkeypatch.setenv("ENABLE_AUTO_PROCESSING", "false")
    monkeypatch.setattr(file_monitor, "file_monitor", None)


def test_status_answers_with_the_monitor_state(admin_client):
    resp = admin_client.get("/admin/auto-process/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == {"running": False}
    assert body["config"]["enabled"] is False


@pytest.mark.parametrize("path", ["/admin/auto-process/start", "/admin/auto-process/stop"])
def test_start_and_stop_reach_the_monitor(admin_client, path):
    resp = admin_client.post(path)
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
