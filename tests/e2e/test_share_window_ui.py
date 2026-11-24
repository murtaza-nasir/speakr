"""Browser test of the share window and the user_search_scope setting (D#519).

The server tests (tests/test_share_recipient_scope.py and others) cover who
may be found and named; this test covers the two places that expose it: the
select in Admin, System settings that saves one of the three levels, and the
share window of a recording (templates/modals/unified-share-modal.html,
composables/sharing.js), which at each level must find the right people and
nobody else, show its hints, and share with a search result and with a full
username or email address. The message for the search limit (HTTP 429) is
checked with the response replaced in the browser.

The person sharing is not a site admin: an admin may name every account.
Setup, through the app's own HTTP API as the seeded admin: internal sharing
on, three users with generated passwords (A and B in one group, C in none),
and a seeded recording shared with A with the right to share it on. For each
level the admin chooses it in the select, and A, after loading the app
again, opens the share window of that recording. Afterwards the users, the
group and the shares are removed and both settings cleared.

Each step fails on a JavaScript error, a same-origin response of 400 or
higher that the step does not expect, and template text left in the page
(tests/e2e/browser_checks.py).

It runs only with SPEAKR_BROWSER_SMOKE=1 against a running Speakr at
SPEAKR_BASE_URL, the instance of the browser smoke test. With
SPEAKR_E2E_SHOTS set, a screenshot of each main step is saved in that
directory.
"""

import os
import re
import secrets
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

pytestmark = pytest.mark.skipif(os.environ.get("SPEAKR_BROWSER_SMOKE") != "1",
                                reason="browser test; set SPEAKR_BROWSER_SMOKE=1 with Speakr running")

playwright_api = pytest.importorskip("playwright.sync_api") if os.environ.get("SPEAKR_BROWSER_SMOKE") == "1" else None

from browser_checks import PageProblems, check_common, en, login, open_page, precondition_unmet  # noqa: E402
from seed_recordings import TITLES  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")

SETTING = "user_search_scope"
LEVELS = ("everyone", "groups", "exact")
LEVEL_LABEL = {"everyone": "userSearchScopeEveryone", "groups": "userSearchScopeGroups", "exact": "userSearchScopeExact"}
MEETING_TITLE = TITLES[1]
# Names unique to this run, so a second run against the same instance works
RUN = secrets.token_hex(3)
# Part of every test username, and of nothing else
COMMON = f"sw{RUN}"
SEARCH = "/api/users/search"
WINDOW = ".modal-overlay:visible"


class Session:
    """One signed-in user: a browser context, its page and its problems."""

    def __init__(self, browser, email, password):
        self.context = browser.new_context(viewport={"width": 1400, "height": 900}, locale="en-US")
        self.page = self.context.new_page()
        self.problems = PageProblems(self.page, BASE)
        login(self.page, BASE, email, password)
        self.problems.reset()

    def open(self, path):
        return open_page(self.page, self.problems, BASE, path)

    def check(self, label):
        check_common(self.page, self.problems, label)
        missing = self.problems.unseen()
        assert not missing, f"{label}: expected responses that did not occur: {missing}"

    def api(self, method, path, data=None):
        token = self.page.locator("meta[name=csrf-token]").first.get_attribute("content")
        return self.page.request.fetch(f"{BASE}{path}", method=method, headers={"X-CSRFToken": token}, data=data)


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(SHOTS, f"{name}.png"))


class World:
    def __init__(self, browser):
        self.browser = browser
        self.users = {}
        self.admin = Session(browser, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.sharer = None
        self.recording_id = None
        self.group_id = None

    def shared_with(self):
        """User ids the recording is shared with now."""
        r = self.admin.api("GET", f"/api/recordings/{self.recording_id}/shares-internal")
        assert r.ok, f"shares: {r.status} {r.text()}"
        return {share["user_id"]: share for share in r.json().get("shares", []) if not share.get("is_owner")}

    def unshare(self, *who):
        """Take the recording back from these users, as the person who shared it with them."""
        shares = self.shared_with()
        for name in who:
            share = shares.get(self.users[name]["id"])
            if share:
                r = self.sharer.api("DELETE", f"/api/internal-shares/{share['id']}")
                assert r.ok, f"revoke: {r.status} {r.text()}"


@pytest.fixture(scope="module")
def world():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        w = World(browser)
        admin = w.admin
        admin.open("/admin")
        listing = admin.api("GET", "/api/recordings?per_page=50")
        ids = {r["title"]: r["id"] for r in (listing.json().get("recordings", []) if listing.ok else [])}
        if MEETING_TITLE not in ids:
            admin.context.close()
            browser.close()
            precondition_unmet("no seeded recordings on this instance (tests/e2e/seed_recordings.py)")
        w.recording_id = ids[MEETING_TITLE]
        try:
            r = admin.api("POST", "/admin/settings", {"key": "enable_internal_sharing", "value": "true", "setting_type": "boolean"})
            assert r.ok, f"internal sharing: {r.status} {r.text()}"
            for who in ("a", "b", "c"):
                username = f"{COMMON}{who}"
                user = {"username": username, "email": f"{username}@example.com", "password": secrets.token_urlsafe(16)}
                r = admin.api("POST", "/admin/users", user)
                assert r.status == 201, f"user {username}: {r.status} {r.text()}"
                user["id"] = r.json()["id"]
                w.users[who] = user
            r = admin.api("POST", "/api/admin/groups", {"name": f"Share window {RUN}"})
            assert r.status == 201, f"group: {r.status} {r.text()}"
            w.group_id = r.json()["id"]
            for who in ("a", "b"):
                r = admin.api("POST", f"/api/admin/groups/{w.group_id}/members", {"user_id": w.users[who]["id"], "role": "member"})
                assert r.status == 201, f"member: {r.status} {r.text()}"
            # A may share the admin's recording on
            r = admin.api("POST", f"/api/recordings/{w.recording_id}/share-internal",
                          {"user_id": w.users["a"]["id"], "can_edit": False, "can_reshare": True})
            assert r.ok, f"share with A: {r.status} {r.text()}"
            w.sharer = Session(browser, w.users["a"]["email"], w.users["a"]["password"])
            yield w
        finally:
            admin.open("/admin")
            if w.group_id:
                admin.api("DELETE", f"/api/admin/groups/{w.group_id}")
            for user in w.users.values():
                admin.api("DELETE", f"/admin/users/{user['id']}")
            # An empty value lets the .env value apply again
            admin.api("POST", "/admin/settings", {"key": SETTING, "value": "", "setting_type": "string"})
            admin.api("POST", "/admin/settings", {"key": "enable_internal_sharing", "value": "", "setting_type": "boolean"})
            if w.sharer:
                w.sharer.context.close()
            admin.context.close()
            browser.close()


# --- the admin select ---------------------------------------------------------

def setting_card(page):
    card = page.locator("div.shadow-sm", has=page.locator("h4", has_text=re.compile(rf"^{SETTING}$")))
    card.wait_for(state="visible", timeout=10000)
    return card


def choose_level(world, level):
    """Choose a level with the select in Admin, System settings, and save it."""
    admin = world.admin
    page = admin.page
    admin.open("/admin")
    page.locator("nav.tabs button.tab", has_text=en("adminDashboard.systemSettings")).first.click()
    page.wait_for_timeout(800)
    setting_card(page).locator(f'button[title="{en("buttons.editSetting")}"]').click()
    dialog = page.locator(WINDOW).last
    dialog.wait_for(state="visible", timeout=5000)
    select = dialog.locator("select")
    assert select.count() == 1, "no select for user_search_scope"
    options = select.locator("option")
    assert options.evaluate_all("els => els.map(e => e.value)") == list(LEVELS)
    assert options.all_inner_texts() == [en(f"adminDashboard.{LEVEL_LABEL[name]}") for name in LEVELS]
    select.select_option(level)
    shot(page, f"share-window-test-admin-{level}")
    with page.expect_response(lambda r: r.url.endswith("/admin/settings") and r.request.method == "POST") as response:
        dialog.locator("button", has_text=re.compile(rf"^\W*{re.escape(en('buttons.saveChanges'))}\s*$")).click()
    assert response.value.ok, f"saving {SETTING}={level}: {response.value.status}"
    page.locator(WINDOW).first.wait_for(state="detached", timeout=5000)
    # The card shows the level by its name, and the app is told the level
    label = en(f"adminDashboard.{LEVEL_LABEL[level]}")
    page.wait_for_function("([key, label]) => [...document.querySelectorAll('div.shadow-sm')].some((card) => { const h = card.querySelector('h4');"
                           " return !!h && h.textContent.trim() === key && card.innerText.includes(label); })", arg=[SETTING, label], timeout=10000)
    saved = [s for s in admin.api("GET", "/admin/settings").json() if s["key"] == SETTING][0]
    assert saved["value"] == level
    assert admin.api("GET", "/api/config").json()["user_search_scope"] == level
    admin.check(f"admin, user_search_scope {level}")


# --- the share window ---------------------------------------------------------

def open_share_window(world):
    """A loads the app again (the level is read when it starts) and opens the share window."""
    sharer = world.sharer
    page = sharer.page
    sharer.open(f"/recordings/{world.recording_id}")
    button = page.locator(f"button[title='{en('buttons.shareRecording')}']:visible").first
    button.wait_for(state="visible", timeout=30000)
    button.click()
    window = page.locator(WINDOW).last
    window.wait_for(state="visible", timeout=5000)
    window.locator("h4", has_text=en("sharing.internalSharing")).wait_for(state="visible", timeout=10000)
    # The list of people it is shared with has loaded
    window.locator("h5", has_text=en("unifiedShareModal.alreadySharedWith")).wait_for(state="visible", timeout=10000)
    page.wait_for_timeout(500)
    return window


def search(window, text):
    """Type in the search field and wait for the answer. Returns the usernames found."""
    page = window.page
    field = window.locator("input[type=search]")
    if len(text.strip()) < 2:
        field.fill(text)
        page.wait_for_timeout(500)
        return []
    with page.expect_response(lambda r: SEARCH in r.url, timeout=10000):
        field.fill(text)
    page.wait_for_function("() => !document.querySelector('.modal-overlay .fa-spinner')", timeout=10000)
    page.wait_for_timeout(200)
    return window.locator("button[aria-pressed] .truncate").all_inner_texts()


def share_selected(world, window, who):
    """Select a person from the results and share with the Add button."""
    page = window.page
    username = world.users[who]["username"]
    result = window.locator("button[aria-pressed]", has_text=username)
    result.click()
    assert result.get_attribute("aria-pressed") == "true"
    add = window.locator("button", has_text=en("sharing.addSelected").replace("{{count}}", "1"))
    with page.expect_response(lambda r: r.url.endswith(f"/api/recordings/{world.recording_id}/share-internal") and r.request.method == "POST") as response:
        add.click()
    assert response.value.ok, f"sharing with {username}: {response.value.status}"
    assert world.users[who]["id"] in world.shared_with(), f"the recording is not shared with {username}"


def toast(page, text):
    message = page.locator("#toastContainer .toast-message", has_text=text)
    message.first.wait_for(state="visible", timeout=10000)
    return message.first


def close_window(page):
    page.locator(f"{WINDOW} .modal-close").last.click()
    page.locator(WINDOW).first.wait_for(state="detached", timeout=5000)


def test_everyone_part_of_a_username_finds_every_account(world):
    choose_level(world, "everyone")
    window = open_share_window(world)
    page = window.page
    users = world.users
    assert world.sharer.api("GET", "/api/config").json()["user_search_scope"] == "everyone"

    # Nothing is searched before two characters
    hint = window.locator("#unified_share_search_hint")
    assert hint.inner_text().strip() == en("sharing.searchMinChars")
    assert search(window, "s") == []
    assert hint.is_visible()

    # Part of a username: B and C, in a group or not, and never the person searching
    assert sorted(search(window, COMMON)) == [users["b"]["username"], users["c"]["username"]]
    assert hint.count() == 0
    shot(page, "share-window-test-everyone-results")

    # Nobody by that name: no hint about the full name at this level
    assert search(window, f"{COMMON}zz") == []
    assert window.get_by_text(en("unifiedShareModal.noUsersFound")).is_visible()
    assert window.get_by_text(en("sharing.fullIdentifierHint")).count() == 0

    # Shared with a search result ...
    assert users["b"]["username"] in search(window, COMMON)
    share_selected(world, window, "b")
    toast(page, en("sharing.sharedWithCount").replace("{{count}}", "1"))
    # ... who is then no longer offered
    assert search(window, COMMON) == [users["c"]["username"]]
    # ... and with a full email address
    assert search(window, users["c"]["email"]) == [users["c"]["username"]]
    share_selected(world, window, "c")
    shot(page, "share-window-test-everyone-shared")
    close_window(page)
    world.sharer.check("share window, everyone")
    world.unshare("b", "c")


def test_groups_part_of_a_username_finds_only_people_in_a_shared_group(world):
    choose_level(world, "groups")
    window = open_share_window(world)
    page = window.page
    users = world.users
    assert window.locator("#unified_share_search_hint").inner_text().strip() == en("sharing.searchMinChars")

    # Part of a username: B, who is in A's group, and not C
    assert search(window, COMMON) == [users["b"]["username"]]
    # Part of C's name finds nobody, with the hint to type the full username or email
    assert search(window, users["c"]["username"][:-1] + "x") == []
    assert search(window, users["c"]["username"][2:]) == []
    assert window.get_by_text(en("unifiedShareModal.noUsersFound")).is_visible()
    assert window.get_by_text(en("sharing.fullIdentifierHint")).is_visible()
    shot(page, "share-window-test-groups-hint")

    # The full username finds C, and the recording is shared with that result
    assert search(window, users["c"]["username"]) == [users["c"]["username"]]
    share_selected(world, window, "c")
    # The full email address finds B
    assert search(window, users["b"]["email"].upper()) == [users["b"]["username"]]
    share_selected(world, window, "b")
    close_window(page)
    world.sharer.check("share window, groups")
    world.unshare("b", "c")


def test_exact_only_the_full_username_or_email(world):
    choose_level(world, "exact")
    window = open_share_window(world)
    page = window.page
    users = world.users
    recording = f"/api/recordings/{world.recording_id}/share-internal"

    # No search while typing: one field for the full username or email, with its hint
    assert window.locator("input[type=search]").count() == 0
    assert window.get_by_text(en("sharing.fullIdentifierHint")).is_visible()
    field = window.locator(f"input[placeholder='{en('unifiedShareModal.usernamePlaceholder')}']")
    add = window.locator("button", has_text=re.compile(rf"^\s*{re.escape(en('unifiedShareModal.add'))}\s*$")).first
    assert add.is_disabled()
    shot(page, "share-window-test-exact")

    # Part of a username finds nobody
    field.fill(COMMON)
    with page.expect_response(lambda r: SEARCH in r.url) as response:
        add.click()
    assert response.value.json() == []
    toast(page, en("sharing.userNotFound").replace("{{username}}", COMMON))
    assert world.shared_with().keys() == {users["a"]["id"]}

    # The full username, with the button
    field.fill(users["b"]["username"])
    with page.expect_response(lambda r: r.url.endswith(recording) and r.request.method == "POST") as response:
        add.click()
    assert response.value.ok, f"sharing by username: {response.value.status}"
    page.wait_for_function("(el) => el.value === ''", arg=field.element_handle(), timeout=5000)
    assert users["b"]["id"] in world.shared_with()

    # The full email address, with Enter
    field.fill(users["c"]["email"])
    with page.expect_response(lambda r: r.url.endswith(recording) and r.request.method == "POST") as response:
        field.press("Enter")
    assert response.value.ok, f"sharing by email: {response.value.status}"
    page.wait_for_function("(el) => el.value === ''", arg=field.element_handle(), timeout=5000)
    assert set(world.shared_with()) == {users["a"]["id"], users["b"]["id"], users["c"]["id"]}
    # Both are in the list of people it is shared with
    listed = window.locator("h5", has_text=en("unifiedShareModal.alreadySharedWith")).locator("xpath=..")
    page.wait_for_function("(el) => el.querySelectorAll('button[title]').length >= 2", arg=listed.element_handle(), timeout=10000)
    shot(page, "share-window-test-exact-shared")

    # The search limit: the answer says so
    world.sharer.problems.expect(429, SEARCH)
    page.route(f"**{SEARCH}*", lambda route: route.fulfill(status=429, headers={"Retry-After": "60"}, body="Too Many Requests"))
    try:
        field.fill(f"{COMMON}nobody")
        add.click()
        toast(page, en("sharing.searchRateLimited"))
    finally:
        page.unroute(f"**{SEARCH}*")
    close_window(page)
    world.sharer.check("share window, exact")
    world.unshare("b", "c")


def test_the_search_limit_is_reported_while_typing(world):
    choose_level(world, "everyone")
    window = open_share_window(world)
    page = window.page
    world.sharer.problems.expect(429, SEARCH)
    page.route(f"**{SEARCH}*", lambda route: route.fulfill(status=429, headers={"Retry-After": "60"}, body="Too Many Requests"))
    try:
        with page.expect_response(lambda r: SEARCH in r.url and r.status == 429):
            window.locator("input[type=search]").fill(COMMON)
        message = toast(page, en("sharing.searchRateLimited"))
        assert "429" not in message.inner_text()
        # No results and no spinner left behind
        page.wait_for_function("() => !document.querySelector('.modal-overlay .fa-spinner')", timeout=5000)
        assert window.locator("button[aria-pressed]").count() == 0
        shot(page, "share-window-test-search-limit")
    finally:
        page.unroute(f"**{SEARCH}*")
    # The next search works again
    assert sorted(search(window, COMMON + "b")) == [world.users["b"]["username"]]
    close_window(page)
    world.sharer.check("share window, search limit")
