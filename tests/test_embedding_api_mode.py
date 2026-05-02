#!/usr/bin/env python3
"""
Unit tests for issue #262 follow-up: API-mode embeddings.

Verifies that:
- USE_API_EMBEDDINGS reflects the EMBEDDING_BASE_URL env var.
- EMBEDDING_IDENTIFIER composes the provider + model name.
- generate_embeddings() routes through the API client when API mode is on.
- The local SentenceTransformer path is not initialised in API mode.

The OpenAI client is patched out; the test does not make real network calls.

Run with: docker exec speakr-dev python /app/tests/test_embedding_api_mode.py
"""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


PASSED = 0
FAILED = 0


def run(name, func):
    global PASSED, FAILED
    try:
        func()
        print(f"  ✓ {name}")
        PASSED += 1
    except AssertionError as e:
        print(f"  ✗ {name}: {e}")
        FAILED += 1
    except Exception as e:
        print(f"  ✗ {name}: EXCEPTION - {e}")
        FAILED += 1


def reload_with_env(**env):
    """Reload the embeddings module with the requested env overrides."""
    keys = list(env.keys()) + [
        "EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_DIMENSIONS",
    ]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in env.items():
            if v is not None:
                os.environ[k] = v
        import src.services.embeddings as emb
        importlib.reload(emb)
        return emb
    finally:
        # Restore env so later tests see a clean slate, then reload again.
        for k in keys:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


def test_default_is_local_mode():
    emb = reload_with_env()
    assert emb.USE_API_EMBEDDINGS is False, f"got USE_API_EMBEDDINGS={emb.USE_API_EMBEDDINGS}"
    assert emb.EMBEDDING_MODEL == "all-MiniLM-L6-v2"
    assert emb.EMBEDDING_IDENTIFIER == "local::all-MiniLM-L6-v2", emb.EMBEDDING_IDENTIFIER


def test_base_url_switches_to_api_mode():
    emb = reload_with_env(
        EMBEDDING_BASE_URL="https://api.openai.com/v1",
        EMBEDDING_API_KEY="sk-test",
        EMBEDDING_MODEL="text-embedding-3-small",
    )
    assert emb.USE_API_EMBEDDINGS is True
    assert emb.EMBEDDING_BASE_URL == "https://api.openai.com/v1"
    assert emb.EMBEDDING_MODEL == "text-embedding-3-small"
    assert emb.EMBEDDING_IDENTIFIER == "https://api.openai.com/v1::text-embedding-3-small"


def test_dimensions_parsed():
    emb = reload_with_env(
        EMBEDDING_BASE_URL="https://api.openai.com/v1",
        EMBEDDING_MODEL="text-embedding-3-large",
        EMBEDDING_DIMENSIONS="1024",
    )
    assert emb.EMBEDDING_DIMENSIONS == 1024


def test_dimensions_invalid_falls_back_to_none():
    emb = reload_with_env(
        EMBEDDING_BASE_URL="https://api.openai.com/v1",
        EMBEDDING_DIMENSIONS="not-a-number",
    )
    assert emb.EMBEDDING_DIMENSIONS is None


def test_generate_embeddings_uses_api_when_active():
    emb = reload_with_env(
        EMBEDDING_BASE_URL="http://localhost:9999/v1",
        EMBEDDING_API_KEY="not-needed",
        EMBEDDING_MODEL="bge-base",
    )
    fake_response = MagicMock()
    fake_response.data = [
        MagicMock(embedding=[0.1, 0.2, 0.3]),
        MagicMock(embedding=[0.4, 0.5, 0.6]),
    ]
    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = fake_response

    from flask import Flask
    test_app = Flask(__name__)
    with test_app.app_context():
        with patch.object(emb, "get_embedding_api_client", return_value=fake_client):
            vectors = emb.generate_embeddings(["hello", "world"])

    assert len(vectors) == 2, f"expected 2 vectors, got {len(vectors)}"
    assert all(v.dtype.name == "float32" for v in vectors)
    fake_client.embeddings.create.assert_called_once()
    call_kwargs = fake_client.embeddings.create.call_args.kwargs
    assert call_kwargs["model"] == "bge-base"
    assert call_kwargs["input"] == ["hello", "world"]
    # No explicit dimensions, so kwarg should be absent.
    assert "dimensions" not in call_kwargs


def test_generate_embeddings_passes_dimensions_when_set():
    emb = reload_with_env(
        EMBEDDING_BASE_URL="http://localhost:9999/v1",
        EMBEDDING_MODEL="text-embedding-3-large",
        EMBEDDING_DIMENSIONS="512",
    )
    fake_response = MagicMock()
    fake_response.data = [MagicMock(embedding=[0.0] * 512)]
    fake_client = MagicMock()
    fake_client.embeddings.create.return_value = fake_response

    from flask import Flask
    test_app = Flask(__name__)
    with test_app.app_context():
        with patch.object(emb, "get_embedding_api_client", return_value=fake_client):
            emb.generate_embeddings(["hello"])

    call_kwargs = fake_client.embeddings.create.call_args.kwargs
    assert call_kwargs["dimensions"] == 512


def test_get_embedding_model_returns_none_in_api_mode():
    emb = reload_with_env(EMBEDDING_BASE_URL="http://localhost:9999/v1")
    from flask import Flask
    test_app = Flask(__name__)
    with test_app.app_context():
        assert emb.get_embedding_model() is None


def main():
    print("=== Issue #262 follow-up: API-mode embeddings ===\n")
    run("default config is local mode", test_default_is_local_mode)
    run("EMBEDDING_BASE_URL switches to API mode", test_base_url_switches_to_api_mode)
    run("EMBEDDING_DIMENSIONS parses to int", test_dimensions_parsed)
    run("invalid EMBEDDING_DIMENSIONS falls back to None", test_dimensions_invalid_falls_back_to_none)
    run("generate_embeddings uses API client when active", test_generate_embeddings_uses_api_when_active)
    run("generate_embeddings forwards dimensions when set", test_generate_embeddings_passes_dimensions_when_set)
    run("get_embedding_model returns None in API mode", test_get_embedding_model_returns_none_in_api_mode)

    # Reset the module to its default state for any subsequent test runs.
    reload_with_env()

    print(f"\nResults: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
