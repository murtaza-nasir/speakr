/**
 * Pure helpers behind the Identify Speakers modal (#395).
 *
 * Everything here works on plain data so it can be unit-tested without Vue
 * or a browser. The composable (composables/speaker-modal.js) owns the
 * reactive state and calls these.
 */

/** Minimum speaking time for a speaker to count as a main participant. */
export const MINOR_SPEAKER_SECONDS = 30;

/**
 * Parse a recording's stored transcription for the modal.
 *
 * Returns { isJson, segments, plainText }. Each segment carries its index in
 * the stored array (`index`), which every edit must use.
 */
export function parseModalSegments(transcription) {
    if (!transcription) return { isJson: false, segments: [], plainText: '' };
    let data = null;
    try {
        data = JSON.parse(transcription);
    } catch (e) {
        data = null;
    }
    if (!Array.isArray(data)) {
        return { isJson: false, segments: [], plainText: String(transcription) };
    }
    const segments = data.map((seg, index) => {
        const start = seg.start_time ?? seg.startTime;
        const end = seg.end_time ?? seg.endTime;
        return {
            index,
            speakerId: seg.speaker || '',
            sentence: seg.sentence || '',
            start: typeof start === 'number' && isFinite(start) ? start : null,
            end: typeof end === 'number' && isFinite(end) ? end : null,
        };
    });
    return { isJson: true, segments, plainText: '' };
}

/**
 * Per-speaker totals.
 *
 * Returns { hasTimes, totalSeconds, bySpeaker } where bySpeaker maps a
 * speaker id to { seconds, share, turns, segments, firstIndex, longestIndex }.
 * A turn is a run of consecutive segments by the same speaker.
 * hasTimes is false when any segment lacks usable times; seconds and share
 * are then 0 and callers fall back to appearance order.
 */
export function computeSpeakerStats(segments) {
    const hasTimes = segments.length > 0
        && segments.every(s => s.start !== null && s.end !== null);
    const bySpeaker = {};
    let totalSeconds = 0;
    let previous = null;
    for (const seg of segments) {
        const id = seg.speakerId;
        if (!id) { previous = null; continue; }
        let agg = bySpeaker[id];
        if (!agg) {
            agg = { seconds: 0, share: 0, turns: 0, segments: 0, firstIndex: seg.index,
                    longestIndex: seg.index, longestSeconds: -1 };
            bySpeaker[id] = agg;
        }
        agg.segments += 1;
        if (previous !== id) agg.turns += 1;
        previous = id;
        if (hasTimes) {
            const dur = Math.max(0, seg.end - seg.start);
            agg.seconds += dur;
            totalSeconds += dur;
            if (dur > agg.longestSeconds) {
                agg.longestSeconds = dur;
                agg.longestIndex = seg.index;
            }
        }
    }
    for (const agg of Object.values(bySpeaker)) {
        agg.share = hasTimes && totalSeconds > 0 ? Math.round((agg.seconds / totalSeconds) * 100) : 0;
        delete agg.longestSeconds;
    }
    return { hasTimes, totalSeconds, bySpeaker };
}

/**
 * Order speaker ids for the list.
 *
 * mode: 'appearance' (first segment), 'duration' (most speaking time first)
 * or 'name' (typed name, then label). Ties keep appearance order, and
 * 'duration' without times falls back to appearance.
 */
export function orderSpeakers(ids, stats, mode, nameOf = () => '') {
    const first = (id) => stats.bySpeaker[id]?.firstIndex ?? Number.MAX_SAFE_INTEGER;
    const byAppearance = (a, b) => first(a) - first(b);
    const sorted = [...ids];
    if (mode === 'duration' && stats.hasTimes) {
        const secs = (id) => stats.bySpeaker[id]?.seconds ?? 0;
        sorted.sort((a, b) => (secs(b) - secs(a)) || byAppearance(a, b));
    } else if (mode === 'name') {
        const key = (id) => (nameOf(id) || '').trim().toLocaleLowerCase() || `￿${id.toLocaleLowerCase()}`;
        sorted.sort((a, b) => key(a).localeCompare(key(b)) || byAppearance(a, b));
    } else {
        sorted.sort(byAppearance);
    }
    return sorted;
}

/**
 * Split ordered ids into main and minor speakers.
 *
 * A minor speaker spoke for less than `threshold` seconds in total. Speakers
 * that already have a name, and speakers added by hand (no segments yet),
 * always stay in the main list. Without times, or when the split would leave
 * fewer than two main speakers, nothing is split out.
 */
export function splitMinorSpeakers(orderedIds, stats, threshold = MINOR_SPEAKER_SECONDS, hasName = () => false) {
    if (!stats.hasTimes) return { main: [...orderedIds], minor: [] };
    const main = [];
    const minor = [];
    for (const id of orderedIds) {
        const agg = stats.bySpeaker[id];
        if (agg && agg.seconds < threshold && !hasName(id)) minor.push(id);
        else main.push(id);
    }
    if (main.length < 2 || minor.length === 0) return { main: [...orderedIds], minor: [] };
    return { main, minor };
}

const norm = (s) => (s || '').trim().replace(/\s+/g, ' ').toLocaleLowerCase();

/**
 * Options for a speaker's name field.
 *
 * voice: voice-match suggestions for this label, [{ name, similarity }].
 * saved: the user's saved speakers from /speakers/search, [{ name, use_count }].
 * query: the text typed so far (filters both lists by substring).
 * takenNames: names already given to OTHER labels in this modal; they sink
 *   below the rest (assigning one is a deliberate merge, so they stay offered).
 *
 * Voice matches come first, duplicates are dropped ignoring case.
 */
export function buildNameOptions({ voice = [], saved = [], query = '', takenNames = [], limit = 8 }) {
    const q = norm(query);
    const taken = new Set(takenNames.map(norm));
    const seen = new Set();
    const out = [];
    const push = (opt) => {
        const key = norm(opt.name);
        if (!key || seen.has(key)) return;
        if (q && !key.includes(q)) return;
        seen.add(key);
        out.push({ ...opt, taken: taken.has(key) });
    };
    for (const v of voice) push({ name: v.name, source: 'voice', similarity: v.similarity });
    for (const s of saved) push({ name: s.name, source: 'saved', useCount: s.use_count ?? s.useCount ?? 0 });
    const free = out.filter(o => !o.taken);
    const used = out.filter(o => o.taken);
    return [...free, ...used].slice(0, limit);
}

/**
 * The saved spelling of `name` when it differs only in case or spacing,
 * otherwise null. The server applies the saved spelling on save; the modal
 * shows it first so the user is not surprised.
 */
export function findSavedSpelling(name, savedNames) {
    const key = norm(name);
    if (!key) return null;
    for (const saved of savedNames) {
        if (norm(saved) === key && saved !== name.trim()) return saved;
    }
    return null;
}

/**
 * Index into `segments` of the segment playing at `time`, or -1.
 * Segments are in time order; uses a binary search on start times.
 */
export function playingSegmentPosition(segments, time) {
    if (!segments.length || time === null || time === undefined || !isFinite(time)) return -1;
    let lo = 0;
    let hi = segments.length - 1;
    let found = -1;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const start = segments[mid].start;
        if (start === null) return -1;
        if (start <= time) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    if (found === -1) return -1;
    const seg = segments[found];
    return seg.end !== null && time <= seg.end + 0.25 ? found : -1;
}

/**
 * Turn groups for one speaker: positions (into `segments`) where a run of
 * that speaker starts. Used for Previous / Next when the list is unfiltered.
 */
export function turnStarts(segments, speakerId) {
    const starts = [];
    let previous = null;
    segments.forEach((seg, pos) => {
        if (seg.speakerId === speakerId && previous !== speakerId) starts.push(pos);
        previous = seg.speakerId;
    });
    return starts;
}

/** m:ss or h:mm:ss for a number of seconds. */
export function formatClock(seconds) {
    if (seconds === null || seconds === undefined || !isFinite(seconds) || seconds < 0) return '0:00';
    const total = Math.floor(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const ss = String(s).padStart(2, '0');
    return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

/**
 * Reassign every segment of `sourceIds` to `targetId` in a stored
 * transcription array (not mutated). Returns the new array and how many
 * segments moved.
 */
export function mergeSpeakerSegments(transcriptionData, sourceIds, targetId) {
    const sources = new Set(sourceIds.filter(id => id && id !== targetId));
    let moved = 0;
    const data = transcriptionData.map(seg => {
        if (sources.has(seg.speaker)) {
            moved += 1;
            return { ...seg, speaker: targetId };
        }
        return seg;
    });
    return { data, moved };
}
