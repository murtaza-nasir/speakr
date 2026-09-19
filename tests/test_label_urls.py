"""Behaviour tests for the /label/<name> deep link.

/label/foo is the label-scoped counterpart of the /recordings/<id> deep link:
the server just serves the SPA shell, and the frontend resolves the name
against the viewer's own accessible tags and applies it as the tag filter, so
one URL points at a group of recordings.

Two things are therefore server-side and testable here:
  - the route itself exists, is login-gated, and serves the shell for the
    name shapes a real tag can have (plain, spaced, cased, non-existent);
  - the tag filter behind it (``?q=tag:<name>`` on /api/recordings) matches a
    tag name EXACTLY and case-insensitively, so /label/foo cannot quietly
    include recordings tagged "foobar".

Harness notes follow tests/test_cov_recordings_read.py: all DB setup happens
inside a short-lived ``_db()`` context that is exited before any client
request, because Flask-Login caches the resolved ``current_user`` on the
application context.

Run:
    python -m pytest tests/test_label_urls.py -q
"""

import os
import sys
import uuid
import tempfile
from contextlib import contextmanager
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Standalone safety net (conftest.py already sets these under pytest).
if "SQLALCHEMY_DATABASE_URI" not in os.environ:
    _D = tempfile.mkdtemp(prefix="speakr_label_urls_")
    os.environ["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(_D, 'test.db')}"
    os.environ.setdefault("UPLOAD_FOLDER", os.path.join(_D, "uploads"))
    os.environ.setdefault("SECRET_KEY", "pytest-secret-key")
    os.environ.setdefault("ENABLE_AUTO_PROCESSING", "false")
    os.environ.setdefault("TEXT_MODEL_API_KEY", "test-key")
    os.environ.setdefault("TRANSCRIPTION_API_KEY", "test-key")
    os.environ.setdefault("TRANSCRIPTION_BASE_URL", "https://api.openai.com/v1")

from src.app import app, db
from src.models import User, Recording
from src.models.organization import (
    Group,
    GroupMembership,
    RecordingTag,
    Tag,
)

app.config["WTF_CSRF_ENABLED"] = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_PREFIX = "labelurl"


@contextmanager
def _db():
    """Short-lived app context for DB work; exit before any HTTP request."""
    with app.app_context():
        yield


def make_user():
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"{_PREFIX}_{suffix}",
        email=f"{_PREFIX}_{suffix}@test.local",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def make_recording(user, *, title="rec"):
    rec = Recording(
        audio_path="local://labelurl/audio.mp3",
        original_filename="audio.mp3",
        title=title,
        status="COMPLETED",
        transcription="hello world this is a transcript",
        summary="a summary",
        user_id=user.id,
        mime_type="audio/mpeg",
    )
    db.session.add(rec)
    db.session.commit()
    return rec


def tag_recording(recording, tag):
    db.session.add(RecordingTag(recording_id=recording.id, tag_id=tag.id, order=1))
    db.session.commit()


def login(client, user_id):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True


def new_client():
    return app.test_client()


def ids_for_label(client, label_name):
    """IDs the list endpoint returns for the filter /label/<name> produces.

    The frontend turns a selected tag into the ``tag:<name>`` search token
    (spaces encoded as underscores), so this mirrors what the browser sends.
    """
    token = label_name.replace(" ", "_")
    resp = client.get(f"/api/recordings?q=tag:{quote(token)}&per_page=100")
    assert resp.status_code == 200
    return {r["id"] for r in resp.get_json()["recordings"]}


@pytest.fixture
def owner():
    with _db():
        return make_user().id


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #

def test_label_url_serves_the_spa_shell(owner):
    """/label/<name> is a real route and renders the app, like /recordings/<id>."""
    with _db():
        u = db.session.get(User, owner)
        tag = Tag(name=f"{_PREFIX}shell", user_id=u.id)
        db.session.add(tag)
        db.session.commit()

    c = new_client()
    login(c, owner)
    resp = c.get(f"/label/{_PREFIX}shell")
    assert resp.status_code == 200
    # Same shell the '/' route serves — the label is resolved client-side.
    #
    # Compared with the CSRF meta token blanked out. Flask-WTF signs that
    # token with a timed serializer, so its bytes embed the current second;
    # two renders that straddle a second boundary differ there and nowhere
    # else. That made a raw byte comparison flake in the full suite while
    # passing every time this file ran alone.
    import re
    def _shell(body):
        return re.sub(rb'(name="csrf-token"\s+content=")[^"]*(")', rb'\1\2', body)
    assert _shell(resp.data) == _shell(c.get("/").data)


def test_label_url_accepts_spaces_and_mixed_case(owner):
    """A percent-encoded, differently-cased name still reaches the shell."""
    c = new_client()
    login(c, owner)
    assert c.get(f"/label/{quote('Team Sync')}").status_code == 200
    assert c.get("/label/TEAM+SYNC").status_code == 200


def test_unknown_label_still_serves_the_shell(owner):
    """The server doesn't know the viewer's tags here; the frontend reports it.

    Serving the shell keeps tag-access logic in one place (the tags API) and
    lets the app say "No label named X" in context instead of showing a bare
    404 page.
    """
    c = new_client()
    login(c, owner)
    assert c.get(f"/label/{_PREFIX}-does-not-exist").status_code == 200


def test_label_url_requires_login():
    c = new_client()
    resp = c.get("/label/anything")
    assert resp.status_code in (302, 401)


# --------------------------------------------------------------------------- #
# The filter behind it
# --------------------------------------------------------------------------- #

def test_label_filter_is_exact_not_substring(owner):
    """/label/foo must not drag in recordings tagged 'foobar'."""
    base = f"{_PREFIX}{uuid.uuid4().hex[:6]}"
    with _db():
        u = db.session.get(User, owner)
        exact = Tag(name=base, user_id=u.id)
        longer = Tag(name=f"{base}bar", user_id=u.id)
        db.session.add_all([exact, longer])
        db.session.commit()

        wanted = make_recording(u, title="exactly-foo")
        unwanted = make_recording(u, title="foobar-only")
        tag_recording(wanted, exact)
        tag_recording(unwanted, longer)
        wanted_id, unwanted_id = wanted.id, unwanted.id

    c = new_client()
    login(c, owner)
    ids = ids_for_label(c, base)
    assert wanted_id in ids
    assert unwanted_id not in ids


def test_label_filter_is_case_insensitive(owner):
    base = f"{_PREFIX}Case{uuid.uuid4().hex[:6]}"
    with _db():
        u = db.session.get(User, owner)
        tag = Tag(name=base, user_id=u.id)
        db.session.add(tag)
        db.session.commit()
        rec = make_recording(u, title="cased")
        tag_recording(rec, tag)
        rec_id = rec.id

    c = new_client()
    login(c, owner)
    assert rec_id in ids_for_label(c, base.lower())
    assert rec_id in ids_for_label(c, base.upper())


def test_label_filter_matches_a_name_containing_spaces(owner):
    name = f"{_PREFIX} spaced {uuid.uuid4().hex[:6]}"
    with _db():
        u = db.session.get(User, owner)
        tag = Tag(name=name, user_id=u.id)
        db.session.add(tag)
        db.session.commit()
        rec = make_recording(u, title="spaced")
        other = make_recording(u, title="untagged")
        tag_recording(rec, tag)
        rec_id, other_id = rec.id, other.id

    c = new_client()
    login(c, owner)
    ids = ids_for_label(c, name)
    assert rec_id in ids
    assert other_id not in ids


def test_label_filter_unions_a_personal_and_a_group_tag_of_the_same_name(owner):
    """Tag names aren't unique across the tags one viewer can see.

    A user can hold only one tag per name (_user_tag_uc on name+user_id), but
    a group tag belongs to the admin who created it — so a member with a
    personal tag of the same name sees two distinct "foo" tags. /label/<name>
    covers their union, which is what the frontend asks for by putting every
    matching tag id in the filter.
    """
    name = f"{_PREFIX}dual{uuid.uuid4().hex[:6]}"
    with _db():
        u = db.session.get(User, owner)
        admin = make_user()
        group = Group(name=f"{_PREFIX}-group-{uuid.uuid4().hex[:8]}")
        db.session.add(group)
        db.session.commit()
        db.session.add_all([
            GroupMembership(group_id=group.id, user_id=admin.id, role="admin"),
            GroupMembership(group_id=group.id, user_id=u.id, role="member"),
        ])

        personal = Tag(name=name, user_id=u.id)
        group_tag = Tag(name=name, user_id=admin.id, group_id=group.id)
        db.session.add_all([personal, group_tag])
        db.session.commit()

        # Both recordings are the viewer's own; only the tag differs, so the
        # assertion is about tag resolution and not about sharing.
        via_personal = make_recording(u, title="via-personal")
        via_group = make_recording(u, title="via-group")
        tag_recording(via_personal, personal)
        tag_recording(via_group, group_tag)
        personal_id, group_rec_id = via_personal.id, via_group.id
        group_db_id = group.id

    c = new_client()
    login(c, owner)
    # The frontend emits one tag: token per matching tag id; both resolve to
    # the same name, so a single token already covers the union.
    ids = ids_for_label(c, name)
    assert personal_id in ids
    assert group_rec_id in ids

    with _db():
        GroupMembership.query.filter_by(group_id=group_db_id).delete()
        db.session.delete(db.session.get(Group, group_db_id))
        db.session.commit()


def test_label_filter_does_not_leak_another_users_recordings(owner):
    """Same label name, two users: each only ever sees their own recordings."""
    name = f"{_PREFIX}shared{uuid.uuid4().hex[:6]}"
    with _db():
        mine = db.session.get(User, owner)
        theirs = make_user()
        my_tag = Tag(name=name, user_id=mine.id)
        their_tag = Tag(name=name, user_id=theirs.id)
        db.session.add_all([my_tag, their_tag])
        db.session.commit()

        my_rec = make_recording(mine, title="mine")
        their_rec = make_recording(theirs, title="theirs")
        tag_recording(my_rec, my_tag)
        tag_recording(their_rec, their_tag)
        my_id, their_id = my_rec.id, their_rec.id

    c = new_client()
    login(c, owner)
    ids = ids_for_label(c, name)
    assert my_id in ids
    assert their_id not in ids


# --------------------------------------------------------------------------- #
# Module-level cleanup
# --------------------------------------------------------------------------- #

def teardown_module(module):
    with app.app_context():
        for u in User.query.filter(User.username.like(f"{_PREFIX}_%")).all():
            for r in Recording.query.filter_by(user_id=u.id).all():
                RecordingTag.query.filter_by(recording_id=r.id).delete()
                db.session.delete(r)
            Tag.query.filter_by(user_id=u.id).delete()
            GroupMembership.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        for g in Group.query.filter(Group.name.like(f"{_PREFIX}-group-%")).all():
            db.session.delete(g)
        db.session.commit()
