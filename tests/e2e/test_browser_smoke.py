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

from browser_checks import PageProblems, check_common, login, open_page, precondition_unmet  # noqa: E402
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


USER_SORT_STORAGE_KEY = "speakr.adminUserSort"
# Sort key of each column of the User Management table, in the order of the
# columns, with the direction of the first click.
USER_SORT_COLUMNS = (
    ("id", "id", "asc"),
    ("username", "username", "asc"),
    ("email", "email", "asc"),
    ("admin", "is_admin", "desc"),
    ("public share", "can_share_publicly", "desc"),
    ("recordings", "recordings_count", "desc"),
    ("storage used", "storage_used", "desc"),
    ("last active", "last_active", "desc"),
)


def _admin_users_table(page):
    """The User Management table (the only one with a Public share column)."""
    table = page.locator("table").filter(
        has=page.locator("th", has_text="Public share")).first
    table.locator("th", has_text="Public share").first.wait_for(
        state="visible", timeout=15000)
    return table


def _admin_user_ids(table):
    """The ids of the listed users, in the order of the rows."""
    return [int(text) for text in table.locator("tbody tr.user-row td:first-child").all_inner_texts()]


def _is_user_list_request(response, **params):
    """True for a response of GET /admin/users with these query parameters."""
    if "/admin/users?" not in response.url or response.request.method != "GET":
        return False
    query = dict(pair.split("=", 1) for pair in response.url.split("?", 1)[1].split("&"))
    return all(query.get(name) == str(value) for name, value in params.items())


def _forget_user_sort(page):
    page.evaluate("key => { try { localStorage.removeItem(key); } catch (e) {} }", USER_SORT_STORAGE_KEY)


def _expected_user_order(users, key, order):
    """The ids of all users in the order of ``key``: equal values by id."""
    def value(user):
        if key == "last_active":
            # Never used first in ascending order, last in descending order.
            return (user["last_seen_at"] is not None, user["last_seen_at"] or "")
        if key in ("username", "email"):
            return user[key].lower()
        if key in ("is_admin", "can_share_publicly"):
            return bool(user[key])
        return user[key]
    if key == "id":
        return sorted((user["id"] for user in users), reverse=(order == "desc"))
    rows = sorted(users, key=lambda user: user["id"])
    rows.sort(key=value, reverse=(order == "desc"))
    return [user["id"] for user in rows]


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

    # Sorting: the list is ordered by id at first, so the column has no
    # aria-sort. The first click asks the server for the most recent first.
    assert header.get_attribute("aria-sort") is None
    with page.expect_response(
            lambda r: _is_user_list_request(r, sort="last_active")) as sorted_response:
        header.locator("button").click()
    assert "order=desc" in sorted_response.value.url, sorted_response.value.url
    assert sorted_response.value.ok
    page.wait_for_function(
        "el => el.getAttribute('aria-sort') === 'descending'", arg=header.element_handle())
    with page.expect_response(
            lambda r: _is_user_list_request(r, sort="last_active", order="asc")):
        header.locator("button").click()
    page.wait_for_function(
        "el => el.getAttribute('aria-sort') === 'ascending'", arg=header.element_handle())
    assert check_rows(), "/admin: no user rows after sorting"
    _forget_user_sort(page)
    _check_common(page, problems, "/admin")


def test_admin_users_table_sorting(browser_page):
    """Every data column of the User Management table is sorted on the server.

    The list is paged and searched on the server, so an order made from the
    rows of one page would be wrong. Seventeen accounts are added, which is
    more than one page of fifteen, with usernames in the reverse of the order
    of their ids. For each column and both directions the ids of the rows of
    all pages, read from the table, must be the order of all accounts. The
    seeded recordings belong to the first admin, so that account moves to the
    last page when the list is sorted by recordings in ascending order.

    Also checked: the direction of the first click and its reverse, aria-sort
    on the sorted column only, the first page after a change of the sort, the
    sort with a search, the rows kept and marked busy while a page is loading,
    a late response that is not shown, the sort kept after a reload, and the
    sort control above the table at phone width.
    """
    page, problems = browser_page
    _open(page, problems, "/admin")
    _forget_user_sort(page)
    run = os.urandom(3).hex()
    csrf = page.locator("meta[name=csrf-token]").get_attribute("content")
    created = []

    def api(method, path, data=None):
        return page.request.fetch(f"{BASE}{path}", method=method,
                                  headers={"X-CSRFToken": csrf}, data=data)

    try:
        for number in range(17):
            # Later ids have earlier names; some names begin with a capital.
            letter = "zyxwvutsrqponmlkj"[number]
            username = f"{letter.upper() if number % 3 == 0 else letter}sort{run}"
            response = api("POST", "/admin/users", {
                "username": username, "email": f"{number:02d}sort{run}@example.com",
                "password": f"Sort-{os.urandom(9).hex()}", "is_admin": number % 5 == 0})
            assert response.status == 201, f"user {username}: {response.status} {response.text()}"
            created.append(response.json()["id"])

        _open(page, problems, "/admin")
        everyone = api("GET", "/admin/users?per_page=1000").json()["users"]
        assert len(everyone) > 15, "/admin: the accounts do not fill more than one page"
        # The instance of the image smoke test has no recordings; the check
        # of the account with recordings is left out there.
        has_recordings = any(user["recordings_count"] for user in everyone)

        table = _admin_users_table(page)
        headers = table.locator("thead th")
        labels = [h.strip().lower() for h in headers.all_inner_texts()]
        assert labels == [label for label, _, _ in USER_SORT_COLUMNS] + ["actions"], (
            f"/admin: unexpected User Management columns: {labels}")
        index = {label: i for i, label in enumerate(labels)}
        assert headers.nth(index["actions"]).locator("button").count() == 0, (
            "/admin: the Actions column should not be sortable")
        assert not page.locator("[data-user-sort-compact]").is_visible(), (
            "/admin: the sort control for narrow widths is shown at 1400 px")

        def assert_marked(label, order):
            """aria-sort is on this column only, and the table is not busy."""
            page.wait_for_function(
                "([el, want]) => el.getAttribute('aria-sort') === want",
                arg=[headers.nth(index[label]).element_handle(),
                     "ascending" if order == "asc" else "descending"])
            for other in labels[:-1]:
                if other != label:
                    assert headers.nth(index[other]).get_attribute("aria-sort") is None, (
                        f"/admin: {other} has aria-sort while {label} is sorted")
            page.wait_for_function("el => el.getAttribute('aria-busy') === 'false'",
                                   arg=table.element_handle())

        def ids_of_all_pages(key, order):
            """The ids of the first page (shown now) and of every next page."""
            listed = _admin_user_ids(table)
            assert len(listed) == 15, f"/admin: {len(listed)} rows on the first page"
            next_page = page.locator("button[aria-label='Next page']")
            page_number = 1
            while next_page.is_enabled():
                page_number += 1
                with page.expect_response(lambda r: _is_user_list_request(
                        r, page=page_number, sort=key, order=order)):
                    next_page.click()
                page.wait_for_function(
                    "([el, first]) => el.getAttribute('aria-busy') === 'false'"
                    " && el.querySelector('tbody tr.user-row td').innerText.trim() !== String(first)",
                    arg=[table.element_handle(), listed[-15] if len(listed) >= 15 else -1])
                listed.extend(_admin_user_ids(table))
            return listed

        # Without a stored sort the list is in the order of the ids.
        assert_marked("id", "asc")
        assert ids_of_all_pages("id", "asc") == _expected_user_order(everyone, "id", "asc")

        for label, key, first_order in USER_SORT_COLUMNS[1:] + USER_SORT_COLUMNS[:1]:
            for order in (first_order, "desc" if first_order == "asc" else "asc"):
                # The change is requested from the server and starts at page 1,
                # although the last page was open.
                with page.expect_response(lambda r: _is_user_list_request(
                        r, sort=key, order=order, page=1, per_page=15)) as response:
                    headers.nth(index[label]).locator("button").click()
                body = response.value.json()
                assert (body["sort"], body["order"]) == (key, order)
                assert_marked(label, order)
                listed = ids_of_all_pages(key, order)
                assert listed == _expected_user_order(everyone, key, order), (
                    f"/admin: {key} {order} is not the order of all accounts: {listed}")

        if has_recordings:
            # Recordings ascending: the owner of the seeded recordings is on the
            # last page, which an order of the first page alone could not show.
            owner = max(everyone, key=lambda user: user["recordings_count"])["id"]
            with page.expect_response(lambda r: _is_user_list_request(
                    r, sort="recordings_count", order="desc")):
                headers.nth(index["recordings"]).locator("button").click()
            assert_marked("recordings", "desc")
            assert _admin_user_ids(table)[0] == owner
            with page.expect_response(lambda r: _is_user_list_request(
                    r, sort="recordings_count", order="asc")):
                headers.nth(index["recordings"]).locator("button").click()
            assert_marked("recordings", "asc")
            assert owner not in _admin_user_ids(table), (
                "/admin: the account with recordings is on the first page of the ascending order")

        # A search keeps the sort, and its result is in that order.
        with page.expect_response(lambda r: _is_user_list_request(
                r, sort="username", order="asc")):
            headers.nth(index["username"]).locator("button").click()
        assert_marked("username", "asc")
        with page.expect_response(lambda r: _is_user_list_request(
                r, search=f"sort{run}", sort="username", order="asc", page=1)):
            page.get_by_placeholder("Search users").fill(f"sort{run}")
        page.wait_for_function(
            "([el, want]) => el.getAttribute('aria-busy') === 'false'"
            " && el.querySelector('tbody tr.user-row td').innerText.trim() === String(want)",
            arg=[table.element_handle(), created[-1]])
        mine = [user for user in everyone if user["id"] in created]
        found = ids_of_all_pages("username", "asc")
        assert found == _expected_user_order(mine, "username", "asc")
        assert found[0] == created[-1], (
            "/admin: the name that is first in the alphabet belongs to the last id")

        # While a page is loading the rows stay and the table is marked busy.
        # The request for the order by email is held back; the order by id is
        # requested meanwhile, and the held request, cancelled or answered
        # late, does not change the table.
        held = []
        page.route("**/admin/users?*sort=email*", lambda route: held.append(route))
        shown = _admin_user_ids(table)
        assert shown, "/admin: no rows before the held request"
        headers.nth(index["email"]).locator("button").click()
        page.wait_for_function("el => el.getAttribute('aria-busy') === 'true'",
                               arg=table.element_handle())
        assert _admin_user_ids(table) == shown, "/admin: the rows were changed while loading"
        assert headers.nth(index["email"]).get_attribute("aria-sort") == "ascending"
        with page.expect_response(lambda r: _is_user_list_request(
                r, sort="id", order="asc")):
            headers.nth(index["id"]).locator("button").click()
        assert_marked("id", "asc")
        assert len(held) == 1, f"/admin: {len(held)} requests for the order by email"
        try:
            held[0].continue_()
        except Exception:  # cancelled by the page when the newer request was made
            pass
        page.unroute("**/admin/users?*sort=email*")
        page.wait_for_timeout(1000)
        assert_marked("id", "asc")
        assert _admin_user_ids(table) == _expected_user_order(mine, "id", "asc")[:15]
        with page.expect_response(lambda r: _is_user_list_request(
                r, sort="id", order="desc")):
            headers.nth(index["id"]).locator("button").click()
        assert_marked("id", "desc")

        # The sort is kept for this browser: after a reload the first request
        # asks for it and the header shows it.
        with page.expect_response(lambda r: _is_user_list_request(r, sort="id", order="desc")):
            page.reload()
        table = _admin_users_table(page)
        headers = table.locator("thead th")
        assert_marked("id", "desc")

        # At phone width the table is scrolled sideways; the sort is offered
        # above it as a select with a button for the direction.
        page.set_viewport_size({"width": 390, "height": 800})
        compact = page.locator("[data-user-sort-compact]")
        compact.wait_for(state="visible", timeout=5000)
        assert compact.locator("select").input_value() == "id"
        with page.expect_response(lambda r: _is_user_list_request(
                r, sort="storage_used", order="desc", page=1)):
            compact.locator("select").select_option("storage_used")
        assert_marked("storage used", "desc")
        with page.expect_response(lambda r: _is_user_list_request(
                r, sort="storage_used", order="asc", page=1)):
            compact.locator("[data-user-sort-direction]").click()
        assert_marked("storage used", "asc")
        assert "Ascending" in compact.locator("[data-user-sort-direction]").inner_text()
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, f"/admin: the page is {overflow} px wider than the phone screen"
        page.set_viewport_size({"width": 1400, "height": 900})

        _check_common(page, problems, "/admin")
    finally:
        page.set_viewport_size({"width": 1400, "height": 900})
        _forget_user_sort(page)
        for user_id in created:
            api("DELETE", f"/admin/users/{user_id}")


def test_inquire_page(browser_page):
    page, problems = browser_page
    response = _open(page, problems, "/inquire")
    if response is None or response.status != 200 or "/inquire" not in page.url:
        precondition_unmet("Inquire mode is not enabled")
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
        precondition_unmet("no recordings on this instance")
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
        precondition_unmet("needs two recordings")
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
