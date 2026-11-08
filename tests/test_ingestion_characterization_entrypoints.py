"""Characterization of the other ingestion entry points (D556 step 0).

The share target, API v1 upload, the ASR Voice Recorder integration, sliced
upload finalization, the file monitor, and direct calls that exercise the
options only callers pass (prepare, on_conflict, reuse_duplicate,
enforce_upload_limits, success_status, source). Each case is a
representative subset of the POST /upload matrix in
test_ingestion_characterization_upload.py, chosen for what the entry point
does on its own. Expected observations are pinned in
tests/fixtures/ingestion_characterization.json.

SHARED-DB: every case uses its own owner and removes what it created.
"""

import io
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import jsonify
from werkzeug.datastructures import FileStorage, MultiDict

from src.api import api_v1
from src.app import app
from src.database import db
from src.models import Notification, Recording, RecordingSession
from tests.ingestion_char_support import check, knobs, media, needs_ffmpeg, world  # noqa: F401

pytestmark = needs_ffmpeg

DEFERRED = {"convert_workers": 1}
ONE = {"max_mb": 1, "video_mb": 4}


@pytest.fixture(autouse=True)
def _no_route_rate_limits():
    original = api_v1.limiter
    api_v1.limiter = None
    try:
        yield
    finally:
        api_v1.limiter = original


# --- share target --------------------------------------------------------------

SHARE_CASES = {
    "share/wav-with-title-text-url": ("wav", "shared.wav",
                                      {"title": "From the phone", "text": "Call notes",
                                       "url": "https://example.com/x"}, {}),
    "share/deferred/webm": ("webm", "shared.webm", {}, DEFERRED),
    "share/mp4-with-audio/retention": ("mp4_av", "shared.mp4", {}, {"retention": True}),
    "share/random-bytes": ("random", "shared.mp3", {}, {}),
    "share/empty": ("empty", "shared.wav", {}, {}),
    "share/over-max": ("big_wav", "shared.wav", {}, ONE),
}


@pytest.mark.parametrize("case_id", sorted(SHARE_CASES))
def test_share_target(world, media, case_id):
    key, filename, form, k = SHARE_CASES[case_id]
    with knobs(**k):
        resp = world.post_upload(media[key], filename, form, url="/share-target", field="shared_audio")
    check(case_id, world.observe(resp))


def test_share_target_quota_and_missing_file(world, media):
    world.user.storage_quota_mb = 1
    db.session.commit()
    with knobs():
        quota = world.post_upload(media["big_wav"], "shared.wav", url="/share-target", field="shared_audio")
        missing = world.client.post("/share-target", data={"text": "no file"},
                                    content_type="multipart/form-data")
    check("share/quota-and-missing-file", world.observe(quota, missing))


# --- API v1 upload -------------------------------------------------------------

def _api(world, token, payload, filename, form=None):
    data = dict(form or {})
    data["file"] = (io.BytesIO(world.sent(payload)), filename)
    return world.client.post("/api/v1/recordings/upload", data=data, content_type="multipart/form-data",
                             headers={"X-API-Token": token})


@pytest.mark.parametrize("mode", ["inline", "deferred"])
def test_api_v1_upload_with_options(world, media, mode):
    token = world.token()
    tag = world.tag("api")
    form = {"title": "API upload", "notes": "via token", "participants": " Ann, Bob ",
            "idempotency_key": "ing0-key", "tag_ids[0]": str(tag.id),
            "meeting_date": "2024-05-01T12:00:00+02:00"}
    with knobs(**(DEFERRED if mode == "deferred" else {})):
        resp = _api(world, token, media["mp3"], "api.mp3", form)
    check(f"api/{mode}/with-options", world.observe(resp))


def test_api_v1_idempotency_key_replay(world, media):
    token = world.token()
    with knobs():
        first = _api(world, token, media["wav"], "a.wav", {"idempotency_key": "k1"})
        same = _api(world, token, media["wav"], "a.wav", {"idempotency_key": "k1"})
        other_bytes = _api(world, token, media["mp3"], "b.mp3", {"idempotency_key": "k1"})
        no_key = _api(world, token, media["wav"], "a.wav")
    check("api/idempotency-key-replay", world.observe(first, same, other_bytes, no_key))


def test_api_v1_ignored_and_strict_tags(world, media):
    token = world.token()
    stranger = world.stranger()
    theirs = world.tag("theirs", owner=stranger)
    with knobs():
        lenient = _api(world, token, media["wav"], "a.wav", {"tag_ids[0]": str(theirs.id), "folder_id": "999999"})
        strict = _api(world, token, media["mp3"], "b.mp3", {"tag_ids[0]": str(theirs.id), "strict": "true"})
    check("api/ignored-and-strict-tags", world.observe(lenient, strict))


def test_api_v1_refusals(world, media):
    token = world.token()
    with knobs(**ONE):
        junk = _api(world, token, media["random"], "junk.mp3")
        empty = _api(world, token, media["empty"], "empty.wav")
        big = _api(world, token, media["big_wav"], "big.wav")
        long_key = _api(world, token, media["wav"], "a.wav", {"idempotency_key": "k" * 101})
    check("api/refusals", world.observe(junk, empty, big, long_key))


def test_api_v1_on_conflict_race(world, media):
    """A retry with the same key commits first while this upload is in flight (#476)."""
    token = world.token()
    from src.services import recording_ingestion as ingestion
    real_probe = ingestion.get_codec_info
    winner = []

    def _probe(*args, **kwargs):
        # The outer upload has passed its key check; the retry runs to the end now.
        if not winner:
            winner.append(None)
            winner[0] = _api(world, token, media["wav"], "a.wav", {"idempotency_key": "race"})
        return real_probe(*args, **kwargs)

    with knobs(), patch("src.services.recording_ingestion.get_codec_info", side_effect=_probe):
        loser = _api(world, token, media["wav"], "a.wav", {"idempotency_key": "race"})
    check("api/on-conflict-race", world.observe(winner[0], loser))


# --- ASR Voice Recorder ----------------------------------------------------------

def _asr(world, secret, payload, filename, **fields):
    data = {"secret": secret, **fields, "file": (io.BytesIO(world.sent(payload)), filename)}
    return world.client.post("/api/v1/integrations/asr-voice-recorder/upload", data=data,
                             content_type="multipart/form-data")


@pytest.mark.parametrize("mode", ["inline", "deferred"])
def test_asr_upload_and_reuse_duplicate(world, media, mode):
    secret = world.token()
    with knobs(**(DEFERRED if mode == "deferred" else {})):
        first = _asr(world, secret, media["mp3"], "x.mp3", file_name="Voice 001.mp3", note="hello",
                     date="1710495000000")
        retry = _asr(world, secret, media["mp3"], "x.mp3", file_name="Voice 001.mp3", note="hello",
                     date="1710495000000")
        other_note = _asr(world, secret, media["mp3"], "x.mp3", file_name="Voice 001.mp3", note="changed",
                          date="1710495000000")
        no_date = _asr(world, secret, media["wav"], "y.wav", file_name="Voice 002.wav")
        no_date_retry = _asr(world, secret, media["wav"], "y.wav", file_name="Voice 002.wav")
    check(f"asr/{mode}/upload-and-reuse-duplicate", world.observe(first, retry, other_note, no_date,
                                                                  no_date_retry))


def test_asr_retry_requeues_a_lost_transcription(world, media):
    """reuse_duplicate: a replayed QUEUED recording without a job gets its job back."""
    secret = world.token()
    with knobs():
        first = _asr(world, secret, media["wav"], "y.wav", file_name="Voice 003.wav", note="n")
        from src.models import ProcessingJob
        ProcessingJob.query.filter_by(user_id=world.uid).delete()
        db.session.commit()
        retry = _asr(world, secret, media["wav"], "y.wav", file_name="Voice 003.wav", note="n")
    check("asr/retry-requeues-lost-job", world.observe(first, retry))


def test_asr_refusals(world, media):
    secret = world.token()
    with knobs(**ONE):
        junk = _asr(world, secret, media["random"], "junk.mp3", file_name="junk.mp3")
        empty = _asr(world, secret, media["empty"], "e.wav", file_name="e.wav")
        big = _asr(world, secret, media["big_wav"], "big.wav", file_name="big.wav")
    check("asr/refusals", world.observe(junk, empty, big))


# --- sliced upload ---------------------------------------------------------------

SLICE = 16 * 1024


def _sliced(world, payload, filename, mime, form=None):
    world.sent(payload)
    opened = world.client.post("/upload/session", json={"filename": filename, "mime_type": mime,
                                                        "total_bytes": len(payload)})
    assert opened.status_code == 201, opened.get_data(as_text=True)
    sid = opened.get_json()["session_id"]
    for index, start in enumerate(range(0, len(payload), SLICE), start=1):
        r = world.client.post(f"/upload/session/{sid}/chunks/{index}", data=payload[start:start + SLICE],
                              content_type="application/octet-stream")
        assert r.status_code == 204, r.get_data(as_text=True)
    resp = world.client.post(f"/upload/session/{sid}/finalize-upload", data=dict(form or {}),
                             content_type="multipart/form-data")
    return sid, resp


def _session_state(world, sid):
    db.session.expire_all()
    s = db.session.get(RecordingSession, sid)
    from src.api.recording_sessions import _session_dir
    import os
    return {"status": s.status, "error": s.error_message,
            "finalized_recording": world.norm(s.finalized_recording_id, "finalized_recording_id"),
            "slices_dir_exists": os.path.isdir(_session_dir(sid))}


@pytest.mark.parametrize("mode", ["inline", "deferred"])
def test_sliced_upload(world, media, mode):
    tag = world.tag("sliced")
    with knobs(**(DEFERRED if mode == "deferred" else {})):
        sid, resp = _sliced(world, media["mp4_av"], "talk.mp4", "video/mp4",
                            {"title": "Sliced talk", "tag_ids[0]": str(tag.id)})
        replay = world.client.post(f"/upload/session/{sid}/finalize-upload", data={},
                                   content_type="multipart/form-data")
    check(f"sliced/{mode}/mp4-with-audio", world.observe(resp, replay, extra={
        "session": _session_state(world, sid)}))


@pytest.mark.parametrize("key, filename, mime", [("random", "junk.mp3", "audio/mpeg"),
                                                 ("big_wav", "big.wav", "audio/wav")])
def test_sliced_upload_refused(world, media, key, filename, mime):
    with knobs(**ONE):
        sid, resp = _sliced(world, media[key], filename, mime)
    check(f"sliced/refused/{key}", world.observe(resp, extra={"session": _session_state(world, sid)}))


# --- file monitor ----------------------------------------------------------------

@pytest.fixture
def watch(world, monkeypatch):
    monkeypatch.setenv("AUTO_PROCESS_STABILITY_TIME", "0")
    from src.file_monitor import FileMonitor
    base = Path(tempfile.mkdtemp(prefix="ing0-watch-"))
    folder = base / "inbox"
    folder.mkdir()
    monitor = FileMonitor(str(base), 9999, "admin_only")

    def scan(tag_id=None):
        monitor._scan_directory_for_user(folder, world.uid, tag_id)
        db.session.expire_all()

    yield folder, scan
    shutil.rmtree(base, ignore_errors=True)


def _monitor_extra(world, folder):
    notes = Notification.query.filter_by(user_id=world.uid).order_by(Notification.id).all()
    return {"watch_folder": sorted(p.name for p in folder.iterdir()),
            "notifications": [{"kind": n.kind, "message_key": n.message_key,
                               "params": sorted((n.params or {}).keys())} for n in notes]}


MONITOR_CASES = {
    "monitor/mp3": ("mp3", "memo.mp3", {}),
    "monitor/deferred/wav": ("wav", "memo.wav", DEFERRED),
    "monitor/mp4-with-audio/retention": ("mp4_av", "clip.mp4", {"retention": True}),
    "monitor/random-bytes": ("random", "junk.mp3", {}),
    "monitor/empty": ("empty", "empty.wav", {}),
    "monitor/oversized": ("big_wav", "big.wav", {**ONE, "max_content": 10_000}),
    # enforce_upload_limits=False is documented to leave out the limit on the
    # extracted audio of a video too.
    "monitor/video-with-large-extracted-audio": ("long_audio_mkv", "long.mkv", ONE),
    "monitor/deferred/video-with-large-extracted-audio": ("long_audio_mkv", "long.mkv", {**ONE, **DEFERRED}),
    "monitor/video-over-max-small-audio": ("big_mp4_av", "big.mp4", ONE),
    "monitor/deferred/video-over-max-small-audio": ("big_mp4_av", "big.mp4", {**ONE, **DEFERRED}),
}


@pytest.mark.parametrize("case_id", sorted(MONITOR_CASES))
def test_file_monitor(world, watch, media, case_id):
    folder, scan = watch
    key, filename, k = MONITOR_CASES[case_id]
    (folder / filename).write_bytes(world.sent(media[key]))
    with knobs(**k):
        scan()
    check(case_id, world.observe(extra=_monitor_extra(world, folder)))


def test_file_monitor_tag_filename_date_and_duplicate(world, watch, media):
    folder, scan = watch
    tag = world.tag("watched", default_language="nl")
    world.user.parse_filename_dates = True
    world.user.filename_date_pattern = "auto"
    db.session.commit()
    (folder / "recording-2024-03-15T09-30-00.wav").write_bytes(world.sent(media["wav"]))
    with knobs():
        scan(tag.id)
        (folder / "again.wav").write_bytes(media["wav"])
        scan(tag.id)
    check("monitor/tag-filename-date-duplicate", world.observe(extra=_monitor_extra(world, folder)))


def test_file_monitor_over_quota(world, watch, media):
    folder, scan = watch
    world.user.storage_quota_mb = 1
    db.session.commit()
    (folder / "big.wav").write_bytes(world.sent(media["big_wav"]))
    with knobs():
        scan()
    check("monitor/over-quota", world.observe(extra=_monitor_extra(world, folder)))


# --- direct calls: the options only callers pass ---------------------------------

def _ingest(world, payload, filename, form=None, **kwargs):
    from src.api.recordings import ingest_uploaded_recording
    with app.test_request_context("/upload", method="POST"):
        result = ingest_uploaded_recording(
            owner=world.user,
            uploaded_file=FileStorage(stream=io.BytesIO(world.sent(payload)), filename=filename),
            form=MultiDict(form or {}), **kwargs)
    return result


def test_direct_prepare_runs_before_the_first_commit(world, media):
    seen = []

    def prepare(recording):
        seen.append({"has_id": recording.id is not None, "audio_path": recording.audio_path,
                     "title": recording.title, "status": recording.status,
                     "visible_to_a_session_query": Recording.query.filter_by(id=recording.id).count() == 1})
        recording.participants = "From prepare"
        recording.title = "Prepared title"

    with knobs():
        resp = _ingest(world, media["wav"], "direct.wav", prepare=prepare,
                       original_filename="renamed.wav", processing_source="custom", success_status=201)
    check("direct/prepare", world.observe(resp, extra={"prepare_saw": seen}))


@pytest.mark.parametrize("answer", ["replay", "none"])
def test_direct_on_conflict(world, media, answer):
    """A unique index hit at commit is answered by on_conflict(), or with a 500."""
    with knobs():
        first = _ingest(world, media["wav"], "first.wav",
                        prepare=lambda r: setattr(r, "upload_idempotency_key", "dup"))
        calls = []

        def on_conflict():
            calls.append(Recording.query.filter_by(user_id=world.uid).count())
            return (jsonify({"answered_by": "on_conflict"}), 409) if answer == "replay" else None

        second = _ingest(world, media["mp3"], "second.mp3",
                         prepare=lambda r: setattr(r, "upload_idempotency_key", "dup"),
                         on_conflict=on_conflict)
    check(f"direct/on-conflict/{answer}", world.observe(first, second, extra={
        "on_conflict_saw_recordings": calls}))


@pytest.mark.parametrize("enforce", [True, False])
def test_direct_upload_limits(world, media, enforce):
    """Request ceiling and max_file_size_mb inside ingestion, and enforce_upload_limits=False."""
    with knobs(**ONE, max_content=1024 * 1024):
        over_both = _ingest(world, media["big_wav"], "big.wav", enforce_upload_limits=enforce)
    with knobs(**ONE):
        video = _ingest(world, media["long_audio_mkv"], "long.mkv", enforce_upload_limits=enforce)
    check(f"direct/upload-limits/{'enforced' if enforce else 'not-enforced'}",
          world.observe(over_both, video))


def test_direct_reuse_duplicate_with_meeting_date(world, media):
    form = {"meeting_date": "2024-03-15T09:30:00Z", "notes": "n"}
    with knobs():
        first = _ingest(world, media["wav"], "a.wav", form, reuse_duplicate=True, success_status=200)
        same = _ingest(world, media["wav"], "a.wav", form, reuse_duplicate=True, success_status=200)
        other_date = _ingest(world, media["wav"], "a.wav", {**form, "meeting_date": "2024-03-16T09:30:00Z"},
                             reuse_duplicate=True, success_status=200)
        other_name = _ingest(world, media["wav"], "b.wav", form, reuse_duplicate=True, success_status=200)
    check("direct/reuse-duplicate-meeting-date", world.observe(first, same, other_date, other_name))


def test_direct_path_source(world, media, tmp_path):
    from src.services.recording_source import PathSource
    path = tmp_path / "on-disk.mp3"
    path.write_bytes(world.sent(media["mp3"]))
    source = PathSource(path)
    with knobs():
        with app.test_request_context("/"):
            from src.api.recordings import ingest_uploaded_recording
            resp = ingest_uploaded_recording(owner=world.user, source=source, form={},
                                             original_filename="on-disk.mp3", processing_source="auto_process",
                                             enforce_upload_limits=False)
    source.close()
    check("direct/path-source", world.observe(resp, extra={"source_left_in_place": path.exists()}))
