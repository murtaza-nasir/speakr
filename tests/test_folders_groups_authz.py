"""Authorization tests for folder + group surfaces.

Covers the previously-untested authz boundaries in:
  - src/api/folders.py  (personal folders, group folders, recording-folder
                          assignment, bulk folder move)
  - src/api/groups.py   (sync-shares, admin group CRUD + members)

Focus is on negatives / IDOR: a user must not be able to read or mutate
another user's folders, move recordings they don't own, move their own
recordings into folders they have no access to, or reach admin-only group
endpoints. The bulk folder-move IDOR (#287 pattern) is exercised directly.

Folders are gated by an admin SystemSetting ('enable_folders'); these tests
flip it on (and one test verifies the gate when it is off).

Run:
  HOME=/tmp /tmp/speakr_testvenv/bin/python -m pytest \
      tests/test_folders_groups_authz.py -p no:cacheprovider -q
"""
import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.api.folders as folders_module
import src.api.groups as groups_module
from src.app import app, db
from src.models import (
    User,
    Recording,
    Folder,
    Group,
    GroupMembership,
    SystemSetting,
)

app.config["WTF_CSRF_ENABLED"] = False


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _setup_user(prefix, is_admin=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
        is_admin=is_admin,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    # These tests wrap setup + multiple HTTP requests inside a single outer
    # app.app_context(). Flask-Login caches the resolved user on `g._login_user`
    # for the lifetime of that context, so a request made as user A leaves a
    # stale cached identity that shadows a later request made as user B. Clear
    # it so each request re-resolves current_user from its own session cookie.
    from flask import g
    for attr in ("_login_user", "_flask_login_user"):
        if hasattr(g, attr):
            try:
                delattr(g, attr)
            except (AttributeError, KeyError):
                pass


def _make_recording(user_id, title="r"):
    rec = Recording(
        user_id=user_id,
        title=title,
        audio_path="/tmp/x.mp3",
        status="COMPLETED",
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def _make_folder(user_id, name=None, group_id=None):
    folder = Folder(
        name=name or f"f_{uuid.uuid4().hex[:8]}",
        user_id=user_id,
        group_id=group_id,
    )
    db.session.add(folder)
    db.session.commit()
    return folder


def _make_group(name=None):
    group = Group(name=name or f"g_{uuid.uuid4().hex[:8]}")
    db.session.add(group)
    db.session.commit()
    return group


def _enable_folders():
    SystemSetting.set_setting("enable_folders", "true", setting_type="boolean")


def _disable_folders():
    SystemSetting.set_setting("enable_folders", "false", setting_type="boolean")


def _cleanup(*objs):
    for obj in objs:
        if obj is None:
            continue
        try:
            existing = db.session.get(type(obj), obj.id)
            if existing is not None:
                db.session.delete(existing)
        except Exception:
            pass
    db.session.commit()


# ==========================================================================
# Personal folders: cross-user IDOR
# ==========================================================================

def test_list_folders_returns_only_own_personal_folders():
    with app.app_context():
        _enable_folders()
        alice = _setup_user("fld_a")
        bob = _setup_user("fld_b")
        alice_folder = _make_folder(alice.id, "alice-private")
        bob_folder = _make_folder(bob.id, "bob-private")

        client = app.test_client()
        _login(client, alice)
        resp = client.get("/api/folders")
        assert resp.status_code == 200
        names = {f["name"] for f in resp.get_json()}
        assert "alice-private" in names
        assert "bob-private" not in names, (
            "Folder listing leaked another user's personal folder"
        )

        _cleanup(alice_folder, bob_folder, alice, bob)


def test_user_cannot_update_other_users_folder():
    with app.app_context():
        _enable_folders()
        alice = _setup_user("fld_upd_a")
        bob = _setup_user("fld_upd_b")
        bob_folder = _make_folder(bob.id, "bob-secret")

        client = app.test_client()
        _login(client, alice)
        resp = client.put(
            f"/api/folders/{bob_folder.id}",
            json={"name": "hijacked"},
        )
        assert resp.status_code == 403, (
            f"Expected 403 editing another user's folder, got {resp.status_code}"
        )
        assert "permission" in resp.get_json()["error"].lower()
        # Ensure the folder was not actually renamed.
        db.session.expire_all()
        assert db.session.get(Folder, bob_folder.id).name == "bob-secret"

        _cleanup(bob_folder, alice, bob)


def test_user_cannot_delete_other_users_folder():
    with app.app_context():
        _enable_folders()
        alice = _setup_user("fld_del_a")
        bob = _setup_user("fld_del_b")
        bob_folder = _make_folder(bob.id, "bob-keep")

        client = app.test_client()
        _login(client, alice)
        resp = client.delete(f"/api/folders/{bob_folder.id}")
        assert resp.status_code == 403, (
            f"Expected 403 deleting another user's folder, got {resp.status_code}"
        )
        assert "permission" in resp.get_json()["error"].lower()
        assert db.session.get(Folder, bob_folder.id) is not None, (
            "Another user's folder was deleted (IDOR)"
        )

        _cleanup(bob_folder, alice, bob)


def test_update_nonexistent_folder_returns_404():
    with app.app_context():
        _enable_folders()
        alice = _setup_user("fld_404")
        client = app.test_client()
        _login(client, alice)
        resp = client.put("/api/folders/999999999", json={"name": "x"})
        assert resp.status_code == 404
        _cleanup(alice)


def test_create_personal_folder_succeeds_for_self():
    with app.app_context():
        _enable_folders()
        alice = _setup_user("fld_create")
        client = app.test_client()
        _login(client, alice)
        resp = client.post("/api/folders", json={"name": "my-folder"})
        assert resp.status_code == 201
        created = resp.get_json()
        assert created["name"] == "my-folder"
        folder = db.session.get(Folder, created["id"])
        assert folder is not None and folder.user_id == alice.id

        _cleanup(folder, alice)


# ==========================================================================
# Folders feature gate
# ==========================================================================

def test_folder_endpoints_refuse_when_feature_disabled():
    with app.app_context():
        _disable_folders()
        alice = _setup_user("fld_gate")
        folder = _make_folder(alice.id, "gated")
        client = app.test_client()
        _login(client, alice)

        # POST create is rejected with 403 (specifically the feature gate, not
        # an incidental CSRF/auth 403).
        resp = client.post("/api/folders", json={"name": "nope"})
        assert resp.status_code == 403
        assert "not enabled" in resp.get_json()["error"]

        # PUT update is rejected with 403.
        resp = client.put(f"/api/folders/{folder.id}", json={"name": "x"})
        assert resp.status_code == 403
        assert "not enabled" in resp.get_json()["error"]

        # DELETE is rejected with 403.
        resp = client.delete(f"/api/folders/{folder.id}")
        assert resp.status_code == 403
        assert "not enabled" in resp.get_json()["error"]

        # GET list returns an empty array (feature-off sentinel, not an error).
        resp = client.get("/api/folders")
        assert resp.status_code == 200
        assert resp.get_json() == []

        _cleanup(folder, alice)
        _enable_folders()


# ==========================================================================
# Move recording to folder: ownership + folder-access checks
# ==========================================================================

def test_cannot_move_recording_you_dont_own():
    with app.app_context():
        _enable_folders()
        alice = _setup_user("mv_own_a")
        bob = _setup_user("mv_own_b")
        bob_rec = _make_recording(bob.id, "bob rec")
        alice_folder = _make_folder(alice.id, "alice-target")

        client = app.test_client()
        _login(client, alice)
        resp = client.put(
            f"/api/recordings/{bob_rec.id}/folder",
            json={"folder_id": alice_folder.id},
        )
        assert resp.status_code == 403, (
            f"Expected 403 moving another user's recording, got {resp.status_code}"
        )
        db.session.expire_all()
        assert db.session.get(Recording, bob_rec.id).folder_id is None

        _cleanup(bob_rec, alice_folder, alice, bob)


def test_cannot_move_own_recording_into_other_users_folder():
    with app.app_context():
        _enable_folders()
        alice = _setup_user("mv_fld_a")
        bob = _setup_user("mv_fld_b")
        alice_rec = _make_recording(alice.id, "alice rec")
        bob_folder = _make_folder(bob.id, "bob-folder")

        client = app.test_client()
        _login(client, alice)
        resp = client.put(
            f"/api/recordings/{alice_rec.id}/folder",
            json={"folder_id": bob_folder.id},
        )
        assert resp.status_code == 403, (
            f"Expected 403 targeting another user's folder, got {resp.status_code}"
        )
        db.session.expire_all()
        assert db.session.get(Recording, alice_rec.id).folder_id is None, (
            "Recording was moved into a folder the user has no access to (IDOR)"
        )

        _cleanup(alice_rec, bob_folder, alice, bob)


def test_move_own_recording_into_own_folder_succeeds():
    with app.app_context():
        _enable_folders()
        alice = _setup_user("mv_ok")
        rec = _make_recording(alice.id, "rec")
        folder = _make_folder(alice.id, "target")

        client = app.test_client()
        _login(client, alice)
        resp = client.put(
            f"/api/recordings/{rec.id}/folder",
            json={"folder_id": folder.id},
        )
        assert resp.status_code == 200
        db.session.expire_all()
        assert db.session.get(Recording, rec.id).folder_id == folder.id

        _cleanup(rec, folder, alice)


# ==========================================================================
# Bulk folder move: the #287 IDOR surface
# ==========================================================================

def test_bulk_folder_move_only_affects_own_recordings():
    """Bulk move must silently skip recordings the caller can't edit, while
    still moving the caller's own. A victim's recording id slipped into the
    list must not be moved into the attacker's folder."""
    with app.app_context():
        _enable_folders()
        attacker = _setup_user("bulk_att")
        victim = _setup_user("bulk_vic")
        attacker_rec = _make_recording(attacker.id, "attacker rec")
        victim_rec = _make_recording(victim.id, "victim rec")
        attacker_folder = _make_folder(attacker.id, "attacker-folder")

        client = app.test_client()
        _login(client, attacker)
        resp = client.post(
            "/api/recordings/bulk/folder",
            json={
                "recording_ids": [attacker_rec.id, victim_rec.id],
                "folder_id": attacker_folder.id,
            },
        )
        assert resp.status_code == 200
        body = resp.get_json()
        # Only the attacker's own recording should have been updated.
        assert body["updated_count"] == 1, (
            f"Expected 1 updated, got {body['updated_count']} — victim record "
            "may have been moved (IDOR)"
        )
        db.session.expire_all()
        assert db.session.get(Recording, attacker_rec.id).folder_id == attacker_folder.id
        assert db.session.get(Recording, victim_rec.id).folder_id is None, (
            "Victim's recording was moved into the attacker's folder (IDOR)"
        )

        _cleanup(attacker_rec, victim_rec, attacker_folder, attacker, victim)


def test_bulk_folder_move_rejects_other_users_target_folder():
    """If the target folder belongs to another user, the whole bulk op is
    rejected with 403 before any recording is touched."""
    with app.app_context():
        _enable_folders()
        attacker = _setup_user("bulk_tgt_att")
        victim = _setup_user("bulk_tgt_vic")
        attacker_rec = _make_recording(attacker.id, "attacker rec")
        victim_folder = _make_folder(victim.id, "victim-folder")

        client = app.test_client()
        _login(client, attacker)
        resp = client.post(
            "/api/recordings/bulk/folder",
            json={
                "recording_ids": [attacker_rec.id],
                "folder_id": victim_folder.id,
            },
        )
        assert resp.status_code == 403, (
            f"Expected 403 targeting another user's folder, got {resp.status_code}"
        )
        db.session.expire_all()
        assert db.session.get(Recording, attacker_rec.id).folder_id is None

        _cleanup(attacker_rec, victim_folder, attacker, victim)


# ==========================================================================
# Group folders: membership / admin gating
# ==========================================================================

def test_non_admin_member_cannot_create_group_folder_via_api_folders():
    """POST /api/folders with a group_id requires the caller to be an *admin*
    of that group. A plain member is rejected with 403."""
    with app.app_context():
        _enable_folders()
        member = _setup_user("gf_member")
        group = _make_group("gf-group")
        db.session.add(GroupMembership(user_id=member.id, group_id=group.id, role="member"))
        db.session.commit()

        client = app.test_client()
        _login(client, member)
        resp = client.post(
            "/api/folders",
            json={"name": "team-folder", "group_id": group.id},
        )
        assert resp.status_code == 403, (
            f"Expected 403 for non-admin creating group folder, got {resp.status_code}"
        )

        _cleanup(
            GroupMembership.query.filter_by(group_id=group.id).first(),
            group, member,
        )


def test_outsider_cannot_create_group_folder():
    """A user who is not a member at all cannot create a group folder."""
    with app.app_context():
        _enable_folders()
        outsider = _setup_user("gf_outsider")
        group = _make_group("gf-group2")

        client = app.test_client()
        _login(client, outsider)
        resp = client.post(
            "/api/folders",
            json={"name": "team-folder", "group_id": group.id},
        )
        assert resp.status_code == 403

        _cleanup(group, outsider)


def test_group_admin_can_create_group_folder():
    with app.app_context():
        _enable_folders()
        admin = _setup_user("gf_admin")
        group = _make_group("gf-group3")
        db.session.add(GroupMembership(user_id=admin.id, group_id=group.id, role="admin"))
        db.session.commit()

        client = app.test_client()
        _login(client, admin)
        resp = client.post(
            "/api/folders",
            json={"name": "team-folder", "group_id": group.id},
        )
        assert resp.status_code == 201
        created = resp.get_json()
        folder = db.session.get(Folder, created["id"])
        assert folder is not None and folder.group_id == group.id

        _cleanup(
            folder,
            GroupMembership.query.filter_by(group_id=group.id).first(),
            group, admin,
        )


def test_get_group_folders_requires_membership():
    """GET /api/groups/<gid>/folders returns 403 for a non-member, 200 for a
    member."""
    with app.app_context():
        _enable_folders()
        member = _setup_user("ggf_member")
        outsider = _setup_user("ggf_outsider")
        group = _make_group("ggf-group")
        db.session.add(GroupMembership(user_id=member.id, group_id=group.id, role="member"))
        db.session.commit()
        group_folder = _make_folder(member.id, "team-shared", group_id=group.id)

        # Outsider: 403. (Use a dedicated client — reusing one client across
        # two logged-in identities does not reliably switch current_user.)
        outsider_client = app.test_client()
        _login(outsider_client, outsider)
        resp = outsider_client.get(f"/api/groups/{group.id}/folders")
        assert resp.status_code == 403, (
            f"Non-member reached group folders, got {resp.status_code}"
        )

        # Member: 200 and sees the folder.
        member_client = app.test_client()
        _login(member_client, member)
        resp = member_client.get(f"/api/groups/{group.id}/folders")
        assert resp.status_code == 200
        names = {f["name"] for f in resp.get_json()["folders"]}
        assert "team-shared" in names

        _cleanup(
            group_folder,
            GroupMembership.query.filter_by(group_id=group.id).first(),
            group, member, outsider,
        )


def test_create_group_folder_route_requires_admin_membership():
    """POST /api/groups/<gid>/folders (the dedicated route, gated by
    ENABLE_INTERNAL_SHARING) rejects non-admins with 403."""
    with app.app_context():
        _enable_folders()
        member = _setup_user("cgf_member")
        group = _make_group("cgf-group")
        db.session.add(GroupMembership(user_id=member.id, group_id=group.id, role="member"))
        db.session.commit()

        client = app.test_client()
        _login(client, member)
        with patch.object(folders_module, "ENABLE_INTERNAL_SHARING", True):
            resp = client.post(
                f"/api/groups/{group.id}/folders",
                json={"name": "team-folder"},
            )
        assert resp.status_code == 403

        _cleanup(
            GroupMembership.query.filter_by(group_id=group.id).first(),
            group, member,
        )


# ==========================================================================
# groups.py: sync-shares authz
# ==========================================================================

def test_sync_shares_requires_group_admin():
    """POST /api/groups/<gid>/sync-shares: a plain member gets 403."""
    with app.app_context():
        member = _setup_user("ss_member")
        group = _make_group("ss-group")
        db.session.add(GroupMembership(user_id=member.id, group_id=group.id, role="member"))
        db.session.commit()

        client = app.test_client()
        _login(client, member)
        with patch.object(groups_module, "ENABLE_INTERNAL_SHARING", True):
            resp = client.post(f"/api/groups/{group.id}/sync-shares")
        assert resp.status_code == 403, (
            f"Non-admin reached sync-shares, got {resp.status_code}"
        )

        _cleanup(
            GroupMembership.query.filter_by(group_id=group.id).first(),
            group, member,
        )


def test_sync_shares_outsider_gets_403():
    with app.app_context():
        outsider = _setup_user("ss_outsider")
        group = _make_group("ss-group2")

        client = app.test_client()
        _login(client, outsider)
        with patch.object(groups_module, "ENABLE_INTERNAL_SHARING", True):
            resp = client.post(f"/api/groups/{group.id}/sync-shares")
        assert resp.status_code == 403

        _cleanup(group, outsider)


# ==========================================================================
# Admin group endpoints: site-admin gating
# ==========================================================================

def test_create_group_requires_site_admin():
    """POST /api/admin/groups: a normal authenticated user gets 403."""
    with app.app_context():
        normal = _setup_user("ag_normal")
        client = app.test_client()
        _login(client, normal)
        with patch.object(groups_module, "ENABLE_INTERNAL_SHARING", True):
            resp = client.post("/api/admin/groups", json={"name": "x"})
        assert resp.status_code == 403, (
            f"Non-admin reached admin group creation, got {resp.status_code}"
        )
        _cleanup(normal)


def test_create_group_succeeds_for_site_admin():
    with app.app_context():
        admin = _setup_user("ag_admin", is_admin=True)
        client = app.test_client()
        _login(client, admin)
        name = f"admin-group-{uuid.uuid4().hex[:6]}"
        with patch.object(groups_module, "ENABLE_INTERNAL_SHARING", True):
            resp = client.post("/api/admin/groups", json={"name": name})
        assert resp.status_code == 201
        group = Group.query.filter_by(name=name).first()
        assert group is not None

        _cleanup(group, admin)


def test_delete_group_requires_site_admin():
    """DELETE /api/admin/groups/<gid> is site-admin only. Even a *group*
    admin (but not site admin) must be rejected with 403."""
    with app.app_context():
        group_admin = _setup_user("dg_groupadmin")
        group = _make_group("dg-group")
        db.session.add(GroupMembership(user_id=group_admin.id, group_id=group.id, role="admin"))
        db.session.commit()

        client = app.test_client()
        _login(client, group_admin)
        resp = client.delete(f"/api/admin/groups/{group.id}")
        assert resp.status_code == 403, (
            f"Group admin (non-site-admin) deleted a group, got {resp.status_code}"
        )
        assert db.session.get(Group, group.id) is not None

        _cleanup(
            GroupMembership.query.filter_by(group_id=group.id).first(),
            group, group_admin,
        )


def test_add_member_requires_admin():
    """POST /api/admin/groups/<gid>/members: a normal non-member user gets
    403 (not site admin, not group admin)."""
    with app.app_context():
        normal = _setup_user("am_normal")
        target = _setup_user("am_target")
        group = _make_group("am-group")

        client = app.test_client()
        _login(client, normal)
        with patch.object(groups_module, "ENABLE_INTERNAL_SHARING", True):
            resp = client.post(
                f"/api/admin/groups/{group.id}/members",
                json={"user_id": target.id, "role": "member"},
            )
        assert resp.status_code == 403
        assert GroupMembership.query.filter_by(group_id=group.id).first() is None

        _cleanup(group, normal, target)


def test_group_admin_can_add_member():
    """A group admin (not site admin) CAN add members to their own group."""
    with app.app_context():
        group_admin = _setup_user("gam_admin")
        target = _setup_user("gam_target")
        group = _make_group("gam-group")
        db.session.add(GroupMembership(user_id=group_admin.id, group_id=group.id, role="admin"))
        db.session.commit()

        client = app.test_client()
        _login(client, group_admin)
        with patch.object(groups_module, "ENABLE_INTERNAL_SHARING", True):
            resp = client.post(
                f"/api/admin/groups/{group.id}/members",
                json={"user_id": target.id, "role": "member"},
            )
        assert resp.status_code == 201
        assert GroupMembership.query.filter_by(
            group_id=group.id, user_id=target.id
        ).first() is not None

        _cleanup(
            GroupMembership.query.filter_by(group_id=group.id, user_id=target.id).first(),
            GroupMembership.query.filter_by(group_id=group.id, user_id=group_admin.id).first(),
            group, group_admin, target,
        )


def test_get_teams_requires_admin_or_group_admin():
    """GET /api/admin/groups: a plain user with no admin role gets 403."""
    with app.app_context():
        normal = _setup_user("gt_normal")
        client = app.test_client()
        _login(client, normal)
        resp = client.get("/api/admin/groups")
        assert resp.status_code == 403
        _cleanup(normal)
