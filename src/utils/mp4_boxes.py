"""Reading the box structure of MP4 and QuickTime files without decoding them (#539).

A fragmented MP4, as OBS writes it, keeps its sample tables in many small
``moof`` boxes spread over the file instead of one ``moov`` index. A browser
playing such a file seeks by fetching fragment after fragment in small range
requests, so seeking in a retained video is slow. Remuxing it into a regular
MP4 with the index at the front fixes that without touching the streams.

Only box headers are read: each header gives the box size, and the reader
seeks past the payload. The scan is capped, and anything that does not parse
as a well-formed ISO base media file is reported as unknown, so the caller
leaves it alone.
"""

import os
import struct
from typing import Optional

# Box types a well-formed MP4, M4V or QuickTime file starts with.
_FIRST_BOX_TYPES = {b'ftyp', b'styp', b'moov', b'mdat', b'free', b'skip', b'wide', b'pnot', b'uuid'}

# Upper bounds on boxes visited: a fragmented recording has two top-level
# boxes (moof, mdat) per fragment, so hours of video stay far below these.
MAX_TOP_LEVEL_BOXES = 500_000
MAX_MOOV_CHILDREN = 10_000


class _Unparsable(Exception):
    pass


def _boxes(fh, start, end, limit):
    """Yield (type, offset, header_size, size) for each box in [start, end)."""
    pos = start
    count = 0
    while pos < end:
        count += 1
        if count > limit:
            raise _Unparsable('too many boxes')
        if end - pos < 8:
            raise _Unparsable('truncated box header')
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            raise _Unparsable('truncated box header')
        size, box_type = struct.unpack('>I4s', header)
        header_size = 8
        if size == 1:
            large = fh.read(8)
            if len(large) < 8:
                raise _Unparsable('truncated largesize')
            size = struct.unpack('>Q', large)[0]
            header_size = 16
        elif size == 0:
            size = end - pos  # the box runs to the end of its parent
        if size < header_size:
            raise _Unparsable('box smaller than its header')
        if pos + size > end:
            raise _Unparsable('box runs past the end of the file')
        if not all(0x20 <= c <= 0x7e for c in box_type):
            raise _Unparsable('box type is not a four-character code')
        yield box_type, pos, header_size, size
        pos += size


def is_fragmented_mp4(path) -> Optional[bool]:
    """Whether the file at path is a fragmented MP4 or QuickTime file.

    True when the ``moov`` box holds an ``mvex`` box or the file has
    top-level ``moof`` boxes; False for a well-formed file with neither;
    None when the file is not an ISO base media file, has no ``moov`` box,
    is truncated or otherwise does not parse.
    """
    try:
        file_size = os.path.getsize(path)
        if file_size < 8:
            return None
        with open(path, 'rb') as fh:
            moov = None
            has_moof = False
            for index, (box_type, offset, header_size, size) in enumerate(
                    _boxes(fh, 0, file_size, MAX_TOP_LEVEL_BOXES)):
                if index == 0 and box_type not in _FIRST_BOX_TYPES:
                    return None
                if box_type == b'moov':
                    if moov is not None:
                        return None  # two indexes: not a file to rewrite
                    moov = (offset + header_size, offset + size)
                elif box_type == b'moof':
                    has_moof = True
            if moov is None:
                return None
            has_mvex = any(child == b'mvex' for child, _, _, _ in
                           _boxes(fh, moov[0], moov[1], MAX_MOOV_CHILDREN))
            return has_mvex or has_moof
    except (OSError, struct.error, _Unparsable):
        return None
