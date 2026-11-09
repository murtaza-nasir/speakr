"""An element with v-text or v-html has no content of its own.

Vue's production build refuses such a template (compiler error 57, "v-text
will override element's existing children"), so the whole Vue app on that
page fails to mount; the development build only warns. The page header broke
this way on 9 Nov 2026 when its back link carried fallback text.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")

# <tag ...attributes...>content</tag> for elements without nested tags (nested
# ones are matched at their own level). Attribute values are read as quoted
# strings, so a ">" inside one (v-if="items.length > 0") does not end the tag.
PATTERN = re.compile(
    r"<(?P<tag>[a-zA-Z][\w-]*)"
    r"(?P<attrs>(?:\s+[^\s=<>\"'/]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?)*)"
    r"\s*>(?P<body>[^<]*)</(?P=tag)>",
    re.S,
)
BINDS_CONTENT = re.compile(r"\sv-(?:text|html)\s*=")


def _templates():
    for folder, _, files in os.walk(TEMPLATES):
        for name in files:
            if name.endswith(".html"):
                yield os.path.join(folder, name)


@pytest.mark.parametrize("path", sorted(_templates()), ids=lambda p: os.path.relpath(p, TEMPLATES))
def test_no_content_beside_v_text_or_v_html(path):
    text = open(path, encoding="utf-8").read()
    bad = [m.group(0)[:120] for m in PATTERN.finditer(text)
           if BINDS_CONTENT.search(m.group("attrs")) and m.group("body").strip()
           and "{%" not in m.group("body") and "{{" not in m.group("body")]
    assert not bad, f"v-text/v-html element with its own content: {bad}"


def _flagged(sample):
    return [m.group("body") for m in PATTERN.finditer(sample)
            if BINDS_CONTENT.search(m.group("attrs")) and m.group("body").strip()]


def test_the_pattern_finds_the_case_it_guards():
    sample = """<span v-text="t('nav.x')">Fallback</span> <span v-text="t('y')"></span>"""
    assert _flagged(sample) == ["Fallback"]


def test_a_greater_than_sign_inside_an_attribute_is_not_content():
    sample = """<span v-text="t('tags')" v-if="tags.length > 0"></span><b class="x">Plain</b>"""
    assert _flagged(sample) == []
