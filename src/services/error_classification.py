"""Classification of job errors as permanent (do not retry) or transient.

Extracted from src.services.job_queue so the retry policy can be tested
without instantiating the queue or a database. FairJobQueue._is_permanent_error
delegates to is_permanent_error to keep the existing call sites and
tests working.
"""

import re

# The status of the failed request as the connectors and SDKs word it
# ("status 500", "Error code: 503"). The first one in the message is the
# request's own status; anything after it is the response body.
_STATUS_RE = re.compile(r'(?:error\s*code|status)[:\s]*(\d{3})(?!\d)', re.IGNORECASE)


def is_permanent_error(error_str: str) -> bool:
    """Return True when error_str describes a permanent failure.

    Permanent errors are not retried: bad requests, oversized payloads,
    authentication/authorization failures, billing/quota problems, missing
    models, and unsupported/invalid file formats. Everything else (network
    blips, timeouts, 5xx) is treated as transient and retried up to
    MAX_RETRIES.

    The status code wins over the words: a 5xx is transient even when its
    body contains "unauthorized" or "bad request" (#594), because the body
    is whatever the service or the audio put there.
    """
    error_lower = (error_str or "").lower()

    status = _STATUS_RE.search(error_lower)
    if status and status.group(1).startswith('5'):
        return False

    # HTTP status codes that indicate permanent errors
    permanent_codes = ['400', '413', '401', '402', '403', '404']
    for code in permanent_codes:
        if f'error code: {code}' in error_lower or f'status {code}' in error_lower:
            return True

    # Specific error patterns that are permanent (simple substring matching)
    permanent_patterns = [
        'maximum content size limit',
        'file too large',
        'payload too large',
        'invalid api key',
        'incorrect api key',
        'authentication failed',
        'unauthorized',
        'permission denied',
        'access denied',
        'billing',
        'payment required',
        'quota exceeded',
        'insufficient funds',
        # A paid submission with no clear outcome (Tencent ASR); only a
        # reprocess, which starts a new job, submits it again.
        'had no clear outcome',
        'model not found',
        'invalid model',
        'unsupported format',
        'invalid file format',
        'invalid_request_error',
        'bad request',
        'unparseable',
    ]

    for pattern in permanent_patterns:
        if pattern in error_lower:
            return True

    return False


class SummaryStepFailed(Exception):
    """The summary step failed after storing its outcome on the recording (#583).

    generate_summary_only_task has already written the
    "[Summary generation failed: ...]" text, the FAILED status and the
    error_message when this is raised. The job queue classifies the message
    (permanent or transient) like any other job error, but keeps the stored
    summary instead of replacing it. ``stored_summary`` holds that text so the
    caller can report it.
    """

    def __init__(self, message, stored_summary=None):
        super().__init__(message)
        self.stored_summary = stored_summary
