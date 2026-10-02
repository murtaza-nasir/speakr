"""The share target runs the standard upload ingestion (#412 audit P13).

It used to save straight into UPLOAD_FOLDER, so a shared file skipped the
storage backend (S3), the file hash and duplicate check, the meeting date and
the MIME type. It now calls ingest_uploaded_recording like every upload.

SHARED-DB: the user and recording are removed afterwards.
"""

import io
import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import Recording, User

app.config["WTF_CSRF_ENABLED"] = False



from contextlib import contextmanager as _contextmanager


@_contextmanager
def _no_media_tools():
    """The share target runs the standard upload ingestion (#412), which probes
    and converts audio with ffmpeg. These tests send placeholder bytes, so the
    media steps are stubbed as in tests/test_cov_recordings_write.py."""
    from unittest.mock import MagicMock, patch as _patch

    def _convert(filepath, **kwargs):
        r = MagicMock()
        r.output_path = filepath
        r.was_converted = r.was_compressed = False
        r.original_codec = r.final_codec = "opus"
        r.size_reduction_percent = 0.0
        return r
    with _patch("src.api.recordings.convert_if_needed", side_effect=_convert), \
         _patch("src.api.recordings.get_codec_info",
                return_value={"has_video": False, "audio_codec": "opus", "video_codec": None, "duration": 5.0}), \
         _patch("src.api.recordings.get_duration", return_value=5.0), \
         _patch("src.api.recordings.get_creation_date", return_value=None):
        yield


def test_shared_file_gets_hash_meeting_date_and_source():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"shi_{s}", email=f"shi_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        uid = user.id
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    with _no_media_tools(), patch("src.services.job_queue.job_queue.enqueue", return_value=1):
        resp = client.post("/share-target",
                           data={"text": "From my phone",
                                 "shared_audio": (io.BytesIO(b"fake-audio-bytes-" + s.encode()), "memo.webm")},
                           content_type="multipart/form-data")
    assert resp.status_code == 302 and "share_target=ok" in resp.headers["Location"], resp.headers.get("Location")
    with app.app_context():
        rec = Recording.query.filter_by(user_id=uid).one()
        try:
            assert rec.processing_source == "share_target"
            assert rec.file_hash, "the duplicate check needs a file hash"
            assert rec.meeting_date is not None
            assert rec.notes == "From my phone"
        finally:
            from src.services.storage import get_storage_service
            try:
                get_storage_service().delete(rec.audio_path)
            except Exception:
                pass
            db.session.delete(rec)
            db.session.delete(db.session.get(User, uid))
            db.session.commit()
