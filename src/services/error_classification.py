"""Classification of job errors as permanent (do not retry) or transient.

Extracted from src.services.job_queue so the retry policy can be tested
without instantiating the queue or a database. FairJobQueue._is_permanent_error
delegates to is_permanent_error to keep the existing call sites and
tests working.
"""


def is_permanent_error(error_str: str) -> bool:
    """Return True when error_str describes a permanent failure.

    Permanent errors are not retried: bad requests, oversized payloads,
    authentication/authorization failures, billing/quota problems, missing
    models, and unsupported/invalid file formats. Everything else (network
    blips, timeouts, 5xx) is treated as transient and retried up to
    MAX_RETRIES.
    """
    error_lower = (error_str or "").lower()

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
