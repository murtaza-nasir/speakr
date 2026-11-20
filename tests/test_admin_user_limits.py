"""Budgets and the storage quota are whole numbers of 0 or more (#651).

A text budget used to be stored as given, after which four admin pages
failed for every administrator."""
import logging
import uuid

import pytest
from sqlalchemy import text

from src.app import app, db
from src.models import User
from tests.test_cov_admin import admin_client, admin_user, ctx, normal_user  # noqa: F401  (fixtures)

BAD = ["abc", "1.5", -1, True, [], {}, -2.5, "-3"]
FIELDS = {"monthly_token_budget": "invalid_token_budget",
          "monthly_transcription_budget": "invalid_transcription_budget",
          "storage_quota_mb": "invalid_storage_quota"}


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", BAD)
def test_update_refuses_a_limit_that_is_not_a_whole_number(admin_client, normal_user, field, value):
    resp = admin_client.put(f"/admin/users/{normal_user.id}", json={field: value})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == FIELDS[field]
    db.session.expire_all()
    assert getattr(db.session.get(User, normal_user.id), field) is None


@pytest.mark.parametrize("value,stored", [(1000, 1000), ("1000", 1000), (" 7 ", 7), (12.0, 12),
                                          (0, None), ("0", None), ("", None), (None, None)])
def test_update_accepts_a_whole_number_or_no_limit(admin_client, normal_user, value, stored):
    resp = admin_client.put(f"/admin/users/{normal_user.id}", json={"monthly_token_budget": value})
    assert resp.status_code == 200, resp.data
    db.session.expire_all()
    assert db.session.get(User, normal_user.id).monthly_token_budget == stored


@pytest.mark.parametrize("field", ["monthly_token_budget", "monthly_transcription_budget"])
def test_create_refuses_a_text_budget_and_creates_no_account(admin_client, field):
    name = "b_" + uuid.uuid4().hex[:8]
    resp = admin_client.post("/admin/users", json={"username": name, "email": f"{name}@local.test",
                                                   "password": "secret123", field: "abc"})
    assert resp.status_code == 400 and resp.get_json()["code"] == FIELDS[field]
    assert User.query.filter_by(username=name).first() is None


def test_create_stores_a_numeric_budget(admin_client):
    name = "b_" + uuid.uuid4().hex[:8]
    resp = admin_client.post("/admin/users", json={"username": name, "email": f"{name}@local.test",
                                                   "password": "secret123", "monthly_token_budget": "500"})
    assert resp.status_code == 201, resp.data
    assert resp.get_json()["monthly_token_budget"] == 500
    user = User.query.filter_by(username=name).first()
    db.session.delete(user)
    db.session.commit()


def test_a_text_budget_stored_before_the_fix_is_cleared_at_startup(normal_user, caplog):
    from src.init_db import clear_text_user_limits
    if db.engine.dialect.name != "sqlite":
        pytest.skip("only SQLite stores text in an INTEGER column")
    with db.engine.begin() as conn:
        conn.execute(text('UPDATE "user" SET monthly_token_budget = :v, storage_quota_mb = 50 WHERE id = :id'),
                     {"v": "abc", "id": normal_user.id})
    with caplog.at_level(logging.WARNING):
        clear_text_user_limits(db.engine, logging.getLogger("test"))
    db.session.expire_all()
    user = db.session.get(User, normal_user.id)
    assert user.monthly_token_budget is None and user.storage_quota_mb == 50
    assert str(normal_user.id) in caplog.text
