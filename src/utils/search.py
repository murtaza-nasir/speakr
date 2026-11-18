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


def transcript_patterns(term):
    """Patterns that find `term` in a stored transcript, old rows included (#622).

    JSON transcripts saved before #622 used json.dumps with ensure_ascii, so
    "español" is stored as "espa\\u00f1ol"; quotes and backslashes are
    escaped in every JSON transcript. The term is searched as typed and in
    both JSON-escaped forms. ILIKE folds only ASCII letters on SQLite, and
    inside a \\u escape on any backend, so the lowercased term is searched
    too when it differs beyond ASCII case. Escaped rows are rewritten once
    at startup (src/services/transcript_unicode_rewrite.py); the escaped
    forms stay here for rows restored from a backup taken before it ran.
    """
    import json
    texts, seen = [], set()
    for form in (term, term.lower()):
        for text in (form, json.dumps(form)[1:-1], json.dumps(form, ensure_ascii=False)[1:-1]):
            key = text.encode().lower()  # bytes.lower() folds ASCII only, as ILIKE does
            if key not in seen:
                seen.add(key)
                texts.append(text)
    return [contains_pattern(text) for text in texts]


def transcript_contains(column, term):
    """ILIKE condition: `column` (a transcript) contains `term`; see transcript_patterns."""
    from sqlalchemy import or_
    return or_(*[column.ilike(pattern, escape=LIKE_ESCAPE) for pattern in transcript_patterns(term)])
