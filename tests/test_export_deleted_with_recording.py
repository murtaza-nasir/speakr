"""The automatically exported file can be deleted with its recording
(privacy review A3).

With automatic export on, the export of a deleted recording was renamed with
the [deleted]_ prefix and its content (transcript and summary) kept. That
stays the default, because the export folder is usually a notes vault whose
files the user edits and wants to keep. With auto_export_delete_with_recording
(AUTO_EXPORT_DELETE_WITH_RECORDING) on, the file is deleted with the recording.

SHARED-DB: users, recordings and the setting are restored. Exports go to a
temporary directory.
"""

import os
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.utils import secure_filename

from src.app import app, db
from src.models import Recording, SystemSetting, User
from src import file_exporter
from src.utils.system_settings import ENV_BACKED_SETTINGS

KEY = 'auto_export_delete_with_recording'
ENV = 'AUTO_EXPORT_DELETE_WITH_RECORDING'
CONTENT = "transcript of the marmot-export-marker visit"


@contextmanager
def _export_env():
    tmpdir = tempfile.mkdtemp(prefix="speakr_export_test_")
    try:
        with patch.object(file_exporter, "ENABLE_AUTO_EXPORT", True), \
             patch.object(file_exporter, "AUTO_EXPORT_DIR", tmpdir):
            yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def world(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    with app.app_context():
        row = SystemSetting.query.filter_by(key=KEY).first()
        saved = (row.value, row.setting_type) if row else None
        SystemSetting.set_setting(KEY, None, ENV_BACKED_SETTINGS[KEY][3], 'boolean')
        s = uuid.uuid4().hex[:8]
        user = User(username=f"xd_{s}", email=f"xd_{s}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        uid, username = user.id, user.username
    with _export_env() as tmpdir:
        yield dict(user=uid, dir=os.path.join(tmpdir, secure_filename(username)), base=tmpdir)
    with app.app_context():
        db.session.rollback()
        Recording.query.filter_by(user_id=uid).delete()
        User.query.filter_by(id=uid).delete()
        db.session.commit()
        if saved is None:
            row = SystemSetting.query.filter_by(key=KEY).first()
            if row:
                db.session.delete(row)
                db.session.commit()
        else:
            SystemSetting.set_setting(KEY, saved[0], None, saved[1])


def _exported_recording(world, name="Visit notes"):
    """A recording with its export file on disk; returns (id, path of the file)."""
    with app.app_context():
        rec = Recording(user_id=world["user"], title=name, status="COMPLETED", original_filename="x.mp3",
                        audio_path="local://recordings/x.mp3", transcription=CONTENT)
        rec.export_filename = name
        db.session.add(rec)
        db.session.commit()
        rec_id = rec.id
    os.makedirs(world["dir"], exist_ok=True)
    path = os.path.join(world["dir"], f"{name}.md")
    with open(path, "w") as f:
        f.write(CONTENT)
    return rec_id, path


def _delete(rec_id):
    """Production order: the row is deleted before the export is handled."""
    with app.app_context():
        db.session.delete(db.session.get(Recording, rec_id))
        db.session.commit()
        return file_exporter.mark_export_as_deleted(rec_id)


def _files(world):
    return sorted(os.listdir(world["dir"]))


def test_default_is_off():
    assert ENV_BACKED_SETTINGS[KEY][:3] == (ENV, False, 'boolean')


def test_by_default_the_export_is_renamed_and_its_content_kept(world):
    rec_id, path = _exported_recording(world)
    new_path = _delete(rec_id)
    assert os.path.basename(new_path) == "[deleted]_Visit notes.md"
    assert _files(world) == ["[deleted]_Visit notes.md"]
    assert open(new_path).read() == CONTENT


@pytest.mark.parametrize("how", ["env", "setting"])
def test_the_export_is_deleted_with_the_recording_when_asked(world, monkeypatch, how):
    if how == "env":
        monkeypatch.setenv(ENV, 'true')
    else:
        monkeypatch.setenv(ENV, 'false')
        with app.app_context():
            SystemSetting.set_setting(KEY, 'true', setting_type='boolean')
    rec_id, path = _exported_recording(world)
    other_id, other_path = _exported_recording(world, name="Another visit")
    assert _delete(rec_id) is None
    assert not os.path.exists(path)
    assert _files(world) == ["Another visit.md"], "only the export of the deleted recording is removed"
    # Nothing with the content is left under the export folder.
    left = [os.path.join(folder, name) for folder, _, names in os.walk(world["base"]) for name in names]
    assert left == [other_path]


def test_legacy_file_name_is_deleted_too(world, monkeypatch):
    monkeypatch.setenv(ENV, 'true')
    with app.app_context():
        rec = Recording(user_id=world["user"], title="t", status="COMPLETED", original_filename="x.mp3",
                        audio_path="local://recordings/x.mp3", transcription=CONTENT)
        db.session.add(rec)
        db.session.commit()
        rec_id = rec.id
    os.makedirs(world["dir"], exist_ok=True)
    legacy = os.path.join(world["dir"], f"recording_{rec_id}.md")
    with open(legacy, "w") as f:
        f.write(CONTENT)
    assert _delete(rec_id) is None
    assert _files(world) == []


def test_files_renamed_before_the_setting_was_turned_on_are_left_alone(world, monkeypatch):
    rec_id, _ = _exported_recording(world)
    kept = _delete(rec_id)
    monkeypatch.setenv(ENV, 'true')
    second_id, _ = _exported_recording(world, name="Second")
    assert _delete(second_id) is None
    assert _files(world) == [os.path.basename(kept)]


def test_deleting_a_recording_through_the_service_deletes_its_export(world, monkeypatch):
    monkeypatch.setenv(ENV, 'true')
    rec_id, path = _exported_recording(world)
    with app.app_context(), patch("src.services.webhook_dispatch.emit_webhook_event"):
        from src.services.recording_deletion import delete_recording_completely
        delete_recording_completely(db.session.get(Recording, rec_id))
        assert db.session.get(Recording, rec_id) is None
    assert not os.path.exists(path) and _files(world) == []
