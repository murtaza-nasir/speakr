"""Markup of the folder lists: a subfolder's indent leaves its count in line (#585)."""
import glob
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = sorted(glob.glob(os.path.join(ROOT, 'templates', '**', '*.html'), recursive=True))


def _indented_tags(html):
    """The start of each tag up to its folderIndentStyle( call."""
    for match in re.finditer(r'folderIndentStyle\(', html):
        start = max(m.start() for m in re.finditer(r'<[a-zA-Z]', html[:match.start()]))
        yield html[start:match.start()]


@pytest.mark.parametrize('path', TEMPLATES, ids=lambda p: os.path.relpath(p, ROOT))
def test_the_indent_sits_on_the_leading_mark_not_the_row(path):
    with open(path, encoding='utf-8') as f:
        html = f.read()
    for tag in _indented_tags(html):
        # The row is the v-for element. A full-width row with a margin
        # overflows its list, so its count or check mark moves out of line.
        assert 'v-for=' not in tag, (os.path.relpath(path, ROOT), tag[:120])


def test_some_list_is_indented():
    found = 0
    for path in TEMPLATES:
        with open(path, encoding='utf-8') as f:
            found += len(list(_indented_tags(f.read())))
    assert found >= 2
