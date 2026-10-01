"""Sampling temperatures for the LLM requests Speakr makes (#411).

Each kind of request has a temperature. The value saved by an administrator in
the Default Prompts tab (SystemSetting ``llm_temperature_<kind>``) applies when
it is set; otherwise the built-in default applies, unchanged from earlier
releases. A saved value that is not a number between 0 and 2 is ignored with a
warning. Values are read on every request, so a change in the admin settings
applies to the next request without a restart.

Inquire and speaker identification keep fixed temperatures, tuned per step.
"""

import logging

logger = logging.getLogger(__name__)

MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0

# kind -> built-in default
TEMPERATURES = {
    'summary': 0.5,
    'title': 0.7,
    'chat': 0.7,
    'event': 0.2,
}


def setting_key(kind):
    return f'llm_temperature_{kind}'


def parse_temperature(value):
    """A float in [0, 2], or None when the value is empty or invalid."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or not (MIN_TEMPERATURE <= number <= MAX_TEMPERATURE):  # NaN or out of range
        return None
    return number


def _admin_value(kind):
    try:
        from src.models import SystemSetting
        setting = SystemSetting.query.filter_by(key=setting_key(kind)).first()
        return setting.value if setting else None
    except Exception as e:  # no app context or database not ready
        logger.debug(f"Could not read admin temperature for {kind}: {e}")
        return None


def resolve_temperature(kind):
    """(value, source) for a kind; source is 'admin' or 'default'."""
    default = TEMPERATURES[kind]
    raw_admin = _admin_value(kind)
    if raw_admin not in (None, ''):
        value = parse_temperature(raw_admin)
        if value is not None:
            return value, 'admin'
        logger.warning(f"Ignoring admin {kind} temperature {raw_admin!r}: not a number between 0 and 2")
    return default, 'default'


def get_temperature(kind):
    """The temperature to send with a request of this kind."""
    return resolve_temperature(kind)[0]


def temperatures_status():
    """Effective value, source and default for every kind."""
    status = {}
    for kind, default in TEMPERATURES.items():
        value, source = resolve_temperature(kind)
        status[kind] = {'value': value, 'source': source, 'default': default}
    return status
