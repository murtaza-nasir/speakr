/**
 * Generic diarization labels: the one rule that tells them from names.
 *
 * A diarizer numbers the voices of one recording (SPEAKER_00, SPEAKER_01).
 * Such a label is never a saved speaker, a participant or a filter value; in
 * the transcript it stays, as the way an unnamed turn is shown.
 *
 * The rule is the one of src/utils/speaker_labels.py (the full description
 * is there). The text is normalized (Unicode NFKC, trimmed, inner whitespace
 * collapsed, lowercased) and is generic when it is:
 *
 * 1. a speaker word, an optional separator (space, _, -, #) and one to four
 *    digits: SPEAKER_00, speaker 1, Speaker-2, spk_0, Sprecher 1, 发言人 1;
 * 2. "speaker", a separator and one letter: Speaker A, SPEAKER_B;
 * 3. "unknown" or "unidentified", optionally followed by "speaker" and by a
 *    number: Unknown, UNKNOWN_SPEAKER, Unknown Speaker 2;
 * 4. the bare word "speaker" or "spk".
 *
 * Everything else is a name: a single letter, a number alone, a role
 * (Guest 1), a name that contains one of the words (Speaker Johnson). An
 * empty value is not a label.
 *
 * Both implementations are tested against
 * tests/fixtures/speaker_label_cases.json.
 */

export const SPEAKER_WORDS = ['speaker', 'spk', 'spkr', 'sprecher', 'hablante', 'orateur', 'orador', 'спикер', '发言人'];

const NUMBERED = new RegExp(`^(?:${SPEAKER_WORDS.join('|')})[ _\\-#]*[0-9]{1,4}$`);
const LETTERED = /^speaker[ _\-]+[a-z]$/;
const UNKNOWN = /^(?:unknown|unidentified)(?:[ _\-]*speaker)?(?:[ _\-#]*[0-9]{1,4})?$/;
const BARE = new Set(['speaker', 'spk']);

/** The form the rule is applied to. */
export function normalizeLabel(name) {
    if (typeof name !== 'string') return '';
    return name.normalize('NFKC').split(/\s+/).filter(Boolean).join(' ').toLowerCase();
}

/** True when `name` is a diarization label, false for a name a person gave. */
export function isGenericSpeakerLabel(name) {
    const text = normalizeLabel(name);
    if (!text) return false;
    return BARE.has(text) || NUMBERED.test(text) || LETTERED.test(text) || UNKNOWN.test(text);
}

/** True for a non-empty name that is not a diarization label. */
export function isRealSpeakerName(name) {
    return typeof name === 'string' && name.trim() !== '' && !isGenericSpeakerLabel(name);
}

/** The saved speakers ([{ name }]) that carry a real name. */
export function realSpeakers(speakers) {
    return (Array.isArray(speakers) ? speakers : []).filter((speaker) => speaker && isRealSpeakerName(speaker.name));
}
