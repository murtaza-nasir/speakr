"""A text that makes the Markdown parser raise is shown escaped, as plain
paragraphs, instead of failing the request (CVE-2025-69534)."""
from unittest.mock import patch

from src.utils import markdown as md_module
from src.utils.markdown import md_to_html


def test_parser_error_falls_back_to_escaped_paragraphs():
    with patch.object(md_module.markdown.Markdown, "convert", side_effect=AssertionError("bad")):
        html = md_to_html("First <b>part</b>\n\nSecond part")
    assert html == '<p dir="auto">First &lt;b&gt;part&lt;/b&gt;</p><p dir="auto">Second part</p>'


def test_next_conversion_on_the_thread_works():
    with patch.object(md_module.markdown.Markdown, "convert", side_effect=AssertionError("bad")):
        md_to_html("x")
    assert md_to_html("**ok**") == '<p dir="auto"><strong>ok</strong></p>'
