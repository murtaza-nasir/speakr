"""UI themes chosen per account (src/config/ui_themes.py, docs/advanced/ui-themes.md).

The theme is saved with the account (User.ui_theme, null = the default),
written to localStorage by includes/theme_account.html and applied before the
first paint by includes/ui_theme.html. Only available themes are offered and
accepted; today that is the default alone, so nothing on the pages changes
until a second theme is made available.

SHARED-DB: the test users are removed afterwards.
"""

import json
import os
import re
import secrets
import sys

import pytest
from jinja2 import ChoiceLoader, DictLoader
from sqlalchemy import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.config import ui_themes
from src.models import User

app.config["WTF_CSRF_ENABLED"] = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
TW_LINK = "<link rel=\"stylesheet\" href=\"{{ url_for('static', filename='css/tailwind.css') }}\">"
UI_INCLUDE = "{% include 'includes/ui_theme.html' %}"


@pytest.fixture
def user():
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f"uitheme_{s}", email=f"uitheme_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        uid = u.id
    yield uid
    with app.app_context():
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


@pytest.fixture(autouse=True)
def only_default_available(monkeypatch):
    """These tests describe the registry as it was before any theme was ported:
    the default alone is available. Ported themes do not change that here."""
    themes = tuple(ui_themes.UiTheme(t.id, available=t.id == ui_themes.DEFAULT_THEME)
                   for t in ui_themes.UI_THEMES)
    monkeypatch.setattr(ui_themes, "UI_THEMES", themes)


@pytest.fixture
def classic_available(monkeypatch):
    """The classic theme offered, as it will be once it is ported."""
    themes = tuple(ui_themes.UiTheme(t.id, available=t.available or t.id == "classic")
                   for t in ui_themes.UI_THEMES)
    monkeypatch.setattr(ui_themes, "UI_THEMES", themes)


def _client(uid):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    return client


def _saved(html):
    m = re.search(r"var saved = (\{.*?\});", html)
    return json.loads(m.group(1)) if m else None


def _themes_json(html):
    m = re.search(r"var themes = (\{.*?\});", html)
    return json.loads(m.group(1)) if m else None


def _stored(uid):
    with app.app_context():
        return db.session.get(User, uid).ui_theme


# ---------------------------------------------------------------- registry


def test_registry_lists_the_six_themes_and_offers_only_the_default():
    assert [t.id for t in ui_themes.all_themes()] == ["default", "classic", "workspace", "focus", "desk", "cards"]
    assert [t.id for t in ui_themes.available_themes()] == ["default"]
    assert ui_themes.get_theme("default").stylesheet is None


@pytest.mark.parametrize("value", [None, "", "nope", "classic", 3, "<x>"])
def test_unknown_or_unavailable_values_resolve_to_the_default(value):
    assert ui_themes.resolve_theme(value) == "default"


def test_the_column_is_added_at_startup():
    with app.app_context():
        columns = {c["name"] for c in inspect(db.engine).get_columns("user")}
    assert "ui_theme" in columns


# ---------------------------------------------------------------- the setting


def test_null_means_the_default_theme(user):
    page = _client(user).get("/").get_data(as_text=True)
    assert _saved(page)["ui_theme"] is None
    assert _themes_json(page) == {"default": ""}
    assert _stored(user) is None


def test_the_theme_is_saved_and_returned_on_every_page(user):
    client = _client(user)
    resp = client.post("/api/user/preferences", json={"ui_theme": "default"})
    assert resp.status_code == 200
    assert _stored(user) == "default"
    for path in ("/", "/account"):
        assert _saved(client.get(path).get_data(as_text=True))["ui_theme"] == "default", path
    # Saving the theme leaves the other theme settings alone.
    with app.app_context():
        u = db.session.get(User, user)
        assert (u.theme_dark_mode, u.theme_color_scheme) == (None, None)


def test_null_goes_back_to_the_default(user):
    client = _client(user)
    client.post("/api/user/preferences", json={"ui_theme": "default"})
    assert client.post("/api/user/preferences", json={"ui_theme": None}).status_code == 200
    assert _stored(user) is None


@pytest.mark.parametrize("value", ["classic", "nope", "<x>", 3, ["default"], ""])
def test_unknown_and_unavailable_themes_are_refused(user, value):
    client = _client(user)
    resp = client.post("/api/user/preferences", json={"ui_theme": value})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_ui_theme"
    assert _stored(user) is None


def test_an_available_theme_is_accepted(user, classic_available):
    client = _client(user)
    assert client.post("/api/user/preferences", json={"ui_theme": "classic"}).status_code == 200
    assert _stored(user) == "classic"
    page = client.get("/").get_data(as_text=True)
    assert _saved(page)["ui_theme"] == "classic"
    assert set(_themes_json(page)) == {"default", "classic"}


def test_a_stored_theme_that_is_not_available_is_shown_as_the_default(user, classic_available):
    with app.app_context():
        db.session.get(User, user).ui_theme = "cards"
        db.session.commit()
    page = _client(user).get("/account").get_data(as_text=True)
    # The page script receives the stored id and falls back to the default
    # for an id that is not in its list of available themes.
    assert _saved(page)["ui_theme"] == "cards"
    assert "cards" not in _themes_json(page)
    assert 'data-ui-theme-choice="default" aria-pressed="true"' in page


# ---------------------------------------------------------------- before the first paint


def _pages():
    pages = []
    for dirpath, _, files in os.walk(TEMPLATES):
        for name in files:
            if name.endswith(".html"):
                text = open(os.path.join(dirpath, name), encoding="utf-8").read()
                if "</head>" in text:
                    pages.append((os.path.relpath(os.path.join(dirpath, name), TEMPLATES), text))
    return pages


@pytest.mark.parametrize("rel,text", _pages(), ids=[p for p, _ in _pages()])
def test_every_page_applies_the_theme_after_its_last_stylesheet(rel, text):
    head = text.split("</head>", 1)[0]
    assert head.count(UI_INCLUDE) == 1, rel
    # Right after the compiled stylesheet, so the theme's stylesheet comes last.
    assert head.index(UI_INCLUDE) > head.index(TW_LINK), rel
    between = head[head.index(TW_LINK) + len(TW_LINK):head.index(UI_INCLUDE)]
    assert between.strip() == "", rel


def test_the_pre_paint_script_sets_the_attribute_and_the_stylesheet():
    script = open(os.path.join(TEMPLATES, "includes", "ui_theme.html"), encoding="utf-8").read()
    assert "root.setAttribute('data-ui-theme', id)" in script
    assert "link.setAttribute('href', href)" in script
    assert "localStorage.getItem('uiTheme')" in script
    assert "window.speakrApplyUiTheme" in script


@pytest.mark.parametrize("path", ["/", "/account"])
def test_signed_in_pages_set_the_theme_before_the_body(user, path):
    page = _client(user).get(path).get_data(as_text=True)
    head = page.split("</head>", 1)[0]
    assert "var themes = " in head and "speakrApplyUiTheme" in head
    assert head.index("/static/css/tailwind.css") < head.index("var themes = ")
    # The account's saved value is put in localStorage before the theme reads it.
    assert head.index("var saved =") < head.index("var themes = ")


def test_signed_out_pages_set_the_theme_too():
    page = app.test_client().get("/login").get_data(as_text=True)
    head = page.split("</head>", 1)[0]
    assert "var themes = " in head
    # No account: nothing is saved (includes/theme_account.html is not there).
    assert "window.speakrSaveTheme =" not in page and "var saved =" not in page


def test_an_available_theme_with_a_stylesheet_is_linked(user, classic_available, monkeypatch):
    monkeypatch.setattr(ui_themes.UiTheme, "stylesheet",
                        property(lambda self: "css/themes/classic.css" if self.id == "classic" else None))
    page = _client(user).get("/").get_data(as_text=True)
    assert _themes_json(page) == {"default": "", "classic": "/static/css/themes/classic.css"}


# ---------------------------------------------------------------- the picker


def test_no_picker_while_only_the_default_is_available(user):
    client = _client(user)
    account = client.get("/account").get_data(as_text=True)
    assert 'id="account-appearance"' not in account
    assert 'data-ui-theme-choice="' not in account
    assert "ui-theme-card" not in client.get("/").get_data(as_text=True)


def test_the_picker_shows_only_available_themes(user, classic_available):
    client = _client(user)
    account = client.get("/account").get_data(as_text=True)
    assert 'id="account-appearance"' in account
    assert re.findall(r'data-ui-theme-choice="([^"]+)"', account) == ["default", "classic"]
    # The default is selected while nothing is saved.
    assert 'data-ui-theme-choice="default" aria-pressed="true"' in account
    assert 'data-ui-theme-choice="classic" aria-pressed="false"' in account
    assert "/static/img/themes/default.webp" in account
    assert "Classic" in account and "workspace" not in account.split('id="account-appearance"', 1)[1].split("</section>", 1)[0]
    assert "?open=appearance" in account
    # In the main app's appearance dialog the cards follow the reactive value.
    index = client.get("/").get_data(as_text=True)
    assert "@click=\"setUiTheme('classic')\"" in index
    assert ":aria-pressed=\"uiTheme === 'classic' ? 'true' : 'false'\"" in index


def test_the_picker_text_follows_the_interface_language(user, classic_available):
    with app.app_context():
        db.session.get(User, user).ui_language = "de"
        db.session.commit()
    account = _client(user).get("/account").get_data(as_text=True)
    assert "Klassisch" in account and "Darstellung" in account


# ---------------------------------------------------------------- slots in a page


def test_slots_render_the_default_markup_alone_without_overrides(user):
    page = _client(user).get("/").get_data(as_text=True)
    assert "uiTheme ===" not in page
    # The default partials are in the page.
    assert "handleRecordingClick(recording, $event, visibleRecordingIds) || selectRecording(recording)" in page
    assert "data-header-folder-toggle" in page


def test_a_theme_override_becomes_a_branch_switched_by_uiTheme(user, classic_available, monkeypatch):
    original = app.jinja_env.loader
    override = '<div class="classic-row-marker" @click="selectRecording(recording)">x</div>'
    monkeypatch.setattr(app.jinja_env, "loader", ChoiceLoader([
        DictLoader({"themes/classic/sidebar-row.html": override}), original]))
    real = ui_themes.slot_overrides
    monkeypatch.setattr(ui_themes, "slot_overrides",
                        lambda slot: ["classic"] if slot == "sidebar-row" else real(slot))
    app.jinja_env.cache.clear()
    try:
        page = _client(user).get("/").get_data(as_text=True)
    finally:
        app.jinja_env.cache.clear()
    branch = page.index("<template v-if=\"uiTheme === 'classic'\">")
    default = page.index("<template v-else>", branch)
    assert branch < page.index("classic-row-marker") < default
    assert default < page.index("handleRecordingClick(recording, $event, visibleRecordingIds)")
    assert page.count("<template v-if=\"uiTheme ===") == 1
