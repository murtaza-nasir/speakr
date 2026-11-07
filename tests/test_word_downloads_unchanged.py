"""The summary, notes and chat Word downloads, pinned to the files they return.

The values were first captured before the shared helpers in
src/services/document.py (#554, #555). They were re-recorded on purpose when
the downloads moved to one font (Arial on the styles, so runs carry no font of
their own) and to style spacing in place of empty paragraphs.

Each case records the status, Content-Type and Content-Disposition, then every
paragraph (style, text) and every run (text, font name, east-Asia font, bold,
italic) of the document, including table cells. An ASCII and a Japanese title
cover both Content-Disposition forms. The chat case
includes an assistant turn with thinking, so the [Model Reasoning] paragraph
is part of the pinned file.
"""
import email.utils
import io
import logging
import os
import sys
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document
from docx.oxml.ns import qn

import src.api.recordings as rec_module
import src.services.document as document_module
from src.app import app, db
from src.models import User, Recording
from src.models.organization import Tag, RecordingTag

app.config["WTF_CSRF_ENABLED"] = False

_PREFIX = "wordpin"

_BODY = (
    "## Overview\n"
    "\n"
    "Plain line with **bold**, *italic*, ***both***, ~~gone~~ and `code`.\n"
    "日本語の行に **太字** と `コード` と [リンク](https://example.com/会議) があります。\n"
    "\n"
    "- first item\n"
    "- 二番目の項目\n"
    "\n"
    "1. one\n"
    "2. 二\n"
    "\n"
    "| Name | 担当 |\n"
    "| --- | --- |\n"
    "| Ada | 予算 |\n"
    "\n"
    "---\n"
    "\n"
    "```\n"
    "print('x')\n"
    "```\n"
)

_CHAT_MESSAGES = [
    {"role": "user", "content": "What was discussed?"},
    {"role": "assistant", "content": "The **roadmap**.", "thinking": "hidden-reasoning"},
    {"role": "system", "content": "A note."},
    {"role": "user", "content": "予算は？"},
    {"role": "assistant", "content": "予算は **増額** です。", "thinking": "考え中"},
    {"role": "user"},
]

_TITLES = {"ascii": "Weekly Sync", "japanese": "週次会議"}


def _run_info(run):
    rpr = run._element.rPr
    fonts = rpr.rFonts if rpr is not None else None
    east_asia = fonts.get(qn("w:eastAsia")) if fonts is not None else None
    return [run.text, run.font.name, east_asia, run.bold, run.italic]


def _paragraph_info(paragraph):
    return [paragraph.style.name, paragraph.text, [_run_info(r) for r in paragraph.runs]]


def _capture(resp):
    out = {
        "status": resp.status_code,
        "content_type": resp.headers.get("Content-Type"),
        "disposition": resp.headers.get("Content-Disposition"),
    }
    if resp.status_code != 200:
        out["code"] = resp.get_json().get("code")
        return out
    doc = Document(io.BytesIO(resp.data))
    out["paragraphs"] = [_paragraph_info(p) for p in doc.paragraphs]
    out["tables"] = [
        [[[_paragraph_info(p) for p in cell.paragraphs] for cell in row.cells] for row in table.rows]
        for table in doc.tables
    ]
    return out


@pytest.fixture(scope="module")
def recordings():
    with app.app_context():
        suffix = uuid.uuid4().hex[:8]
        user = User(username=f"{_PREFIX}_{suffix}", email=f"{_PREFIX}_{suffix}@test.local",
                    password="x", timezone="UTC")
        db.session.add(user)
        db.session.commit()
        ids = {}
        for key, title in _TITLES.items():
            rec = Recording(
                user_id=user.id, title=title, status="COMPLETED",
                audio_path="local://wordpin/a.mp3", original_filename="a.mp3",
                transcription="hello", summary=_BODY, notes=_BODY,
                participants="Ada, Lin" if key == "ascii" else "佐藤, 鈴木",
                created_at=datetime(2026, 3, 4, 9, 30),
                meeting_date=datetime(2026, 3, 3, 14, 0),
            )
            db.session.add(rec)
            db.session.commit()
            tag = Tag(name=f"Planning-{suffix}" if key == "ascii" else f"会議-{suffix}", user_id=user.id)
            db.session.add(tag)
            db.session.commit()
            db.session.add(RecordingTag(recording_id=rec.id, tag_id=tag.id, order=1))
            db.session.commit()
            ids[key] = rec.id
        user_id = user.id
    yield user_id, ids, suffix
    with app.app_context():
        for rid in ids.values():
            RecordingTag.query.filter_by(recording_id=rid).delete()
            Recording.query.filter_by(id=rid).delete()
        Tag.query.filter_by(user_id=user_id).delete()
        User.query.filter_by(id=user_id).delete()
        db.session.commit()


def _client(user_id):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
    return client


def _download(user_id, rid, kind):
    client = _client(user_id)
    if kind == "chat":
        with patch("src.utils.timezones.now_local", return_value=datetime(2026, 3, 5, 16, 7)):
            return client.post(f"/recording/{rid}/download/chat", json={"messages": _CHAT_MESSAGES})
    return client.get(f"/recording/{rid}/download/{kind}")


def _normalise(value, suffix):
    """Replace the per-run tag suffix so the pinned values do not depend on it."""
    if isinstance(value, str):
        return value.replace(suffix, "SUFFIX")
    if isinstance(value, list):
        return [_normalise(v, suffix) for v in value]
    if isinstance(value, dict):
        return {k: _normalise(v, suffix) for k, v in value.items()}
    return value


@pytest.mark.parametrize("kind", ["summary", "notes", "chat"])
@pytest.mark.parametrize("title_key", ["ascii", "japanese"])
def test_word_download_unchanged(recordings, kind, title_key):
    user_id, ids, suffix = recordings
    got = _normalise(_capture(_download(user_id, ids[title_key], kind)), suffix)
    assert got == EXPECTED[f"{kind}-{title_key}"]


def _style_fonts(style):
    """(ascii, eastAsia) from the first style in the base-style chain that names them."""
    while style is not None:
        rpr = style.element.rPr
        fonts = rpr.rFonts if rpr is not None else None
        if fonts is not None and fonts.get(qn("w:ascii")):
            return fonts.get(qn("w:ascii")), fonts.get(qn("w:eastAsia"))
        style = style.base_style
    return None, None


def _default_fonts(doc):
    fonts = doc.styles.element.find(qn("w:docDefaults")).find(qn("w:rPrDefault")).find(qn("w:rPr")).find(qn("w:rFonts"))
    return fonts.get(qn("w:ascii")), fonts.get(qn("w:eastAsia"))


def _all_paragraphs(doc):
    paragraphs = list(doc.paragraphs)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
    return paragraphs


def _effective_fonts(doc, paragraph, run):
    """The run's (ascii, eastAsia) fonts as Word resolves them: run, then style, then defaults."""
    rpr = run._element.rPr
    fonts = rpr.rFonts if rpr is not None else None
    ascii_font = fonts.get(qn("w:ascii")) if fonts is not None else None
    east_asia = fonts.get(qn("w:eastAsia")) if fonts is not None else None
    for style_fonts in (_style_fonts(run.style) if run.style.name != "Default Paragraph Font" else (None, None),
                        _style_fonts(paragraph.style), _default_fonts(doc)):
        ascii_font = ascii_font or style_fonts[0]
        east_asia = east_asia or style_fonts[1]
    return ascii_font, east_asia


@pytest.mark.parametrize("kind", ["summary", "notes", "chat"])
@pytest.mark.parametrize("title_key", ["ascii", "japanese"])
def test_every_text_run_resolves_to_arial(recordings, kind, title_key):
    user_id, ids, _ = recordings
    doc = Document(io.BytesIO(_download(user_id, ids[title_key], kind).data))
    checked = 0
    for paragraph in _all_paragraphs(doc):
        for run in paragraph.runs:
            if run.font.name == "Courier New":  # inline code and code blocks stay monospace
                continue
            assert _effective_fonts(doc, paragraph, run) == ("Arial", "Arial"), (paragraph.text, run.text)
            checked += 1
    assert checked > 5


@pytest.mark.parametrize("kind", ["summary", "notes", "chat"])
def test_styles_carry_no_theme_fonts(recordings, kind):
    user_id, ids, _ = recordings
    doc = Document(io.BytesIO(_download(user_id, ids["ascii"], kind).data))
    used = {p.style.name for p in _all_paragraphs(doc)}
    for name in used:
        rpr = doc.styles[name].element.rPr
        if rpr is None or rpr.rFonts is None:  # inherits its base style's fonts
            continue
        fonts = rpr.rFonts
        assert not [a for a in fonts.attrib if a.endswith("Theme") or a.endswith("theme")], name


@pytest.mark.parametrize("kind", ["summary", "notes", "chat"])
def test_no_empty_spacer_paragraphs(recordings, kind):
    user_id, ids, _ = recordings
    doc = Document(io.BytesIO(_download(user_id, ids["ascii"], kind).data))
    assert [p.style.name for p in _all_paragraphs(doc) if not p.text.strip()] == []


@pytest.mark.parametrize("kind", ["summary", "notes", "chat"])
def test_non_ascii_filename_logs_rfc2231_header(recordings, kind, caplog):
    user_id, ids, _ = recordings
    with caplog.at_level(logging.INFO):
        resp = _download(user_id, ids["japanese"], kind)
    header = resp.headers["Content-Disposition"]
    assert f"DEBUG CHINESE FILENAME (RFC2231): Original='{kind}-週次会議.docx', Header='{header}'" in caplog.text


@pytest.mark.parametrize("kind", ["summary", "notes", "chat"])
def test_rfc2231_failure_falls_back_to_recording_id(recordings, kind):
    user_id, ids, _ = recordings
    rid = ids["japanese"]
    targets = [m for m in (rec_module, document_module) if hasattr(m, "encode_rfc2231")]
    targets.append(email.utils)
    patches = [patch.object(m, "encode_rfc2231", side_effect=ValueError("boom")) for m in targets]
    for p in patches:
        p.start()
    try:
        resp = _download(user_id, rid, kind)
    finally:
        for p in patches:
            p.stop()
    assert resp.status_code == 200
    assert resp.headers["Content-Disposition"] == f'attachment; filename="download-{rid}.docx"'


# Re-recorded when the downloads moved to one font and style spacing.
EXPECTED = {'summary-ascii': {'status': 200,
                   'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                   'disposition': 'attachment; filename="summary-Weekly-Sync.docx"',
                   'paragraphs': [['Title',
                                   'Summary: Weekly Sync',
                                   [['Summary: Weekly Sync', None, None, None, None]]],
                                  ['Normal',
                                   'Uploaded: 2026-03-04 09:30',
                                   [['Uploaded: 2026-03-04 09:30', None, None, None, None]]],
                                  ['Normal',
                                   'Recording Date: 2026-03-03',
                                   [['Recording Date: 2026-03-03', None, None, None, None]]],
                                  ['Normal',
                                   'Participants: Ada, Lin',
                                   [['Participants: Ada, Lin', None, None, None, None]]],
                                  ['Normal',
                                   'Tags: Planning-SUFFIX',
                                   [['Tags: Planning-SUFFIX', None, None, None, None]]],
                                  ['Heading 2', 'Overview', [['Overview', None, None, None, None]]],
                                  ['Normal',
                                   'Plain line with bold, italic, both, gone and code.',
                                   [['Plain line with ', None, None, None, None],
                                    ['bold', None, None, True, None],
                                    [', ', None, None, None, None],
                                    ['italic', None, None, None, True],
                                    [', ', None, None, None, None],
                                    ['both', None, None, True, True],
                                    [', ', None, None, None, None],
                                    ['gone', None, None, None, None],
                                    [' and ', None, None, None, None],
                                    ['code', 'Courier New', None, None, None],
                                    ['.', None, None, None, None]]],
                                  ['Normal',
                                   '日本語の行に 太字 と コード と リンク (https://example.com/会議) があります。',
                                   [['日本語の行に ', None, None, None, None],
                                    ['太字', None, None, True, None],
                                    [' と ', None, None, None, None],
                                    ['コード', 'Courier New', 'Consolas', None, None],
                                    [' と ', None, None, None, None],
                                    ['リンク (https://example.com/会議)', None, None, None, None],
                                    [' があります。', None, None, None, None]]],
                                  ['List Bullet', 'first item', [['first item', None, None, None, None]]],
                                  ['List Bullet', '二番目の項目', [['二番目の項目', None, None, None, None]]],
                                  ['List Number', 'one', [['one', None, None, None, None]]],
                                  ['List Number', '二', [['二', None, None, None, None]]],
                                  ['Normal',
                                   '──────────────────────────────────────────────────',
                                   [['──────────────────────────────────────────────────',
                                     None,
                                     None,
                                     None,
                                     None]]],
                                  ['Normal',
                                   "print('x')",
                                   [["print('x')", 'Courier New', None, None, None]]]],
                   'tables': [[[[['Normal', 'Name', [['Name', None, None, True, None]]]],
                                [['Normal', '担当', [['担当', None, None, True, None]]]]],
                               [[['Normal', 'Ada', [['Ada', None, None, None, None]]]],
                                [['Normal', '予算', [['予算', None, None, None, None]]]]]]]},
 'summary-japanese': {'status': 200,
                      'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                      'disposition': 'attachment; '
                                     "filename*=utf-8''summary-%E9%80%B1%E6%AC%A1%E4%BC%9A%E8%AD%B0.docx",
                      'paragraphs': [['Title', 'Summary: 週次会議', [['Summary: 週次会議', None, None, None, None]]],
                                     ['Normal',
                                      'Uploaded: 2026-03-04 09:30',
                                      [['Uploaded: 2026-03-04 09:30', None, None, None, None]]],
                                     ['Normal',
                                      'Recording Date: 2026-03-03',
                                      [['Recording Date: 2026-03-03', None, None, None, None]]],
                                     ['Normal',
                                      'Participants: 佐藤, 鈴木',
                                      [['Participants: 佐藤, 鈴木', None, None, None, None]]],
                                     ['Normal',
                                      'Tags: 会議-SUFFIX',
                                      [['Tags: 会議-SUFFIX', None, None, None, None]]],
                                     ['Heading 2', 'Overview', [['Overview', None, None, None, None]]],
                                     ['Normal',
                                      'Plain line with bold, italic, both, gone and code.',
                                      [['Plain line with ', None, None, None, None],
                                       ['bold', None, None, True, None],
                                       [', ', None, None, None, None],
                                       ['italic', None, None, None, True],
                                       [', ', None, None, None, None],
                                       ['both', None, None, True, True],
                                       [', ', None, None, None, None],
                                       ['gone', None, None, None, None],
                                       [' and ', None, None, None, None],
                                       ['code', 'Courier New', None, None, None],
                                       ['.', None, None, None, None]]],
                                     ['Normal',
                                      '日本語の行に 太字 と コード と リンク (https://example.com/会議) があります。',
                                      [['日本語の行に ', None, None, None, None],
                                       ['太字', None, None, True, None],
                                       [' と ', None, None, None, None],
                                       ['コード', 'Courier New', 'Consolas', None, None],
                                       [' と ', None, None, None, None],
                                       ['リンク (https://example.com/会議)', None, None, None, None],
                                       [' があります。', None, None, None, None]]],
                                     ['List Bullet', 'first item', [['first item', None, None, None, None]]],
                                     ['List Bullet', '二番目の項目', [['二番目の項目', None, None, None, None]]],
                                     ['List Number', 'one', [['one', None, None, None, None]]],
                                     ['List Number', '二', [['二', None, None, None, None]]],
                                     ['Normal',
                                      '──────────────────────────────────────────────────',
                                      [['──────────────────────────────────────────────────',
                                        None,
                                        None,
                                        None,
                                        None]]],
                                     ['Normal',
                                      "print('x')",
                                      [["print('x')", 'Courier New', None, None, None]]]],
                      'tables': [[[[['Normal', 'Name', [['Name', None, None, True, None]]]],
                                   [['Normal', '担当', [['担当', None, None, True, None]]]]],
                                  [[['Normal', 'Ada', [['Ada', None, None, None, None]]]],
                                   [['Normal', '予算', [['予算', None, None, None, None]]]]]]]},
 'notes-ascii': {'status': 200,
                 'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                 'disposition': 'attachment; filename="notes-Weekly-Sync.docx"',
                 'paragraphs': [['Title',
                                 'Notes: Weekly Sync',
                                 [['Notes: Weekly Sync', None, None, None, None]]],
                                ['Normal',
                                 'Uploaded: 2026-03-04 09:30',
                                 [['Uploaded: 2026-03-04 09:30', None, None, None, None]]],
                                ['Normal',
                                 'Recording Date: 2026-03-03',
                                 [['Recording Date: 2026-03-03', None, None, None, None]]],
                                ['Normal',
                                 'Participants: Ada, Lin',
                                 [['Participants: Ada, Lin', None, None, None, None]]],
                                ['Normal',
                                 'Tags: Planning-SUFFIX',
                                 [['Tags: Planning-SUFFIX', None, None, None, None]]],
                                ['Heading 2', 'Overview', [['Overview', None, None, None, None]]],
                                ['Normal',
                                 'Plain line with bold, italic, both, gone and code.',
                                 [['Plain line with ', None, None, None, None],
                                  ['bold', None, None, True, None],
                                  [', ', None, None, None, None],
                                  ['italic', None, None, None, True],
                                  [', ', None, None, None, None],
                                  ['both', None, None, True, True],
                                  [', ', None, None, None, None],
                                  ['gone', None, None, None, None],
                                  [' and ', None, None, None, None],
                                  ['code', 'Courier New', None, None, None],
                                  ['.', None, None, None, None]]],
                                ['Normal',
                                 '日本語の行に 太字 と コード と リンク (https://example.com/会議) があります。',
                                 [['日本語の行に ', None, None, None, None],
                                  ['太字', None, None, True, None],
                                  [' と ', None, None, None, None],
                                  ['コード', 'Courier New', 'Consolas', None, None],
                                  [' と ', None, None, None, None],
                                  ['リンク (https://example.com/会議)', None, None, None, None],
                                  [' があります。', None, None, None, None]]],
                                ['List Bullet', 'first item', [['first item', None, None, None, None]]],
                                ['List Bullet', '二番目の項目', [['二番目の項目', None, None, None, None]]],
                                ['List Number', 'one', [['one', None, None, None, None]]],
                                ['List Number', '二', [['二', None, None, None, None]]],
                                ['Normal',
                                 '──────────────────────────────────────────────────',
                                 [['──────────────────────────────────────────────────',
                                   None,
                                   None,
                                   None,
                                   None]]],
                                ['Normal', "print('x')", [["print('x')", 'Courier New', None, None, None]]]],
                 'tables': [[[[['Normal', 'Name', [['Name', None, None, True, None]]]],
                              [['Normal', '担当', [['担当', None, None, True, None]]]]],
                             [[['Normal', 'Ada', [['Ada', None, None, None, None]]]],
                              [['Normal', '予算', [['予算', None, None, None, None]]]]]]]},
 'notes-japanese': {'status': 200,
                    'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    'disposition': 'attachment; '
                                   "filename*=utf-8''notes-%E9%80%B1%E6%AC%A1%E4%BC%9A%E8%AD%B0.docx",
                    'paragraphs': [['Title', 'Notes: 週次会議', [['Notes: 週次会議', None, None, None, None]]],
                                   ['Normal',
                                    'Uploaded: 2026-03-04 09:30',
                                    [['Uploaded: 2026-03-04 09:30', None, None, None, None]]],
                                   ['Normal',
                                    'Recording Date: 2026-03-03',
                                    [['Recording Date: 2026-03-03', None, None, None, None]]],
                                   ['Normal',
                                    'Participants: 佐藤, 鈴木',
                                    [['Participants: 佐藤, 鈴木', None, None, None, None]]],
                                   ['Normal',
                                    'Tags: 会議-SUFFIX',
                                    [['Tags: 会議-SUFFIX', None, None, None, None]]],
                                   ['Heading 2', 'Overview', [['Overview', None, None, None, None]]],
                                   ['Normal',
                                    'Plain line with bold, italic, both, gone and code.',
                                    [['Plain line with ', None, None, None, None],
                                     ['bold', None, None, True, None],
                                     [', ', None, None, None, None],
                                     ['italic', None, None, None, True],
                                     [', ', None, None, None, None],
                                     ['both', None, None, True, True],
                                     [', ', None, None, None, None],
                                     ['gone', None, None, None, None],
                                     [' and ', None, None, None, None],
                                     ['code', 'Courier New', None, None, None],
                                     ['.', None, None, None, None]]],
                                   ['Normal',
                                    '日本語の行に 太字 と コード と リンク (https://example.com/会議) があります。',
                                    [['日本語の行に ', None, None, None, None],
                                     ['太字', None, None, True, None],
                                     [' と ', None, None, None, None],
                                     ['コード', 'Courier New', 'Consolas', None, None],
                                     [' と ', None, None, None, None],
                                     ['リンク (https://example.com/会議)', None, None, None, None],
                                     [' があります。', None, None, None, None]]],
                                   ['List Bullet', 'first item', [['first item', None, None, None, None]]],
                                   ['List Bullet', '二番目の項目', [['二番目の項目', None, None, None, None]]],
                                   ['List Number', 'one', [['one', None, None, None, None]]],
                                   ['List Number', '二', [['二', None, None, None, None]]],
                                   ['Normal',
                                    '──────────────────────────────────────────────────',
                                    [['──────────────────────────────────────────────────',
                                      None,
                                      None,
                                      None,
                                      None]]],
                                   ['Normal',
                                    "print('x')",
                                    [["print('x')", 'Courier New', None, None, None]]]],
                    'tables': [[[[['Normal', 'Name', [['Name', None, None, True, None]]]],
                                 [['Normal', '担当', [['担当', None, None, True, None]]]]],
                                [[['Normal', 'Ada', [['Ada', None, None, None, None]]]],
                                 [['Normal', '予算', [['予算', None, None, None, None]]]]]]]},
 'chat-ascii': {'status': 200,
                'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'disposition': 'attachment; filename="chat-Weekly-Sync.docx"',
                'paragraphs': [['Title',
                                'Chat Conversation: Weekly Sync',
                                [['Chat Conversation: Weekly Sync', None, None, None, None]]],
                               ['Normal',
                                'Recording Date: 2026-03-03 14:00',
                                [['Recording Date: 2026-03-03 14:00', None, None, None, None]]],
                               ['Normal',
                                'Chat Export Date: 2026-03-05 16:07',
                                [['Chat Export Date: 2026-03-05 16:07', None, None, None, None]]],
                               ['Conversation Role', 'You:', [['You:', None, None, True, None]]],
                               ['Normal',
                                'What was discussed?',
                                [['What was discussed?', None, None, None, None]]],
                               ['Conversation Role', 'Assistant:', [['Assistant:', None, None, True, None]]],
                               ['Normal',
                                '[Model Reasoning]\nhidden-reasoning',
                                [['[Model Reasoning]\n', None, None, None, True],
                                 ['hidden-reasoning', None, None, None, True]]],
                               ['Normal',
                                'The roadmap.',
                                [['The ', None, None, None, None],
                                 ['roadmap', None, None, True, None],
                                 ['.', None, None, None, None]]],
                               ['Conversation Role', 'System:', [['System:', None, None, True, None]]],
                               ['Normal', 'A note.', [['A note.', None, None, None, None]]],
                               ['Conversation Role', 'You:', [['You:', None, None, True, None]]],
                               ['Normal', '予算は？', [['予算は？', None, None, None, None]]],
                               ['Conversation Role', 'Assistant:', [['Assistant:', None, None, True, None]]],
                               ['Normal',
                                '[Model Reasoning]\n考え中',
                                [['[Model Reasoning]\n', None, None, None, True],
                                 ['考え中', None, None, None, True]]],
                               ['Normal',
                                '予算は 増額 です。',
                                [['予算は ', None, None, None, None],
                                 ['増額', None, None, True, None],
                                 [' です。', None, None, None, None]]],
                               ['Conversation Role', 'You:', [['You:', None, None, True, None]]]],
                'tables': []},
 'chat-japanese': {'status': 200,
                   'content_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                   'disposition': 'attachment; '
                                  "filename*=utf-8''chat-%E9%80%B1%E6%AC%A1%E4%BC%9A%E8%AD%B0.docx",
                   'paragraphs': [['Title',
                                   'Chat Conversation: 週次会議',
                                   [['Chat Conversation: 週次会議', None, None, None, None]]],
                                  ['Normal',
                                   'Recording Date: 2026-03-03 14:00',
                                   [['Recording Date: 2026-03-03 14:00', None, None, None, None]]],
                                  ['Normal',
                                   'Chat Export Date: 2026-03-05 16:07',
                                   [['Chat Export Date: 2026-03-05 16:07', None, None, None, None]]],
                                  ['Conversation Role', 'You:', [['You:', None, None, True, None]]],
                                  ['Normal',
                                   'What was discussed?',
                                   [['What was discussed?', None, None, None, None]]],
                                  ['Conversation Role',
                                   'Assistant:',
                                   [['Assistant:', None, None, True, None]]],
                                  ['Normal',
                                   '[Model Reasoning]\nhidden-reasoning',
                                   [['[Model Reasoning]\n', None, None, None, True],
                                    ['hidden-reasoning', None, None, None, True]]],
                                  ['Normal',
                                   'The roadmap.',
                                   [['The ', None, None, None, None],
                                    ['roadmap', None, None, True, None],
                                    ['.', None, None, None, None]]],
                                  ['Conversation Role', 'System:', [['System:', None, None, True, None]]],
                                  ['Normal', 'A note.', [['A note.', None, None, None, None]]],
                                  ['Conversation Role', 'You:', [['You:', None, None, True, None]]],
                                  ['Normal', '予算は？', [['予算は？', None, None, None, None]]],
                                  ['Conversation Role',
                                   'Assistant:',
                                   [['Assistant:', None, None, True, None]]],
                                  ['Normal',
                                   '[Model Reasoning]\n考え中',
                                   [['[Model Reasoning]\n', None, None, None, True],
                                    ['考え中', None, None, None, True]]],
                                  ['Normal',
                                   '予算は 増額 です。',
                                   [['予算は ', None, None, None, None],
                                    ['増額', None, None, True, None],
                                    [' です。', None, None, None, None]]],
                                  ['Conversation Role', 'You:', [['You:', None, None, True, None]]]],
                   'tables': []}}
