"""Generic diarization labels: the one rule that tells them from names.

A diarizer does not know who is speaking. It numbers the voices it hears, and
the transcript shows those numbers until a person gives a name: SPEAKER_00,
SPEAKER_01 and so on. Such a label describes one recording only (SPEAKER_00 of
one recording and SPEAKER_00 of the next are unrelated voices), so it is never
stored as a saved speaker, never given a voice sample or a snippet, never
listed as a participant or offered as a filter. In the transcript itself it
stays: it is how an unnamed turn is shown.

The rule. The text is normalized first (Unicode NFKC, surrounding space
removed, inner whitespace collapsed to one space, lowercased). A label is
generic when the normalized text is one of:

1. A speaker word, an optional separator (space, ``_``, ``-``, ``#``) and a
   number of one to four digits: ``SPEAKER_00``, ``SPEAKER_7``, ``speaker 1``,
   ``Speaker-2``, ``Speaker1``, ``Speaker #3``, ``spk_0``, ``spkr1``. The
   speaker words are ``speaker``, ``spk``, ``spkr`` and the words the segment
   editor uses for its default name in each shipped language (``Sprecher 1``,
   ``Hablante 1``, ``Orateur 1``, ``Orador 1``, ``Спикер 1``, ``发言人 1``).
2. ``speaker``, a separator and one letter: ``Speaker A``, ``SPEAKER_B``.
3. ``unknown`` or ``unidentified``, optionally followed by ``speaker`` and
   optionally by a number: ``Unknown``, ``Unknown Speaker``,
   ``UNKNOWN_SPEAKER``, ``unknown 2``, ``Unidentified speaker 1``.
4. The bare words ``speaker`` and ``spk``.

Everything else is a name, including a single letter (``A``), a number alone,
a role (``Guest 1``, ``Host``, ``Keynote Speaker``) and any name that merely
contains one of the words (``Speaker Johnson``, ``Unknown Mortal``). An empty
value is not a label; callers treat it as no name at all.

The same rule is implemented in static/js/modules/utils/speaker-labels.js.
Both are tested against tests/fixtures/speaker_label_cases.json: a change to
the rule is made in all three files.
"""

import re
import unicodedata

# The English diarizer words, then transcription.speaker of every locale
# (tests/test_generic_speaker_labels.py checks the locale files against this).
SPEAKER_WORDS = ("speaker", "spk", "spkr", "sprecher", "hablante", "orateur", "orador", "спикер", "发言人")

_WORDS = "|".join(SPEAKER_WORDS)
_NUMBERED = re.compile(rf"^(?:{_WORDS})[ _\-#]*[0-9]{{1,4}}$")
_LETTERED = re.compile(r"^speaker[ _\-]+[a-z]$")
_UNKNOWN = re.compile(r"^(?:unknown|unidentified)(?:[ _\-]*speaker)?(?:[ _\-#]*[0-9]{1,4})?$")
_BARE = frozenset(("speaker", "spk"))


def normalize_label(name):
    """The form the rule is applied to (see the module docstring)."""
    if not isinstance(name, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", name).split()).lower()


def is_generic_speaker_label(name):
    """True when `name` is a diarization label, False for a name a person gave.

    False for None, a non-string and an empty value.
    """
    text = normalize_label(name)
    if not text:
        return False
    return bool(text in _BARE or _NUMBERED.match(text) or _LETTERED.match(text) or _UNKNOWN.match(text))


def is_real_speaker_name(name):
    """True for a non-empty name that is not a diarization label."""
    return isinstance(name, str) and bool(name.strip()) and not is_generic_speaker_label(name)


def real_speaker_names(names):
    """The entries of `names` that are real names, in order, stripped."""
    if isinstance(names, str):
        names = [names]
    return [n.strip() for n in (names or []) if is_real_speaker_name(n)]


def clean_participants(text):
    """A participants field without diarization labels.

    Participants are comma-separated. The text is returned unchanged when it
    holds no label, so a field a person typed keeps its spacing. None stays
    None; a field that held only labels becomes None.
    """
    if not isinstance(text, str) or not text.strip():
        return text
    parts = [p.strip() for p in text.split(",")]
    if not any(is_generic_speaker_label(p) for p in parts):
        return text
    return ", ".join(p for p in parts if p and not is_generic_speaker_label(p)) or None
