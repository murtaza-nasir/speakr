"""Length of stored file names (#430).

A file system allows 255 bytes in one name. Speakr adds a timestamp and an id
to an uploaded file's name when it stores the file, so a name of 208 or more
characters failed with a 500. The stored name is shortened; the original
name is kept in Recording.original_filename for display.
"""

import os

MAX_NAME_BYTES = 150


def shorten_filename(name, max_bytes=MAX_NAME_BYTES):
    """``name`` cut to ``max_bytes`` of UTF-8, keeping its extension."""
    if len(name.encode('utf-8')) <= max_bytes:
        return name
    stem, ext = os.path.splitext(name)
    if len(ext.encode('utf-8')) > 20:
        ext = ''
    budget = max_bytes - len(ext.encode('utf-8'))
    stem = stem.encode('utf-8')[:budget].decode('utf-8', 'ignore')
    return stem + ext
