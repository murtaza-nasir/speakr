"""Harness for the ingestion characterization tests (D556 refactor, step 0).

The tests pin what ``ingest_uploaded_recording`` does today, seen from each
entry point: the HTTP answer, the Recording row, the stored object, the queued
jobs, the ``recording.created`` webhooks and what is left in the staging
directory. Every case runs against the real ffprobe/ffmpeg binaries and the
real local storage, and turns what it saw into a normalized "observation"
(ids, timestamps, paths and hashes replaced by placeholders). The expected
observations live in ``tests/fixtures/ingestion_characterization.json``.

To regenerate the fixture after an INTENDED behaviour change, run the
characterization modules with ``INGEST_CHAR_RECORD=1`` and review the diff
of the JSON file line by line: every changed line is a changed behaviour.
"""

import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import uuid
import warnings
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import g
from flask.testing import FlaskClient

from src.app import app
from src.database import db
from src.models import (APIToken, Folder, ProcessingJob, Recording, RecordingTag, SystemSetting, Tag,
                        User)
from src.services.job_queue import job_queue
from src.services.storage import get_storage_service
from src.utils.token_auth import hash_token

app.config["WTF_CSRF_ENABLED"] = False

FIXTURE = Path(__file__).parent / "fixtures" / "ingestion_characterization.json"
RECORD = os.environ.get("INGEST_CHAR_RECORD") == "1"

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg and ffprobe are not installed",
)

MB = 1024 * 1024


# --- media -------------------------------------------------------------------

def _ffmpeg(out, *args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, str(out)], check=True, capture_output=True)
    return out.read_bytes()


def _make_media(d):
    sine = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    tiny_video = ["-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=2"]
    rnd = random.Random(556)
    m = {
        "wav": _ffmpeg(d / "a.wav", *sine, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"),
        "wav_b": _ffmpeg(d / "b.wav", "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
                         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"),
        "mp3": _ffmpeg(d / "a.mp3", *sine, "-c:a", "libmp3lame", "-b:a", "64k"),
        "webm": _ffmpeg(d / "a.webm", *sine, "-c:a", "libopus"),
        "mp4_av": _ffmpeg(d / "av.mp4", *tiny_video, *sine, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-shortest"),
        "mp4_v": _ffmpeg(d / "v.mp4", *tiny_video, "-c:v", "libx264", "-pix_fmt", "yuv420p"),
        "random": rnd.randbytes(50_000),
        "random_big": rnd.randbytes(int(1.5 * MB)),
        "empty": b"",
        # 11 s of 48 kHz stereo PCM: a little over 2 MB.
        "big_wav": _ffmpeg(d / "big.wav", "-f", "lavfi", "-i", "sine=frequency=440:duration=11",
                           "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le"),
        # A video between 1 and 4 MB (the regular and the audio-only-video limits
        # once max_file_size_mb is 1) with a short audio track.
        "big_mp4_av": _ffmpeg(d / "bigav.mp4", "-f", "lavfi", "-i",
                              "color=c=gray:s=160x120:r=25:d=3,noise=alls=80:allf=t",
                              *sine, "-c:v", "libx264", "-preset", "ultrafast", "-qp", "8",
                              "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"),
        # A video over 1 MB whose audio track alone is over 1 MB once extracted.
        "long_audio_mkv": _ffmpeg(d / "longaudio.mkv", "-f", "lavfi", "-i",
                                  "testsrc=size=32x32:rate=1:duration=90",
                                  "-f", "lavfi", "-i", "sine=frequency=440:duration=90",
                                  "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                  "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", "-shortest"),
    }
    return m


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    return _make_media(tmp_path_factory.mktemp("ing0media"))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def streams_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                          "-of", "json", str(path)], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return [f"{s.get('codec_type')}:{s.get('codec_name')}" for s in json.loads(out.stdout).get("streams", [])]


# --- knobs -------------------------------------------------------------------

@contextmanager
def knobs(*, convert_workers=0, retention=False, passthrough=False, chunking=False,
          max_mb=None, video_mb=None, max_content=None):
    """Pin every setting that steers ingestion to a known value for one case."""
    with ExitStack() as stack:
        stack.enter_context(patch("src.api.recordings.CONVERT_WORKERS", convert_workers))
        stack.enter_context(patch("src.api.recordings.VIDEO_RETENTION", retention))
        stack.enter_context(patch("src.api.recordings.VIDEO_PASSTHROUGH_ASR", passthrough))
        if chunking:
            service = MagicMock()
            service.needs_chunking.return_value = False
            config = MagicMock(enabled=True, source="env", mode="size", limit_value=20)
            stack.enter_context(patch("src.api.recordings.chunking_service", service))
            stack.enter_context(patch("src.audio_chunking.get_effective_chunking_config",
                                      return_value=config))
        else:
            stack.enter_context(patch("src.api.recordings.chunking_service", None))
        if max_mb is not None or video_mb is not None:
            original = SystemSetting.get_setting
            pinned = {"max_file_size_mb": max_mb, "max_audio_only_video_size_mb": video_mb}

            def _setting(k, d=None):
                if pinned.get(k) is not None:
                    return pinned[k]
                return original(k, d)
            stack.enter_context(patch.object(SystemSetting, "get_setting", side_effect=_setting))
        if max_content is not None:
            stack.enter_context(patch.dict(app.config, {"MAX_CONTENT_LENGTH": max_content}))
        yield


# --- world -------------------------------------------------------------------

class _Client(FlaskClient):
    """A test client whose every request loads its user afresh."""

    def open(self, *args, **kwargs):
        g.pop("_login_user", None)
        return super().open(*args, **kwargs)


def _files_under(root):
    if not root or not os.path.isdir(root):
        return set()
    found = set()
    for base, _dirs, files in os.walk(root):
        for name in files:
            found.add(os.path.join(base, name))
    return found


_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KIND_BY_KEY = {
    "tags": "tag", "tag": "tag", "tag_ids": "tag", "tag_id": "tag",
    "folder": "folder", "folder_id": "folder",
    "user_id": "user", "owner_id": "user",
    "recording_id": "rec", "existing_recording_id": "rec", "finalized_recording_id": "rec",
    "part_ids": "part",
}
_SIZE_KEYS = {"file_size", "size", "file_bytes", "used_bytes", "extracted_audio_mb"}


class World:
    """One owner, its client and everything the case creates; removed afterwards."""

    def __init__(self):
        s = uuid.uuid4().hex[:8]
        self.user = User(username=f"ing0_{s}", email=f"ing0_{s}@local.test", password="x")
        db.session.add(self.user)
        db.session.commit()
        self.uid = self.user.id
        self.username = self.user.username
        self.other_users = []
        self.client = _Client(app, app.response_class)
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.uid)
            sess["_fresh"] = True
        self.labels = {}
        self.uploads = {}          # sha256 -> label of the media sent
        self.webhooks = []
        storage = get_storage_service()
        self.staging = storage.get_staging_dir()
        self.roots = {self.staging, app.config.get("UPLOAD_FOLDER"),
                      getattr(getattr(storage, "local", None), "root", None)}
        self.roots.discard(None)
        self.before = set().union(*(_files_under(r) for r in self.roots))
        self.staging_before = _files_under(self.staging)

    # helpers to build the case
    def tag(self, name, owner=None, **kw):
        t = Tag(name=name, user_id=owner.id if owner else self.uid, **kw)
        db.session.add(t)
        db.session.commit()
        self.labels[("tag", t.id)] = f"T:{name}"
        return t

    def folder(self, name, owner=None, **kw):
        f = Folder(name=name, user_id=owner.id if owner else self.uid, **kw)
        db.session.add(f)
        db.session.commit()
        self.labels[("folder", f.id)] = f"F:{name}"
        return f

    def stranger(self):
        s = uuid.uuid4().hex[:8]
        u = User(username=f"ing0x_{s}", email=f"ing0x_{s}@local.test", password="x")
        db.session.add(u)
        db.session.commit()
        self.other_users.append(u.id)
        return u

    def token(self):
        plaintext = f"ing0-{uuid.uuid4().hex}"
        db.session.add(APIToken(user_id=self.uid, token_hash=hash_token(plaintext), name="ing0"))
        db.session.commit()
        return plaintext

    def sent(self, payload):
        self.uploads.setdefault(sha256(payload), f"upload#{len(self.uploads) + 1}")
        return payload

    def post_upload(self, payload, filename, form=None, url="/upload", field="file", **kw):
        data = dict(form or {})
        data[field] = (io.BytesIO(self.sent(payload)), filename)
        return self.client.post(url, data=data, content_type="multipart/form-data", **kw)

    # observation
    def _label(self, kind, value):
        if kind == "rec":
            self._label_recordings()
        if kind == "part":
            from src.models.upload_join import UploadJoinPart
            for p in UploadJoinPart.query.filter_by(user_id=self.uid).all():
                self.labels.setdefault(("part", p.id), f"P{p.part_index}")
        return self.labels.get((kind, value), f"<{kind}-id>")

    def _label_recordings(self):
        rec_ids = [r.id for r in Recording.query.filter(Recording.user_id == self.uid)
                   .order_by(Recording.id).all()]
        for n, rid in enumerate(rec_ids, 1):
            self.labels.setdefault(("rec", rid), f"R{n}")

    def norm(self, value, key=None, kind="rec"):
        if isinstance(value, dict):
            return {k: self.norm(value[k], k, _KIND_BY_KEY.get(k, kind)) for k in sorted(value)}
        if isinstance(value, (list, tuple)):
            return [self.norm(v, key, kind) for v in value]
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int) and key and (key == "id" or key.endswith("_id") or key.endswith("_ids")):
            if kind == "user":
                return "<owner>" if value == self.uid else "<user-id>"
            return self._label(kind, value)
        if key in _SIZE_KEYS and isinstance(value, (int, float)):
            return "<size>"
        if isinstance(value, float):
            return round(value, 1)
        if isinstance(value, str):
            return self._norm_str(value, key)
        return value

    def _norm_str(self, s, key=None):
        if _ISO.match(s):
            try:
                parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                if abs(parsed - datetime.utcnow()) < timedelta(days=2):
                    return "<now>"
            except ValueError:
                pass
            return s
        if _HEX64.match(s):
            return self.uploads.get(s, "<sha256>")
        if self.username in s:
            s = s.replace(self.username, "<username>")
        if "://" in s or s.startswith("/"):
            return "<path>" + os.path.splitext(s)[1]
        return s

    def _recording(self, rec, storage):
        stored = None
        if rec.audio_path:
            try:
                with storage.materialize(rec.audio_path) as m:
                    data = open(m.local_path, "rb").read()
                    stored = {
                        "ext": os.path.splitext(m.local_path)[1],
                        "streams": streams_of(m.local_path),
                        "content": self.uploads.get(sha256(data), "<converted>"),
                        "size_is_file_size": len(data) == rec.file_size,
                    }
            except Exception as e:  # pragma: no cover - recorded, not raised
                stored = {"missing": type(e).__name__}
        tags = [rt.tag.name for rt in RecordingTag.query.filter_by(recording_id=rec.id)
                .order_by(RecordingTag.order).all()]
        folder = db.session.get(Folder, rec.folder_id).name if rec.folder_id else None
        return {
            "id": self._label("rec", rec.id),
            "status": rec.status,
            "title": rec.title,
            "title_source": rec.title_source,
            "original_filename": rec.original_filename,
            "mime_type": rec.mime_type,
            "processing_source": rec.processing_source,
            "is_inbox": rec.is_inbox,
            "keep_audio_only": rec.keep_audio_only,
            "notes": rec.notes,
            "participants": rec.participants,
            "idempotency_key": rec.upload_idempotency_key,
            "file_size_present": rec.file_size is not None,
            "audio_duration_seconds": (round(rec.audio_duration_seconds, 1)
                                       if rec.audio_duration_seconds is not None else None),
            "meeting_date": self._norm_str(rec.meeting_date.isoformat()) if rec.meeting_date else None,
            "file_hash": (self.uploads.get(rec.file_hash, "<other-hash>") if rec.file_hash else None),
            "prompt_variables": rec.prompt_variables,
            "tags": tags,
            "folder": folder,
            "stored": stored,
        }

    def _body(self, resp):
        if resp is None:
            return None
        if isinstance(resp, tuple):
            resp, status = resp[0], resp[1]
        else:
            status = resp.status_code
        out = {"status": status}
        if 300 <= status < 400:
            loc = resp.headers.get("Location", "")
            m = re.search(r"recording_id=(\d+)", loc)
            if m:
                loc = loc.replace(m.group(0), "recording_id=" + self._label("rec", int(m.group(1))))
            out["location"] = loc
            return out
        body = resp.get_json(silent=True)
        if body is None:
            out["text"] = resp.get_data(as_text=True)[:60]
        else:
            out["json"] = self.norm(body)
        return out

    def observe(self, *responses, extra=None):
        db.session.expire_all()
        storage = get_storage_service()
        self._label_recordings()
        recs = Recording.query.filter(Recording.user_id == self.uid).order_by(Recording.id).all()
        jobs = ProcessingJob.query.filter(ProcessingJob.user_id == self.uid).order_by(ProcessingJob.id).all()
        obs = {
            "responses": [self._body(r) for r in responses],
            "recordings": [self._recording(r, storage) for r in recs],
            "jobs": [{
                "recording": self._label("rec", j.recording_id),
                "job_type": j.job_type,
                "status": j.status,
                "is_new_upload": j.is_new_upload,
                "params": self.norm(json.loads(j.params) if j.params else None),
            } for j in jobs],
            "webhooks": [{"recording": self._label("rec", rid), "title": title}
                         for rid, title in self.webhooks],
            "staging_leftovers": len(_files_under(self.staging) - self.staging_before),
        }
        if extra:
            obs.update(extra)
        return obs

    def cleanup(self):
        db.session.rollback()
        uids = [self.uid, *self.other_users]
        rec_ids = [r.id for r in Recording.query.filter(Recording.user_id.in_(uids)).all()]
        with warnings.catch_warnings():
            # user <-> naming/transcript templates form a cycle; no case creates those rows.
            warnings.simplefilter("ignore")
            tables = list(reversed(db.metadata.sorted_tables))
        with db.engine.begin() as conn:
            for table in tables:
                if table.name == "recording":
                    continue
                if rec_ids and "recording_id" in table.c:
                    conn.execute(table.delete().where(table.c.recording_id.in_(rec_ids)))
            for table in tables:
                if table.name in ("user",):
                    continue
                if "user_id" in table.c:
                    conn.execute(table.delete().where(table.c.user_id.in_(uids)))
            conn.execute(User.__table__.delete().where(User.__table__.c.id.in_(uids)))
        db.session.expire_all()
        for path in set().union(*(_files_under(r) for r in self.roots)) - self.before:
            try:
                os.remove(path)
            except OSError:
                pass


@pytest.fixture
def world():
    job_queue.init_app(app)
    with app.app_context():
        # Jobs other modules left queued would be claimed by nothing here, but
        # keep the queue as the conversion tests do.
        w = World()
        from src.services import webhook_dispatch
        real = webhook_dispatch.emit_recording_created

        def _emit(recording):
            w.webhooks.append((recording.id, recording.title))
            return real(recording)

        with patch("src.services.webhook_dispatch.emit_recording_created", side_effect=_emit):
            try:
                yield w
            finally:
                w.cleanup()


# --- expected observations ---------------------------------------------------

def _load():
    if FIXTURE.exists():
        return json.loads(FIXTURE.read_text())
    return {}


def check(case_id, observation):
    """Compare with the pinned observation, or pin it under INGEST_CHAR_RECORD=1."""
    observation = json.loads(json.dumps(observation, default=str))
    if RECORD:
        data = _load()
        data[case_id] = observation
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        return
    expected = _load().get(case_id)
    assert expected is not None, f"no pinned observation for {case_id}; record it with INGEST_CHAR_RECORD=1"
    assert observation == expected
