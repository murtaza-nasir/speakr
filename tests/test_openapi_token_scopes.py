"""/api/v1/openapi.json lists only what the requesting token may call (#526).

Open WebUI builds one tool per operation, so a read-only token used to be
offered every write and delete tool. Full tokens and sessions keep the whole
document.

SHARED-DB: the user and tokens are removed afterwards.
"""

import json
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import APIToken, User
from src.utils.token_auth import hash_token

URL = "/api/v1/openapi.json"


@pytest.fixture
def tokens():
    with app.app_context():
        s = secrets.token_hex(4)
        u = User(username=f"oas_{s}", email=f"oas_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        plain = {}
        for name, scopes in (("full", None), ("read", ["read"]), ("read_write", ["read", "write"])):
            plain[name] = f"tok-{secrets.token_urlsafe(16)}"
            db.session.add(APIToken(user_id=u.id, token_hash=hash_token(plain[name]), name=name,
                                    scopes=None if scopes is None else json.dumps(scopes)))
        db.session.commit()
        uid = u.id
    yield uid, plain
    with app.app_context():
        APIToken.query.filter_by(user_id=uid).delete()
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _operations(spec):
    return {(method, path): op for path, ops in spec["paths"].items()
            for method, op in ops.items() if isinstance(op, dict)}


def _fetch(token):
    r = app.test_client().get(URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    return _operations(r.get_json())


def test_full_token_gets_every_operation(tokens):
    _, plain = tokens
    ops = _fetch(plain["full"])
    assert ("delete", "/recordings/{id}") in ops
    assert any(op.get("x-required-scopes") == ["read"] for op in ops.values())


def test_read_token_gets_only_read_operations(tokens):
    _, plain = tokens
    full, read = _fetch(plain["full"]), _fetch(plain["read"])
    assert read
    assert set(read) < set(full)
    assert all(set(op["x-required-scopes"]) <= {"read"} for op in read.values())
    assert ("get", "/recordings") in read
    assert ("delete", "/recordings/{id}") not in read
    expected = {key for key, op in full.items()
                if op.get("x-required-scopes") is not None and set(op["x-required-scopes"]) <= {"read"}}
    assert set(read) == expected


def test_read_write_token_adds_write_operations(tokens):
    _, plain = tokens
    read, read_write = _fetch(plain["read"]), _fetch(plain["read_write"])
    assert set(read) < set(read_write)
    assert all(set(op["x-required-scopes"]) <= {"read", "write"} for op in read_write.values())
    assert ("delete", "/recordings/{id}") not in read_write


def test_session_gets_every_operation(tokens):
    uid, plain = tokens
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    r = c.get(URL)
    assert r.status_code == 200
    assert set(_operations(r.get_json())) == set(_fetch(plain["full"]))
