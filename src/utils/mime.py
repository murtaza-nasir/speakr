"""Shared MIME-type helpers.

Single source of truth for the MIME type of a stored media file. The pipeline
already probes every file with ffprobe (``get_codec_info``), so we derive the
MIME from the ACTUAL container + stream contents instead of guessing from the
filename extension. Guessing is wrong for ambiguous containers — most visibly
``.webm``, which src/app.py registers as ``audio/webm`` for in-app audio
recordings, so ``mimetypes.guess_type`` reports audio/webm for EVERY ``.webm``
even one carrying a video stream (which then hides the video player in the UI).

Order of preference:
  1. ffprobe ``format_name`` + ``has_video`` (content truth).
  2. extension-based fallback, only when the probe is unavailable.
"""

import mimetypes
import os


# Extension fallback for files known to carry video, used only when a probe is
# unavailable. Notably '.webm' (registered as audio/webm in src/app.py).
_VIDEO_MIME_BY_EXT = {
    '.webm': 'video/webm',
    '.ogg': 'video/ogg',
    '.ogv': 'video/ogg',
    '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime',
    '.m4v': 'video/x-m4v',
    '.ts': 'video/mp2t',
    '.mts': 'video/mp2t',
}


def video_mime_for_path(path):
    """Extension-based ``video/*`` MIME for a file known to retain video.

    Fallback used when no probe is available. Prefer :func:`resolve_media_mime`,
    which derives the type from the actual file contents.

    Our explicit map is authoritative: ``mimetypes.guess_type`` reads the host's
    ``/etc/mime.types`` and is NOT consistent across platforms (e.g. ``.m4v``
    resolves to ``video/x-m4v`` on some systems and ``video/mp4`` on others), so
    consulting it first made the result host-dependent. Only fall through to it
    for extensions we don't explicitly know about.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _VIDEO_MIME_BY_EXT:
        return _VIDEO_MIME_BY_EXT[ext]
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed.startswith('video/'):
        return guessed
    return 'video/mp4'


def _mime_from_format(format_name, has_video, path=''):
    """Map an ffprobe ``format_name`` (+ ``has_video``) to a MIME type.

    ``format_name`` is a comma-separated list of names the demuxer matched
    (e.g. ``'matroska,webm'``, ``'mov,mp4,m4a,3gp,3g2,mj2'``). Containers that
    can hold either audio or video (webm/matroska, mp4, ogg, asf) pick the
    video or audio MIME from ``has_video``. Returns ``None`` for an
    unrecognised container so the caller can fall back.
    """
    fmt = (format_name or '').lower()
    ext = os.path.splitext(path or '')[1].lower()

    def matches(*names):
        return any(n in fmt for n in names)

    if matches('webm', 'matroska'):
        # One demuxer covers both; the extension disambiguates webm vs mkv.
        if has_video:
            return 'video/x-matroska' if ext == '.mkv' else 'video/webm'
        return 'audio/x-matroska' if ext == '.mkv' else 'audio/webm'
    if matches('mp4', 'mov', 'm4a', '3gp', '3g2', 'mj2'):
        return 'video/mp4' if has_video else 'audio/mp4'
    if matches('ogg'):
        return 'video/ogg' if has_video else 'audio/ogg'
    if matches('asf'):  # Windows Media (wmv / wma)
        return 'video/x-ms-wmv' if has_video else 'audio/x-ms-wma'
    if matches('mp3'):
        return 'audio/mpeg'
    if matches('flac'):
        return 'audio/flac'
    if matches('wav', 'wave'):
        return 'audio/wav'
    if matches('aiff', 'aif'):
        return 'audio/aiff'
    if matches('aac', 'adts'):
        return 'audio/aac'
    if matches('avi'):
        return 'video/x-msvideo'
    if matches('flv'):
        return 'video/x-flv'
    if matches('mpegts', 'mpeg-ts', 'mp2t'):
        return 'video/mp2t'
    return None


def resolve_media_mime(path, codec_info=None, has_video=None, timeout=10):
    """Authoritative MIME for a stored media file, derived from its contents.

    Args:
        path: the stored media file.
        codec_info: a pre-fetched ``get_codec_info`` dict, if the caller has
            one (avoids a second probe).
        has_video: caller's authoritative video flag. Overrides the probe's
            own ``has_video`` when provided (e.g. a retention branch that has
            already decided the file is video). ``None`` = trust the probe.
        timeout: ffprobe timeout when this function does its own probe.

    Falls back to an extension-based guess only when no probe data is
    available (probe failed / ffprobe missing).
    """
    fmt = None
    hv = has_video
    if codec_info:
        fmt = codec_info.get('format_name')
        if hv is None:
            hv = bool(codec_info.get('has_video'))
    if fmt is None:
        try:
            from src.utils.ffprobe import get_codec_info
            info = get_codec_info(path, timeout=timeout)
            if info:
                fmt = info.get('format_name')
                if hv is None:
                    hv = bool(info.get('has_video'))
        except Exception:
            pass

    if fmt:
        m = _mime_from_format(fmt, bool(hv), path)
        if m:
            return m

    # No usable probe data — fall back to the filename.
    if hv:
        return video_mime_for_path(path)
    guessed, _ = mimetypes.guess_type(path)
    return guessed or 'application/octet-stream'
