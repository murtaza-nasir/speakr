"""
Markdown and HTML utilities for converting and sanitizing text content.

This module provides functions for converting markdown to HTML and
sanitizing HTML to prevent XSS and other security issues.
"""

import re
import threading
import markdown
import bleach

_EXTENSIONS = [
    'fenced_code',      # Fenced code blocks
    'tables',           # Table support
    'attr_list',        # Attribute lists
    'def_list',         # Definition lists
    'footnotes',        # Footnotes
    'abbr',             # Abbreviations
    'codehilite',       # Syntax highlighting for code blocks
    'smarty'            # Smart quotes, dashes, etc.
]

# A Markdown instance keeps state while it converts, so one instance shared by
# the request threads mixed the HTML of concurrent conversions (#442). Each
# thread builds its own instance once and reuses it.
_local = threading.local()


def _markdown():
    md = getattr(_local, 'md', None)
    if md is None:
        md = _local.md = markdown.Markdown(extensions=_EXTENSIONS)
    return md


def sanitize_html(text):
    """
    Sanitize HTML content to prevent XSS and other security issues.

    Args:
        text (str): HTML text to sanitize

    Returns:
        str: Sanitized HTML text
    """
    if not text:
        return ""

    # Remove any template-like syntax that could be exploited
    # Remove {{ }} style template syntax
    text = re.sub(r'\{\{.*?\}\}', '', text, flags=re.DOTALL)
    text = re.sub(r'\{%.*?%\}', '', text, flags=re.DOTALL)

    # Remove other template-like syntax
    text = re.sub(r'<%.*?%>', '', text, flags=re.DOTALL)
    text = re.sub(r'<\?.*?\?>', '', text, flags=re.DOTALL)

    # Define allowed tags and attributes for safe HTML
    allowed_tags = [
        'p', 'br', 'strong', 'b', 'em', 'i', 'u', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li', 'blockquote', 'code', 'pre', 'a', 'img', 'table', 'thead',
        'tbody', 'tr', 'th', 'td', 'dl', 'dt', 'dd', 'div', 'span', 'hr', 'sup', 'sub'
    ]

    allowed_attributes = {
        'a': ['href', 'title'],
        'img': ['src', 'alt', 'title', 'width', 'height'],
        'code': ['class'],  # For syntax highlighting
        'pre': ['class'],   # For syntax highlighting
        'div': ['class'],   # For code blocks
        'span': ['class'],  # For syntax highlighting
        'th': ['align'],
        'td': ['align'],
        'table': ['class']
    }

    # Sanitize the HTML to remove dangerous content
    sanitized_html = bleach.clean(
        text,
        tags=allowed_tags,
        attributes=allowed_attributes,
        protocols=['http', 'https', 'mailto'],
        strip=True  # Strip disallowed tags instead of escaping them
    )

    return sanitized_html


def md_to_html(text):
    """
    Convert markdown text to sanitized HTML.

    Args:
        text (str): Markdown text to convert

    Returns:
        str: Sanitized HTML output
    """
    if not text:
        return ""

    # Fix list spacing
    def fix_list_spacing(text):
        lines = text.split('\n')
        result = []
        in_list = False

        for line in lines:
            stripped = line.strip()

            # Check if this line is a list item (starts with -, *, +, or number.)
            is_list_item = (
                stripped.startswith(('- ', '* ', '+ ')) or
                (stripped and stripped[0].isdigit() and '. ' in stripped[:10])
            )

            # If we're starting a new list or continuing a list, ensure proper spacing
            if is_list_item:
                if not in_list and result and result[-1].strip():
                    # Starting a new list - add blank line before
                    result.append('')
                in_list = True
            elif in_list and stripped and not is_list_item:
                # Ending a list - add blank line after the list
                if result and result[-1].strip():
                    result.append('')
                in_list = False

            result.append(line)

        return '\n'.join(result)

    # Fix list spacing
    processed_text = fix_list_spacing(text)

    # Reset the thread's instance to clear any state from its previous conversion
    md = _markdown()
    md.reset()
    html = md.convert(processed_text)

    # Apply sanitization to the generated HTML
    return _with_auto_direction(sanitize_html(html))


# Block elements that take their direction from their own text (#414): an
# Arabic, Hebrew, Persian or Urdu paragraph or list reads right to left, with
# its bullet on the right, while the rest of the summary stays left to right.
# Code stays left to right.
_AUTO_DIR_TAGS = re.compile(r'<(p|li|ul|ol|h[1-6]|blockquote|td|th|dt|dd)(?=[\s>])(?![^>]*\bdir=)')


def _with_auto_direction(html):
    return _AUTO_DIR_TAGS.sub(r'<\1 dir="auto"', html)
