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

Ingestion. Transcription services number voices in different ways (``A``,
``S01``, ``speaker_0``, ``SPEAKER_0``). The result of a service is brought to
the one form ``SPEAKER_00``, ``SPEAKER_01`` before it is stored; see
diarization_label_map() below. That mapping is for the output of a service
only. It is never applied to a stored transcript or to text a person typed,
where ``A`` may be a name.
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


# ---------------------------------------------------------------------------
# Ingestion: the labels of a transcription service in the form SPEAKER_NN
# ---------------------------------------------------------------------------

_CANONICAL = re.compile(r"^SPEAKER_([0-9]{2,})$")
_TRAILING_NUMBER = re.compile(r"^[^0-9]*?([0-9]{1,4})$")
_ONE_LETTER = re.compile(r"^(?:speaker[ _\-]+)?([a-z])$")
_TWO_CAPITALS = re.compile(r"^[A-Z]{2}$")


def raw_label_scheme(label):
    """(scheme, number) of a label as a transcription service returned it.

    scheme is one of:

    - ``'canonical'``: ``SPEAKER_00`` exactly (upper case, two digits or
      more). number is its number.
    - ``'letter'``: one letter in either case (``A``), ``Speaker A``, or two
      capitals (``AA``). number is the position of the letter: A is 0, Z is
      25, AA is 26, AB is 27.
    - ``'number'``: a number alone, as text or as an integer (``0``, ``12``).
    - ``'generic_number'``: a label of the rule above that ends in a number
      and is not canonical (``SPEAKER_0``, ``speaker_1``, ``Speaker 2``).
    - ``'prefixed_number'``: any other text that ends in a number of one to
      four digits and holds no other digit (``S01``, ``voice-3``).
    - ``None``: everything else. number is None. This covers an empty value,
      the unknown family (``Unknown``, ``UNKNOWN_SPEAKER``), a bare
      ``speaker`` and text without a number (``Dana``, ``Interviewer``).

    For 'number', 'generic_number' and 'prefixed_number', number is the
    number the label carries, unchanged (``S01`` carries 1).
    """
    if isinstance(label, bool):
        return None, None
    if isinstance(label, int):
        return ('number', label) if 0 <= label <= 9999 else (None, None)
    if not isinstance(label, str):
        return None, None
    match = _CANONICAL.match(label)
    if match:
        return 'canonical', int(match.group(1))
    text = normalize_label(label)
    if not text:
        return None, None
    match = _ONE_LETTER.match(text)
    if match:
        return 'letter', ord(match.group(1)) - ord('a')
    stripped = unicodedata.normalize("NFKC", label).strip()
    if _TWO_CAPITALS.match(stripped):
        return 'letter', 26 * (ord(stripped[0]) - ord('A') + 1) + ord(stripped[1]) - ord('A')
    if _UNKNOWN.match(text):
        return None, None
    match = _TRAILING_NUMBER.match(text)
    if match:
        number = int(match.group(1))
        if text.isdigit():
            return 'number', number
        return ('generic_number' if is_generic_speaker_label(label) else 'prefixed_number'), number
    return None, None


def diarization_label_map(labels):
    """{raw label: SPEAKER_NN} for the labels of one transcription result.

    `labels` are the speaker values of one result in order of first
    appearance, as the service returned them (repeats, None and empty values
    are allowed). The returned dict holds only the labels that change; a
    label that is not a key stays as it is.

    The mapping (schemes as in raw_label_scheme()):

    - A canonical label (``SPEAKER_00``) is never changed, and its number is
      not given to another label.
    - A letter takes the number of its position: ``A`` becomes
      ``SPEAKER_00``, ``B`` ``SPEAKER_01``, ``AA`` ``SPEAKER_26``. The result
      does not depend on the order of the turns.
    - A label that carries a number keeps that number: ``0`` and
      ``SPEAKER_0`` become ``SPEAKER_00``; ``S01``, ``speaker_1`` and
      ``Speaker 1`` become ``SPEAKER_01``. The number is not shifted, so a
      service that counts from 1 has no ``SPEAKER_00``.
    - When two labels of one result ask for the same number (``A`` and ``0``,
      or ``S01`` beside an existing ``SPEAKER_01``), the label that appears
      first keeps it. Each of the others receives the lowest number that no
      label of the result holds or asked for, in order of first appearance.
    - Every other label is left unchanged: an empty value, the unknown
      family (``Unknown``, ``UNKNOWN_SPEAKER``; no voice was identified, so
      no number is claimed) and text without a number or letter position
      (``Dana``), which is taken for a name the service supplied.

    The mapping is one to one, and the same labels in the same order always
    give the same result.

    Only the output of a transcription service is passed to this function,
    before a person has seen it. A stored transcript is never passed: there a
    single letter or a number may be a name somebody typed.
    """
    seen = []
    for label in labels or []:
        if isinstance(label, (str, int)) and not isinstance(label, bool) and label not in seen:
            seen.append(label)

    schemes = {label: raw_label_scheme(label) for label in seen}
    taken = {number for scheme, number in schemes.values() if scheme == 'canonical'}
    mapping, waiting = {}, []
    for label in seen:
        scheme, number = schemes[label]
        if scheme in (None, 'canonical'):
            continue
        if number in taken:
            waiting.append(label)
        else:
            taken.add(number)
            mapping[label] = number
    wanted = taken | {schemes[label][1] for label in waiting}
    free = 0
    for label in waiting:
        while free in wanted:
            free += 1
        mapping[label] = free
        wanted.add(free)
    return {label: f"SPEAKER_{number:02d}" for label, number in mapping.items()}
