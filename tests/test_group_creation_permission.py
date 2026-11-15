"""The create_groups permission and group ownership (#576).

Covers the resolver (every grant path and every mode), who may create and
delete a group, how a group admin who is not a site admin adds members,
instance-wide unique group names, the admin endpoints that set the grants
and the mode, and the API v1 fields.

SHARED-DB: users, groups, grants and the mode setting are removed or reset
afterwards.
"""

import os
import sys
import uuid
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.api.groups as groups_module
from src.app import app, db
from src.auth import permissions
from src.auth.permissions import CREATE_GROUPS, set_group_grant, set_user_grant, user_can
from src.models import Group, GroupMembership, PermissionGrant, SystemSetting, User
from src.utils.localization import translate

app.config["WTF_CSRF_ENABLED"] = False

MODE_KEY = "permission_create_groups"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _user(prefix="u", is_admin=False, **extra):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"{prefix[:10]}_{suffix}", email=f"{prefix}_{suffix}@local.test",
                password="x", is_admin=is_admin, **extra)
    db.session.add(user)
    db.session.commit()
    return user


def _group(name=None, created_by=None, admins=(), members=()):
    group = Group(name=name or f"g_{uuid.uuid4().hex[:8]}", created_by=created_by)
    db.session.add(group)
    db.session.flush()
    for user in admins:
        db.session.add(GroupMembership(group_id=group.id, user_id=user.id, role="admin"))
    for user in members:
        db.session.add(GroupMembership(group_id=group.id, user_id=user.id, role="member"))
    db.session.commit()
    return group


def _client(user):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    from flask import g
    for attr in ("_login_user", "_flask_login_user"):
        if hasattr(g, attr):
            try:
                delattr(g, attr)
            except (AttributeError, KeyError):
                pass
    return client


def _mode(value):
    SystemSetting.set_setting(MODE_KEY, value, setting_type="string")


def _grant_user(user, granted=True):
    set_user_grant(CREATE_GROUPS, user.id, granted)
    db.session.commit()


def _grant_group(group, granted=True):
    set_group_grant(CREATE_GROUPS, group.id, granted)
    db.session.commit()


@pytest.fixture
def ctx():
    """An app context that removes what the test made and resets the mode."""
    with app.app_context():
        before_users = {u.id for u in User.query.with_entities(User.id).all()}
        before_groups = {g.id for g in Group.query.with_entities(Group.id).all()}
        with patch.object(groups_module, "ENABLE_INTERNAL_SHARING", True):
            yield
        db.session.rollback()
        for group in Group.query.all():
            if group.id not in before_groups:
                db.session.delete(group)
        db.session.commit()
        for user in User.query.all():
            if user.id not in before_users:
                db.session.delete(user)
        db.session.commit()
        _mode("admins")


# --------------------------------------------------------------------------
# Resolver
# --------------------------------------------------------------------------

def test_default_mode_is_admins_only(ctx):
    SystemSetting.query.filter_by(key=MODE_KEY).delete()
    db.session.commit()
    assert permissions.permission_mode(CREATE_GROUPS) == "admins"
    plain = _user("plain")
    assert user_can(plain, CREATE_GROUPS) is False
    assert user_can(_user("site", is_admin=True), CREATE_GROUPS) is True


def test_site_admin_holds_the_permission_in_every_mode(ctx):
    admin = _user("site", is_admin=True)
    for mode in ("admins", "selected", "everyone"):
        _mode(mode)
        assert user_can(admin, CREATE_GROUPS) is True


def test_admins_mode_ignores_grants(ctx):
    _mode("admins")
    user = _user("granted")
    group = _group(members=[user])
    _grant_user(user)
    _grant_group(group)
    assert user_can(user, CREATE_GROUPS) is False


def test_selected_mode_individual_grant(ctx):
    _mode("selected")
    user = _user("granted")
    other = _user("other")
    assert user_can(user, CREATE_GROUPS) is False
    _grant_user(user)
    assert user_can(user, CREATE_GROUPS) is True
    assert user_can(other, CREATE_GROUPS) is False
    _grant_user(user, False)
    assert user_can(user, CREATE_GROUPS) is False


def test_selected_mode_group_grant_covers_every_member(ctx):
    _mode("selected")
    member = _user("member")
    group_admin = _user("gadmin")
    outsider = _user("outsider")
    group = _group(admins=[group_admin], members=[member])
    _grant_group(group)
    assert user_can(member, CREATE_GROUPS) is True
    assert user_can(group_admin, CREATE_GROUPS) is True
    assert user_can(outsider, CREATE_GROUPS) is False
    # Leaving the group ends the permission
    GroupMembership.query.filter_by(group_id=group.id, user_id=member.id).delete()
    db.session.commit()
    assert user_can(member, CREATE_GROUPS) is False
    _grant_group(group, False)
    assert user_can(group_admin, CREATE_GROUPS) is False


def test_everyone_mode(ctx):
    _mode("everyone")
    assert user_can(_user("anyone"), CREATE_GROUPS) is True


def test_unknown_mode_value_counts_as_admins(ctx):
    _mode("nonsense")
    user = _user("granted")
    _grant_user(user)
    assert permissions.permission_mode(CREATE_GROUPS) == "admins"
    assert user_can(user, CREATE_GROUPS) is False


def test_anonymous_and_unknown_permission(ctx):
    _mode("everyone")
    assert user_can(None, CREATE_GROUPS) is False
    from flask_login import AnonymousUserMixin
    assert user_can(AnonymousUserMixin(), CREATE_GROUPS) is False
    with pytest.raises(ValueError):
        user_can(_user("x"), "fly")


def test_grants_are_not_duplicated(ctx):
    user = _user("dup")
    _grant_user(user)
    _grant_user(user)
    assert PermissionGrant.query.filter_by(permission=CREATE_GROUPS, user_id=user.id).count() == 1


def test_grants_go_with_their_user_and_group(ctx):
    user = _user("gone")
    group = _group()
    _grant_user(user)
    _grant_group(group)
    db.session.delete(group)
    db.session.delete(user)
    db.session.commit()
    assert PermissionGrant.query.filter_by(user_id=user.id).count() == 0
    assert PermissionGrant.query.filter_by(group_id=group.id).count() == 0


# --------------------------------------------------------------------------
# Creating groups
# --------------------------------------------------------------------------

def test_unpermitted_user_cannot_create(ctx):
    user = _user("plain")
    resp = _client(user).post("/api/admin/groups", json={"name": f"n_{uuid.uuid4().hex[:6]}"})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "group_create_not_permitted"


def test_grant_without_selected_mode_does_not_allow_creation(ctx):
    _mode("admins")
    user = _user("granted")
    _grant_user(user)
    resp = _client(user).post("/api/admin/groups", json={"name": f"n_{uuid.uuid4().hex[:6]}"})
    assert resp.status_code == 403


def test_permitted_user_creates_and_becomes_group_admin(ctx):
    _mode("selected")
    user = _user("creator")
    _grant_user(user)
    name = f"made_{uuid.uuid4().hex[:6]}"
    resp = _client(user).post("/api/admin/groups", json={"name": name, "description": "d"})
    assert resp.status_code == 201, resp.data
    body = resp.get_json()
    group = db.session.get(Group, body["id"])
    assert group.created_by == user.id
    assert body["created_by"] == user.id
    assert body["can_delete"] is True
    assert "members_create_groups_granted" not in body
    membership = GroupMembership.query.filter_by(group_id=group.id, user_id=user.id).one()
    assert membership.role == "admin"


def test_member_of_granted_group_can_create(ctx):
    _mode("selected")
    user = _user("member")
    _grant_group(_group(members=[user]))
    resp = _client(user).post("/api/admin/groups", json={"name": f"n_{uuid.uuid4().hex[:6]}"})
    assert resp.status_code == 201


def test_everyone_mode_allows_any_user(ctx):
    _mode("everyone")
    resp = _client(_user("anyone")).post("/api/admin/groups", json={"name": f"n_{uuid.uuid4().hex[:6]}"})
    assert resp.status_code == 201


def test_switching_the_mode_back_stops_creation(ctx):
    user = _user("creator")
    _grant_user(user)
    _mode("selected")
    client = _client(user)
    assert client.post("/api/admin/groups", json={"name": f"a_{uuid.uuid4().hex[:6]}"}).status_code == 201
    _mode("admins")
    assert client.post("/api/admin/groups", json={"name": f"b_{uuid.uuid4().hex[:6]}"}).status_code == 403
    _mode("everyone")
    _grant_user(user, False)
    assert client.post("/api/admin/groups", json={"name": f"c_{uuid.uuid4().hex[:6]}"}).status_code == 201


def test_site_admin_create_records_creator_without_membership(ctx):
    admin = _user("site", is_admin=True)
    resp = _client(admin).post("/api/admin/groups", json={"name": f"n_{uuid.uuid4().hex[:6]}",
                                                         "members_create_groups_granted": True})
    assert resp.status_code == 201
    group = db.session.get(Group, resp.get_json()["id"])
    assert group.created_by == admin.id
    assert GroupMembership.query.filter_by(group_id=group.id).count() == 0
    assert permissions.group_has_grant(CREATE_GROUPS, group.id)


def test_creation_needs_internal_sharing(ctx):
    _mode("everyone")
    user = _user("anyone")
    with patch.object(groups_module, "ENABLE_INTERNAL_SHARING", False):
        resp = _client(user).post("/api/admin/groups", json={"name": f"n_{uuid.uuid4().hex[:6]}"})
        assert resp.status_code == 403
        assert groups_module.can_create_groups(user) is False


# --------------------------------------------------------------------------
# Deleting groups
# --------------------------------------------------------------------------

def test_creator_who_is_group_admin_can_delete(ctx):
    creator = _user("creator")
    group = _group(created_by=creator.id, admins=[creator])
    resp = _client(creator).delete(f"/api/admin/groups/{group.id}")
    assert resp.status_code == 200
    db.session.expire_all()
    assert db.session.get(Group, group.id) is None


def test_other_group_admin_cannot_delete(ctx):
    creator = _user("creator")
    other = _user("other")
    group = _group(created_by=creator.id, admins=[creator, other])
    resp = _client(other).delete(f"/api/admin/groups/{group.id}")
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "group_delete_not_permitted"
    assert db.session.get(Group, group.id) is not None


def test_demoted_creator_cannot_delete(ctx):
    creator = _user("creator")
    group = _group(created_by=creator.id, members=[creator])
    assert _client(creator).delete(f"/api/admin/groups/{group.id}").status_code == 403
    # Not a member at all
    GroupMembership.query.filter_by(group_id=group.id).delete()
    db.session.commit()
    assert _client(creator).delete(f"/api/admin/groups/{group.id}").status_code == 403


def test_site_admin_deletes_a_group_without_creator(ctx):
    admin = _user("site", is_admin=True)
    group = _group()
    assert _client(admin).delete(f"/api/admin/groups/{group.id}").status_code == 200


def test_plain_user_cannot_delete(ctx):
    group = _group()
    resp = _client(_user("plain")).delete(f"/api/admin/groups/{group.id}")
    assert resp.status_code == 403


def test_group_list_reports_who_may_delete(ctx):
    creator = _user("creator")
    other = _user("other")
    group = _group(created_by=creator.id, admins=[creator, other])
    rows = {g["id"]: g for g in _client(creator).get("/api/admin/groups").get_json()["groups"]}
    assert rows[group.id]["can_delete"] is True
    rows = {g["id"]: g for g in _client(other).get("/api/admin/groups").get_json()["groups"]}
    assert rows[group.id]["can_delete"] is False


def test_deleting_the_creator_keeps_the_group(ctx):
    admin = _user("site", is_admin=True)
    creator = _user("creator")
    group = _group(created_by=creator.id, admins=[creator])
    assert _client(admin).delete(f"/admin/users/{creator.id}").status_code == 200
    db.session.expire_all()
    kept = db.session.get(Group, group.id)
    assert kept is not None and kept.created_by is None


# --------------------------------------------------------------------------
# Adding members
# --------------------------------------------------------------------------

def test_group_admin_cannot_add_by_user_id(ctx):
    group_admin = _user("gadmin")
    target = _user("target")
    group = _group(admins=[group_admin])
    resp = _client(group_admin).post(f"/api/admin/groups/{group.id}/members",
                                     json={"user_id": target.id, "role": "member"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "member_identifier_required"
    assert GroupMembership.query.filter_by(group_id=group.id, user_id=target.id).first() is None


def test_group_admin_adds_by_exact_username_or_email(ctx):
    group_admin = _user("gadmin")
    by_name = _user("byname")
    by_mail = _user("bymail")
    group = _group(admins=[group_admin])
    client = _client(group_admin)
    resp = client.post(f"/api/admin/groups/{group.id}/members",
                       json={"identifier": by_name.username, "role": "member"})
    assert resp.status_code == 201, resp.data
    resp = client.post(f"/api/admin/groups/{group.id}/members",
                       json={"identifier": f"  {by_mail.email.upper()} ", "role": "admin"})
    assert resp.status_code == 201, resp.data
    roles = {m.user_id: m.role for m in GroupMembership.query.filter_by(group_id=group.id)}
    assert roles[by_name.id] == "member" and roles[by_mail.id] == "admin"


def test_group_admin_gets_no_partial_match(ctx):
    group_admin = _user("gadmin")
    target = _user("target")
    group = _group(admins=[group_admin])
    client = _client(group_admin)
    for partial in (target.username[:-2], target.email[:-3], "@local.test", "%", "target_%"):
        resp = client.post(f"/api/admin/groups/{group.id}/members",
                           json={"identifier": partial, "role": "member"})
        assert resp.status_code == 404, partial
        assert resp.get_json()["code"] == "user_not_found"
    assert GroupMembership.query.filter_by(group_id=group.id).count() == 1


def test_site_admin_still_adds_by_user_id(ctx):
    admin = _user("site", is_admin=True)
    target = _user("target")
    group = _group()
    resp = _client(admin).post(f"/api/admin/groups/{group.id}/members",
                               json={"user_id": target.id, "role": "member"})
    assert resp.status_code == 201


def test_group_admin_user_listing_stays_scoped(ctx):
    group_admin = _user("gadmin")
    member = _user("member")
    stranger = _user("stranger")
    _group(admins=[group_admin], members=[member])
    resp = _client(group_admin).get(f"/admin/users?search={stranger.username}")
    assert resp.status_code == 200
    assert resp.get_json()["users"] == []


# --------------------------------------------------------------------------
# Unique group names
# --------------------------------------------------------------------------

def test_duplicate_names_are_refused_ignoring_case(ctx):
    admin = _user("site", is_admin=True, ui_language="de")
    name = f"Sales_{uuid.uuid4().hex[:6]}"
    client = _client(admin)
    assert client.post("/api/admin/groups", json={"name": name}).status_code == 201
    for dup in (name, name.lower(), f"  {name.upper()}  "):
        resp = client.post("/api/admin/groups", json={"name": dup})
        assert resp.status_code == 400, dup
        body = resp.get_json()
        assert body["code"] == "group_name_exists"
        assert body["error"] == translate("server.apiError.group_name_exists", "de")
        assert body["error"] != translate("server.apiError.group_name_exists", "en")


def test_rename_to_a_taken_name_is_refused(ctx):
    creator = _user("creator")
    first = _group(name=f"Alpha_{uuid.uuid4().hex[:6]}")
    second = _group(admins=[creator])
    resp = _client(creator).put(f"/api/admin/groups/{second.id}", json={"name": first.name.upper()})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "group_name_exists"
    # Renaming a group to its own name in another case is allowed
    resp = _client(creator).put(f"/api/admin/groups/{second.id}", json={"name": second.name.upper()})
    assert resp.status_code == 200


def test_groups_that_already_differ_only_by_case_can_still_be_edited(ctx):
    """An install from before the case-insensitive rule may hold both."""
    admin = _user("site", is_admin=True)
    stem = uuid.uuid4().hex[:6]
    upper, lower = _group(name=f"Sales_{stem}"), _group(name=f"sales_{stem}")
    other = _group(name=f"Other_{stem}")
    client = _client(admin)
    for group in (upper, lower):
        # The edit window sends the unchanged name with the new description.
        resp = client.put(f"/api/admin/groups/{group.id}", json={"name": group.name, "description": "edited"})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["description"] == "edited"
        resp = client.put(f"/api/admin/groups/{group.id}", json={"name": f"  {group.name}  "})
        assert resp.status_code == 200, resp.get_json()
    assert db.session.get(Group, upper.id).name == f"Sales_{stem}"
    assert db.session.get(Group, lower.id).name == f"sales_{stem}"
    # Another case of its own name passes the check too (SALES_x is free).
    assert client.put(f"/api/admin/groups/{lower.id}", json={"name": f"SALES_{stem}"}).status_code == 200
    # A rename to a different, taken name is still refused, as is the exact
    # name of the twin (the unique index).
    for taken in (other.name.lower(), f"Sales_{stem}"):
        resp = client.put(f"/api/admin/groups/{lower.id}", json={"name": taken})
        assert resp.status_code == 400 and resp.get_json()["code"] == "group_name_exists", taken
    # Creating a third one stays refused.
    resp = client.post("/api/admin/groups", json={"name": f"sAlEs_{stem}"})
    assert resp.status_code == 400 and resp.get_json()["code"] == "group_name_exists"


# --------------------------------------------------------------------------
# Admin endpoints for grants and the mode
# --------------------------------------------------------------------------

def test_admin_sets_the_user_grant(ctx):
    admin = _user("site", is_admin=True)
    user = _user("target")
    client = _client(admin)
    resp = client.put(f"/admin/users/{user.id}", json={"create_groups_granted": True})
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["create_groups_granted"] is True
    assert permissions.user_has_grant(CREATE_GROUPS, user.id)
    rows = client.get(f"/admin/users?search={user.username}").get_json()["users"]
    assert rows[0]["create_groups_granted"] is True
    resp = client.put(f"/admin/users/{user.id}", json={"create_groups_granted": False})
    assert resp.get_json()["create_groups_granted"] is False


def test_only_site_admin_sets_the_group_grant(ctx):
    admin = _user("site", is_admin=True)
    group_admin = _user("gadmin")
    group = _group(admins=[group_admin])
    resp = _client(group_admin).put(f"/api/admin/groups/{group.id}",
                                    json={"name": group.name, "members_create_groups_granted": True})
    assert resp.status_code == 403
    assert not permissions.group_has_grant(CREATE_GROUPS, group.id)
    resp = _client(admin).put(f"/api/admin/groups/{group.id}",
                              json={"name": group.name, "members_create_groups_granted": True})
    assert resp.status_code == 200
    assert resp.get_json()["members_create_groups_granted"] is True
    assert permissions.group_has_grant(CREATE_GROUPS, group.id)


def test_mode_setting_accepts_only_known_modes(ctx):
    admin = _user("site", is_admin=True)
    client = _client(admin)
    resp = client.post("/admin/settings", json={"key": MODE_KEY, "value": "some", "setting_type": "string"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "invalid_permission_mode"
    resp = client.post("/admin/settings", json={"key": MODE_KEY, "value": "Selected", "setting_type": "string"})
    assert resp.status_code == 200
    assert permissions.permission_mode(CREATE_GROUPS) == "selected"
    assert _client(_user("plain")).post("/admin/settings", json={"key": MODE_KEY, "value": "everyone"}).status_code == 403


# --------------------------------------------------------------------------
# Pages and API v1
# --------------------------------------------------------------------------

def test_permitted_user_reaches_group_management(ctx):
    _mode("everyone")
    user = _user("anyone")
    client = _client(user)
    assert client.get("/group-management").status_code == 200
    resp = client.get("/api/admin/groups")
    assert resp.status_code == 200
    assert resp.get_json() == {"groups": [], "can_create_groups": True}
    assert client.get("/admin").headers["Location"].endswith("/group-management")


def test_unpermitted_user_is_kept_out(ctx):
    client = _client(_user("plain"))
    assert client.get("/group-management").status_code == 302
    assert client.get("/api/admin/groups").status_code == 403


def test_api_v1_reports_can_create_groups(ctx):
    user = _user("api")
    client = _client(user)
    assert client.get("/api/v1/users/me").get_json()["can_create_groups"] is False
    assert client.get("/api/v1/capabilities").get_json()["features"]["can_create_groups"] is False
    _mode("selected")
    _grant_user(user)
    assert client.get("/api/v1/users/me").get_json()["can_create_groups"] is True
    assert client.get("/api/v1/capabilities").get_json()["features"]["can_create_groups"] is True
