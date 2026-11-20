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

import contextlib
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


def test_the_service_worker_fetches_nothing_at_install():
    """The worker fetches nothing at install: static files are cached as
    pages ask for them, under the URLs the templates link. The files of the
    offline recorder are stored when the signed-in app asks for them (the
    PREPARE_OFFLINE_RECORDER message, #650), and the plain offline page is
    part of the worker file. No page of the server is ever in a cache."""
    text = open(os.path.join(ROOT, "static", "sw.js"), encoding="utf-8").read()
    assert "addAll" not in text and "cache.add(" not in text
    install = text[text.index("self.addEventListener('install'"):]
    install = install[:install.index("});")]
    assert "fetch(" not in install and "prepareRecorder" not in install and "caches" not in install
    assert "ASSETS_TO_CACHE" not in text and "offline.html" not in text
    assert not os.path.exists(os.path.join(ROOT, "static", "offline.html"))


def test_compiled_stylesheet_has_the_template_classes():
    """Runs where the stylesheet has been built (CI builds it first)."""
    path = os.path.join(ROOT, "static", "css", "tailwind.css")
    if not os.path.exists(path):
        pytest.skip("static/css/tailwind.css is not built; run scripts/build_css.sh")
    css = open(path, encoding="utf-8").read()
    for cls in (r".text-\[var\(--text-primary\)\]", r".bg-\[var\(--bg-tertiary\)\]"):
        assert cls in css, cls


def test_the_transcription_error_card_takes_its_colours_from_the_notice_level():
    """One level (danger) for every cause of an error; no colour per error
    type. A transcription canceled in the processing queue is no error: where
    the stored card is read it gets the level info, the only other level."""
    card = open(os.path.join(ROOT, "templates", "components", "detail", "_transcription-error.html"),
                encoding="utf-8").read()
    assert """:class="'notice--' + (processedTranscription.error.level || 'danger')\"""" in card
    assert "error.type" not in card
    app = open(os.path.join(ROOT, "static", "js", "app.modular.js"), encoding="utf-8").read()
    assert re.findall(r"\blevel: '(\w+)'", app) == ["info"]
    assert "if (data.y === 'canceled')" in app
    assert not re.search(r"(bg|text|border)-(red|amber|orange|yellow|blue|purple|pink|indigo|gray)-[0-9]", card)
    css = open(os.path.join(ROOT, "static", "css", "primitives.css"), encoding="utf-8").read()
    for level in ("danger", "warn", "info", "success"):
        assert f".notice--{level}" in css


def test_pages_with_primitive_classes_link_primitives_css():
    """A page that uses the dialog, button or field primitives links primitives.css.

    group-admin.html did not, so its dialogs had no overlay and sat unstyled
    below the page footer (found by tests/e2e/test_group_permission_ui.py).
    """
    uses = re.compile(r'modal-overlay|btn--|class="field')
    for name in sorted(os.listdir(os.path.join(ROOT, "templates"))):
        if not name.endswith(".html"):
            continue
        page = open(os.path.join(ROOT, "templates", name), encoding="utf-8").read()
        if "<html" in page and uses.search(page):
            assert "css/primitives.css" in page, f"templates/{name} uses primitives without primitives.css"


# --- i18n_bootstrap() --------------------------------------------------------

def test_bootstrap_for_another_language_has_that_language_only():
    """English is not embedded beside the viewer's language (discussion #611)."""
    data = i18n_bootstrap("de")
    assert data["locale"] == "de"
    assert list(data["translations"]) == ["de"]
    english = i18n_bootstrap("en")["translations"]["en"]
    assert data["translations"]["de"]["nav"]["account"] != english["nav"]["account"]
    # Every locale is complete (tests/test_localization.py), so no English
    # string has to be embedded for a missing key.
    assert data["fallback"] == {}


@pytest.mark.parametrize("locale", ["de", "es", "fr", "pt-BR", "ru", "zh"])
def test_bootstrap_of_each_language_is_smaller_than_english_beside_it(locale):
    import json
    data = i18n_bootstrap(locale)
    assert set(data) == {"locale", "translations", "fallback"} and list(data["translations"]) == [locale]
    english = len(json.dumps(i18n_bootstrap("en")["translations"]["en"]))
    assert len(json.dumps(data)) < len(json.dumps(data["translations"][locale])) + english // 100


def test_bootstrap_embeds_the_english_string_of_a_missing_key(tmp_path, monkeypatch):
    """A key the language lacks arrives as its English string, nested as in
    the locale file, so it never shows as a raw key and English stays out."""
    import json
    from src.utils import i18n_bootstrap as module
    (tmp_path / "en.json").write_text(json.dumps(
        {"nav": {"account": "Account", "admin": "Admin"}, "only": {"english": "English only"},
         "list": {"1": "One", "2": "Two"}, "empty": "Filled"}), encoding="utf-8")
    (tmp_path / "de.json").write_text(json.dumps(
        {"nav": {"account": "Konto", "admin": "Verwaltung"}, "list": {"1": "Eins"}, "empty": ""}), encoding="utf-8")
    monkeypatch.setattr(module, "_LOCALES_DIR", str(tmp_path))
    monkeypatch.setattr(module, "_cache", {})
    monkeypatch.setattr(module, "_gap_cache", {})
    data = module.i18n_bootstrap("de")
    assert list(data["translations"]) == ["de"]
    assert data["fallback"] == {"only": {"english": "English only"}, "list": {"2": "Two"}, "empty": "Filled"}
    # English itself has no fallback table.
    assert "fallback" not in module.i18n_bootstrap("en")
    # A changed locale file is read again.
    import os
    (tmp_path / "de.json").write_text(json.dumps(
        {"nav": {"account": "Konto", "admin": "Verwaltung"}, "only": {"english": "Nur Englisch"},
         "list": {"1": "Eins", "2": "Zwei"}, "empty": "Voll"}), encoding="utf-8")
    stat = os.stat(tmp_path / "de.json")
    os.utime(tmp_path / "de.json", (stat.st_atime, stat.st_mtime + 5))
    assert module.i18n_bootstrap("de")["fallback"] == {}


def test_bootstrap_for_english_has_one_table():
    data = i18n_bootstrap("en")
    assert data == {"locale": "en", "translations": {"en": data["translations"]["en"]}}
    assert data["translations"]["en"]["nav"]["account"] == "Account"


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


@contextlib.contextmanager
def _signed_in_client(language):
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"pa_{s}", email=f"pa_{s}@local.test", password="x", is_admin=True,
                    ui_language=language)
        db.session.add(user)
        db.session.commit()
        client = _Client(app, app.response_class)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True
        yield client
        db.session.delete(db.session.get(User, user.id))
        db.session.commit()


@pytest.fixture
def german_client():
    with _signed_in_client("de") as client:
        yield client


@pytest.fixture
def english_client():
    with _signed_in_client("en") as client:
        yield client


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
    assert data["locale"] == "de" and list(data["translations"]) == ["de"], path
    assert data["fallback"] == {}, path
    # A locale file fetched later carries the app version.
    assert f"window.__I18N_ASSET_VERSION = {json.dumps(_app_version_value())};" in html, path
    # |tojson escapes markup characters, so no translation can end the script.
    assert "</" not in m.group(1), path


# Scripts and stylesheets are linked as <path>?v=<app version>. The service
# worker (static/sw.js) caches the app shell by full URL, so a worker left over
# from the release before cannot answer the new page's requests with its own
# copies; without the version, new HTML ran once against the old
# app.modular.js after an upgrade.
STATIC_ASSET_URL = re.compile(r"url_for\('static',\s*filename='[^']+\.(?:js|css)'[^)]*\)")


def test_templates_version_every_script_and_stylesheet_url():
    offenders = []
    for dirpath, _, files in os.walk(TEMPLATES):
        for name in files:
            if not name.endswith(".html"):
                continue
            path = os.path.join(dirpath, name)
            text = open(path, encoding="utf-8").read()
            offenders += [f"{os.path.relpath(path, ROOT)}: {m.group(0)}"
                          for m in STATIC_ASSET_URL.finditer(text) if "v=app_version" not in m.group(0)]
    assert not offenders, "add v=app_version:\n" + "\n".join(offenders)


def _app_version_value():
    from src.app import _app_version
    return _app_version


def test_an_english_page_embeds_english_as_before(english_client):
    import json
    html = english_client.get("/").get_data(as_text=True)
    data = json.loads(re.search(r"window\.__I18N_BOOTSTRAP = (\{.*?\});</script>", html, re.S).group(1))
    assert data == {"locale": "en", "translations": {"en": i18n_bootstrap("en")["translations"]["en"]}}


def test_the_page_in_another_language_is_smaller_by_the_english_table(german_client):
    """The German main page no longer carries the English table (about 190 KB)."""
    import json
    html = german_client.get("/").get_data(as_text=True)
    english = json.dumps(i18n_bootstrap("en")["translations"]["en"])
    german = json.dumps(i18n_bootstrap("de")["translations"]["de"])
    data = re.search(r"window\.__I18N_BOOTSTRAP = (\{.*?\});</script>", html, re.S).group(1)
    assert len(data) < len(german) * 1.2
    assert len(data) < len(german) + len(english) // 2


def _version_query():
    from urllib.parse import quote
    from src.app import _app_version
    assert _app_version
    return "?v=" + quote(_app_version, safe="")


def test_rendered_index_has_versioned_entry_asset_urls(german_client):
    html = german_client.get("/").get_data(as_text=True)
    query = _version_query()
    for asset in ("js/app.modular.js", "css/styles.css", "css/tailwind.css", "js/i18n.js", "js/csrf-refresh.js",
                  "vendor/js/vue.global.js"):
        assert f'/static/{asset}{query}"' in html, asset
    assert '/static/js/app.modular.js"' not in html


@pytest.mark.parametrize("path", ["/account", "/admin"])
def test_other_pages_version_the_shared_assets(german_client, path):
    html = german_client.get(path).get_data(as_text=True)
    query = _version_query()
    for asset in ("css/styles.css", "css/tailwind.css", "js/i18n.js"):
        assert f'/static/{asset}{query}"' in html, (path, asset)


def test_the_worker_answers_from_its_cache_only_for_its_own_release():
    """A static URL is answered from the cache only when its ?v= is the
    worker's own (static/js/sw.test.js tests the function); the templates
    version every script and stylesheet (above), so the worker of one
    release never answers the page of another with its copies."""
    text = open(os.path.join(ROOT, "static", "sw.js"), encoding="utf-8").read()
    assert "const VERSION = new URLSearchParams(self.location.search).get('v') || '';" in text
    assert "return version && v === version ? 'static-cache-first' : 'static-network-only';" in text
    assert "const CACHE_NAME = `${CACHE_PREFIX}${VERSION || 'unversioned'}`;" in text


SERVICE_WORKER_CLIENT = "filename='js/service-worker-client.js', v=app_version) }}\""


@pytest.mark.parametrize("page", ["index.html", "account.html", "admin.html", "group-admin.html", "inquire.html"])
def test_signed_in_pages_register_the_service_worker(page):
    text = _read(page)
    assert text.count(SERVICE_WORKER_CLIENT + " data-register></script>") == 1, page


def test_the_sign_in_page_clears_after_a_sign_out_without_registering_a_worker():
    text = _read("login.html")
    assert text.count(SERVICE_WORKER_CLIENT + "></script>") == 1
    assert "data-register" not in text


@pytest.mark.parametrize("page", ["share.html", "register.html"])
def test_public_pages_do_not_load_the_service_worker_script(page):
    assert "service-worker-client.js" not in _read(page)


def test_only_the_client_script_registers_a_worker():
    """One place registers, with scope /; nothing registers /static/sw.js."""
    hits = []
    for top in ("templates", os.path.join("static", "js")):
        for d, _, fs in os.walk(os.path.join(ROOT, top)):
            for f in fs:
                if f.endswith((".html", ".js")) and not f.endswith(".test.js") and "vendor" not in d:
                    text = open(os.path.join(d, f), encoding="utf-8").read()
                    if "serviceWorker.register" in text or "sw.register(" in text:
                        hits.append(f)
                    assert "'/static/sw.js" not in text and "`/static/sw.js" not in text, f
    assert hits == ["service-worker-client.js"]


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
    # The theme includes are inline scripts and must come first (D#419).
    early = [m.group(0) for m in re.finditer(r"<script src=[^>]*>|\{% include '[^']*' %\}", head)
             if m.start() < last_css and not any(t in m.group(0) for t in ("theme_bootstrap", "theme_account"))]
    assert not early, f"{page}: before the last stylesheet: {early}"


def test_main_page_has_no_fixed_color_loading_screen():
    """The main page had a second loading screen in Tailwind gray-800 (navy).

    It was shown on every load until the app mounted, which showed as a blue
    flash with any other color scheme. The theme-colored overlay in
    includes/loading_overlay.html is the only loading screen.
    """
    html = _read("index.html")
    assert 'id="loader"' not in html
    assert "includes/loading_overlay.html" in html


def test_loading_overlay_hides_the_page_from_the_first_paint():
    """The hiding class is set on <html> in <head>, not by a timer on <body>."""
    overlay = _read("includes/loading_overlay.html")
    assert "document.documentElement.classList.add('app-loading')" in overlay
    assert "html.app-loading body > *:not(.app-loading-overlay)" in overlay
    loader_js = open(os.path.join(ROOT, "static", "js", "loading.js"), encoding="utf-8").read()
    assert "document.documentElement.classList.remove('app-loading')" in loader_js


# --- Public share page ----------------------------------------------------------

@pytest.fixture
def shared_recording():
    """A recording with a public link, owned by a German-speaking user."""
    from src.models import Recording, Share
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        owner = User(username=f"ps_{s}", email=f"ps_{s}@local.test", password="x", ui_language="de")
        db.session.add(owner)
        db.session.commit()
        rec = Recording(user_id=owner.id, title="Share page test", audio_path="/tmp/x.mp3", status="COMPLETED")
        db.session.add(rec)
        db.session.commit()
        share = Share(recording_id=rec.id, user_id=owner.id)
        db.session.add(share)
        db.session.commit()
        ids = (owner.id, rec.id, share.public_id)
    yield ids
    with app.app_context():
        Share.query.filter_by(recording_id=ids[1]).delete()
        db.session.delete(db.session.get(Recording, ids[1]))
        db.session.delete(db.session.get(User, ids[0]))
        db.session.commit()


def _bootstrap_locale(html):
    import json
    m = re.search(r"window\.__I18N_BOOTSTRAP = (\{.*?\});</script>", html, re.S)
    assert m
    assert html.index("window.__I18N_BOOTSTRAP") < html.index("/static/js/i18n.js")
    data = json.loads(m.group(1))
    return data["locale"], set(data["translations"])


@pytest.mark.parametrize("accept, locale", [("fr-FR,fr;q=0.9", "fr"), ("pt", "pt-BR"), (None, "en")])
def test_public_share_page_follows_the_visitors_browser_language(shared_recording, accept, locale):
    """An anonymous visitor gets the Accept-Language locale, not the owner's."""
    headers = {"Accept-Language": accept} if accept else {}
    response = app.test_client().get(f"/share/{shared_recording[2]}", headers=headers)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert _bootstrap_locale(html) == (locale, {locale})
    assert f'<html lang="{locale}"' in html


def test_public_share_page_uses_a_signed_in_viewers_language(german_client, shared_recording):
    response = german_client.get(f"/share/{shared_recording[2]}", headers={"Accept-Language": "fr"})
    assert response.status_code == 200
    assert _bootstrap_locale(response.get_data(as_text=True)) == ("de", {"de"})


# --- Sign-in pages (server-side text) ----------------------------------------

@pytest.mark.parametrize("accept, locale", [("fr-FR,fr;q=0.9", "fr"), ("pt", "pt-BR"), (None, "en")])
@pytest.mark.parametrize("path, key", [("/login", "authPages.loginTitle"), ("/register", "authPages.createAccount")])
def test_sign_in_pages_follow_the_visitors_browser_language(path, key, accept, locale):
    """The forms are rendered on the server in the Accept-Language locale, so
    they read correctly without JavaScript."""
    from markupsafe import escape
    from src.utils.localization import translate
    headers = {"Accept-Language": accept} if accept else {}
    with patch.dict(os.environ, {"ALLOW_REGISTRATION": "true"}):
        response = app.test_client().get(path, headers=headers)
    assert response.status_code == 200, path
    html = response.get_data(as_text=True)
    assert f'<html lang="{locale}">' in html
    with app.test_request_context():
        heading = translate(key, locale)
    assert heading != key
    assert str(escape(heading)) in html
    # No catalog key leaks into the page as literal text.
    assert "authPages." not in html
