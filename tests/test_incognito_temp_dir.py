"""Incognito temporary files have a folder of their own, and leftovers of a
crash are removed (privacy review A6).

The upload, its converted copies and its chunk folder are written to one
folder per request under INCOGNITO_TEMP_DIR and removed with it. An entry left
by a request that did not end normally is removed by cleanup_stale (at startup
in every process and every hour) once nothing in it was written for
incognito_temp_max_age_hours, so that a request in flight in another worker
keeps its files.

SHARED-DB: the user is removed afterwards.
"""

import io
import os
import sys
import tempfile
import threading
import time
import uuid
import wave
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User
import src.api.recordings as rec_api
import src.tasks.processing as proc
from src.services import incognito_temp
from src.services.transcription.base import ConnectorSpecifications, TranscriptionResponse
from src.utils.system_settings import ENV_BACKED_SETTINGS

app.config["WTF_CSRF_ENABLED"] = False
HOUR = 3600


@pytest.fixture
def folders(tmp_path, monkeypatch):
    system_tmp = tmp_path / "tmp"
    system_tmp.mkdir()
    root = tmp_path / "incognito"
    monkeypatch.setenv("INCOGNITO_TEMP_DIR", str(root))
    monkeypatch.delenv("INCOGNITO_TEMP_MAX_AGE_HOURS", raising=False)
    with patch.object(tempfile, "tempdir", str(system_tmp)):
        yield SimpleNamespace(system=system_tmp, root=root)


@pytest.fixture
def client():
    with app.app_context():
        s = uuid.uuid4().hex[:8]
        user = User(username=f"inct_{s}", email=f"inct_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        uid = user.id
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(uid)
    yield c
    with app.app_context():
        db.session.rollback()
        db.session.delete(db.session.get(User, uid))
        db.session.commit()


def _wav():
    data = io.BytesIO()
    with wave.open(data, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 16000)
    data.seek(0)
    return data


class _Connector:
    specifications = ConnectorSpecifications()
    supports_diarization = False
    default_diarize = False
    PROVIDER_NAME = "fake"

    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def transcribe(self, request):
        self.sent.append(request.audio_file.name)
        if self.fail:
            raise self.fail
        return TranscriptionResponse(text="a transcript of more than ten characters")


def _upload(client, connector, chunked=False):
    seen = {}

    def create_chunks(path, temp_dir, specs=None):
        seen["chunk_dir"] = temp_dir
        target = os.path.join(temp_dir, "chunk_0.mp3")
        with open(target, "wb") as f:
            f.write(b"chunk audio")
        return [{"index": 0, "start_time": 0.0, "end_time": 1.0, "duration": 1.0, "size_mb": 0.1,
                 "filename": "chunk_0.mp3", "path": target}]

    registry = SimpleNamespace(get_active_connector=lambda: connector)
    with patch.object(rec_api, "ENABLE_INCOGNITO_MODE", True), \
         patch("src.services.transcription.get_registry", return_value=registry), \
         patch.object(proc.llm_config, "is_configured", return_value=False), \
         patch.object(proc.chunking_service, "needs_chunking", return_value=chunked), \
         patch.object(proc.chunking_service, "create_chunks", side_effect=create_chunks), \
         patch("src.audio_chunking.extract_speaker_samples", return_value=[]):
        response = client.post("/api/recordings/incognito", data={"file": (_wav(), "visit.wav"), "language": "en"},
                               content_type="multipart/form-data")
    return response, seen


def _everything(folder):
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*")) if folder.exists() else []


def test_upload_is_written_to_a_request_folder_and_removed(client, folders):
    connector = _Connector()
    response, _ = _upload(client, connector)
    assert response.status_code == 200, response.get_data(as_text=True)
    (sent,) = connector.sent
    request_dir = os.path.dirname(sent)
    assert os.path.dirname(request_dir) == str(folders.root)
    assert os.path.basename(request_dir).startswith(incognito_temp.REQUEST_PREFIX)
    assert "visit" not in sent, "the name given by the user is not part of the path"
    assert _everything(folders.root) == []
    assert _everything(folders.system) == [], "nothing of the request in the system temporary folder"


def test_chunk_folder_is_inside_the_request_folder(client, folders):
    connector = _Connector()
    response, seen = _upload(client, connector, chunked=True)
    assert response.status_code == 200, response.get_data(as_text=True)
    assert seen["chunk_dir"].startswith(str(folders.root) + os.sep)
    assert os.path.dirname(os.path.dirname(seen["chunk_dir"])) == str(folders.root)
    assert _everything(folders.root) == [] and _everything(folders.system) == []


def test_request_folder_is_removed_after_a_failed_transcription(client, folders):
    response, _ = _upload(client, _Connector(fail=RuntimeError("service down")))
    assert response.status_code == 500
    assert _everything(folders.root) == [] and _everything(folders.system) == []


def test_chunks_of_an_ordinary_file_stay_in_the_system_temporary_folder(folders, tmp_path):
    ordinary = tmp_path / "uploads" / "a.wav"
    ordinary.parent.mkdir()
    ordinary.write_bytes(b"x")
    assert incognito_temp.dir_for_temp_files(str(ordinary)) is None
    request_dir = incognito_temp.new_request_dir()
    inside = os.path.join(request_dir, "tmpabc.wav")
    open(inside, "wb").close()
    assert incognito_temp.dir_for_temp_files(inside) == request_dir
    assert oct(os.stat(request_dir).st_mode & 0o777) == "0o700"
    assert oct(os.stat(folders.root).st_mode & 0o777) == "0o700"


# --- leftovers of a crash -----------------------------------------------------

def _leftover(folders, age_hours, with_chunks=True):
    """What a killed request leaves: its folder with the upload, a copy and chunks."""
    request_dir = incognito_temp.new_request_dir()
    names = ["tmpupload.wav", "tmpupload.mp3"]
    if with_chunks:
        os.makedirs(os.path.join(request_dir, "tmpchunks"))
        names.append(os.path.join("tmpchunks", "chunk_0.mp3"))
    for name in names:
        with open(os.path.join(request_dir, name), "wb") as f:
            f.write(b"audio")
    stamp = time.time() - age_hours * HOUR
    for folder, dirs, files in os.walk(request_dir, topdown=False):
        for name in files + dirs:
            os.utime(os.path.join(folder, name), (stamp, stamp))
    os.utime(request_dir, (stamp, stamp))
    return request_dir


def test_default_age_is_six_hours():
    assert ENV_BACKED_SETTINGS['incognito_temp_max_age_hours'][:3] == ('INCOGNITO_TEMP_MAX_AGE_HOURS', 6, 'integer')
    with app.app_context():
        assert incognito_temp.max_age_seconds() == 6 * HOUR


def test_leftovers_older_than_the_threshold_are_removed_and_recent_ones_kept(folders):
    old = _leftover(folders, age_hours=7)
    recent = _leftover(folders, age_hours=5)
    stray = folders.root / "tmpstray.wav"
    stray.write_bytes(b"audio")
    os.utime(stray, (time.time() - 30 * HOUR,) * 2)
    with app.app_context():
        assert incognito_temp.cleanup_stale() == 2
    assert not os.path.exists(old) and not stray.exists()
    assert os.path.isdir(recent) and len(_everything(folders.root)) == 5


def test_a_request_in_flight_keeps_its_files(folders):
    """An old folder with one file written a moment ago belongs to a running request."""
    request_dir = _leftover(folders, age_hours=20)
    with open(os.path.join(request_dir, "tmpchunks", "chunk_1.mp3"), "wb") as f:
        f.write(b"being written by another worker")
    with app.app_context():
        assert incognito_temp.cleanup_stale() == 0
    assert os.path.exists(os.path.join(request_dir, "tmpupload.wav"))


def test_env_threshold_applies(folders, monkeypatch):
    request_dir = _leftover(folders, age_hours=2)
    with app.app_context():
        assert incognito_temp.cleanup_stale() == 0
    monkeypatch.setenv("INCOGNITO_TEMP_MAX_AGE_HOURS", "1")
    with app.app_context():     # a setting is read once per request
        assert incognito_temp.cleanup_stale() == 1
    assert not os.path.exists(request_dir)


def test_workers_starting_together_sweep_without_error(folders):
    for _ in range(12):
        _leftover(folders, age_hours=10)
    keep = _leftover(folders, age_hours=1)
    results, errors = [], []

    def sweep():
        try:
            results.append(incognito_temp.cleanup_stale(max_age=6 * HOUR))
        except Exception as e:           # cleanup_stale never raises
            errors.append(e)

    threads = [threading.Thread(target=sweep) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert os.listdir(folders.root) == [os.path.basename(keep)]
    assert sum(results) >= 12


def test_a_link_in_the_folder_is_removed_without_following_it(folders, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "keep.txt"
    precious.write_text("not an incognito file")
    os.makedirs(folders.root, exist_ok=True)
    link = folders.root / "req-link"
    os.symlink(outside, link)
    old = time.time() - 50 * HOUR
    os.utime(link, (old, old), follow_symlinks=False)
    os.utime(precious, (old, old))
    assert incognito_temp.cleanup_stale(max_age=6 * HOUR) == 1
    assert not os.path.lexists(link) and precious.exists()


def test_the_sweep_never_raises(folders, monkeypatch):
    monkeypatch.setenv("INCOGNITO_TEMP_DIR", "/proc/speakr-cannot-create-this")
    assert incognito_temp.cleanup_stale(max_age=1) == 0


def test_the_hourly_cleanup_and_the_startup_call_the_sweep():
    import inspect
    import src.config.startup as startup
    assert "cleanup_stale" in inspect.getsource(startup.initialize_recording_session_cleanup)
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "app.py")) as f:
        assert "_cleanup_incognito_temp()" in f.read()
