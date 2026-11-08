"""Right-to-left text in summaries, notes and transcripts (#414).

Each block takes its direction from its own text: an Arabic, Hebrew,
Persian or Urdu paragraph reads right to left, with list bullets on the
right, and the rest stays left to right. Code stays left to right, and a
direction supplied in the text itself is still removed by the sanitizer.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.markdown import md_to_html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_blocks_take_their_direction_from_their_text():
    html = md_to_html("## خلاصہ\n\nٹیم نے بات کی۔\n\n- پہلا\n- Dan will send it.\n\n> نقل\n")
    for tag in ("h2", "p", "ul", "li", "blockquote"):
        assert re.search(rf'<{tag} dir="auto">', html), tag


def test_code_and_tables_keep_their_shape():
    html = md_to_html("```\nx = 1\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<pre" in html and 'pre dir=' not in html and 'code dir=' not in html
    assert '<td dir="auto">1</td>' in html and '<table>' in html


def test_a_direction_in_the_text_is_not_kept():
    html = md_to_html('<p dir="rtl" onclick="x()">hi</p>')
    assert 'dir="rtl"' not in html and "onclick" not in html


def test_transcript_and_chat_text_follow_their_own_direction():
    css = open(os.path.join(ROOT, "static/css/styles.css")).read()
    rule = css[css.index("Right-to-left text (#414)"):]
    for selector in (".speaker-text", ".speaker-bubble-content", ".transcript-segment", ".bidi-auto"):
        assert selector in rule
    assert "unicode-bidi: plaintext" in rule and "text-align: start" in rule
    for path in ("templates/components/header.html", "templates/themes/default/sidebar-row.html"):
        assert 'dir="auto"' in open(os.path.join(ROOT, path)).read(), path
