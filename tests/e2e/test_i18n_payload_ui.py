"""Browser test of the translations a page carries (discussion #611).

A page in another language than English embeds that language only; the
English table (about 190 KB) is no longer in it. English is fetched, once
and from the versioned locale URL, only when a key is missing in a language
that the browser fetched itself (a saved language that differs from the one
of the page). Until it arrives the text is empty, never the key, and the
Vue-rendered text is updated when it does. This test covers, in headless
Chromium:

- the public share page for a German browser: German text, one embedded
  table, no request for a locale file, also for a key that exists nowhere;
- the same page with French saved in the browser and two keys taken out of
  the French file: French is fetched, English is fetched once for the
  missing keys, the labels end in English, and the keys are never shown;
- the sign-in page for a German browser (rendered on the server);
- the main page of an English account: English embedded as before, no
  locale request; and with German saved in the browser: one request for
  the German file, none for English.

The server side is in tests/test_page_assets.py, i18n.js in
static/js/i18n.test.js. It runs only with SPEAKR_BROWSER_SMOKE=1 against a
running Speakr at SPEAKR_BASE_URL, the instance of the browser smoke test.
"""

import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import ROOT, PageProblems, login, precondition_unmet  # noqa: E402
from seed_recordings import TITLES  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))

# Keys the share page shows as text, taken out of the French file below
MISSING = ("sharePage.subtitle", "transcription.title")
# Records whether a missing key was ever shown as text
WATCH_FOR_KEYS = """(keys) => {
    window.__keysShown = [];
    const look = () => {
        // The template source, t('a.b'), is in the document until Vue mounts; that is not a shown key.
        const text = (document.body ? document.body.innerText : '').replace(/t\\('[^']*'\\)/g, '');
        for (const key of keys) if (text.includes(key) && !window.__keysShown.includes(key)) window.__keysShown.push(key);
    };
    new MutationObserver(look).observe(document, { childList: true, subtree: true, characterData: true });
    document.addEventListener('DOMContentLoaded', look);
}"""
BOOTSTRAP = """() => ({
    locale: window.__I18N_BOOTSTRAP.locale,
    tables: Object.keys(window.__I18N_BOOTSTRAP.translations),
    fallback: window.__I18N_BOOTSTRAP.fallback === undefined ? null : Object.keys(window.__I18N_BOOTSTRAP.fallback).length,
    version: window.__I18N_ASSET_VERSION,
    ready: window.i18n.isReady,
    current: window.i18n.getLocale(),
    loaded: [...window.i18n.loadedLocales].sort(),
})"""


def strings(language):
    with open(os.path.join(ROOT, "static", "locales", f"{language}.json"), encoding="utf-8") as f:
        return json.load(f)


def text(table, key):
    value = table
    for part in key.split("."):
        value = value[part]
    return value


def locale_requests(page):
    """The locale files the page asks for, as path with query."""
    seen = []
    page.on("request", lambda r: seen.append(r.url[len(BASE):]) if "/static/locales/" in r.url else None)
    return seen


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def owner(browser):
    """The signed-in admin: one sign-in for the file, and a public link."""
    context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
    page = context.new_page()
    login(page, BASE, ADMIN_EMAIL, ADMIN_PASSWORD)
    yield context, page
    context.close()


@pytest.fixture(scope="module")
def share_link(owner):
    _, page = owner
    listing = page.request.get(f"{BASE}/api/recordings?per_page=50")
    items = listing.json().get("recordings", []) if listing.ok else []
    ids = [r["id"] for r in items if r.get("title") in TITLES]
    if not ids:
        precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
    token = page.locator('meta[name="csrf-token"]').first.get_attribute("content")
    headers = {"X-CSRFToken": token, "Content-Type": "application/json"}
    # A link is made only over https, which a proxy reports this way.
    https = {"X-Forwarded-Proto": "https", "Referer": BASE.replace("http://", "https://") + "/"}
    response = page.request.post(f"{BASE}/api/recording/{ids[0]}/share", headers={**headers, **https},
                                 data=json.dumps({"share_summary": True, "share_notes": False, "force_new": True}))
    if response.status == 403:
        precondition_unmet(f"public links cannot be made on this instance: {response.text()[:200]}")
    assert response.ok, f"could not share: HTTP {response.status} {response.text()[:200]}"
    body = response.json()
    yield "/share/" + body["share_url"].rsplit("/share/", 1)[1]
    page.request.delete(f"{BASE}/api/share/{body['share']['id']}", headers=headers)


def test_a_german_share_page_carries_german_only_and_fetches_no_locale(browser, share_link):
    de, en = strings("de"), strings("en")
    context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="de-DE")
    try:
        page = context.new_page()
        problems = PageProblems(page, BASE)
        requests = locale_requests(page)
        context.add_init_script(f"({WATCH_FOR_KEYS})({json.dumps(list(MISSING))})")
        response = page.goto(f"{BASE}{share_link}")
        page.wait_for_load_state("load")
        page.get_by_text(text(de, "sharePage.subtitle"), exact=True).first.wait_for(state="visible", timeout=15000)

        state = page.evaluate(BOOTSTRAP)
        assert state["locale"] == "de" and state["tables"] == ["de"], state
        assert state["fallback"] == 0, state  # every locale is complete: no English string is needed
        assert state["ready"] is True and state["current"] == "de" and state["loaded"] == ["de"], state
        assert state["version"]
        # The English table is not in the page.
        html = response.text()
        assert json.dumps(text(en, "sharePage.subtitle")) not in html
        assert json.dumps(text(de, "sharePage.subtitle")) in html

        body = page.inner_text("body")
        assert text(de, "transcription.title") in body
        assert not re.findall(r"\b(?:sharePage|transcription|summary|buttons|tooltips)\.[a-zA-Z]+\b", body), body[:500]

        # A key that exists in no language is answered at once with the key
        # (what the helpers with a default text rely on), and English is not fetched for it.
        assert page.evaluate("() => window.i18n.t('no.such.key')") == "no.such.key"
        assert page.evaluate("() => window.i18n.items('no.such.list')") == []
        page.wait_for_timeout(800)
        assert requests == [], requests
        assert page.evaluate("() => window.__keysShown") == []
        assert not problems.failed and not problems.errors, (problems.failed, problems.errors)
    finally:
        context.close()


def test_a_key_missing_in_a_fetched_language_gets_english_and_is_never_shown(browser, share_link):
    fr, en = strings("fr"), strings("en")
    incomplete = strings("fr")
    for key in MISSING:
        section, name = key.split(".")
        del incomplete[section][name]

    context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="de-DE")
    try:
        # French without two keys, as a locale file that lags behind English would be.
        context.route("**/static/locales/fr.json*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(incomplete)))
        context.add_init_script("localStorage.setItem('preferredLanguage', 'fr')")
        context.add_init_script(f"({WATCH_FOR_KEYS})({json.dumps(list(MISSING))})")
        page = context.new_page()
        problems = PageProblems(page, BASE)
        requests = locale_requests(page)
        page.goto(f"{BASE}{share_link}")
        page.wait_for_load_state("load")

        # The labels of the missing keys end in English; the others are French.
        page.get_by_text(text(en, "sharePage.subtitle"), exact=True).first.wait_for(state="visible", timeout=15000)
        body = page.inner_text("body")
        assert text(en, "transcription.title") in body
        assert text(fr, "sharePage.subtitle") not in body
        state = page.evaluate(BOOTSTRAP)
        assert state["tables"] == ["de"] and state["current"] == "fr" and state["loaded"] == ["de", "en", "fr"], state
        assert page.evaluate("() => window.i18n.t('summary.title')") == text(fr, "summary.title")
        assert page.evaluate("() => window.i18n.t('sharePage.subtitle')") == text(en, "sharePage.subtitle")

        # French once, then English once, both from the versioned URL.
        version = state["version"]
        assert requests == [f"/static/locales/fr.json?v={version}", f"/static/locales/en.json?v={version}"], requests
        # The raw keys were never on the screen, also not before English arrived.
        assert page.evaluate("() => window.__keysShown") == []
        assert not problems.failed and not problems.errors, (problems.failed, problems.errors)

        # Further missing keys cost no further request.
        assert page.evaluate("() => window.i18n.t('no.such.key')") == "no.such.key"
        page.wait_for_timeout(500)
        assert len(requests) == 2, requests
    finally:
        context.close()


def test_the_sign_in_page_of_a_german_browser(browser):
    de = strings("de")
    context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="de-DE")
    try:
        page = context.new_page()
        problems = PageProblems(page, BASE)
        requests = locale_requests(page)
        page.goto(f"{BASE}/login")
        page.wait_for_load_state("load")
        assert page.locator("html").get_attribute("lang") == "de"
        assert text(de, "authPages.loginTitle") in page.title()
        assert page.locator("input[name=email]").count() == 1 and page.locator("input[name=password]").count() == 1
        page.wait_for_timeout(500)
        assert requests == [], requests
        assert not problems.failed and not problems.errors, (problems.failed, problems.errors)
    finally:
        context.close()


def test_an_english_main_page_is_unchanged_and_a_saved_language_fetches_only_itself(browser, owner):
    de = strings("de")
    owner_context, _ = owner
    context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
    try:
        context.add_cookies(owner_context.cookies())
        page = context.new_page()
        problems = PageProblems(page, BASE)
        requests = locale_requests(page)
        page.goto(f"{BASE}/")
        page.wait_for_load_state("load")
        page.wait_for_timeout(2000)
        state = page.evaluate(BOOTSTRAP)
        assert state["locale"] == "en" and state["tables"] == ["en"] and state["fallback"] is None, state
        assert state["ready"] is True and state["loaded"] == ["en"], state
        assert requests == [], requests
        assert not problems.failed and not problems.errors, (problems.failed, problems.errors)

        # German saved in this browser (the account is English): German is
        # fetched, English is already in the page.
        page.evaluate("() => localStorage.setItem('preferredLanguage', 'de')")
        problems.reset()
        page.goto(f"{BASE}/")
        page.wait_for_load_state("load")
        page.wait_for_timeout(2000)
        state = page.evaluate(BOOTSTRAP)
        assert state["current"] == "de" and state["loaded"] == ["de", "en"], state
        assert requests == [f"/static/locales/de.json?v={state['version']}"], requests
        page.locator("[data-user-menu-toggle]:visible").first.click()
        menu = page.locator("[data-user-menu-dropdown]:visible").first
        menu.wait_for(state="visible", timeout=5000)
        assert text(de, "nav.signOut") in menu.inner_text()
        assert not problems.failed and not problems.errors, (problems.failed, problems.errors)
    finally:
        context.close()
