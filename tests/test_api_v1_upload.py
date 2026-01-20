#!/usr/bin/env python3
"""
Integration test for API v1 recording upload endpoint.

Validates API token authentication and expected 400 response when no file is provided.
"""

import secrets
import sys
import os

# Add the parent directory to the path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, APIToken
from src.utils.token_auth import hash_token


def _get_or_create_test_user():
    user = User.query.filter_by(username="api_test_user").first()
    created = False
    if not user:
        user = User(username="api_test_user", email="api_test_user@local.test")
        db.session.add(user)
        db.session.commit()
        created = True
    return user, created


def _create_api_token(user):
    plaintext = f"test-token-{secrets.token_urlsafe(16)}"
    token = APIToken(
        user_id=user.id,
        token_hash=hash_token(plaintext),
        name="test-api-token"
    )
    db.session.add(token)
    db.session.commit()
    return token, plaintext


def test_upload_requires_file():
    with app.app_context():
        user, created_user = _get_or_create_test_user()
        token_record, token = _create_api_token(user)
        client = app.test_client()

        try:
            response = client.post(
                "/api/v1/recordings/upload",
                headers={"X-API-Token": token}
            )

            if response.status_code != 400:
                print(f"❌ Expected 400, got {response.status_code}")
                return False

            payload = response.get_json(silent=True) or {}
            if payload.get("error") != "No file provided":
                print(f"❌ Unexpected error payload: {payload}")
                return False

            print("✅ Token auth works and missing file returns 400 as expected")
            return True
        finally:
            db.session.delete(token_record)
            db.session.commit()
            if created_user:
                db.session.delete(user)
                db.session.commit()


def main():
    print("🚀 Running API v1 upload test...\n")
    ok = test_upload_requires_file()
    print("\n" + ("✅ PASS" if ok else "❌ FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
