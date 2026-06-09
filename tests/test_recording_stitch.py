"""Tests for the recording-stitch worker (#287 c/d).

Exercises the REAL assembly path (no mocking) on synthetic audio that
faithfully mirrors how a browser ``MediaRecorder`` emits data:

  - A continuous recording is one container whose FIRST timeslice chunk
    carries the init header and whose later chunks are headerless
    continuations. We simulate this by generating a real WebM/Opus file
    and byte-splitting it into fragments (only fragment 0 keeps the
    header) — exactly the shape real chunks have.
  - A recording that was resumed after a page reload is multiple complete
    containers (one per MediaRecorder), which we simulate with two whole
    WebM files planted as two chunks.

The earlier version of this test planted two *complete* WAV files as
chunks and fed them to ffmpeg's concat demuxer. That shape is unrealistic
(real timeslice chunks are NOT independently-valid files) and it masked a
bug where the demuxer decoded only the first headered chunk and silently
dropped the rest. These tests use realistic fixtures so that regression
cannot reappear unnoticed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.models import User, Recording, RecordingSession
from src.services.recording_stitch import (
    stitch_recording_session,
    StitchError,
    _chunk_paths,
    _mime_to_extension,
    _chunk_starts_segment,
    _partition_into_segments,
    _WEBM_EBML_MAGIC,
)


app.config["WTF_CSRF_ENABLED"] = False


def _have_webm_encoder():
    """True if this ffmpeg build can mux WebM/Opus (it can in CI + the app
    image; guard defensively so a minimal build skips rather than fails)."""
    try:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
             '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1',
             '-ac', '1', '-ar', '48000', '-c:a', 'libopus', '-f', 'webm',
             os.path.join(tempfile.gettempdir(), f'_webmprobe_{uuid.uuid4().hex}.webm')],
            capture_output=True,
        )
        return r.returncode == 0
    except FileNotFoundError:
        return False


_WEBM_OK = _have_webm_encoder()
_needs_webm = pytest.mark.skipif(not _WEBM_OK, reason="ffmpeg build lacks WebM/Opus encoder")


def _generate_webm(path, duration_seconds=3, freq_hz=440):
    """Write a real WebM/Opus file — a stand-in for one MediaRecorder run."""
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-f', 'lavfi', '-i', f'sine=frequency={freq_hz}:duration={duration_seconds}',
        '-ac', '1', '-ar', '48000', '-c:a', 'libopus', '-f', 'webm',
        path,
    ]
    subprocess.run(cmd, check=True)


def _split_into_chunks(src_path, sess_dir, n_parts, start_index=1):
    """Byte-split ``src_path`` into ``n_parts`` chunk-NNNNNN.bin fragments in
    ``sess_dir`` (continuing from ``start_index``). This reproduces
    MediaRecorder timeslice output: only the first fragment of the file holds
    the container header. Returns the next free chunk index."""
    with open(src_path, 'rb') as f:
        data = f.read()
    size = len(data)
    step = size // n_parts
    idx = start_index
    for i in range(n_parts):
        lo = i * step
        hi = size if i == n_parts - 1 else (i + 1) * step
        with open(os.path.join(sess_dir, f'chunk-{idx:06d}.bin'), 'wb') as out:
            out.write(data[lo:hi])
        idx += 1
    return idx


def _probe_duration(path):
    cmd = [
        'ffprobe', '-hide_banner', '-loglevel', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        path,
    ]
    out = subprocess.run(cmd, check=True, capture_output=True)
    return float(out.stdout.decode().strip())


def _make_user():
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"stitch_{suffix}",
        email=f"stitch_{suffix}@local.test",
        password="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _plant_session(upload_folder, user, mime_type='audio/webm', chunk_count=1):
    """Create the placeholder Recording + RecordingSession the way
    finalize_session does, and return (recording, session, sess_dir)."""
    recording = Recording(
        user_id=user.id,
        title="Stitch test",
        status='STITCHING',
        mime_type=mime_type,
        processing_source='recording_session',
    )
    db.session.add(recording)
    db.session.flush()
    session = RecordingSession(
        user_id=user.id,
        mime_type=mime_type,
        status='finalizing',
        chunk_count=chunk_count,
        bytes_received=12345,
        finalized_recording_id=recording.id,
    )
    db.session.add(session)
    db.session.commit()
    sess_dir = os.path.join(upload_folder, "_sessions", session.id)
    os.makedirs(sess_dir, exist_ok=True)
    return recording, session, sess_dir


# --------------------------------------------------------------------------
# Pure unit tests for segment detection (codec-free, always run)
# --------------------------------------------------------------------------

def test_mime_to_extension_maps_known_types():
    assert _mime_to_extension('audio/webm') == 'webm'
    assert _mime_to_extension('audio/mp4') == 'm4a'
    assert _mime_to_extension('audio/ogg') == 'ogg'
    assert _mime_to_extension('audio/whatever') == 'webm'
    assert _mime_to_extension('') == 'webm'


def test_chunk_paths_returns_sorted_chunks():
    tmp = tempfile.mkdtemp(prefix="speakr-stitch-")
    try:
        for name in ('chunk-000003.bin', 'chunk-000001.bin', 'chunk-000002.bin'):
            with open(os.path.join(tmp, name), 'wb') as f:
                f.write(b'.')
        with open(os.path.join(tmp, 'session.json'), 'wb') as f:
            f.write(b'{}')
        with open(os.path.join(tmp, 'chunk-bad.txt'), 'wb') as f:
            f.write(b'.')
        paths = _chunk_paths(tmp)
        assert [os.path.basename(p) for p in paths] == ['chunk-000001.bin', 'chunk-000002.bin', 'chunk-000003.bin']
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_chunk_paths_empty_dir_returns_empty_list():
    tmp = tempfile.mkdtemp(prefix="speakr-stitch-empty-")
    try:
        assert _chunk_paths(tmp) == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write(tmp, name, data):
    p = os.path.join(tmp, name)
    with open(p, 'wb') as f:
        f.write(data)
    return p


def test_chunk_starts_segment_detects_webm_and_mp4_headers():
    tmp = tempfile.mkdtemp(prefix="speakr-seg-")
    try:
        webm = _write(tmp, 'a.bin', _WEBM_EBML_MAGIC + b'\x00' * 32)
        mp4 = _write(tmp, 'b.bin', b'\x00\x00\x00\x20ftypiso5' + b'\x00' * 16)
        cont = _write(tmp, 'c.bin', b'\x6e\x79\x20\x01raw cluster continuation')
        assert _chunk_starts_segment(webm) is True
        assert _chunk_starts_segment(mp4) is True
        assert _chunk_starts_segment(cont) is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_partition_single_segment_when_only_first_chunk_has_header():
    """The common case: one continuous recording. Only chunk 1 has a header,
    so everything collapses into a single segment (→ byte-join path)."""
    tmp = tempfile.mkdtemp(prefix="speakr-seg1-")
    try:
        c1 = _write(tmp, 'chunk-000001.bin', _WEBM_EBML_MAGIC + b'header+clusters')
        c2 = _write(tmp, 'chunk-000002.bin', b'\x99\xec\xb8\x72 continuation A')
        c3 = _write(tmp, 'chunk-000003.bin', b'\x4f\xb1\x7d\xb1 continuation B')
        segments = _partition_into_segments([c1, c2, c3])
        assert len(segments) == 1
        assert segments[0] == [c1, c2, c3]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_partition_starts_new_segment_on_each_header():
    """Resume case: a second header (a fresh MediaRecorder) opens a new
    segment, and its own continuations attach to it."""
    tmp = tempfile.mkdtemp(prefix="speakr-seg2-")
    try:
        c1 = _write(tmp, 'chunk-000001.bin', _WEBM_EBML_MAGIC + b'seg1 header')
        c2 = _write(tmp, 'chunk-000002.bin', b'\x99\xec\xb8\x72 seg1 cont')
        c3 = _write(tmp, 'chunk-000003.bin', _WEBM_EBML_MAGIC + b'seg2 header')
        c4 = _write(tmp, 'chunk-000004.bin', b'\x4f\xb1\x7d\xb1 seg2 cont')
        segments = _partition_into_segments([c1, c2, c3, c4])
        assert segments == [[c1, c2], [c3, c4]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# End-to-end stitch tests on realistic WebM fixtures (skip if no encoder)
# --------------------------------------------------------------------------

@_needs_webm
def test_stitch_single_segment_preserves_full_duration_end_to_end():
    """A 6s recording delivered as 4 byte-fragments (only the first carries
    the header) must stitch to ~6s. This is the regression guard: the old
    concat-demuxer produced ~1.5s here by dropping fragments 2-4."""
    upload_folder = tempfile.mkdtemp(prefix="speakr-stitch-uploads-")
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _make_user()
        recording, session, sess_dir = _plant_session(upload_folder, user, 'audio/webm', chunk_count=4)

        src = os.path.join(upload_folder, 'src.webm')
        _generate_webm(src, duration_seconds=6, freq_hz=440)
        _split_into_chunks(src, sess_dir, n_parts=4)
        os.remove(src)

        recording_id, audio_path, metadata = stitch_recording_session(session.id)

        assert os.path.exists(audio_path)
        duration = _probe_duration(audio_path)
        assert 5.5 <= duration <= 6.5, f"expected ~6s, got {duration} (chunks were dropped?)"

        rec = db.session.get(Recording, recording_id)
        assert rec.status == 'PENDING'
        assert rec.audio_path == audio_path
        assert rec.file_size and rec.file_size > 0
        assert rec.original_filename

        sess = db.session.get(RecordingSession, session.id)
        assert sess.status == 'finalized'
        assert sess.finalized_at is not None
        assert not os.path.isdir(sess_dir)

        os.remove(audio_path)
        db.session.delete(sess)
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


@_needs_webm
def test_stitch_multi_segment_resume_sums_durations_end_to_end():
    """A resumed recording is two complete containers (3s + 4s) planted as
    two chunks; assembly must concat across segments to ~7s."""
    upload_folder = tempfile.mkdtemp(prefix="speakr-stitch-resume-")
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _make_user()
        recording, session, sess_dir = _plant_session(upload_folder, user, 'audio/webm', chunk_count=2)

        seg1 = os.path.join(sess_dir, 'chunk-000001.bin')
        seg2 = os.path.join(sess_dir, 'chunk-000002.bin')
        _generate_webm(seg1, duration_seconds=3, freq_hz=440)
        _generate_webm(seg2, duration_seconds=4, freq_hz=660)

        recording_id, audio_path, metadata = stitch_recording_session(session.id)

        assert os.path.exists(audio_path)
        duration = _probe_duration(audio_path)
        assert 6.3 <= duration <= 7.7, f"expected ~7s across two segments, got {duration}"

        os.remove(audio_path)
        rec = db.session.get(Recording, recording_id)
        sess = db.session.get(RecordingSession, session.id)
        db.session.delete(sess)
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


@_needs_webm
def test_stitch_with_no_user_title_sets_recognised_placeholder():
    """An in-app recording carries no user title, so the stitched recording
    must get a placeholder that is_placeholder_title recognises — otherwise
    generate_title_task would skip AI titling (the reported bug)."""
    from src.utils.titles import is_placeholder_title
    upload_folder = tempfile.mkdtemp(prefix="speakr-stitch-title-")
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _make_user()
        recording, session, sess_dir = _plant_session(upload_folder, user, 'audio/webm', chunk_count=1)
        session.finalize_metadata = json.dumps({"notes": "n", "tags": []})  # no title
        db.session.commit()
        _generate_webm(os.path.join(sess_dir, 'chunk-000001.bin'), duration_seconds=2)

        recording_id, audio_path, metadata = stitch_recording_session(session.id)

        rec = db.session.get(Recording, recording_id)
        assert is_placeholder_title(rec.title, rec.original_filename) is True, \
            f"title {rec.title!r} would block AI titling"

        os.remove(audio_path)
        db.session.delete(db.session.get(RecordingSession, session.id))
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


@_needs_webm
def test_stitch_preserves_a_real_user_title():
    """If the user did supply a title, the shared resolver keeps it (and AI
    titling leaves it alone)."""
    from src.utils.titles import is_placeholder_title
    upload_folder = tempfile.mkdtemp(prefix="speakr-stitch-utitle-")
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _make_user()
        recording, session, sess_dir = _plant_session(upload_folder, user, 'audio/webm', chunk_count=1)
        session.finalize_metadata = json.dumps({"title": "Q3 Planning Sync"})
        db.session.commit()
        _generate_webm(os.path.join(sess_dir, 'chunk-000001.bin'), duration_seconds=2)

        recording_id, audio_path, metadata = stitch_recording_session(session.id)

        rec = db.session.get(Recording, recording_id)
        assert rec.title == "Q3 Planning Sync"
        assert is_placeholder_title(rec.title, rec.original_filename) is False

        os.remove(audio_path)
        db.session.delete(db.session.get(RecordingSession, session.id))
        db.session.delete(rec)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_stitch_raises_when_no_chunks_on_disk():
    upload_folder = tempfile.mkdtemp(prefix="speakr-stitch-nochunks-")
    with app.app_context():
        app.config["UPLOAD_FOLDER"] = upload_folder
        user = _make_user()
        recording, session, sess_dir = _plant_session(upload_folder, user, 'audio/webm', chunk_count=0)
        shutil.rmtree(sess_dir, ignore_errors=True)  # no chunks on disk

        try:
            stitch_recording_session(session.id)
            assert False, "expected StitchError"
        except StitchError as e:
            assert 'no chunks' in str(e).lower()

        db.session.delete(session)
        db.session.delete(recording)
        db.session.delete(user)
        db.session.commit()
    shutil.rmtree(upload_folder, ignore_errors=True)


def test_stitch_raises_when_session_missing():
    with app.app_context():
        try:
            stitch_recording_session('00000000-0000-0000-0000-000000000000')
            assert False, "expected StitchError for missing session"
        except StitchError as e:
            assert 'not found' in str(e).lower()


_ORIGINAL_UPLOAD_FOLDER = app.config.get("UPLOAD_FOLDER")


def setup_function(function):  # noqa: D401 - pytest hook
    if _ORIGINAL_UPLOAD_FOLDER is not None:
        app.config["UPLOAD_FOLDER"] = _ORIGINAL_UPLOAD_FOLDER


def teardown_function(function):
    if _ORIGINAL_UPLOAD_FOLDER is not None:
        app.config["UPLOAD_FOLDER"] = _ORIGINAL_UPLOAD_FOLDER


def teardown_module(module):
    if _ORIGINAL_UPLOAD_FOLDER is not None:
        app.config["UPLOAD_FOLDER"] = _ORIGINAL_UPLOAD_FOLDER
    with app.app_context():
        for u in User.query.filter(User.username.like("stitch_%")).all():
            for s in RecordingSession.query.filter_by(user_id=u.id).all():
                db.session.delete(s)
            for r in Recording.query.filter_by(user_id=u.id).all():
                if r.audio_path and os.path.exists(r.audio_path):
                    try:
                        os.remove(r.audio_path)
                    except OSError:
                        pass
                db.session.delete(r)
            db.session.delete(u)
        db.session.commit()


if __name__ == "__main__":
    test_mime_to_extension_maps_known_types()
    test_chunk_paths_returns_sorted_chunks()
    test_chunk_paths_empty_dir_returns_empty_list()
    test_chunk_starts_segment_detects_webm_and_mp4_headers()
    test_partition_single_segment_when_only_first_chunk_has_header()
    test_partition_starts_new_segment_on_each_header()
    if _WEBM_OK:
        test_stitch_single_segment_preserves_full_duration_end_to_end()
        test_stitch_multi_segment_resume_sums_durations_end_to_end()
    else:
        print("(skipping WebM end-to-end tests: no encoder)")
    test_stitch_raises_when_no_chunks_on_disk()
    test_stitch_raises_when_session_missing()
    print("All stitch tests passed.")
