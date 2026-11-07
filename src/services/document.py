"""
Document processing and conversion services.
"""

import re
from email.utils import encode_rfc2231
from io import BytesIO
from urllib.parse import quote, urlsplit

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from docx.text.run import Run
from flask import current_app, request, send_file

DOCX_MIMETYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'

# One font for all text in the Word downloads. Arial covers Latin, Cyrillic
# and, through Word's font fallback, CJK text; it is also the east-Asia font.
DOCUMENT_FONT = 'Arial'
CODE_FONT = 'Courier New'
CODE_EAST_ASIA_FONT = 'Consolas'

# The styles the downloads and the markdown converter use
_DOCUMENT_STYLES = (
    'Normal', 'Title',
    'Heading 1', 'Heading 2', 'Heading 3', 'Heading 4', 'Heading 5', 'Heading 6',
    'List Paragraph', 'List Bullet', 'List Number', 'Table Grid',
)

ROLE_LABEL_STYLE = 'Conversation Role'
HYPERLINK_STYLE = 'Hyperlink'
HYPERLINK_COLOR = RGBColor(0x05, 0x63, 0xC1)  # Word's own hyperlink blue

# Characters a link target keeps as they are; anything else (spaces,
# non-ASCII letters) is percent-encoded
_URL_SAFE = ":/?#[]@!$&'()*+,;=%~-._"

_FONT_ATTRIBUTES = ('w:ascii', 'w:hAnsi', 'w:cs', 'w:eastAsia')
_THEME_FONT_ATTRIBUTES = ('w:asciiTheme', 'w:hAnsiTheme', 'w:cstheme', 'w:eastAsiaTheme')


def _set_fonts(rpr, name):
    """Set every script's font in rpr to name and drop the theme fonts, which would win."""
    fonts = rpr.find(qn('w:rFonts'))
    if fonts is None:
        fonts = OxmlElement('w:rFonts')
        rpr.insert(0, fonts)
    for attribute in _THEME_FONT_ATTRIBUTES:
        fonts.attrib.pop(qn(attribute), None)
    for attribute in _FONT_ATTRIBUTES:
        fonts.set(qn(attribute), name)


def new_document():
    """A Document whose text is Arial throughout, with spacing set on the styles.

    The fonts are set on the document defaults and on each style the downloads
    use, so no run needs a font of its own. Paragraphs are spaced by the Normal
    style, so content needs no empty paragraphs between blocks.
    """
    doc = Document()
    styles_element = doc.styles.element
    defaults = styles_element.find(qn('w:docDefaults'))
    if defaults is not None:
        rpr = defaults.find(qn('w:rPrDefault') + '/' + qn('w:rPr'))
        if rpr is not None:
            _set_fonts(rpr, DOCUMENT_FONT)

    for name in _DOCUMENT_STYLES:
        try:
            style = doc.styles[name]
        except KeyError:
            continue
        _set_fonts(style.element.get_or_add_rPr(), DOCUMENT_FONT)

    normal = doc.styles['Normal'].paragraph_format
    normal.space_before = Pt(0)
    normal.space_after = Pt(6)
    normal.line_spacing = 1.15

    _role_label_style(doc)
    _hyperlink_style(doc.styles)
    return doc


def _hyperlink_style(styles):
    """The Hyperlink character style (blue, underlined, Arial), added when the template lacks it."""
    try:
        return styles[HYPERLINK_STYLE]
    except KeyError:
        pass
    style = styles.add_style(HYPERLINK_STYLE, WD_STYLE_TYPE.CHARACTER)
    style.font.color.rgb = HYPERLINK_COLOR
    style.font.underline = True
    _set_fonts(style.element.get_or_add_rPr(), DOCUMENT_FONT)
    return style


def link_base_url():
    """The base for absolute links in a download: APP_BASE_URL when set, else this request's root."""
    from src.services.email import get_app_base_url
    return get_app_base_url() or request.url_root.rstrip('/')


def resolve_link_url(url, base_url):
    """The absolute http(s) target for a markdown link, or None when it must stay plain text.

    A path starting with "/" is joined to base_url, query string included.
    An http or https URL is kept. Every other target, including other schemes
    (javascript:, data:, file:), protocol-relative "//host" paths and paths
    relative to the current page, is not linked.
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in url):
        return None
    if url.startswith('/'):
        if url.startswith('//') or url.startswith('/\\') or not base_url:
            return None
        url = base_url.rstrip('/') + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        return None
    return quote(url, safe=_URL_SAFE)


def add_hyperlink(paragraph, text, url):
    """Append a Word hyperlink to url showing text, in the Hyperlink character style."""
    part = paragraph.part
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), part.relate_to(url, RT.HYPERLINK, is_external=True))
    run_element = OxmlElement('w:r')
    hyperlink.append(run_element)
    paragraph._p.append(hyperlink)
    run = Run(run_element, paragraph)
    run.text = text
    run.style = _hyperlink_style(part.styles)
    return hyperlink


def add_link(paragraph, text, url, base_url):
    """A hyperlink when url resolves to a safe absolute target, otherwise text alone."""
    target = resolve_link_url(url, base_url)
    if target is None:
        return paragraph.add_run(text)
    return add_hyperlink(paragraph, text, target)


def append_sources(doc, sources, base_url):
    """A "Sources" label, then one numbered line per (number, title, url), each title a link."""
    if not sources:
        return
    label = doc.add_paragraph()
    label.add_run('Sources').bold = True
    label.paragraph_format.space_before = Pt(6)
    label.paragraph_format.space_after = Pt(2)
    label.paragraph_format.keep_with_next = True
    for number, title, url in sources:
        line = doc.add_paragraph()
        line.paragraph_format.left_indent = Pt(12)
        line.paragraph_format.space_after = Pt(2)
        line.add_run(f'{number}. ')
        add_link(line, title, url, base_url)


def _role_label_style(doc):
    """The paragraph style of the "You:" and "Assistant:" labels, added on first use."""
    try:
        return doc.styles[ROLE_LABEL_STYLE]
    except KeyError:
        pass
    role = doc.styles.add_style(ROLE_LABEL_STYLE, WD_STYLE_TYPE.PARAGRAPH)
    role.base_style = doc.styles['Normal']
    role.font.bold = True
    role.paragraph_format.space_before = Pt(12)
    role.paragraph_format.space_after = Pt(2)
    role.paragraph_format.keep_with_next = True
    return role


def add_document_header(doc, title, lines):
    """The title, then one paragraph per metadata line, with a gap below the last."""
    doc.add_heading(title, 0)
    paragraph = None
    for line in lines:
        paragraph = doc.add_paragraph(line)
    if paragraph is not None:
        paragraph.paragraph_format.space_after = Pt(12)


def docx_attachment_response(doc, filename, fallback_filename):
    """Return doc as a Word download named filename.

    A non-ASCII filename uses the RFC 2231 form; fallback_filename is used if
    that encoding fails.
    """
    doc_stream = BytesIO()
    doc.save(doc_stream)
    doc_stream.seek(0)

    # as_attachment=True needs a download name, which a BytesIO lacks (500);
    # the name goes in Content-Disposition instead.
    response = send_file(doc_stream, as_attachment=False, mimetype=DOCX_MIMETYPE)
    try:
        filename.encode('ascii')
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    except UnicodeEncodeError:
        try:
            encoded_value = encode_rfc2231(filename, charset='utf-8')
            header_value = f'attachment; filename*={encoded_value}'
            current_app.logger.info(f"DEBUG CHINESE FILENAME (RFC2231): Original='{filename}', Header='{header_value}'")
            response.headers['Content-Disposition'] = header_value
        except Exception as e:
            current_app.logger.error(f"RFC2231 encoding failed: {e}, using fallback")
            response.headers['Content-Disposition'] = f'attachment; filename="{fallback_filename}"'
    return response


def append_conversation_turn(doc, role, content, thinking=None, base_url=None, sources=None):
    """Add one chat turn: a bold role label, any assistant reasoning, the markdown content,
    then the Sources list for an assistant turn that has one.

    The label uses the Conversation Role style, whose space before separates
    the turns, so no blank paragraph is needed. Links are made absolute against
    base_url (see resolve_link_url).
    """
    if role == 'user':
        label = 'You'
    elif role == 'assistant':
        label = 'Assistant'
    else:
        label = role.title()
    doc.add_paragraph(style=_role_label_style(doc)).add_run(f'{label}:').bold = True

    if thinking and role == 'assistant':
        p = doc.add_paragraph()
        p.add_run('[Model Reasoning]\n').italic = True
        p.add_run(thinking).italic = True

    process_markdown_to_docx(doc, content, base_url)
    if role == 'assistant':
        append_sources(doc, sources, base_url)


def process_markdown_to_docx(doc, content, base_url=None):
    """Convert markdown content to properly formatted Word document elements.

    Supports:
    - Tables (markdown pipe tables)
    - Headings (# ## ###)
    - Bold text (**text**)
    - Italic text (*text* or _text_)
    - Bold italic (***text***)
    - Inline code (`code`)
    - Code blocks (```code```)
    - Strikethrough (~~text~~)
    - Links ([text](url)): a Word hyperlink showing the text. A target starting
      with "/" is joined to base_url; http(s) targets are kept; any other
      target, or a relative one without base_url, is written as plain text.
    - Bullet lists (- or *)
    - Numbered lists (1. 2. 3.)
    - Horizontal rules (--- or ***)
    """
    from docx.enum.text import WD_PARAGRAPH_ALIGNMENT

    def add_formatted_run(paragraph, text):
        """Add a run with inline formatting to a paragraph."""
        if not text:
            return

        # Pattern for all inline formatting
        # Order matters: check triple asterisk before double/single
        patterns = [
            (r'\*\*\*(.*?)\*\*\*', lambda p, t: (lambda r: (setattr(r, 'bold', True), setattr(r, 'italic', True)))(p.add_run(t))),  # Bold italic
            (r'\*\*(.*?)\*\*', lambda p, t: (lambda r: setattr(r, 'bold', True))(p.add_run(t))),  # Bold
            (r'(?<!\*)\*(?!\*)(.*?)\*(?!\*)', lambda p, t: (lambda r: setattr(r, 'italic', True))(p.add_run(t))),  # Italic with *
            (r'\b_(.*?)_\b', lambda p, t: (lambda r: setattr(r, 'italic', True))(p.add_run(t))),  # Italic with _
            (r'~~(.*?)~~', lambda p, t: (lambda r: setattr(r, 'strike', True))(p.add_run(t))),  # Strikethrough
            (r'`([^`]+)`', lambda p, t: add_code_run(p, t)),  # Inline code
            (r'\[([^\]]+)\]\(([^)]+)\)', lambda p, t, u: add_link_run(p, t, u)),  # Links
        ]

        def add_code_run(para, text):
            """Add inline code with monospace font and background."""
            run = para.add_run(text)
            run.font.name = CODE_FONT
            run.font.size = Pt(10)
            run.font.color.rgb = RGBColor(220, 20, 60)  # Crimson color for code
            # Check if we need Unicode support for code
            try:
                text.encode('ascii')
            except UnicodeEncodeError:
                # Use Consolas as fallback for better Unicode support in monospace
                r = run._element
                r.rPr.rFonts.set(qn('w:eastAsia'), CODE_EAST_ASIA_FONT)
            return run

        def add_link_run(para, text, url):
            return add_link(para, text, url, base_url)

        # Process the text with all patterns
        remaining_text = text
        while remaining_text:
            earliest_match = None
            earliest_pos = len(remaining_text)
            matched_pattern = None

            # Find the earliest matching pattern
            for pattern, handler in patterns:
                match = re.search(pattern, remaining_text)
                if match and match.start() < earliest_pos:
                    earliest_match = match
                    earliest_pos = match.start()
                    matched_pattern = handler

            if earliest_match:
                # Add text before the match
                if earliest_pos > 0:
                    paragraph.add_run(remaining_text[:earliest_pos])

                # Apply formatting for the matched text
                if '[' in earliest_match.group(0) and '](' in earliest_match.group(0):
                    # Special handling for links (two groups)
                    matched_pattern(paragraph, earliest_match.group(1), earliest_match.group(2))
                else:
                    matched_pattern(paragraph, earliest_match.group(1))

                # Continue with remaining text
                remaining_text = remaining_text[earliest_match.end():]
            else:
                # No more patterns, add the rest as plain text
                paragraph.add_run(remaining_text)
                break

    def parse_table(lines, start_idx):
        """Parse a markdown table starting at the given index."""
        if start_idx >= len(lines):
            return None, start_idx

        # Check if this looks like a table
        if '|' not in lines[start_idx]:
            return None, start_idx

        table_data = []
        idx = start_idx

        while idx < len(lines) and '|' in lines[idx]:
            # Skip separator lines
            if re.match(r'^[\s\|\-:]+$', lines[idx]):
                idx += 1
                continue

            # Parse cells
            cells = [cell.strip() for cell in lines[idx].split('|')]
            # Remove empty cells at start and end
            if cells and not cells[0]:
                cells = cells[1:]
            if cells and not cells[-1]:
                cells = cells[:-1]

            if cells:
                table_data.append(cells)
            idx += 1

        if table_data:
            return table_data, idx
        return None, start_idx

    # Split content into lines
    lines = content.split('\n')
    tables = []  # the paragraph after each table gets space above it at the end
    i = 0
    in_code_block = False
    code_block_content = []

    while i < len(lines):
        line = lines[i]

        # Handle code blocks
        if line.strip().startswith('```'):
            if not in_code_block:
                in_code_block = True
                code_block_content = []
            else:
                # End of code block - add it as preformatted text
                in_code_block = False
                if code_block_content:
                    p = doc.add_paragraph()
                    p.style = 'Normal'
                    code_text = '\n'.join(code_block_content)
                    run = p.add_run(code_text)
                    run.font.name = CODE_FONT
                    run.font.size = Pt(10)
                    run.font.color.rgb = RGBColor(64, 64, 64)
                    # Check if we need Unicode support for code blocks
                    try:
                        code_text.encode('ascii')
                    except UnicodeEncodeError:
                        r = run._element
                        r.rPr.rFonts.set(qn('w:eastAsia'), CODE_EAST_ASIA_FONT)
            i += 1
            continue

        if in_code_block:
            code_block_content.append(line)
            i += 1
            continue

        # Check for table
        table_data, end_idx = parse_table(lines, i)
        if table_data:
            # Create Word table
            table = doc.add_table(rows=len(table_data), cols=len(table_data[0]))
            table.style = 'Table Grid'

            # Populate table
            for row_idx, row_data in enumerate(table_data):
                for col_idx, cell_text in enumerate(row_data):
                    if col_idx < len(table.rows[row_idx].cells):
                        cell = table.rows[row_idx].cells[col_idx]
                        # A new cell holds one empty paragraph; write into it
                        p = cell.paragraphs[0]
                        add_formatted_run(p, cell_text)
                        # Make header row bold
                        if row_idx == 0:
                            for run in p.runs:
                                run.bold = True

            tables.append(table)
            i = end_idx
            continue

        line = line.rstrip()

        # Blank lines only separate blocks; the paragraph styles space them
        if not line:
            i += 1
            continue

        # Horizontal rule
        if re.match(r'^(\*{3,}|-{3,}|_{3,})$', line.strip()):
            p = doc.add_paragraph('─' * 50)
            p.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
            i += 1
            continue

        # Headings
        if line.startswith('# '):
            doc.add_heading(line[2:], 1)
        elif line.startswith('## '):
            doc.add_heading(line[3:], 2)
        elif line.startswith('### '):
            doc.add_heading(line[4:], 3)
        elif line.startswith('#### '):
            doc.add_heading(line[5:], 4)
        # Bullet points
        elif line.lstrip().startswith('- ') or line.lstrip().startswith('* '):
            # Get the indentation level
            indent = len(line) - len(line.lstrip())
            bullet_text = line.lstrip()[2:]
            p = doc.add_paragraph(style='List Bullet')
            # Add indentation if nested
            if indent > 0:
                p.paragraph_format.left_indent = Pt(indent * 10)
            add_formatted_run(p, bullet_text)
        # Numbered lists
        elif re.match(r'^\s*\d+\.', line):
            match = re.match(r'^(\s*)(\d+)\.\s*(.*)', line)
            if match:
                indent = len(match.group(1))
                list_text = match.group(3)
                p = doc.add_paragraph(style='List Number')
                if indent > 0:
                    p.paragraph_format.left_indent = Pt(indent * 10)
                add_formatted_run(p, list_text)
        # Blockquote
        elif line.startswith('> '):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(30)
            add_formatted_run(p, line[2:])
            # Add a gray color to indicate quote
            for run in p.runs:
                run.font.color.rgb = RGBColor(100, 100, 100)
        else:
            # Regular paragraph
            p = doc.add_paragraph()
            add_formatted_run(p, line)

        i += 1

    # A paragraph that follows a table gets space above it, in place of the
    # empty paragraph the converter used to add there
    for table in tables:
        following = table._tbl.getnext()
        if following is not None and following.tag == qn('w:p'):
            spacing = following.get_or_add_pPr().get_or_add_spacing()
            if spacing.before is None:
                spacing.before = Pt(8)

# --- Database Models ---
# --- Database Models ---
# Models have been extracted to src/models/ and imported at the top of this file

# --- Forms for Authentication ---
# --- Custom Password Validator ---
# password_check utility has been extracted to src/utils/security.py


# --- Blueprint Registration ---
# Import and register all blueprints for modular route organization



