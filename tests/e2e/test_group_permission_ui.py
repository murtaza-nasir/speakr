"""Browser test of who can create groups (#576), in a real browser.

The server tests (tests/test_group_creation_permission.py) cover the rules;
this test covers the pages that expose them: the permission_create_groups
System Setting with its three radio buttons, the "Can create groups" toggle
in the admin edit-user dialog, the "Members can create groups" toggle in the
admin group dialog, the Group Management entry in the account menu, and the
group management page of a user who is not a site admin (create, duplicate
name, add a member by exact username, make a member a group admin, delete).

The steps run in file order and build on each other. Setup, through the
app's own HTTP API as the seeded admin: internal sharing on (groups need it),
the permission mode set to admins, four users with generated passwords and a
group with user C as its member. Every user signs in through the login form.
Afterwards the users and groups are removed and both settings restored.

Each step fails on a JavaScript error, a same-origin response of 400 or
higher that the step does not expect, and template text left in the page
(tests/e2e/browser_checks.py, shared with test_browser_smoke.py).

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

from browser_checks import PageProblems, check_common, en, login, open_page  # noqa: E402
from smoke_env import read_example  # noqa: E402

BASE = os.environ.get("SPEAKR_BASE_URL", "http://127.0.0.1:8899").rstrip("/")
_example = read_example()
ADMIN_EMAIL = os.environ.get("SPEAKR_SMOKE_USER", _example.get("ADMIN_EMAIL", "admin@example.com"))
ADMIN_PASSWORD = os.environ.get("SPEAKR_SMOKE_PASSWORD", _example.get("ADMIN_PASSWORD", "changeme"))
SHOTS = os.environ.get("SPEAKR_E2E_SHOTS")

SETTING = "permission_create_groups"
GROUPS_PAGE = "/group-management"
# Names unique to this run, so a second run against the same instance works
RUN = secrets.token_hex(3)


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

    def check(self, path):
        check_common(self.page, self.problems, path)
        missing = self.problems.unseen()
        assert not missing, f"{path}: expected responses that did not occur: {missing}"

    def csrf(self):
        return self.page.locator("meta[name=csrf-token]").get_attribute("content")

    def api(self, method, path, data=None):
        """A request through the app's HTTP API with this user's session."""
        headers = {"X-CSRFToken": self.csrf()}
        response = self.page.request.fetch(f"{BASE}{path}", method=method, headers=headers, data=data)
        return response

    def menu_has_group_management(self):
        page = self.page
        page.locator("[data-user-menu-toggle]:visible").first.click()
        menu = page.locator("[data-user-menu-dropdown]:visible").first
        menu.wait_for(state="visible", timeout=5000)
        present = menu.locator(f'a[href="{GROUPS_PAGE}"]').count() == 1
        if present:
            text = menu.locator(f'a[href="{GROUPS_PAGE}"]').inner_text().strip()
            assert text == en("nav.groupManagement"), f"menu entry reads {text!r}"
        return present

    def close_menu(self):
        self.page.keyboard.press("Escape")
        self.page.mouse.click(5, 400)


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        # Let dialog and toggle transitions finish
        page.wait_for_timeout(400)
        page.screenshot(path=os.path.join(SHOTS, f"{name}.png"), full_page=True)


def button(scope, label):
    """The button whose text is the label, with or without an icon.

    The accessible name of a button with a Font Awesome icon includes the
    icon glyph, so get_by_role with an exact name does not find it.
    """
    return scope.locator("button", has_text=re.compile(rf"^\W*{re.escape(label)}\s*$"))


def modal(page):
    overlay = page.locator(".modal-overlay:visible").last
    overlay.wait_for(state="visible", timeout=5000)
    # A dialog covers the page; without primitives.css it sat below the footer
    position = overlay.evaluate("el => getComputedStyle(el).position")
    assert position == "fixed", f"the dialog is not an overlay (position {position})"
    return overlay


def wait_closed(page):
    page.locator(".modal-overlay:visible").first.wait_for(state="detached", timeout=5000)


class World:
    def __init__(self, browser):
        self.browser = browser
        self.sessions = {}
        self.users = {}
        self.groups = {}

    def session(self, who):
        if who not in self.sessions:
            if who == "admin":
                self.sessions[who] = Session(self.browser, ADMIN_EMAIL, ADMIN_PASSWORD)
            else:
                user = self.users[who]
                self.sessions[who] = Session(self.browser, user["email"], user["password"])
        return self.sessions[who]


@pytest.fixture(scope="module")
def world():
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        w = World(browser)
        admin = w.session("admin")
        admin.open("/admin")
        for key, value, kind in (("enable_internal_sharing", "true", "boolean"), (SETTING, "admins", "string")):
            r = admin.api("POST", "/admin/settings", {"key": key, "value": value, "setting_type": kind})
            assert r.ok, f"setting {key}: {r.status} {r.text()}"
        for who in ("a", "b", "c", "d"):
            username = f"e2e{who}_{RUN}"
            user = {"username": username, "email": f"{username}@example.com",
                    "password": secrets.token_urlsafe(16)}
            r = admin.api("POST", "/admin/users", user)
            assert r.status == 201, f"user {username}: {r.status} {r.text()}"
            user["id"] = r.json()["id"]
            w.users[who] = user
        # A group with user C as its member, for the group grant
        r = admin.api("POST", "/api/admin/groups", {"name": f"Team C {RUN}"})
        assert r.status == 201, f"group: {r.status} {r.text()}"
        w.groups["c_team"] = r.json()
        r = admin.api("POST", f"/api/admin/groups/{w.groups['c_team']['id']}/members",
                      {"user_id": w.users["c"]["id"], "role": "member"})
        assert r.status == 201, f"member: {r.status} {r.text()}"
        yield w
        _teardown(w)
        for s in w.sessions.values():
            s.context.close()
        browser.close()


def _teardown(w):
    admin = w.session("admin")
    admin.open("/admin")
    r = admin.api("GET", "/api/admin/groups")
    if r.ok:
        for group in r.json().get("groups", []):
            if group["name"].endswith(RUN):
                admin.api("DELETE", f"/api/admin/groups/{group['id']}")
    for user in w.users.values():
        admin.api("DELETE", f"/admin/users/{user['id']}")
    admin.api("POST", "/admin/settings", {"key": SETTING, "value": "admins", "setting_type": "string"})
    # An empty value lets the .env value apply again
    admin.api("POST", "/admin/settings", {"key": "enable_internal_sharing", "value": "", "setting_type": "boolean"})


# --- Admin page helpers ---

def admin_tab(page, label):
    page.locator("nav.tabs button.tab", has_text=label).first.click()
    page.wait_for_timeout(800)


def setting_card(page):
    card = page.locator("div.shadow-sm", has=page.locator("h4", has_text=re.compile(rf"^{SETTING}$")))
    card.wait_for(state="visible", timeout=10000)
    return card


def user_row(page, email):
    row = page.locator(f'tbody tr:has(td:text-is("{email}"))')
    row.wait_for(state="visible", timeout=10000)
    return row


def group_row(page, name):
    return page.locator(f'tbody tr:has(div:text-is("{name}"))')


def set_permission_mode(admin, mode, shot_name):
    """Choose a mode with the radio buttons in Admin, System settings."""
    page = admin.page
    admin.open("/admin")
    admin_tab(page, en("adminDashboard.systemSettings"))
    setting_card(page).locator(f'button[title="{en("buttons.editSetting")}"]').click()
    dialog = modal(page)
    radios = dialog.locator("input[type=radio]")
    assert radios.count() == 3, f"{radios.count()} radio buttons for {SETTING}"
    label = {"admins": "permissionModeAdmins", "selected": "permissionModeSelected",
             "everyone": "permissionModeEveryone"}[mode]
    dialog.locator("label", has_text=en(f"adminDashboard.{label}")).click()
    assert dialog.locator(f'input[type=radio][value="{mode}"]').is_checked()
    shot(page, shot_name)
    with page.expect_response(lambda r: r.url.endswith("/admin/settings") and r.request.method == "POST") as resp:
        button(dialog, en("buttons.saveChanges")).click()
    assert resp.value.ok, f"saving {SETTING}: {resp.value.status}"
    wait_closed(page)
    return en(f"adminDashboard.{label}")


# --- Steps ---

def test_default_mode_hides_group_management_from_a_plain_user(world):
    d = world.session("d")
    d.open("/")
    assert not d.menu_has_group_management(), "a plain user sees Group Management in admins mode"
    shot(d.page, "01-plain-user-menu")
    d.close_menu()
    d.check("/")

    d.open(GROUPS_PAGE)
    assert GROUPS_PAGE not in d.page.url, f"{GROUPS_PAGE} opened for a plain user: {d.page.url}"
    shot(d.page, "02-plain-user-group-management-redirect")
    d.check(GROUPS_PAGE)
    r = d.api("GET", "/api/admin/groups")
    assert r.status == 403


def test_admin_sets_mode_selected_with_the_radio_buttons(world):
    admin = world.session("admin")
    label = set_permission_mode(admin, "selected", "03-admin-mode-selected-dialog")
    page = admin.page
    assert label in setting_card(page).inner_text()
    admin.check("/admin")

    admin.open("/admin")
    admin_tab(page, en("adminDashboard.systemSettings"))
    card = setting_card(page)
    assert label in card.inner_text(), f"after reload the setting reads {card.inner_text()!r}"
    card.locator(f'button[title="{en("buttons.editSetting")}"]').click()
    dialog = modal(page)
    assert dialog.locator('input[type=radio][value="selected"]').is_checked()
    shot(page, "04-admin-mode-selected-after-reload")
    button(dialog, en("buttons.cancel")).click()
    admin.check("/admin")


def test_admin_grants_user_a_in_the_edit_user_dialog(world):
    admin, a = world.session("admin"), world.users["a"]
    page = admin.page
    admin.open("/admin")
    toggle_label = en("adminDashboard.canCreateGroups")
    user_row(page, a["email"]).locator(f'button[title="{en("buttons.editUser")}"]').click()
    dialog = modal(page)
    checkbox = dialog.locator("label", has_text=toggle_label).locator("input[type=checkbox]")
    assert not checkbox.is_checked()
    dialog.locator("label", has_text=toggle_label).click()
    assert checkbox.is_checked()
    shot(page, "05-admin-edit-user-a-can-create-groups")
    with page.expect_response(lambda r: f"/admin/users/{a['id']}" in r.url and r.request.method == "PUT") as resp:
        button(dialog, en("adminDashboard.updateUser")).click()
    assert resp.value.ok
    wait_closed(page)
    admin.check("/admin")

    admin.open("/admin")
    user_row(page, a["email"]).locator(f'button[title="{en("buttons.editUser")}"]').click()
    dialog = modal(page)
    assert dialog.locator("label", has_text=toggle_label).locator("input[type=checkbox]").is_checked(), \
        "Can create groups is off after a reload"
    shot(page, "06-admin-edit-user-a-after-reload")
    button(dialog, en("buttons.cancel")).click()

    # A site admin can already create groups: no toggle
    user_row(page, ADMIN_EMAIL.lower()).locator(f'button[title="{en("buttons.editUser")}"]').click()
    dialog = modal(page)
    assert dialog.locator("label", has_text=toggle_label).count() == 0, "the toggle is shown for a site admin"
    shot(page, "07-admin-edit-site-admin-no-toggle")
    button(dialog, en("buttons.cancel")).click()
    admin.check("/admin")


def _create_group(session, name):
    page = session.page
    button(page, en("groups.createTeam")).first.click()
    dialog = modal(page)
    dialog.locator("input[required]").fill(name)
    with page.expect_response(lambda r: r.url.endswith("/api/admin/groups") and r.request.method == "POST") as resp:
        dialog.locator("button[type=submit]").click()
    return dialog, resp.value


def test_user_a_creates_a_group_and_a_duplicate_name_is_refused(world):
    a = world.session("a")
    page = a.page
    a.open("/")
    assert a.menu_has_group_management(), "user A has no Group Management entry"
    shot(page, "08-user-a-menu")
    page.locator(f'[data-user-menu-dropdown]:visible a[href="{GROUPS_PAGE}"]').click()
    page.wait_for_url(f"**{GROUPS_PAGE}")
    page.wait_for_timeout(1500)

    name = f"Research {RUN}"
    _, resp = _create_group(a, name)
    assert resp.status == 201, f"create: {resp.status} {resp.text()}"
    world.groups["a_group"] = resp.json()
    wait_closed(page)
    row = group_row(page, name)
    row.wait_for(state="visible", timeout=5000)
    assert row.locator(f'button[title="{en("tooltips.deleteTeam")}"]').count() == 1, "no delete button for the creator"

    row.locator(f'button[title="{en("tooltips.manageMembers")}"]').click()
    dialog = modal(page)
    member = dialog.locator("div.p-3.justify-between", has_text=world.users["a"]["username"])
    member.wait_for(state="visible", timeout=5000)
    assert member.locator("select").input_value() == "admin", "the creator is not the group's admin"
    shot(page, "09-user-a-group-created-members")
    button(dialog, en("common.done")).click()
    wait_closed(page)
    shot(page, "10-user-a-group-list")

    a.problems.expect(400, "/api/admin/groups")
    dialog, resp = _create_group(a, f"  {name.upper()}   ")
    assert resp.status == 400 and resp.json().get("code") == "group_name_exists"
    message = en("server.apiError.group_name_exists")
    playwright_api.expect(dialog.get_by_text(message, exact=True)).to_be_visible()
    shot(page, "11-user-a-duplicate-name-refused")
    button(dialog, en("buttons.cancel")).click()
    wait_closed(page)
    assert group_row(page, name).count() == 1
    a.check(GROUPS_PAGE)


def test_user_a_adds_user_b_by_exact_username_and_makes_b_a_group_admin(world):
    a, b = world.session("a"), world.users["b"]
    page = a.page
    name = world.groups["a_group"]["name"]
    a.open(GROUPS_PAGE)
    group_row(page, name).locator(f'button[title="{en("tooltips.manageMembers")}"]').click()
    dialog = modal(page)
    field = dialog.locator(f'input[placeholder="{en("groups.memberIdentifierPlaceholder")}"]')
    add = button(dialog, en("unifiedShareModal.add"))

    a.problems.expect(404, f"/api/admin/groups/{world.groups['a_group']['id']}/members")
    field.fill(b["username"][:-2])
    with page.expect_response(lambda r: r.url.endswith("/members") and r.request.method == "POST") as resp:
        add.click()
    assert resp.value.status == 404 and resp.value.json().get("code") == "user_not_found"
    playwright_api.expect(dialog.get_by_text(en("server.apiError.user_not_found"), exact=True)).to_be_visible()
    assert dialog.locator("div.p-3.justify-between", has_text=b["username"]).count() == 0
    shot(page, "12-user-a-partial-username-refused")

    field.fill(b["username"])
    with page.expect_response(lambda r: r.url.endswith("/members") and r.request.method == "POST") as resp:
        add.click()
    assert resp.value.status == 201, f"add: {resp.value.status} {resp.value.text()}"
    member = dialog.locator("div.p-3.justify-between", has_text=b["username"])
    member.wait_for(state="visible", timeout=5000)
    with page.expect_response(lambda r: f"/members/{b['id']}" in r.url and r.request.method == "PUT") as resp:
        member.locator("select").select_option("admin")
    assert resp.value.ok, f"role: {resp.value.status}"
    shot(page, "13-user-a-b-is-group-admin")
    button(dialog, en("common.done")).click()
    wait_closed(page)
    a.check(GROUPS_PAGE)

    a.open(GROUPS_PAGE)
    group_row(page, name).locator(f'button[title="{en("tooltips.manageMembers")}"]').click()
    dialog = modal(page)
    member = dialog.locator("div.p-3.justify-between", has_text=b["username"])
    member.wait_for(state="visible", timeout=5000)
    assert member.locator("select").input_value() == "admin", "B's role did not persist"
    button(dialog, en("common.done")).click()
    a.check(GROUPS_PAGE)


def test_group_admin_b_has_no_delete_button(world):
    b = world.session("b")
    page = b.page
    b.open("/")
    assert b.menu_has_group_management(), "group admin B has no Group Management entry"
    b.close_menu()
    b.open(GROUPS_PAGE)
    assert GROUPS_PAGE in page.url
    row = group_row(page, world.groups["a_group"]["name"])
    row.wait_for(state="visible", timeout=5000)
    assert row.locator(f'button[title="{en("tooltips.manageMembers")}"]').count() == 1
    assert row.locator(f'button[title="{en("tooltips.deleteTeam")}"]').count() == 0, "B sees a delete button"
    # B holds no grant and the mode is selected: no create button
    assert button(page, en("groups.createTeam")).count() == 0
    shot(page, "14-group-admin-b-no-delete")
    b.check(GROUPS_PAGE)
    r = b.api("DELETE", f"/api/admin/groups/{world.groups['a_group']['id']}")
    assert r.status == 403 and r.json().get("code") == "group_delete_not_permitted", f"{r.status} {r.text()}"


def test_members_of_a_granted_group_can_create_groups(world):
    admin = world.session("admin")
    page = admin.page
    team = world.groups["c_team"]["name"]
    toggle_label = en("groups.membersCanCreateGroups")

    admin.open("/admin")
    admin_tab(page, en("adminDashboard.groupsTab"))
    group_row(page, team).locator(f'button[title="{en("tooltips.editTeam")}"]').click()
    dialog = modal(page)
    checkbox = dialog.locator("label", has_text=toggle_label).locator("input[type=checkbox]")
    assert not checkbox.is_checked()
    dialog.locator("label", has_text=toggle_label).click()
    assert checkbox.is_checked()
    shot(page, "15-admin-group-members-can-create")
    with page.expect_response(lambda r: "/api/admin/groups/" in r.url and r.request.method == "PUT") as resp:
        button(dialog, en("groups.updateTeam")).click()
    assert resp.value.ok
    wait_closed(page)
    admin.check("/admin")

    admin.open("/admin")
    admin_tab(page, en("adminDashboard.groupsTab"))
    group_row(page, team).locator(f'button[title="{en("tooltips.editTeam")}"]').click()
    dialog = modal(page)
    assert dialog.locator("label", has_text=toggle_label).locator("input[type=checkbox]").is_checked(), \
        "Members can create groups is off after a reload"
    button(dialog, en("buttons.cancel")).click()
    admin.check("/admin")

    c = world.session("c")
    c.open("/")
    assert c.menu_has_group_management(), "a member of the granted group has no Group Management entry"
    c.close_menu()
    c.open(GROUPS_PAGE)
    name = f"Field team {RUN}"
    _, resp = _create_group(c, name)
    assert resp.status == 201, f"create: {resp.status} {resp.text()}"
    wait_closed(c.page)
    row = group_row(c.page, name)
    row.wait_for(state="visible", timeout=5000)
    assert row.locator(f'button[title="{en("tooltips.deleteTeam")}"]').count() == 1
    shot(c.page, "16-user-c-created-group")
    c.check(GROUPS_PAGE)


def test_mode_everyone_lets_a_user_without_a_grant_create(world):
    admin = world.session("admin")
    label = set_permission_mode(admin, "everyone", "17-admin-mode-everyone-dialog")
    assert label in setting_card(admin.page).inner_text()
    admin.check("/admin")

    d = world.session("d")
    d.open("/")
    assert d.menu_has_group_management(), "in mode everyone user D has no Group Management entry"
    d.close_menu()
    d.open(GROUPS_PAGE)
    assert GROUPS_PAGE in d.page.url
    name = f"Open group {RUN}"
    _, resp = _create_group(d, name)
    assert resp.status == 201, f"create: {resp.status} {resp.text()}"
    wait_closed(d.page)
    group_row(d.page, name).wait_for(state="visible", timeout=5000)
    shot(d.page, "18-user-d-created-group")
    d.check(GROUPS_PAGE)


def test_user_a_deletes_their_own_group(world):
    a = world.session("a")
    page = a.page
    name = world.groups["a_group"]["name"]
    a.open(GROUPS_PAGE)
    group_row(page, name).locator(f'button[title="{en("tooltips.deleteTeam")}"]').click()
    dialog = modal(page)
    shot(page, "19-user-a-delete-confirm")
    with page.expect_response(lambda r: "/api/admin/groups/" in r.url and r.request.method == "DELETE") as resp:
        button(dialog, en("groups.deleteTeam")).click()
    assert resp.value.ok, f"delete: {resp.value.status}"
    wait_closed(page)
    page.wait_for_timeout(800)
    assert group_row(page, name).count() == 0, "the group is still listed"
    shot(page, "20-user-a-group-deleted")
    a.check(GROUPS_PAGE)
