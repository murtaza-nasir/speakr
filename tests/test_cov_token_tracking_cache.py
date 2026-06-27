"""
Coverage for prompt-cache token tracking in src/services/token_tracking.py.

record_usage() now accepts cached_tokens / cache_write_tokens (subsets of the
prompt tokens that a prefix-caching backend served from / wrote to cache).
These must accumulate into the daily aggregate row and surface through
get_today_usage().

The test DB is shared across the suite, so every assertion is scoped to a
freshly created unique user — never to global aggregates.
"""

import pytest

from src.app import app, db
from src.models import User
from src.models.token_usage import TokenUsage
from src.services.token_tracking import token_tracker


@pytest.fixture
def isolated_user():
    with app.app_context():
        user = User(username="cache_track_test_user", email="cache_track_test@local.test")
        user.password = "unused"
        db.session.add(user)
        db.session.commit()
        uid = user.id
        try:
            yield uid
        finally:
            TokenUsage.query.filter_by(user_id=uid).delete()
            db.session.query(User).filter_by(id=uid).delete()
            db.session.commit()


def test_record_usage_accumulates_cache_tokens(isolated_user):
    with app.app_context():
        token_tracker.record_usage(
            user_id=isolated_user,
            operation_type="summarization",
            prompt_tokens=8000, completion_tokens=20, total_tokens=8020,
            cached_tokens=6000, cache_write_tokens=1500,
        )
        # Second call on the same day/operation must add to the existing row.
        token_tracker.record_usage(
            user_id=isolated_user,
            operation_type="summarization",
            prompt_tokens=8000, completion_tokens=20, total_tokens=8020,
            cached_tokens=7000, cache_write_tokens=0,
        )

        row = TokenUsage.query.filter_by(
            user_id=isolated_user, operation_type="summarization"
        ).first()
        assert row is not None
        assert row.cached_tokens == 13000
        assert row.cache_write_tokens == 1500
        assert row.request_count == 2


def test_get_today_usage_surfaces_cache_tokens(isolated_user):
    with app.app_context():
        token_tracker.record_usage(
            user_id=isolated_user,
            operation_type="chat",
            prompt_tokens=500, completion_tokens=10, total_tokens=510,
            cached_tokens=300, cache_write_tokens=120,
        )
        today = token_tracker.get_today_usage(user_id=isolated_user)
        assert today["cached_tokens"] == 300
        assert today["cache_write_tokens"] == 120


def test_record_usage_defaults_cache_tokens_to_zero(isolated_user):
    """Existing callers that pass no cache args must still work and store 0."""
    with app.app_context():
        token_tracker.record_usage(
            user_id=isolated_user,
            operation_type="title_generation",
            prompt_tokens=100, completion_tokens=5, total_tokens=105,
        )
        row = TokenUsage.query.filter_by(
            user_id=isolated_user, operation_type="title_generation"
        ).first()
        assert row.cached_tokens == 0
        assert row.cache_write_tokens == 0
