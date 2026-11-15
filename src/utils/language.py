"""Language code normalization for ASR requests.

Older user records were saved when the transcription-language input was a
free-text field, so values like "français", "English", or "fr-FR" are present
in the wild. WhisperX and other ASR backends expect ISO 639-1 codes (`fr`,
`en`, ...) and reject anything else with a hard 500. This helper coerces
arbitrary input to a clean ISO code or returns None to mean "auto-detect".

Whitelist below matches the languages WhisperX accepts and the dropdown
offered in account settings (filled from static/js/asr-languages.js).
"""

# ISO 639-1 codes accepted by WhisperX. Source: faster-whisper tokenizer.
# Keep this in sync with CODES in static/js/asr-languages.js, the list the
# account page and the upload options are filled from.
SUPPORTED_CODES = frozenset({
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs",
    "ca", "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi",
    "fo", "fr", "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy",
    "id", "is", "it", "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb",
    "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt",
    "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru",
    "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
    "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi",
    "yi", "yo", "zh", "yue",
})

# Display names → ISO code. Lowercased keys; we lowercase input before lookup.
# Covers: English names, native names, common alternates.
_NAME_TO_CODE = {
    # English names
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "russian": "ru",
    "chinese": "zh", "japanese": "ja", "korean": "ko", "arabic": "ar",
    "hindi": "hi", "polish": "pl", "ukrainian": "uk", "vietnamese": "vi",
    "thai": "th", "turkish": "tr", "indonesian": "id", "swedish": "sv",
    "norwegian": "no", "danish": "da", "finnish": "fi", "czech": "cs",
    "greek": "el", "hebrew": "he", "hungarian": "hu", "romanian": "ro",
    "bulgarian": "bg", "croatian": "hr", "serbian": "sr", "slovak": "sk",
    "slovenian": "sl", "estonian": "et", "latvian": "lv", "lithuanian": "lt",
    "persian": "fa", "farsi": "fa", "urdu": "ur", "bengali": "bn",
    "tamil": "ta", "telugu": "te", "marathi": "mr", "gujarati": "gu",
    "kannada": "kn", "malayalam": "ml", "punjabi": "pa", "burmese": "my",
    "khmer": "km", "lao": "lo", "mongolian": "mn", "nepali": "ne",
    "malay": "ms", "filipino": "tl", "tagalog": "tl", "swahili": "sw",
    "afrikaans": "af", "albanian": "sq", "armenian": "hy", "azerbaijani": "az",
    "basque": "eu", "belarusian": "be", "bosnian": "bs", "catalan": "ca",
    "welsh": "cy", "galician": "gl", "georgian": "ka", "icelandic": "is",
    "kazakh": "kk", "macedonian": "mk", "maltese": "mt", "yiddish": "yi",
    "yoruba": "yo", "cantonese": "yue",
    # Native names (selected — only those known to be misstored)
    "français": "fr", "francais": "fr",
    "deutsch": "de",
    "español": "es", "espanol": "es",
    "italiano": "it",
    "português": "pt", "portugues": "pt",
    "русский": "ru",
    "中文": "zh", "汉语": "zh", "中國話": "zh", "普通话": "zh",
    "日本語": "ja",
    "한국어": "ko",
    "العربية": "ar",
    "हिन्दी": "hi",
    "polski": "pl",
    "українська": "uk",
    "tiếng việt": "vi",
    "ไทย": "th",
    "türkçe": "tr",
    "bahasa indonesia": "id",
    "svenska": "sv",
    "norsk": "no",
    "dansk": "da",
    "suomi": "fi",
    "čeština": "cs", "cestina": "cs",
    "ελληνικά": "el",
    "עברית": "he",
    "magyar": "hu",
    "română": "ro", "romana": "ro",
}


def normalize_language_code(value):
    """Coerce a user-supplied language string to an ISO 639-1 code or None.

    Empty / None / 'auto' / 'auto-detect' → None (signals auto-detect).
    Already-valid 2-letter codes pass through (lowercased).
    Locale codes like 'en-US' are stripped to 'en'.
    Display names like 'English', 'Français' map via _NAME_TO_CODE.
    Anything else returns None — better to auto-detect than crash the ASR call.
    """
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s in ("auto", "auto-detect", "auto detect"):
        return None

    # Locale codes like 'en-US', 'fr_FR' → take the language portion.
    for sep in ("-", "_"):
        if sep in s:
            head = s.split(sep, 1)[0]
            if head in SUPPORTED_CODES:
                return head
            # Fall through; might still match by display name below.

    # Already a valid 2-letter (or 3-letter for 'haw'/'yue') code.
    if s in SUPPORTED_CODES:
        return s

    # Display name lookup.
    if s in _NAME_TO_CODE:
        code = _NAME_TO_CODE[s]
        return code if code in SUPPORTED_CODES else None

    # Last resort: first 2 chars happen to be a valid code (e.g. 'english' → 'en').
    # Skip — too aggressive and would map garbage to wrong languages.
    return None


# Full output/translation target language names shared by the account preference
# and the translation menu. Each entry is (full_name, iso_code). The stored
# value is the English name, which is placed into the LLM prompt. The list
# holds every code of SUPPORTED_CODES (the shared ASR list in
# static/js/asr-languages.js) under the English name the browser shows for it,
# plus "Portuguese (Brazilian)", in alphabetical order. A name already stored
# for a user must keep its spelling. The pages label an entry with the
# `languages.<code>` locale key when the locale has one, and with the
# browser's own language name otherwise, so a new entry needs no locale key.
OUTPUT_LANGUAGES = [
    ("Afrikaans", "af"),
    ("Albanian", "sq"),
    ("Amharic", "am"),
    ("Arabic", "ar"),
    ("Armenian", "hy"),
    ("Assamese", "as"),
    ("Azerbaijani", "az"),
    ("Bangla", "bn"),
    ("Bashkir", "ba"),
    ("Basque", "eu"),
    ("Belarusian", "be"),
    ("Bosnian", "bs"),
    ("Breton", "br"),
    ("Bulgarian", "bg"),
    ("Burmese", "my"),
    ("Cantonese", "yue"),
    ("Catalan", "ca"),
    ("Chinese", "zh"),
    ("Croatian", "hr"),
    ("Czech", "cs"),
    ("Danish", "da"),
    ("Dutch", "nl"),
    ("English", "en"),
    ("Estonian", "et"),
    ("Faroese", "fo"),
    ("Filipino", "tl"),
    ("Finnish", "fi"),
    ("French", "fr"),
    ("Galician", "gl"),
    ("Georgian", "ka"),
    ("German", "de"),
    ("Greek", "el"),
    ("Gujarati", "gu"),
    ("Haitian Creole", "ht"),
    ("Hausa", "ha"),
    ("Hawaiian", "haw"),
    ("Hebrew", "he"),
    ("Hindi", "hi"),
    ("Hungarian", "hu"),
    ("Icelandic", "is"),
    ("Indonesian", "id"),
    ("Italian", "it"),
    ("Japanese", "ja"),
    ("Javanese", "jw"),
    ("Kannada", "kn"),
    ("Kazakh", "kk"),
    ("Khmer", "km"),
    ("Korean", "ko"),
    ("Lao", "lo"),
    ("Latin", "la"),
    ("Latvian", "lv"),
    ("Lingala", "ln"),
    ("Lithuanian", "lt"),
    ("Luxembourgish", "lb"),
    ("Macedonian", "mk"),
    ("Malagasy", "mg"),
    ("Malay", "ms"),
    ("Malayalam", "ml"),
    ("Maltese", "mt"),
    ("Maori", "mi"),
    ("Marathi", "mr"),
    ("Mongolian", "mn"),
    ("Nepali", "ne"),
    ("Norwegian", "no"),
    ("Norwegian Nynorsk", "nn"),
    ("Occitan", "oc"),
    ("Pashto", "ps"),
    ("Persian", "fa"),
    ("Polish", "pl"),
    ("Portuguese", "pt"),
    ("Portuguese (Brazilian)", "pt-BR"),
    ("Punjabi", "pa"),
    ("Romanian", "ro"),
    ("Russian", "ru"),
    ("Sanskrit", "sa"),
    ("Serbian", "sr"),
    ("Shona", "sn"),
    ("Sindhi", "sd"),
    ("Sinhala", "si"),
    ("Slovak", "sk"),
    ("Slovenian", "sl"),
    ("Somali", "so"),
    ("Spanish", "es"),
    ("Sundanese", "su"),
    ("Swahili", "sw"),
    ("Swedish", "sv"),
    ("Tajik", "tg"),
    ("Tamil", "ta"),
    ("Tatar", "tt"),
    ("Telugu", "te"),
    ("Thai", "th"),
    ("Tibetan", "bo"),
    ("Turkish", "tr"),
    ("Turkmen", "tk"),
    ("Ukrainian", "uk"),
    ("Urdu", "ur"),
    ("Uzbek", "uz"),
    ("Vietnamese", "vi"),
    ("Welsh", "cy"),
    ("Yiddish", "yi"),
    ("Yoruba", "yo"),
]


def output_language_name_for_code(code):
    """Map an ISO interface code to its full output-language name.

    Returns None when no matching entry exists so callers can fall back.
    """
    if not code:
        return None
    needle = str(code).strip().lower()
    for name, iso in OUTPUT_LANGUAGES:
        if iso.lower() == needle:
            return name
    return None


def canonical_translation_language(value):
    """Resolve a user-supplied target language to a canonical full name.

    Accepts either the full language name or its ISO code (as listed in
    OUTPUT_LANGUAGES). Returns None for anything not recognised. Centralised
    here so the web route and the auto-translate trigger share one definition
    instead of reaching into a private helper of the recordings blueprint.
    """
    if value is None:
        return None
    needle = str(value).strip()
    if not needle:
        return None
    lowered = needle.lower()
    for name, iso in OUTPUT_LANGUAGES:
        if name.lower() == lowered or str(iso).lower() == lowered:
            return name
    return None
