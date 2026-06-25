"""Coverage-focused unit tests for src/audio_chunking.py.

These exercise the pure-logic surface of the chunking service (config
resolution, chunk-boundary/step math, transcription merging, speaker-sample
selection, statistics/recommendations, cleanup) plus the ffmpeg/ffprobe
wrappers with subprocess mocked. Everything here is offline and hermetic: no
real media files are produced and no network or binaries are invoked.
"""

import os
import sys
import json
import base64
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import audio_chunking as ac
from src.audio_chunking import (
    AudioChunkingService,
    EffectiveChunkingConfig,
    get_effective_chunking_config,
    get_audio_duration_ffprobe,
    extract_speaker_samples,
    samples_to_data_urls,
    ChunkProcessingError,
    ChunkingNotSupportedError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Env vars that get_effective_chunking_config consults. We clear them before
# each test so the host environment (or conftest defaults) can't leak in.
_CHUNK_ENV_KEYS = [
    "CHUNK_OVERLAP_SECONDS",
    "ENABLE_CHUNKING",
    "CHUNK_LIMIT",
    "CHUNK_SIZE_MB",
]


@pytest.fixture(autouse=True)
def _clean_chunk_env(monkeypatch):
    for key in _CHUNK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def make_specs(**kwargs):
    """A lightweight stand-in for ConnectorSpecifications (duck-typed)."""
    defaults = dict(
        max_file_size_bytes=None,
        max_duration_seconds=None,
        min_duration_for_chunking=None,
        handles_chunking_internally=False,
        requires_chunking_param=False,
        recommended_chunk_seconds=None,
        supported_codecs=None,
        unsupported_codecs=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# get_effective_chunking_config
# ---------------------------------------------------------------------------

def test_config_app_default_no_specs():
    cfg = get_effective_chunking_config(None)
    assert cfg.enabled is True
    assert cfg.mode == "size"
    assert cfg.limit_value == 20.0
    assert cfg.source == "app_default"
    assert cfg.overlap_seconds == 3


def test_config_overlap_seconds_from_env(monkeypatch):
    monkeypatch.setenv("CHUNK_OVERLAP_SECONDS", "7")
    cfg = get_effective_chunking_config(None)
    assert cfg.overlap_seconds == 7


def test_config_disabled_via_env(monkeypatch):
    monkeypatch.setenv("ENABLE_CHUNKING", "false")
    cfg = get_effective_chunking_config(None)
    assert cfg.enabled is False
    assert cfg.mode == "none"
    assert cfg.source == "disabled"


def test_config_user_chunk_limit_mb(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "10MB")
    cfg = get_effective_chunking_config(None)
    assert cfg.mode == "size"
    assert cfg.limit_value == 10.0
    assert cfg.source == "env"


def test_config_user_chunk_limit_seconds(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "1200s")
    cfg = get_effective_chunking_config(None)
    assert cfg.mode == "duration"
    assert cfg.limit_value == 1200.0
    assert cfg.source == "env"


def test_config_user_chunk_limit_minutes(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "20m")
    cfg = get_effective_chunking_config(None)
    assert cfg.mode == "duration"
    assert cfg.limit_value == 20 * 60
    assert cfg.source == "env"


def test_config_user_chunk_limit_invalid(monkeypatch):
    # Non-numeric MB string -> ValueError swallowed, falls through to app default.
    monkeypatch.setenv("CHUNK_LIMIT", "MB")
    cfg = get_effective_chunking_config(None)
    assert cfg.source == "app_default"


def test_config_legacy_chunk_size_mb(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_MB", "15")
    cfg = get_effective_chunking_config(None)
    assert cfg.mode == "size"
    assert cfg.limit_value == 15.0
    assert cfg.source == "env"


def test_config_legacy_chunk_size_mb_invalid(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_MB", "notanumber")
    cfg = get_effective_chunking_config(None)
    assert cfg.source == "app_default"


def test_config_connector_internal_chunking():
    specs = make_specs(handles_chunking_internally=True)
    cfg = get_effective_chunking_config(specs)
    assert cfg.enabled is False
    assert cfg.source == "connector_internal"


def test_config_connector_hard_duration_limit_uses_recommended():
    specs = make_specs(max_duration_seconds=1000, recommended_chunk_seconds=600)
    cfg = get_effective_chunking_config(specs)
    assert cfg.enabled is True
    assert cfg.mode == "duration"
    assert cfg.limit_value == 600
    assert cfg.source == "connector_limit"


def test_config_connector_hard_duration_limit_85pct_fallback():
    # No recommended -> 85% of max_duration.
    specs = make_specs(max_duration_seconds=1000)
    cfg = get_effective_chunking_config(specs)
    assert cfg.mode == "duration"
    assert cfg.limit_value == int(1000 * 0.85)
    assert cfg.source == "connector_limit"


def test_config_connector_and_user_duration_min(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "100s")
    specs = make_specs(max_duration_seconds=1000, recommended_chunk_seconds=600)
    cfg = get_effective_chunking_config(specs)
    # MIN(connector 600, user 100) = 100
    assert cfg.limit_value == 100
    assert cfg.source == "user_and_connector"


def test_config_connector_hard_size_limit():
    # 100MB max -> 80% = 80MB
    specs = make_specs(max_file_size_bytes=100 * 1024 * 1024)
    cfg = get_effective_chunking_config(specs)
    assert cfg.mode == "size"
    assert cfg.limit_value == pytest.approx(80.0)
    assert cfg.source == "connector_limit"


def test_config_connector_and_user_size_min(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "10MB")
    specs = make_specs(max_file_size_bytes=100 * 1024 * 1024)
    cfg = get_effective_chunking_config(specs)
    # MIN(connector 80, user 10) = 10
    assert cfg.limit_value == pytest.approx(10.0)
    assert cfg.source == "user_and_connector"


def test_config_connector_recommended_soft():
    # No hard limits, but a recommended chunk length set.
    specs = make_specs(recommended_chunk_seconds=480)
    cfg = get_effective_chunking_config(specs)
    assert cfg.mode == "duration"
    assert cfg.limit_value == 480
    assert cfg.source == "connector_recommended"


# ---------------------------------------------------------------------------
# get_effective_chunking_config: cfg.enabled assertions
#
# These pin the `enabled` flag of the EffectiveChunkingConfig returned by each
# distinct branch. The mode/limit/source assertions above don't touch `enabled`,
# so a mutated `enabled=` field in these branches would otherwise go unnoticed.
# ---------------------------------------------------------------------------

def test_config_connector_hard_size_limit_enabled():
    # Connector hard size-limit branch (no user override): chunking is REQUIRED,
    # so enabled must be True.
    specs = make_specs(max_file_size_bytes=100 * 1024 * 1024)
    cfg = get_effective_chunking_config(specs)
    assert cfg.enabled is True
    assert cfg.source == "connector_limit"


def test_config_connector_and_user_size_min_enabled(monkeypatch):
    # Same hard size-limit return, now via the MIN(user, connector) path: still
    # a required-chunking branch, enabled must be True.
    monkeypatch.setenv("CHUNK_LIMIT", "10MB")
    specs = make_specs(max_file_size_bytes=100 * 1024 * 1024)
    cfg = get_effective_chunking_config(specs)
    assert cfg.enabled is True
    assert cfg.source == "user_and_connector"


def test_config_connector_recommended_soft_enabled():
    # Connector-recommended (soft, no hard limit) branch: chunking is optional
    # but enabled, so enabled must be True.
    specs = make_specs(recommended_chunk_seconds=480)
    cfg = get_effective_chunking_config(specs)
    assert cfg.enabled is True
    assert cfg.source == "connector_recommended"


def test_config_disabled_branch_enabled_false(monkeypatch):
    # Disabled-via-env branch (ENABLE_CHUNKING=false) returns the disabled config
    # with enabled=False, regardless of connector specs (as long as there are no
    # hard limits and the connector doesn't chunk internally).
    monkeypatch.setenv("ENABLE_CHUNKING", "false")
    specs = make_specs(recommended_chunk_seconds=480)
    cfg = get_effective_chunking_config(specs)
    assert cfg.enabled is False
    assert cfg.mode == "none"
    assert cfg.source == "disabled"


# ---------------------------------------------------------------------------
# AudioChunkingService construction / needs_chunking
# ---------------------------------------------------------------------------

def test_service_init_defaults():
    svc = AudioChunkingService()
    assert svc.max_chunk_size_mb == 20
    assert svc.overlap_seconds == 3
    assert svc.max_chunk_size_bytes == 20 * 1024 * 1024
    assert svc.max_chunk_duration_seconds is None
    assert svc.chunk_stats == []


def test_needs_chunking_disabled_returns_false(monkeypatch):
    monkeypatch.setenv("ENABLE_CHUNKING", "false")
    svc = AudioChunkingService()
    assert svc.needs_chunking("/nonexistent.mp3") is False


def test_needs_chunking_legacy_asr_endpoint(monkeypatch):
    # Default config is enabled, but legacy use_asr_endpoint short-circuits.
    svc = AudioChunkingService()
    assert svc.needs_chunking("/whatever.mp3", use_asr_endpoint=True) is False


def test_needs_chunking_size_mode_over_limit(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "10MB")
    svc = AudioChunkingService()
    with mock.patch("os.path.getsize", return_value=20 * 1024 * 1024):
        assert svc.needs_chunking("/big.mp3") is True


def test_needs_chunking_size_mode_under_limit(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "10MB")
    svc = AudioChunkingService()
    with mock.patch("os.path.getsize", return_value=2 * 1024 * 1024):
        assert svc.needs_chunking("/small.mp3") is False


def test_needs_chunking_duration_mode_over_limit(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "100s")
    svc = AudioChunkingService()
    with mock.patch("os.path.getsize", return_value=1234), \
         mock.patch.object(svc, "get_audio_duration", return_value=200.0):
        assert svc.needs_chunking("/long.mp3") is True


def test_needs_chunking_duration_mode_under_limit(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "100s")
    svc = AudioChunkingService()
    with mock.patch("os.path.getsize", return_value=1234), \
         mock.patch.object(svc, "get_audio_duration", return_value=50.0):
        assert svc.needs_chunking("/short.mp3") is False


def test_needs_chunking_duration_unknown_returns_true(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "100s")
    svc = AudioChunkingService()
    with mock.patch("os.path.getsize", return_value=1234), \
         mock.patch.object(svc, "get_audio_duration", return_value=None):
        assert svc.needs_chunking("/unknown.mp3") is True


def test_needs_chunking_oserror_returns_false(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "10MB")
    svc = AudioChunkingService()
    with mock.patch("os.path.getsize", side_effect=OSError("boom")):
        assert svc.needs_chunking("/gone.mp3") is False


def test_needs_chunking_with_config(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "10MB")
    svc = AudioChunkingService()
    with mock.patch("os.path.getsize", return_value=20 * 1024 * 1024):
        needs, cfg = svc.needs_chunking_with_config("/big.mp3")
    assert needs is True
    assert isinstance(cfg, EffectiveChunkingConfig)
    assert cfg.mode == "size"


# ---------------------------------------------------------------------------
# get_audio_duration (instance) + module helper
# ---------------------------------------------------------------------------

def test_get_audio_duration_success():
    svc = AudioChunkingService()
    with mock.patch("subprocess.run", return_value=completed(stdout="123.45\n")) as m:
        assert svc.get_audio_duration("/a.mp3") == pytest.approx(123.45)
    # ffprobe with the right query was invoked.
    args = m.call_args[0][0]
    assert args[0] == "ffprobe"
    assert "format=duration" in args


def test_get_audio_duration_bad_value():
    svc = AudioChunkingService()
    with mock.patch("subprocess.run", return_value=completed(stdout="notanumber")):
        assert svc.get_audio_duration("/a.mp3") is None


def test_get_audio_duration_called_process_error():
    svc = AudioChunkingService()
    err = subprocess.CalledProcessError(1, ["ffprobe"])
    with mock.patch("subprocess.run", side_effect=err):
        assert svc.get_audio_duration("/a.mp3") is None


def test_get_audio_duration_missing_binary():
    svc = AudioChunkingService()
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert svc.get_audio_duration("/a.mp3") is None


def test_module_get_audio_duration_ffprobe_success():
    with mock.patch("subprocess.run", return_value=completed(stdout="42.0")):
        assert get_audio_duration_ffprobe("/a.mp3") == pytest.approx(42.0)


def test_module_get_audio_duration_ffprobe_failure():
    with mock.patch("subprocess.run", side_effect=Exception("x")):
        assert get_audio_duration_ffprobe("/a.mp3") is None


# ---------------------------------------------------------------------------
# parse_chunk_limit
# ---------------------------------------------------------------------------

def test_parse_chunk_limit_mb(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "25MB")
    assert AudioChunkingService().parse_chunk_limit() == ("size", 25.0)


def test_parse_chunk_limit_seconds(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "900s")
    assert AudioChunkingService().parse_chunk_limit() == ("duration", 900.0)


def test_parse_chunk_limit_minutes(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "15M")
    assert AudioChunkingService().parse_chunk_limit() == ("duration", 900.0)


def test_parse_chunk_limit_legacy(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_MB", "18")
    assert AudioChunkingService().parse_chunk_limit() == ("size", 18.0)


def test_parse_chunk_limit_default(monkeypatch):
    # Nothing set -> default 20MB.
    assert AudioChunkingService().parse_chunk_limit() == ("size", 20.0)


def test_parse_chunk_limit_legacy_invalid(monkeypatch):
    monkeypatch.setenv("CHUNK_SIZE_MB", "bad")
    assert AudioChunkingService().parse_chunk_limit() == ("size", 20.0)


# ---------------------------------------------------------------------------
# calculate_optimal_chunking
# ---------------------------------------------------------------------------

def test_calc_chunking_single_chunk_size(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "20MB")
    svc = AudioChunkingService()
    # 5MB file, 600s -> 1 chunk.
    num, dur = svc.calculate_optimal_chunking(5 * 1024 * 1024, 600.0)
    assert num == 1
    assert dur == pytest.approx(600.0)


def test_calc_chunking_multiple_chunks_size(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "10MB")
    svc = AudioChunkingService()
    # 100MB, 3600s. max_size = 10*0.95 = 9.5MB -> ceil(100/9.5)=11 chunks.
    num, dur = svc.calculate_optimal_chunking(100 * 1024 * 1024, 3600.0)
    assert num == 11
    # chunk_duration = 3600/11 ~ 327, clamped to min 300.
    assert dur == pytest.approx(max(300, 3600.0 / 11))


def test_calc_chunking_duration_mode(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "600s")
    svc = AudioChunkingService()
    # 50MB, 1800s -> ceil(1800/600)=3 chunks.
    num, dur = svc.calculate_optimal_chunking(50 * 1024 * 1024, 1800.0)
    assert num == 3
    assert dur == pytest.approx(600.0)


def test_calc_chunking_min_duration_clamp(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "30s")
    svc = AudioChunkingService()
    # Many small chunks would give <300s; clamp to min(300, total).
    num, dur = svc.calculate_optimal_chunking(10 * 1024 * 1024, 120.0)
    # total < 300, so chunk_duration clamps to total_duration (120).
    assert dur == pytest.approx(120.0)


def test_calc_chunking_exception_fallback(monkeypatch):
    monkeypatch.setenv("CHUNK_LIMIT", "600s")
    svc = AudioChunkingService()
    # Force the config lookup inside the method to raise.
    with mock.patch.object(ac, "get_effective_chunking_config", side_effect=RuntimeError("boom")):
        num, dur = svc.calculate_optimal_chunking(50 * 1024 * 1024, 1800.0)
    # Fallback: 10-minute chunks, min 2.
    assert num == max(2, -(-1800 // 600))
    assert dur == pytest.approx(1800.0 / num)


# ---------------------------------------------------------------------------
# convert_to_mp3_and_get_info
# ---------------------------------------------------------------------------

def test_convert_already_mp3_copies(tmp_path, monkeypatch):
    src = tmp_path / "in.mp3"
    src.write_bytes(b"x" * 100)
    svc = AudioChunkingService()
    monkeypatch.setattr(svc, "get_audio_duration", lambda p: 60.0)
    path, dur, size = svc.convert_to_mp3_and_get_info(str(src), str(tmp_path))
    assert path.endswith("_converted.mp3")
    assert os.path.exists(path)
    assert dur == 60.0
    assert size == 100


def test_convert_non_mp3_calls_convert(tmp_path, monkeypatch):
    src = tmp_path / "in.wav"
    src.write_bytes(b"wavdata")
    svc = AudioChunkingService()
    monkeypatch.setattr(svc, "get_audio_duration", lambda p: 30.0)

    def fake_convert(in_path, out_path):
        with open(out_path, "wb") as f:
            f.write(b"mp3data")

    with mock.patch.object(ac, "convert_to_mp3", side_effect=fake_convert) as m:
        path, dur, size = svc.convert_to_mp3_and_get_info(str(src), str(tmp_path))
    m.assert_called_once()
    assert dur == 30.0
    assert size == len(b"mp3data")


def test_convert_missing_output_raises(tmp_path, monkeypatch):
    src = tmp_path / "in.wav"
    src.write_bytes(b"data")
    svc = AudioChunkingService()
    with mock.patch.object(ac, "convert_to_mp3", side_effect=lambda i, o: None):
        with pytest.raises(ValueError, match="MP3 file was not created"):
            svc.convert_to_mp3_and_get_info(str(src), str(tmp_path))


def test_convert_no_duration_raises(tmp_path, monkeypatch):
    src = tmp_path / "in.mp3"
    src.write_bytes(b"abc")
    svc = AudioChunkingService()
    monkeypatch.setattr(svc, "get_audio_duration", lambda p: None)
    with pytest.raises(ValueError, match="Could not determine MP3 file duration"):
        svc.convert_to_mp3_and_get_info(str(src), str(tmp_path))


def test_convert_ffmpeg_error_propagates(tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(b"data")
    svc = AudioChunkingService()
    with mock.patch.object(ac, "convert_to_mp3", side_effect=ac.FFmpegError("bad")):
        with pytest.raises(ac.FFmpegError):
            svc.convert_to_mp3_and_get_info(str(src), str(tmp_path))


# ---------------------------------------------------------------------------
# create_chunks
# ---------------------------------------------------------------------------

def test_create_chunks_single(tmp_path, monkeypatch):
    src = tmp_path / "in.mp3"
    src.write_bytes(b"x" * 50)
    svc = AudioChunkingService()
    # Patch conversion to a known mp3 in temp_dir, and force single chunk.
    mp3 = tmp_path / "conv.mp3"
    mp3.write_bytes(b"y" * 50)
    monkeypatch.setattr(svc, "convert_to_mp3_and_get_info", lambda f, d: (str(mp3), 100.0, 50))
    monkeypatch.setattr(svc, "calculate_optimal_chunking", lambda s, d, c=None: (1, 100.0))

    chunks = svc.create_chunks(str(src), str(tmp_path))
    assert len(chunks) == 1
    assert chunks[0]["index"] == 0
    assert chunks[0]["start_time"] == 0
    assert chunks[0]["end_time"] == 100.0
    assert os.path.exists(chunks[0]["path"])


def test_create_chunks_multiple(tmp_path, monkeypatch):
    src = tmp_path / "in.mp3"
    src.write_bytes(b"x" * 50)
    mp3 = tmp_path / "conv.mp3"
    mp3.write_bytes(b"y" * 50)
    svc = AudioChunkingService()
    monkeypatch.setattr(svc, "convert_to_mp3_and_get_info", lambda f, d: (str(mp3), 1200.0, 50))
    monkeypatch.setattr(svc, "calculate_optimal_chunking", lambda s, d, c=None: (3, 420.0))

    # Mock ffmpeg: create the output chunk file so os.path.exists/getsize work.
    def fake_run(cmd, **kwargs):
        out = cmd[-1]
        with open(out, "wb") as f:
            f.write(b"chunkdata")
        return completed(returncode=0)

    with mock.patch("subprocess.run", side_effect=fake_run):
        chunks = svc.create_chunks(str(src), str(tmp_path))

    assert len(chunks) == 3
    assert [c["index"] for c in chunks] == [0, 1, 2]
    # Boundaries are monotonic / cover the file.
    assert chunks[0]["start_time"] == 0
    assert chunks[-1]["end_time"] == pytest.approx(1200.0)


def test_create_chunks_ffmpeg_failure_skips(tmp_path, monkeypatch):
    src = tmp_path / "in.mp3"
    src.write_bytes(b"x" * 50)
    mp3 = tmp_path / "conv.mp3"
    mp3.write_bytes(b"y" * 50)
    svc = AudioChunkingService()
    monkeypatch.setattr(svc, "convert_to_mp3_and_get_info", lambda f, d: (str(mp3), 1200.0, 50))
    monkeypatch.setattr(svc, "calculate_optimal_chunking", lambda s, d, c=None: (3, 420.0))

    # All ffmpeg calls fail -> all chunks skipped, empty list returned.
    with mock.patch("subprocess.run", return_value=completed(returncode=1, stderr="boom")):
        chunks = svc.create_chunks(str(src), str(tmp_path))
    assert chunks == []


def test_create_chunks_conversion_error_propagates(tmp_path, monkeypatch):
    src = tmp_path / "in.mp3"
    src.write_bytes(b"x")
    svc = AudioChunkingService()
    monkeypatch.setattr(svc, "convert_to_mp3_and_get_info",
                        lambda f, d: (_ for _ in ()).throw(RuntimeError("convfail")))
    with pytest.raises(RuntimeError, match="convfail"):
        svc.create_chunks(str(src), str(tmp_path))


# ---------------------------------------------------------------------------
# merge_transcriptions + helpers
# ---------------------------------------------------------------------------

def test_merge_empty():
    assert AudioChunkingService().merge_transcriptions([]) == ""


def test_merge_single():
    svc = AudioChunkingService()
    assert svc.merge_transcriptions([{"transcription": "hello world"}]) == "hello world"


def test_merge_no_overlap_concatenates():
    svc = AudioChunkingService()
    chunks = [
        {"transcription": "First chunk text.", "start_time": 0, "end_time": 10},
        {"transcription": "Second chunk text.", "start_time": 20, "end_time": 30},
    ]
    merged = svc.merge_transcriptions(chunks)
    assert "First chunk text." in merged
    assert "Second chunk text." in merged


def test_merge_skips_empty_chunk_text():
    svc = AudioChunkingService()
    chunks = [
        {"transcription": "Only real text.", "start_time": 0, "end_time": 10},
        {"transcription": "   ", "start_time": 10, "end_time": 20},
    ]
    merged = svc.merge_transcriptions(chunks)
    assert merged == "Only real text."


def test_merge_sorts_by_start_time():
    svc = AudioChunkingService()
    chunks = [
        {"transcription": "Beta.", "start_time": 100, "end_time": 110},
        {"transcription": "Alpha.", "start_time": 0, "end_time": 10},
    ]
    merged = svc.merge_transcriptions(chunks)
    assert merged.index("Alpha") < merged.index("Beta")


def test_merge_overlapping_dedupes_common_sentence():
    svc = AudioChunkingService()
    # prev_end (15) > new_start (10) -> overlap window. The shared sentence
    # "the quick brown fox jumps" should be collapsed, not duplicated.
    shared = "the quick brown fox jumps over the lazy dog"
    chunks = [
        {"transcription": f"intro sentence here. {shared}.", "start_time": 0, "end_time": 15},
        {"transcription": f"{shared}. closing remarks follow.", "start_time": 10, "end_time": 25},
    ]
    merged = svc.merge_transcriptions(chunks)
    assert merged.lower().count(shared) == 1
    assert "closing remarks follow" in merged.lower()


def test_merge_overlap_no_match_concatenates():
    svc = AudioChunkingService()
    chunks = [
        {"transcription": "totally unrelated alpha beta.", "start_time": 0, "end_time": 15},
        {"transcription": "completely different gamma delta.", "start_time": 10, "end_time": 25},
    ]
    merged = svc.merge_transcriptions(chunks)
    assert "alpha beta" in merged.lower()
    assert "gamma delta" in merged.lower()


def test_split_into_sentences():
    svc = AudioChunkingService()
    out = svc._split_into_sentences("One. Two! Three?  ")
    assert out == ["One", "Two", "Three"]


def test_sentences_similar_true_and_false():
    svc = AudioChunkingService()
    assert svc._sentences_similar("the cat sat", "the cat sat") is True
    assert svc._sentences_similar("the cat sat", "a dog ran fast") is False


def test_sentences_similar_empty():
    svc = AudioChunkingService()
    assert svc._sentences_similar("", "anything") is False


def test_merge_overlapping_text_empty_sentences():
    svc = AudioChunkingService()
    # new_text has no sentence-y content -> falls back to concatenation branch.
    out = svc._merge_overlapping_text("existing text.", "   ", new_start_time=5, prev_end_time=10)
    assert "existing text." in out


# ---------------------------------------------------------------------------
# analyze_chunk_audio_properties
# ---------------------------------------------------------------------------

def _probe_json():
    return json.dumps({
        "format": {"duration": "120.0", "size": "1920000", "bit_rate": "128000"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {
                "codec_type": "audio",
                "codec_name": "mp3",
                "sample_rate": "44100",
                "channels": "2",
                "bits_per_raw_sample": "0",
            },
        ],
    })


def test_analyze_chunk_audio_properties_success():
    svc = AudioChunkingService()
    with mock.patch("subprocess.run", return_value=completed(stdout=_probe_json())):
        analysis = svc.analyze_chunk_audio_properties("/c.mp3")
    assert analysis["duration"] == pytest.approx(120.0)
    assert analysis["codec"] == "mp3"
    assert analysis["sample_rate"] == 44100
    assert analysis["channels"] == 2
    assert "effective_bitrate" in analysis
    assert "compression_ratio" in analysis


def test_analyze_chunk_no_audio_stream():
    svc = AudioChunkingService()
    data = json.dumps({"format": {}, "streams": [{"codec_type": "video"}]})
    with mock.patch("subprocess.run", return_value=completed(stdout=data)):
        analysis = svc.analyze_chunk_audio_properties("/c.mp3")
    assert analysis == {"error": "No audio stream found"}


def test_analyze_chunk_ffprobe_error():
    svc = AudioChunkingService()
    with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, ["ffprobe"])):
        analysis = svc.analyze_chunk_audio_properties("/c.mp3")
    assert "error" in analysis


# ---------------------------------------------------------------------------
# log_processing_statistics / get_performance_recommendations
# ---------------------------------------------------------------------------

def test_log_processing_statistics_empty():
    # No exception, no output assertions; just exercise the early-return path.
    AudioChunkingService().log_processing_statistics([])


def test_log_processing_statistics_with_outliers():
    svc = AudioChunkingService()
    results = [
        {"processing_time": 10, "size_mb": 15, "duration": 300},
        {"processing_time": 12, "size_mb": 16, "duration": 300},
        {"processing_time": 90, "size_mb": 18, "duration": 300},  # outlier
    ]
    with mock.patch.object(ac, "logger") as mock_logger:
        ret = svc.log_processing_statistics(results)
    assert ret is None
    # The 90s outlier (vs ~37s avg) must trip the performance-outlier warning
    # branch — proving the outlier path actually ran, not just "didn't crash".
    assert mock_logger.warning.called
    warned = " ".join(str(c.args[0]) for c in mock_logger.warning.call_args_list)
    assert "outlier" in warned.lower()


def test_get_performance_recommendations_empty():
    assert AudioChunkingService().get_performance_recommendations([]) == []


def test_get_performance_recommendations_all_branches():
    svc = AudioChunkingService()
    # High variance (max > avg*3) + slow + timeout + tiny chunks all triggered.
    results = [
        {"processing_time": 1, "duration": 5, "size_mb": 5},
        {"processing_time": 1, "duration": 5, "size_mb": 5},
        {"processing_time": 1, "duration": 5, "size_mb": 5},
        {"processing_time": 400, "duration": 5, "size_mb": 5},
    ]
    recs = svc.get_performance_recommendations(results)
    assert any("variance" in r.lower() for r in recs)
    assert any("slow" in r.lower() for r in recs)
    assert any("5 minutes" in r for r in recs)
    assert any("small" in r.lower() for r in recs)


def test_get_performance_recommendations_large_chunks():
    svc = AudioChunkingService()
    results = [
        {"processing_time": 10, "duration": 100, "size_mb": 24},
        {"processing_time": 11, "duration": 100, "size_mb": 23},
    ]
    recs = svc.get_performance_recommendations(results)
    assert any("size limit" in r.lower() for r in recs)


# ---------------------------------------------------------------------------
# cleanup_chunks
# ---------------------------------------------------------------------------

def test_cleanup_chunks_removes_files(tmp_path):
    f1 = tmp_path / "c0.mp3"
    f2 = tmp_path / "c1.mp3"
    mp3 = tmp_path / "conv.mp3"
    for f in (f1, f2, mp3):
        f.write_bytes(b"x")
    svc = AudioChunkingService()
    chunks = [{"path": str(f1), "filename": "c0.mp3"}, {"path": str(f2), "filename": "c1.mp3"}]
    svc.cleanup_chunks(chunks, temp_mp3_path=str(mp3))
    assert not f1.exists()
    assert not f2.exists()
    assert not mp3.exists()


def test_cleanup_chunks_missing_files_no_error(tmp_path):
    svc = AudioChunkingService()
    chunks = [{"path": str(tmp_path / "gone.mp3"), "filename": "gone.mp3"}]
    # Should not raise even though file doesn't exist.
    with mock.patch("os.remove") as mock_remove:
        ret = svc.cleanup_chunks(chunks, temp_mp3_path=str(tmp_path / "alsogone.mp3"))
    assert ret is None
    # Neither the (missing) chunk nor the (missing) temp mp3 exists, so removal
    # must never be attempted.
    mock_remove.assert_not_called()


def test_cleanup_chunks_remove_error_swallowed(tmp_path):
    f1 = tmp_path / "c0.mp3"
    f1.write_bytes(b"x")
    svc = AudioChunkingService()
    chunks = [{"path": str(f1), "filename": "c0.mp3"}]
    with mock.patch("os.remove", side_effect=OSError("denied")) as mock_remove:
        ret = svc.cleanup_chunks(chunks)  # warning logged, no raise
    assert ret is None
    # The file exists, so removal WAS attempted; the OSError was swallowed.
    mock_remove.assert_called_once_with(str(f1))


# ---------------------------------------------------------------------------
# extract_speaker_samples
# ---------------------------------------------------------------------------

def test_extract_speaker_samples_no_valid_segments(tmp_path):
    # All segments Unknown / missing times -> empty result.
    segments = [
        {"speaker": "Unknown", "start_time": 0, "end_time": 5},
        {"speaker": "A", "start_time": None, "end_time": 5},
    ]
    out = extract_speaker_samples(str(tmp_path / "a.mp3"), segments, str(tmp_path))
    assert out == {}


def test_extract_speaker_samples_success(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    # Use non-zero start times: the code reads `start_time or start`, so 0.0 is
    # treated as missing.
    segments = [
        {"speaker": "A", "start_time": 1.0, "end_time": 6.0},
        {"speaker": "B", "start_time": 7.0, "end_time": 12.0},
    ]

    def fake_run(cmd, **kwargs):
        out = cmd[-1]
        with open(out, "wb") as f:
            f.write(b"sample")
        return completed(returncode=0)

    with mock.patch("subprocess.run", side_effect=fake_run), \
         mock.patch.object(ac, "get_audio_duration_ffprobe", return_value=5.0):
        out = extract_speaker_samples(str(audio), segments, str(tmp_path))

    assert set(out.keys()) == {"A", "B"}
    for p in out.values():
        assert os.path.exists(p)


def test_extract_speaker_samples_object_segments(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    seg = SimpleNamespace(speaker="A", start_time=1.0, end_time=5.0, start=None, end=None)

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"sample")
        return completed(returncode=0)

    with mock.patch("subprocess.run", side_effect=fake_run), \
         mock.patch.object(ac, "get_audio_duration_ffprobe", return_value=4.0):
        out = extract_speaker_samples(str(audio), [seg], str(tmp_path))
    assert "A" in out


def test_extract_speaker_samples_too_short_skipped(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    segments = [{"speaker": "A", "start_time": 1.0, "end_time": 6.0}]

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"sample")
        return completed(returncode=0)

    # Actual duration below OpenAI minimum (1.2s) -> sample removed, skipped.
    with mock.patch("subprocess.run", side_effect=fake_run), \
         mock.patch.object(ac, "get_audio_duration_ffprobe", return_value=0.5):
        out = extract_speaker_samples(str(audio), segments, str(tmp_path))
    assert out == {}


def test_extract_speaker_samples_ffmpeg_failure(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    segments = [{"speaker": "A", "start_time": 1.0, "end_time": 6.0}]
    with mock.patch("subprocess.run", return_value=completed(returncode=1, stderr="fail")):
        out = extract_speaker_samples(str(audio), segments, str(tmp_path))
    assert out == {}


def test_extract_speaker_samples_combines_short_segments(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    # Two short adjacent segments that combine to >= min_duration (1.5).
    segments = [
        {"speaker": "A", "start_time": 0.5, "end_time": 1.1},
        {"speaker": "A", "start_time": 1.3, "end_time": 2.3},
    ]

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"sample")
        return completed(returncode=0)

    with mock.patch("subprocess.run", side_effect=fake_run), \
         mock.patch.object(ac, "get_audio_duration_ffprobe", return_value=2.0):
        out = extract_speaker_samples(str(audio), segments, str(tmp_path))
    assert "A" in out


def test_extract_speaker_samples_max_speakers(tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    segments = [
        {"speaker": s, "start_time": 1.0, "end_time": 6.0}
        for s in ["A", "B", "C", "D", "E", "F"]
    ]

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"sample")
        return completed(returncode=0)

    with mock.patch("subprocess.run", side_effect=fake_run), \
         mock.patch.object(ac, "get_audio_duration_ffprobe", return_value=5.0):
        out = extract_speaker_samples(str(audio), segments, str(tmp_path), max_speakers=2)
    assert len(out) == 2
    assert set(out.keys()) == {"A", "B"}


# ---------------------------------------------------------------------------
# samples_to_data_urls
# ---------------------------------------------------------------------------

def test_samples_to_data_urls_success(tmp_path):
    f = tmp_path / "s.mp3"
    f.write_bytes(b"hello")
    out = samples_to_data_urls({"A": str(f)})
    expected = "data:audio/mpeg;base64," + base64.b64encode(b"hello").decode()
    assert out["A"] == expected


def test_samples_to_data_urls_missing_file_skipped(tmp_path):
    out = samples_to_data_urls({"A": str(tmp_path / "missing.mp3")})
    assert out == {}


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------

def test_exception_classes():
    assert issubclass(ChunkProcessingError, Exception)
    assert issubclass(ChunkingNotSupportedError, Exception)
    with pytest.raises(ChunkProcessingError):
        raise ChunkProcessingError("x")
    with pytest.raises(ChunkingNotSupportedError):
        raise ChunkingNotSupportedError("y")


def test_extract_speaker_samples_zero_start_not_dropped(tmp_path):
    """Regression: a segment starting at exactly 0.0 must be kept. The code
    previously read ``start_time or start``, so a literal 0.0 start was treated
    as missing and the speaker whose only segment began at 0s was dropped."""
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"audio")
    segments = [
        {"speaker": "A", "start_time": 0.0, "end_time": 6.0},
        {"speaker": "B", "start_time": 7.0, "end_time": 13.0},
    ]

    def fake_run(cmd, **kwargs):
        with open(cmd[-1], "wb") as f:
            f.write(b"sample")
        return completed(returncode=0)

    with mock.patch("subprocess.run", side_effect=fake_run), \
         mock.patch.object(ac, "get_audio_duration_ffprobe", return_value=4.0):
        out = extract_speaker_samples(str(audio), segments, str(tmp_path))
    assert "A" in out  # 0.0-start speaker no longer dropped
    assert "B" in out
