"""The UI themes as they ship: all six are offered (src/config/ui_themes.py).

tests/test_ui_themes.py pins the registry to the default alone, so its tests
describe the mechanism independently of which themes are ported. These tests
use the registry unchanged.

SHARED-DB: the test user is removed afterwards.
"""
import json
import os
import re
import secrets

import pytest

from src.app import app, db
from src.config import ui_themes
from src.config.ui_themes import STATIC_DIR
from src.models import User

app.config["WTF_CSRF_ENABLED"] = False

SHIPPED = ["default", "classic", "workspace", "focus", "desk", "cards"]


@pytest.fixture
def user():
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f"uiship_{s}", email=f"uiship_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _client(uid):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return client


def test_all_six_themes_are_offered():
    assert [t.id for t in ui_themes.UI_THEMES if t.available] == SHIPPED


@pytest.mark.parametrize("theme_id", SHIPPED[1:])
def test_every_offered_theme_has_a_stylesheet_and_a_preview(theme_id):
    theme = next(t for t in ui_themes.UI_THEMES if t.id == theme_id)
    assert theme.stylesheet == f"css/themes/{theme_id}.css"
    assert os.path.isfile(os.path.join(STATIC_DIR, theme.preview))


@pytest.mark.parametrize("theme_id", SHIPPED[1:])
def test_every_rule_of_a_theme_is_scoped_to_that_theme(theme_id):
    css = open(os.path.join(STATIC_DIR, "css", "themes", f"{theme_id}.css"), encoding="utf-8").read()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    scope = f'html[data-ui-theme="{theme_id}"]'
    # Selectors of style rules: the text before each "{" that is not an at-rule.
    for selector in re.findall(r"([^{}]+)\{", css):
        selector = selector.strip()
        if not selector or selector.startswith("@") or re.fullmatch(r"(from|to|[\d.]+%)(\s*,\s*[\d.]+%)*", selector):
            continue
        for part in _top_level_parts(selector):
            assert part.strip().startswith(scope), f"{theme_id}.css: unscoped selector {part.strip()!r}"


def _top_level_parts(selector):
    """Split a selector list on its commas, but not the commas inside :is() or :has()."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(selector):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(selector[start:i])
            start = i + 1
    parts.append(selector[start:])
    return parts


# Every page header: the main app's (and the share page's), Ask Ambrose's, and
# the account, admin and group pages' page-header.html.
EVERY_PAGE_HEADER = ":is(#app > header, #inquire-app > header, #global-header)"


@pytest.mark.parametrize("theme_id", SHIPPED)
def test_the_header_controls_are_styled_on_every_page_header(theme_id):
    """A rule for the shared header controls (components/header-controls.html)
    scoped to #app > header alone styles them on the recordings page only, and
    Ask Ambrose and the account pages fall back to the unstyled buttons."""
    name = "styles.css" if theme_id == "default" else f"themes/{theme_id}.css"
    css = open(os.path.join(STATIC_DIR, "css", name), encoding="utf-8").read()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for selector in re.findall(r"([^{}]+)\{", css):
        for part in _top_level_parts(selector):
            part = part.strip()
            if "header-action" not in part and "data-user-menu-toggle" not in part:
                continue
            if "#app" not in part or EVERY_PAGE_HEADER in part:
                continue
            # The recordings page's own controls: the detail view's header and
            # the New button that classic moves into the rail.
            if "app-header--detail" in part or "btn--primary.header-action" in part:
                continue
            raise AssertionError(f"{name}: header controls styled on the recordings page only: {part!r}")


def test_the_picker_offers_all_six(user):
    client = _client(user)
    account = client.get("/account").get_data(as_text=True)
    assert re.findall(r'data-ui-theme-choice="([^"]+)"', account) == SHIPPED
    index = client.get("/").get_data(as_text=True)
    for theme_id in SHIPPED:
        assert f"@click=\"setUiTheme('{theme_id}')\"" in index


@pytest.mark.parametrize("theme_id", SHIPPED[1:])
def test_a_saved_theme_is_returned_on_every_page(user, theme_id):
    client = _client(user)
    resp = client.post("/api/user/preferences", json={"ui_theme": theme_id})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    for path in ("/", "/account"):
        saved = re.search(r"var saved = (\{.*?\});", client.get(path).get_data(as_text=True))
        assert saved and json.loads(saved.group(1))["ui_theme"] == theme_id, path
