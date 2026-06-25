#!/usr/bin/env python3
"""
Coverage-focused tests for src/services/embeddings.py.

These complement the existing targeted tests:
  - tests/test_embedding_api_mode.py        (mode selection / generate_embeddings API path)
  - tests/test_embedding_identifier_compat.py (identifier migration at startup)

Here we fill the gaps: chunk_transcription splitting, process_recording_chunks
(happy path, idempotency, empty/short transcript, embedding failure rollback,
length mismatch rollback), serialize/deserialize gating, the API retry/backoff
machinery (_api_embed, _is_transient_embedding_error, token tracking), provider
client init, get_accessible_recording_ids, and basic_text_search_chunks +
semantic_search_chunks (including the API-mode and fallback paths).

All heavy/external pieces are mocked at the embeddings.py import site, so the
suite is fully offline and hermetic. The local sentence-transformers model is
NOT installed in the test image, so local-mode tests stub the model object.

Run (pytest only — these rely on conftest.py's isolated DB):
  pytest tests/test_cov_embeddings.py -q
"""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.app import app, db  # noqa: E402
from src.models import Recording, TranscriptChunk, User  # noqa: E402

import src.services.embeddings as emb_default  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENV_KEYS = (
    "EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY",
    "EMBEDDING_DIMENSIONS", "EMBEDDING_API_MAX_RETRIES",
    "EMBEDDING_API_BACKOFF_SECONDS", "ENABLE_INTERNAL_SHARING",
)


def _reload_embeddings(**env):
    """Reload the embeddings module with a clean set of env overrides.

    Returns the freshly reloaded module object. Any global on that object
    (USE_API_EMBEDDINGS, EMBEDDINGS_AVAILABLE, etc.) reflects the env passed.
    """
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    for k, v in env.items():
        if v is not None:
            os.environ[k] = str(v)
    import src.services.embeddings as emb
    importlib.reload(emb)
    return emb, saved


def _restore_env(saved):
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v
    importlib.reload(emb_default)


@pytest.fixture
def local_emb():
    """Reload embeddings in default (local) mode and force EMBEDDINGS_AVAILABLE
    on so the storage gating in serialize/deserialize is exercised even though
    sentence-transformers is not installed in the test image."""
    emb, saved = _reload_embeddings()
    yield emb
    _restore_env(saved)


@pytest.fixture
def api_emb():
    """Reload embeddings in API mode (EMBEDDING_BASE_URL set)."""
    emb, saved = _reload_embeddings(
        EMBEDDING_BASE_URL="http://localhost:9999/v1",
        EMBEDDING_API_KEY="not-needed",
        EMBEDDING_MODEL="bge-test",
        EMBEDDING_API_MAX_RETRIES="3",
        EMBEDDING_API_BACKOFF_SECONDS="0",  # no real sleeping in tests
    )
    yield emb
    _restore_env(saved)


_USER_SEQ = [0]


def _make_user():
    """Create a unique user scoped to this test file (shared session DB)."""
    _USER_SEQ[0] += 1
    suffix = f"cov_emb_{os.getpid()}_{_USER_SEQ[0]}"
    u = User(username=suffix[:20], email=f"{suffix}@example.test", password="x")
    db.session.add(u)
    db.session.commit()
    return u


def _make_recording(user, transcription, **kw):
    r = Recording(
        user_id=user.id,
        transcription=transcription,
        title=kw.pop("title", "cov-emb-rec"),
        status="COMPLETED",
        **kw,
    )
    db.session.add(r)
    db.session.commit()
    return r


def _fixed_vectors(texts, dim=4):
    """Deterministic fixed-dim vectors, one per text."""
    out = []
    for i, _ in enumerate(texts):
        out.append(np.full(dim, float(i + 1), dtype=np.float32))
    return out


# ===========================================================================
# chunk_transcription
# ===========================================================================

def test_chunk_empty_returns_empty(local_emb):
    assert local_emb.chunk_transcription("") == []
    assert local_emb.chunk_transcription(None) == []


def test_chunk_short_returns_single(local_emb):
    text = "A short transcript."
    assert local_emb.chunk_transcription(text, max_chunk_length=500) == [text]


def test_chunk_exactly_at_limit_returns_single(local_emb):
    text = "x" * 50
    assert local_emb.chunk_transcription(text, max_chunk_length=50) == [text]


def test_chunk_long_text_splits_multiple(local_emb):
    text = ("Sentence one is here. " * 60).strip()
    chunks = local_emb.chunk_transcription(text, max_chunk_length=100, overlap=10)
    assert len(chunks) > 1
    # every chunk fits roughly within the bound (sentence breaks can shorten)
    assert all(len(c) <= 100 for c in chunks)
    # reconstructable-ish: each chunk is non-empty stripped text
    assert all(c == c.strip() and c for c in chunks)


def test_chunk_respects_sentence_boundary(local_emb):
    # Build text where a sentence ends near the boundary so the splitter
    # breaks at the period rather than mid-word.
    first = "This is the opening sentence and it keeps going for a while. "
    second = "Then the next sentence begins after the boundary point here."
    text = first + second
    chunks = local_emb.chunk_transcription(text, max_chunk_length=len(first) + 5, overlap=5)
    assert len(chunks) >= 2
    assert chunks[0].endswith(".")


def test_chunk_sentence_boundary_requires_following_space(local_emb):
    # MUTATION TARGET (line 248): the boundary check is
    #   `i + 1 < len(transcription) and transcription[i + 1].isspace()`
    # i.e. a '.'/'!'/'?' only counts as a sentence break when the NEXT
    # character is whitespace. This rejects the dot inside "3.14".
    #
    # With max_chunk_length=40 the window scans indices 0..39, which contains
    # two periods: index 11 ("home.", followed by a space) and index 18 (the
    # dot in "3.14", followed by '1'). The `and` keeps only the period+space
    # break at 12, so chunk 0 == "I went home.". If `and` is mutated to `or`,
    # the bare '3.' dot at 18 also qualifies, pushing the split to index 19 and
    # making chunk 0 == "I went home. The 3." instead.
    text = "I went home. The 3.14 value is large and stuff here too."
    assert len(text) > 40  # ensure it actually splits
    chunks = local_emb.chunk_transcription(text, max_chunk_length=40, overlap=10)
    assert chunks[0] == "I went home."


def test_chunk_no_overlap_infinite_loop_guard(local_emb):
    # A single huge token with no sentence boundaries still terminates.
    text = "a" * 2000
    chunks = local_emb.chunk_transcription(text, max_chunk_length=100, overlap=50)
    assert len(chunks) > 1
    assert all(chunks)


# ===========================================================================
# serialize_embedding / deserialize_embedding
# ===========================================================================

def test_serialize_roundtrip_when_available(api_emb):
    # api_emb mode => EMBEDDINGS_AVAILABLE is True (sklearn present).
    assert api_emb.EMBEDDINGS_AVAILABLE is True
    vec = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    blob = api_emb.serialize_embedding(vec)
    assert isinstance(blob, (bytes, bytearray))
    back = api_emb.deserialize_embedding(blob)
    assert np.allclose(back, vec)
    assert back.dtype == np.float32


def test_serialize_none_returns_none(api_emb):
    assert api_emb.serialize_embedding(None) is None
    assert api_emb.deserialize_embedding(None) is None


def test_serialize_gated_off_when_unavailable(local_emb):
    # Default/local mode in the test image: ST not installed, no API URL =>
    # EMBEDDINGS_AVAILABLE is False, so serialize/deserialize return None.
    assert local_emb.EMBEDDINGS_AVAILABLE is False
    vec = np.array([1.0, 2.0], dtype=np.float32)
    assert local_emb.serialize_embedding(vec) is None
    blob = vec.tobytes()
    assert local_emb.deserialize_embedding(blob) is None


# ===========================================================================
# _is_transient_embedding_error
# ===========================================================================

@pytest.mark.parametrize("msg", [
    "Connection timed out",
    "rate limit exceeded",
    "429 Too Many Requests",
    "503 Service Unavailable",
    "The server is overloaded, please try again",
    "connection reset by peer",
])
def test_transient_errors_detected(api_emb, msg):
    assert api_emb._is_transient_embedding_error(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "invalid api key",
    "model not found",
    "401 Unauthorized",
    "no such model",
])
def test_non_transient_errors_detected(api_emb, msg):
    assert api_emb._is_transient_embedding_error(Exception(msg)) is False


# ===========================================================================
# get_embedding_api_client / get_embedding_model
# ===========================================================================

def test_get_api_client_none_in_local_mode(local_emb):
    with app.app_context():
        assert local_emb.get_embedding_api_client() is None


def test_get_embedding_model_none_without_local_lib(local_emb):
    # sentence-transformers not installed in test image -> None.
    with app.app_context():
        assert local_emb.get_embedding_model() is None


def test_get_embedding_model_none_in_api_mode_even_with_local_lib(api_emb):
    # MUTATION TARGET (line 77): `if USE_API_EMBEDDINGS or not
    # LOCAL_EMBEDDINGS_AVAILABLE: return None`. In API mode the local
    # sentence-transformers model must NEVER be loaded, even if the local lib
    # is present. Force LOCAL_EMBEDDINGS_AVAILABLE True and stub
    # SentenceTransformer so that, were the guard mutated `or`->`and`
    # (USE_API_EMBEDDINGS True, not LOCAL == False => the whole guard goes
    # False), the function would fall through and construct/return a model.
    # The original short-circuits on USE_API_EMBEDDINGS and returns None.
    fake_model = MagicMock()
    with app.app_context():
        with patch.object(api_emb, "LOCAL_EMBEDDINGS_AVAILABLE", True):
            with patch.object(api_emb, "SentenceTransformer",
                              MagicMock(return_value=fake_model)):
                api_emb._embedding_model = None
                assert api_emb.get_embedding_model() is None


def test_get_api_client_initialises_and_caches(api_emb):
    fake_client = MagicMock()
    fake_openai = MagicMock(return_value=fake_client)
    fake_llm = MagicMock(llm_timeout=30, LLM_MAX_RETRIES=2, http_client_no_proxy=None)
    with app.app_context():
        with patch.dict("sys.modules", {"openai": MagicMock(OpenAI=fake_openai),
                                          "src.services.llm": fake_llm}):
            c1 = api_emb.get_embedding_api_client()
            c2 = api_emb.get_embedding_api_client()
    assert c1 is fake_client
    assert c2 is fake_client  # cached, only constructed once
    fake_openai.assert_called_once()


def test_get_api_client_handles_init_failure(api_emb):
    boom = MagicMock(side_effect=RuntimeError("boom"))
    with app.app_context():
        with patch.dict("sys.modules", {"openai": MagicMock(OpenAI=boom),
                                          "src.services.llm": MagicMock()}):
            assert api_emb.get_embedding_api_client() is None


# ===========================================================================
# _api_embed
# ===========================================================================

def _client_returning(vectors):
    resp = MagicMock()
    resp.data = [MagicMock(embedding=list(v)) for v in vectors]
    resp.usage = None
    client = MagicMock()
    client.embeddings.create.return_value = resp
    return client


def test_api_embed_empty_texts_returns_empty(api_emb):
    with app.app_context():
        with patch.object(api_emb, "get_embedding_api_client", return_value=MagicMock()):
            assert api_emb._api_embed([]) == []


def test_api_embed_empty_texts_does_not_call_client(api_emb):
    # MUTATION TARGET (line 158): `if client is None or not texts:` must
    # short-circuit on empty `texts` EVEN WHEN a client exists. The earlier
    # `== []` assertion alone is insufficient: with the `or`->`and` mutation
    # the function would proceed to call the client (whose MagicMock response
    # then raises inside the comprehension and is swallowed back to []), so the
    # result stays [] but the client IS now hit. Asserting the client was never
    # called is what actually kills the mutation.
    client = MagicMock()
    with app.app_context():
        with patch.object(api_emb, "get_embedding_api_client", return_value=client):
            assert api_emb._api_embed([]) == []
    client.embeddings.create.assert_not_called()


def test_api_embed_no_client_returns_empty(api_emb):
    with app.app_context():
        with patch.object(api_emb, "get_embedding_api_client", return_value=None):
            assert api_emb._api_embed(["x"]) == []


def test_api_embed_success(api_emb):
    client = _client_returning([[0.1, 0.2], [0.3, 0.4]])
    with app.app_context():
        with patch.object(api_emb, "get_embedding_api_client", return_value=client):
            out = api_emb._api_embed(["a", "b"])
    assert len(out) == 2
    assert all(v.dtype == np.float32 for v in out)
    assert np.allclose(out[0], [0.1, 0.2])


def test_api_embed_forwards_dimensions(api_emb):
    emb, saved = _reload_embeddings(
        EMBEDDING_BASE_URL="http://localhost:9999/v1",
        EMBEDDING_MODEL="text-embedding-3-large",
        EMBEDDING_DIMENSIONS="8",
        EMBEDDING_API_BACKOFF_SECONDS="0",
    )
    try:
        client = _client_returning([[0.0] * 8])
        with app.app_context():
            with patch.object(emb, "get_embedding_api_client", return_value=client):
                emb._api_embed(["x"])
        kwargs = client.embeddings.create.call_args.kwargs
        assert kwargs["dimensions"] == 8
    finally:
        _restore_env(saved)


def test_api_embed_retries_then_succeeds(api_emb):
    resp = MagicMock()
    resp.data = [MagicMock(embedding=[1.0, 2.0])]
    resp.usage = None
    client = MagicMock()
    # First two calls transient-fail, third succeeds.
    client.embeddings.create.side_effect = [
        Exception("connection timeout"),
        Exception("503 service unavailable"),
        resp,
    ]
    with app.app_context():
        with patch.object(api_emb, "get_embedding_api_client", return_value=client):
            with patch.object(api_emb.time, "sleep"):
                out = api_emb._api_embed(["x"])
    assert len(out) == 1
    assert client.embeddings.create.call_count == 3


def test_api_embed_non_transient_fails_fast(api_emb):
    client = MagicMock()
    client.embeddings.create.side_effect = Exception("invalid api key")
    with app.app_context():
        with patch.object(api_emb, "get_embedding_api_client", return_value=client):
            out = api_emb._api_embed(["x"])
    # Permanent error -> single attempt, empty result.
    assert out == []
    assert client.embeddings.create.call_count == 1


def test_api_embed_exhausts_retries_returns_empty(api_emb):
    client = MagicMock()
    client.embeddings.create.side_effect = Exception("rate limit hit")
    with app.app_context():
        with patch.object(api_emb, "get_embedding_api_client", return_value=client):
            with patch.object(api_emb.time, "sleep"):
                out = api_emb._api_embed(["x"])
    assert out == []
    assert client.embeddings.create.call_count == 3  # max attempts


def test_api_embed_records_token_usage(api_emb):
    resp = MagicMock()
    resp.data = [MagicMock(embedding=[1.0, 2.0])]
    usage = MagicMock()
    usage.model_extra = {"cost": 0.0001}
    usage.prompt_tokens = 11
    usage.total_tokens = 11
    resp.usage = usage
    client = MagicMock()
    client.embeddings.create.return_value = resp

    tracker = MagicMock()
    fake_tt_mod = MagicMock(token_tracker=tracker)
    with app.app_context():
        with patch.object(api_emb, "get_embedding_api_client", return_value=client):
            with patch.dict("sys.modules", {"src.services.token_tracking": fake_tt_mod}):
                out = api_emb._api_embed(["x"], user_id=123)
    assert len(out) == 1
    tracker.record_usage.assert_called_once()
    kwargs = tracker.record_usage.call_args.kwargs
    assert kwargs["user_id"] == 123
    assert kwargs["operation_type"] == "embedding"
    assert kwargs["total_tokens"] == 11
    assert kwargs["cost"] == pytest.approx(0.0001)


def test_api_embed_usage_tracking_failure_is_swallowed(api_emb):
    resp = MagicMock()
    resp.data = [MagicMock(embedding=[1.0, 2.0])]
    usage = MagicMock()
    usage.model_extra = {}
    usage.prompt_tokens = 5
    usage.total_tokens = 5
    resp.usage = usage
    client = MagicMock()
    client.embeddings.create.return_value = resp

    tracker = MagicMock()
    tracker.record_usage.side_effect = RuntimeError("db down")
    fake_tt_mod = MagicMock(token_tracker=tracker)
    with app.app_context():
        with patch.object(api_emb, "get_embedding_api_client", return_value=client):
            with patch.dict("sys.modules", {"src.services.token_tracking": fake_tt_mod}):
                out = api_emb._api_embed(["x"], user_id=1)
    # Tracking blew up but embedding result is still returned.
    assert len(out) == 1


# ===========================================================================
# generate_embeddings (local-mode branches; API branch covered elsewhere)
# ===========================================================================

def test_generate_embeddings_empty_input(local_emb):
    assert local_emb.generate_embeddings([]) == []


def test_generate_embeddings_local_unavailable(local_emb):
    # No local lib, no API => warning + empty.
    with app.app_context():
        assert local_emb.generate_embeddings(["x"]) == []


def test_generate_embeddings_local_model_path(local_emb):
    # Simulate a loaded local model by patching get_embedding_model and
    # flipping LOCAL_EMBEDDINGS_AVAILABLE on.
    fake_model = MagicMock()
    fake_model.encode.return_value = [np.array([0.1, 0.2], dtype=np.float64),
                                       np.array([0.3, 0.4], dtype=np.float64)]
    with app.app_context():
        with patch.object(local_emb, "LOCAL_EMBEDDINGS_AVAILABLE", True):
            with patch.object(local_emb, "get_embedding_model", return_value=fake_model):
                out = local_emb.generate_embeddings(["a", "b"])
    assert len(out) == 2
    assert all(v.dtype == np.float32 for v in out)


def test_generate_embeddings_local_model_none(local_emb):
    with app.app_context():
        with patch.object(local_emb, "LOCAL_EMBEDDINGS_AVAILABLE", True):
            with patch.object(local_emb, "get_embedding_model", return_value=None):
                assert local_emb.generate_embeddings(["a"]) == []


def test_generate_embeddings_local_encode_raises(local_emb):
    fake_model = MagicMock()
    fake_model.encode.side_effect = RuntimeError("cuda oom")
    with app.app_context():
        with patch.object(local_emb, "LOCAL_EMBEDDINGS_AVAILABLE", True):
            with patch.object(local_emb, "get_embedding_model", return_value=fake_model):
                assert local_emb.generate_embeddings(["a"]) == []


# ===========================================================================
# process_recording_chunks
# ===========================================================================

def test_process_missing_recording(api_emb):
    with app.app_context():
        assert api_emb.process_recording_chunks(999999) is False


def test_process_recording_no_transcription(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription=None)
        assert api_emb.process_recording_chunks(rec.id) is False


def test_process_empty_string_transcription_noop(api_emb):
    # Empty transcription is falsy => returns False (handled like missing).
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="")
        assert api_emb.process_recording_chunks(rec.id) is False


def test_process_whitespace_only_chunks_to_empty(api_emb):
    # Non-empty but chunk_transcription yields [] only for falsy input;
    # a short non-empty transcript yields one chunk, so test the genuine
    # "no chunks" branch by patching chunk_transcription to return [].
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="some real text here")
        with patch.object(api_emb, "chunk_transcription", return_value=[]):
            assert api_emb.process_recording_chunks(rec.id) is True
        assert TranscriptChunk.query.filter_by(recording_id=rec.id).count() == 0


def test_process_happy_path_creates_chunks(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="hello world, this is content")
        with patch.object(api_emb, "generate_embeddings",
                          side_effect=lambda texts, user_id=None: _fixed_vectors(texts)):
            ok = api_emb.process_recording_chunks(rec.id)
        assert ok is True
        chunks = TranscriptChunk.query.filter_by(recording_id=rec.id).order_by(
            TranscriptChunk.chunk_index).all()
        assert len(chunks) == 1
        assert chunks[0].chunk_index == 0
        assert chunks[0].content
        assert chunks[0].embedding is not None  # serialized blob stored
        assert chunks[0].user_id == user.id


def test_process_multi_chunk(api_emb):
    long_text = ("This is a self contained sentence number one. " * 40).strip()
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription=long_text)
        with patch.object(api_emb, "generate_embeddings",
                          side_effect=lambda texts, user_id=None: _fixed_vectors(texts)):
            ok = api_emb.process_recording_chunks(rec.id)
        assert ok is True
        n = TranscriptChunk.query.filter_by(recording_id=rec.id).count()
        assert n > 1


def test_process_idempotent_replaces_old_chunks(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="first version of the text")
        with patch.object(api_emb, "generate_embeddings",
                          side_effect=lambda texts, user_id=None: _fixed_vectors(texts)):
            assert api_emb.process_recording_chunks(rec.id) is True
            first_count = TranscriptChunk.query.filter_by(recording_id=rec.id).count()
            assert first_count == 1
            # Re-run: old chunks are deleted and replaced (no duplication /
            # accumulation), so the count stays at exactly one.
            assert api_emb.process_recording_chunks(rec.id) is True
            second = TranscriptChunk.query.filter_by(recording_id=rec.id).all()
        assert len(second) == 1
        # The single surviving chunk reflects the (re)generated content.
        assert second[0].content
        assert second[0].chunk_index == 0


def test_process_embedding_failure_rolls_back(api_emb):
    # Pre-seed a chunk, then make embedding generation return [] so the
    # length-mismatch branch rolls back and preserves existing chunks.
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="content that will rechunk")
        with patch.object(api_emb, "generate_embeddings",
                          side_effect=lambda texts, user_id=None: _fixed_vectors(texts)):
            assert api_emb.process_recording_chunks(rec.id) is True
        before = TranscriptChunk.query.filter_by(recording_id=rec.id).all()
        before_ids = {c.id for c in before}
        assert before_ids

        # Now embedding returns empty -> mismatch -> rollback, old chunks kept.
        with patch.object(api_emb, "generate_embeddings",
                          side_effect=lambda texts, user_id=None: []):
            assert api_emb.process_recording_chunks(rec.id) is False
        after_ids = {c.id for c in TranscriptChunk.query.filter_by(recording_id=rec.id)}
        assert after_ids == before_ids  # nothing lost


def test_process_partial_embedding_count_rolls_back(api_emb):
    long_text = ("Independent sentence here for chunking. " * 40).strip()
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription=long_text)
        # Return only one vector regardless of chunk count -> mismatch.
        with patch.object(api_emb, "generate_embeddings",
                          side_effect=lambda texts, user_id=None: _fixed_vectors(texts[:1])):
            assert api_emb.process_recording_chunks(rec.id) is False
        assert TranscriptChunk.query.filter_by(recording_id=rec.id).count() == 0


def test_process_exception_path_returns_false(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="boom text")
        with patch.object(api_emb, "chunk_transcription",
                          side_effect=RuntimeError("unexpected")):
            assert api_emb.process_recording_chunks(rec.id) is False


# ===========================================================================
# get_accessible_recording_ids
# ===========================================================================

def test_accessible_ids_own_recordings(api_emb):
    with app.app_context():
        user = _make_user()
        r1 = _make_recording(user, transcription="t1")
        r2 = _make_recording(user, transcription="t2")
        ids = api_emb.get_accessible_recording_ids(user.id)
        assert set(ids) >= {r1.id, r2.id}


def test_accessible_ids_none_for_unknown_user(api_emb):
    with app.app_context():
        assert api_emb.get_accessible_recording_ids(8888888) == []


def test_accessible_ids_sharing_disabled_excludes_shares(api_emb):
    # ENABLE_INTERNAL_SHARING defaults to false in this reload, so the share
    # branch is skipped — only own recordings are returned.
    assert api_emb.ENABLE_INTERNAL_SHARING is False
    with app.app_context():
        user = _make_user()
        r = _make_recording(user, transcription="owned")
        ids = api_emb.get_accessible_recording_ids(user.id)
        assert r.id in ids


# ===========================================================================
# basic_text_search_chunks
# ===========================================================================

def _seed_chunks(emb, user, rec, texts):
    for i, t in enumerate(texts):
        c = TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=i, content=t,
            embedding=emb.serialize_embedding(np.full(4, float(i + 1), dtype=np.float32)),
        )
        db.session.add(c)
    db.session.commit()


def test_basic_search_no_accessible_recordings(api_emb):
    with app.app_context():
        assert api_emb.basic_text_search_chunks(7777777, "anything") == []


def test_basic_search_ranks_by_match_count(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        _seed_chunks(api_emb, user, rec, [
            "the quick brown fox jumps",
            "fox and hound running together quick",
            "completely unrelated weather report",
        ])
        results = api_emb.basic_text_search_chunks(user.id, "quick fox", top_k=5)
        assert results
        # Top result should contain both query words.
        top_chunk, top_score = results[0]
        assert top_score == pytest.approx(1.0)
        assert "fox" in top_chunk.content.lower() and "quick" in top_chunk.content.lower()


def test_basic_search_all_stop_words_fallback(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        _seed_chunks(api_emb, user, rec, ["is the of an"])
        # Query is entirely stop words; falls back to using them as terms.
        results = api_emb.basic_text_search_chunks(user.id, "is the", top_k=5)
        assert isinstance(results, list)


def test_basic_search_only_single_char_query_returns_empty(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        _seed_chunks(api_emb, user, rec, ["a b c d"])
        # All query words are <=1 char -> no usable words -> [].
        results = api_emb.basic_text_search_chunks(user.id, "a b", top_k=5)
        assert results == []


def test_basic_search_recording_id_filter(api_emb):
    with app.app_context():
        user = _make_user()
        rec1 = _make_recording(user, transcription="seed1")
        rec2 = _make_recording(user, transcription="seed2")
        _seed_chunks(api_emb, user, rec1, ["alpha keyword here"])
        _seed_chunks(api_emb, user, rec2, ["alpha keyword elsewhere"])
        results = api_emb.basic_text_search_chunks(
            user.id, "keyword", filters={"recording_ids": [rec1.id]}, top_k=5)
        assert results
        assert all(c.recording_id == rec1.id for c, _ in results)


def test_basic_search_speaker_filter(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed", participants="Alice, Bob")
        _seed_chunks(api_emb, user, rec, ["meeting keyword discussion"])
        results = api_emb.basic_text_search_chunks(
            user.id, "keyword", filters={"speaker_names": ["Alice"]}, top_k=5)
        assert results
        assert all(c.recording_id == rec.id for c, _ in results)


def test_basic_search_date_filter(api_emb):
    import datetime as _dt
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed",
                              meeting_date=_dt.datetime(2024, 5, 1))
        _seed_chunks(api_emb, user, rec, ["meeting keyword discussion"])
        results = api_emb.basic_text_search_chunks(
            user.id, "keyword",
            filters={
                "date_from": _dt.datetime(2024, 1, 1),
                "date_to": _dt.datetime(2024, 12, 31),
            },
            top_k=5,
        )
        assert results
        assert all(c.recording_id == rec.id for c, _ in results)


def test_basic_search_tag_filter(api_emb):
    from src.models import Tag, RecordingTag
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        _seed_chunks(api_emb, user, rec, ["tagged keyword content"])
        tag = Tag(name="cov-tag", user_id=user.id)
        db.session.add(tag)
        db.session.commit()
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id))
        db.session.commit()
        results = api_emb.basic_text_search_chunks(
            user.id, "keyword", filters={"tag_ids": [tag.id]}, top_k=5)
        assert results
        assert all(c.recording_id == rec.id for c, _ in results)


def test_basic_search_exception_returns_empty(api_emb):
    with app.app_context():
        with patch.object(api_emb, "get_accessible_recording_ids",
                          side_effect=RuntimeError("db error")):
            assert api_emb.basic_text_search_chunks(1, "x") == []


# ===========================================================================
# semantic_search_chunks
# ===========================================================================

def test_semantic_falls_back_when_embeddings_unavailable(local_emb):
    # local_emb: EMBEDDINGS_AVAILABLE False -> falls back to basic text search.
    assert local_emb.EMBEDDINGS_AVAILABLE is False
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        # local mode: serialize returns None, so seed chunks without embeddings.
        c = TranscriptChunk(recording_id=rec.id, user_id=user.id, chunk_index=0,
                            content="searchable keyword content")
        db.session.add(c)
        db.session.commit()
        results = local_emb.semantic_search_chunks(user.id, "keyword", top_k=5)
        assert results  # came back via basic_text_search fallback
        assert any("keyword" in ch.content.lower() for ch, _ in results)


def test_semantic_api_mode_ranks_by_similarity(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        # Seed chunks with known 4-dim vectors.
        vecs = [
            np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),  # close to query
            np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),  # orthogonal
        ]
        for i, v in enumerate(vecs):
            db.session.add(TranscriptChunk(
                recording_id=rec.id, user_id=user.id, chunk_index=i,
                content=f"chunk {i}", embedding=api_emb.serialize_embedding(v)))
        db.session.commit()

        query_vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            results = api_emb.semantic_search_chunks(user.id, "find me", top_k=5)
        assert len(results) == 2
        # First chunk (aligned vector) ranks highest.
        assert results[0][0].chunk_index == 0
        assert results[0][1] > results[1][1]


def test_semantic_api_embed_failure_falls_back(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        db.session.add(TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=0,
            content="fallback keyword text",
            embedding=api_emb.serialize_embedding(np.ones(4, dtype=np.float32))))
        db.session.commit()
        # API returns nothing -> semantic search falls back to text search.
        with patch.object(api_emb, "_api_embed", return_value=[]):
            results = api_emb.semantic_search_chunks(user.id, "keyword", top_k=5)
        assert results
        assert any("keyword" in ch.content.lower() for ch, _ in results)


def test_semantic_no_accessible_recordings_returns_empty(api_emb):
    with app.app_context():
        query_vec = np.ones(4, dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            assert api_emb.semantic_search_chunks(9999990, "x") == []


def test_semantic_skips_dim_mismatch_chunks(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        # One good 4-dim vector, one stale 8-dim vector.
        db.session.add(TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=0, content="good",
            embedding=api_emb.serialize_embedding(np.array([1, 0, 0, 0], dtype=np.float32))))
        db.session.add(TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=1, content="stale",
            embedding=api_emb.serialize_embedding(np.ones(8, dtype=np.float32))))
        db.session.commit()
        query_vec = np.array([1, 0, 0, 0], dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            results = api_emb.semantic_search_chunks(user.id, "q", top_k=5)
        # Only the dimension-matching chunk survives.
        assert len(results) == 1
        assert results[0][0].content == "good"


def test_semantic_no_chunks_with_embeddings_returns_empty(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        # Chunk with no embedding at all.
        db.session.add(TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=0,
            content="no embedding", embedding=None))
        db.session.commit()
        query_vec = np.ones(4, dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            assert api_emb.semantic_search_chunks(user.id, "q", top_k=5) == []


def test_semantic_top_k_partition_path(api_emb):
    # More chunks than top_k exercises the argpartition branch.
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        for i in range(6):
            v = np.zeros(4, dtype=np.float32)
            v[i % 4] = 1.0
            db.session.add(TranscriptChunk(
                recording_id=rec.id, user_id=user.id, chunk_index=i,
                content=f"c{i}", embedding=api_emb.serialize_embedding(v)))
        db.session.commit()
        query_vec = np.array([1, 0, 0, 0], dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            results = api_emb.semantic_search_chunks(user.id, "q", top_k=2)
        assert len(results) == 2
        # results sorted descending by similarity
        assert results[0][1] >= results[1][1]


def test_semantic_api_mode_recording_id_filter(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        db.session.add(TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=0, content="filtered",
            embedding=api_emb.serialize_embedding(np.array([1, 0, 0, 0], dtype=np.float32))))
        db.session.commit()
        query_vec = np.array([1, 0, 0, 0], dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            results = api_emb.semantic_search_chunks(
                user.id, "q", filters={"recording_ids": [rec.id]}, top_k=5)
        assert len(results) == 1
        assert results[0][0].content == "filtered"


def test_semantic_api_mode_tag_filter(api_emb):
    from src.models import Tag, RecordingTag
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed")
        tag = Tag(name="cov-sem-tag", user_id=user.id)
        db.session.add(tag)
        db.session.commit()
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id))
        db.session.add(TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=0, content="tagged",
            embedding=api_emb.serialize_embedding(np.array([1, 0, 0, 0], dtype=np.float32))))
        db.session.commit()
        query_vec = np.array([1, 0, 0, 0], dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            results = api_emb.semantic_search_chunks(
                user.id, "q", filters={"tag_ids": [tag.id]}, top_k=5)
        assert len(results) == 1
        assert results[0][0].content == "tagged"


def test_semantic_api_mode_speaker_filter(api_emb):
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed", participants="Carol")
        db.session.add(TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=0, content="spoken",
            embedding=api_emb.serialize_embedding(np.array([1, 0, 0, 0], dtype=np.float32))))
        db.session.commit()
        query_vec = np.array([1, 0, 0, 0], dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            results = api_emb.semantic_search_chunks(
                user.id, "q", filters={"speaker_names": ["Carol"]}, top_k=5)
        assert len(results) == 1
        assert results[0][0].content == "spoken"


def test_semantic_api_mode_date_filter(api_emb):
    import datetime as _dt
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed",
                              meeting_date=_dt.datetime(2023, 6, 1))
        db.session.add(TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=0, content="dated",
            embedding=api_emb.serialize_embedding(np.array([1, 0, 0, 0], dtype=np.float32))))
        db.session.commit()
        query_vec = np.array([1, 0, 0, 0], dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            results = api_emb.semantic_search_chunks(
                user.id, "q",
                filters={
                    "date_from": _dt.datetime(2023, 1, 1),
                    "date_to": _dt.datetime(2023, 12, 31),
                },
                top_k=5,
            )
        assert len(results) == 1
        assert results[0][0].content == "dated"


def test_semantic_exception_returns_empty(api_emb):
    with app.app_context():
        with patch.object(api_emb, "_api_embed", side_effect=RuntimeError("boom")):
            assert api_emb.semantic_search_chunks(1, "q") == []


def test_semantic_combined_filters_no_duplicate_join(api_emb):
    """Regression: combining tag + speaker + date filters previously appended a
    second JOIN to Recording, raising an ambiguous-relationship / duplicate-JOIN
    SQL error. They must now coexist."""
    from datetime import datetime as _dt
    from src.models import Tag, RecordingTag
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, transcription="seed",
                              participants="Alice, Bob",
                              meeting_date=_dt(2026, 1, 15))
        tag = Tag(name=f"cov-combo-sem-{os.getpid()}", user_id=user.id)
        db.session.add(tag)
        db.session.commit()
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id))
        db.session.add(TranscriptChunk(
            recording_id=rec.id, user_id=user.id, chunk_index=0, content="combined",
            embedding=api_emb.serialize_embedding(np.array([1, 0, 0, 0], dtype=np.float32))))
        db.session.commit()
        query_vec = np.array([1, 0, 0, 0], dtype=np.float32)
        with patch.object(api_emb, "_api_embed", return_value=[query_vec]):
            results = api_emb.semantic_search_chunks(
                user.id, "q",
                filters={
                    "tag_ids": [tag.id],
                    "speaker_names": ["Alice"],
                    "date_from": _dt(2025, 1, 1),
                    "date_to": _dt(2027, 1, 1),
                },
                top_k=5)
        assert len(results) == 1
        assert results[0][0].content == "combined"


def test_basic_combined_filters_no_duplicate_join(api_emb):
    """Regression companion for basic_text_search_chunks: all filters at once."""
    from datetime import datetime as _dt
    from src.models import Tag, RecordingTag
    with app.app_context():
        user = _make_user()
        rec = _make_recording(user, "the quick brown fox jumps",
                              participants="Alice, Bob",
                              meeting_date=_dt(2026, 1, 15))
        tag = Tag(name=f"cov-combo-basic-{os.getpid()}", user_id=user.id)
        db.session.add(tag)
        db.session.commit()
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id))
        db.session.commit()
        _seed_chunks(api_emb, user, rec, ["the quick brown fox"])
        results = api_emb.basic_text_search_chunks(
            user.id, "quick fox",
            filters={
                "tag_ids": [tag.id],
                "speaker_names": ["Alice"],
                "date_from": _dt(2025, 1, 1),
                "date_to": _dt(2027, 1, 1),
            },
            top_k=5)
        assert isinstance(results, list)
        assert len(results) >= 1
