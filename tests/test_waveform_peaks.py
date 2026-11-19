"""Waveform peaks for the timeline of the segment editor.

src/services/waveform_peaks.py turns a recording's audio into min/max pairs
at several resolutions; GET /audio/<id>/peaks serves them.

Covered here:
  - the shape of the peaks of a generated WAV file (silence, a loud part, a
    half-loud part), decoded by the real ffmpeg, and the coarser levels;
  - the coarser base level of a very long recording;
  - who gets them: the owner, a user the recording is shared with while
    internal sharing is on, and nobody else (the rule of GET /audio/<id>);
  - caching: a second request does not compute again;
  - invalidation: another audio file is computed again and the old peaks
    file is removed;
  - a long computation: 202 until the file is there, computed once;
  - audio that cannot be decoded, removed audio, and a deleted recording;
  - an UPLOAD_FOLDER that cannot be written: 204, one warning, no decoding.
"""

import math
import os
import shutil
import struct
import tempfile
import threading
import time
import uuid
import wave
from unittest.mock import patch

import numpy as np
import pytest

import src.api.recordings as rec_module
import src.services.waveform_peaks as peaks
from src.app import app, db
from src.models import InternalShare, Recording, User

app.config["WTF_CSRF_ENABLED"] = False

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")

_PREFIX = "peaks"
RATE = 8000
# seconds, amplitude (of 32767): silence, loud, half as loud, silence
PARTS = ((5, 0.0), (10, 0.9), (10, 0.45), (5, 0.0))
SECONDS = sum(seconds for seconds, _ in PARTS)


def write_wav(path, parts=PARTS, rate=RATE):
    frames = bytearray()
    n = 0
    for seconds, amplitude in parts:
        for _ in range(seconds * rate):
            frames += struct.pack("<h", int(32767 * amplitude * math.sin(2 * math.pi * 220 * n / rate)))
            n += 1
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


# ---- the computation ---------------------------------------------------------

@needs_ffmpeg
def test_peaks_of_a_generated_wav_have_the_shape_of_the_audio(tmp_path):
    audio = str(tmp_path / "shape.wav")
    write_wav(audio)
    decoded = peaks.decode_peaks(peaks.compute_peaks(audio))

    assert decoded["duration"] == pytest.approx(SECONDS, abs=0.05)
    base = decoded["levels"][0]
    assert base["rate"] == 100
    assert len(base["min"]) == len(base["max"]) == pytest.approx(SECONDS * 100, abs=2)

    def part(level, start, end):
        a, b = int(start * level["rate"]) + 1, int(end * level["rate"]) - 1
        return level["min"][a:b], level["max"][a:b]

    low, high = part(base, 0, 5)
    assert np.abs(low).max() <= 1 and np.abs(high).max() <= 1            # silence
    low, high = part(base, 5, 15)
    assert high.min() >= 120 and low.max() <= -120                       # the loudest part fills the range
    assert high.max() == 127
    low, high = part(base, 15, 25)
    assert 58 <= high.min() and high.max() <= 68                         # half as loud
    assert -68 <= low.min() and low.max() <= -58
    low, high = part(base, 25, 30)
    assert np.abs(low).max() <= 1 and np.abs(high).max() <= 1

    # Coarser levels: a quarter of the buckets each, the extremes of their four
    assert [level["rate"] for level in decoded["levels"]] == [100, 25]
    coarse = decoded["levels"][1]
    assert len(coarse["max"]) == math.ceil(len(base["max"]) / 4)
    for i in (0, 200, 500, len(coarse["max"]) - 2):
        assert coarse["max"][i] == base["max"][i * 4:i * 4 + 4].max()
        assert coarse["min"][i] == base["min"][i * 4:i * 4 + 4].min()
    low, high = part(coarse, 5, 15)
    assert high.min() >= 120 and low.max() <= -120


def test_pcm_is_reduced_in_chunks_of_any_size():
    samples = (np.arange(1000, dtype=np.int16) - 500) * 20
    data = samples.astype("<i2").tobytes()
    whole = peaks.reduce_pcm([data])
    pieces = peaks.reduce_pcm([data[:7], data[7:333], b"", data[333:1500], data[1500:]])
    assert np.array_equal(whole[0], pieces[0]) and np.array_equal(whole[1], pieces[1])
    step = peaks.SAMPLES_PER_BUCKET
    assert len(whole[0]) == math.ceil(1000 / step)                       # the last, shorter bucket is kept
    assert whole[0][0] == samples[:step].min() and whole[1][0] == samples[:step].max()
    assert whole[1][-1] == samples[-1]


def test_a_very_long_recording_gets_a_coarser_base_level():
    count = 10_000
    mins = np.full(count, -1000, dtype=np.int16)
    maxs = np.full(count, 1000, dtype=np.int16)
    maxs[5000] = 20000
    with patch.object(peaks, "MAX_BASE_BUCKETS", 3000):
        levels = peaks.build_levels(mins, maxs)
    rate, low, high = levels[0]
    assert rate == 25 and len(high) == 2500                              # every four buckets joined
    assert high.max() == 127 and high[1250] == 127                       # scaled to the loudest sample
    assert [len(level[1]) for level in levels] == [2500, 625]
    decoded = peaks.decode_peaks(peaks.encode_peaks(levels, 100.0))
    assert decoded["duration"] == 100.0
    assert np.array_equal(decoded["levels"][1]["max"], levels[1][2])


def test_silence_is_not_scaled_up():
    zeros = np.zeros(500, dtype=np.int16)
    (_, low, high), = peaks.build_levels(zeros, zeros)
    assert not low.any() and not high.any()
    faint = np.full(500, 20, dtype=np.int16)
    (_, low, high), = peaks.build_levels(-faint, faint)
    assert high.max() <= 6                                               # the gain is limited


# ---- the route ----------------------------------------------------------------

def _user():
    suffix = uuid.uuid4().hex[:9]
    user = User(username=f"{_PREFIX}_{suffix}", email=f"{_PREFIX}_{suffix}@test.local", password="x")
    db.session.add(user)
    db.session.commit()
    return user.id


def _client(user_id=None):
    client = app.test_client()
    if user_id is not None:
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user_id)
            sess["_fresh"] = True
    return client


def _cache_files(recording_id):
    folder = peaks.cache_dir(app.config["UPLOAD_FOLDER"])
    if not os.path.isdir(folder):
        return []
    return sorted(name for name in os.listdir(folder) if name.startswith(f"{recording_id}-"))


def _wait_for_ready(client, url, seconds=20):
    deadline = time.monotonic() + seconds
    while True:
        response = client.get(url)
        if response.status_code != 202 or time.monotonic() > deadline:
            return response
        time.sleep(0.1)


@pytest.fixture
def scene():
    """An owner with a recording of a real WAV file, and another user."""
    folder = tempfile.mkdtemp(prefix="speakr_peaks_")
    audio = os.path.join(folder, "audio.wav")
    write_wav(audio)
    with app.app_context():
        owner, other = _user(), _user()
        recording = Recording(user_id=owner, title="peaks", status="COMPLETED", audio_path=audio,
                              original_filename="audio.wav", mime_type="audio/wav")
        db.session.add(recording)
        db.session.commit()
        recording_id = recording.id
    yield {"owner": owner, "other": other, "id": recording_id, "audio": audio, "url": f"/audio/{recording_id}/peaks"}
    with app.app_context():
        InternalShare.query.filter_by(recording_id=recording_id).delete()
        recording = db.session.get(Recording, recording_id)
        if recording is not None:
            db.session.delete(recording)
        for user_id in (owner, other):
            db.session.delete(db.session.get(User, user_id))
        db.session.commit()
    peaks.remove_peaks(app.config["UPLOAD_FOLDER"], recording_id)
    shutil.rmtree(folder, ignore_errors=True)


@needs_ffmpeg
def test_the_owner_gets_the_peaks(scene):
    response = _wait_for_ready(_client(scene["owner"]), scene["url"])
    assert response.status_code == 200
    assert response.mimetype == "application/octet-stream"
    assert "private" in response.headers["Cache-Control"]
    decoded = peaks.decode_peaks(response.data)
    assert decoded["duration"] == pytest.approx(SECONDS, abs=0.05)
    assert decoded["levels"][0]["max"][1000] >= 120
    assert len(_cache_files(scene["id"])) == 1


@needs_ffmpeg
def test_nobody_else_gets_them(scene):
    # Not signed in: the login redirect of every page, never the data
    anonymous = _client().get(scene["url"])
    assert anonymous.status_code in (302, 401)
    assert not anonymous.data.startswith(peaks.MAGIC)

    # Another user: the same answer as for a recording that does not exist
    other = _client(scene["other"])
    denied = other.get(scene["url"])
    missing = other.get("/audio/99999999/peaks")
    assert denied.status_code == missing.status_code == 404
    assert denied.get_json() == missing.get_json()
    # Nothing was computed for a user who may not play the audio
    assert _cache_files(scene["id"]) == []

    # Also not when the owner's peaks are already cached
    assert _wait_for_ready(_client(scene["owner"]), scene["url"]).status_code == 200
    assert other.get(scene["url"]).status_code == 404


@needs_ffmpeg
def test_a_shared_recording_follows_the_rule_of_the_audio_route(scene):
    with app.app_context():
        db.session.add(InternalShare(recording_id=scene["id"], owner_id=scene["owner"],
                                     shared_with_user_id=scene["other"], can_edit=False))
        db.session.commit()
    other = _client(scene["other"])
    with patch("src.app.ENABLE_INTERNAL_SHARING", True), patch.object(rec_module, "ENABLE_INTERNAL_SHARING", True):
        assert other.get(f"/audio/{scene['id']}").status_code == 200
        assert _wait_for_ready(other, scene["url"]).status_code == 200
    with patch("src.app.ENABLE_INTERNAL_SHARING", False), patch.object(rec_module, "ENABLE_INTERNAL_SHARING", False):
        assert other.get(f"/audio/{scene['id']}").status_code == 404
        assert other.get(scene["url"]).status_code == 404


@needs_ffmpeg
def test_peaks_are_computed_once_and_again_when_the_audio_changes(scene):
    client = _client(scene["owner"])
    real = peaks.compute_peaks
    calls = []

    def counted(path):
        calls.append(path)
        return real(path)

    with patch.object(peaks, "compute_peaks", side_effect=counted):
        first = _wait_for_ready(client, scene["url"])
        assert first.status_code == 200 and len(calls) == 1
        again = client.get(scene["url"])
        assert again.status_code == 200 and again.data == first.data
        assert len(calls) == 1, "the cached file was not used"
        # The browser's copy is confirmed without sending the file
        etag = first.headers.get("ETag")
        assert etag and client.get(scene["url"], headers={"If-None-Match": etag}).status_code == 304
        before = _cache_files(scene["id"])
        assert len(before) == 1

        # Another audio file under the same path: computed again, the old file removed
        write_wav(scene["audio"], parts=((2, 0.5), (2, 0.0)))
        changed = _wait_for_ready(client, scene["url"])
        assert changed.status_code == 200 and len(calls) == 2
        assert peaks.decode_peaks(changed.data)["duration"] == pytest.approx(4, abs=0.05)
        after = _cache_files(scene["id"])
        assert len(after) == 1 and after != before
        assert changed.headers.get("ETag") != etag


def test_a_long_computation_answers_202_and_runs_once(scene):
    client = _client(scene["owner"])
    release = threading.Event()
    calls = []

    def slow(path):
        calls.append(path)
        release.wait(20)
        return peaks.encode_peaks(peaks.build_levels(np.array([-5], dtype=np.int16), np.array([5], dtype=np.int16)), 0.01)

    with patch.object(peaks, "compute_peaks", side_effect=slow), patch.object(peaks, "QUICK_WAIT_SECONDS", 0.05):
        try:
            first = client.get(scene["url"])
            assert first.status_code == 202
            assert first.get_json() == {"status": "pending", "retry_after": 2}
            assert first.headers["Cache-Control"] == "no-store"
            assert client.get(scene["url"]).status_code == 202
            assert len(calls) == 1, "a second request started a second computation"
        finally:
            release.set()
        done = _wait_for_ready(client, scene["url"])
        assert done.status_code == 200 and done.data.startswith(peaks.MAGIC)
        assert len(calls) == 1
    # The file is published before the worker removes its lock in its
    # finally block, so a 200 can arrive first: wait for the worker to end.
    for worker in [t for t in threading.enumerate() if t.name.startswith("peaks-")]:
        worker.join(5)
    assert not any(name.endswith(".lock") for name in _cache_files(scene["id"]))


@needs_ffmpeg
def test_audio_that_cannot_be_decoded_has_no_peaks(scene):
    with open(scene["audio"], "wb") as handle:
        handle.write(b"this is not audio" * 50)
    client = _client(scene["owner"])
    response = _wait_for_ready(client, scene["url"])
    assert response.status_code == 204 and response.data == b""
    # Remembered: the next request does not run ffmpeg again
    with patch.object(peaks, "compute_peaks", side_effect=AssertionError("computed again")):
        assert client.get(scene["url"]).status_code == 204


def test_missing_and_removed_audio_have_no_peaks(scene):
    from datetime import datetime
    client = _client(scene["owner"])
    os.remove(scene["audio"])
    assert client.get(scene["url"]).status_code == 204
    with app.app_context():
        db.session.get(Recording, scene["id"]).audio_deleted_at = datetime.utcnow()
        db.session.commit()
    assert client.get(scene["url"]).status_code == 204
    with app.app_context():
        db.session.get(Recording, scene["id"]).audio_path = None
        db.session.commit()
    assert client.get(scene["url"]).status_code == 404


@needs_ffmpeg
def test_deleting_the_recording_removes_its_peaks(scene):
    from src.services.recording_deletion import delete_recording_completely
    assert _wait_for_ready(_client(scene["owner"]), scene["url"]).status_code == 200
    assert len(_cache_files(scene["id"])) == 1
    with app.app_context():
        delete_recording_completely(db.session.get(Recording, scene["id"]))
    assert _cache_files(scene["id"]) == []
    assert not os.path.exists(scene["audio"])


# ---- a cache folder that cannot be written --------------------------------------

def _peaks_warnings(caplog):
    return [r for r in caplog.records if "cache folder" in r.getMessage()]


def test_a_cache_folder_that_cannot_be_created_answers_204_and_warns_once(scene, tmp_path, caplog):
    import logging
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    client = _client(scene["owner"])
    peaks._unwritable_reported.clear()
    with patch.dict(app.config, {"UPLOAD_FOLDER": str(blocker / "uploads")}), \
         patch.object(peaks, "compute_peaks", side_effect=AssertionError("decoded")), \
         caplog.at_level(logging.WARNING):
        for _ in range(3):
            response = client.get(scene["url"])
            assert response.status_code == 204 and response.data == b""
    warnings = _peaks_warnings(caplog)
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert warnings[0].exc_info is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@needs_ffmpeg
def test_a_cache_folder_that_cannot_be_written_answers_204_and_recovers(scene, caplog):
    import builtins
    import logging
    client = _client(scene["owner"])
    peaks._unwritable_reported.clear()

    def read_only(path, mode="r", *args, **kwargs):
        if str(path).endswith(".lock"):
            raise PermissionError(13, "Read-only file system", path)
        return builtins.open(path, mode, *args, **kwargs)

    with patch.object(peaks, "open", read_only, create=True), \
         patch.object(peaks, "compute_peaks", side_effect=AssertionError("decoded")), \
         caplog.at_level(logging.WARNING):
        for _ in range(peaks.MAX_CONCURRENT + 2):       # no computation slot is kept
            assert client.get(scene["url"]).status_code == 204
    assert len(_peaks_warnings(caplog)) == 1
    # Writable again: the peaks are computed as usual.
    assert _wait_for_ready(client, scene["url"]).status_code == 200


@needs_ffmpeg
def test_existing_peaks_are_served_from_a_folder_that_cannot_be_written(scene):
    client = _client(scene["owner"])
    assert _wait_for_ready(client, scene["url"]).status_code == 200
    with patch.object(peaks.os, "makedirs", side_effect=PermissionError(30, "Read-only file system")), \
         patch.object(peaks, "open", side_effect=PermissionError(30, "Read-only file system"), create=True):
        assert client.get(scene["url"]).status_code == 200
