"""What the public share page receives (/share/<public_id>).

A person with only the link gets the page's data as JSON in #share-data
(src/api/shares.py, view_shared_recording). These tests pin that down: the
fields, the short form of the transcript with nothing of a stored segment
beyond speaker, times and text, the summary and notes only when the link
shares them, and the plain HTML copy of the transcript that
READABLE_PUBLIC_LINKS adds for readers that do not run the page's script.
Text in a summary, notes or a transcript that looks like a template
expression must never reach the part of the page that Vue compiles.
"""
import json
import re
import secrets

import pytest

from src.api import shares
from src.api.shares import public_transcript
from src.app import app, db
from src.models import Recording, Share, User

app.config["WTF_CSRF_ENABLED"] = False

DATA_KEYS = {"public_id", "title", "transcript", "summary", "summary_raw", "notes", "notes_raw",
             "meeting_date", "mime_type", "audio_deleted_at", "audio_duration"}
SEGMENTS = [
    {"speaker": "Ana", "sentence": "Good morning.", "start_time": 0.0, "end_time": 2.5,
     "words": [{"word": "Good", "start": 0.0}], "speaker_id": 41, "embedding": [0.1, 0.2], "confidence": 0.93},
    {"speaker": "Ana", "sentence": "Shall we <b>start</b>? ${ 7 * 7 }", "start_time": 2.5, "end_time": 4},
    {"speaker": "Ben", "sentence": "Yes.", "startTime": 4, "endTime": 6},
    {"sentence": "(a door closes)", "start_time": "6.0", "end_time": None},
]
PROBE = "A probe ${ 7 * 7 } in the text."


@pytest.fixture
def shared():
    """A recording with a public link; yields a function that renders the page."""
    with app.app_context():
        name = f"spp_{secrets.token_hex(4)}"
        user = User(username=name, email=f"{name}@local.test", password="x", can_share_publicly=True)
        db.session.add(user)
        db.session.commit()
        recording = Recording(user_id=user.id, title="Planning <call>", status="COMPLETED", original_filename="secret-name.wav",
                              transcription=json.dumps(SEGMENTS), summary=PROBE, notes=PROBE, participants="Ana, Ben")
        db.session.add(recording)
        db.session.commit()
        share = Share(recording_id=recording.id, user_id=user.id, share_summary=True, share_notes=True)
        db.session.add(share)
        db.session.commit()
        ids = {"user": user.id, "recording": recording.id, "share": share.id, "public_id": share.public_id,
               "username": name, "email": user.email}

    def page(**changes):
        if changes:
            with app.app_context():
                for model, key in ((Share, "share"), (Recording, "recording")):
                    row = db.session.get(model, ids[key])
                    for field, value in changes.items():
                        if hasattr(row, field):
                            setattr(row, field, value)
                db.session.commit()
        response = app.test_client().get(f"/share/{ids['public_id']}")
        assert response.status_code == 200
        return response.get_data(as_text=True)

    page.ids = ids
    yield page
    with app.app_context():
        db.session.rollback()
        Share.query.filter_by(recording_id=ids["recording"]).delete()
        db.session.delete(db.session.get(Recording, ids["recording"]))
        db.session.delete(db.session.get(User, ids["user"]))
        db.session.commit()


def _data(html):
    match = re.search(r'<script type="application/json" id="share-data">(.*?)</script>', html, re.S)
    assert match, "no #share-data in the page"
    return json.loads(match.group(1))


def _vue_template(html):
    """The part of the page that Vue compiles: the #app element, without what v-pre keeps out."""
    start = html.index('<div id="app"')
    end = html.index('<script type="application/json" id="share-data">')
    template = html[start:end]
    return re.sub(r'<div class="[^"]*(?:summary|notes)-box" v-pre>.*?</div>\s*</div>', "", template, flags=re.S)


# --- public_transcript -------------------------------------------------------

def test_a_stored_segment_leaves_the_server_as_speaker_times_and_text():
    result = public_transcript(json.dumps(SEGMENTS))
    assert result == {
        "speakers": ["Ana", "Ben"],
        "segments": [
            [0, 0.0, 2.5, "Good morning."],
            [0, 2.5, 4, "Shall we <b>start</b>? ${ 7 * 7 }"],
            [1, 4, 6, "Yes."],
            [-1, None, None, "(a door closes)"],
        ],
    }


@pytest.mark.parametrize("stored, text", [
    (None, ""), ("", ""), ("Just text.\nTwo lines.", "Just text.\nTwo lines."),
    ('{"not": "a list"}', '{"not": "a list"}'), ("[Ana]: Hello.", "[Ana]: Hello."),
])
def test_any_other_transcript_is_text(stored, text):
    assert public_transcript(stored) == {"text": text}


def test_odd_segments_do_not_break_the_page():
    stored = json.dumps([{"speaker": 7, "sentence": None, "start_time": True}, "a string", None, {"speaker": "", "sentence": "x"}])
    assert public_transcript(stored) == {"speakers": [], "segments": [[-1, None, None, ""], [-1, None, None, "x"]]}


def test_twenty_thousand_segments_stay_small():
    stored = json.dumps([{"speaker": f"SPEAKER_0{i % 4}", "sentence": f"Segment {i}: a sentence of a long meeting.",
                          "start_time": i * 1.5, "end_time": i * 1.5 + 1.4} for i in range(20000)])
    sent = json.dumps(public_transcript(stored))
    # The stored JSON, embedded as a string in the page's data, was larger than itself
    assert len(sent) < 0.7 * len(json.dumps(stored))


# --- the page ----------------------------------------------------------------

def test_the_page_receives_only_what_it_shows(shared):
    html = shared()
    data = _data(html)
    assert set(data) == DATA_KEYS
    assert data["transcript"] == public_transcript(json.dumps(SEGMENTS))
    assert data["title"] == "Planning <call>" and data["public_id"] == shared.ids["public_id"]
    block = html.split('id="share-data">', 1)[1].split("</script>", 1)[0]
    # Nothing of the stored segments beyond the four values, and nothing about the owner
    for private in ("words", "embedding", "confidence", "speaker_id"):
        assert private not in block, private
    for private in ("secret-name.wav", shared.ids["username"], shared.ids["email"]):
        assert private not in html, private
    # The block cannot be closed, or opened as markup, from inside
    assert "<" not in block and ">" not in block and "'" not in block
    assert 'name="robots" content="noindex' in html


def test_summary_and_notes_only_when_the_link_shares_them(shared):
    data = _data(shared())
    assert data["summary_raw"] == PROBE and data["notes_raw"] == PROBE
    html = shared(share_notes=False)
    data = _data(html)
    assert data["notes"] is None and data["notes_raw"] is None and data["summary_raw"] == PROBE
    assert "notes-box" not in html
    html = shared(share_summary=False)
    data = _data(html)
    assert data["summary"] is None and data["summary_raw"] is None
    assert "summary-box" not in html and PROBE not in html.replace("\\u0027", "'")


def test_text_of_the_recording_is_never_part_of_the_vue_template(shared):
    """The page's markup inside #app is compiled by Vue as a template, with ${ } as its
    delimiters. A summary, notes or a segment with such text in that markup would be
    evaluated in the visitor's browser."""
    html = shared()
    assert "${ 7 * 7 }" in html
    template = _vue_template(html)
    assert "7 * 7" not in template
    assert html.count("v-pre>") == 2
    for box in ("summary-box", "notes-box"):
        assert re.search(rf'<div class="[^"]*{box}" v-pre>\s*<p[^>]*>A probe \$\{{ 7 \* 7 \}} in the text\.</p>', html), box


def test_readable_links_add_the_transcript_as_plain_html(shared, monkeypatch):
    plain = shared()
    assert "<noscript>" not in plain and "transcript-readable" not in plain
    monkeypatch.setattr(shares, "READABLE_PUBLIC_LINKS", True)
    html = shared()
    assert _data(html) == _data(plain)
    readable = html.split("<noscript>", 1)[1].split("</noscript>", 1)[0]
    assert html.index("<noscript>") > html.index('id="share-data"'), "the readable copy is inside the Vue template"
    assert "7 * 7" not in _vue_template(html)
    assert "<h1>Planning &lt;call&gt;</h1>" in readable
    # One heading per turn, every segment as a paragraph, the text escaped
    assert re.findall(r"<h2>(.*?)</h2>", readable) == ["Ana", "Ben"]
    assert re.findall(r"<p>(.*?)</p>", readable) == [
        "Good morning.", "Shall we &lt;b&gt;start&lt;/b&gt;? ${ 7 * 7 }", "Yes.", "(a door closes)"]


def test_readable_links_with_a_text_transcript(shared, monkeypatch):
    monkeypatch.setattr(shares, "READABLE_PUBLIC_LINKS", True)
    html = shared(transcription="First line.\n<i>Second</i> line.")
    assert _data(html)["transcript"] == {"text": "First line.\n<i>Second</i> line."}
    readable = html.split("<noscript>", 1)[1].split("</noscript>", 1)[0]
    assert "First line.\n&lt;i&gt;Second&lt;/i&gt; line." in readable
