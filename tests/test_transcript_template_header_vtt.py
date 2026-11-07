"""Transcript template header, the |vtt filter, and the API v1 transcript
parameters translation and template_id.

SHARED-DB: the users, tokens, templates and recordings are removed afterwards.
"""

import json
import os
import secrets
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.file_exporter import format_transcription_with_template
from src.models import APIToken, Recording, User
from src.models.templates import TranscriptTemplate
from src.utils.token_auth import hash_token
from src.utils.transcript_render import render_segments, render_transcription

app.config["WTF_CSRF_ENABLED"] = False

SEGMENTS = [
    {"speaker": "Alice", "sentence": "Hello there.", "start_time": 1.5, "end_time": 4.299},
    {"speaker": "Bob", "sentence": "Hi back.", "start_time": 4.299, "end_time": 7.0},
]
TRANSLATED = [
    {"speaker": "Alice", "sentence": "Hola.", "start_time": 1.5, "end_time": 4.299},
    {"speaker": "Bob", "sentence": "Adios.", "start_time": 4.299, "end_time": 7.0},
]
VTT_TEMPLATE = "{{start_time|vtt}} --> {{end_time|vtt}}\n<v {{speaker}}>{{text}}\n"
VTT_BODY = ("00:01.500 --> 00:04.299\n<v Alice>Hello there.\n\n"
            "00:04.299 --> 00:07.000\n<v Bob>Hi back.\n")


def _vtt(seconds):
    return render_segments([{"start_time": seconds}], "{{start_time|vtt}}")


# --- |vtt -------------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (None, "00:00.000"),
    (0, "00:00.000"),
    (0.0004, "00:00.000"),
    (0.0006, "00:00.001"),
    (4.299, "00:04.299"),
    (59.9996, "01:00.000"),         # rounds across a second and a minute
    (61.25, "01:01.250"),
    (3599.999, "59:59.999"),
    (3599.9996, "01:00:00.000"),    # rounds across the hour
    (3600, "01:00:00.000"),
    (3600.0, "01:00:00.000"),
    (3725.5, "01:02:05.500"),
    (7322.25, "02:02:02.250"),
    (86399.9999, "24:00:00.000"),
    (360000, "100:00:00.000"),
])
def test_vtt_time(seconds, expected):
    assert _vtt(seconds) == expected


def test_vtt_on_both_times_and_beside_other_filters():
    seg = [{"speaker": "Alice", "sentence": "Hi.", "start_time": 59.9996, "end_time": 3600}]
    fmt = "{{start_time|vtt}} --> {{end_time|vtt}} {{start_time|srt}} {{speaker|upper}}: {{text}}"
    # |vtt rounds the milliseconds; |srt keeps truncating them.
    assert render_segments(seg, fmt) == "01:00.000 --> 01:00:00.000 00:00:59,999 ALICE: Hi."


def test_vtt_on_other_placeholders_is_not_a_filter():
    seg = [{"speaker": "Alice", "sentence": "Hi.", "start_time": 1}]
    assert render_segments(seg, "{{speaker|vtt}} {{text}}") == "{{speaker|vtt}} Hi."


def test_webvtt_template_with_header():
    assert render_segments(SEGMENTS, VTT_TEMPLATE, "WEBVTT") == "WEBVTT\n\n" + VTT_BODY


def test_summarizer_path_supports_vtt():
    assert render_transcription(json.dumps(SEGMENTS), "{{start_time|vtt}} {{text}}") == \
        "00:01.500 Hello there.\n00:04.299 Hi back."


# --- header -----------------------------------------------------------------

def test_header_is_written_once_and_followed_by_a_blank_line():
    out = render_segments(SEGMENTS, "{{speaker}}: {{text}}", "Meeting notes")
    assert out == "Meeting notes\n\nAlice: Hello there.\nBob: Hi back."
    assert out.count("Meeting notes") == 1


@pytest.mark.parametrize("header", [None, "", "\n", "   ", " \n\n"])
def test_no_header_renders_exactly_as_before(header):
    assert render_segments(SEGMENTS, "{{speaker}}: {{text}}", header) == \
        render_segments(SEGMENTS, "{{speaker}}: {{text}}") == "Alice: Hello there.\nBob: Hi back."


def test_trailing_line_breaks_in_the_header_are_ignored():
    assert render_segments(SEGMENTS[:1], "{{text}}", "WEBVTT\n\n") == "WEBVTT\n\nHello there."
    assert render_segments(SEGMENTS[:1], "{{text}}", "WEBVTT\r\n") == "WEBVTT\n\nHello there."


def test_multi_line_header_is_kept_and_placeholders_in_it_are_literal():
    out = render_segments(SEGMENTS[:1], "{{text}}", "Kind: captions\n{{speaker}}")
    assert out == "Kind: captions\n{{speaker}}\n\nHello there."


def test_header_with_no_segments():
    assert render_segments([], "{{text}}", "WEBVTT") == "WEBVTT\n\n"


# --- fixtures for the app paths ---------------------------------------------

@pytest.fixture
def world():
    with app.app_context():
        s = secrets.token_hex(4)
        owner = User(username=f"thv_{s}", email=f"thv_{s}@local.test", password="x")
        other = User(username=f"thv2_{s}", email=f"thv2_{s}@local.test", password="x")
        db.session.add_all([owner, other])
        db.session.commit()
        vtt = TranscriptTemplate(user_id=owner.id, name="WebVTT", template=VTT_TEMPLATE,
                                 header="WEBVTT", is_default=False)
        plain = TranscriptTemplate(user_id=owner.id, name="Plain", template="{{speaker}}: {{text}}",
                                   is_default=True)
        foreign = TranscriptTemplate(user_id=other.id, name="Theirs", template="X {{text}}",
                                     header="THEIRS", is_default=True)
        diarized = Recording(user_id=owner.id, title="Weekly", status="COMPLETED", original_filename="a.wav",
                             transcription=json.dumps(SEGMENTS), transcription_translation=json.dumps(TRANSLATED),
                             transcription_translation_language="Spanish")
        untranslated = Recording(user_id=owner.id, title="Solo", status="COMPLETED", original_filename="b.wav",
                                 transcription=json.dumps(SEGMENTS))
        plain_rec = Recording(user_id=owner.id, title="Plain", status="COMPLETED", original_filename="c.wav",
                              transcription="Just words.", transcription_translation="Solo palabras.",
                              transcription_translation_language="Spanish")
        token = f"tok-{secrets.token_urlsafe(16)}"
        db.session.add_all([vtt, plain, foreign, diarized, untranslated, plain_rec,
                            APIToken(user_id=owner.id, token_hash=hash_token(token), name="thv")])
        db.session.commit()
        ids = dict(owner=owner.id, other=other.id, vtt=vtt.id, plain=plain.id, foreign=foreign.id,
                   rec=diarized.id, untranslated=untranslated.id, plain_rec=plain_rec.id, token=token)
    yield ids
    with app.app_context():
        db.session.rollback()
        for uid in (ids["owner"], ids["other"]):
            Recording.query.filter_by(user_id=uid).delete()
            TranscriptTemplate.query.filter_by(user_id=uid).delete()
            APIToken.query.filter_by(user_id=uid).delete()
            User.query.filter_by(id=uid).delete()
        db.session.commit()


def _client(user_id):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


def _set_default(template_id, user_id):
    with app.app_context():
        TranscriptTemplate.query.filter_by(user_id=user_id).update({"is_default": False})
        db.session.get(TranscriptTemplate, template_id).is_default = True
        db.session.commit()


# --- header in the export and the download ----------------------------------

def test_export_writes_the_default_templates_header(world):
    _set_default(world["vtt"], world["owner"])
    with app.app_context():
        user = db.session.get(User, world["owner"])
        assert format_transcription_with_template(json.dumps(SEGMENTS), user) == "WEBVTT\n\n" + VTT_BODY
        # A plain-text transcript is returned as-is, without the header.
        assert format_transcription_with_template("Just words.", user) == "Just words."


def test_export_with_an_explicit_template(world):
    with app.app_context():
        user = db.session.get(User, world["owner"])
        tmpl = db.session.get(TranscriptTemplate, world["vtt"])
        assert format_transcription_with_template(json.dumps(SEGMENTS), user, tmpl) == "WEBVTT\n\n" + VTT_BODY


def test_download_txt_with_header(world):
    resp = _client(world["owner"]).get(f"/recording/{world['rec']}/download/transcript?template_id={world['vtt']}")
    assert resp.status_code == 200
    assert resp.data.decode() == "WEBVTT\n\n" + VTT_BODY
    assert 'filename="Weekly_WebVTT.txt"' in resp.headers["Content-Disposition"]


def test_download_md_puts_the_title_heading_before_the_header(world):
    resp = _client(world["owner"]).get(
        f"/recording/{world['rec']}/download/transcript?template_id={world['vtt']}&format=md")
    assert resp.status_code == 200
    assert resp.data.decode() == "# Weekly\n\nWEBVTT\n\n" + VTT_BODY + "\n"


def test_download_translation_with_header(world):
    resp = _client(world["owner"]).get(
        f"/recording/{world['rec']}/download/transcript?template_id={world['vtt']}&translation=1")
    assert resp.data.decode().startswith("WEBVTT\n\n00:01.500 --> 00:04.299\n<v Alice>Hola.\n")


def test_download_plain_text_ignores_the_header(world):
    resp = _client(world["owner"]).get(
        f"/recording/{world['plain_rec']}/download/transcript?template_id={world['vtt']}")
    assert resp.data.decode() == "Just words."


def test_download_without_header_is_unchanged(world):
    resp = _client(world["owner"]).get(f"/recording/{world['rec']}/download/transcript")
    assert resp.data.decode() == "Alice: Hello there.\nBob: Hi back."


def test_download_with_a_foreign_template_id_uses_the_basic_format(world):
    """Existing behaviour of the web download, kept: no 404, no foreign header."""
    resp = _client(world["owner"]).get(
        f"/recording/{world['rec']}/download/transcript?template_id={world['foreign']}")
    assert resp.data.decode() == "[Alice]: Hello there.\n[Bob]: Hi back."


# --- template API -----------------------------------------------------------

def test_template_api_creates_returns_and_updates_the_header(world):
    client = _client(world["owner"])
    created = client.post("/api/transcript-templates",
                          json={"name": "Captions", "template": VTT_TEMPLATE, "header": "WEBVTT"})
    assert created.status_code == 201
    body = created.get_json()
    assert body["header"] == "WEBVTT"
    tid = body["id"]

    listed = {t["id"]: t for t in client.get("/api/transcript-templates").get_json()}
    assert listed[tid]["header"] == "WEBVTT"
    assert listed[world["plain"]]["header"] is None

    # An update without header leaves it unchanged, as for description.
    renamed = client.put(f"/api/transcript-templates/{tid}", json={"name": "Captions 2"}).get_json()
    assert renamed["name"] == "Captions 2" and renamed["header"] == "WEBVTT"

    changed = client.put(f"/api/transcript-templates/{tid}", json={"header": "WEBVTT\nKind: captions"}).get_json()
    assert changed["header"] == "WEBVTT\nKind: captions"

    cleared = client.put(f"/api/transcript-templates/{tid}", json={"header": ""}).get_json()
    assert cleared["header"] == ""


def test_template_api_create_without_header(world):
    body = _client(world["owner"]).post("/api/transcript-templates",
                                        json={"name": "NoHeader", "template": "{{text}}"}).get_json()
    assert body["header"] is None


def test_default_templates_have_no_header(world):
    client = _client(world["other"])
    with app.app_context():
        TranscriptTemplate.query.filter_by(user_id=world["other"]).delete()
        db.session.commit()
    templates = client.post("/api/transcript-templates/create-defaults").get_json()["templates"]
    assert templates and all(t["header"] is None for t in templates)


# --- API v1 transcript ------------------------------------------------------

def _v1(world, rid, query):
    with app.test_client() as c:
        return c.get(f"/api/v1/recordings/{rid}/transcript?{query}",
                     headers={"Authorization": f"Bearer {world['token']}"})


@pytest.mark.parametrize("fmt", ["json", "text", "srt", "vtt"])
def test_v1_without_translation_has_no_translation_keys(world, fmt):
    body = _v1(world, world["rec"], f"format={fmt}").get_json()
    assert "translation" not in body and "translation_language" not in body


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE"])
def test_v1_translation_json(world, value):
    resp = _v1(world, world["rec"], f"format=json&translation={value}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert [s["text"] for s in body["segments"]] == ["Hola.", "Adios."]
    assert body["translation"] is True and body["translation_language"] == "Spanish"
    assert body["kind"] == "segments"


def test_v1_translation_false_is_the_original(world):
    plain = _v1(world, world["rec"], "format=text").get_json()
    assert _v1(world, world["rec"], "format=text&translation=false").get_json() == plain
    assert _v1(world, world["rec"], "format=text&translation=0").get_json() == plain


def test_v1_translation_text(world):
    body = _v1(world, world["rec"], "format=text&translation=true").get_json()
    assert body == {"format": "text", "content": "Alice: Hola.\nBob: Adios.",
                    "translation": True, "translation_language": "Spanish"}


@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_v1_translation_subtitles(world, fmt):
    original = _v1(world, world["rec"], f"format={fmt}").get_json()["content"]
    body = _v1(world, world["rec"], f"format={fmt}&translation=1").get_json()
    assert body["content"] == original.replace("Hello there.", "Hola.").replace("Hi back.", "Adios.")
    assert body["translation"] is True


def test_v1_translation_of_a_plain_transcript(world):
    body = _v1(world, world["plain_rec"], "format=json&translation=yes").get_json()
    assert body["kind"] == "plain" and body["raw"] == "Solo palabras." and body["translation"] is True


@pytest.mark.parametrize("fmt", ["json", "text", "srt", "vtt"])
def test_v1_translation_missing_is_404(world, fmt):
    resp = _v1(world, world["untranslated"], f"format={fmt}&translation=true")
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "no_translation"


def test_v1_text_with_own_template_id(world):
    body = _v1(world, world["rec"], f"format=text&template_id={world['vtt']}").get_json()
    assert body == {"format": "text", "content": "WEBVTT\n\n" + VTT_BODY}


def test_v1_text_with_template_id_and_translation(world):
    body = _v1(world, world["rec"], f"format=text&template_id={world['vtt']}&translation=1").get_json()
    assert body["content"].startswith("WEBVTT\n\n00:01.500 --> 00:04.299\n<v Alice>Hola.\n")


@pytest.mark.parametrize("which", ["foreign", "missing", "garbage"])
def test_v1_text_with_unusable_template_id_is_404(world, which):
    value = {"foreign": world["foreign"], "missing": 999999999, "garbage": "abc"}[which]
    resp = _v1(world, world["rec"], f"format=text&template_id={value}")
    assert resp.status_code == 404
    assert resp.get_json()["code"] == "template_not_found"


def test_v1_text_without_template_id_uses_the_default(world):
    _set_default(world["vtt"], world["owner"])
    assert _v1(world, world["rec"], "format=text").get_json()["content"] == "WEBVTT\n\n" + VTT_BODY
    assert _v1(world, world["rec"], "format=text&template_id=").get_json()["content"] == "WEBVTT\n\n" + VTT_BODY


def test_v1_template_id_is_ignored_by_other_formats(world):
    for fmt in ("json", "srt", "vtt"):
        assert _v1(world, world["rec"], f"format={fmt}&template_id={world['foreign']}").get_json() == \
            _v1(world, world["rec"], f"format={fmt}").get_json()
