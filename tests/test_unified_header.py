"""Every page shows the same header controls and menu.

The controls on the right of the header are one template,
components/header-controls.html. The main app renders it in its own Vue app;
account, admin and group management render it through page-header.html and
static/js/global-header.js; Inquire includes it in its own header. These tests
pin that every page carries the shared menu and none keeps an old copy.

SHARED-DB: the test users are removed afterwards.
"""

import os
import re
import sys
import uuid

import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User

app.config["WTF_CSRF_ENABLED"] = False

# Markers only the shared template has.
SHARED_MARKERS = ('data-notifications-toggle', 'openSharesList', 'openColorSchemeModal', 'switchToUploadView')


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


@pytest.fixture
def admin_client():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"uh_{s}", email=f"uh_{s}@local.test", password="x", is_admin=True)
        db.session.add(user)
        db.session.commit()
        client = _Client(app, app.response_class)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield client
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()


@pytest.mark.parametrize("path", ["/", "/account", "/admin"])
def test_each_page_renders_the_shared_header_controls(admin_client, path):
    html = admin_client.get(path).get_data(as_text=True)
    for marker in SHARED_MARKERS:
        assert marker in html, (path, marker)
    buttons = re.findall(r'<button[^>]*data-user-menu-toggle', html)
    assert len(buttons) == 1, (path, len(buttons))


@pytest.mark.parametrize("path", ["/account", "/admin"])
def test_pages_outside_the_main_app_mount_the_header_app_and_drop_the_old_controls(admin_client, path):
    html = admin_client.get(path).get_data(as_text=True)
    assert 'id="global-header"' in html
    assert 'js/global-header.js' in html
    assert 'id="darkModeToggle"' not in html and 'id="userDropdownButton"' not in html


def test_the_inquire_page_uses_the_shared_controls(admin_client):
    from unittest.mock import patch
    with patch("src.app.ENABLE_INQUIRE_MODE", True):
        response = admin_client.get("/inquire")
    if response.status_code != 200:
        pytest.skip("Inquire mode is not available in this configuration")
    html = response.get_data(as_text=True)
    for marker in SHARED_MARKERS:
        assert marker in html, marker


def test_the_page_label_has_no_fallback_text_inside_v_text(admin_client):
    """Vue refuses to compile an element with both v-text and children, and
    the header app then renders nothing."""
    html = admin_client.get("/account").get_data(as_text=True)
    assert "v-text=\"t('nav.account')\"></span>" in html


def test_the_loading_overlay_follows_the_theme_and_waits_before_showing(admin_client):
    html = admin_client.get("/account").get_data(as_text=True)
    assert "background: var(--bg-primary" in html
    assert "app-loading-appear" in html
