"""Matching people's names: case and accents ignored, whole name parts, and
spelling variants of transliterated names (Mohammad, Mohammed, Muhammad and
Mohamed are one name). Used by the find_people tool (inquire_agent) and by
the recording chat's participants line (recording_chat).

Deterministic and dependency-free. A variant key is built from a folded name
part in five steps:

1. ph becomes f, ou becomes u, y becomes i;
2. a trailing h is dropped (Fatimah, Fatima);
3. doubled letters are collapsed (Mohammed, Mohamed);
4. every run of vowels becomes one placeholder (Mohammad, Muhammad; Ahmed, Ahmad);
5. the result is the key.

Short name parts (under five letters) and parts with fewer than two
consonants never match by variant: "Dana" and "Dina" stay apart.
"""

import json
import re
import unicodedata

_VOWEL_RUN = re.compile(r'[aeiou]+')
_DOUBLE = re.compile(r'(.)\1+')
_WORD = re.compile(r'[^\W\d_]+')


def fold(text):
    """Lower case without accents: "Nájjar" is "najjar"."""
    decomposed = unicodedata.normalize('NFKD', str(text or ''))
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def name_tokens(text):
    """The folded name parts of a name, an alias or an email local part
    ("m.najjar" is ["m", "najjar"])."""
    return _WORD.findall(fold(text))


def variant_key(token):
    """The spelling-variant key of one folded name part, or None when the
    part is too short to compare by variant."""
    t = token.replace('ph', 'f').replace('ou', 'u').replace('y', 'i')
    if len(t) > 3 and t.endswith('h'):
        t = t[:-1]
    t = _DOUBLE.sub(r'\1', t)
    if len(token) < 5 or len(_VOWEL_RUN.sub('', t)) < 2:
        return None
    return _VOWEL_RUN.sub('a', t)


def token_match(query_token, name_token):
    """'exact', 'initial', 'variant' or None for one query part against one
    name part (both folded)."""
    if query_token == name_token:
        return 'exact'
    if len(query_token) == 1:
        return 'initial' if name_token.startswith(query_token) else None
    key = variant_key(query_token)
    if key and key == variant_key(name_token):
        return 'variant'
    return None


def name_match(query, name):
    """How a query matches a name: (matched parts, query parts, variant used)
    or None when no query part matches. Every query part counts once; initials
    alone never make a match."""
    q_tokens = name_tokens(query)
    n_tokens = name_tokens(name)
    if not q_tokens or not n_tokens:
        return None
    matched, variant, substantive = 0, False, False
    for qt in q_tokens:
        kinds = [k for k in (token_match(qt, nt) for nt in n_tokens) if k]
        if not kinds:
            continue
        matched += 1
        if 'exact' in kinds:
            substantive = True
        elif 'variant' in kinds:
            substantive = variant = True
    if not substantive:
        return None
    return matched, len(q_tokens), variant


def email_local(email):
    return (email or '').split('@', 1)[0]


def aliases_of(speaker):
    """A speaker profile's aliases as a list of strings."""
    try:
        raw = json.loads(speaker.aliases) if speaker.aliases else []
    except (TypeError, ValueError):
        return []
    return [str(a).strip() for a in raw if isinstance(a, str) and a.strip()] if isinstance(raw, list) else []


def closeness(query, name):
    """0..1: how close the variant keys of two names are, for suggestions."""
    from difflib import SequenceMatcher
    q = ' '.join(variant_key(t) or t for t in name_tokens(query))
    n = ' '.join(variant_key(t) or t for t in name_tokens(name))
    best = SequenceMatcher(None, q, n).ratio() if q and n else 0.0
    for part in n.split():
        for qpart in q.split():
            best = max(best, SequenceMatcher(None, qpart, part).ratio())
    return best
