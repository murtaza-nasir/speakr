"""Coverage tests for the WRITE paths in src/api/recordings.py.

Focus: the /upload handler end-to-end (Recording row creation, audio_path
locator, job enqueue, keep_audio_only, tag/folder association, per-upload
transcription params, duplicate detection, size/type validation rejects,
missing-file 400), plus metadata edit endpoints (/save, update_transcription,
toggle_inbox/highlight, tag add/remove/reorder, bulk-tags) and the
_resolve_transcription_model / resolve_upload_title helpers.

External effects (codec probe, conversion, storage, job queue, ffprobe) are
mocked at the recordings.py import site so nothing touches real storage or
shells out. Storage.upload_local_file is faked to return a StoredObject with a
.locator without moving any file.

SHARED-DB: every assertion is scoped to the specific user/recording the test
created. No assertions on global counts of all recordings.
"""

import io
import os
import sys
import uuid
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Tag, Folder, RecordingTag, SystemSetting

app.config["WTF_CSRF_ENABLED"] = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _mk_user(prefix="up"):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{prefix}_{suffix}",
        email=f"{prefix}_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _mk_tag(user, **kwargs):
    tag = Tag(
        name=f"tag_{uuid.uuid4().hex[:8]}",
        user_id=user.id,
        **kwargs,
    )
    db.session.add(tag)
    db.session.commit()
    return tag


def _mk_folder(user, **kwargs):
    folder = Folder(
        name=f"folder_{uuid.uuid4().hex[:8]}",
        user_id=user.id,
        **kwargs,
    )
    db.session.add(folder)
    db.session.commit()
    return folder


def _mk_recording(user, **kwargs):
    rec = Recording(
        user_id=user.id,
        title=kwargs.pop("title", f"rec_{uuid.uuid4().hex[:8]}"),
        status=kwargs.pop("status", "COMPLETED"),
        audio_path=kwargs.pop("audio_path", "local://recordings/x.mp3"),
        original_filename=kwargs.pop("original_filename", "x.mp3"),
        **kwargs,
    )
    db.session.add(rec)
    db.session.commit()
    return rec


class _FakeStoredObject:
    def __init__(self, locator, key):
        self.locator = locator
        self.key = key
        self.size = 1024
        self.content_type = None
        self.etag = None


class _FakeStorage:
    """Drop-in fake for StorageService used by the upload route.

    upload_local_file does NOT move any file (delete_source ignored) and just
    returns a StoredObject with a deterministic locator so audio_path gets set.
    """

    def __init__(self, staging_dir):
        self._staging = staging_dir
        self.uploaded = []

    def get_staging_dir(self):
        os.makedirs(self._staging, exist_ok=True)
        return self._staging

    def build_recording_key(self, original_filename, recording_id=None, *, now=None):
        return f"recordings/test/{recording_id}/{original_filename}"

    def upload_local_file(self, local_path, key, *, content_type=None, delete_source=False):
        self.uploaded.append((local_path, key))
        return _FakeStoredObject(locator=f"local://{key}", key=key)


@contextmanager
def _upload_mocks(staging_dir, has_video=False, audio_codec="mp3"):
    """Patch all external upload effects. convert_if_needed returns the input
    path untouched (no conversion); storage is faked so no file moves."""
    fake_storage = _FakeStorage(staging_dir)

    def _convert(filepath, **kwargs):
        result = MagicMock()
        result.output_path = filepath
        result.was_converted = False
        result.was_compressed = False
        result.original_codec = audio_codec
        result.final_codec = audio_codec
        result.size_reduction_percent = 0.0
        return result

    with patch("src.api.recordings.get_storage_service", return_value=fake_storage), \
         patch("src.api.recordings.get_codec_info",
               return_value={"has_video": has_video, "audio_codec": audio_codec,
                             "video_codec": "h264" if has_video else None, "duration": 12.0}), \
         patch("src.api.recordings.convert_if_needed", side_effect=_convert), \
         patch("src.api.recordings.get_duration", return_value=12.0), \
         patch("src.api.recordings.get_creation_date", return_value=None), \
         patch("src.services.job_queue.job_queue.enqueue", return_value=1) as enqueue_mock:
        yield fake_storage, enqueue_mock


def _do_upload(client, filename="sample.mp3", payload=b"\x00" * 4096, **form):
    data = dict(form)
    data["file"] = (io.BytesIO(payload), filename)
    return client.post("/upload", data=data, content_type="multipart/form-data")


def _latest_rec(user):
    return (Recording.query.filter_by(user_id=user.id)
            .order_by(Recording.id.desc()).first())


def _cleanup(*objs):
    for o in objs:
        try:
            db.session.delete(o)
        except Exception:
            pass
    db.session.commit()


# --------------------------------------------------------------------------- #
# Upload — success / row creation
# --------------------------------------------------------------------------- #

def test_upload_creates_recording_row_with_locator_and_enqueues():
    with app.app_context():
        user = _mk_user("up_ok")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging) as (storage, enqueue):
            resp = _do_upload(client, title="Hello", notes="my note")
        assert resp.status_code == 202, resp.data
        rec = _latest_rec(user)
        assert rec is not None
        assert rec.title == "Hello"
        assert rec.notes == "my note"
        assert rec.status == "PENDING"
        assert rec.processing_source == "upload"
        assert rec.original_filename == "sample.mp3"
        # audio_path is the storage locator returned by upload_local_file
        assert rec.audio_path is not None and rec.audio_path.startswith("local://")
        assert storage.uploaded  # upload_local_file was invoked
        assert rec.file_hash is not None
        assert rec.audio_duration_seconds == 12.0
        # Enqueued a transcribe job for this recording
        assert enqueue.called
        kwargs = enqueue.call_args.kwargs
        assert kwargs["recording_id"] == rec.id
        assert kwargs["job_type"] == "transcribe"
        assert kwargs["is_new_upload"] is True
        _cleanup(rec, user)


def test_upload_default_title_is_placeholder():
    with app.app_context():
        user = _mk_user("up_title")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            resp = _do_upload(client, filename="interview.mp3")
        assert resp.status_code == 202
        rec = _latest_rec(user)
        assert rec.title == "Recording - interview.mp3"
        _cleanup(rec, user)


def test_upload_user_meeting_date_parsed():
    with app.app_context():
        user = _mk_user("up_date")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            resp = _do_upload(client, meeting_date="2022-03-04T09:15:00Z")
        assert resp.status_code == 202
        rec = _latest_rec(user)
        assert rec.meeting_date.year == 2022
        assert rec.meeting_date.month == 3
        assert rec.meeting_date.day == 4
        _cleanup(rec, user)


def test_upload_file_last_modified_used_for_meeting_date():
    with app.app_context():
        user = _mk_user("up_lm")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        # 1577836800000 ms = 2020-01-01 UTC
        with _upload_mocks(staging):
            resp = _do_upload(client, file_last_modified="1577836800000")
        assert resp.status_code == 202
        rec = _latest_rec(user)
        assert rec.meeting_date.year == 2020
        _cleanup(rec, user)


# --------------------------------------------------------------------------- #
# Upload — keep_audio_only
# --------------------------------------------------------------------------- #

def test_upload_keep_audio_only_flag_persisted():
    with app.app_context():
        user = _mk_user("up_kao")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging, has_video=True, audio_codec="aac"):
            resp = _do_upload(client, filename="movie.mp4", keep_audio_only="true")
        assert resp.status_code == 202
        rec = _latest_rec(user)
        assert rec.keep_audio_only is True
        _cleanup(rec, user)


def test_upload_keep_audio_only_default_when_video_retention_off():
    with app.app_context(), patch("src.api.recordings.VIDEO_RETENTION", False):
        user = _mk_user("up_vroff")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging, has_video=True, audio_codec="aac"):
            resp = _do_upload(client, filename="movie.mp4")
        assert resp.status_code == 202
        rec = _latest_rec(user)
        # VIDEO_RETENTION off => effective_audio_only True even without flag
        assert rec.keep_audio_only is True
        _cleanup(rec, user)


# --------------------------------------------------------------------------- #
# Upload — tag / folder association + per-upload transcription params
# --------------------------------------------------------------------------- #

def test_upload_with_tags_creates_associations_in_order():
    with app.app_context():
        user = _mk_user("up_tags")
        client = app.test_client()
        _login(client, user)
        tag1 = _mk_tag(user)
        tag2 = _mk_tag(user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging) as (storage, enqueue):
            resp = _do_upload(client, **{"tag_ids[0]": str(tag1.id),
                                         "tag_ids[1]": str(tag2.id)})
        assert resp.status_code == 202
        rec = _latest_rec(user)
        assocs = (RecordingTag.query.filter_by(recording_id=rec.id)
                  .order_by(RecordingTag.order).all())
        assert [a.tag_id for a in assocs] == [tag1.id, tag2.id]
        assert [a.order for a in assocs] == [1, 2]
        # First tag id is passed to the job
        assert enqueue.call_args.kwargs["params"]["tag_id"] == tag1.id
        for a in assocs:
            db.session.delete(a)
        db.session.commit()
        _cleanup(rec, tag1, tag2, user)


def test_upload_single_tag_id_backward_compat():
    with app.app_context():
        user = _mk_user("up_stag")
        client = app.test_client()
        _login(client, user)
        tag = _mk_tag(user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            resp = _do_upload(client, tag_id=str(tag.id))
        assert resp.status_code == 202
        rec = _latest_rec(user)
        assocs = RecordingTag.query.filter_by(recording_id=rec.id).all()
        assert len(assocs) == 1 and assocs[0].tag_id == tag.id
        for a in assocs:
            db.session.delete(a)
        db.session.commit()
        _cleanup(rec, tag, user)


def test_upload_other_users_tag_is_ignored():
    with app.app_context():
        owner = _mk_user("up_own")
        other = _mk_user("up_oth")
        foreign_tag = _mk_tag(other)
        client = app.test_client()
        _login(client, owner)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            resp = _do_upload(client, **{"tag_ids[0]": str(foreign_tag.id)})
        assert resp.status_code == 202
        rec = _latest_rec(owner)
        assert RecordingTag.query.filter_by(recording_id=rec.id).count() == 0
        _cleanup(rec, foreign_tag, owner, other)


def test_upload_with_folder_sets_folder_id():
    with app.app_context():
        user = _mk_user("up_fld")
        client = app.test_client()
        _login(client, user)
        folder = _mk_folder(user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            resp = _do_upload(client, folder_id=str(folder.id))
        assert resp.status_code == 202
        rec = _latest_rec(user)
        assert rec.folder_id == folder.id
        _cleanup(rec, folder, user)


def test_upload_other_users_folder_ignored():
    with app.app_context():
        owner = _mk_user("up_fown")
        other = _mk_user("up_foth")
        foreign_folder = _mk_folder(other)
        client = app.test_client()
        _login(client, owner)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            resp = _do_upload(client, folder_id=str(foreign_folder.id))
        assert resp.status_code == 202
        rec = _latest_rec(owner)
        assert rec.folder_id is None
        _cleanup(rec, foreign_folder, owner, other)


def test_upload_per_request_transcription_params_passed_to_job():
    with app.app_context():
        user = _mk_user("up_params")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging) as (storage, enqueue):
            resp = _do_upload(
                client,
                language="en",
                min_speakers="2",
                max_speakers="4",
                hotwords="acme, foo",
                initial_prompt="prior context",
            )
        assert resp.status_code == 202
        params = enqueue.call_args.kwargs["params"]
        assert params["language"] == "en"
        assert params["min_speakers"] == 2
        assert params["max_speakers"] == 4
        assert params["hotwords"] == "acme, foo"
        assert params["initial_prompt"] == "prior context"
        rec = _latest_rec(user)
        _cleanup(rec, user)


def test_upload_invalid_speaker_counts_become_none():
    with app.app_context():
        user = _mk_user("up_badspk")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging) as (storage, enqueue):
            resp = _do_upload(client, min_speakers="abc", max_speakers="")
        assert resp.status_code == 202
        params = enqueue.call_args.kwargs["params"]
        assert params["min_speakers"] is None
        assert params["max_speakers"] is None
        rec = _latest_rec(user)
        _cleanup(rec, user)


def test_upload_tag_defaults_applied_when_not_user_provided():
    with app.app_context():
        user = _mk_user("up_tagdef")
        client = app.test_client()
        _login(client, user)
        tag = _mk_tag(user, default_language="fr", default_hotwords="bonjour")
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging) as (storage, enqueue):
            resp = _do_upload(client, **{"tag_ids[0]": str(tag.id)})
        assert resp.status_code == 202
        params = enqueue.call_args.kwargs["params"]
        assert params["language"] == "fr"
        assert params["hotwords"] == "bonjour"
        rec = _latest_rec(user)
        for a in RecordingTag.query.filter_by(recording_id=rec.id).all():
            db.session.delete(a)
        db.session.commit()
        _cleanup(rec, tag, user)


def test_upload_folder_defaults_applied_when_no_tags():
    with app.app_context():
        user = _mk_user("up_flddef")
        client = app.test_client()
        _login(client, user)
        folder = _mk_folder(user, default_language="de")
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging) as (storage, enqueue):
            resp = _do_upload(client, folder_id=str(folder.id))
        assert resp.status_code == 202
        params = enqueue.call_args.kwargs["params"]
        assert params["language"] == "de"
        rec = _latest_rec(user)
        _cleanup(rec, folder, user)


def test_upload_prompt_variables_sanitized_and_stored():
    with app.app_context():
        user = _mk_user("up_pvar")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            resp = _do_upload(client, prompt_variables='{"client_name": "Acme"}')
        assert resp.status_code == 202
        rec = _latest_rec(user)
        assert rec.prompt_variables == {"client_name": "Acme"}
        _cleanup(rec, user)


def test_upload_invalid_prompt_variables_json_ignored():
    with app.app_context():
        user = _mk_user("up_pvbad")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        with _upload_mocks(staging):
            resp = _do_upload(client, prompt_variables="{not json")
        assert resp.status_code == 202
        rec = _latest_rec(user)
        assert rec.prompt_variables is None
        _cleanup(rec, user)


# --------------------------------------------------------------------------- #
# Upload — duplicate detection
# --------------------------------------------------------------------------- #

def test_upload_duplicate_hash_returns_warning():
    with app.app_context():
        user = _mk_user("up_dup")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        payload = b"duplicate-bytes" * 500
        with _upload_mocks(staging):
            r1 = _do_upload(client, payload=payload, title="First")
        assert r1.status_code == 202
        first = _latest_rec(user)
        with _upload_mocks(staging):
            r2 = _do_upload(client, payload=payload, title="Second")
        assert r2.status_code == 202
        body = r2.get_json()
        assert "duplicate_warning" in body
        assert body["duplicate_warning"]["existing_recording_id"] == first.id
        second = _latest_rec(user)
        assert second.id != first.id
        _cleanup(second, first, user)


# --------------------------------------------------------------------------- #
# Upload — validation rejects
# --------------------------------------------------------------------------- #

def test_upload_missing_file_returns_400():
    with app.app_context():
        user = _mk_user("up_nofile")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/upload", data={"title": "x"},
                           content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "No file provided" in resp.get_json()["error"]
        _cleanup(user)


def test_upload_empty_filename_returns_400():
    with app.app_context():
        user = _mk_user("up_emptyfn")
        client = app.test_client()
        _login(client, user)
        data = {"file": (io.BytesIO(b"abc"), "")}
        resp = client.post("/upload", data=data, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "No file selected" in resp.get_json()["error"]
        _cleanup(user)


def test_upload_too_large_returns_413():
    with app.app_context():
        user = _mk_user("up_big")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        # Force a tiny per-request limit so a small payload trips it.
        with _upload_mocks(staging), \
             patch.object(SystemSetting, "get_setting",
                          side_effect=lambda k, d=None: 0 if k == "max_file_size_mb" else d), \
             patch("src.api.recordings.chunking_service", None):
            resp = _do_upload(client, payload=b"x" * 8192)
        assert resp.status_code == 413
        body = resp.get_json()
        assert "max_size_mb" in body
        # No recording row created for the rejected upload
        assert Recording.query.filter_by(user_id=user.id).count() == 0
        _cleanup(user)


def test_upload_requires_login():
    with app.app_context():
        client = app.test_client()  # not logged in
        resp = _do_upload(client)
        # @login_required redirects (302) or 401
        assert resp.status_code in (302, 401)


def test_upload_ffmpeg_error_returns_500():
    with app.app_context():
        user = _mk_user("up_ffmpeg")
        client = app.test_client()
        _login(client, user)
        staging = os.path.join(app.config["UPLOAD_FOLDER"], f"stg_{uuid.uuid4().hex[:6]}")
        from src.utils.ffmpeg_utils import FFmpegError
        fake_storage = _FakeStorage(staging)
        with patch("src.api.recordings.get_storage_service", return_value=fake_storage), \
             patch("src.api.recordings.get_codec_info",
                   return_value={"has_video": False, "audio_codec": "flac"}), \
             patch("src.api.recordings.convert_if_needed",
                   side_effect=FFmpegError("boom")), \
             patch("src.services.job_queue.job_queue.enqueue", return_value=1):
            resp = _do_upload(client, filename="a.flac")
        assert resp.status_code == 500
        # An error payload is returned; the exact wording (convert-specific vs
        # the generic upload handler) is an implementation detail.
        assert resp.get_json().get("error")
        _cleanup(user)


# --------------------------------------------------------------------------- #
# /save metadata edit
# --------------------------------------------------------------------------- #

def test_save_updates_title_notes_participants():
    with app.app_context():
        user = _mk_user("sv_ok")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user, title="old")
        resp = client.post("/save", json={
            "id": rec.id, "title": "new title",
            "participants": "Alice, Bob", "notes": "edited notes",
        })
        assert resp.status_code == 200
        db.session.refresh(rec)
        assert rec.title == "new title"
        assert rec.participants == "Alice, Bob"
        assert "edited notes" in (rec.notes or "")
        _cleanup(rec, user)


def test_save_meeting_date_date_only():
    with app.app_context():
        user = _mk_user("sv_date")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        resp = client.post("/save", json={"id": rec.id, "meeting_date": "2021-07-08"})
        assert resp.status_code == 200
        db.session.refresh(rec)
        assert rec.meeting_date.year == 2021
        assert rec.meeting_date.month == 7
        assert rec.meeting_date.day == 8
        _cleanup(rec, user)


def test_save_no_data_returns_400():
    with app.app_context():
        user = _mk_user("sv_nd")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/save", json={})
        assert resp.status_code == 400
        _cleanup(user)


def test_save_missing_id_returns_400():
    with app.app_context():
        user = _mk_user("sv_nid")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/save", json={"title": "x"})
        assert resp.status_code == 400
        _cleanup(user)


def test_save_nonexistent_returns_404():
    with app.app_context():
        user = _mk_user("sv_404")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/save", json={"id": 99999999, "title": "x"})
        assert resp.status_code == 404
        _cleanup(user)


def test_save_other_users_recording_forbidden():
    with app.app_context():
        owner = _mk_user("sv_own")
        other = _mk_user("sv_oth")
        rec = _mk_recording(owner, title="keep")
        client = app.test_client()
        _login(client, other)
        resp = client.post("/save", json={"id": rec.id, "title": "hacked"})
        assert resp.status_code == 403
        db.session.refresh(rec)
        assert rec.title == "keep"
        _cleanup(rec, owner, other)


# --------------------------------------------------------------------------- #
# update_transcription
# --------------------------------------------------------------------------- #

def test_update_transcription_sets_field():
    with app.app_context():
        user = _mk_user("ut_ok")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        resp = client.post(f"/recording/{rec.id}/update_transcription",
                           json={"transcription": "new transcript text"})
        assert resp.status_code == 200
        db.session.refresh(rec)
        assert rec.transcription == "new transcript text"
        _cleanup(rec, user)


def test_update_transcription_missing_data_400():
    with app.app_context():
        user = _mk_user("ut_nd")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        resp = client.post(f"/recording/{rec.id}/update_transcription", json={})
        assert resp.status_code == 400
        _cleanup(rec, user)


def test_update_transcription_nonexistent_404():
    with app.app_context():
        user = _mk_user("ut_404")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/recording/99999999/update_transcription",
                           json={"transcription": "x"})
        assert resp.status_code == 404
        _cleanup(user)


def test_update_transcription_forbidden_for_other_user():
    with app.app_context():
        owner = _mk_user("ut_own")
        other = _mk_user("ut_oth")
        rec = _mk_recording(owner, transcription="orig")
        client = app.test_client()
        _login(client, other)
        resp = client.post(f"/recording/{rec.id}/update_transcription",
                           json={"transcription": "tamper"})
        assert resp.status_code == 403
        db.session.refresh(rec)
        assert rec.transcription == "orig"
        _cleanup(rec, owner, other)


# --------------------------------------------------------------------------- #
# toggle inbox / highlight
# --------------------------------------------------------------------------- #

def test_toggle_inbox_flips_status():
    with app.app_context():
        user = _mk_user("tib")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        r1 = client.post(f"/recording/{rec.id}/toggle_inbox")
        assert r1.status_code == 200
        first = r1.get_json()["is_inbox"]
        r2 = client.post(f"/recording/{rec.id}/toggle_inbox")
        assert r2.status_code == 200
        assert r2.get_json()["is_inbox"] != first
        _cleanup(rec, user)


def test_toggle_highlight_flips_status():
    with app.app_context():
        user = _mk_user("thl")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        r1 = client.post(f"/recording/{rec.id}/toggle_highlight")
        assert r1.status_code == 200
        first = r1.get_json()["is_highlighted"]
        r2 = client.post(f"/recording/{rec.id}/toggle_highlight")
        assert r2.status_code == 200
        assert r2.get_json()["is_highlighted"] != first
        _cleanup(rec, user)


def test_toggle_inbox_nonexistent_404():
    with app.app_context():
        user = _mk_user("tib404")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/recording/99999999/toggle_inbox")
        assert resp.status_code == 404
        _cleanup(user)


# --------------------------------------------------------------------------- #
# Tag add / remove / reorder
# --------------------------------------------------------------------------- #

def test_add_tag_to_recording():
    with app.app_context():
        user = _mk_user("at_ok")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        tag = _mk_tag(user)
        resp = client.post(f"/api/recordings/{rec.id}/tags", json={"tag_id": tag.id})
        assert resp.status_code == 200
        assert RecordingTag.query.filter_by(recording_id=rec.id, tag_id=tag.id).count() == 1
        for a in RecordingTag.query.filter_by(recording_id=rec.id).all():
            db.session.delete(a)
        db.session.commit()
        _cleanup(rec, tag, user)


def test_add_tag_missing_tag_id_400():
    with app.app_context():
        user = _mk_user("at_nid")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        resp = client.post(f"/api/recordings/{rec.id}/tags", json={})
        assert resp.status_code == 400
        _cleanup(rec, user)


def test_add_tag_duplicate_400():
    with app.app_context():
        user = _mk_user("at_dup")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        tag = _mk_tag(user)
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=1))
        db.session.commit()
        resp = client.post(f"/api/recordings/{rec.id}/tags", json={"tag_id": tag.id})
        assert resp.status_code == 400
        for a in RecordingTag.query.filter_by(recording_id=rec.id).all():
            db.session.delete(a)
        db.session.commit()
        _cleanup(rec, tag, user)


def test_add_foreign_personal_tag_forbidden():
    with app.app_context():
        owner = _mk_user("at_own")
        other = _mk_user("at_oth")
        rec = _mk_recording(owner)
        foreign_tag = _mk_tag(other)
        client = app.test_client()
        _login(client, owner)
        resp = client.post(f"/api/recordings/{rec.id}/tags",
                           json={"tag_id": foreign_tag.id})
        assert resp.status_code == 403
        _cleanup(rec, foreign_tag, owner, other)


def test_remove_tag_from_recording():
    with app.app_context():
        user = _mk_user("rt_ok")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        tag = _mk_tag(user)
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=1))
        db.session.commit()
        resp = client.delete(f"/api/recordings/{rec.id}/tags/{tag.id}")
        assert resp.status_code == 200
        assert RecordingTag.query.filter_by(recording_id=rec.id, tag_id=tag.id).count() == 0
        _cleanup(rec, tag, user)


def test_remove_tag_not_on_recording_404():
    with app.app_context():
        user = _mk_user("rt_404")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        tag = _mk_tag(user)
        resp = client.delete(f"/api/recordings/{rec.id}/tags/{tag.id}")
        assert resp.status_code == 404
        _cleanup(rec, tag, user)


def test_reorder_recording_tags():
    with app.app_context():
        user = _mk_user("ro_ok")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        t1 = _mk_tag(user)
        t2 = _mk_tag(user)
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=t1.id, order=1))
        db.session.add(RecordingTag(recording_id=rec.id, tag_id=t2.id, order=2))
        db.session.commit()
        resp = client.put(f"/api/recordings/{rec.id}/tags/reorder",
                          json={"tag_ids": [t2.id, t1.id]})
        assert resp.status_code == 200
        a1 = RecordingTag.query.filter_by(recording_id=rec.id, tag_id=t1.id).first()
        a2 = RecordingTag.query.filter_by(recording_id=rec.id, tag_id=t2.id).first()
        assert a2.order == 1 and a1.order == 2
        for a in RecordingTag.query.filter_by(recording_id=rec.id).all():
            db.session.delete(a)
        db.session.commit()
        _cleanup(rec, t1, t2, user)


def test_reorder_missing_tag_ids_400():
    with app.app_context():
        user = _mk_user("ro_400")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        resp = client.put(f"/api/recordings/{rec.id}/tags/reorder", json={})
        assert resp.status_code == 400
        _cleanup(rec, user)


def test_reorder_forbidden_for_other_user():
    with app.app_context():
        owner = _mk_user("ro_own")
        other = _mk_user("ro_oth")
        rec = _mk_recording(owner)
        client = app.test_client()
        _login(client, other)
        resp = client.put(f"/api/recordings/{rec.id}/tags/reorder",
                          json={"tag_ids": []})
        assert resp.status_code == 403
        _cleanup(rec, owner, other)


# --------------------------------------------------------------------------- #
# bulk-tags
# --------------------------------------------------------------------------- #

def test_bulk_tags_add_and_remove():
    with app.app_context():
        user = _mk_user("bt_ok")
        client = app.test_client()
        _login(client, user)
        rec1 = _mk_recording(user)
        rec2 = _mk_recording(user)
        tag = _mk_tag(user)
        # Add
        resp = client.post("/api/recordings/bulk-tags", json={
            "recording_ids": [rec1.id, rec2.id], "tag_id": tag.id, "action": "add"})
        assert resp.status_code == 200
        assert set(resp.get_json()["affected_ids"]) == {rec1.id, rec2.id}
        assert RecordingTag.query.filter_by(tag_id=tag.id, recording_id=rec1.id).count() == 1
        # Remove
        resp2 = client.post("/api/recordings/bulk-tags", json={
            "recording_ids": [rec1.id], "tag_id": tag.id, "action": "remove"})
        assert resp2.status_code == 200
        assert RecordingTag.query.filter_by(tag_id=tag.id, recording_id=rec1.id).count() == 0
        for a in RecordingTag.query.filter(
                RecordingTag.recording_id.in_([rec1.id, rec2.id])).all():
            db.session.delete(a)
        db.session.commit()
        _cleanup(rec1, rec2, tag, user)


def test_bulk_tags_missing_args_400():
    with app.app_context():
        user = _mk_user("bt_400")
        client = app.test_client()
        _login(client, user)
        resp = client.post("/api/recordings/bulk-tags", json={"recording_ids": []})
        assert resp.status_code == 400
        _cleanup(user)


def test_bulk_tags_invalid_action_400():
    with app.app_context():
        user = _mk_user("bt_act")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        tag = _mk_tag(user)
        resp = client.post("/api/recordings/bulk-tags", json={
            "recording_ids": [rec.id], "tag_id": tag.id, "action": "frobnicate"})
        assert resp.status_code == 400
        _cleanup(rec, tag, user)


def test_bulk_tags_nonexistent_tag_404():
    with app.app_context():
        user = _mk_user("bt_404t")
        client = app.test_client()
        _login(client, user)
        rec = _mk_recording(user)
        resp = client.post("/api/recordings/bulk-tags", json={
            "recording_ids": [rec.id], "tag_id": 99999999, "action": "add"})
        assert resp.status_code == 404
        _cleanup(rec, user)


# --------------------------------------------------------------------------- #
# _resolve_transcription_model helper
# --------------------------------------------------------------------------- #

def test_resolve_transcription_model_no_allowlist_accepts():
    from src.api.recordings import _resolve_transcription_model
    with app.app_context(), \
         patch("src.config.app_config.TRANSCRIPTION_MODELS_AVAILABLE", []), \
         patch.object(SystemSetting, "get_setting", return_value=None):
        assert _resolve_transcription_model("whisper-large") == "whisper-large"


def test_resolve_transcription_model_falls_back_to_admin_default():
    from src.api.recordings import _resolve_transcription_model
    with app.app_context(), \
         patch.object(SystemSetting, "get_setting",
                      side_effect=lambda k, d=None:
                      "admin-default" if k == "transcription_default_model" else None):
        assert _resolve_transcription_model("") == "admin-default"
        assert _resolve_transcription_model(None) == "admin-default"


def test_resolve_transcription_model_drops_value_not_in_allowlist():
    from src.api.recordings import _resolve_transcription_model
    with app.app_context(), \
         patch("src.config.app_config.TRANSCRIPTION_MODELS_AVAILABLE", ["model-a"]), \
         patch.object(SystemSetting, "get_setting", return_value=None):
        assert _resolve_transcription_model("model-b") is None
        assert _resolve_transcription_model("model-a") == "model-a"


def test_resolve_transcription_model_accepts_from_db_visible_list():
    from src.api.recordings import _resolve_transcription_model
    import json as _json

    def _get(k, d=None):
        if k == "transcription_models_visible_json":
            return _json.dumps([{"value": "vis-model"}, "plain-model"])
        return None
    with app.app_context(), \
         patch("src.config.app_config.TRANSCRIPTION_MODELS_AVAILABLE", []), \
         patch.object(SystemSetting, "get_setting", side_effect=_get):
        assert _resolve_transcription_model("vis-model") == "vis-model"
        assert _resolve_transcription_model("plain-model") == "plain-model"
        assert _resolve_transcription_model("nope") is None
