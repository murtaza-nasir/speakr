"""Authorization / IDOR regression tests for src/api/shares.py.

This is the highest-risk private-data-leak surface in the app (public links +
internal user-to-user sharing) and previously had ZERO test coverage. These
tests assert the EXACT observed behaviour of the routes (status codes + body),
focused on the negative / cross-user (IDOR) boundaries.

Two harness details matter a great deal here:

1. Flask-Login caches the resolved ``current_user`` on the application context.
   If client requests are issued INSIDE an enclosing ``with app.app_context():``
   block, the first request's user is cached and every later request in that
   same context resolves to the SAME user regardless of which session cookie the
   test client sends. That silently makes a "recipient" client act as the
   "owner". To avoid it, all DB work happens inside a short-lived ``_db()``
   context manager (which exits before any HTTP call) and every ``client.*``
   request is issued with NO app context active, so each request pushes its own.

2. ``src/api/shares.py`` and ``src/app.py`` each read ENABLE_PUBLIC_SHARING /
   ENABLE_INTERNAL_SHARING into module-level booleans AT IMPORT TIME (from the
   project .env via load_dotenv). The exact ambient value differs by machine/CI,
   so the tests force the flags to a known value via context managers that patch
   the module globals in BOTH modules (has_recording_access in src.app gates on
   its own copy of ENABLE_INTERNAL_SHARING).

``create_share`` requires request.is_secure, so the secure-path tests issue the
request with wsgi.url_scheme=https via environ_overrides.

Run:
    HOME=/tmp /tmp/speakr_testvenv/bin/python -m pytest \
        tests/test_shares_authz.py -p no:cacheprovider -q
"""

import os
import sys
import tempfile
import uuid
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# When run standalone (python tests/test_shares_authz.py), conftest.py is NOT
# loaded, so point the app at a throwaway DB / upload dir before importing it.
# Under pytest, conftest.py has already set these and setdefault is a no-op.
if "SQLALCHEMY_DATABASE_URI" not in os.environ:
    _STANDALONE_DIR = tempfile.mkdtemp(prefix="speakr_shares_authz_")
    os.environ["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{os.path.join(_STANDALONE_DIR, 'test.db')}"
    )
    os.environ.setdefault("UPLOAD_FOLDER", os.path.join(_STANDALONE_DIR, "uploads"))
    os.environ.setdefault("SECRET_KEY", "pytest-secret-key")
    os.environ.setdefault("ENABLE_AUTO_PROCESSING", "false")
    os.environ.setdefault("TEXT_MODEL_API_KEY", "test-key")

import src.app as app_module
import src.api.shares as shares_module
from src.app import app, db
from src.models import User, Recording, Share, InternalShare, SharedRecordingState

app.config["WTF_CSRF_ENABLED"] = False

HTTPS = {"wsgi.url_scheme": "https"}


# --- DB / context helpers ---------------------------------------------------

@contextmanager
def _db():
    """Short-lived app context for DB work only.

    Must be exited before issuing client requests so Flask-Login does not cache
    a stale current_user across differently-authenticated requests.
    """
    with app.app_context():
        yield


def _setup_user(prefix, can_share_publicly=True):
    """Create a user and return its id (a plain int, safe to use after the
    context closes)."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
        can_share_publicly=can_share_publicly,
    )
    db.session.add(user)
    db.session.commit()
    return user.id


def _make_recording(user_id, title="r"):
    rec = Recording(
        user_id=user_id,
        title=title,
        audio_path="/tmp/x.mp3",
        status="COMPLETED",
    )
    db.session.add(rec)
    db.session.commit()
    return rec.id


def _make_public_share(recording_id, user_id):
    share = Share(recording_id=recording_id, user_id=user_id)
    db.session.add(share)
    db.session.commit()
    return share.public_id, share.id


def _make_internal_share(recording_id, owner_id, recipient_id,
                         can_edit=False, can_reshare=False):
    share = InternalShare(
        recording_id=recording_id,
        owner_id=owner_id,
        shared_with_user_id=recipient_id,
        can_edit=can_edit,
        can_reshare=can_reshare,
    )
    db.session.add(share)
    db.session.commit()
    return share.id


def _client_for(user_id):
    """Build a fresh test client with the session pre-authenticated as user_id.

    Returned with NO app context active; the caller must issue requests while no
    app context is on the stack (see module docstring).
    """
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


def _anon_client():
    return app.test_client()


# --- config-flag forcing ----------------------------------------------------

@contextmanager
def _flag(module_attr, value, *modules):
    """Temporarily set a module-level config bool on each given module."""
    saved = [(m, getattr(m, module_attr)) for m in modules]
    for m in modules:
        setattr(m, module_attr, value)
    try:
        yield
    finally:
        for m, old in saved:
            setattr(m, module_attr, old)


def _internal_enabled():
    return _flag("ENABLE_INTERNAL_SHARING", True, app_module, shares_module)


def _internal_disabled():
    return _flag("ENABLE_INTERNAL_SHARING", False, app_module, shares_module)


def _public_enabled():
    return _flag("ENABLE_PUBLIC_SHARING", True, shares_module)


def _public_disabled():
    return _flag("ENABLE_PUBLIC_SHARING", False, shares_module)


# --- cleanup ----------------------------------------------------------------

def _cleanup_ids(user_ids=(), recording_ids=()):
    with _db():
        for rid in recording_ids:
            Share.query.filter_by(recording_id=rid).delete()
            InternalShare.query.filter_by(recording_id=rid).delete()
            SharedRecordingState.query.filter_by(recording_id=rid).delete()
            rec = db.session.get(Recording, rid)
            if rec is not None:
                db.session.delete(rec)
        for uid in user_ids:
            u = db.session.get(User, uid)
            if u is not None:
                db.session.delete(u)
        db.session.commit()


# =============================================================================
# Public share link: anonymous view
# =============================================================================

def test_public_view_valid_public_id_returns_recording():
    """A valid public_id renders the shared recording to an anonymous viewer."""
    with _db():
        owner = _setup_user("pv_owner")
        rec = _make_recording(owner, "secret meeting")
        public_id, _ = _make_public_share(rec, owner)

    resp = _anon_client().get(f"/share/{public_id}")
    assert resp.status_code == 200
    assert b"secret meeting" in resp.data

    _cleanup_ids([owner], [rec])


def test_public_view_nonexistent_public_id_returns_404():
    """An unknown public_id must 404 — never leak that recordings exist."""
    resp = _anon_client().get("/share/this-id-does-not-exist-xyz")
    assert resp.status_code == 404


def test_public_view_revoked_public_id_returns_404():
    """After the share row is deleted (revoked) the link 404s and leaks nothing."""
    with _db():
        owner = _setup_user("pv_revoked")
        rec = _make_recording(owner, "revoked content")
        public_id, share_id = _make_public_share(rec, owner)
        db.session.delete(db.session.get(Share, share_id))
        db.session.commit()

    resp = _anon_client().get(f"/share/{public_id}")
    assert resp.status_code == 404
    assert b"revoked content" not in resp.data

    _cleanup_ids([owner], [rec])


# =============================================================================
# Public shared-audio endpoint
# =============================================================================

def test_shared_audio_nonexistent_public_id_returns_404():
    """An unknown public_id on the audio endpoint must be a 404, not a 500.

    get_shared_audio() now re-raises HTTPException so first_or_404()'s 404
    propagates instead of being masked as a generic 500 by the broad
    ``except Exception``. No audio is served either way; this asserts the
    correct status (matches the sibling HTML view /share/<public_id>).
    """
    resp = _anon_client().get("/share/audio/nope-not-a-real-id")
    assert resp.status_code == 404


def test_shared_audio_valid_share_missing_file_returns_404():
    """A valid share whose audio file is absent on disk returns 404 (not 200).

    The recording.audio_path ('/tmp/x.mp3') does not exist, so the route hits the
    os.path.exists branch -> 'Audio file missing from server'. Proves the
    endpoint resolves the share but never serves a phantom/other file.
    """
    with _db():
        owner = _setup_user("audio_owner")
        rec = _make_recording(owner, "with audio")
        public_id, _ = _make_public_share(rec, owner)

    resp = _anon_client().get(f"/share/audio/{public_id}")
    assert resp.status_code == 404
    assert b"missing from server" in resp.data

    _cleanup_ids([owner], [rec])


# =============================================================================
# Create public share link: ownership boundary
# =============================================================================

def test_create_share_by_owner_succeeds():
    """The owner can create a public share link for their own recording over HTTPS."""
    with _db():
        owner = _setup_user("cs_owner")
        rec = _make_recording(owner, "mine to share")

    with _public_enabled():
        resp = _client_for(owner).post(
            f"/api/recording/{rec}/share",
            json={"share_summary": True, "share_notes": True},
            environ_overrides=HTTPS,
        )
    assert resp.status_code == 201, resp.data
    body = resp.get_json()
    assert body["success"] is True
    assert "share_url" in body

    with _db():
        assert Share.query.filter_by(recording_id=rec).count() == 1
    _cleanup_ids([owner], [rec])


def test_create_share_for_other_users_recording_is_denied():
    """IDOR: a non-owner cannot create a public link for someone else's recording.

    The route returns 404 ('Recording not found or you do not have permission')
    and NO Share row is created for the victim's recording.
    """
    with _db():
        victim = _setup_user("cs_victim")
        attacker = _setup_user("cs_attacker")
        victim_rec = _make_recording(victim, "victims private rec")

    with _public_enabled():
        resp = _client_for(attacker).post(
            f"/api/recording/{victim_rec}/share", json={}, environ_overrides=HTTPS,
        )
    assert resp.status_code == 404, resp.data
    with _db():
        assert Share.query.filter_by(recording_id=victim_rec).count() == 0, (
            "IDOR: attacker created a public share link for the victim's recording"
        )
    _cleanup_ids([victim, attacker], [victim_rec])


def test_create_share_refused_when_public_sharing_disabled():
    """With ENABLE_PUBLIC_SHARING off, even the owner is refused (403)."""
    with _db():
        owner = _setup_user("cs_disabled")
        rec = _make_recording(owner)

    with _public_disabled():
        resp = _client_for(owner).post(
            f"/api/recording/{rec}/share", json={}, environ_overrides=HTTPS,
        )
    assert resp.status_code == 403, resp.data
    with _db():
        assert Share.query.filter_by(recording_id=rec).count() == 0
    _cleanup_ids([owner], [rec])


def test_create_share_refused_when_user_lacks_permission():
    """A user with can_share_publicly=False is refused (403)."""
    with _db():
        owner = _setup_user("cs_noperm", can_share_publicly=False)
        rec = _make_recording(owner)

    with _public_enabled():
        resp = _client_for(owner).post(
            f"/api/recording/{rec}/share", json={}, environ_overrides=HTTPS,
        )
    assert resp.status_code == 403, resp.data
    with _db():
        assert Share.query.filter_by(recording_id=rec).count() == 0
    _cleanup_ids([owner], [rec])


def test_create_share_refused_over_insecure_connection():
    """Creating a share over plain HTTP (not is_secure) is refused (403)."""
    with _db():
        owner = _setup_user("cs_http")
        rec = _make_recording(owner)

    with _public_enabled():
        resp = _client_for(owner).post(f"/api/recording/{rec}/share", json={})
    assert resp.status_code == 403, resp.data
    with _db():
        assert Share.query.filter_by(recording_id=rec).count() == 0
    _cleanup_ids([owner], [rec])


def test_create_share_requires_login():
    """Anonymous create-share bounces to login (302/401)."""
    with _db():
        owner = _setup_user("cs_anon")
        rec = _make_recording(owner)

    resp = _anon_client().post(
        f"/api/recording/{rec}/share", json={}, environ_overrides=HTTPS,
    )
    assert resp.status_code in (302, 401), resp.status_code
    _cleanup_ids([owner], [rec])


# =============================================================================
# get_existing_share ownership boundary
# =============================================================================

def test_get_existing_share_other_users_recording_returns_404():
    """IDOR: GET share-status for another user's recording must 404, even if a
    share exists, so existence/metadata never leaks."""
    with _db():
        victim = _setup_user("ges_victim")
        attacker = _setup_user("ges_attacker")
        rec = _make_recording(victim)
        _make_public_share(rec, victim)

    resp = _client_for(attacker).get(f"/api/recording/{rec}/share")
    assert resp.status_code == 404, resp.data
    _cleanup_ids([victim, attacker], [rec])


# =============================================================================
# Manage public shares: /api/shares, PUT/DELETE /api/share/<id>
# =============================================================================

def test_get_shares_returns_only_own_shares():
    """GET /api/shares returns ONLY the caller's shares, not other users'."""
    with _db():
        a = _setup_user("ls_a")
        b = _setup_user("ls_b")
        rec_a = _make_recording(a)
        rec_b = _make_recording(b)
        _, share_a_id = _make_public_share(rec_a, a)
        _, share_b_id = _make_public_share(rec_b, b)

    resp = _client_for(a).get("/api/shares")
    assert resp.status_code == 200
    ids = {s["id"] for s in resp.get_json()}
    assert share_a_id in ids
    assert share_b_id not in ids, "GET /api/shares leaked another user's share"
    _cleanup_ids([a, b], [rec_a, rec_b])


def test_update_other_users_share_returns_404():
    """IDOR: PUT on a share you don't own must 404 and not mutate it."""
    with _db():
        victim = _setup_user("us_victim")
        attacker = _setup_user("us_attacker")
        rec = _make_recording(victim)
        _, share_id = _make_public_share(rec, victim)

    resp = _client_for(attacker).put(
        f"/api/share/{share_id}", json={"share_summary": False},
    )
    assert resp.status_code == 404, resp.data
    with _db():
        share = db.session.get(Share, share_id)
        assert share.share_summary is True, "IDOR: attacker mutated victim's share"
    _cleanup_ids([victim, attacker], [rec])


def test_delete_other_users_share_returns_404_and_survives():
    """IDOR: DELETE on a share you don't own must 404 and the share survives."""
    with _db():
        victim = _setup_user("ds_victim")
        attacker = _setup_user("ds_attacker")
        rec = _make_recording(victim)
        _, share_id = _make_public_share(rec, victim)

    resp = _client_for(attacker).delete(f"/api/share/{share_id}")
    assert resp.status_code == 404, resp.data
    with _db():
        assert db.session.get(Share, share_id) is not None, (
            "IDOR: attacker deleted victim's public share"
        )
    _cleanup_ids([victim, attacker], [rec])


# =============================================================================
# users/search + can-share-publicly auth gating
# =============================================================================

def test_users_search_requires_login():
    resp = _anon_client().get("/api/users/search?q=alice")
    assert resp.status_code in (302, 401), resp.status_code


def test_users_search_403_when_internal_sharing_disabled():
    """With internal sharing off, user search is refused even for a logged-in
    user — prevents user enumeration when the feature is off."""
    with _db():
        user = _setup_user("srch_off")
    with _internal_disabled():
        resp = _client_for(user).get("/api/users/search?q=ab")
    assert resp.status_code == 403, resp.data
    _cleanup_ids([user], [])


def test_can_share_publicly_requires_login():
    resp = _anon_client().get("/api/permissions/can-share-publicly")
    assert resp.status_code in (302, 401), resp.status_code


def test_can_share_publicly_reflects_user_permission():
    """The endpoint reports the AND of user.can_share_publicly and the global
    ENABLE_PUBLIC_SHARING flag (forced True here)."""
    with _db():
        allowed = _setup_user("csp_yes", can_share_publicly=True)
        denied = _setup_user("csp_no", can_share_publicly=False)

    with _public_enabled():
        r1 = _client_for(allowed).get("/api/permissions/can-share-publicly")
        r2 = _client_for(denied).get("/api/permissions/can-share-publicly")
    assert r1.status_code == 200
    assert r1.get_json()["can_share_publicly"] is True
    assert r2.status_code == 200
    assert r2.get_json()["can_share_publicly"] is False
    _cleanup_ids([allowed, denied], [])


# =============================================================================
# Internal sharing: feature-flag gating (forced off)
# =============================================================================

def test_internal_share_403_when_feature_disabled():
    with _db():
        owner = _setup_user("is_off_owner")
        target = _setup_user("is_off_target")
        rec = _make_recording(owner)

    with _internal_disabled():
        resp = _client_for(owner).post(
            f"/api/recordings/{rec}/share-internal", json={"user_id": target},
        )
    assert resp.status_code == 403, resp.data
    with _db():
        assert InternalShare.query.filter_by(recording_id=rec).count() == 0
    _cleanup_ids([owner, target], [rec])


def test_shared_with_me_403_when_feature_disabled():
    with _db():
        user = _setup_user("swm_off")
    with _internal_disabled():
        resp = _client_for(user).get("/api/recordings/shared-with-me")
    assert resp.status_code == 403, resp.data
    _cleanup_ids([user], [])


# =============================================================================
# Internal sharing: ownership / IDOR boundaries (feature forced on)
# =============================================================================

def test_internal_share_owner_can_share_and_recipient_sees_it():
    """Happy path + visibility: owner shares -> recipient appears in
    shares-internal AND the recording appears in the recipient's
    shared-with-me, while a third party sees neither."""
    with _db():
        owner = _setup_user("isb_owner")
        recipient = _setup_user("isb_recipient")
        rec = _make_recording(owner, "team rec")

    with _internal_enabled():
        post = _client_for(owner).post(
            f"/api/recordings/{rec}/share-internal", json={"user_id": recipient},
        )
        assert post.status_code == 201, post.data

        listing = _client_for(owner).get(f"/api/recordings/{rec}/shares-internal")
        assert listing.status_code == 200
        user_ids = {s["user_id"] for s in listing.get_json()["shares"]}
        assert recipient in user_ids
        assert owner in user_ids  # owner entry is injected at the front

        swm = _client_for(recipient).get("/api/recordings/shared-with-me")
        assert swm.status_code == 200
        rec_ids = {r["id"] for r in swm.get_json()}
        assert rec in rec_ids, "recipient cannot see the recording shared with them"

    _cleanup_ids([owner, recipient], [rec])


def test_internal_share_nonowner_without_reshare_denied():
    """IDOR: a user who does NOT own and was NOT granted reshare cannot share
    the recording with anyone (403/404). No InternalShare is created."""
    with _db():
        owner = _setup_user("isn_owner")
        attacker = _setup_user("isn_attacker")
        third = _setup_user("isn_third")
        rec = _make_recording(owner)

    with _internal_enabled():
        resp = _client_for(attacker).post(
            f"/api/recordings/{rec}/share-internal", json={"user_id": third},
        )
    assert resp.status_code in (403, 404), resp.data
    with _db():
        assert InternalShare.query.filter_by(
            recording_id=rec, shared_with_user_id=third
        ).count() == 0, "Non-owner without reshare created an internal share (IDOR)"
    _cleanup_ids([owner, attacker, third], [rec])


def test_shares_internal_third_party_cannot_view():
    """IDOR: a third party (not owner, not recipient) cannot list a recording's
    internal shares (403/404)."""
    with _db():
        owner = _setup_user("siv_owner")
        recipient = _setup_user("siv_recipient")
        third = _setup_user("siv_third")
        rec = _make_recording(owner)
        _make_internal_share(rec, owner, recipient)

    with _internal_enabled():
        resp = _client_for(third).get(f"/api/recordings/{rec}/shares-internal")
    assert resp.status_code in (403, 404), resp.data
    _cleanup_ids([owner, recipient, third], [rec])


def test_shared_with_me_returns_only_callers_shares():
    """shared-with-me returns ONLY recordings shared with the caller, not
    recordings shared with other users."""
    with _db():
        owner = _setup_user("swmo_owner")
        me = _setup_user("swmo_me")
        other = _setup_user("swmo_other")
        rec_for_me = _make_recording(owner, "for me")
        rec_for_other = _make_recording(owner, "for other")
        _make_internal_share(rec_for_me, owner, me)
        _make_internal_share(rec_for_other, owner, other)

    with _internal_enabled():
        resp = _client_for(me).get("/api/recordings/shared-with-me")
    assert resp.status_code == 200
    rec_ids = {r["id"] for r in resp.get_json()}
    assert rec_for_me in rec_ids
    assert rec_for_other not in rec_ids, (
        "shared-with-me leaked a recording shared with a DIFFERENT user"
    )
    _cleanup_ids([owner, me, other], [rec_for_me, rec_for_other])


# =============================================================================
# Revoke internal share: only the owner (grantor) may revoke
# =============================================================================

def test_revoke_internal_share_third_party_denied_and_survives():
    """IDOR: a third party cannot revoke an internal share (403) and it survives."""
    with _db():
        owner = _setup_user("rev_owner")
        recipient = _setup_user("rev_recipient")
        third = _setup_user("rev_third")
        rec = _make_recording(owner)
        share_id = _make_internal_share(rec, owner, recipient)

    with _internal_enabled():
        resp = _client_for(third).delete(f"/api/internal-shares/{share_id}")
    assert resp.status_code in (403, 404), resp.data
    with _db():
        assert db.session.get(InternalShare, share_id) is not None, (
            "IDOR: third party revoked an internal share they don't own"
        )
    _cleanup_ids([owner, recipient, third], [rec])


def test_revoke_internal_share_recipient_cannot_revoke():
    """Per the route's 'only owner can revoke' rule, even the RECIPIENT of the
    share cannot revoke it (403); the share survives."""
    with _db():
        owner = _setup_user("revr_owner")
        recipient = _setup_user("revr_recipient")
        rec = _make_recording(owner)
        share_id = _make_internal_share(rec, owner, recipient)

    with _internal_enabled():
        resp = _client_for(recipient).delete(f"/api/internal-shares/{share_id}")
    assert resp.status_code == 403, resp.data
    with _db():
        assert db.session.get(InternalShare, share_id) is not None, (
            "Recipient revoked an internal share owned by someone else"
        )
    _cleanup_ids([owner, recipient], [rec])


def test_revoke_internal_share_owner_succeeds():
    """The grantor/owner of the share can revoke it (200) and it is deleted."""
    with _db():
        owner = _setup_user("revo_owner")
        recipient = _setup_user("revo_recipient")
        rec = _make_recording(owner)
        share_id = _make_internal_share(rec, owner, recipient)

    with _internal_enabled():
        resp = _client_for(owner).delete(f"/api/internal-shares/{share_id}")
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["success"] is True
    with _db():
        assert db.session.get(InternalShare, share_id) is None
    _cleanup_ids([owner, recipient], [rec])


def test_revoke_internal_share_requires_login():
    with _db():
        owner = _setup_user("revl_owner")
        recipient = _setup_user("revl_recipient")
        rec = _make_recording(owner)
        share_id = _make_internal_share(rec, owner, recipient)

    with _internal_enabled():
        resp = _anon_client().delete(f"/api/internal-shares/{share_id}")
    assert resp.status_code in (302, 401), resp.status_code
    with _db():
        assert db.session.get(InternalShare, share_id) is not None
    _cleanup_ids([owner, recipient], [rec])


# --- module teardown: sweep any stragglers ---------------------------------

def teardown_module(module):
    with app.app_context():
        prefixes = (
            "pv_", "cs_", "audio_", "ges_", "ls_", "us_", "ds_", "srch_",
            "csp_", "is_", "isb_", "isn_", "siv_", "swm", "rev",
        )
        users = User.query.filter(
            db.or_(*[User.username.like(f"{p}%") for p in prefixes])
        ).all()
        for u in users:
            for rec in Recording.query.filter_by(user_id=u.id).all():
                Share.query.filter_by(recording_id=rec.id).delete()
                InternalShare.query.filter_by(recording_id=rec.id).delete()
                SharedRecordingState.query.filter_by(recording_id=rec.id).delete()
                db.session.delete(rec)
            db.session.delete(u)
        db.session.commit()


if __name__ == "__main__":
    # Standalone run: create the schema (pytest's conftest session fixture does
    # this automatically) before invoking pytest on this file.
    from src.init_db import initialize_database
    with app.app_context():
        initialize_database(app)
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
