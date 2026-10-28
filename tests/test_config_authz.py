"""/api/config needs a signed-in user (#502).

The document carries the administrator's default summary prompt, which the
upload form of every signed-in user reads for {{name}} placeholders.

SHARED-DB: the user is removed afterwards.
"""

import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User


def test_anonymous_caller_gets_401():
    resp = app.test_client().get('/api/config')
    assert resp.status_code == 401
    body = resp.get_json()
    assert body['code'] == 'authentication_required'
    assert 'admin_default_summary_prompt' not in body and 'max_file_size_mb' not in body


def test_signed_in_member_gets_the_whole_document():
    with app.app_context():
        s = secrets.token_hex(4)
        user = User(username=f"cfg_{s}", email=f"cfg_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        uid = user.id
    try:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(uid)
        resp = client.get('/api/config')
        assert resp.status_code == 200
        body = resp.get_json()
        for key in ('admin_default_summary_prompt', 'max_file_size_mb', 'user_summary_prompt',
                    'can_delete_recordings'):
            assert key in body
    finally:
        with app.app_context():
            User.query.filter_by(id=uid).delete()
            db.session.commit()
