"""Unit tests for the shared Word helpers in src/services/document.py (#554, #555)."""
import os
import sys
from email.utils import encode_rfc2231
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document
from docx.oxml.ns import qn

from src.app import app
from src.services.document import (
    add_unicode_heading, add_unicode_paragraph,
    docx_attachment_response, process_markdown_to_docx,
)


def _east_asia(run):
    rpr = run._element.rPr
    if rpr is None or rpr.rFonts is None:
        return None
    return rpr.rFonts.get(qn('w:eastAsia'))


def test_unicode_paragraph_sets_east_asia_font():
    run = add_unicode_paragraph(Document(), '预算').runs[0]
    assert run.font.name == 'Arial'
    assert _east_asia(run) == 'Arial'


def test_ascii_paragraph_leaves_font_unset():
    run = add_unicode_paragraph(Document(), 'Tags: Planning').runs[0]
    assert run.font.name is None
    assert _east_asia(run) is None


def test_unicode_heading_sets_east_asia_font():
    heading = add_unicode_heading(Document(), 'Summary: 预算', 0)
    assert heading.style.name == 'Title'
    assert [(r.font.name, _east_asia(r)) for r in heading.runs] == [('Arial', 'Arial')]


def test_markdown_paragraph_uses_shared_font():
    doc = Document()
    process_markdown_to_docx(doc, '预算')
    run = [p for p in doc.paragraphs if p.text == '预算'][0].runs[0]
    assert _east_asia(run) == 'Arial'


def test_inline_code_keeps_consolas():
    doc = Document()
    process_markdown_to_docx(doc, '`预算`')
    run = [p for p in doc.paragraphs if p.text == '预算'][0].runs[0]
    assert run.font.name == 'Courier New'
    assert _east_asia(run) == 'Consolas'


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
