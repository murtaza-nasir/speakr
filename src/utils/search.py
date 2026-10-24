"""Literal LIKE / ILIKE patterns for text typed by a user (#485).

'%' and '_' are wildcards in LIKE, and PostgreSQL and MySQL also read a
backslash as an escape by default, so text from a search box has to be
escaped before it goes into a pattern. Every caller passes
escape=LIKE_ESCAPE: the escape character is only honoured when the
statement declares it (SQLite has no default one).
"""

LIKE_ESCAPE = '\\'


def escape_like(term):
    """`term` with the LIKE metacharacters escaped, so it matches literally.

    The backslash goes first; escaping it after '%' and '_' would double the
    escapes just added.
    """
    return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def contains_pattern(term):
    """Pattern that matches values containing `term` literally."""
    return f'%{escape_like(term)}%'
