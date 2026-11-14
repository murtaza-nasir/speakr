"""resolve_timestamp_template_format: a user's own transcript template, or
None (#304, #606).

SHARED-DB: the users and their templates are removed afterwards.
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db  # noqa: E402
from src.models import TranscriptTemplate, User  # noqa: E402
from src.utils.transcript_render import resolve_timestamp_template_format  # noqa: E402


@pytest.fixture
def users():
    with app.app_context():
        made = []
        for _ in range(2):
            suffix = uuid.uuid4().hex[:8]
            made.append(User(username=f"rtt_{suffix}", email=f"rtt_{suffix}@local.test", password="x"))
        db.session.add_all(made)
        db.session.commit()
        tpl = TranscriptTemplate(user_id=made[0].id, name="Mine", template="{{start_time}} {{text}}")
        db.session.add(tpl)
        db.session.commit()
        yield made[0], made[1], tpl.id
        TranscriptTemplate.query.filter_by(id=tpl.id).delete()
        for user in made:
            db.session.delete(user)
        db.session.commit()


def test_resolve_timestamp_template_format_scopes_to_owner(users):
    owner, other, template_id = users
    assert resolve_timestamp_template_format(owner, template_id) == "{{start_time}} {{text}}"
    assert resolve_timestamp_template_format(other, template_id) is None


def test_resolve_timestamp_template_format_none_inputs(users):
    owner, _, template_id = users
    assert resolve_timestamp_template_format(None, template_id) is None
    assert resolve_timestamp_template_format(owner, None) is None
    assert resolve_timestamp_template_format(owner, template_id + 100000) is None
