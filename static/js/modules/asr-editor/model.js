/**
 * Editing model of the segment transcript editor.
 *
 * Pure: no Vue and no DOM. Segments are plain objects in one plain array,
 * kept sorted by start time. Every edit is a short list of primitive steps
 * (set a field, insert, delete, move), each of which is undone by running it
 * backwards, so the undo log holds operations and never copies of the
 * transcript. The log is also what the local draft stores.
 *
 * A segment is { id, speaker, sid, sentence, start_time, end_time, x }:
 * `id` is a number that stays with the segment for the editing session,
 * `sid` is the saved speaker the name is linked to (speaker_id in the stored
 * transcript) and `x` holds any other stored field unchanged.
 */

export const PROBLEM = { OVERLAP: 1, DURATION: 2, ORDER: 4 };

const EPS = 0.0005;          // times closer than this are the same time
const TOUCH = 0.011;         // edges closer than this touch
const SCAN_LIMIT = 64;       // how far the same-speaker neighbour is looked for
const FULL_RECHECK = 256;    // edits touching more segments recheck everything
const BURST_MS = 1000;       // typing pauses longer than this start a new undo step

const fin = Number.isFinite;
const round3 = (t) => Math.round(t * 1000) / 1000;

function toNumber(value) {
    if (typeof value === 'number') return fin(value) ? value : null;
    if (typeof value === 'string' && value.trim() !== '' && fin(Number(value))) return Number(value);
    return null;
}

function toSegment(raw, id) {
    const { speaker, sentence, start_time, end_time, speaker_id, ...rest } = raw || {};
    return {
        id,
        speaker: speaker == null ? '' : String(speaker),
        sid: speaker_id == null ? undefined : speaker_id,
        sentence: sentence == null ? '' : String(sentence),
        start_time: toNumber(start_time),
        end_time: toNumber(end_time),
        x: Object.keys(rest).length ? rest : null,
    };
}

/** The stored form of a segment. */
export function segmentToJSON(seg) {
    const out = { speaker: seg.speaker, sentence: seg.sentence, start_time: seg.start_time, end_time: seg.end_time };
    if (seg.sid != null) out.speaker_id = seg.sid;
    return seg.x ? Object.assign(out, seg.x) : out;
}

/**
 * Time at which a segment is cut when its text is split at `charOffset`.
 *
 * In order: the given time when it lies inside the segment (the playhead),
 * the start of the first word at or after the offset when word timings are
 * supplied ([{ start, end, word }], in text order), and otherwise the same
 * proportion of the duration as of the characters (between 5% and 95%).
 */
export function splitTime(seg, charOffset, { time, words } = {}) {
    const start = fin(seg.start_time) ? seg.start_time : 0;
    const end = fin(seg.end_time) ? Math.max(seg.end_time, start) : start;
    if (fin(time) && time > start + EPS && time < end - EPS) return round3(time);
    if (Array.isArray(words) && words.length) {
        let pos = 0;
        for (const w of words) {
            const text = String(w.word == null ? '' : w.word);
            const at = seg.sentence.indexOf(text.trim(), pos);
            const from = at < 0 ? pos : at;
            if (from >= charOffset && fin(w.start) && w.start > start + EPS && w.start < end - EPS) return round3(w.start);
            pos = from + text.trim().length;
        }
    }
    const length = seg.sentence.length;
    // Neither half is left without time, also when the cut is at either end
    // of the text.
    const share = length > 0 ? Math.min(0.95, Math.max(0.05, charOffset / length)) : 0.5;
    return round3(start + (end - start) * share);
}

const speakerValue = (seg) => (seg.sid == null ? seg.speaker : { n: seg.speaker, id: seg.sid });
const speakerName = (value) => (typeof value === 'string' ? value : value.n);

function put(seg, field, value) {
    if (field === 'speaker') {
        if (typeof value === 'string') {
            seg.speaker = value;
            seg.sid = undefined;
        } else {
            seg.speaker = value.n;
            seg.sid = value.id;
        }
    } else {
        seg[field] = value;
    }
}

const read = (seg, field) => (field === 'speaker' ? speakerValue(seg) : seg[field]);
const same = (a, b) => (a === b) || (a && b && typeof a === 'object' && typeof b === 'object' && a.n === b.n && a.id === b.id);
const clone = (seg) => ({ ...seg });

export function createAsrModel(rawSegments, { now = () => Date.now() } = {}) {
    let nextId = 1;
    let segs = [];
    const log = [];              // applied entries are log[0 .. pointer)
    let pointer = 0;
    let savedPointer = 0;        // -1 once the saved state can no longer be reached by undo/redo
    const orphanTouched = new Set();
    let rev = 0;                 // any change
    let orderRev = 0;            // insert, delete or move
    const listeners = new Set();

    // ---- loading -----------------------------------------------------------

    const loaded = (Array.isArray(rawSegments) ? rawSegments : []).map((raw) => toSegment(raw, nextId++));
    {
        // Stable sort by start time; a segment without one stays behind the
        // segment it followed.
        let key = -Infinity;
        const keyed = loaded.map((seg, order) => {
            if (fin(seg.start_time)) key = seg.start_time;
            return { seg, key, order };
        });
        let sorted = true;
        for (let i = 1; i < keyed.length; i++) if (keyed[i].key < keyed[i - 1].key) { sorted = false; break; }
        if (!sorted) keyed.sort((a, b) => (a.key - b.key) || (a.order - b.order));
        segs = keyed.map((k) => k.seg);
    }

    // ---- index and order helpers ------------------------------------------

    let idIndex = null;
    let idIndexRev = -1;
    const indexOfId = (id) => {
        if (idIndexRev !== orderRev || !idIndex) {
            idIndex = new Map();
            for (let i = 0; i < segs.length; i++) idIndex.set(segs[i].id, i);
            idIndexRev = orderRev;
        }
        const i = idIndex.get(id);
        return i === undefined ? -1 : i;
    };

    // Sort key of position i: its start, or the nearest earlier start.
    const keyAt = (i) => {
        for (let k = i; k >= 0; k--) if (fin(segs[k].start_time)) return segs[k].start_time;
        return -Infinity;
    };
    // First index in [lo, hi) whose key is greater than t.
    const upperBound = (t, lo = 0, hi = segs.length) => {
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (keyAt(mid) > t) hi = mid; else lo = mid + 1;
        }
        return lo;
    };
    // First index in [lo, hi) whose key is t or more.
    const lowerBound = (t, lo = 0, hi = segs.length) => {
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (keyAt(mid) >= t) hi = mid; else lo = mid + 1;
        }
        return lo;
    };

    // ---- problems ----------------------------------------------------------

    const problems = new Map();   // id -> mask

    const previousSameSpeaker = (i) => {
        const speaker = segs[i].speaker;
        for (let k = i - 1, n = 0; k >= 0 && n < SCAN_LIMIT; k--, n++) {
            if (segs[k].speaker === speaker) return segs[k];
        }
        return null;
    };

    const maskOf = (i, previous) => {
        const seg = segs[i];
        let mask = 0;
        const s = seg.start_time;
        const e = seg.end_time;
        if (!fin(s) || !fin(e) || e <= s + EPS) mask |= PROBLEM.DURATION;
        if (fin(s)) {
            if (i > 0 && keyAt(i - 1) > s + EPS) mask |= PROBLEM.ORDER;
            if (previous && fin(previous.end_time) && previous.end_time > s + EPS) mask |= PROBLEM.OVERLAP;
        }
        return mask;
    };

    const setMask = (id, mask) => {
        if (mask) problems.set(id, mask); else problems.delete(id);
    };

    const recheckAll = () => {
        problems.clear();
        const last = new Map();   // speaker -> index of its latest segment
        for (let i = 0; i < segs.length; i++) {
            const seg = segs[i];
            const k = last.get(seg.speaker);
            setMask(seg.id, maskOf(i, k !== undefined && i - k <= SCAN_LIMIT ? segs[k] : null));
            last.set(seg.speaker, i);
        }
    };

    const recheckAt = (i) => {
        if (i < 0 || i >= segs.length) return;
        setMask(segs[i].id, maskOf(i, previousSameSpeaker(i)));
    };

    // The segments whose problems may have changed after an edit at i: the
    // segment, its neighbours, and the next segment of each speaker after it
    // (the one that looks back at i, or at what was removed at i).
    const recheckAround = (i) => {
        recheckAt(i - 1);
        recheckAt(i);
        const seen = new Set();
        for (let k = i + 1, n = 0; k < segs.length && n < SCAN_LIMIT; k++, n++) {
            const speaker = segs[k].speaker;
            if (n === 0 || !seen.has(speaker)) recheckAt(k);
            seen.add(speaker);
        }
    };

    recheckAll();

    // ---- primitive steps ---------------------------------------------------

    const applyStep = (st, forward) => {
        switch (st.k) {
            case 'set':
                put(segs[st.i], st.f, forward ? st.b : st.a);
                break;
            case 'mset': {
                const value = forward ? st.b : st.a;
                const many = forward ? st.bm : st.am;
                for (let n = 0; n < st.idx.length; n++) put(segs[st.idx[n]], st.f, many ? value[n] : value);
                break;
            }
            case 'ins':
            case 'del':
                if ((st.k === 'ins') === forward) segs.splice(st.i, 0, clone(st.seg));
                else segs.splice(st.i, 1);
                orderRev++;
                break;
            case 'mov': {
                const from = forward ? st.i : st.j;
                const to = forward ? st.j : st.i;
                const [seg] = segs.splice(from, 1);
                segs.splice(to, 0, seg);
                orderRev++;
                break;
            }
            default:
                throw new Error(`unknown step ${st.k}`);
        }
    };

    const checkStep = (st) => {
        const inRange = (i, extra = 0) => Number.isInteger(i) && i >= 0 && i < segs.length + extra;
        if (!st || typeof st !== 'object') throw new Error('not a step');
        if ((st.k === 'set' || st.k === 'mset') && !['sentence', 'speaker', 'start_time', 'end_time'].includes(st.f)) throw new Error('unknown field');
        if (st.k === 'set' && !inRange(st.i)) throw new Error('step out of range');
        if (st.k === 'mset' && !(Array.isArray(st.idx) && st.idx.every((i) => inRange(i)))) throw new Error('step out of range');
        if (st.k === 'ins' && !(inRange(st.i, 1) && st.seg)) throw new Error('step out of range');
        if (st.k === 'del' && !inRange(st.i)) throw new Error('step out of range');
        if (st.k === 'mov' && !(inRange(st.i) && inRange(st.j))) throw new Error('step out of range');
    };

    // ---- transactions ------------------------------------------------------

    let tx = null;   // { steps, ids:Set, spots:Set(ids near deletions and moves), structural }

    const touch = (seg) => { if (seg) tx.ids.add(seg.id); };
    const spot = (i) => {
        if (i > 0 && segs[i - 1]) tx.spots.add(segs[i - 1].id);
        if (segs[i]) tx.spots.add(segs[i].id);
    };

    const exec = (st) => {
        if (st.k === 'set') {
            if (same(st.a, st.b)) return;
            touch(segs[st.i]);
        } else if (st.k === 'mset') {
            if (!st.idx.length) return;
            for (const i of st.idx) touch(segs[i]);
        } else if (st.k === 'del') {
            touch(segs[st.i]);
        } else if (st.k === 'mov') {
            if (st.i === st.j) return;
            touch(segs[st.i]);
            spot(st.i + 1);
        }
        applyStep(st, true);
        if (st.k === 'ins') touch(segs[st.i]);
        if (st.k === 'del') spot(st.i);
        if (st.k !== 'set' && st.k !== 'mset') tx.structural = true;
        tx.steps.push(st);
    };

    const setField = (i, f, value) => exec({ k: 'set', i, f, a: read(segs[i], f), b: value });

    // Put the segment at i where its start time belongs. Returns its index.
    const resort = (i) => {
        const seg = segs[i];
        const s = seg.start_time;
        if (!fin(s)) return i;
        const before = i > 0 ? keyAt(i - 1) : -Infinity;
        let after = Infinity;
        for (let k = i + 1; k < segs.length; k++) if (fin(segs[k].start_time)) { after = segs[k].start_time; break; }
        if (before <= s && s <= after) return i;
        let j;
        if (s < before) {
            j = upperBound(s, 0, i);
        } else {
            // First later position whose start is s or more; the segment
            // lands just before it (one place lower once it is taken out).
            let lo = i + 1;
            let hi = segs.length;
            while (lo < hi) {
                const mid = (lo + hi) >> 1;
                const key = fin(segs[mid].start_time) ? segs[mid].start_time : keyAt(mid);
                if (key >= s) hi = mid; else lo = mid + 1;
            }
            j = lo - 1;
        }
        exec({ k: 'mov', i, j });
        return j;
    };

    const emit = (event) => {
        rev++;
        for (const fn of listeners) fn(event);
    };

    const recheckTouched = (ids, spots) => {
        if (ids.size + spots.size > FULL_RECHECK) {
            recheckAll();
            return;
        }
        for (const id of ids) if (indexOfId(id) < 0) problems.delete(id);
        for (const id of ids) { const i = indexOfId(id); if (i >= 0) recheckAround(i); }
        for (const id of spots) { const i = indexOfId(id); if (i >= 0) recheckAround(i); }
    };

    const dropRedo = () => {
        if (pointer >= log.length) return;
        if (savedPointer > pointer) {
            for (let k = pointer; k < savedPointer; k++) for (const id of log[k].ids) orphanTouched.add(id);
            savedPointer = -1;
        }
        log.length = pointer;
    };

    /**
     * Run `body` as one undo step. `burst` is a key: an edit with the same
     * key as the last step, made shortly after it, joins that step.
     */
    const transact = (label, body, { burst = null, meta = null } = {}) => {
        tx = { steps: [], ids: new Set(), spots: new Set(), structural: false };
        let result;
        try {
            result = body();
        } catch (error) {
            for (let k = tx.steps.length - 1; k >= 0; k--) applyStep(tx.steps[k], false);
            tx = null;
            throw error;
        }
        const done = tx;
        tx = null;
        if (!done.steps.length) return { changed: false, result };

        const at = now();
        const last = pointer > 0 && pointer === log.length ? log[pointer - 1] : null;
        if (burst && last && last.burst === burst && at - last.at <= BURST_MS && savedPointer !== pointer) {
            // Join the last step. A run of sets of one field keeps only the
            // first old value and the newest new value.
            for (const st of done.steps) {
                const tail = last.steps[last.steps.length - 1];
                if (st.k === 'set' && tail && tail.k === 'set' && tail.i === st.i && tail.f === st.f) tail.b = st.b;
                else last.steps.push(st);
            }
            for (const id of done.ids) if (!last.ids.includes(id)) last.ids.push(id);
            last.at = at;
        } else {
            dropRedo();
            const entry = { label, steps: done.steps, ids: [...done.ids], at };
            if (burst) entry.burst = burst;
            if (meta) entry.meta = meta;
            log.push(entry);
            pointer = log.length;
        }
        recheckTouched(done.ids, done.spots);
        emit({ type: label, source: 'do', ids: [...done.ids], structural: done.structural });
        return { changed: true, result };
    };

    const stepIds = (entry) => {
        // Segments near the deletions and moves of an entry, for the problem
        // recheck after it is undone or redone.
        const spots = new Set();
        for (const st of entry.steps) {
            if (st.k === 'del' || st.k === 'ins') {
                if (st.i > 0 && segs[st.i - 1]) spots.add(segs[st.i - 1].id);
                if (segs[st.i]) spots.add(segs[st.i].id);
                if (segs[st.i + 1]) spots.add(segs[st.i + 1].id);
            } else if (st.k === 'mov') {
                for (const i of [st.i, st.j, st.i + 1, st.j + 1]) if (segs[i]) spots.add(segs[i].id);
            }
        }
        return spots;
    };

    const isStructural = (entry) => entry.steps.some((st) => st.k !== 'set' && st.k !== 'mset');

    const undo = () => {
        if (pointer === 0) return null;
        const entry = log[pointer - 1];
        const spots = stepIds(entry);
        for (let k = entry.steps.length - 1; k >= 0; k--) applyStep(entry.steps[k], false);
        pointer--;
        entry.burst = null;
        for (const id of stepIds(entry)) spots.add(id);
        recheckTouched(new Set(entry.ids), spots);
        emit({ type: entry.label, source: 'undo', ids: entry.ids, structural: isStructural(entry) });
        return entry;
    };

    const redo = () => {
        if (pointer >= log.length) return null;
        const entry = log[pointer];
        const spots = stepIds(entry);
        for (const st of entry.steps) applyStep(st, true);
        pointer++;
        for (const id of stepIds(entry)) spots.add(id);
        recheckTouched(new Set(entry.ids), spots);
        emit({ type: entry.label, source: 'redo', ids: entry.ids, structural: isStructural(entry) });
        return entry;
    };

    const endBurst = () => {
        if (pointer > 0) log[pointer - 1].burst = null;
    };

    // ---- operations --------------------------------------------------------

    const valid = (i) => Number.isInteger(i) && i >= 0 && i < segs.length;
    const cleanIndices = (indices) => [...new Set((indices || []).filter(valid))].sort((a, b) => a - b);
    const cleanTime = (t) => round3(Math.max(0, t));

    const setText = (i, text, { burst = true } = {}) => {
        if (!valid(i)) return false;
        const value = String(text == null ? '' : text);
        return transact('text', () => setField(i, 'sentence', value),
            { burst: burst ? `text:${segs[i].id}` : null }).changed;
    };

    // The neighbour whose edge touches the given edge of segment i, or -1.
    const touching = (i, edge) => {
        const seg = segs[i];
        if (edge === 'start') {
            for (let k = i - 1, n = 0; k >= 0 && n < 4; k--, n++) {
                if (fin(segs[k].end_time) && Math.abs(segs[k].end_time - seg.start_time) <= TOUCH) return k;
            }
        } else {
            for (let k = i + 1, n = 0; k < segs.length && n < 4; k++, n++) {
                if (fin(segs[k].start_time) && Math.abs(segs[k].start_time - seg.end_time) <= TOUCH) return k;
            }
        }
        return -1;
    };

    /**
     * Move the start or end of segment i to time t. With `linked`, a
     * neighbour whose edge touches this one moves with it. Returns the
     * segment's index afterwards, or -1 when nothing changed.
     */
    const moveEdge = (i, edge, t, { linked = false, burst = false } = {}) => {
        if (!valid(i) || !fin(t) || (edge !== 'start' && edge !== 'end')) return -1;
        const value = cleanTime(t);
        const id = segs[i].id;
        const { changed } = transact('time', () => {
            const neighbour = linked && fin(segs[i][`${edge}_time`]) ? touching(i, edge) : -1;
            if (edge === 'start') {
                if (neighbour >= 0) setField(neighbour, 'end_time', value);
                setField(i, 'start_time', value);
                resort(i);
            } else {
                setField(i, 'end_time', value);
                if (neighbour >= 0) {
                    setField(neighbour, 'start_time', value);
                    resort(neighbour);
                }
            }
        }, { burst: burst ? `time:${id}:${edge}` : null });
        return changed ? indexOfId(id) : -1;
    };

    const setStart = (i, t, options) => moveEdge(i, 'start', t, options);
    const setEnd = (i, t, options) => moveEdge(i, 'end', t, options);

    /** Set both times of segment i in one step. Returns its index afterwards, or -1. */
    const setTimes = (i, start, end) => {
        if (!valid(i) || !fin(start) || !fin(end)) return -1;
        const id = segs[i].id;
        const { changed } = transact('time', () => {
            setField(i, 'start_time', cleanTime(start));
            setField(i, 'end_time', cleanTime(end));
            resort(i);
        });
        return changed ? indexOfId(id) : -1;
    };

    /** Move segment i so that it starts at `start`, keeping its length. */
    const move = (i, start) => {
        if (!valid(i) || !fin(start) || !fin(segs[i].start_time)) return -1;
        const seg = segs[i];
        const length = fin(seg.end_time) ? seg.end_time - seg.start_time : 0;
        return setTimes(i, start, Math.max(0, start) + length);
    };

    /**
     * Split segment i at a character offset. Returns the index of the new
     * second half, or -1. Options: `time` (the playhead) and `words` (word
     * timings), see splitTime.
     */
    const split = (i, charOffset, options = {}) => {
        if (!valid(i)) return -1;
        const seg = segs[i];
        const offset = Math.min(seg.sentence.length, Math.max(0, Math.floor(charOffset)));
        const boundary = splitTime(seg, offset, options);
        const left = seg.sentence.slice(0, offset).replace(/\s+$/, '');
        const right = seg.sentence.slice(offset).replace(/^\s+/, '');
        const id = nextId++;
        transact('split', () => {
            const end = seg.end_time;
            const extra = seg.x ? { ...seg.x } : null;
            if (extra) delete extra.words;
            setField(i, 'sentence', left);
            if (fin(seg.start_time) && fin(end)) setField(i, 'end_time', boundary);
            const second = {
                id, speaker: seg.speaker, sid: seg.sid, sentence: right,
                start_time: fin(seg.start_time) && fin(end) ? boundary : seg.start_time, end_time: end,
                x: extra && Object.keys(extra).length ? extra : null,
            };
            // After the first half, and after any segment that starts before the cut.
            const at = fin(second.start_time) ? Math.max(i + 1, upperBound(second.start_time, i + 1)) : i + 1;
            exec({ k: 'ins', i: at, seg: second });
        });
        return indexOfId(id);
    };

    /**
     * Merge segments into the first of them: its speaker is kept, the texts
     * are joined in order and the times cover all of them. Returns
     * { index, caret } (caret: where the text of the second segment begins)
     * or null.
     */
    const merge = (indices) => {
        const list = cleanIndices(indices);
        if (list.length < 2) return null;
        const first = list[0];
        const head = segs[first];
        let text = head.sentence.replace(/\s+$/, '');
        let caret = -1;
        let end = head.end_time;
        for (const i of list.slice(1)) {
            const part = segs[i].sentence.trim();
            if (part) {
                if (caret < 0) caret = text ? text.length + 1 : 0;
                text = text ? `${text} ${part}` : part;
            }
            if (fin(segs[i].end_time) && (!fin(end) || segs[i].end_time > end)) end = segs[i].end_time;
        }
        transact('merge', () => {
            setField(first, 'sentence', text);
            setField(first, 'end_time', end);
            for (let k = list.length - 1; k >= 1; k--) exec({ k: 'del', i: list[k], seg: clone(segs[list[k]]) });
        });
        return { index: first, caret: caret < 0 ? text.length : caret };
    };

    const mergeWithPrevious = (i) => (valid(i) && i > 0 ? merge([i - 1, i]) : null);
    const mergeWithNext = (i) => (valid(i) && i < segs.length - 1 ? merge([i, i + 1]) : null);

    /** Add a segment. Returns its index. */
    const add = (start, end, speaker, { sentence = '', speakerId } = {}) => {
        const id = nextId++;
        const s = fin(start) ? cleanTime(start) : null;
        const seg = {
            id, speaker: String(speaker == null ? '' : speaker), sid: speakerId == null ? undefined : speakerId,
            sentence: String(sentence), start_time: s, end_time: fin(end) ? cleanTime(end) : null, x: null,
        };
        transact('add', () => exec({ k: 'ins', i: s == null ? segs.length : upperBound(s), seg }));
        return indexOfId(id);
    };

    const remove = (indices) => {
        const list = cleanIndices(indices);
        if (!list.length) return false;
        return transact('remove', () => {
            for (let k = list.length - 1; k >= 0; k--) exec({ k: 'del', i: list[k], seg: clone(segs[list[k]]) });
        }).changed;
    };

    const asSpeaker = (name, speakerId) => (speakerId == null ? String(name) : { n: String(name), id: speakerId });

    const setSpeaker = (indices, name, { speakerId } = {}) => {
        const value = asSpeaker(name, speakerId);
        const list = cleanIndices(indices).filter((i) => !same(speakerValue(segs[i]), value));
        if (!list.length || !speakerName(value).trim()) return false;
        return transact('speaker', () => exec({
            k: 'mset', f: 'speaker', idx: list, a: list.map((i) => speakerValue(segs[i])), am: true, b: value, bm: false,
        })).changed;
    };

    /**
     * Rename a speaker in the whole recording, as one undo step. `match`
     * marks the rename as an accepted voice match ({ label, name, speakerId }),
     * reported by acceptedMatches() until the next save.
     */
    const renameSpeaker = (from, to, { speakerId, match = null } = {}) => {
        const value = asSpeaker(to, speakerId);
        if (!speakerName(value).trim()) return false;
        const list = [];
        for (let i = 0; i < segs.length; i++) if (segs[i].speaker === from && !same(speakerValue(segs[i]), value)) list.push(i);
        if (!list.length) return false;
        return transact('rename', () => exec({
            k: 'mset', f: 'speaker', idx: list, a: list.map((i) => speakerValue(segs[i])), am: true, b: value, bm: false,
        }), { meta: match ? { match } : null }).changed;
    };

    /** Replace the text of several segments as one undo step: [{ index, text }]. */
    const replaceText = (changes) => {
        const list = (changes || []).filter((c) => valid(c.index) && segs[c.index].sentence !== c.text)
            .sort((a, b) => a.index - b.index);
        if (!list.length) return false;
        return transact('replace', () => {
            if (list.length === 1) {
                setField(list[0].index, 'sentence', String(list[0].text));
                return;
            }
            exec({
                k: 'mset', f: 'sentence', idx: list.map((c) => c.index),
                a: list.map((c) => segs[c.index].sentence), am: true, b: list.map((c) => String(c.text)), bm: true,
            });
        }).changed;
    };

    // ---- problems, speakers, lookups --------------------------------------

    const problemAt = (i) => (valid(i) ? problems.get(segs[i].id) || 0 : 0);

    /** Index of the next segment with a problem after `from` (wrapping), or -1. */
    const nextProblem = (from = -1, direction = 1) => {
        const n = segs.length;
        if (!problems.size || !n) return -1;
        for (let k = 1; k <= n; k++) {
            const i = (((from + direction * k) % n) + n) % n;
            if (problems.has(segs[i].id)) return i;
        }
        return -1;
    };

    let speakerCache = null;      // name -> id of the speaker's first segment
    let speakerNames = null;
    let speakerCacheRev = -1;
    const scanSpeakers = () => {
        if (speakerCacheRev !== rev || !speakerCache) {
            speakerCache = new Map();
            for (const seg of segs) if (seg.speaker && !speakerCache.has(seg.speaker)) speakerCache.set(seg.speaker, seg.id);
            speakerNames = [...speakerCache.keys()];
            speakerCacheRev = rev;
        }
    };
    /** Speaker names in order of first appearance. */
    const speakers = () => {
        scanSpeakers();
        return speakerNames;
    };
    /** Id of the first segment of a speaker, or null. */
    const firstIdOfSpeaker = (name) => {
        scanSpeakers();
        const id = speakerCache.get(name);
        return id === undefined ? null : id;
    };

    /** Index of the segment being spoken at time t, or -1. */
    const indexAtTime = (t) => {
        if (!fin(t) || !segs.length) return -1;
        const last = upperBound(t) - 1;
        for (let k = last, n = 0; k >= 0 && n < 8; k--, n++) {
            const seg = segs[k];
            if (fin(seg.start_time) && fin(seg.end_time) && seg.start_time <= t && t < seg.end_time) return k;
        }
        return last;
    };

    // ---- saving, changes, draft -------------------------------------------

    const isDirty = () => pointer !== savedPointer;

    /** What changed since the last save: the changed segments and the ids removed. */
    const changes = () => {
        const ids = new Set(orphanTouched);
        if (savedPointer >= 0) {
            for (let k = Math.min(pointer, savedPointer); k < Math.max(pointer, savedPointer); k++) {
                for (const id of log[k].ids) ids.add(id);
            }
        } else {
            for (let k = 0; k < pointer; k++) for (const id of log[k].ids) ids.add(id);
        }
        const changed = [];
        const removedIds = [];
        for (const id of ids) {
            const i = indexOfId(id);
            if (i >= 0) changed.push(segs[i]); else removedIds.push(id);
        }
        changed.sort((a, b) => indexOfId(a.id) - indexOfId(b.id));
        return { dirty: isDirty(), changed, removedIds };
    };

    const markSaved = () => {
        savedPointer = pointer;
        orphanTouched.clear();
        for (let k = 0; k < pointer; k++) if (log[k].meta && log[k].meta.match) log[k].meta.recorded = true;
        endBurst();
        emit({ type: 'saved', source: 'save', ids: [], structural: false });
    };

    /** Voice matches accepted since the last save: [{ label, name, speakerId, ids }]. */
    const acceptedMatches = () => {
        const out = [];
        for (let k = 0; k < pointer; k++) {
            const meta = log[k].meta;
            if (meta && meta.match && !meta.recorded) out.push({ ...meta.match, ids: log[k].ids });
        }
        return out;
    };

    /**
     * Take the speaker names and links the server stored (it may respell a
     * name and it links names to saved speakers). Not an edit: nothing is
     * added to the undo log. Ignored unless the lists line up.
     */
    const adoptSaved = (stored) => {
        if (!Array.isArray(stored) || stored.length !== segs.length) return false;
        for (let i = 0; i < segs.length; i++) {
            const raw = stored[i] || {};
            if (typeof raw.speaker === 'string') segs[i].speaker = raw.speaker;
            segs[i].sid = raw.speaker_id == null ? undefined : raw.speaker_id;
        }
        recheckAll();
        emit({ type: 'adopt', source: 'save', ids: [], structural: false });
        return true;
    };

    /**
     * The operations applied since the last save, for the local draft: what
     * turns the saved transcript into the current one. Null when the current
     * state is not the saved one plus edits (the saved state was undone past).
     */
    const exportLog = () => {
        if (savedPointer < 0 || pointer < savedPointer) return null;
        return log.slice(savedPointer, pointer).map(({ label, steps, meta }) => (
            meta && !meta.recorded ? { label, steps, meta } : { label, steps }
        ));
    };

    /**
     * Replay a stored log on a freshly loaded model. The steps address
     * segments by position, so they fit any model loaded from the same
     * transcript; inserted segments get new ids. Throws when the log does
     * not fit, leaving the model as it was.
     */
    const importLog = (entries) => {
        if (!Array.isArray(entries)) throw new Error('not a log');
        if (pointer !== 0 || log.length) throw new Error('the model already has edits');
        const applied = [];
        const restored = [];
        try {
            for (const entry of entries) {
                if (!entry || !Array.isArray(entry.steps)) throw new Error('not a log entry');
                const ids = new Set();
                const steps = [];
                for (const stored of entry.steps) {
                    const st = stored && stored.k === 'ins' && stored.seg ? { ...stored, seg: { ...stored.seg, id: nextId++ } } : stored;
                    checkStep(st);
                    if (st.k === 'set' || st.k === 'del' || st.k === 'mov') ids.add(segs[st.i].id);
                    if (st.k === 'mset') for (const i of st.idx) ids.add(segs[i].id);
                    applyStep(st, true);
                    applied.push(st);
                    if (st.k === 'ins') ids.add(st.seg.id);
                    steps.push(st);
                }
                restored.push({ label: entry.label || 'edit', steps, ids: [...ids], at: 0, ...(entry.meta ? { meta: entry.meta } : {}) });
            }
        } catch (error) {
            for (let k = applied.length - 1; k >= 0; k--) applyStep(applied[k], false);
            throw error;
        }
        for (const entry of restored) log.push(entry);
        pointer = log.length;
        recheckAll();
        emit({ type: 'restore', source: 'do', ids: [], structural: true });
        return true;
    };

    return {
        get segments() { return segs; },
        get length() { return segs.length; },
        get rev() { return rev; },
        get orderRev() { return orderRev; },
        get canUndo() { return pointer > 0; },
        get canRedo() { return pointer < log.length; },
        get dirty() { return isDirty(); },
        get problemCount() { return problems.size; },
        get undoDepth() { return pointer; },
        get: (i) => segs[i],
        indexOfId,
        indexAtTime,
        toJSON: () => segs.map(segmentToJSON),

        setText, setStart, setEnd, moveEdge, setTimes, move, split, merge, mergeWithPrevious, mergeWithNext,
        add, remove, setSpeaker, renameSpeaker, replaceText,
        undo, redo, endBurst,

        problemAt, nextProblem, speakers, firstIdOfSpeaker,
        changes, markSaved, acceptedMatches, adoptSaved, exportLog, importLog,
        onChange: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
    };
}
