"""Joining several uploaded files into one recording.

The upload dialog sends each file with join_group / join_index / join_count.
Each file is stored as a part; the file that completes the group creates one
recording and queues the merge job, which joins the parts' audio and queues
a single transcription with the upload's options.

SHARED-DB: every test uses its own users and removes what it created.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from flask import g
from flask.testing import FlaskClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Tag, Folder, RecordingTag, ProcessingJob
from src.models.upload_join import UploadJoinPart
from src.services import upload_join as uj

app.config["WTF_CSRF_ENABLED"] = False


class _Client(FlaskClient):
    def open(self, *args, **kwargs):
        g.pop('_login_user', None)
        return super().open(*args, **kwargs)


_users = []


@pytest.fixture
def ctx(tmp_path):
    with app.app_context():
        yield tmp_path
        db.session.rollback()
        for uid in _users:
            UploadJoinPart.query.filter_by(user_id=uid).delete()
            rec_ids = [r.id for r in Recording.query.filter_by(user_id=uid).all()]
            if rec_ids:
                RecordingTag.query.filter(RecordingTag.recording_id.in_(rec_ids)).delete(synchronize_session=False)
                ProcessingJob.query.filter(ProcessingJob.recording_id.in_(rec_ids)).delete(synchronize_session=False)
                Recording.query.filter(Recording.id.in_(rec_ids)).delete(synchronize_session=False)
            Tag.query.filter_by(user_id=uid).delete()
            Folder.query.filter_by(user_id=uid).delete()
        db.session.commit()
        _users.clear()


def _user():
    s = uuid.uuid4().hex[:8]
    u = User(username=f"uj_{s}", email=f"uj_{s}@local.test", password="x")
    db.session.add(u)
    db.session.commit()
    _users.append(u.id)
    return u


def _client(user):
    c = _Client(app, app.response_class)
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
    return c


class _Stored:
    def __init__(self, locator):
        self.locator = locator
        self.key = locator


class _FakeStorage:
    """Upload-side fake: records stored and deleted locators, moves nothing."""

    def __init__(self, staging):
        self.staging = str(staging)
        self.stored = []
        self.deleted = []

    def get_staging_dir(self):
        os.makedirs(self.staging, exist_ok=True)
        return self.staging

    def build_recording_key(self, original_filename, recording_id=None, *, now=None):
        return f"recordings/test/{recording_id}/{uuid.uuid4().hex[:6]}_{original_filename}"

    def upload_local_file(self, local_path, key, *, content_type=None, delete_source=False):
        self.stored.append(key)
        return _Stored(f"local://{key}")

    def delete(self, locator, missing_ok=True):
        self.deleted.append(locator)
        return True

    def exists(self, locator):
        return locator not in self.deleted


@contextmanager
def _upload_mocks(staging, durations=None):
    storage = _FakeStorage(staging)
    durations = list(durations or [])

    def _convert(filepath, **kwargs):
        r = MagicMock()
        r.output_path = filepath
        r.was_converted = r.was_compressed = False
        return r

    def _duration(path, timeout=30):
        return durations.pop(0) if durations else 60.0

    with patch("src.api.recordings.get_storage_service", return_value=storage), \
         patch("src.api.recordings.get_codec_info",
               return_value={"has_video": False, "audio_codec": "mp3", "duration": 60.0}), \
         patch("src.api.recordings.convert_if_needed", side_effect=_convert), \
         patch("src.api.recordings.get_duration", side_effect=_duration), \
         patch("src.api.recordings.get_creation_date", return_value=None), \
         patch("src.services.webhook_dispatch.emit_webhook_event") as webhook, \
         patch("src.services.job_queue.job_queue.enqueue", return_value=1) as enqueue:
        yield storage, enqueue, webhook


def _part(client, group, index, count, name=None, payload=None, **form):
    data = {"join_group": group, "join_index": str(index), "join_count": str(count)}
    data.update(form)
    data["file"] = (io.BytesIO(payload or os.urandom(2048)), name or f"meeting_part{index + 1}.mp3")
    return client.post("/upload", data=data, content_type="multipart/form-data")


def _group():
    return uuid.uuid4().hex


# ------------------------------------------------------------ the join

def test_three_files_become_one_recording_with_the_upload_options(ctx):
    user = _user()
    tag = Tag(name=f"t_{uuid.uuid4().hex[:6]}", user_id=user.id)
    folder = Folder(name=f"f_{uuid.uuid4().hex[:6]}", user_id=user.id)
    db.session.add_all([tag, folder])
    db.session.commit()
    c, group = _client(user), _group()
    form = {"tag_ids[0]": str(tag.id), "folder_id": str(folder.id), "language": "de", "notes": "agenda"}
    stamps = [1_700_000_300_000, 1_700_000_100_000, 1_700_000_200_000]  # ms, part 2 is the earliest

    with _upload_mocks(ctx, durations=[600.0, 300.0, 120.0]) as (storage, enqueue, webhook):
        bodies = [_part(c, group, i, 3, file_last_modified=str(stamps[i]), **form) for i in range(3)]

    assert [b.status_code for b in bodies] == [202, 202, 202]
    first, second, last = (b.get_json() for b in bodies)
    assert first == {"join_pending": True, "join_group": group, "join_index": 0, "received": 1, "total": 3}
    assert second["received"] == 2 and second["join_pending"]
    assert last["joined_parts"] == 3 and last["id"]

    rec = db.session.get(Recording, last["id"])
    assert rec.status == "QUEUED" and rec.audio_path is None
    assert rec.original_filename == "meeting_part1.m4a"
    assert rec.folder_id == folder.id and [t.id for t in rec.tags] == [tag.id]
    assert rec.notes == "agenda"
    assert rec.audio_duration_seconds == pytest.approx(1020.0)
    assert rec.meeting_date == datetime.utcfromtimestamp(stamps[1] / 1000)

    parts = UploadJoinPart.query.filter_by(user_id=user.id, group_id=group).order_by(UploadJoinPart.part_index).all()
    assert all(p.claimed_at and p.recording_id == rec.id for p in parts)
    jobs = [call.kwargs for call in enqueue.call_args_list]
    assert [j["job_type"] for j in jobs] == ["merge"]
    assert jobs[0]["params"]["part_ids"] == [p.id for p in parts]
    assert jobs[0]["params"]["transcribe_params"]["language"] == "de"
    assert [call.kwargs["event_type"] for call in webhook.call_args_list] == ["recording.created"]
    assert Recording.query.filter_by(user_id=user.id).count() == 1


def test_parts_may_arrive_in_any_order(ctx):
    user = _user()
    c, group = _client(user), _group()
    with _upload_mocks(ctx) as (storage, enqueue, _):
        for i in (2, 0, 1):
            r = _part(c, group, i, 3)
    body = r.get_json()
    assert body["joined_parts"] == 3
    params = enqueue.call_args.kwargs["params"]
    order = [db.session.get(UploadJoinPart, pid).part_index for pid in params["part_ids"]]
    assert order == [0, 1, 2]


def test_a_user_title_is_kept(ctx):
    user = _user()
    c, group = _client(user), _group()
    with _upload_mocks(ctx):
        _part(c, group, 0, 2, title="Board meeting")
        body = _part(c, group, 1, 2, title="Board meeting").get_json()
    assert db.session.get(Recording, body["id"]).title == "Board meeting"


# ------------------------------------------------------------ retries and races

def test_retrying_a_part_after_the_join_returns_the_same_recording(ctx):
    user = _user()
    c, group = _client(user), _group()
    with _upload_mocks(ctx) as (storage, enqueue, _):
        _part(c, group, 0, 2)
        joined = _part(c, group, 1, 2).get_json()
        again = _part(c, group, 1, 2).get_json()
    assert again["id"] == joined["id"] and again["idempotent_replay"] is True
    assert Recording.query.filter_by(user_id=user.id).count() == 1
    assert enqueue.call_count == 1


def test_retrying_a_part_before_the_join_replaces_it(ctx):
    user = _user()
    c, group = _client(user), _group()
    with _upload_mocks(ctx) as (storage, enqueue, _):
        _part(c, group, 0, 3)
        first_locator = UploadJoinPart.query.filter_by(user_id=user.id, group_id=group).one().audio_path
        body = _part(c, group, 0, 3).get_json()
    assert body["received"] == 1
    part = UploadJoinPart.query.filter_by(user_id=user.id, group_id=group).one()
    assert part.audio_path != first_locator and first_locator in storage.deleted


def test_a_group_is_claimed_once(ctx):
    user = _user()
    c, group = _client(user), _group()
    with _upload_mocks(ctx):
        _part(c, group, 0, 2)
        # Store the last part without letting the request claim the group.
        with patch("src.services.upload_join.claim_group", return_value=None):
            _part(c, group, 1, 2)
    assert uj.claim_group(user.id, group, 2) is not None
    db.session.commit()
    assert uj.claim_group(user.id, group, 2) is None


def test_join_groups_belong_to_one_user(ctx):
    alice, bob = _user(), _user()
    group = _group()
    with _upload_mocks(ctx):
        _part(_client(alice), group, 0, 2)
        body = _part(_client(bob), group, 1, 2).get_json()
    assert body["join_pending"] and body["received"] == 1
    assert Recording.query.filter(Recording.user_id.in_([alice.id, bob.id])).count() == 0


@pytest.mark.parametrize("fields", [
    {"join_group": "short", "join_index": "0", "join_count": "2"},
    {"join_group": "has spaces in it", "join_index": "0", "join_count": "2"},
    {"join_group": "abcdefgh12", "join_index": "0", "join_count": "1"},
    {"join_group": "abcdefgh12", "join_index": "0", "join_count": "21"},
    {"join_group": "abcdefgh12", "join_index": "2", "join_count": "2"},
    {"join_group": "abcdefgh12", "join_index": "x", "join_count": "2"},
])
def test_invalid_join_fields_are_rejected(ctx, fields):
    user = _user()
    data = dict(fields)
    data["file"] = (io.BytesIO(b"\x00" * 64), "a.mp3")
    with _upload_mocks(ctx):
        r = _client(user).post("/upload", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    assert UploadJoinPart.query.filter_by(user_id=user.id).count() == 0


# ------------------------------------------------------------ giving up and cleanup

def test_discarding_a_join_removes_its_parts(ctx):
    user = _user()
    c, group = _client(user), _group()
    with _upload_mocks(ctx):
        _part(c, group, 0, 3)
        _part(c, group, 1, 3)
    storage = _FakeStorage(ctx)
    with patch("src.services.storage.get_storage_service", return_value=storage):
        r = c.delete(f"/upload/join/{group}")
    assert r.get_json() == {"success": True, "removed": 2}
    assert UploadJoinPart.query.filter_by(user_id=user.id).count() == 0
    assert len(storage.deleted) == 2


def test_cleanup_removes_stale_parts_but_waits_for_a_pending_merge(ctx):
    user = _user()
    old = datetime.utcnow() - timedelta(hours=30)
    waiting = Recording(user_id=user.id, title="w", status="QUEUED", audio_path=None, original_filename="w.m4a")
    done = Recording(user_id=user.id, title="d", status="COMPLETED", audio_path="local://d", original_filename="d.m4a")
    db.session.add_all([waiting, done])
    db.session.flush()
    rows = [
        UploadJoinPart(user_id=user.id, group_id="abandoned1", part_index=0, part_count=2,
                       audio_path="local://a", created_at=old),
        UploadJoinPart(user_id=user.id, group_id="waiting123", part_index=0, part_count=2,
                       audio_path="local://w", created_at=old, claimed_at=old, recording_id=waiting.id),
        UploadJoinPart(user_id=user.id, group_id="finished12", part_index=0, part_count=2,
                       audio_path=None, created_at=old, claimed_at=old, recording_id=done.id),
        UploadJoinPart(user_id=user.id, group_id="fresh12345", part_index=0, part_count=2,
                       audio_path="local://f"),
    ]
    db.session.add_all(rows)
    db.session.commit()
    storage = _FakeStorage(ctx)
    removed = uj.cleanup_stale_parts(storage=storage)
    assert removed >= 2
    left = {p.group_id for p in UploadJoinPart.query.filter_by(user_id=user.id).all()}
    assert left == {"waiting123", "fresh12345"}
    assert storage.deleted == ["local://a"]


# ------------------------------------------------------------ the merge job

class _MergeStorage(_FakeStorage):
    def __init__(self, staging, paths=None):
        super().__init__(staging)
        self.paths = paths or {}

    @contextmanager
    def materialize(self, locator):
        m = MagicMock()
        m.local_path = self.paths.get(locator, "/nonexistent")
        yield m


def _claimed_group(user, n=2, locators=None):
    group = _group()
    parts = []
    for i in range(n):
        p = UploadJoinPart(user_id=user.id, group_id=group, part_index=i, part_count=n,
                           audio_path=(locators or [f"local://p{i}"] * n)[i], original_filename=f"p{i}.wav",
                           file_size=10, audio_duration_seconds=1.0)
        db.session.add(p)
        parts.append(p)
    db.session.commit()
    assert uj.claim_group(user.id, group, n) is not None
    with patch("src.services.job_queue.job_queue.enqueue", return_value=1) as enqueue:
        rec = uj.create_joined_recording(user, parts, transcribe_params={"language": "fr"})
    return rec, parts, enqueue.call_args.kwargs["params"]


def test_merge_job_joins_parts_and_queues_one_transcription(ctx):
    from src.services import recording_merge
    user = _user()
    rec, parts, params = _claimed_group(user)
    storage = _MergeStorage(ctx)

    def _fake_concat(inputs, output):
        assert len(inputs) == 2
        with open(output, "wb") as f:
            f.write(b"\x00" * 1024)

    with patch("src.services.recording_merge.get_storage_service", return_value=storage), \
         patch("src.services.recording_merge._concat_audio", side_effect=_fake_concat), \
         patch("src.services.recording_merge.job_queue.enqueue", return_value=1) as enqueue:
        recording_merge.run_merge_job(rec, params)

    db.session.expire_all()
    rec = db.session.get(Recording, rec.id)
    assert rec.audio_path and rec.file_size == 1024
    assert all(db.session.get(UploadJoinPart, p.id).audio_path is None for p in parts)
    assert sorted(storage.deleted) == ["local://p0", "local://p1"]
    assert enqueue.call_args.kwargs["job_type"] == "transcribe"
    assert enqueue.call_args.kwargs["params"] == {"language": "fr"}


def test_merge_job_refuses_parts_of_another_recording(ctx):
    from src.services import recording_merge
    user = _user()
    rec_a, _, params_a = _claimed_group(user)
    rec_b, _, _ = _claimed_group(user)
    with patch("src.services.recording_merge.get_storage_service", return_value=_MergeStorage(ctx)):
        with pytest.raises(recording_merge.MergeError):
            recording_merge.run_merge_job(rec_b, params_a)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_merge_job_really_joins_the_audio(ctx):
    """End to end through ffmpeg: two tones become one file as long as both."""
    from src.services import recording_merge
    from src.utils.ffprobe import get_duration
    paths = {}
    for i, (freq, secs) in enumerate([(440, 1.0), (660, 2.0)]):
        path = os.path.join(str(ctx), f"tone{i}.wav")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", f"sine=frequency={freq}:duration={secs}", path], check=True)
        paths[f"local://tone{i}"] = path
    user = _user()
    rec, _, params = _claimed_group(user, locators=list(paths))
    storage = _MergeStorage(ctx, paths)
    kept = {}

    def _keep(local_path, key, *, content_type=None, delete_source=False):
        dest = os.path.join(str(ctx), "joined.m4a")
        shutil.copy(local_path, dest)
        kept["path"] = dest
        return _Stored(f"local://{key}")

    storage.upload_local_file = _keep
    with patch("src.services.recording_merge.get_storage_service", return_value=storage), \
         patch("src.services.recording_merge.job_queue.enqueue", return_value=1):
        recording_merge.run_merge_job(rec, params)
    assert get_duration(kept["path"]) == pytest.approx(3.0, abs=0.2)


# ------------------------------------------------------------ sliced uploads

def test_a_lost_response_for_a_sliced_part_is_answered_on_retry(ctx):
    """A large part goes up in slices; if the finalize response is lost, the
    retry is answered with the join's state, not a 409."""
    from src.models import RecordingSession
    user = _user()
    c, group = _client(user), _group()
    session = RecordingSession(user_id=user.id, kind='sliced_upload', status='finalized',
                               upload_filename='p0.wav', mime_type='audio/wav',
                               finalized_at=datetime.utcnow(), finalized_recording_id=None)
    db.session.add(session)
    db.session.commit()
    try:
        form = {"join_group": group, "join_index": "0", "join_count": "2"}
        with _upload_mocks(ctx):
            db.session.add(UploadJoinPart(user_id=user.id, group_id=group, part_index=0, part_count=2,
                                          audio_path="local://p0"))
            db.session.commit()
            body = c.post(f"/upload/session/{session.id}/finalize-upload", data=form).get_json()
        assert body["join_pending"] and body["received"] == 1 and body["idempotent_replay"]

        with _upload_mocks(ctx):
            joined = _part(c, group, 1, 2).get_json()
            again = c.post(f"/upload/session/{session.id}/finalize-upload", data=form).get_json()
        assert again["id"] == joined["id"] and again["idempotent_replay"] is True
    finally:
        db.session.delete(db.session.get(RecordingSession, session.id))
        db.session.commit()


# ------------------------------------------------------------ conversion

def test_audio_parts_skip_conversion_but_video_parts_do_not(ctx):
    """The merge re-encodes every part, so converting an audio part at upload
    only delays the response; a video part still has its audio extracted."""
    user = _user()
    c = _client(user)

    def _convert(filepath, **kwargs):
        r = MagicMock()
        r.output_path = filepath
        r.was_converted = r.was_compressed = False
        return r

    def _probe(video):
        return {"has_video": video, "audio_codec": "aac", "video_codec": "h264" if video else None, "duration": 60.0}

    with _upload_mocks(ctx):
        with patch("src.api.recordings.convert_if_needed", side_effect=_convert) as convert, \
             patch("src.api.recordings.get_codec_info", return_value=_probe(False)):
            _part(c, _group(), 0, 2, name="long.m4a")
        assert convert.call_count == 0

        with patch("src.api.recordings.convert_if_needed", side_effect=_convert) as convert, \
             patch("src.api.recordings.get_codec_info", return_value=_probe(True)), \
             patch("src.api.recordings.VIDEO_RETENTION", False):
            _part(c, _group(), 0, 2, name="talk.mp4")
        assert convert.call_count == 1

        with patch("src.api.recordings.convert_if_needed", side_effect=_convert) as convert, \
             patch("src.api.recordings.get_codec_info", return_value=_probe(False)):
            data = {"file": (io.BytesIO(os.urandom(512)), "single.m4a")}
            r = c.post("/upload", data=data, content_type="multipart/form-data")
        assert r.status_code == 202 and convert.call_count == 1
