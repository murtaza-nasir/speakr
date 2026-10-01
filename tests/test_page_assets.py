"""Pages load the compiled Tailwind stylesheet and embed their translations.

Tailwind CSS used to be generated in the browser by the Play CDN script
(vendor/js/tailwind.min.js), so every page was painted unstyled first and
restyled once the script had run. The stylesheet is now compiled at build
time (scripts/build_css.sh, the Dockerfile css-stage) and linked in <head>.
Translations for i18n.js are embedded in the page
(templates/includes/i18n_bootstrap.html), so the first render shows text and
not translation keys.

SHARED-DB: the test users are removed afterwards.
"""

import os
import re
import sys
import uuid
from unittest.mock import patch

import pytest
from flask import g
from flask.testing import FlaskClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.app import app, db
from src.models import User
from src.utils.i18n_bootstrap import i18n_bootstrap

app.config["WTF_CSRF_ENABLED"] = False

TEMPLATES = os.path.join(ROOT, "templates")
CSS_LINK = "filename='css/tailwind.css'"
I18N_SCRIPT = "filename='js/i18n.js'"
BOOTSTRAP_INCLUDE = "{% include 'includes/i18n_bootstrap.html' %}"


def _page_templates():
    """Full pages: templates with their own <head>."""
    pages = []
    for dirpath, _, files in os.walk(TEMPLATES):
        for name in files:
            if name.endswith(".html"):
                path = os.path.join(dirpath, name)
                if "</head>" in open(path, encoding="utf-8").read():
                    pages.append(os.path.relpath(path, TEMPLATES))
    return sorted(pages)


PAGES = _page_templates()


def _read(rel):
    return open(os.path.join(TEMPLATES, rel), encoding="utf-8").read()


def test_page_templates_are_found():
    assert {"index.html", "account.html", "admin.html", "inquire.html", "login.html", "share.html"} <= set(PAGES)


@pytest.mark.parametrize("rel", PAGES)
def test_each_page_links_the_compiled_stylesheet_last_in_head(rel):
    text = _read(rel)
    assert "tailwind.min.js" not in text and "tailwind.config" not in text, rel
    head = text.split("</head>", 1)[0]
    assert head.count(CSS_LINK) == 1, rel
    # Last stylesheet in <head>, as the runtime-generated styles were, so the
    # cascade order against the page's own styles is unchanged.
    after = head.split(CSS_LINK, 1)[1]
    assert "<link" not in after and "<style" not in after, rel


@pytest.mark.parametrize("rel", [p for p in PAGES if I18N_SCRIPT in _read(p)])
def test_pages_that_load_i18n_embed_the_translations_first(rel):
    text = _read(rel)
    assert text.count(BOOTSTRAP_INCLUDE) == 1, rel
    assert text.index(BOOTSTRAP_INCLUDE) < text.index(I18N_SCRIPT), rel


def test_no_code_references_the_play_cdn_build():
    for top in ("templates", os.path.join("static", "js"), os.path.join("static", "sw.js")):
        target = os.path.join(ROOT, top)
        paths = [target] if os.path.isfile(target) else [
            os.path.join(d, f) for d, _, fs in os.walk(target) for f in fs if f.endswith((".html", ".js"))]
        for path in paths:
            assert "tailwind.min.js" not in open(path, encoding="utf-8").read(), path


def test_the_service_worker_caches_the_compiled_stylesheet():
    text = open(os.path.join(ROOT, "static", "sw.js"), encoding="utf-8").read()
    assert "'/static/css/tailwind.css'" in text


def test_compiled_stylesheet_has_the_dynamic_error_colors():
    """Runs where the stylesheet has been built (CI builds it first)."""
    path = os.path.join(ROOT, "static", "css", "tailwind.css")
    if not os.path.exists(path):
        pytest.skip("static/css/tailwind.css is not built; run scripts/build_css.sh")
    css = open(path, encoding="utf-8").read()
    for cls in (r".bg-amber-500\/10", r".border-indigo-500\/30", r".dark\:text-pink-400", r".text-\[var\(--text-primary\)\]"):
        assert cls in css, cls


# --- i18n_bootstrap() --------------------------------------------------------

def test_bootstrap_has_the_locale_and_english():
    data = i18n_bootstrap("de")
    assert data["locale"] == "de"
    assert set(data["translations"]) == {"de", "en"}
    assert data["translations"]["de"]["nav"]["account"] != data["translations"]["en"]["nav"]["account"]


def test_bootstrap_for_english_has_one_table():
    data = i18n_bootstrap("en")
    assert data == {"locale": "en", "translations": {"en": data["translations"]["en"]}}


@pytest.mark.parametrize("locale", ["xx", "../en", "en/../de", "", None])
def test_bootstrap_for_an_unknown_or_invalid_locale_is_english_only(locale):
    with app.test_request_context():
        data = i18n_bootstrap(locale)
    assert data["locale"] == "en"
    assert list(data["translations"]) == ["en"]


# --- Rendered pages -----------------------------------------------------------

class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


@pytest.fixture
def german_client():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"pa_{s}", email=f"pa_{s}@local.test", password="x", is_admin=True, ui_language="de")
        db.session.add(user)
        db.session.commit()
        client = _Client(app, app.response_class)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield client
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()


@pytest.mark.parametrize("path", ["/", "/account", "/admin", "/inquire"])
def test_rendered_pages_embed_the_users_language_before_i18n(german_client, path):
    with patch("src.app.ENABLE_INQUIRE_MODE", True), patch("src.api.inquire.ENABLE_INQUIRE_MODE", True):
        response = german_client.get(path)
    assert response.status_code == 200, path
    html = response.get_data(as_text=True)
    assert "/static/css/tailwind.css" in html.split("</head>", 1)[0], path
    m = re.search(r"window\.__I18N_BOOTSTRAP = (\{.*?\});</script>", html, re.S)
    assert m, path
    assert html.index("window.__I18N_BOOTSTRAP") < html.index("/static/js/i18n.js"), path
    import json
    data = json.loads(m.group(1))
    assert data["locale"] == "de" and set(data["translations"]) == {"de", "en"}, path
    # |tojson escapes markup characters, so no translation can end the script.
    assert "</" not in m.group(1), path


THEME_INCLUDE = "{% include 'includes/theme_bootstrap.html' %}"


@pytest.mark.parametrize("page", PAGES)
def test_theme_is_applied_before_first_paint(page):
    """Each page sets the dark/theme classes in <head>, before any stylesheet.

    On a page whose theme was set only by Vue after mounting, the first paint
    was light, and the body's color transition then faded it to dark.
    """
    html = _read(page)
    head = html.split("</head>")[0]
    applies = THEME_INCLUDE in head or re.search(r"classList\.(add|toggle)\('dark'", head)
    assert applies, f"{page}: no theme script in <head>"
    if THEME_INCLUDE in head:
        first_css = head.find('rel="stylesheet"')
        assert first_css == -1 or head.find(THEME_INCLUDE) < first_css, f"{page}: theme include after a stylesheet"


@pytest.mark.parametrize("page", ["index.html", "inquire.html"])
def test_scripts_follow_the_stylesheets(page):
    """No external script blocks the parser before the stylesheets are known.

    While such a script was loading, some browsers painted the default white
    canvas, which showed as a white flash before a dark page.
    """
    head = _read(page).split("</head>")[0]
    last_css = max(m.start() for m in re.finditer(r'rel="stylesheet"', head))
    early = [m.group(0) for m in re.finditer(r"<script src=[^>]*>|\{% include '[^']*' %\}", head)
             if m.start() < last_css and "theme_bootstrap" not in m.group(0)]
    assert not early, f"{page}: before the last stylesheet: {early}"
