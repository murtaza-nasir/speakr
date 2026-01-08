"""
User-friendly error formatting utility.

Transforms technical error messages into user-friendly explanations with
actionable guidance. Works for both known error patterns and unknown errors.
"""

import re
import json
from typing import Dict, Optional, Tuple


# Known error patterns with user-friendly messages
ERROR_PATTERNS = [
    # File size errors
    {
        'patterns': [
            r'maximum content size limit.*exceeded',
            r'file.*too large',
            r'413.*exceeded',
            r'payload too large',
        ],
        'title': 'File Too Large',
        'message': 'The audio file exceeds the maximum size allowed by the transcription service.',
        'guidance': 'Try enabling audio chunking in your settings, or compress the audio file before uploading.',
        'icon': 'fa-file-audio',
        'type': 'size_limit'
    },
    # Timeout errors
    {
        'patterns': [
            r'timed?\s*out',
            r'timeout',
            r'deadline exceeded',
            r'request took too long',
        ],
        'title': 'Processing Timeout',
        'message': 'The transcription took too long to complete.',
        'guidance': 'This can happen with very long recordings. Try splitting the audio into smaller parts, or increase the timeout setting if available.',
        'icon': 'fa-clock',
        'type': 'timeout'
    },
    # Authentication errors
    {
        'patterns': [
            r'401.*unauthorized',
            r'invalid.*api.*key',
            r'authentication.*failed',
            r'api key.*invalid',
            r'incorrect api key',
        ],
        'title': 'Authentication Error',
        'message': 'The transcription service rejected the API credentials.',
        'guidance': 'Please check that your API key is correct and has not expired. Contact your administrator if the problem persists.',
        'icon': 'fa-key',
        'type': 'auth'
    },
    # Rate limit errors
    {
        'patterns': [
            r'rate.*limit',
            r'too many requests',
            r'429',
            r'quota.*exceeded',
        ],
        'title': 'Rate Limit Exceeded',
        'message': 'Too many requests were sent to the transcription service.',
        'guidance': 'Please wait a few minutes before trying again. The system will automatically retry failed jobs.',
        'icon': 'fa-hourglass-half',
        'type': 'rate_limit'
    },
    # Connection errors
    {
        'patterns': [
            r'connection.*refused',
            r'connection.*reset',
            r'could not connect',
            r'network.*unreachable',
            r'name.*resolution.*failed',
            r'dns.*failed',
        ],
        'title': 'Connection Error',
        'message': 'Could not connect to the transcription service.',
        'guidance': 'Please check your internet connection and ensure the transcription service is available. If using a self-hosted service, verify it is running.',
        'icon': 'fa-wifi',
        'type': 'connection'
    },
    # Service unavailable
    {
        'patterns': [
            r'503.*service unavailable',
            r'502.*bad gateway',
            r'500.*internal server error',
            r'service.*unavailable',
            r'server.*error',
        ],
        'title': 'Service Unavailable',
        'message': 'The transcription service is temporarily unavailable.',
        'guidance': 'This is usually temporary. Please try again in a few minutes.',
        'icon': 'fa-server',
        'type': 'service_error'
    },
    # Invalid audio format
    {
        'patterns': [
            r'invalid.*file.*format',
            r'unsupported.*format',
            r'could not.*decode',
            r'audio.*corrupt',
            r'not.*valid.*audio',
        ],
        'title': 'Invalid Audio Format',
        'message': 'The audio file format is not supported or the file may be corrupted.',
        'guidance': 'Try converting the audio to MP3 or WAV format before uploading. If the file plays correctly on your device, try re-exporting it.',
        'icon': 'fa-file-audio',
        'type': 'format'
    },
    # Insufficient funds/billing
    {
        'patterns': [
            r'insufficient.*funds',
            r'billing.*issue',
            r'payment.*required',
            r'account.*suspended',
        ],
        'title': 'Billing Issue',
        'message': 'There is a billing issue with the transcription service account.',
        'guidance': 'Please check your account status and payment information with the transcription service provider.',
        'icon': 'fa-credit-card',
        'type': 'billing'
    },
    # Model not found
    {
        'patterns': [
            r'model.*not.*found',
            r'invalid.*model',
            r'model.*does not exist',
        ],
        'title': 'Model Not Available',
        'message': 'The requested transcription model is not available.',
        'guidance': 'Please check the model name in your settings. The model may have been deprecated or renamed.',
        'icon': 'fa-microchip',
        'type': 'model'
    },
    # Audio extraction failed
    {
        'patterns': [
            r'audio.*extraction.*failed',
            r'could not.*extract.*audio',
            r'ffmpeg.*failed',
            r'no audio.*stream',
        ],
        'title': 'Audio Extraction Failed',
        'message': 'Could not extract audio from the uploaded file.',
        'guidance': 'The file may be corrupted or in an unsupported format. Try converting it to a standard audio format (MP3, WAV) before uploading.',
        'icon': 'fa-file-video',
        'type': 'extraction'
    },
]


def extract_error_details(error_text: str) -> Dict:
    """
    Extract structured error details from raw error text.
    Attempts to parse JSON error responses from APIs.
    """
    details = {
        'raw': error_text,
        'code': None,
        'message': None,
        'type': None,
    }

    # Try to extract error code
    code_match = re.search(r'(?:error\s*code|status)[:\s]*(\d{3})', error_text, re.IGNORECASE)
    if code_match:
        details['code'] = code_match.group(1)

    # Try to parse JSON error structure
    json_match = re.search(r'\{[^{}]*["\']error["\'][^{}]*\}', error_text)
    if json_match:
        try:
            # Clean up the JSON-like string
            json_str = json_match.group(0).replace("'", '"')
            error_obj = json.loads(json_str)
            if 'error' in error_obj:
                err = error_obj['error']
                if isinstance(err, dict):
                    details['message'] = err.get('message')
                    details['type'] = err.get('type')
                    details['code'] = details['code'] or err.get('code')
        except (json.JSONDecodeError, KeyError):
            pass

    # Try to extract message from common patterns
    if not details['message']:
        msg_match = re.search(r"['\"]message['\"]\s*:\s*['\"]([^'\"]+)['\"]", error_text)
        if msg_match:
            details['message'] = msg_match.group(1)

    return details


def format_error_for_user(error_text: str) -> Dict:
    """
    Transform a technical error message into a user-friendly format.

    Returns:
        Dict with keys:
        - title: Short, user-friendly title
        - message: Plain language explanation
        - guidance: Actionable suggestion
        - icon: FontAwesome icon class
        - type: Error category
        - technical: Original error (for advanced users/debugging)
        - is_known: Whether this matched a known pattern
    """
    if not error_text:
        return {
            'title': 'Unknown Error',
            'message': 'An unexpected error occurred.',
            'guidance': 'Please try again. If the problem persists, contact support.',
            'icon': 'fa-exclamation-triangle',
            'type': 'unknown',
            'technical': '',
            'is_known': False
        }

    error_lower = error_text.lower()

    # Check against known patterns
    for pattern_info in ERROR_PATTERNS:
        for pattern in pattern_info['patterns']:
            if re.search(pattern, error_lower):
                return {
                    'title': pattern_info['title'],
                    'message': pattern_info['message'],
                    'guidance': pattern_info['guidance'],
                    'icon': pattern_info['icon'],
                    'type': pattern_info['type'],
                    'technical': error_text,
                    'is_known': True
                }

    # Unknown error - try to make it more readable
    details = extract_error_details(error_text)

    # Clean up the error message for display
    clean_message = details['message'] or error_text

    # Remove common prefixes
    for prefix in ['Transcription failed:', 'Processing failed:', 'Error:', 'Exception:']:
        if clean_message.startswith(prefix):
            clean_message = clean_message[len(prefix):].strip()

    # Truncate very long messages
    if len(clean_message) > 200:
        clean_message = clean_message[:200] + '...'

    # Generate a reasonable title based on error code
    title = 'Processing Error'
    if details['code']:
        code = details['code']
        if code.startswith('4'):
            title = 'Request Error'
        elif code.startswith('5'):
            title = 'Server Error'

    return {
        'title': title,
        'message': clean_message,
        'guidance': 'If this error persists, try reprocessing the recording or contact support for assistance.',
        'icon': 'fa-exclamation-circle',
        'type': 'unknown',
        'technical': error_text,
        'is_known': False
    }


def format_error_for_storage(error_text: str) -> str:
    """
    Format an error message for storage in the database.
    Returns a JSON string that can be parsed by the frontend for nice display.

    The format is: ERROR_JSON:{"title": "...", "message": "...", ...}

    This allows the frontend to detect formatted errors and display them nicely,
    while still being human-readable if viewed raw.
    """
    formatted = format_error_for_user(error_text)

    # Create a compact JSON representation
    error_data = {
        't': formatted['title'],
        'm': formatted['message'],
        'g': formatted['guidance'],
        'i': formatted['icon'],
        'y': formatted['type'],
        'k': formatted['is_known'],
    }

    # Only include technical details if it adds value
    if formatted['technical'] and formatted['technical'] != formatted['message']:
        error_data['d'] = formatted['technical'][:500]  # Limit technical detail length

    try:
        json_str = json.dumps(error_data, ensure_ascii=False)
        return f"ERROR_JSON:{json_str}"
    except (TypeError, ValueError):
        # Fallback to plain text if JSON encoding fails
        return f"{formatted['title']}: {formatted['message']}"


def parse_stored_error(stored_text: str) -> Optional[Dict]:
    """
    Parse a stored error message. Returns the formatted error dict if it's
    a JSON-formatted error, or None if it's plain text.
    """
    if not stored_text or not stored_text.startswith('ERROR_JSON:'):
        return None

    try:
        json_str = stored_text[11:]  # Remove 'ERROR_JSON:' prefix
        data = json.loads(json_str)
        return {
            'title': data.get('t', 'Error'),
            'message': data.get('m', 'An error occurred'),
            'guidance': data.get('g', ''),
            'icon': data.get('i', 'fa-exclamation-circle'),
            'type': data.get('y', 'unknown'),
            'is_known': data.get('k', False),
            'technical': data.get('d', ''),
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def is_transcription_error(transcription_text: str) -> bool:
    """
    Check if the transcription text is actually an error message.

    Returns True if the text is an error message (not valid transcription content).
    This should be used to prevent operations like summarization or chat on failed recordings.
    """
    if not transcription_text:
        return False

    # Check for JSON-formatted error
    if transcription_text.startswith('ERROR_JSON:'):
        return True

    # Check for legacy error prefixes
    error_prefixes = [
        'Transcription failed:',
        'Processing failed:',
        'ASR processing failed:',
        'Audio extraction failed:',
        'Upload/Processing failed:',
    ]

    for prefix in error_prefixes:
        if transcription_text.startswith(prefix):
            return True

    return False
