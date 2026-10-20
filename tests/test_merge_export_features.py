"""Tests for the v0.9.6 export/merge features.

Covers:
  - #322 Markdown transcript export: ?format=md on /download/transcript
  - #321 Bulk export existing recordings to disk: /api/recordings/export-existing
  - #323 Merge recordings: /api/recordings/merge + src.services.recording_merge

External effects (ffmpeg, storage, job queue, file_exporter) are mocked so the
suite touches no real storage and shells out to nothing.

SHARED-DB: every assertion is scoped to the user/recordings the test created.
"""

import os
import sys
import uuid
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, Tag, RecordingTag

app.config["WTF_CSRF_ENABLED"] = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _mk_user(prefix="mx"):
    suffix = uuid.uuid4().hex[:8]
    user = User(username=f"{prefix[:11]}_{suffix}", email=f"{prefix}_{suffix}@local.test", password="x")
    db.session.add(user)
    db.session.commit()
    return user


def _login(client, user):
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


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


# --------------------------------------------------------------------------- #
# #322 — Markdown transcript export
# --------------------------------------------------------------------------- #

def test_transcript_download_markdown_format():
    with app.app_context():
        user = _mk_user()
        rec = _mk_recording(user, title="Weekly Sync", transcription="Alice: hello\nBob: hi")
        client = app.test_client()
        _login(client, user)

        resp = client.get(f"/recording/{rec.id}/download/transcript?format=md")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/markdown")
        assert resp.headers["Content-Disposition"].endswith('.md"')
        body = resp.get_data(as_text=True)
        assert body.startswith("# Weekly Sync")
        assert "Alice: hello" in body


def test_transcript_download_txt_default_unchanged():
    with app.app_context():
        user = _mk_user()
        rec = _mk_recording(user, title="Plain One", transcription="just some text")
        client = app.test_client()
        _login(client, user)

        resp = client.get(f"/recording/{rec.id}/download/transcript")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/plain")
        assert resp.headers["Content-Disposition"].endswith('.txt"')
        body = resp.get_data(as_text=True)
        # No markdown heading injected in txt mode
        assert not body.startswith("# ")


def test_transcript_download_invalid_format_falls_back_to_txt():
    with app.app_context():
        user = _mk_user()
        rec = _mk_recording(user, title="X", transcription="text")
        client = app.test_client()
        _login(client, user)
        resp = client.get(f"/recording/{rec.id}/download/transcript?format=bogus")
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/plain")


# --------------------------------------------------------------------------- #
# #321 — Bulk export existing recordings
# --------------------------------------------------------------------------- #

def test_bulk_export_disabled_returns_400():
    with app.app_context():
        user = _mk_user()
        client = app.test_client()
        _login(client, user)
        with patch("src.file_exporter.ENABLE_AUTO_EXPORT", False):
            resp = client.post("/api/recordings/export-existing")
        assert resp.status_code == 400


def test_bulk_export_exports_completed_recordings():
    with app.app_context():
        user = _mk_user()
        r1 = _mk_recording(user, transcription="a", status="COMPLETED")
        r2 = _mk_recording(user, summary="s", status="COMPLETED")
        # Not exportable: no transcription/summary
        _mk_recording(user, status="COMPLETED", transcription=None, summary=None)
        # Not exportable: not completed
        _mk_recording(user, transcription="x", status="PROCESSING")
        client = app.test_client()
        _login(client, user)

        exported_ids = []

        def _fake_export(rid):
            exported_ids.append(rid)
            return f"/data/exports/user/recording_{rid}.md"

        with patch("src.file_exporter.ENABLE_AUTO_EXPORT", True), \
             patch("src.file_exporter.export_recording", side_effect=_fake_export):
            resp = client.post("/api/recordings/export-existing")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["exported"] == 2
        assert data["total"] == 2
        assert set(exported_ids) == {r1.id, r2.id}


def test_bulk_export_only_touches_own_recordings():
    with app.app_context():
        me = _mk_user()
        other = _mk_user()
        mine = _mk_recording(me, transcription="a", status="COMPLETED")
        _mk_recording(other, transcription="b", status="COMPLETED")
        client = app.test_client()
        _login(client, me)

        seen = []
        with patch("src.file_exporter.ENABLE_AUTO_EXPORT", True), \
             patch("src.file_exporter.export_recording", side_effect=lambda rid: seen.append(rid) or "p"):
            resp = client.post("/api/recordings/export-existing")

        assert resp.status_code == 200
        assert seen == [mine.id]


# --------------------------------------------------------------------------- #
# #323 — Merge recordings
# --------------------------------------------------------------------------- #

class _FakeStoredObject:
    def __init__(self, locator, key):
        self.locator = locator
        self.key = key
        self.size = 1024
        self.content_type = None
        self.etag = None


class _FakeMaterialized:
    def __init__(self, path):
        self.local_path = path


class _FakeStorage:
    def __init__(self):
        self.deleted = []

    def exists(self, locator):
        return True

    @contextmanager
    def materialize(self, locator):
        yield _FakeMaterialized(f"/tmp/{uuid.uuid4().hex}.wav")

    def build_recording_key(self, original_filename, recording_id=None, *, now=None):
        return f"recordings/test/{recording_id}/{original_filename}"

    def upload_local_file(self, local_path, key, *, content_type=None, delete_source=False):
        return _FakeStoredObject(locator=f"local://{key}", key=key)

    def delete(self, locator, missing_ok=True):
        self.deleted.append(locator)
        return True


@contextmanager
def _endpoint_mocks():
    """Mock only the enqueue for the request-phase (create_merge_recording).
    The endpoint does NO ffmpeg — that is deferred to the worker."""
    with patch("src.services.recording_merge.get_storage_service", return_value=_FakeStorage()), \
         patch("src.services.recording_merge.job_queue.enqueue", return_value=1) as enqueue_mock:
        yield enqueue_mock


@contextmanager
def _worker_mocks():
    """Mock the worker-phase (run_merge_job): storage + ffmpeg concat + enqueue."""
    fake_storage = _FakeStorage()

    def _fake_concat(input_paths, output_path):
        with open(output_path, "wb") as f:
            f.write(b"\x00" * 2048)

    with patch("src.services.recording_merge.get_storage_service", return_value=fake_storage), \
         patch("src.services.recording_merge._concat_audio", side_effect=_fake_concat), \
         patch("src.services.recording_merge.job_queue.enqueue", return_value=1) as enqueue_mock:
        yield fake_storage, enqueue_mock


def test_merge_requires_two_recordings():
    with app.app_context():
        user = _mk_user()
        client = app.test_client()
        _login(client, user)
        r1 = _mk_recording(user)
        resp = client.post("/api/recordings/merge", json={"recording_ids": [r1.id]})
        assert resp.status_code == 400


def test_merge_endpoint_creates_processing_recording_and_enqueues_merge_job():
    with app.app_context():
        user = _mk_user()
        tag = Tag(name=f"t_{uuid.uuid4().hex[:6]}", user_id=user.id)
        db.session.add(tag)
        db.session.commit()
        r1 = _mk_recording(user, title="Part 1")
        db.session.add(RecordingTag(recording_id=r1.id, tag_id=tag.id, order=1))
        db.session.commit()
        r2 = _mk_recording(user, title="Part 2")
        client = app.test_client()
        _login(client, user)

        with _endpoint_mocks() as enqueue:
            resp = client.post("/api/recordings/merge",
                               json={"recording_ids": [r1.id, r2.id]})

        assert resp.status_code == 200, resp.get_data(as_text=True)
        data = resp.get_json()
        new_id = data["recording_id"]
        assert new_id not in (r1.id, r2.id)

        merged = db.session.get(Recording, new_id)
        assert merged.processing_source == "merge"
        # Non-terminal: enqueue() would set QUEUED; create sets it too.
        assert merged.status == "QUEUED"
        assert merged.mime_type == "audio/mp4"
        # No audio yet — concat happens on the worker
        assert merged.audio_path is None
        # Inherited the first source's tag
        assert tag.id in [t.id for t in merged.tags]
        # Sources preserved and untouched at request time
        assert db.session.get(Recording, r1.id) is not None
        assert db.session.get(Recording, r2.id) is not None
        # A 'merge' job was enqueued carrying the ordered source ids
        enqueue.assert_called_once()
        kwargs = enqueue.call_args.kwargs
        assert kwargs["job_type"] == "merge"
        assert kwargs["params"]["source_ids"] == [r1.id, r2.id]


def test_merge_endpoint_rejects_processing_source():
    with app.app_context():
        user = _mk_user()
        r1 = _mk_recording(user, title="Done", status="COMPLETED")
        r2 = _mk_recording(user, title="Busy", status="PROCESSING")
        client = app.test_client()
        _login(client, user)
        with _endpoint_mocks():
            resp = client.post("/api/recordings/merge",
                               json={"recording_ids": [r1.id, r2.id]})
        assert resp.status_code == 400
        assert "processing" in resp.get_json()["error"].lower()


def test_merge_endpoint_rejects_other_users_recording():
    with app.app_context():
        me = _mk_user()
        other = _mk_user()
        mine = _mk_recording(me, title="Mine")
        theirs = _mk_recording(other, title="Theirs")
        client = app.test_client()
        _login(client, me)
        with _endpoint_mocks():
            resp = client.post("/api/recordings/merge",
                               json={"recording_ids": [mine.id, theirs.id]})
        assert resp.status_code == 400


def test_merge_endpoint_custom_title_applied():
    with app.app_context():
        user = _mk_user()
        r1 = _mk_recording(user, title="A")
        r2 = _mk_recording(user, title="B")
        client = app.test_client()
        _login(client, user)
        with _endpoint_mocks():
            resp = client.post("/api/recordings/merge",
                               json={"recording_ids": [r1.id, r2.id], "title": "Combined Call"})
        assert resp.status_code == 200
        merged = db.session.get(Recording, resp.get_json()["recording_id"])
        assert merged.title == "Combined Call"


def test_run_merge_job_concats_uploads_and_enqueues_transcribe():
    from src.services.recording_merge import create_merge_recording, run_merge_job
    with app.app_context():
        user = _mk_user()
        r1 = _mk_recording(user, title="A")
        r2 = _mk_recording(user, title="B")
        with _endpoint_mocks():
            merged = create_merge_recording(user, [r1.id, r2.id])

        params = {"source_ids": [r1.id, r2.id], "delete_originals": False}
        with _worker_mocks() as (storage, enqueue):
            run_merge_job(merged, params)

        db.session.refresh(merged)
        assert merged.audio_path.startswith("local://")
        assert merged.file_size and merged.file_size > 0
        # Sources kept
        assert db.session.get(Recording, r1.id) is not None
        # Transcription queued
        enqueue.assert_called_once()
        assert enqueue.call_args.kwargs["job_type"] == "transcribe"


def test_run_merge_job_delete_originals_removes_sources_after_concat():
    from src.services.recording_merge import create_merge_recording, run_merge_job
    with app.app_context():
        user = _mk_user()
        r1 = _mk_recording(user, title="A")
        r2 = _mk_recording(user, title="B")
        with _endpoint_mocks():
            merged = create_merge_recording(user, [r1.id, r2.id], delete_originals=True)

        params = {"source_ids": [r1.id, r2.id], "delete_originals": True}
        with _worker_mocks():
            run_merge_job(merged, params)

        assert db.session.get(Recording, r1.id) is None
        assert db.session.get(Recording, r2.id) is None
        # Merged recording itself survives
        assert db.session.get(Recording, merged.id) is not None


def test_run_merge_job_can_keep_the_originals_without_their_audio():
    """#413: the sources stay with their transcripts; their audio is removed
    only after the merged file is stored, so the audio is never lost."""
    from src.services.recording_merge import create_merge_recording, run_merge_job
    with app.app_context():
        user = _mk_user()
        r1 = _mk_recording(user, title="A")
        r2 = _mk_recording(user, title="B")
        r1.transcription = "kept words"
        db.session.commit()
        with _endpoint_mocks() as enqueue:
            merged = create_merge_recording(user, [r1.id, r2.id], remove_original_audio=True)
        assert enqueue.call_args.kwargs["params"]["remove_original_audio"] is True

        params = {"source_ids": [r1.id, r2.id], "remove_original_audio": True}
        removed = []
        fake_retention_storage = MagicMock()
        fake_retention_storage.exists.return_value = True
        fake_retention_storage.delete.side_effect = lambda path, **kw: removed.append(path)
        with _worker_mocks(), patch("src.services.retention.get_storage_service", return_value=fake_retention_storage):
            run_merge_job(merged, params)

        for rid in (r1.id, r2.id):
            rec = db.session.get(Recording, rid)
            assert rec is not None and rec.audio_deleted_at is not None
        assert db.session.get(Recording, r1.id).transcription == "kept words"
        assert len(removed) == 2
        assert db.session.get(Recording, merged.id).audio_path.startswith("local://")


def test_a_failed_merge_leaves_the_originals_audio():
    from src.services.recording_merge import create_merge_recording, run_merge_job, MergeError
    import pytest
    with app.app_context():
        user = _mk_user()
        r1 = _mk_recording(user, title="A")
        r2 = _mk_recording(user, title="B")
        with _endpoint_mocks():
            merged = create_merge_recording(user, [r1.id, r2.id], remove_original_audio=True)
        params = {"source_ids": [r1.id, r2.id], "remove_original_audio": True}
        with _worker_mocks() as (storage, _enqueue):
            storage.upload_local_file = MagicMock(side_effect=OSError("disk full"))
            with pytest.raises(MergeError):
                run_merge_job(merged, params)
        for rid in (r1.id, r2.id):
            assert db.session.get(Recording, rid).audio_deleted_at is None


def test_run_merge_job_empty_output_raises():
    from src.services.recording_merge import create_merge_recording, run_merge_job, MergeError
    import pytest
    with app.app_context():
        user = _mk_user()
        r1 = _mk_recording(user, title="A")
        r2 = _mk_recording(user, title="B")
        with _endpoint_mocks():
            merged = create_merge_recording(user, [r1.id, r2.id])

        def _empty_concat(input_paths, output_path):
            open(output_path, "wb").close()  # zero-byte

        with patch("src.services.recording_merge.get_storage_service", return_value=_FakeStorage()), \
             patch("src.services.recording_merge._concat_audio", side_effect=_empty_concat), \
             patch("src.services.recording_merge.job_queue.enqueue", return_value=1):
            with pytest.raises(MergeError):
                run_merge_job(merged, {"source_ids": [r1.id, r2.id]})


# --------------------------------------------------------------------------- #
# #323 — Merge-from-recording (stitch straight into a merge, no double transcribe)
# --------------------------------------------------------------------------- #

def test_create_merge_recording_require_settled_false_accepts_processing_source():
    from src.services.recording_merge import create_merge_recording, MergeError
    with app.app_context():
        user = _mk_user()
        done = _mk_recording(user, title="Existing", status="COMPLETED")
        # The freshly-stitched clip is NOT completed yet, but its audio is ready.
        clip = _mk_recording(user, title="Clip", status="PROCESSING")
        # With the guard on, the clip is rejected...
        with _endpoint_mocks():
            import pytest
            with pytest.raises(MergeError):
                create_merge_recording(user, [done.id, clip.id])
        # ...with require_settled off (trusted stitch path), it is accepted.
        with _endpoint_mocks() as enqueue:
            merged = create_merge_recording(user, [done.id, clip.id], require_settled=False)
        assert merged.processing_source == "merge"
        assert enqueue.call_args.kwargs["job_type"] == "merge"
        assert enqueue.call_args.kwargs["params"]["source_ids"] == [done.id, clip.id]


def test_try_kickoff_merge_substitutes_self_and_enqueues():
    from src.services.recording_stitch import _try_kickoff_merge
    with app.app_context():
        user = _mk_user()
        existing = _mk_recording(user, title="Existing", status="COMPLETED")
        clip = _mk_recording(user, title="Clip", status="PROCESSING")  # just stitched
        metadata = {"merge_intent": {"order": [existing.id, "__self__"], "delete_originals": True, "title": "Combined"}}
        with _endpoint_mocks() as enqueue:
            handled = _try_kickoff_merge(clip, user.id, metadata)
        assert handled is True
        # A merge job was enqueued with the clip's real id substituted for __self__
        assert enqueue.call_args.kwargs["job_type"] == "merge"
        assert enqueue.call_args.kwargs["params"]["source_ids"] == [existing.id, clip.id]
        assert enqueue.call_args.kwargs["params"]["delete_originals"] is True


def test_try_kickoff_merge_no_intent_falls_through():
    from src.services.recording_stitch import _try_kickoff_merge
    with app.app_context():
        user = _mk_user()
        clip = _mk_recording(user, title="Clip", status="PROCESSING")
        assert _try_kickoff_merge(clip, user.id, {}) is False
        # order without the __self__ placeholder is ignored (falls through)
        assert _try_kickoff_merge(clip, user.id, {"merge_intent": {"order": [1, 2]}}) is False


def test_try_kickoff_merge_other_users_source_falls_through():
    from src.services.recording_stitch import _try_kickoff_merge
    with app.app_context():
        me = _mk_user()
        other = _mk_user()
        clip = _mk_recording(me, title="Clip", status="PROCESSING")
        theirs = _mk_recording(other, title="Theirs", status="COMPLETED")
        metadata = {"merge_intent": {"order": [theirs.id, "__self__"], "delete_originals": True}}
        with _endpoint_mocks():
            # create_merge_recording raises MergeError (not owned) -> falls through
            assert _try_kickoff_merge(clip, me.id, metadata) is False


# --------------------------------------------------------------------------- #
# #323 — Merge metadata preservation (notes pick, participants/tags union)
# --------------------------------------------------------------------------- #

def test_merge_notes_default_is_first_source():
    from src.services.recording_merge import create_merge_recording
    with app.app_context():
        user = _mk_user()
        a = _mk_recording(user, title="A", notes="alpha notes")
        b = _mk_recording(user, title="B", notes="beta notes")
        with _endpoint_mocks():
            merged = create_merge_recording(user, [a.id, b.id])
        assert merged.notes == "alpha notes"


def test_merge_notes_explicit_source():
    from src.services.recording_merge import create_merge_recording
    with app.app_context():
        user = _mk_user()
        a = _mk_recording(user, title="A", notes="alpha notes")
        b = _mk_recording(user, title="B", notes="beta notes")
        with _endpoint_mocks():
            merged = create_merge_recording(user, [a.id, b.id], notes_source_id=b.id)
        assert merged.notes == "beta notes"


def test_merge_notes_none_keeps_no_notes():
    from src.services.recording_merge import create_merge_recording
    with app.app_context():
        user = _mk_user()
        a = _mk_recording(user, title="A", notes="alpha notes")
        b = _mk_recording(user, title="B", notes="beta notes")
        with _endpoint_mocks():
            merged = create_merge_recording(user, [a.id, b.id], notes_source_id=None)
        assert merged.notes is None


def test_merge_participants_unioned_dedup_case_insensitive():
    from src.services.recording_merge import create_merge_recording
    with app.app_context():
        user = _mk_user()
        a = _mk_recording(user, title="A", participants="Alice, Bob")
        b = _mk_recording(user, title="B", participants="bob, Carol")
        with _endpoint_mocks():
            merged = create_merge_recording(user, [a.id, b.id])
        parts = [p.strip() for p in (merged.participants or "").split(",") if p.strip()]
        assert parts == ["Alice", "Bob", "Carol"]


def test_merge_tags_unioned_first_source_first():
    from src.services.recording_merge import create_merge_recording
    with app.app_context():
        user = _mk_user()
        t1 = Tag(name=f"t1_{uuid.uuid4().hex[:6]}", user_id=user.id)
        t2 = Tag(name=f"t2_{uuid.uuid4().hex[:6]}", user_id=user.id)
        db.session.add_all([t1, t2])
        db.session.commit()
        a = _mk_recording(user, title="A")
        db.session.add(RecordingTag(recording_id=a.id, tag_id=t1.id, order=1))
        b = _mk_recording(user, title="B")
        db.session.add(RecordingTag(recording_id=b.id, tag_id=t2.id, order=1))
        db.session.add(RecordingTag(recording_id=b.id, tag_id=t1.id, order=2))  # dup of a's tag
        db.session.commit()
        with _endpoint_mocks():
            merged = create_merge_recording(user, [a.id, b.id])
        assert [t.id for t in merged.tags] == [t1.id, t2.id]


def test_merge_endpoint_passes_notes_source_id():
    with app.app_context():
        user = _mk_user()
        a = _mk_recording(user, title="A", notes="alpha")
        b = _mk_recording(user, title="B", notes="beta")
        client = app.test_client()
        _login(client, user)
        with _endpoint_mocks():
            resp = client.post("/api/recordings/merge",
                               json={"recording_ids": [a.id, b.id], "notes_source_id": b.id})
        assert resp.status_code == 200
        merged = db.session.get(Recording, resp.get_json()["recording_id"])
        assert merged.notes == "beta"


def test_merge_prompt_variables_union_keeps_all_keys():
    from src.services.recording_merge import create_merge_recording
    with app.app_context():
        user = _mk_user()
        a = _mk_recording(user, title="A", prompt_variables={"agenda": "AAA"})
        b = _mk_recording(user, title="B", prompt_variables={"attendees": "BBB"})
        with _endpoint_mocks():
            merged = create_merge_recording(user, [a.id, b.id])
        assert merged.prompt_variables == {"agenda": "AAA", "attendees": "BBB"}


def test_merge_prompt_variables_conflict_reference_wins():
    from src.services.recording_merge import create_merge_recording
    with app.app_context():
        user = _mk_user()
        a = _mk_recording(user, title="A", prompt_variables={"agenda": "AAA"})
        b = _mk_recording(user, title="B", prompt_variables={"agenda": "BBB"})
        with _endpoint_mocks():
            merged = create_merge_recording(user, [a.id, b.id], notes_source_id=b.id)
        assert merged.prompt_variables == {"agenda": "BBB"}


def test_merge_prompt_variables_conflict_default_reference_is_first():
    from src.services.recording_merge import create_merge_recording
    with app.app_context():
        user = _mk_user()
        a = _mk_recording(user, title="A", prompt_variables={"agenda": "AAA"})
        b = _mk_recording(user, title="B", prompt_variables={"agenda": "BBB"})
        with _endpoint_mocks():
            merged = create_merge_recording(user, [a.id, b.id])  # default reference = first
        assert merged.prompt_variables == {"agenda": "AAA"}


def test_merge_prompt_variables_kept_even_when_notes_dropped():
    from src.services.recording_merge import create_merge_recording
    with app.app_context():
        user = _mk_user()
        a = _mk_recording(user, title="A", notes="a", prompt_variables={"agenda": "AAA"})
        b = _mk_recording(user, title="B")
        with _endpoint_mocks():
            # "keep no notes" still unions prompt variables (they ride with tags).
            merged = create_merge_recording(user, [a.id, b.id], notes_source_id=None)
        assert merged.notes is None
        assert merged.prompt_variables == {"agenda": "AAA"}
