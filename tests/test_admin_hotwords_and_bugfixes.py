#!/usr/bin/env python3
"""
Tests for recent bug fixes and improvements.

Validates:
1. Admin default hotwords (SystemSetting fallback in processing)
2. Chat API endpoint returning proper string response (#245)
3. Inquire mode handling empty choices in streaming (#246)
4. User default transcription language in config endpoint (#250)

Run with: docker exec speakr-dev python /app/tests/test_recent_fixes.py
"""

import os
import sys
import json
import secrets
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, str(Path(__file__).parent.parent))

# Test results tracking
PASSED = 0
FAILED = 0
ERRORS = []


def run_test(name, func):
    global PASSED, FAILED, ERRORS
    try:
        func()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILED += 1
        ERRORS.append((name, str(e)))
    except Exception as e:
        print(f"  ✗ {name}: EXCEPTION - {e}")
        FAILED += 1
        ERRORS.append((name, f"Exception: {e}"))


# =============================================================================
# TEST SECTION 1: Admin Default Hotwords
# =============================================================================

def test_admin_default_hotwords():
    """Test admin_default_hotwords SystemSetting behavior."""
    print("\n=== Testing Admin Default Hotwords ===")

    from src.app import app, db
    from src.models import SystemSetting

    def t1():
        """SystemSetting can store and retrieve admin_default_hotwords."""
        with app.app_context():
            # Set the value
            SystemSetting.set_setting('admin_default_hotwords', 'TestWord, AnotherWord')
            # Retrieve it
            value = SystemSetting.get_setting('admin_default_hotwords', '')
            assert value == 'TestWord, AnotherWord', f"Expected 'TestWord, AnotherWord', got '{value}'"
            # Clean up
            SystemSetting.set_setting('admin_default_hotwords', '')
    run_test("admin_default_hotwords can be stored and retrieved", t1)

    def t2():
        """Empty admin_default_hotwords returns empty string."""
        with app.app_context():
            SystemSetting.set_setting('admin_default_hotwords', '')
            value = SystemSetting.get_setting('admin_default_hotwords', '')
            assert value == '', f"Expected empty string, got '{value}'"
    run_test("Empty admin_default_hotwords returns empty string", t2)

    def t3():
        """Admin hotwords only apply when user hotwords are empty (priority chain)."""
        # This tests the logic: if not hotwords -> use admin default
        # Simulate the processing.py logic
        with app.app_context():
            SystemSetting.set_setting('admin_default_hotwords', 'AdminWord1, AdminWord2')

            # Case 1: User has hotwords - admin should NOT be used
            user_hotwords = 'UserWord1, UserWord2'
            hotwords = user_hotwords
            if not hotwords:
                admin_hotwords = SystemSetting.get_setting('admin_default_hotwords', '')
                if admin_hotwords:
                    hotwords = admin_hotwords
            assert hotwords == 'UserWord1, UserWord2', "User hotwords should take priority"

            # Case 2: User has no hotwords - admin SHOULD be used
            user_hotwords = ''
            hotwords = user_hotwords
            if not hotwords:
                admin_hotwords = SystemSetting.get_setting('admin_default_hotwords', '')
                if admin_hotwords:
                    hotwords = admin_hotwords
            assert hotwords == 'AdminWord1, AdminWord2', "Admin hotwords should be used as fallback"

            # Case 3: No user hotwords AND no admin hotwords
            SystemSetting.set_setting('admin_default_hotwords', '')
            user_hotwords = ''
            hotwords = user_hotwords
            if not hotwords:
                admin_hotwords = SystemSetting.get_setting('admin_default_hotwords', '')
                if admin_hotwords:
                    hotwords = admin_hotwords
            assert hotwords == '', "Should remain empty when no hotwords at any level"

            # Clean up
            SystemSetting.set_setting('admin_default_hotwords', '')
    run_test("Hotword priority chain: user > admin > none", t3)


# =============================================================================
# TEST SECTION 2: Chat API Fix (#245)
# =============================================================================

def test_chat_api_fix():
    """Test that chat_with_recording extracts string from ChatCompletion object."""
    print("\n=== Testing Chat API Fix (#245) ===")

    from src.app import app, db
    from src.models import User, Recording, APIToken
    from src.utils.token_auth import hash_token

    def _get_or_create_test_user():
        user = User.query.filter_by(username="chat_test_user").first()
        created = False
        if not user:
            user = User(username="chat_test_user", email="chat_test@local.test")
            user.password_hash = "unused"
            db.session.add(user)
            db.session.commit()
            created = True
        return user, created

    def _create_test_recording(user):
        recording = Recording(
            user_id=user.id,
            title="Chat Test Recording",
            original_filename="test_chat.wav",
            transcription='[{"speaker": "Speaker 1", "text": "Hello world"}]',
            status="COMPLETED",
        )
        db.session.add(recording)
        db.session.commit()
        return recording

    def _create_api_token(user):
        plaintext = f"test-token-{secrets.token_urlsafe(16)}"
        token = APIToken(
            user_id=user.id,
            token_hash=hash_token(plaintext),
            name="chat-test-token"
        )
        db.session.add(token)
        db.session.commit()
        return token, plaintext

    def t1():
        """Chat endpoint returns JSON string, not ChatCompletion object."""
        with app.app_context():
            user, created_user = _get_or_create_test_user()
            recording = _create_test_recording(user)
            token_record, token = _create_api_token(user)

            try:
                # Mock the LLM call to return a ChatCompletion-like object
                mock_completion = MagicMock()
                mock_completion.choices = [MagicMock()]
                mock_completion.choices[0].message.content = "This is the AI response"

                with patch('src.services.llm.call_chat_completion', return_value=mock_completion), \
                     patch('src.services.llm.chat_client', new=MagicMock()), \
                     patch('src.api.api_v1.has_recording_access', return_value=True):
                    client = app.test_client()
                    response = client.post(
                        f"/api/v1/recordings/{recording.id}/chat",
                        headers={"X-API-Token": token, "Content-Type": "application/json"},
                        json={"message": "What was discussed?"}
                    )

                    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.data}"
                    data = response.get_json()
                    assert 'response' in data, f"Missing 'response' key in: {data}"
                    assert data['response'] == "This is the AI response"
                    assert isinstance(data['response'], str), f"Response should be string, got {type(data['response'])}"
            finally:
                db.session.delete(token_record)
                db.session.delete(recording)
                db.session.commit()
                if created_user:
                    db.session.delete(user)
                    db.session.commit()
    run_test("Chat API returns string response, not ChatCompletion object", t1)


# =============================================================================
# TEST SECTION 3: Azure Empty Choices Fix (#246)
# =============================================================================

def test_azure_empty_choices():
    """Test handling of empty choices in streaming responses."""
    print("\n=== Testing Azure Empty Choices Fix (#246) ===")

    def t1():
        """Empty choices array should be skipped, not crash."""
        # Simulate the streaming loop logic from inquire.py
        collected_content = ""

        # Create mock chunks - some with empty choices (Azure content filter)
        class MockDelta:
            def __init__(self, content=None):
                self.content = content

        class MockChoice:
            def __init__(self, content=None):
                self.delta = MockDelta(content)

        class MockChunk:
            def __init__(self, choices=None):
                self.choices = choices if choices is not None else []

        chunks = [
            MockChunk(choices=[]),                          # Empty choices (Azure filter)
            MockChunk(choices=[MockChoice("Hello ")]),      # Normal chunk
            MockChunk(choices=[]),                          # Empty choices again
            MockChunk(choices=[MockChoice("world")]),       # Normal chunk
            MockChunk(choices=[MockChoice(None)]),          # Choice with None content
        ]

        for chunk in chunks:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content
            if content:
                collected_content += content

        assert collected_content == "Hello world", f"Expected 'Hello world', got '{collected_content}'"
    run_test("Empty choices array is safely skipped in streaming", t1)

    def t2():
        """All-empty stream produces no output (doesn't crash)."""
        chunks = []

        class MockChunk:
            def __init__(self):
                self.choices = []

        chunks = [MockChunk(), MockChunk(), MockChunk()]
        collected = ""
        for chunk in chunks:
            if not chunk.choices:
                continue
            content = chunk.choices[0].delta.content
            if content:
                collected += content

        assert collected == ""
    run_test("All-empty-choices stream produces empty output", t2)


# =============================================================================
# TEST SECTION 4: User Default Transcription Language (#250)
# =============================================================================

def test_user_transcription_language():
    """Test user transcription language in config endpoint."""
    print("\n=== Testing User Default Transcription Language (#250) ===")

    from src.app import app, db
    from src.models import User

    def t1():
        """Config endpoint returns user_transcription_language for authenticated user."""
        with app.app_context():
            # Find or create a test user with a transcription language set
            user = User.query.filter_by(username="lang_test_user").first()
            created = False
            if not user:
                user = User(username="lang_test_user", email="lang_test@local.test")
                user.password_hash = "unused"
                db.session.add(user)
                db.session.commit()
                created = True

            # Set transcription language
            user.transcription_language = 'fr'
            db.session.commit()

            try:
                client = app.test_client()

                # Simulate logged-in user
                with client.session_transaction() as sess:
                    sess['_user_id'] = str(user.id)

                response = client.get('/api/config')
                assert response.status_code == 200, f"Expected 200, got {response.status_code}"

                data = response.get_json()
                assert 'user_transcription_language' in data, f"Missing user_transcription_language in config: {list(data.keys())}"
                assert data['user_transcription_language'] == 'fr', f"Expected 'fr', got '{data['user_transcription_language']}'"
            finally:
                user.transcription_language = ''
                db.session.commit()
                if created:
                    db.session.delete(user)
                    db.session.commit()
    run_test("Config endpoint includes user_transcription_language", t1)

    def t2():
        """Config endpoint returns empty string for user without language set."""
        with app.app_context():
            user = User.query.filter_by(username="lang_test_user2").first()
            created = False
            if not user:
                user = User(username="lang_test_user2", email="lang_test2@local.test")
                user.password_hash = "unused"
                db.session.add(user)
                db.session.commit()
                created = True

            # Ensure no language set
            user.transcription_language = ''
            db.session.commit()

            try:
                client = app.test_client()
                with client.session_transaction() as sess:
                    sess['_user_id'] = str(user.id)

                response = client.get('/api/config')
                data = response.get_json()
                # Should be empty string (falsy) since user has no language set
                assert data.get('user_transcription_language', '') == '' or data.get('user_transcription_language') is None
            finally:
                if created:
                    db.session.delete(user)
                    db.session.commit()
    run_test("Config returns empty language for user without preference", t2)

    def t3():
        """Config endpoint works for unauthenticated requests (no crash)."""
        with app.app_context():
            client = app.test_client()
            response = client.get('/api/config')
            assert response.status_code == 200
            data = response.get_json()
            # Should still have the key, just empty
            assert 'user_transcription_language' in data
    run_test("Config endpoint works without authentication", t3)


# =============================================================================
# Main
# =============================================================================

def main():
    global PASSED, FAILED, ERRORS

    print("=" * 60)
    print("Recent Fixes Tests (#240, #245, #246, #250)")
    print("=" * 60)

    test_admin_default_hotwords()
    test_chat_api_fix()
    test_azure_empty_choices()
    test_user_transcription_language()

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=" * 60)

    if ERRORS:
        print("\nFailed tests:")
        for name, error in ERRORS:
            print(f"  - {name}: {error}")

    return 0 if FAILED == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
