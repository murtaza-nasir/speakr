"""Filters for log records of libraries that carry request data.

httpx logs every request at INFO with its full URL. The transcription
connectors send the initial prompt and the hotwords of a recording as query
parameters, so that line put them in the default log. The connector and the
upload path logged the same parameters themselves; loggable_params replaces
the two user texts by their length.
"""

import logging


class NoQueryString(logging.Filter):
    """Drop the query string from the URL in httpx's "HTTP Request" record."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(self._clean(arg) for arg in args)
        return True

    @staticmethod
    def _clean(arg):
        query = getattr(arg, 'query', None)
        copy_with = getattr(arg, 'copy_with', None)
        if query and callable(copy_with):
            try:
                return copy_with(query=None)
            except Exception:
                return arg
        return arg


# Request parameters written by the user: logged as a length at INFO and in
# full at DEBUG only (discussion #603).
USER_TEXT_PARAMS = ('initial_prompt', 'hotwords')


def loggable_params(params):
    """Request or job parameters with user text replaced by its length."""
    if not isinstance(params, dict):
        return params
    return {key: (f"<{len(str(value))} characters>" if key in USER_TEXT_PARAMS and value else value)
            for key, value in params.items()}


def install():
    """Attach the filters. Safe to call again."""
    httpx_logger = logging.getLogger('httpx')
    if not any(isinstance(f, NoQueryString) for f in httpx_logger.filters):
        httpx_logger.addFilter(NoQueryString())
