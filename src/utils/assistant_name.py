"""The assistant's name (Ambrose by default), saved per user as
``User.assistant_name``. The same rule as mailr's ``assistant_name`` profile
option, so one name works in both apps: 1 to 40 letters, digits, spaces,
dots, hyphens or apostrophes, starting with a letter or digit. An empty value
means the default.

The name is shown in the interface only: the chat prompts do not use it.
"""

import re
from typing import Optional

DEFAULT_ASSISTANT_NAME = 'Ambrose'
MAX_ASSISTANT_NAME_LENGTH = 40

_NAME = re.compile(r"^[\w][\w .'-]{0,%d}$" % (MAX_ASSISTANT_NAME_LENGTH - 1), re.UNICODE)


def clean_assistant_name(value) -> Optional[str]:
    """The value to store: None for an empty value or the default (the
    default is not stored, so a later change of the default reaches every
    account that did not choose a name), else the trimmed name with inner
    whitespace collapsed. Raises ValueError for a name outside the rule."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('assistant_name must be a string')
    name = ' '.join(value.split())
    if not name or name == DEFAULT_ASSISTANT_NAME:
        return None
    if not _NAME.match(name):
        raise ValueError('assistant_name: 1-40 letters, digits, spaces, dots, hyphens or apostrophes')
    return name


def assistant_name_of(user) -> str:
    """The name to show for this user; the default for a visitor, a user
    without a saved name, or a saved value that no longer fits the rule."""
    saved = getattr(user, 'assistant_name', None) if user is not None else None
    if not saved:
        return DEFAULT_ASSISTANT_NAME
    try:
        return clean_assistant_name(saved) or DEFAULT_ASSISTANT_NAME
    except ValueError:
        return DEFAULT_ASSISTANT_NAME


def assistant_initial(name: str) -> str:
    """The letter on the monogram avatar: the first character, upper case."""
    return (name or DEFAULT_ASSISTANT_NAME)[0].upper()
