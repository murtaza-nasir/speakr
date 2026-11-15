"""The temporary files of incognito requests, in a folder of their own.

An incognito upload, its converted copies and its chunk folder used to be
written to the temporary folder of the system, among the files of every other
job. After a crash or a kill of the process during the request they stayed
there until the container was recreated, and nothing could tell them from
other temporary files.

Each incognito request now gets its own folder under one root
(``INCOGNITO_TEMP_DIR``, by default ``speakr-incognito`` in the temporary
folder of the system, mode 0700). The upload is saved there, the conversion
helpers write next to their input, and the chunk folder is created inside it.
The route removes the request folder before it answers.

What a crash leaves behind is removed by ``cleanup_stale``: at startup in
every process, and every hour by the cleanup thread. Several gunicorn workers
start at once, and another worker may be in the middle of an incognito
request, so an entry is removed only when nothing in it was written for
``incognito_temp_max_age_hours`` (``INCOGNITO_TEMP_MAX_AGE_HOURS``, default
6). The value must be longer than the longest incognito request. Two sweeps
that meet on the same entry are harmless: a removal that finds nothing is
ignored.
"""

import logging
import os
import shutil
import tempfile
import time

logger = logging.getLogger(__name__)

REQUEST_PREFIX = 'req-'


def root():
    """The folder that holds the request folders. Created on first use."""
    path = (os.environ.get('INCOGNITO_TEMP_DIR') or '').split('#')[0].strip()
    if not path:
        path = os.path.join(tempfile.gettempdir(), 'speakr-incognito')
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def new_request_dir():
    """A new, empty folder for one incognito request (mode 0700)."""
    return tempfile.mkdtemp(prefix=REQUEST_PREFIX, dir=root())


def remove_request_dir(path):
    """Remove the folder of a request with everything in it. True when it is gone."""
    if not path:
        return True
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.exists(path)


def inside(path):
    """Whether a file is in the incognito folder."""
    if not path:
        return False
    try:
        base = os.path.realpath(root())
        return os.path.commonpath([base, os.path.realpath(path)]) == base
    except (ValueError, OSError):
        return False


def dir_for_temp_files(filepath):
    """Where the temporary files made for ``filepath`` belong.

    The folder of the file when it is an incognito file, so that its chunks
    stay inside the request folder; None (the temporary folder of the system)
    for every other file.
    """
    return os.path.dirname(os.path.abspath(filepath)) if inside(filepath) else None


def max_age_seconds():
    from src.utils.system_settings import ENV_BACKED_SETTINGS, env_fallback
    env_var, default = ENV_BACKED_SETTINGS['incognito_temp_max_age_hours'][:2]
    fallback = env_fallback(env_var, default)
    try:
        from flask import has_app_context
        if has_app_context():
            from src.models import SystemSetting
            value = SystemSetting.get_setting('incognito_temp_max_age_hours', fallback)
            fallback = fallback if value is None else value
    except Exception:
        pass
    try:
        hours = float(fallback)
    except (TypeError, ValueError):
        hours = float(default)
    return max(hours, 0.0) * 3600


def _newest_change(path):
    """The latest modification time of an entry and of everything under it."""
    newest = os.lstat(path).st_mtime
    if os.path.isdir(path) and not os.path.islink(path):
        for folder, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    newest = max(newest, os.lstat(os.path.join(folder, name)).st_mtime)
                except OSError:
                    continue
    return newest


def cleanup_stale(max_age=None, now=None):
    """Remove entries of the incognito folder that nothing wrote to for max_age seconds.

    Returns the number of entries removed. Never raises: a failure is logged
    without the name of a file (the names are random, and the count is enough).
    """
    try:
        base = root()
        max_age = max_age_seconds() if max_age is None else max_age
        now = time.time() if now is None else now
        removed = 0
        with os.scandir(base) as entries:
            names = [entry.path for entry in entries]
        for path in names:
            try:
                if now - _newest_change(path) <= max_age:
                    continue
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                if not os.path.lexists(path):
                    removed += 1
            except FileNotFoundError:
                continue  # another worker removed it first
            except OSError as e:
                logger.warning(f"A leftover incognito temporary entry could not be removed: {type(e).__name__}")
        if removed:
            logger.warning(f"Removed {removed} leftover incognito temporary entr{'y' if removed == 1 else 'ies'} "
                           f"(left by a request that did not end normally)")
        return removed
    except Exception as e:
        logger.warning(f"The sweep of the incognito temporary folder failed: {type(e).__name__}")
        return 0
