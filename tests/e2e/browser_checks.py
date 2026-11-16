"""Checks shared by the browser tests in this directory.

- ``PageProblems`` collects, for the page open in a tab, every same-origin
  response with status 400 or higher that the step does not expect, every
  JavaScript error and every request for the Tailwind Play CDN build.
- ``check_common`` fails on those, on template text left in the page
  (``${`` or ``{{``) and on Tailwind utilities that are not applied.
- ``login`` signs in through the login form.
- ``open_page`` opens a path after leaving the previous page.

Console messages that are not failures (documented allowlist):
- messages from browser extensions (chrome-extension://);
- "Failed to load resource": the browser's echo of a failed response, which
  is already reported with its URL by the response check.
"""

import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

IGNORED_CONSOLE = (re.compile(r"chrome-extension://"), re.compile(r"Failed to load resource"))
PLAY_CDN = re.compile(r"tailwind\.min\.js|cdn\.tailwindcss\.com")


class PageProblems:
    """Failed responses and JavaScript errors for the current page.

    A step that expects a refusal from the server registers it with
    ``expect(status, path)``; a matching response is then recorded in
    ``seen`` instead of ``failed``. ``reset()`` clears both lists and the
    expectations.
    """

    def __init__(self, page, base):
        self.base = base.rstrip("/")
        self.failed, self.errors, self.play_cdn = [], [], []
        self.expected, self.seen = [], []
        page.on("request", self._request)
        page.on("response", self._response)
        page.on("pageerror", lambda exc: self.errors.append(f"uncaught: {exc}"))
        # Requests that ended without an answer (a closed connection, for example). They are
        # kept for the message of a step that fails; no check asserts on them.
        self.unanswered = []
        page.on("requestfailed", lambda request: self.unanswered.append(f"{request.method} {request.url[len(self.base):]}: {request.failure}"))
        page.on("console", self._console)

    def expect(self, status, path):
        self.expected.append((status, path))

    def unseen(self):
        """Expected responses that did not occur."""
        return [e for e in self.expected if e not in self.seen]

    def _request(self, request):
        if PLAY_CDN.search(request.url):
            self.play_cdn.append(f"request: {request.url}")

    def _response(self, response):
        if not response.url.startswith(self.base) or response.status < 400:
            return
        path = response.url[len(self.base):].split("?")[0]
        if (response.status, path) in self.expected:
            self.seen.append((response.status, path))
        else:
            self.failed.append(f"{response.status} {response.request.method} {path}")

    def _console(self, msg):
        text = msg.text
        if "cdn.tailwindcss.com" in text:
            self.play_cdn.append(f"console {msg.type}: {text}")
        if msg.type != "error":
            return
        location = (msg.location or {}).get("url", "")
        if any(p.search(text) or p.search(location) for p in IGNORED_CONSOLE):
            return
        self.errors.append(f"console: {text}")

    def reset(self):
        self.failed.clear()
        self.errors.clear()
        self.play_cdn.clear()
        self.expected.clear()
        self.seen.clear()
        self.unanswered.clear()


def login(page, base, user, password):
    """Sign in through the login form; fails when the form does not accept it."""
    page.goto(f"{base}/login")
    page.fill("input[name=email]", user)
    page.fill("input[name=password]", password)
    page.locator("form:has(input[name=password]) [type=submit]:visible").first.click()
    page.wait_for_load_state("load")
    assert "/login" not in page.url, f"could not log in as {user}"


def open_page(page, problems, base, path, settle_ms=2500):
    # Leave the previous page first: its requests still in flight are
    # aborted on unload and logged as "Failed to fetch", which belongs to
    # that page, not the one being checked.
    page.goto("about:blank")
    page.wait_for_timeout(300)
    problems.reset()
    response = page.goto(f"{base}{path}")
    page.wait_for_load_state("load")
    # Headers mount after the page's scripts run; give polling apps a moment.
    page.wait_for_timeout(settle_ms)
    return response


def check_common(page, problems, path):
    body = page.inner_text("body")
    leftovers = [m for m in ("${", "{{") if m in body]
    assert not leftovers, f"{path}: template text left in the page: {leftovers}"
    assert not problems.failed, f"{path}: failed requests: {problems.failed}"
    assert not problems.errors, f"{path}: JavaScript errors: {problems.errors}"
    assert not problems.play_cdn, f"{path}: Tailwind Play CDN in use: {problems.play_cdn}"
    # Utilities from the compiled stylesheet apply (a probe element, so the
    # check does not depend on any page's markup).
    probe = page.evaluate("""() => {
        const el = document.createElement('div');
        el.className = 'rounded-lg px-4 hidden';
        document.body.appendChild(el);
        const s = getComputedStyle(el);
        const out = {display: s.display, radius: s.borderTopLeftRadius, padding: s.paddingLeft};
        el.remove();
        return out;
    }""")
    assert probe == {"display": "none", "radius": "8px", "padding": "16px"}, f"{path}: Tailwind utilities not applied: {probe}"


_strings = None


def en(key):
    """The English interface string for a translation key (static/locales/en.json)."""
    global _strings
    if _strings is None:
        with open(os.path.join(ROOT, "static", "locales", "en.json"), encoding="utf-8") as f:
            _strings = json.load(f)
    value = _strings
    for part in key.split("."):
        value = value[part]
    return value
