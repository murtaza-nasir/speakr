"""Browser-recorder filenames through POST /upload (#486).

The recorder names files with toISOString(), a UTC time ending in Z. With
filename date parsing on, the meeting date must be that UTC time, not the
time shifted again by the browser timezone.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording

app.config["WTF_CSRF_ENABLED"] = False

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _webm_bytes():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "a.webm")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1",
                        "-c:a", "libopus", path], check=True)
        with open(path, "rb") as f:
            return f.read()


@pytest.mark.parametrize("filename, expected", [
    ("recording-2026-10-25T11-43-00-000Z.webm", datetime(2026, 10, 25, 11, 43)),
    ("recording-2026-10-25T19-43-00.webm", datetime(2026, 10, 25, 11, 43)),
])
def test_recorder_filename_meeting_date(filename, expected):
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f"fd486_{suffix}", email=f"fd486_{suffix}@local.test", password="x",
                    parse_filename_dates=True, filename_date_pattern="auto")
        db.session.add(user)
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.id)
            sess["_fresh"] = True

        data = {
            "file": (io.BytesIO(_webm_bytes()), filename),
            "client_tz_offset": "-480",
            "client_timezone": "Asia/Shanghai",
            "file_last_modified": str(int(datetime(2026, 10, 25, 11, 43, 30).timestamp() * 1000)),
        }
        with patch("src.services.job_queue.job_queue.enqueue", return_value=1):
            resp = client.post("/upload", data=data, content_type="multipart/form-data")
        assert resp.status_code in (200, 202), resp.get_data(as_text=True)[:300]

        rec = Recording.query.filter_by(user_id=user.id).one()
        assert rec.meeting_date == expected
