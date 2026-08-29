/**
 * Shared ASR language list + localized dropdown options.
 *
 * Single source of truth for the Whisper language set, used by:
 *  - the Vue app's transcription-language dropdowns (app.modular.js
 *    `languageOptions`), and
 *  - the tag/folder editor modals' Default Language selects
 *    (organizer-modals.js), on both the account page and the main app.
 *
 * Labels come from Intl.DisplayNames so ~100 languages render localized
 * without translation keys; values stay the short codes the ASR backend
 * expects (en, es, zh, ...).
 */
(function () {
    'use strict';

    // The full Whisper language set (the old hardcoded 11-language list
    // locked out every other supported language, issue #359).
    const CODES = [
        'en', 'zh', 'de', 'es', 'ru', 'ko', 'fr', 'ja', 'pt', 'tr', 'pl', 'ca',
        'nl', 'ar', 'sv', 'it', 'id', 'hi', 'fi', 'vi', 'he', 'uk', 'el', 'ms',
        'cs', 'ro', 'da', 'hu', 'ta', 'no', 'th', 'ur', 'hr', 'bg', 'lt', 'la',
        'mi', 'ml', 'cy', 'sk', 'te', 'fa', 'lv', 'bn', 'sr', 'az', 'sl', 'kn',
        'et', 'mk', 'br', 'eu', 'is', 'hy', 'ne', 'mn', 'bs', 'kk', 'sq', 'sw',
        'gl', 'mr', 'pa', 'si', 'km', 'sn', 'yo', 'so', 'af', 'oc', 'ka', 'be',
        'tg', 'sd', 'gu', 'am', 'yi', 'lo', 'uz', 'fo', 'ht', 'ps', 'tk', 'nn',
        'mt', 'sa', 'lb', 'my', 'bo', 'tl', 'mg', 'as', 'tt', 'haw', 'ln', 'ha',
        'ba', 'jw', 'su', 'yue'
    ];
    // Codes Intl.DisplayNames can't resolve (or resolves under a different
    // ISO tag than Whisper uses).
    const LABEL_OVERRIDES = {
        jw: 'jv',   // Whisper uses 'jw' for Javanese (ISO is 'jv')
    };
    const FALLBACK_NAMES = {
        haw: 'Hawaiian',
        yue: 'Cantonese',
        ba: 'Bashkir',   // some ICU builds return the bare code
        bo: 'Tibetan',
    };

    /** Localized {value, label} options, sorted by label. No empty entry. */
    function buildOptions(locale) {
        const uiLocale = locale || (window.i18n && window.i18n.currentLocale) || 'en';
        let displayNames = null;
        try {
            displayNames = new Intl.DisplayNames([uiLocale, 'en'], { type: 'language' });
        } catch (e) { /* very old browsers: fall back to raw codes */ }
        const options = CODES.map(code => {
            const lookup = LABEL_OVERRIDES[code] || code;
            let label = null;
            if (displayNames) {
                try {
                    const name = displayNames.of(lookup);
                    // DisplayNames echoes back unknown codes; treat that as a miss
                    if (name && name !== lookup) label = name;
                } catch (e) { /* invalid tag for this engine */ }
            }
            if (!label) label = FALLBACK_NAMES[code] || code;
            label = label.charAt(0).toLocaleUpperCase(uiLocale) + label.slice(1);
            return { value: code, label };
        });
        options.sort((a, b) => a.label.localeCompare(b.label, uiLocale));
        return options;
    }

    /**
     * Fill a <select> with the language options, preserving its current
     * value across repopulation (locale changes). The first entry is an
     * empty-value option labeled emptyLabel.
     */
    function populateSelect(select, emptyLabel) {
        if (!select) return;
        const previous = select.value;
        select.textContent = '';
        const emptyOpt = document.createElement('option');
        emptyOpt.value = '';
        emptyOpt.textContent = emptyLabel || 'Auto detect';
        select.appendChild(emptyOpt);
        for (const { value, label } of buildOptions()) {
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = label + ' (' + value + ')';
            select.appendChild(opt);
        }
        if (previous) setValue(select, previous);
    }

    /**
     * Select a stored code, appending it as a raw option if it isn't in the
     * list (legacy free-text values keep round-tripping instead of being
     * silently dropped on the next save).
     */
    function setValue(select, value) {
        if (!select) return;
        value = value || '';
        if (value && ![...select.options].some(o => o.value === value)) {
            const opt = document.createElement('option');
            opt.value = value;
            opt.textContent = value;
            select.appendChild(opt);
        }
        select.value = value;
    }

    window.SpeakrASRLanguages = { CODES, buildOptions, populateSelect, setValue };
})();
