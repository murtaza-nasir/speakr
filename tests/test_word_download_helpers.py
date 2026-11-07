"""Unit tests for the shared Word helpers in src/services/document.py (#554, #555)."""
import os
import sys
from email.utils import encode_rfc2231
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

from src.app import app
from src.services.document import (
    add_document_header, append_conversation_turn, docx_attachment_response,
    new_document, process_markdown_to_docx,
)


def _east_asia(run):
    rpr = run._element.rPr
    if rpr is None or rpr.rFonts is None:
        return None
    return rpr.rFonts.get(qn('w:eastAsia'))


def _style_fonts(style):
    fonts = style.element.rPr.rFonts
    return {name: fonts.get(qn(f'w:{name}')) for name in ('ascii', 'hAnsi', 'cs', 'eastAsia')}


def test_new_document_styles_use_arial_without_theme_fonts():
    doc = new_document()
    for name in ('Normal', 'Title', 'Heading 1', 'Heading 2', 'Heading 3', 'Heading 4',
                 'Heading 5', 'Heading 6', 'List Bullet', 'List Number', 'Table Grid'):
        style = doc.styles[name]
        assert _style_fonts(style) == {'ascii': 'Arial', 'hAnsi': 'Arial', 'cs': 'Arial', 'eastAsia': 'Arial'}, name
        assert not [a for a in style.element.rPr.rFonts.attrib if a.lower().endswith('theme')], name
    defaults = doc.styles.element.find(qn('w:docDefaults')).find(qn('w:rPrDefault')).find(qn('w:rPr')).find(qn('w:rFonts'))
    assert defaults.get(qn('w:ascii')) == 'Arial' and defaults.get(qn('w:eastAsia')) == 'Arial'
    assert not [a for a in defaults.attrib if a.lower().endswith('theme')]


def test_new_document_spaces_normal_paragraphs():
    normal = new_document().styles['Normal'].paragraph_format
    assert normal.space_after == Pt(6)
    assert normal.line_spacing == 1.15


def test_text_runs_carry_no_font_of_their_own():
    doc = new_document()
    add_document_header(doc, 'Summary: 预算 \u2014 plan', ['Tags: 会议'])
    process_markdown_to_docx(doc, '预算 **会议** and *more*')
    runs = [r for p in doc.paragraphs for r in p.runs]
    assert runs and all(r._element.rPr is None or r._element.rPr.rFonts is None for r in runs)


def test_document_header_title_and_gap():
    doc = new_document()
    add_document_header(doc, 'Notes: Weekly', ['Uploaded: 2026-03-04', 'Tags: Planning'])
    assert [(p.style.name, p.text) for p in doc.paragraphs] == [
        ('Title', 'Notes: Weekly'), ('Normal', 'Uploaded: 2026-03-04'), ('Normal', 'Tags: Planning')]
    assert doc.paragraphs[1].paragraph_format.space_after is None
    assert doc.paragraphs[2].paragraph_format.space_after == Pt(12)


def test_blank_lines_add_no_paragraphs():
    doc = new_document()
    process_markdown_to_docx(doc, 'First\n\n\nSecond\n\n- item\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\nAfter')
    assert [p.text for p in doc.paragraphs] == ['First', 'Second', 'item', 'After']
    cells = [p.text for row in doc.tables[0].rows for c in row.cells for p in c.paragraphs]
    assert cells == ['A', 'B', '1', '2']
    assert doc.paragraphs[-1].paragraph_format.space_before == Pt(8)


def test_inline_code_keeps_consolas():
    doc = new_document()
    process_markdown_to_docx(doc, '`预算`')
    run = [p for p in doc.paragraphs if p.text == '预算'][0].runs[0]
    assert run.font.name == 'Courier New'
    assert _east_asia(run) == 'Consolas'


def test_code_block_keeps_courier_new():
    doc = new_document()
    process_markdown_to_docx(doc, '```\nprint(1)\n```')
    run = doc.paragraphs[0].runs[0]
    assert run.font.name == 'Courier New'
    assert run.font.size == Pt(10)


def test_ascii_filename_disposition():
    with app.test_request_context():
        resp = docx_attachment_response(Document(), 'summary-weekly.docx', 'download-4.docx')
    assert resp.status_code == 200
    assert resp.mimetype == 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    assert resp.headers['Content-Disposition'] == 'attachment; filename="summary-weekly.docx"'


def test_non_ascii_filename_uses_rfc2231():
    with app.test_request_context():
        resp = docx_attachment_response(Document(), 'summary-预算.docx', 'download-4.docx')
    expected = encode_rfc2231('summary-预算.docx', charset='utf-8')
    assert resp.headers['Content-Disposition'] == f'attachment; filename*={expected}'


def test_rfc2231_failure_uses_fallback():
    with app.test_request_context(), \
         patch('src.services.document.encode_rfc2231', side_effect=ValueError('boom')):
        resp = docx_attachment_response(Document(), 'summary-预算.docx', 'download-4.docx')
    assert resp.headers['Content-Disposition'] == 'attachment; filename="download-4.docx"'


def _turn(role, content, thinking=None, doc=None):
    doc = doc if doc is not None else new_document()
    append_conversation_turn(doc, role, content, thinking)
    return [p.text for p in doc.paragraphs]


def test_conversation_turn_role_labels():
    assert _turn('user', 'Hi') == ['You:', 'Hi']
    assert _turn('assistant', 'Hello') == ['Assistant:', 'Hello']
    assert _turn('system', 'Note') == ['System:', 'Note']


def test_conversation_turn_reasoning_only_for_assistant():
    assert _turn('assistant', 'Answer', 'why') == ['Assistant:', '[Model Reasoning]\nwhy', 'Answer']
    assert _turn('user', 'Question', 'why') == ['You:', 'Question']
    assert _turn('assistant', 'Answer', '') == ['Assistant:', 'Answer']


def test_role_label_style_spaces_the_turns():
    doc = new_document()
    append_conversation_turn(doc, 'user', 'Hi')
    label = doc.paragraphs[0]
    assert label.style.name == 'Conversation Role'
    assert label.runs[0].bold is True
    fmt = label.style.paragraph_format
    assert fmt.space_before == Pt(12) and fmt.keep_with_next is True
    assert label.style.font.bold is True


def test_role_label_style_added_to_a_plain_document():
    assert _turn('user', 'Hi', doc=Document()) == ['You:', 'Hi']
