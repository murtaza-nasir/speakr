#!/usr/bin/env python3
"""
Test script for Inquire Mode functionality.

These tests create their own isolated data inside the app context rather than
relying on rows already present in the database, so they run correctly against
the fresh, empty DB that conftest.py provisions for the pytest suite.
"""
import os
import sys
import uuid

# Add the parent directory to the path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db, User, Recording, TranscriptChunk, InquireSession


def _unique_suffix():
    """Return a short unique token so test rows never collide on unique cols."""
    return uuid.uuid4().hex[:8]


def test_database_models():
    """Create a user, recording, chunk and inquire session and assert they persist."""
    with app.app_context():
        # The schema must contain the inquire-mode tables.
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        assert 'transcript_chunk' in tables, "transcript_chunk table missing from schema"
        assert 'inquire_session' in tables, "inquire_session table missing from schema"

        suffix = _unique_suffix()
        user = User(username=f"inq_{suffix}", email=f"inq_{suffix}@example.com")
        db.session.add(user)
        db.session.commit()

        recording = Recording(
            user_id=user.id,
            title=f"Test Recording {suffix}",
            status="COMPLETED",
        )
        db.session.add(recording)
        db.session.commit()

        chunk = TranscriptChunk(
            recording_id=recording.id,
            user_id=user.id,
            chunk_index=0,
            content="This is a test transcription chunk.",
            start_time=0.0,
            end_time=5.0,
            speaker_name="Test Speaker",
        )
        db.session.add(chunk)

        session = InquireSession(
            user_id=user.id,
            session_name="Test Session",
            filter_tags='[]',
            filter_speakers='["Test Speaker"]',
        )
        db.session.add(session)
        db.session.commit()

        try:
            # The rows must be retrievable and carry the data we stored.
            stored_chunk = TranscriptChunk.query.get(chunk.id)
            assert stored_chunk is not None
            assert stored_chunk.recording_id == recording.id
            assert stored_chunk.user_id == user.id
            assert stored_chunk.content == "This is a test transcription chunk."
            assert stored_chunk.speaker_name == "Test Speaker"

            stored_session = InquireSession.query.get(session.id)
            assert stored_session is not None
            assert stored_session.user_id == user.id
            assert stored_session.session_name == "Test Session"
            assert stored_session.filter_speakers == '["Test Speaker"]'
        finally:
            # Clean up so repeat runs / other tests start from a clean slate.
            db.session.delete(chunk)
            db.session.delete(session)
            db.session.delete(recording)
            db.session.delete(user)
            db.session.commit()


def test_chunking_functions():
    """Exercise the real chunking/embedding helpers re-exported by src.app."""
    with app.app_context():
        from src.app import (
            chunk_transcription,
            generate_embeddings,
            serialize_embedding,
            deserialize_embedding,
        )

        # chunk_transcription must split a long passage into ordered chunks.
        test_text = (
            "This is a test sentence. This is another sentence for testing. "
            "And here's a third sentence to make sure chunking works properly "
            "with longer text that should be split into multiple chunks."
        )
        chunks = chunk_transcription(test_text, max_chunk_length=100, overlap=20)
        assert isinstance(chunks, list)
        assert len(chunks) >= 1
        # Every chunk must be a non-empty string.
        assert all(isinstance(c, str) and c.strip() for c in chunks)
        # With max_chunk_length=100 on this ~190-char passage we expect a split.
        assert len(chunks) > 1, f"expected multiple chunks, got {len(chunks)}"

        # generate_embeddings' documented contract: one vector per input when an
        # embedding backend is available (API mode via EMBEDDING_BASE_URL, or a
        # local sentence-transformers model), otherwise an EMPTY list — it does
        # not return None placeholders. sentence-transformers isn't a hard
        # dependency (it's absent in CI), so assert whichever applies here.
        from src.services.embeddings import USE_API_EMBEDDINGS, LOCAL_EMBEDDINGS_AVAILABLE
        embeddings = generate_embeddings(["test sentence", "another test"])
        assert isinstance(embeddings, list)
        if USE_API_EMBEDDINGS or LOCAL_EMBEDDINGS_AVAILABLE:
            assert len(embeddings) == 2, "one embedding per input when a backend is available"
            # The vectors must survive a serialize/deserialize round-trip.
            restored = deserialize_embedding(serialize_embedding(embeddings[0]))
            assert restored is not None
        else:
            assert embeddings == [], "no embedding backend -> documented empty-list contract"

        if embeddings[0] is not None:
            serialized = serialize_embedding(embeddings[0])
            assert serialized is not None
            deserialized = deserialize_embedding(serialized)
            assert deserialized is not None
            assert len(deserialized) > 0


def test_api_imports():
    """The inquire-mode endpoint functions must be importable and callable."""
    from src.api.inquire import (
        get_inquire_sessions,
        create_inquire_session,
        inquire_search,
        inquire_chat,
        get_available_filters,
    )
    from src.api.recordings import process_recording_chunks_endpoint

    for fn in (
        get_inquire_sessions,
        create_inquire_session,
        inquire_search,
        inquire_chat,
        get_available_filters,
        process_recording_chunks_endpoint,
    ):
        assert callable(fn), f"{getattr(fn, '__name__', fn)} is not callable"


def main():
    """Run all tests (standalone, without pytest)."""
    print("Starting Inquire Mode Tests...\n")

    tests = [
        ("Database Models", test_database_models),
        ("Chunking Functions", test_chunking_functions),
        ("API Imports", test_api_imports),
    ]

    all_passed = True
    for test_name, test_func in tests:
        print(f"--- {test_name} ---")
        try:
            test_func()
            print(f"PASS - {test_name}")
        except Exception as e:
            print(f"FAIL - {test_name}: {e}")
            all_passed = False

    print("\nAll tests passed!" if all_passed else "\nSome tests failed.")
    return all_passed


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
