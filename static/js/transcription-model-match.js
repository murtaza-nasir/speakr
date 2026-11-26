/**
 * The saved default transcription model of a tag or folder, matched against
 * the models a user may pick. A classic script: it sets
 * window.SpeakrTranscriptionModelMatch.
 *
 * The rule is the server's (asr_config.is_selectable): a visible profile id,
 * the environment id, or a plain model name equal to the model of one of
 * those. Tags and folders saved before the Transcription models section
 * hold plain model names, so such a value is shown as the matching profile,
 * or as the environment, and never as "no longer available".
 */
(function (root) {
    'use strict';

    function fold(value) {
        return String(value === null || value === undefined ? '' : value).trim().toLowerCase();
    }

    /**
     * { value, label, stale, synthetic } for a saved value.
     *
     * value is what the select shows: the saved value, or the id of the
     * profile whose model it names. synthetic means the select needs an
     * option of its own for the value (the environment, or any value while
     * no profile is configured); stale means no model matches.
     */
    function matchSavedModel(saved, options, environment) {
        const target = String(saved === null || saved === undefined ? '' : saved).trim();
        const list = Array.isArray(options) ? options : [];
        if (!target) return { value: '', label: '', stale: false, synthetic: false };
        const exact = list.find((option) => option && option.value === target);
        if (exact) return { value: exact.value, label: exact.label || exact.value, stale: false, synthetic: false };
        if (!list.length) return { value: target, label: target, stale: false, synthetic: true };
        const key = fold(target);
        const byModel = list.find((option) => option && fold(option.model) !== '' && fold(option.model) === key);
        if (byModel) return { value: byModel.value, label: byModel.label || byModel.value, stale: false, synthetic: false };
        if (environment && (fold(environment.value) === key
                || (fold(environment.model) !== '' && fold(environment.model) === key))) {
            return { value: target, label: environment.label || target, stale: false, synthetic: true };
        }
        return { value: target, label: target, stale: true, synthetic: false };
    }

    root.SpeakrTranscriptionModelMatch = { matchSavedModel };
})(typeof window !== 'undefined' ? window : globalThis);
