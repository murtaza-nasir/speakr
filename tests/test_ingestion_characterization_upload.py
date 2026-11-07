"""Characterization of POST /upload, the full ingestion matrix (D556 step 0).

Each case uploads a real file through the browser upload route and compares
what ingestion did with the observation pinned in
tests/fixtures/ingestion_characterization.json (see
tests/ingestion_char_support.py). The cases pin today's behaviour, including
any that look wrong; they are not a specification.

SHARED-DB: every case uses its own owner and removes what it created.
"""

import io

import pytest

from src.database import db
from tests.ingestion_char_support import check, knobs, media, needs_ffmpeg, world  # noqa: F401

pytestmark = needs_ffmpeg

INLINE = {"convert_workers": 0}
DEFERRED = {"convert_workers": 1}

# case id -> (media key, filename, form, knobs)
MEDIA_CASES = {}
for _mode, _k in (("inline", INLINE), ("deferred", DEFERRED)):
    MEDIA_CASES.update({
        f"upload/{_mode}/wav": ("wav", "voice memo.wav", {}, _k),
        f"upload/{_mode}/mp3": ("mp3", "memo.mp3", {}, _k),
        f"upload/{_mode}/webm-opus": ("webm", "browser.webm", {}, _k),
        f"upload/{_mode}/mp4-with-audio": ("mp4_av", "clip.mp4", {}, _k),
        f"upload/{_mode}/mp4-without-audio": ("mp4_v", "silent.mp4", {}, _k),
        f"upload/{_mode}/random-bytes": ("random", "junk.mp3", {}, _k),
        f"upload/{_mode}/random-bytes-video-ext": ("random", "junk.mp4", {}, _k),
        f"upload/{_mode}/empty": ("empty", "empty.wav", {}, _k),
        f"upload/{_mode}/wav-named-mp3": ("wav", "really-wav.mp3", {}, _k),
        f"upload/{_mode}/mp3-named-mp4": ("mp3", "really-mp3.mp4", {}, _k),
    })

VIDEO_CASES = {}
for _mode, _k in (("inline", INLINE), ("deferred", DEFERRED)):
    VIDEO_CASES.update({
        f"video/{_mode}/retention/mp4-with-audio": ("mp4_av", "clip.mp4", {}, {**_k, "retention": True}),
        f"video/{_mode}/retention/keep-audio-only": ("mp4_av", "clip.mp4", {"keep_audio_only": "true"},
                                                      {**_k, "retention": True}),
        f"video/{_mode}/retention/mp4-without-audio": ("mp4_v", "silent.mp4", {}, {**_k, "retention": True}),
        f"video/{_mode}/retention/webm-audio-only": ("webm", "browser.webm", {}, {**_k, "retention": True}),
        f"video/{_mode}/retention/random-bytes-video-ext": ("random", "junk.mp4", {},
                                                            {**_k, "retention": True}),
        f"video/{_mode}/passthrough/mp4-with-audio": ("mp4_av", "clip.mp4", {},
                                                      {**_k, "passthrough": True}),
        f"video/{_mode}/passthrough/keep-audio-only": ("mp4_av", "clip.mp4", {"keep_audio_only": "true"},
                                                       {**_k, "passthrough": True}),
        f"video/{_mode}/passthrough/mp4-without-audio": ("mp4_v", "silent.mp4", {},
                                                         {**_k, "passthrough": True}),
    })

# max_file_size_mb = 1 and max_audio_only_video_size_mb = 4: big_wav (2 MB) is over
# the regular limit, big_mp4_av sits between the two.
ONE = {"max_mb": 1, "video_mb": 4}
SIZE_CASES = {
    "size/over-max/chunking-off": ("big_wav", "big.wav", {}, {**ONE}),
    "size/over-max/chunking-on": ("big_wav", "big.wav", {}, {**ONE, "chunking": True}),
    "size/over-max/deferred/chunking-on": ("big_wav", "big.wav", {}, {**ONE, **DEFERRED, "chunking": True}),
    "size/over-wsgi-ceiling/chunking-off": ("big_wav", "big.wav", {}, {"max_content": 1024 * 1024}),
    "size/over-wsgi-ceiling/chunking-on": ("big_wav", "big.wav", {}, {"max_content": 1024 * 1024,
                                                                      "chunking": True}),
    "size/audio-only-video/inline": ("big_mp4_av", "big.mp4", {}, {**ONE}),
    "size/audio-only-video/deferred": ("big_mp4_av", "big.mp4", {}, {**ONE, **DEFERRED}),
    "size/audio-only-video/retention": ("big_mp4_av", "big.mp4", {}, {**ONE, "retention": True}),
    "size/audio-only-video/retention-keep-audio-only": ("big_mp4_av", "big.mp4", {"keep_audio_only": "true"},
                                                        {**ONE, "retention": True}),
    "size/audio-only-video/over-video-limit": ("big_mp4_av", "big.mp4", {}, {"max_mb": 1, "video_mb": 1}),
    "size/audio-named-mp4-over-max": ("big_wav", "big.mp4", {}, {**ONE}),
    "size/random-named-mp4-over-max/retention": ("random_big", "junk.mp4", {}, {**ONE, "retention": True}),
    "size/random-named-mp4-over-max/no-retention": ("random_big", "junk.mp4", {}, {**ONE}),
    "size/extracted-audio-too-large/chunking-off": ("long_audio_mkv", "long.mkv", {}, {**ONE}),
    "size/extracted-audio-too-large/chunking-on": ("long_audio_mkv", "long.mkv", {}, {**ONE, "chunking": True}),
    "size/extracted-audio-too-large/deferred": ("long_audio_mkv", "long.mkv", {}, {**ONE, **DEFERRED}),
}

SIMPLE = {**MEDIA_CASES, **VIDEO_CASES, **SIZE_CASES}


@pytest.mark.parametrize("case_id", sorted(SIMPLE))
def test_upload_case(world, media, case_id):
    key, filename, form, k = SIMPLE[case_id]
    with knobs(**k):
        resp = world.post_upload(media[key], filename, form)
    check(case_id, world.observe(resp))


@pytest.mark.parametrize("mode", ["inline", "deferred"])
def test_upload_form_fields(world, media, mode):
    """Tags in the order sent, folder, title, notes, meeting date and ASR options."""
    first, second = world.tag("first"), world.tag("second")
    folder = world.folder("projects")
    form = {
        "tag_ids[0]": str(second.id), "tag_ids[1]": str(first.id),
        "folder_id": str(folder.id),
        "title": "Weekly sync", "notes": "Bring the numbers",
        "meeting_date": "2024-03-15T09:30:00Z",
        "language": "fr", "min_speakers": "2", "max_speakers": "3",
        "hotwords": "Speakr, Murtaza", "initial_prompt": "A planning meeting.",
        "prompt_variables": '{"client": "Acme"}',
    }
    with knobs(**(DEFERRED if mode == "deferred" else INLINE)):
        resp = world.post_upload(media["mp3"], "sync.mp3", form)
    check(f"form/{mode}/all-fields", world.observe(resp))


def test_upload_tag_and_folder_defaults_fill_the_params(world, media):
    tag = world.tag("german", default_language="de", default_hotwords="Kaffee")
    folder = world.folder("calls", default_language="es")
    with knobs():
        resp = world.post_upload(media["mp3"], "call.mp3",
                                 {"tag_ids[0]": str(tag.id), "folder_id": str(folder.id)})
    check("form/tag-and-folder-defaults", world.observe(resp))


def test_upload_legacy_tag_id_and_foreign_tag_and_folder(world, media):
    mine = world.tag("mine")
    stranger = world.stranger()
    theirs = world.tag("theirs", owner=stranger)
    f = world.folder("their-folder", owner=stranger)
    with knobs():
        legacy = world.post_upload(media["mp3"], "legacy.mp3", {"tag_id": str(mine.id)})
        foreign = world.post_upload(media["wav"], "foreign.wav",
                                    {"tag_ids[0]": str(theirs.id), "folder_id": str(f.id)})
    check("form/legacy-tag-id-and-foreign-tag-folder", world.observe(legacy, foreign))


@pytest.mark.parametrize("parse", [True, False])
def test_upload_filename_date(world, media, parse):
    world.user.parse_filename_dates = parse
    world.user.filename_date_pattern = "auto"
    db.session.commit()
    with knobs():
        resp = world.post_upload(media["wav"], "recording-2024-03-15T09-30-00.wav", {"client_tz_offset": "0"})
    check(f"form/filename-date/{'on' if parse else 'off'}", world.observe(resp))


def test_upload_file_last_modified(world, media):
    with knobs():
        resp = world.post_upload(media["wav"], "memo.wav", {"file_last_modified": "1710495000000"})
    check("form/file-last-modified", world.observe(resp))


def test_upload_unparseable_meeting_date_and_prompt_variables(world, media):
    with knobs():
        resp = world.post_upload(media["wav"], "memo.wav", {"meeting_date": "not a date",
                                                            "prompt_variables": "{not json",
                                                            "min_speakers": "two"})
    check("form/unparseable-values", world.observe(resp))


@pytest.mark.parametrize("mode", ["inline", "deferred"])
def test_upload_duplicate(world, media, mode):
    """The same bytes twice: both stored, the second carries a duplicate warning."""
    with knobs(**(DEFERRED if mode == "deferred" else INLINE)):
        first = world.post_upload(media["wav"], "memo.wav")
        second = world.post_upload(media["wav"], "memo-again.wav")
    check(f"duplicate/{mode}/upload-route", world.observe(first, second))


@pytest.mark.parametrize("limit", ["over", "recorder-exempt"])
def test_upload_storage_quota(world, media, limit):
    world.user.storage_quota_mb = 1
    db.session.commit()
    form = {"from_recorder": "true"} if limit == "recorder-exempt" else {}
    with knobs():
        resp = world.post_upload(media["big_wav"], "big.wav", form)
    check(f"quota/{limit}", world.observe(resp))


@pytest.mark.parametrize("mode", ["inline", "deferred"])
def test_upload_join_two_parts(world, media, mode):
    group = "ing0group" + mode
    tag = world.tag("joined")
    common = {"join_group": group, "join_count": "2", "title": "Two halves", "tag_ids[0]": str(tag.id)}
    with knobs(**(DEFERRED if mode == "deferred" else INLINE)):
        part0 = world.post_upload(media["wav"], "part-a.wav", {**common, "join_index": "0"})
        mid = world.observe(part0)
        part1 = world.post_upload(media["mp3"], "part-b.mp3", {**common, "join_index": "1"})
        replay = world.post_upload(media["mp3"], "part-b.mp3", {**common, "join_index": "1"})
    from src.models.upload_join import UploadJoinPart
    parts = UploadJoinPart.query.filter_by(user_id=world.uid).order_by(UploadJoinPart.part_index).all()
    check(f"join/{mode}/two-parts", world.observe(part0, part1, replay, extra={
        "after_first_part": {k: mid[k] for k in ("recordings", "jobs", "webhooks", "staging_leftovers")},
        "parts": [{"index": p.part_index, "count": p.part_count, "filename": p.original_filename,
                   "mime_type": p.mime_type, "audio_path": world.norm(p.audio_path, "audio_path"),
                   "recording": world.norm(p.recording_id, "recording_id"),
                   "duration": world.norm(p.audio_duration_seconds), "claimed": p.claimed_at is not None}
                  for p in parts],
    }))


def test_upload_join_part_refused(world, media):
    with knobs():
        bad_count = world.post_upload(media["wav"], "part.wav", {"join_group": "ing0badgroup",
                                                                 "join_index": "0", "join_count": "1"})
        bad_group = world.post_upload(media["wav"], "part.wav", {"join_group": "x!",
                                                                 "join_index": "0", "join_count": "2"})
        junk = world.post_upload(media["random"], "part.mp3", {"join_group": "ing0junkgroup",
                                                               "join_index": "0", "join_count": "2"})
    check("join/refused", world.observe(bad_count, bad_group, junk))


def test_upload_without_a_file(world):
    with knobs():
        none = world.client.post("/upload", data={}, content_type="multipart/form-data")
        nameless = world.client.post("/upload", data={"file": (io.BytesIO(b"abc"), "")},
                                     content_type="multipart/form-data")
    check("upload/no-file", world.observe(none, nameless))
