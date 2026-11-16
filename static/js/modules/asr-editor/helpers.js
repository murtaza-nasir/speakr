/**
 * Pure helpers of the segment transcript editor: times as text, speaker
 * picker options, voice-match suggestions and keyboard checks.
 */

import { isGenericSpeakerLabel } from '../utils/speaker-labels.js';

const PLACEHOLDER = /^SPEAKER_\d+$/i;

/** True for a diarization label (SPEAKER_00), which stands for an unnamed speaker. */
export const isPlaceholderSpeaker = (name) => PLACEHOLDER.test(String(name || '').trim());

const pad2 = (n) => String(n).padStart(2, '0');

/**
 * The label of a speaker added to a recording: SPEAKER_NN with the number
 * after the highest one among `names` (the speakers of the recording) and
 * `reserved` (labels that must not be taken although no speaker has them
 * now). Without any SPEAKER_NN it is the number of speakers, as the labels
 * count from 00: beside Alice and Bob the third speaker is SPEAKER_02. A
 * name that exists in any case is never returned.
 */
export function nextSpeakerLabel(names = [], reserved = []) {
    const own = [...new Set((names || []).map((name) => String(name || '').trim()).filter(Boolean))];
    const all = own.concat((reserved || []).map((name) => String(name || '').trim()).filter(Boolean));
    const taken = new Set(all.map((name) => name.toLowerCase()));
    let highest = -1;
    for (const name of all) {
        const found = /^SPEAKER_(\d+)$/i.exec(name);
        if (found) highest = Math.max(highest, Number(found[1]));
    }
    let number = highest >= 0 ? highest + 1 : own.length;
    while (taken.has(`speaker_${pad2(number)}`)) number++;
    return `SPEAKER_${pad2(number)}`;
}

/** Compact time for a row: 0:38, 12:05, 1:02:03. */
export function formatClock(seconds) {
    if (!Number.isFinite(seconds)) return '–:––';
    const total = Math.max(0, Math.floor(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
}

/** Exact time for titles and the time fields: 0:38.250, 1:02:03.000. */
export function formatExact(seconds) {
    if (!Number.isFinite(seconds)) return '';
    const ms = Math.max(0, Math.round(seconds * 1000));
    const frac = String(ms % 1000).padStart(3, '0');
    return `${formatClock(Math.floor(ms / 1000))}.${frac}`;
}

/**
 * Seconds from typed text: "38", "38.5", "0:38.25", "1:02:03", with a comma
 * or a point as the decimal mark. Null when it is not a time.
 */
export function parseTime(text) {
    const value = String(text == null ? '' : text).trim().replace(',', '.').replace(/s$/i, '');
    if (!value || !/^\d+(?::\d{1,2}){0,2}(?:\.\d*)?$/.test(value)) return null;
    const parts = value.split(':').map(Number);
    if (parts.some((p) => !Number.isFinite(p))) return null;
    if (parts.length > 1 && parts.slice(1).some((p) => p >= 60)) return null;
    const seconds = parts.reduce((total, part) => total * 60 + part, 0);
    return Math.round(seconds * 1000) / 1000;
}

const lower = (s) => String(s || '').trim().toLowerCase();

/**
 * Options of the speaker picker, in order: the speakers of this recording
 * (the first nine carry their number key), the user's saved speakers (those
 * matched by voice first, best match first), then the typed name when it is
 * a new one. A saved speaker or voice match whose name is a diarization
 * label (SPEAKER_00, "Speaker 1") is never offered: such a label is not a
 * person. The speakers of the recording are listed as they are.
 *
 * recording: [name]; saved: [{ id, name }]; voice: [{ speaker_id, name, similarity }].
 */
export function buildSpeakerOptions({ recording = [], saved = [], voice = [], query = '' } = {}) {
    const q = lower(query);
    const fits = (name) => !q || lower(name).includes(q);
    const options = [];
    const taken = new Set();

    recording.forEach((name, i) => {
        taken.add(lower(name));
        if (fits(name)) options.push({ kind: 'recording', name, key: i < 9 ? i + 1 : null });
    });

    const byVoice = [...voice].filter((v) => v && v.name && !isGenericSpeakerLabel(v.name)).sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    for (const match of byVoice) {
        if (taken.has(lower(match.name))) continue;
        taken.add(lower(match.name));
        if (fits(match.name)) options.push({ kind: 'voice', name: match.name, id: match.speaker_id, similarity: match.similarity });
    }
    for (const speaker of saved) {
        if (!speaker || !speaker.name || isGenericSpeakerLabel(speaker.name) || taken.has(lower(speaker.name))) continue;
        taken.add(lower(speaker.name));
        if (fits(speaker.name)) options.push({ kind: 'saved', name: speaker.name, id: speaker.id });
    }

    const typed = String(query || '').trim();
    if (typed && !taken.has(lower(typed))) options.push({ kind: 'new', name: typed });
    return options;
}

/**
 * The best voice match for an unnamed speaker of the recording, or null.
 * `suggestions` is what /speakers/suggestions/<id> returns per label. A
 * match is not offered for a named speaker.
 *
 * When a speaker of the recording already has the matched name (the
 * diarization split one person into two labels), the match is still offered,
 * as a merge: `merge` is true and `same` lists that speaker's name as the
 * recording spells it (more than one when the spellings differ in case).
 */
export function bestSuggestion(suggestions, label, recordingSpeakers = []) {
    if (!isPlaceholderSpeaker(label)) return null;
    const best = ((suggestions && suggestions[label]) || [])[0];
    if (!best || !best.name) return null;
    const same = recordingSpeakers.filter((name) => name !== label && lower(name) === lower(best.name));
    return { ...best, merge: same.length > 0, same };
}

/**
 * A few of the longest segments of one speaker, in time order, each cut to
 * `maxSeconds`: what is played to check a voice match.
 */
export function longestSegments(segments, speaker, { count = 3, maxSeconds = 4 } = {}) {
    const own = [];
    for (const seg of segments) {
        if (seg.speaker !== speaker) continue;
        if (!Number.isFinite(seg.start_time) || !Number.isFinite(seg.end_time)) continue;
        const length = seg.end_time - seg.start_time;
        if (length > 0.2) own.push({ start: seg.start_time, end: seg.end_time, length });
    }
    own.sort((a, b) => b.length - a.length);
    return own.slice(0, count)
        .sort((a, b) => a.start - b.start)
        .map((s) => ({ start: s.start, end: Math.min(s.end, s.start + maxSeconds) }));
}

/** The colour class of a speaker, assigned as the transcript view does it. */
export function speakerColorClass(map, name, colorCount = 16) {
    if (!name) return 'speaker-color-1';
    if (!map[name]) map[name] = `speaker-color-${(Object.keys(map).length % colorCount) + 1}`;
    return map[name];
}

/** True when keys typed at this element are text input. */
export function isTypingTarget(el) {
    if (!el || !el.tagName) return false;
    const tag = el.tagName.toUpperCase();
    if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (tag === 'INPUT') {
        const type = String(el.type || 'text').toLowerCase();
        return !['button', 'checkbox', 'radio', 'range', 'submit', 'reset', 'file', 'color'].includes(type);
    }
    return !!el.isContentEditable;
}

/**
 * The selection after a click on row `index`: a plain click selects the row,
 * Ctrl or Cmd toggles it, Shift selects the range from the anchor.
 * `ids` is the list of row ids in order. Returns { selected: Set, anchor }.
 */
export function nextSelection(ids, selected, anchor, index, { shift = false, toggle = false } = {}) {
    const id = ids[index];
    if (id === undefined) return { selected, anchor };
    if (shift) {
        const from = anchor == null ? -1 : ids.indexOf(anchor);
        if (from >= 0) {
            const lo = Math.min(from, index);
            const hi = Math.max(from, index);
            const range = new Set(toggle ? selected : []);
            for (let i = lo; i <= hi; i++) range.add(ids[i]);
            return { selected: range, anchor };
        }
    }
    if (toggle) {
        const next = new Set(selected);
        if (next.has(id)) next.delete(id); else next.add(id);
        return { selected: next, anchor: id };
    }
    return { selected: new Set([id]), anchor: id };
}
