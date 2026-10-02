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
    with patch("src.services.job_queue.job_queue.enqueue", return_value=1):
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
