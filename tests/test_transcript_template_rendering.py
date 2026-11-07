"""Transcript template rendering: one renderer behind every path.

The three places that render segments through a template are checked against
outputs recorded from the code before they shared one renderer:
  - file_exporter.format_transcription_with_template (export, /export, API v1 text)
  - GET /recording/<id>/download/transcript (web download, any template_id)
  - transcript_render.render_transcription (timestamps for the summarizer/chat)

EXPECTED_USER holds the output of the first two (they were identical copies),
EXPECTED_LLM the output of the third. Two cases changed on purpose and are
asserted separately in FIXED_USER and FIXED_LLM.

SHARED-DB: the user, templates and recordings are removed afterwards.
"""

import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.app import app, db
from src.file_exporter import format_transcription_with_template
from src.models import Recording, User
from src.models.templates import TranscriptTemplate
from src.utils.transcript_render import render_transcription

app.config["WTF_CSRF_ENABLED"] = False

TEMPLATES = {
    "none": None,
    "simple": "{{speaker}}: {{text}}",
    "timestamped": "[{{start_time}} - {{end_time}}] {{speaker}}: {{text}}",
    "interview": "{{speaker|upper}}:\n{{text}}\n",
    "minutes": "• [{{start_time}}] {{speaker}}: {{text}}",
    "court": "{{index}}    {{speaker|upper}}: {{text}}",
    "srt": "{{index}}\n{{start_time|srt}} --> {{end_time|srt}}\n{{text}}\n",
    "screenplay": "                    {{speaker|upper}}\n        {{text}}\n",
    "every_placeholder": "{{index}} / {{speaker}} / {{text}} / {{start_time}} / {{end_time}}",
    "every_upper": "{{index|upper}} / {{speaker|upper}} / {{text|upper}} / {{start_time|upper}} / {{end_time|upper}}",
    "srt_times": "{{start_time|srt}} --> {{end_time|srt}}",
    "multi_line": "=====\nTime: {{start_time}}\nSpeaker: {{speaker}}\nMessage: {{text}}\n=====",
    "no_placeholders": "---",
    "plain_then_upper": "{{speaker}}: {{text|upper}}",
    "unknown_placeholder": "{{foo}} {{foo|upper}} {{text}}",
    "srt_then_upper": "{{start_time|srt}} {{speaker|upper}}: {{text}}",
}

TRANSCRIPTS = {
    "basic": json.dumps([
        {"speaker": "Alice", "sentence": "Hello there.", "start_time": 1.5, "end_time": 4.299},
        {"speaker": "Bob", "sentence": "Hi back.", "start_time": 4.299, "end_time": 7.0},
    ]),
    "missing": json.dumps([
        {"sentence": "No speaker.", "start_time": None, "end_time": None},
        {"speaker": "Carol", "start_time": 2.0},
        {"speaker": "Dan", "sentence": "No times."},
    ]),
    "over_an_hour": json.dumps([
        {"speaker": "Alice", "sentence": "Late.", "start_time": 3599.9996, "end_time": 3600.0},
        {"speaker": "Bob", "sentence": "Later.", "start_time": 3725.5, "end_time": 7322.25},
        {"speaker": "Eve", "sentence": "Day.", "start_time": 86399.5, "end_time": 90061.125},
    ]),
    "unicode": json.dumps([
        {"speaker": "Zoë", "sentence": "Grüße, 你好 ✓", "start_time": 0.0, "end_time": 1.0},
    ], ensure_ascii=False),
    "empty_list": "[]",
    "plain_text": "Just some words, not JSON.",
    "json_object": '{"speaker": "Alice", "sentence": "Not a list."}',
    "json_number": "42",
}

CASES = [(t, s) for t in TEMPLATES for s in TRANSCRIPTS]


@pytest.fixture(scope="module")
def world():
    """One user, one template per non-default entry, one recording per transcript."""
    with app.app_context():
        suffix = uuid.uuid4().hex[:10]
        user = User(username=f"ttr_{suffix}", email=f"ttr_{suffix}@local.test", password="x")
        db.session.add(user)
        db.session.commit()
        templates = {}
        for key, text in TEMPLATES.items():
            if text is None:
                continue
            tmpl = TranscriptTemplate(user_id=user.id, name=f"T-{key}", template=text, is_default=False)
            db.session.add(tmpl)
            db.session.commit()
            templates[key] = tmpl.id
        recordings = {}
        for key, text in TRANSCRIPTS.items():
            rec = Recording(user_id=user.id, title=f"R-{key}", status="COMPLETED",
                            original_filename="a.wav", transcription=text)
            db.session.add(rec)
            db.session.commit()
            recordings[key] = rec.id
        ids = {"user": user.id, "templates": templates, "recordings": recordings}
    yield ids
    with app.app_context():
        db.session.rollback()
        Recording.query.filter_by(user_id=ids["user"]).delete()
        TranscriptTemplate.query.filter_by(user_id=ids["user"]).delete()
        User.query.filter_by(id=ids["user"]).delete()
        db.session.commit()


def _set_default(world, template_key):
    """Make `template_key` the user's only default template (none for "none")."""
    with app.app_context():
        TranscriptTemplate.query.filter_by(user_id=world["user"]).update({"is_default": False})
        if template_key in world["templates"]:
            db.session.get(TranscriptTemplate, world["templates"][template_key]).is_default = True
        db.session.commit()


def render_export(world, template_key, transcript_key):
    _set_default(world, template_key)
    with app.app_context():
        user = db.session.get(User, world["user"])
        return format_transcription_with_template(TRANSCRIPTS[transcript_key], user)


def render_download(world, template_key, transcript_key, extra=""):
    _set_default(world, None)
    query = f"?template_id={world['templates'][template_key]}" if template_key in world["templates"] else "?"
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(world["user"])
        sess["_fresh"] = True
    resp = client.get(f"/recording/{world['recordings'][transcript_key]}/download/transcript{query}{extra}")
    assert resp.status_code == 200, resp.data
    return resp.data.decode("utf-8")


def render_llm(template_key, transcript_key):
    return render_transcription(TRANSCRIPTS[transcript_key], TEMPLATES[template_key])


# Recorded from the code before the shared renderer (see the module docstring).
EXPECTED_USER = {
    'none/basic': '[Alice]: Hello there.\n[Bob]: Hi back.',
    'none/missing': '[Unknown]: No speaker.\n[Carol]: \n[Dan]: No times.',
    'none/over_an_hour': '[Alice]: Late.\n[Bob]: Later.\n[Eve]: Day.',
    'none/unicode': '[Zoë]: Grüße, 你好 ✓',
    'none/empty_list': '',
    'none/plain_text': 'Just some words, not JSON.',
    'none/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'none/json_number': '42',
    'simple/basic': 'Alice: Hello there.\nBob: Hi back.',
    'simple/missing': 'Unknown: No speaker.\nCarol: \nDan: No times.',
    'simple/over_an_hour': 'Alice: Late.\nBob: Later.\nEve: Day.',
    'simple/unicode': 'Zoë: Grüße, 你好 ✓',
    'simple/empty_list': '',
    'simple/plain_text': 'Just some words, not JSON.',
    'simple/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'simple/json_number': '42',
    'timestamped/basic': '[00:00:01 - 00:00:04] Alice: Hello there.\n[00:00:04 - 00:00:07] Bob: Hi back.',
    'timestamped/missing': '[00:00:00 - 00:00:00] Unknown: No speaker.\n[00:00:02 - 00:00:00] Carol: \n[00:00:00 - 00:00:00] Dan: No times.',
    'timestamped/over_an_hour': '[00:59:59 - 01:00:00] Alice: Late.\n[01:02:05 - 02:02:02] Bob: Later.\n[23:59:59 - 25:01:01] Eve: Day.',
    'timestamped/unicode': '[00:00:00 - 00:00:01] Zoë: Grüße, 你好 ✓',
    'timestamped/empty_list': '',
    'timestamped/plain_text': 'Just some words, not JSON.',
    'timestamped/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'timestamped/json_number': '42',
    'interview/basic': 'ALICE:\nHello there.\n\nBOB:\nHi back.\n',
    'interview/missing': 'UNKNOWN:\nNo speaker.\n\nCAROL:\n\n\nDAN:\nNo times.\n',
    'interview/over_an_hour': 'ALICE:\nLate.\n\nBOB:\nLater.\n\nEVE:\nDay.\n',
    'interview/unicode': 'ZOË:\nGrüße, 你好 ✓\n',
    'interview/empty_list': '',
    'interview/plain_text': 'Just some words, not JSON.',
    'interview/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'interview/json_number': '42',
    'minutes/basic': '• [00:00:01] Alice: Hello there.\n• [00:00:04] Bob: Hi back.',
    'minutes/missing': '• [00:00:00] Unknown: No speaker.\n• [00:00:02] Carol: \n• [00:00:00] Dan: No times.',
    'minutes/over_an_hour': '• [00:59:59] Alice: Late.\n• [01:02:05] Bob: Later.\n• [23:59:59] Eve: Day.',
    'minutes/unicode': '• [00:00:00] Zoë: Grüße, 你好 ✓',
    'minutes/empty_list': '',
    'minutes/plain_text': 'Just some words, not JSON.',
    'minutes/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'minutes/json_number': '42',
    'court/basic': '1    ALICE: Hello there.\n2    BOB: Hi back.',
    'court/missing': '1    UNKNOWN: No speaker.\n2    CAROL: \n3    DAN: No times.',
    'court/over_an_hour': '1    ALICE: Late.\n2    BOB: Later.\n3    EVE: Day.',
    'court/unicode': '1    ZOË: Grüße, 你好 ✓',
    'court/empty_list': '',
    'court/plain_text': 'Just some words, not JSON.',
    'court/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'court/json_number': '42',
    'srt/basic': '1\n00:00:01,500 --> 00:00:04,299\nHello there.\n\n2\n00:00:04,299 --> 00:00:07,000\nHi back.\n',
    'srt/missing': '1\n00:00:00,000 --> 00:00:00,000\nNo speaker.\n\n2\n00:00:02,000 --> 00:00:00,000\n\n\n3\n00:00:00,000 --> 00:00:00,000\nNo times.\n',
    'srt/over_an_hour': '1\n00:59:59,999 --> 01:00:00,000\nLate.\n\n2\n01:02:05,500 --> 02:02:02,250\nLater.\n\n3\n23:59:59,500 --> 25:01:01,125\nDay.\n',
    'srt/unicode': '1\n00:00:00,000 --> 00:00:01,000\nGrüße, 你好 ✓\n',
    'srt/empty_list': '',
    'srt/plain_text': 'Just some words, not JSON.',
    'srt/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'srt/json_number': '42',
    'screenplay/basic': '                    ALICE\n        Hello there.\n\n                    BOB\n        Hi back.\n',
    'screenplay/missing': '                    UNKNOWN\n        No speaker.\n\n                    CAROL\n        \n\n                    DAN\n        No times.\n',
    'screenplay/over_an_hour': '                    ALICE\n        Late.\n\n                    BOB\n        Later.\n\n                    EVE\n        Day.\n',
    'screenplay/unicode': '                    ZOË\n        Grüße, 你好 ✓\n',
    'screenplay/empty_list': '',
    'screenplay/plain_text': 'Just some words, not JSON.',
    'screenplay/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'screenplay/json_number': '42',
    'every_placeholder/basic': '1 / Alice / Hello there. / 00:00:01 / 00:00:04\n2 / Bob / Hi back. / 00:00:04 / 00:00:07',
    'every_placeholder/missing': '1 / Unknown / No speaker. / 00:00:00 / 00:00:00\n2 / Carol /  / 00:00:02 / 00:00:00\n3 / Dan / No times. / 00:00:00 / 00:00:00',
    'every_placeholder/over_an_hour': '1 / Alice / Late. / 00:59:59 / 01:00:00\n2 / Bob / Later. / 01:02:05 / 02:02:02\n3 / Eve / Day. / 23:59:59 / 25:01:01',
    'every_placeholder/unicode': '1 / Zoë / Grüße, 你好 ✓ / 00:00:00 / 00:00:01',
    'every_placeholder/empty_list': '',
    'every_placeholder/plain_text': 'Just some words, not JSON.',
    'every_placeholder/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'every_placeholder/json_number': '42',
    'every_upper/basic': '1 / ALICE / HELLO THERE. / 00:00:01 / 00:00:04\n2 / BOB / HI BACK. / 00:00:04 / 00:00:07',
    'every_upper/missing': '1 / UNKNOWN / NO SPEAKER. / 00:00:00 / 00:00:00\n2 / CAROL /  / 00:00:02 / 00:00:00\n3 / DAN / NO TIMES. / 00:00:00 / 00:00:00',
    'every_upper/over_an_hour': '1 / ALICE / LATE. / 00:59:59 / 01:00:00\n2 / BOB / LATER. / 01:02:05 / 02:02:02\n3 / EVE / DAY. / 23:59:59 / 25:01:01',
    'every_upper/unicode': '1 / ZOË / GRÜSSE, 你好 ✓ / 00:00:00 / 00:00:01',
    'every_upper/empty_list': '',
    'every_upper/plain_text': 'Just some words, not JSON.',
    'every_upper/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'every_upper/json_number': '42',
    'srt_times/basic': '00:00:01,500 --> 00:00:04,299\n00:00:04,299 --> 00:00:07,000',
    'srt_times/missing': '00:00:00,000 --> 00:00:00,000\n00:00:02,000 --> 00:00:00,000\n00:00:00,000 --> 00:00:00,000',
    'srt_times/over_an_hour': '00:59:59,999 --> 01:00:00,000\n01:02:05,500 --> 02:02:02,250\n23:59:59,500 --> 25:01:01,125',
    'srt_times/unicode': '00:00:00,000 --> 00:00:01,000',
    'srt_times/empty_list': '',
    'srt_times/plain_text': 'Just some words, not JSON.',
    'srt_times/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'srt_times/json_number': '42',
    'multi_line/basic': '=====\nTime: 00:00:01\nSpeaker: Alice\nMessage: Hello there.\n=====\n=====\nTime: 00:00:04\nSpeaker: Bob\nMessage: Hi back.\n=====',
    'multi_line/missing': '=====\nTime: 00:00:00\nSpeaker: Unknown\nMessage: No speaker.\n=====\n=====\nTime: 00:00:02\nSpeaker: Carol\nMessage: \n=====\n=====\nTime: 00:00:00\nSpeaker: Dan\nMessage: No times.\n=====',
    'multi_line/over_an_hour': '=====\nTime: 00:59:59\nSpeaker: Alice\nMessage: Late.\n=====\n=====\nTime: 01:02:05\nSpeaker: Bob\nMessage: Later.\n=====\n=====\nTime: 23:59:59\nSpeaker: Eve\nMessage: Day.\n=====',
    'multi_line/unicode': '=====\nTime: 00:00:00\nSpeaker: Zoë\nMessage: Grüße, 你好 ✓\n=====',
    'multi_line/empty_list': '',
    'multi_line/plain_text': 'Just some words, not JSON.',
    'multi_line/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'multi_line/json_number': '42',
    'no_placeholders/basic': '---\n---',
    'no_placeholders/missing': '---\n---\n---',
    'no_placeholders/over_an_hour': '---\n---\n---',
    'no_placeholders/unicode': '---',
    'no_placeholders/empty_list': '',
    'no_placeholders/plain_text': 'Just some words, not JSON.',
    'no_placeholders/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'no_placeholders/json_number': '42',
    'plain_then_upper/basic': 'Alice: HELLO THERE.\nBob: HI BACK.',
    'plain_then_upper/missing': 'Unknown: NO SPEAKER.\nCarol: \nDan: NO TIMES.',
    'plain_then_upper/over_an_hour': 'Alice: LATE.\nBob: LATER.\nEve: DAY.',
    'plain_then_upper/unicode': 'Zoë: GRÜSSE, 你好 ✓',
    'plain_then_upper/empty_list': '',
    'plain_then_upper/plain_text': 'Just some words, not JSON.',
    'plain_then_upper/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'plain_then_upper/json_number': '42',
    'unknown_placeholder/basic': ' Hello there.\n Hi back.',
    'unknown_placeholder/missing': ' No speaker.\n \n No times.',
    'unknown_placeholder/over_an_hour': ' Late.\n Later.\n Day.',
    'unknown_placeholder/unicode': ' Grüße, 你好 ✓',
    'unknown_placeholder/empty_list': '',
    'unknown_placeholder/plain_text': 'Just some words, not JSON.',
    'unknown_placeholder/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'unknown_placeholder/json_number': '42',
    'srt_then_upper/basic': ': Hello there.\n: Hi back.',
    'srt_then_upper/missing': ': No speaker.\n: \n: No times.',
    'srt_then_upper/over_an_hour': ': Late.\n: Later.\n: Day.',
    'srt_then_upper/unicode': ': Grüße, 你好 ✓',
    'srt_then_upper/empty_list': '',
    'srt_then_upper/plain_text': 'Just some words, not JSON.',
    'srt_then_upper/json_object': '{"speaker": "Alice", "sentence": "Not a list."}',
    'srt_then_upper/json_number': '42',
}
EXPECTED_LLM = {
    'none/basic': '[00:00:01] Alice: Hello there.\n[00:00:04] Bob: Hi back.',
    'none/missing': '[00:00:00] Unknown: No speaker.\n[00:00:02] Carol: \n[00:00:00] Dan: No times.',
    'none/over_an_hour': '[00:59:59] Alice: Late.\n[01:02:05] Bob: Later.\n[23:59:59] Eve: Day.',
    'none/unicode': '[00:00:00] Zoë: Grüße, 你好 ✓',
    'none/empty_list': '',
    'none/plain_text': None,
    'none/json_object': None,
    'none/json_number': None,
    'simple/basic': 'Alice: Hello there.\nBob: Hi back.',
    'simple/missing': 'Unknown: No speaker.\nCarol: \nDan: No times.',
    'simple/over_an_hour': 'Alice: Late.\nBob: Later.\nEve: Day.',
    'simple/unicode': 'Zoë: Grüße, 你好 ✓',
    'simple/empty_list': '',
    'simple/plain_text': None,
    'simple/json_object': None,
    'simple/json_number': None,
    'timestamped/basic': '[00:00:01 - 00:00:04] Alice: Hello there.\n[00:00:04 - 00:00:07] Bob: Hi back.',
    'timestamped/missing': '[00:00:00 - 00:00:00] Unknown: No speaker.\n[00:00:02 - 00:00:00] Carol: \n[00:00:00 - 00:00:00] Dan: No times.',
    'timestamped/over_an_hour': '[00:59:59 - 01:00:00] Alice: Late.\n[01:02:05 - 02:02:02] Bob: Later.\n[23:59:59 - 25:01:01] Eve: Day.',
    'timestamped/unicode': '[00:00:00 - 00:00:01] Zoë: Grüße, 你好 ✓',
    'timestamped/empty_list': '',
    'timestamped/plain_text': None,
    'timestamped/json_object': None,
    'timestamped/json_number': None,
    'interview/basic': 'ALICE:\nHello there.\n\nBOB:\nHi back.\n',
    'interview/missing': 'UNKNOWN:\nNo speaker.\n\nCAROL:\n\n\nDAN:\nNo times.\n',
    'interview/over_an_hour': 'ALICE:\nLate.\n\nBOB:\nLater.\n\nEVE:\nDay.\n',
    'interview/unicode': 'ZOË:\nGrüße, 你好 ✓\n',
    'interview/empty_list': '',
    'interview/plain_text': None,
    'interview/json_object': None,
    'interview/json_number': None,
    'minutes/basic': '• [00:00:01] Alice: Hello there.\n• [00:00:04] Bob: Hi back.',
    'minutes/missing': '• [00:00:00] Unknown: No speaker.\n• [00:00:02] Carol: \n• [00:00:00] Dan: No times.',
    'minutes/over_an_hour': '• [00:59:59] Alice: Late.\n• [01:02:05] Bob: Later.\n• [23:59:59] Eve: Day.',
    'minutes/unicode': '• [00:00:00] Zoë: Grüße, 你好 ✓',
    'minutes/empty_list': '',
    'minutes/plain_text': None,
    'minutes/json_object': None,
    'minutes/json_number': None,
    'court/basic': ': Hello there.\n: Hi back.',
    'court/missing': ': No speaker.\n: \n: No times.',
    'court/over_an_hour': ': Late.\n: Later.\n: Day.',
    'court/unicode': ': Grüße, 你好 ✓',
    'court/empty_list': '',
    'court/plain_text': None,
    'court/json_object': None,
    'court/json_number': None,
    'srt/basic': '1\n00:00:01,500 --> 00:00:04,299\nHello there.\n\n2\n00:00:04,299 --> 00:00:07,000\nHi back.\n',
    'srt/missing': '1\n00:00:00,000 --> 00:00:00,000\nNo speaker.\n\n2\n00:00:02,000 --> 00:00:00,000\n\n\n3\n00:00:00,000 --> 00:00:00,000\nNo times.\n',
    'srt/over_an_hour': '1\n00:59:59,999 --> 01:00:00,000\nLate.\n\n2\n01:02:05,500 --> 02:02:02,250\nLater.\n\n3\n23:59:59,500 --> 25:01:01,125\nDay.\n',
    'srt/unicode': '1\n00:00:00,000 --> 00:00:01,000\nGrüße, 你好 ✓\n',
    'srt/empty_list': '',
    'srt/plain_text': None,
    'srt/json_object': None,
    'srt/json_number': None,
    'screenplay/basic': '                    ALICE\n        Hello there.\n\n                    BOB\n        Hi back.\n',
    'screenplay/missing': '                    UNKNOWN\n        No speaker.\n\n                    CAROL\n        \n\n                    DAN\n        No times.\n',
    'screenplay/over_an_hour': '                    ALICE\n        Late.\n\n                    BOB\n        Later.\n\n                    EVE\n        Day.\n',
    'screenplay/unicode': '                    ZOË\n        Grüße, 你好 ✓\n',
    'screenplay/empty_list': '',
    'screenplay/plain_text': None,
    'screenplay/json_object': None,
    'screenplay/json_number': None,
    'every_placeholder/basic': '1 / Alice / Hello there. / 00:00:01 / 00:00:04\n2 / Bob / Hi back. / 00:00:04 / 00:00:07',
    'every_placeholder/missing': '1 / Unknown / No speaker. / 00:00:00 / 00:00:00\n2 / Carol /  / 00:00:02 / 00:00:00\n3 / Dan / No times. / 00:00:00 / 00:00:00',
    'every_placeholder/over_an_hour': '1 / Alice / Late. / 00:59:59 / 01:00:00\n2 / Bob / Later. / 01:02:05 / 02:02:02\n3 / Eve / Day. / 23:59:59 / 25:01:01',
    'every_placeholder/unicode': '1 / Zoë / Grüße, 你好 ✓ / 00:00:00 / 00:00:01',
    'every_placeholder/empty_list': '',
    'every_placeholder/plain_text': None,
    'every_placeholder/json_object': None,
    'every_placeholder/json_number': None,
    'every_upper/basic': '1 / ALICE / HELLO THERE. / 00:00:01 / 00:00:04\n2 / BOB / HI BACK. / 00:00:04 / 00:00:07',
    'every_upper/missing': '1 / UNKNOWN / NO SPEAKER. / 00:00:00 / 00:00:00\n2 / CAROL /  / 00:00:02 / 00:00:00\n3 / DAN / NO TIMES. / 00:00:00 / 00:00:00',
    'every_upper/over_an_hour': '1 / ALICE / LATE. / 00:59:59 / 01:00:00\n2 / BOB / LATER. / 01:02:05 / 02:02:02\n3 / EVE / DAY. / 23:59:59 / 25:01:01',
    'every_upper/unicode': '1 / ZOË / GRÜSSE, 你好 ✓ / 00:00:00 / 00:00:01',
    'every_upper/empty_list': '',
    'every_upper/plain_text': None,
    'every_upper/json_object': None,
    'every_upper/json_number': None,
    'srt_times/basic': '00:00:01,500 --> 00:00:04,299\n00:00:04,299 --> 00:00:07,000',
    'srt_times/missing': '00:00:00,000 --> 00:00:00,000\n00:00:02,000 --> 00:00:00,000\n00:00:00,000 --> 00:00:00,000',
    'srt_times/over_an_hour': '00:59:59,999 --> 01:00:00,000\n01:02:05,500 --> 02:02:02,250\n23:59:59,500 --> 25:01:01,125',
    'srt_times/unicode': '00:00:00,000 --> 00:00:01,000',
    'srt_times/empty_list': '',
    'srt_times/plain_text': None,
    'srt_times/json_object': None,
    'srt_times/json_number': None,
    'multi_line/basic': '=====\nTime: 00:00:01\nSpeaker: Alice\nMessage: Hello there.\n=====\n=====\nTime: 00:00:04\nSpeaker: Bob\nMessage: Hi back.\n=====',
    'multi_line/missing': '=====\nTime: 00:00:00\nSpeaker: Unknown\nMessage: No speaker.\n=====\n=====\nTime: 00:00:02\nSpeaker: Carol\nMessage: \n=====\n=====\nTime: 00:00:00\nSpeaker: Dan\nMessage: No times.\n=====',
    'multi_line/over_an_hour': '=====\nTime: 00:59:59\nSpeaker: Alice\nMessage: Late.\n=====\n=====\nTime: 01:02:05\nSpeaker: Bob\nMessage: Later.\n=====\n=====\nTime: 23:59:59\nSpeaker: Eve\nMessage: Day.\n=====',
    'multi_line/unicode': '=====\nTime: 00:00:00\nSpeaker: Zoë\nMessage: Grüße, 你好 ✓\n=====',
    'multi_line/empty_list': '',
    'multi_line/plain_text': None,
    'multi_line/json_object': None,
    'multi_line/json_number': None,
    'no_placeholders/basic': '---\n---',
    'no_placeholders/missing': '---\n---\n---',
    'no_placeholders/over_an_hour': '---\n---\n---',
    'no_placeholders/unicode': '---',
    'no_placeholders/empty_list': '',
    'no_placeholders/plain_text': None,
    'no_placeholders/json_object': None,
    'no_placeholders/json_number': None,
    'plain_then_upper/basic': '\n',
    'plain_then_upper/missing': '\n\n',
    'plain_then_upper/over_an_hour': '\n\n',
    'plain_then_upper/unicode': '',
    'plain_then_upper/empty_list': '',
    'plain_then_upper/plain_text': None,
    'plain_then_upper/json_object': None,
    'plain_then_upper/json_number': None,
    'unknown_placeholder/basic': ' Hello there.\n Hi back.',
    'unknown_placeholder/missing': ' No speaker.\n \n No times.',
    'unknown_placeholder/over_an_hour': ' Late.\n Later.\n Day.',
    'unknown_placeholder/unicode': ' Grüße, 你好 ✓',
    'unknown_placeholder/empty_list': '',
    'unknown_placeholder/plain_text': None,
    'unknown_placeholder/json_object': None,
    'unknown_placeholder/json_number': None,
    'srt_then_upper/basic': '00:00:01,500 ALICE: Hello there.\n00:00:04,299 BOB: Hi back.',
    'srt_then_upper/missing': '00:00:00,000 UNKNOWN: No speaker.\n00:00:02,000 CAROL: \n00:00:00,000 DAN: No times.',
    'srt_then_upper/over_an_hour': '00:59:59,999 ALICE: Late.\n01:02:05,500 BOB: Later.\n23:59:59,500 EVE: Day.',
    'srt_then_upper/unicode': '00:00:00,000 ZOË: Grüße, 你好 ✓',
    'srt_then_upper/empty_list': '',
    'srt_then_upper/plain_text': None,
    'srt_then_upper/json_object': None,
    'srt_then_upper/json_number': None,
}

# The lazy |upper pattern used to start at the first "{{" of the line, so a
# {{start_time|srt}} before a {{x|upper}} was swallowed by the match: the old
# export and download rendered "{{start_time|srt}} {{speaker|upper}}: {{text}}"
# as ": Hello there.". The match no longer crosses a placeholder.
FIXED_USER = {}
# The same pattern made the summarizer path drop everything up to an |upper
# that followed a plain placeholder: "{{speaker}}: {{text|upper}}" rendered as
# an empty line.
FIXED_LLM = {}


@pytest.mark.parametrize("template_key,transcript_key", CASES)
def test_export_matches_recorded_output(world, template_key, transcript_key):
    case = f"{template_key}/{transcript_key}"
    expected = FIXED_USER.get(case, EXPECTED_USER[case])
    assert render_export(world, template_key, transcript_key) == expected


@pytest.mark.parametrize("template_key,transcript_key", CASES)
def test_download_matches_recorded_output(world, template_key, transcript_key):
    case = f"{template_key}/{transcript_key}"
    expected = FIXED_USER.get(case, EXPECTED_USER[case])
    assert render_download(world, template_key, transcript_key) == expected


@pytest.mark.parametrize("template_key,transcript_key", CASES)
def test_llm_render_matches_recorded_output(template_key, transcript_key):
    case = f"{template_key}/{transcript_key}"
    expected = FIXED_LLM.get(case, EXPECTED_LLM[case])
    assert render_llm(template_key, transcript_key) == expected
