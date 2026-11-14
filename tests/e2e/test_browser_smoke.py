"""Browser smoke test: every page loads and its header works, in a real browser.

The API-level tests never render a page, so two header regressions reached a
release: a Vue template error (an element with both v-text and fallback text)
that left the header blank, and a request for vendor/js/vue.global.prod.js, a
file the image does not contain, which left the settings-page headers empty.
This test logs in through the login form, opens each page in headless
Chromium, and fails on:

- any same-origin response with status 400 or higher;
- any JavaScript error (uncaught exception or console error);
- template text left in the page (``${`` or ``{{``);
- a header that is not mounted, or a user menu that does not open with its
  entries;
- the Tailwind Play CDN build (vendor/js/tailwind.min.js or
  cdn.tailwindcss.com) requested, or its "should not be used in production"
  console warning; the stylesheet is compiled at build time instead;
- Tailwind utilities from static/css/tailwind.css not applied;
- translation keys (for example ``nav.account``) shown as text in the header.

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL (the test.yml browser-smoke job starts one from source with
tests/e2e/run_speakr.py; docker-publish.yml runs the built image before it is
pushed). SPEAKR_SMOKE_USER / SPEAKR_SMOKE_PASSWORD default to the admin in
config/env.whisperx.example.

The response, JavaScript and template checks, with their console allowlist,
are in browser_checks.py, shared with test_group_permission_ui.py.
"""

import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser smoke test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, login, open_page  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
USER = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))

MENU_ENTRIES = ("Notifications", "Settings", "Shared recordings", "Sign out")
# A translation key: two or more dot-separated identifiers, the first one
# lower case (nav.account, inquire.title, tokenBudget.percentage).
TRANSLATION_KEY = re.compile(r"\b[a-z][a-zA-Z]*(?:\.[a-zA-Z]+)+\b")


@pytest.fixture(scope="module")
def browser_page():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        page = context.new_page()
        problems = PageProblems(page, BASE)
        login(page, BASE, USER, PASSWORD)
        problems.reset()
        yield page, problems
        context.close()
        browser.close()


def _open(page, problems, path):
    return open_page(page, problems, BASE, path)


def _check_common(page, problems, path):
    check_common(page, problems, path)


def _check_header_translated(page, path, selector):
    header = page.locator(selector).first
    assert header.count() == 1, f"{path}: no header ({selector})"
    keys = TRANSLATION_KEY.findall(header.inner_text())
    assert not keys, f"{path}: translation keys shown in the header: {keys}"


def _check_user_menu(page, path):
    toggle = page.locator("[data-user-menu-toggle]:visible").first
    assert toggle.count() == 1, f"{path}: no user menu button in the header"
    toggle.click()
    menu = page.locator("[data-user-menu-dropdown]:visible").first
    menu.wait_for(state="visible", timeout=5000)
    text = menu.inner_text()
    missing = [entry for entry in MENU_ENTRIES if entry not in text]
    assert not missing, f"{path}: user menu is missing {missing}"
    page.keyboard.press("Escape")
    page.mouse.click(5, 400)


def test_main_view(browser_page):
    page, problems = browser_page
    _open(page, problems, "/")
    assert page.locator("[data-v-app]").count() >= 1, "/: the app did not mount"
    _check_header_translated(page, "/", "header")
    _check_user_menu(page, "/")
    _check_common(page, problems, "/")


@pytest.mark.parametrize("path", ["/account", "/admin"])
def test_settings_pages(browser_page, path):
    page, problems = browser_page
    response = _open(page, problems, path)
    assert response is not None and response.status == 200, f"{path}: HTTP {response and response.status}"
    header = page.locator("#global-header")
    assert header.count() == 1, f"{path}: no shared page header"
    assert header.get_attribute("data-v-app") is not None, f"{path}: the page header did not mount"
    label = header.locator(".t-meta").first.inner_text().strip()
    assert label, f"{path}: the page label in the header is empty"
    _check_header_translated(page, path, "#global-header")
    _check_user_menu(page, path)
    if path == "/account":
        tabs = page.locator(".tabs button:visible, .tabs a:visible")
        assert tabs.count() >= 3, "/account: the tab strip did not render"
    _check_common(page, problems, path)


def _admin_users_table(page):
    """The User Management table (the only one with a Public share column)."""
    table = page.locator("table").filter(
        has=page.locator("th", has_text="Public share")).first
    table.locator("th", has_text="Public share").first.wait_for(
        state="visible", timeout=15000)
    return table


def _admin_user_rows(table):
    """Visible data rows as lists of cell text (the empty-state row is skipped)."""
    rows = table.locator("tbody tr")
    headers = table.locator("thead th")
    out = []
    for index in range(rows.count()):
        cells = rows.nth(index).locator("td")
        if cells.count() < headers.count():  # the empty-state or loading row
            continue
        out.append([cells.nth(i).inner_text().strip() for i in range(cells.count())])
    return out


def _file_size_bytes(text):
    """The primary size in a storage cell ("12.3 MB / 5 GB") in bytes."""
    match = re.match(r"([\d.,]+)\s*(B|KB|MB|GB|TB)", text.split("/")[0].strip())
    assert match, f"unparsable storage cell: {text!r}"
    number = float(match.group(1).replace(",", ""))
    return number * 1024 ** ("B KB MB GB TB".split().index(match.group(2)))


def _assert_sorted(page, values, ascending, column):
    if column in ("username", "email"):
        expected = page.evaluate(
            "([vals, asc]) => [...vals].sort((a, b) => asc ? a.localeCompare(b) : b.localeCompare(a))",
            [values, ascending])
    else:
        expected = sorted(values, reverse=not ascending)
    assert values == expected, f"/admin: {column} not sorted {'ascending' if ascending else 'descending'}: {values}"


def test_admin_users_table_shows_last_active(browser_page):
    """The User Management table renders the Last active column from the listing.

    A fresh instance exercises it with the admin who just signed in: a
    relative time in muted text, with the full date and time in the title and
    no color for an online state. A row without a recorded time must say
    "Never" instead of leaving the cell blank. A click on the column header
    sorts the list on the server and marks the header as sorted.
    """
    page, problems = browser_page
    _open(page, problems, "/admin")

    table = page.locator("table").filter(
        has=page.locator("th", has_text="Last active")).first
    header = table.locator("th", has_text="Last active").first
    header.wait_for(state="visible", timeout=15000)

    headers = [h.strip().lower() for h in table.locator("thead th").all_inner_texts()]
    assert headers[-2:] == ["last active", "actions"], (
        f"/admin: Last active is not the column before Actions: {headers}")

    def check_rows():
        rows = table.locator("tbody tr")
        checked = 0
        for index in range(rows.count()):
            cells = rows.nth(index).locator("td")
            if cells.count() < 9:  # the empty-state or loading row
                continue
            cell = cells.nth(7)
            text = cell.inner_text().strip()
            assert text, f"/admin: empty Last active cell in row {index}"
            cell_classes = cell.get_attribute("class") or ""
            assert "text-[var(--text-muted)]" in cell_classes, (
                f"/admin: Last active is not muted: {cell_classes}")
            value = cell.locator("time, span").first
            classes = (value.get_attribute("class") or "") + " " + cell_classes
            assert "success" not in classes and "danger" not in classes, (
                f"/admin: {text!r} carries an online color: {classes}")
            if text == "Never":
                assert value.evaluate("el => el.tagName") == "SPAN", (
                    "/admin: Never should not be a time element")
                assert not value.get_attribute("title"), (
                    "/admin: Never should not carry a timestamp title")
            else:
                assert value.evaluate("el => el.tagName") == "TIME", (
                    f"/admin: {text!r} is not a time element")
                stamp = value.get_attribute("datetime")
                assert stamp, f"/admin: {text!r} has no machine-readable datetime"
                assert stamp.endswith("Z"), (
                    f"/admin: {text!r} datetime is not an explicit UTC instant: {stamp}")
                assert value.get_attribute("title"), (
                    f"/admin: {text!r} has no full date and time in its title")
            checked += 1
        return checked

    assert check_rows(), "/admin: no user rows to check"

    # Sorting: the first click asks the server for ascending order.
    assert header.get_attribute("aria-sort") == "none"
    with page.expect_response(
            lambda r: "/admin/users?" in r.url and "sort=last_active" in r.url) as sorted_response:
        header.locator("button").click()
    assert "order=asc" in sorted_response.value.url, sorted_response.value.url
    assert sorted_response.value.ok
    page.wait_for_function(
        "el => el.getAttribute('aria-sort') === 'ascending'", arg=header.element_handle())
    with page.expect_response(
            lambda r: "/admin/users?" in r.url and "order=desc" in r.url):
        header.locator("button").click()
    page.wait_for_function(
        "el => el.getAttribute('aria-sort') === 'descending'", arg=header.element_handle())
    assert check_rows(), "/admin: no user rows after sorting"


def test_admin_users_table_sorting(browser_page):
    """Every data column sorts the loaded page and reports its direction.

    User Management paginates and searches on the server; the sort is a
    client-side reorder of the rows already loaded, so this test reads the
    rendered cells, clicks a header, and checks the same rows come back in
    the expected order. The direction opens with text ascending and counts
    or flags descending, and re-selecting the header reverses it.
    """
    page, problems = browser_page
    _open(page, problems, "/admin")

    table = _admin_users_table(page)
    headers = table.locator("thead th")
    labels = [h.strip().lower() for h in headers.all_inner_texts()]
    assert labels[-1] == "actions", f"/admin: unexpected last column: {labels}"
    index = {label: i for i, label in enumerate(labels)}
    expected_columns = ["id", "username", "email", "admin", "public share",
                        "recordings", "storage used"]
    assert labels[:len(expected_columns)] == expected_columns, (
        f"/admin: unexpected User Management columns: {labels}")

    actions_header = headers.nth(index["actions"])
    assert actions_header.locator("button").count() == 0, (
        "/admin: the Actions column should not be sortable")

    def column_values(column):
        rows = _admin_user_rows(table)
        assert rows, "/admin: no user rows to sort"
        i = index[column]
        if column == "id":
            return [int(r[i]) for r in rows]
        if column == "recordings":
            return [int(r[i]) for r in rows]
        if column == "storage used":
            return [_file_size_bytes(r[i]) for r in rows]
        if column == "admin":
            return [r[i].lower() == "yes" for r in rows]
        if column == "public share":
            return [r[i].lower() == "allowed" for r in rows]
        return [r[i] for r in rows]  # raw text; the browser collates it

    def assert_direction(column, ascending):
        actual = headers.nth(index[column]).get_attribute("aria-sort")
        expected = "ascending" if ascending else "descending"
        assert actual == expected, f"/admin: {column} aria-sort is {actual!r}, wanted {expected!r}"
        for other in labels[:-1]:
            if other != column:
                assert headers.nth(index[other]).get_attribute("aria-sort") == "none", (
                    f"/admin: {other} kept a sort indicator while {column} is sorted")
        _assert_sorted(page, column_values(column), ascending, column)

    # First load keeps the listing order: ID ascending.
    assert_direction("id", True)

    for column, first_ascending in (
        ("username", True),
        ("email", True),
        ("admin", False),
        ("public share", False),
        ("recordings", False),
        ("storage used", False),
        ("id", True),  # ID keeps its ascending default when selected again
    ):
        headers.nth(index[column]).locator("button").click()
        page.wait_for_timeout(300)
        assert_direction(column, first_ascending)
        headers.nth(index[column]).locator("button").click()
        page.wait_for_timeout(300)
        assert_direction(column, not first_ascending)

    _check_common(page, problems, "/admin")


def test_inquire_page(browser_page):
    page, problems = browser_page
    response = _open(page, problems, "/inquire")
    if response is None or response.status != 200 or "/inquire" not in page.url:
        pytest.skip("Inquire mode is not enabled")
    problems.failed[:] = [f for f in problems.failed if "/inquire " not in f]
    assert page.locator("#inquire-app[data-v-app]").count() == 1, "/inquire: the app did not mount"
    _check_header_translated(page, "/inquire", "#inquire-app header")
    _check_user_menu(page, "/inquire")
    _check_common(page, problems, "/inquire")


def test_a_recording_detail_view(browser_page):
    page, problems = browser_page
    listing = page.request.get(f"{BASE}/api/recordings?per_page=1")
    items = listing.json().get("recordings", []) if listing.ok else []
    if not items:
        pytest.skip("no recordings on this instance")
    path = f"/recordings/{items[0]['id']}"
    _open(page, problems, path)
    _check_user_menu(page, path)
    _check_common(page, problems, path)


_CHAT_GAP = """() => {
    const p = document.querySelector('.floating-chat-panel');
    const c = document.getElementById('mainContentColumns');
    if (!p || !c) return null;
    const r = p.getBoundingClientRect(), b = c.getBoundingClientRect();
    return {dx: Math.round(b.right - r.right), dy: Math.round(b.bottom - r.bottom),
            inside: r.top >= b.top && r.left >= b.left && r.right <= b.right && r.bottom <= b.bottom};
}"""


def test_the_chat_panel_layout_is_shared_and_anchored(browser_page):
    """One chat layout for every recording, kept as a gap to the content area's corner.

    It used to be saved per recording with absolute coordinates: the panel
    opened on some recordings only, and after a window or sidebar change it
    floated in the middle of the page.
    """
    page, _ = browser_page
    listing = page.request.get(f"{BASE}/api/recordings?per_page=2")
    items = listing.json().get("recordings", []) if listing.ok else []
    if len(items) < 2:
        pytest.skip("needs two recordings")
    first, second = (f"/recordings/{r['id']}" for r in items[:2])
    page.goto(f"{BASE}{first}")
    page.wait_for_timeout(2500)
    page.evaluate("() => localStorage.removeItem('chat_panel_layout')")
    page.click(".floating-chat-fab")
    page.wait_for_timeout(300)
    head = page.locator(".floating-chat-header-title").bounding_box()
    page.mouse.move(head["x"] + 20, head["y"] + 10)
    page.mouse.down()
    page.mouse.move(head["x"] - 80, head["y"] - 40, steps=8)
    page.mouse.up()
    page.wait_for_timeout(300)
    moved = page.evaluate(_CHAT_GAP)
    assert moved and moved["inside"]

    page.goto(f"{BASE}{second}")
    page.wait_for_timeout(2500)
    there = page.evaluate(_CHAT_GAP)
    assert there is not None, "the chat panel did not open on the second recording"
    assert there["inside"] and abs(there["dx"] - moved["dx"]) <= 1

    page.set_viewport_size({"width": 1200, "height": 900})
    page.wait_for_timeout(600)
    narrower = page.evaluate(_CHAT_GAP)
    assert narrower["inside"] and abs(narrower["dx"] - moved["dx"]) <= 1
    keys = page.evaluate("() => Object.keys(localStorage).filter(k => k.startsWith('chat_panel'))")
    assert keys == ["chat_panel_layout"]
    page.set_viewport_size({"width": 1400, "height": 900})
