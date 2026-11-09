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

# <tag ... v-text="..." ...>content</tag> with content that is not whitespace,
# for elements without nested tags (nested ones are matched at their own level).
PATTERN = re.compile(
    r"<(?P<tag>[a-zA-Z][\w-]*)\b(?P<attrs>[^<>]*?\sv-(?:text|html)=(?:\"[^\"]*\"|'[^']*')[^<>]*)>"
    r"(?P<body>[^<]*)</(?P=tag)>",
    re.S,
)


def _templates():
    for folder, _, files in os.walk(TEMPLATES):
        for name in files:
            if name.endswith(".html"):
                yield os.path.join(folder, name)


@pytest.mark.parametrize("path", sorted(_templates()), ids=lambda p: os.path.relpath(p, TEMPLATES))
def test_no_content_beside_v_text_or_v_html(path):
    text = open(path, encoding="utf-8").read()
    bad = [m.group(0)[:120] for m in PATTERN.finditer(text)
           if m.group("body").strip() and "{%" not in m.group("body") and "{{" not in m.group("body")]
    assert not bad, f"v-text/v-html element with its own content: {bad}"


def test_the_pattern_finds_the_case_it_guards():
    sample = """<span v-text="t('nav.x')">Fallback</span> <span v-text="t('y')"></span>"""
    found = [m.group("body") for m in PATTERN.finditer(sample) if m.group("body").strip()]
    assert found == ["Fallback"]
