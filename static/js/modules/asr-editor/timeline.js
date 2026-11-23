/**
 * Timeline of the segment editor: a Vue component that draws the editing
 * model (model.js) on two canvases, above the transcript pane.
 *
 * From the top: a time ruler, the waveform, one lane per speaker with the
 * segments as blocks, and an overview of the whole recording. Blocks are
 * dragged by an edge (start or end), by the body (in time, or to another
 * lane to change the speaker), and empty lane space is dragged to add a
 * segment. A drag is previewed on the canvas and stored on release, as one
 * undo step. The geometry is in timeline-geometry.js and the waveform data
 * in timeline-peaks.js; this file draws and listens.
 *
 * Nothing per segment is reactive. One requestAnimationFrame loop draws when
 * something changed and stops when nothing does; only the blocks in view are
 * looked up (binary search in the lanes) and the draw loop allocates nothing
 * in the steady state. Colours are read from the theme's CSS variables and
 * from the speaker chips, once and again when the theme changes.
 *
 * The selection belongs to the transcript pane (pane.js); the two talk
 * through the session: session.pane and session.timeline.
 *
 * Trim mode (#660): two handles on the canvas mark the part of the audio to
 * keep, with fields for their times above the lanes; the rest is dimmed and
 * the blocks cannot be edited. Trim asks the session (composables/
 * asr-editor.js), which confirms and sends the trim; the range rules are in
 * trim.js.
 * Template: templates/components/asr-editor-timeline.html.
 */

import { bestSuggestion, formatExact, parseTime, speakerColorClass } from './helpers.js';
import { fullRange, handleAt, moveEdge, previewRanges, removesSomething, trimEffect } from './trim.js';
import {
    CREATE_MIN, MAX_PPS, MERGE_PPS,
    beginCreate, beginMove, beginResize, buildLanes, changesOf, clamp, clampView, createTo, dragSlop, edgeSlop, hitTest,
    isDrag, leavesLane, lowerBound, minPps as minPpsOf, moveTo, movesInTime, nextLaneOrder, overviewWindow, resizeTo, revealInView, round3,
    rulerStep, upperBound, zoomAt,
} from './timeline-geometry.js';
import { levelFor, loadPeaks, quietNear } from './timeline-peaks.js';

const RULER = 22;                 // height of the time ruler
const OVERVIEW = 26;              // height of the overview bar
const OPEN_KEY = 'speakr.asrEditor.timeline';
const START_PPS = 30;
const FOLLOW_HOLD_MS = 2500;      // after scrolling by hand, playback does not scroll for this long
const EMPTY = new Set();
// Line dashes, made once: the draw loop allocates nothing
const DASH_NEW = [4, 3];
const DASH_SNAP = [5, 3];
const DASH_NONE = [];

const translate = (key, params) => (window.i18n ? window.i18n.t(key, params) : key);
const percent = (value) => (window.i18n && window.i18n.formatPercent ? window.i18n.formatPercent(value) : `${Math.round(Number(value) || 0)}%`);
const isNarrow = () => typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(max-width: 639px)').matches;

const pad2 = (n) => (n < 10 ? `0${n}` : String(n));

/** A ruler label: 0:05, 1:02:03, and 0:05.5 below one second per tick. */
function tickLabel(seconds, tenths) {
    const total = tenths ? Math.round(seconds * 10) / 10 : Math.round(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total - h * 3600) / 60);
    const s = total - h * 3600 - m * 60;
    const sec = tenths ? (s < 10 ? `0${s.toFixed(1)}` : s.toFixed(1)) : pad2(s);
    return h ? `${h}:${pad2(m)}:${sec}` : `${m}:${sec}`;
}

function roundRect(c, x, y, w, h, r) {
    if (w <= 0 || h <= 0) return false;
    const radius = Math.min(r, w / 2, h / 2);
    c.beginPath();
    if (c.roundRect) c.roundRect(x, y, w, h, radius);
    else c.rect(x, y, w, h);
    return true;
}

export function createAsrEditorTimeline() {
    const { ref, shallowRef, onMounted, onBeforeUnmount, nextTick, watch } = Vue;

    return {
        name: 'AsrEditorTimeline',
        template: '#asr-editor-timeline-template',
        delimiters: ['${', '}'],
        props: {
            // The editing session of the open recording (see composables/asr-editor.js). Not reactive.
            session: { type: Object, required: true },
            // The recording's stored length in seconds, until the audio element knows its own.
            duration: { type: Number, default: 0 },
            // False when the recording has no audio file (removed, incognito): no waveform is asked for.
            hasAudio: { type: Boolean, default: true },
            // Whether Trim is offered: the stored transcript of a finished recording with audio, editable by the user (#660).
            canTrim: { type: Boolean, default: false },
            // Whether the recording has a summary to regenerate after a trim.
            hasSummary: { type: Boolean, default: false },
            // The stored length (audio_duration), which the server checks a trim against.
            storedDuration: { type: Number, default: 0 },
        },

        setup(props) {
            const session = props.session;
            const model = session.model;

            const root = ref(null);
            const box = ref(null);
            const canvas = ref(null);
            const mini = ref(null);
            const heads = ref(null);
            const tip = ref(null);

            const open = ref(true);
            try { open.value = localStorage.getItem(OPEN_KEY) !== '0'; } catch (e) { /* storage unavailable */ }
            const lanes = shallowRef([]);            // lane headers: per speaker, never per segment
            const laneHeight = ref(36);
            const waveHeight = ref(40);
            const waveState = ref('none');           // none | loading | ready
            const narrow = ref(isNarrow());          // phone width: the voice matches are listed above the lanes
            // Trim mode (#660): the range to keep, the text of its two fields, and the summary checkbox
            const trimming = ref(false);
            const trimStartText = ref('');
            const trimEndText = ref('');
            const trimKept = ref('');
            const trimEffectText = ref('');
            const trimSummary = ref(false);
            const trimSending = ref(false);
            const trimInvalid = ref('');             // 'start' | 'end' while a field holds no usable time

            // ---- state of the drawing, none of it reactive -------------------

            let ctx = null;
            let miniCtx = null;
            let miniCache = null;
            let W = 0;                               // width of the canvases, CSS pixels
            let H = 0;
            let dpr = 1;
            let LANE = 36;
            let WAVE = 40;
            const view = { t0: 0, pps: START_PPS };
            let duration = 1;
            let laneOrder = [];
            let data = buildLanes([], []);           // { lanes, laneOf, maxLength, end }
            let laneBg = [];
            let laneFg = [];
            const C = {};
            let peaks = null;
            let playhead = 0;
            let currentId = null;                    // the segment being played
            let hoverSeg = null;
            let drag = null;
            let pinch = null;
            let miniDrag = null;
            const pointers = new Map();
            let dirty = true;
            let miniDirty = true;
            let lanesDirty = true;
            let raf = 0;
            let followAfter = 0;
            let destroyed = false;
            let boundAudio = null;
            const stats = { frameMs: 0, drawn: 0, visible: 0, frames: 0, rebuildMs: 0 };
            const env = { lanes: data.lanes, maxLength: 0, pps: START_PPS, playhead: 0, duration: 1, quietNear: null };
            let trim = null;                         // { start, end } in trim mode, else null
            const miniWin = { x: 0, w: 0 };
            const moveOptions = { alt: false, ctrl: false, keepTime: false };
            // Reused by every frame: blocks to outline on top of merged runs
            const overSegs = [];
            const overLanes = [];
            let overCount = 0;
            const labelCache = new WeakMap();        // segment -> { text, chars, label }
            const tickCache = new Map();
            let tickStep = 0;

            const pane = () => session.pane;
            const selection = () => (session.pane ? session.pane.selection() : EMPTY);
            const audio = () => (session.audio ? session.audio() : null);
            const xOf = (t) => (t - view.t0) * view.pps;
            const tOf = (x) => view.t0 + x / view.pps;
            const lanesTop = () => RULER + WAVE;
            const laneAt = (y) => Math.floor((y - RULER - WAVE) / LANE);

            const requestFrame = () => {
                if (!raf && !destroyed) raf = requestAnimationFrame(frame);
            };
            const touch = () => { dirty = true; requestFrame(); };
            const holdFollow = () => { followAfter = performance.now() + FOLLOW_HOLD_MS; };

            // ---- colours ----------------------------------------------------

            const readColors = () => {
                const el = root.value;
                if (!el) return;
                const style = getComputedStyle(el);
                const v = (name) => style.getPropertyValue(name).trim();
                C.bg = v('--tl-bg');
                C.lane1 = v('--tl-lane');
                C.lane2 = v('--tl-lane-alt');
                C.ruler = v('--tl-ruler');
                C.line = v('--tl-line');
                C.text = v('--tl-text');
                C.muted = v('--tl-muted');
                C.accent = v('--tl-accent');
                C.bad = v('--tl-problem');
                C.wave = v('--tl-wave');
                C.playhead = v('--tl-playhead');
                C.sel = v('--tl-selection');
                const family = getComputedStyle(document.body).fontFamily || 'sans-serif';
                C.rulerFont = `10.5px ${family}`;
                C.blockFont = `11px ${family}`;
                // The blocks have the colours of the speaker chips: read from the lane headers
                laneBg = [];
                laneFg = [];
                const swatches = heads.value ? heads.value.querySelectorAll('[data-asr-lane-swatch]') : [];
                for (const swatch of swatches) {
                    const computed = getComputedStyle(swatch);
                    laneBg.push(computed.backgroundColor);
                    laneFg.push(computed.color);
                }
                dirty = miniDirty = true;
                requestFrame();
            };
            let colorTimers = [];
            const onThemeChange = () => {
                // The theme's stylesheet may still be loading: read again shortly after
                readColors();
                for (const timer of colorTimers) clearTimeout(timer);
                colorTimers = [setTimeout(readColors, 300), setTimeout(readColors, 1500)];
            };

            // ---- lanes ------------------------------------------------------

            const suggestionOf = (name) => bestSuggestion(session.voice.value, name, model.speakers());

            const syncDuration = () => {
                const a = audio();
                const fromAudio = a && Number.isFinite(a.duration) ? a.duration : 0;
                const next = Math.max(1, fromAudio, props.duration || 0, peaks ? peaks.duration : 0, data.end + 0.5);
                if (next !== duration) {
                    duration = next;
                    clampView(view, W || 1, duration);
                    dirty = miniDirty = true;
                }
            };

            const layout = () => {
                const narrow = isNarrow();
                const count = Math.max(1, data.lanes.length);
                const base = narrow ? 32 : 36;
                LANE = count <= 6 ? base : clamp(Math.floor((base * 6) / count), 22, base);
                WAVE = narrow ? 30 : 40;
                laneHeight.value = LANE;
                waveHeight.value = WAVE;
                H = RULER + WAVE + data.lanes.length * LANE;
            };

            const rebuildLanes = () => {
                const rebuildStarted = performance.now();
                lanesDirty = false;
                const present = model.speakers().slice();
                if (model.segments.some((seg) => !seg.speaker)) present.push('');
                laneOrder = nextLaneOrder(laneOrder, present);
                // The speakers added in the editor keep their order among
                // themselves, so that a removed one returns to its place on undo
                const addedNames = model.addedSpeakers();
                if (addedNames.length > 1) {
                    const slots = [];
                    for (let i = 0; i < laneOrder.length; i++) if (addedNames.includes(laneOrder[i])) slots.push(i);
                    const inOrder = addedNames.filter((name) => laneOrder.includes(name));
                    for (let k = 0; k < slots.length; k++) laneOrder[slots[k]] = inOrder[k];
                }
                data = buildLanes(model.segments, laneOrder, model.problemAt);
                env.lanes = data.lanes;
                env.maxLength = data.maxLength;
                stats.rebuildMs = performance.now() - rebuildStarted;
                if (hoverSeg && model.indexOfId(hoverSeg.id) < 0) hoverSeg = null;
                syncDuration();

                // The headers change only when the speakers do
                const numbered = model.speakers();
                const empty = new Set(model.emptySpeakers());
                const next = data.lanes.map((lane, index) => {
                    const match = lane.name ? suggestionOf(lane.name) : null;
                    const number = numbered.indexOf(lane.name);
                    return {
                        index,
                        name: lane.name,
                        label: lane.name || translate('asrEditor.noSpeaker'),
                        color: speakerColorClass(session.colorMap, lane.name),
                        number: number >= 0 && number < 9 ? String(number + 1) : '',
                        // What the name button does, for assistive technology and the tooltip
                        // No segments: the header offers to remove the speaker
                        empty: empty.has(lane.name),
                        rename: lane.name ? translate('asrEditor.renameSpeaker', { name: lane.name }) : translate('asrEditor.setSpeaker'),
                        match: match ? translate('asrEditor.suggestion', { name: match.name, percent: percent(Math.round(match.similarity)) }) : '',
                        // The name is already in the recording: accepting merges the two speakers
                        merge: match && match.merge ? translate('asrEditor.suggestionMergeInto', { name: match.name }) : '',
                    };
                });
                const signature = (list) => list.map((l) => `${l.name}\u0001${l.color}\u0001${l.number}\u0001${l.match}\u0001${l.merge}\u0001${l.empty ? 1 : 0}`).join('\u0002');
                if (signature(next) !== signature(lanes.value)) {
                    lanes.value = next;
                    layout();
                    nextTick(() => {
                        if (destroyed) return;
                        sizeCanvas();
                        readColors();
                    });
                }
                dirty = miniDirty = true;
            };

            // ---- size -------------------------------------------------------

            const sizeCanvas = () => {
                const cv = canvas.value;
                const mv = mini.value;
                const holder = box.value;
                if (!cv || !mv || !holder) return;
                dpr = Math.min(3, window.devicePixelRatio || 1);
                W = Math.max(0, holder.clientWidth);
                if (!W) return;                       // hidden (collapsed)
                const w = Math.round(W * dpr);
                const h = Math.round(H * dpr);
                const mh = Math.round(OVERVIEW * dpr);
                cv.style.height = `${H}px`;
                if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
                if (mv.width !== w || mv.height !== mh) { mv.width = w; mv.height = mh; }
                clampView(view, W, duration);
                dirty = miniDirty = true;
            };

            // ---- drawing ----------------------------------------------------

            const labelOf = (seg, width) => {
                // The part of the text that can show, cut in steps so that the
                // cut text is made again only when the block's width changes much
                const chars = (Math.ceil(width / 64) + 1) * 16;
                let entry = labelCache.get(seg);
                if (!entry) { entry = { text: null, chars: 0, label: '' }; labelCache.set(seg, entry); }
                if (entry.text !== seg.sentence || entry.chars !== chars) {
                    entry.text = seg.sentence;
                    entry.chars = chars;
                    entry.label = seg.sentence.length > chars ? seg.sentence.slice(0, chars) : seg.sentence;
                }
                return entry.label;
            };

            const tickText = (k, sub, tenths) => {
                let text = tickCache.get(k);
                if (text === undefined) {
                    if (tickCache.size > 400) tickCache.clear();
                    text = tickLabel(k * sub, tenths);
                    tickCache.set(k, text);
                }
                return text;
            };

            const drawBlock = (c, seg, s, e, li, selected, current, bad, grips) => {
                const y = RULER + WAVE + li * LANE + 4;
                const h = LANE - 8;
                const x0 = (s - view.t0) * view.pps;
                let x1 = (e - view.t0) * view.pps;
                if (x1 - x0 < 3) x1 = x0 + 3;
                const a = x0 < -8 ? -8 : x0;
                const b = x1 > W + 8 ? W + 8 : x1;
                const w = b - a;
                c.fillStyle = laneBg[li] || C.lane2;
                if (roundRect(c, a, y, w, h, 4)) c.fill();
                c.strokeStyle = laneFg[li] || C.muted;
                c.lineWidth = 1;
                c.globalAlpha = current ? 0.9 : 0.5;
                if (roundRect(c, a + 0.5, y + 0.5, w - 1, h - 1, 3.5)) c.stroke();
                c.globalAlpha = 1;
                if (current) { c.fillStyle = C.playhead; c.fillRect(a + 2, y + h - 3, w > 4 ? w - 4 : 0, 2); }
                if (w > 34 && seg.sentence) {
                    const tx = (a > 0 ? a : 0) + 6;
                    c.save();
                    c.beginPath();
                    c.rect(a + 3, y, w - 7, h);
                    c.clip();
                    c.fillStyle = laneFg[li] || C.text;
                    c.fillText(labelOf(seg, b - tx), tx, y + h / 2 + 0.5);
                    c.restore();
                }
                if (bad) { c.strokeStyle = C.bad; c.lineWidth = 1.5; if (roundRect(c, a + 0.75, y + 0.75, w - 1.5, h - 1.5, 3.5)) c.stroke(); }
                if (selected) { c.strokeStyle = C.sel; c.lineWidth = 2; if (roundRect(c, a - 1, y - 1, w + 2, h + 2, 5)) c.stroke(); }
                if (grips && w > 16) {
                    c.fillStyle = laneFg[li] || C.text;
                    c.globalAlpha = 0.6;
                    c.fillRect(a + 2.5, y + 6, 1.5, h - 12);
                    c.fillRect(b - 4, y + 6, 1.5, h - 12);
                    c.globalAlpha = 1;
                }
            };

            const draw = () => {
                const c = ctx;
                if (!c || !W) return;
                const t0 = view.t0;
                const pps = view.pps;
                const t1 = t0 + W / pps;
                const top = RULER + WAVE;
                const laneCount = data.lanes.length;
                const sel = selection();
                const anySelected = sel.size > 0;
                const st = drag && drag.active && drag.st ? drag.st : null;
                const moving = st && (st.kind === 'resize' || st.kind === 'move') ? st.seg : null;
                const following = st && st.kind === 'resize' ? st.neighbour : null;

                c.setTransform(dpr, 0, 0, dpr, 0, 0);
                c.fillStyle = C.bg;
                c.fillRect(0, 0, W, H);
                for (let i = 0; i < laneCount; i++) {
                    c.fillStyle = i % 2 ? C.lane2 : C.lane1;
                    c.fillRect(0, top + i * LANE, W, LANE);
                }
                if (st && st.kind === 'move' && st.lane !== st.fromLane) {
                    c.globalAlpha = 0.16;
                    c.fillStyle = C.accent;
                    c.fillRect(0, top + st.lane * LANE, W, LANE);
                    c.globalAlpha = 1;
                }

                // Ruler
                c.fillStyle = C.ruler;
                c.fillRect(0, 0, W, RULER);
                const loop = session.pane ? session.pane.loopRange() : null;
                if (loop) {
                    c.globalAlpha = 0.3;
                    c.fillStyle = C.accent;
                    const lw = (loop.end - loop.start) * pps;
                    c.fillRect(xOf(loop.start), 0, lw > 2 ? lw : 2, RULER);
                    c.globalAlpha = 1;
                }
                const step = rulerStep(pps);
                if (step !== tickStep) { tickStep = step; tickCache.clear(); }
                const tenths = step < 1;
                c.font = C.rulerFont;
                c.textBaseline = 'middle';
                c.textAlign = 'left';
                c.fillStyle = C.muted;
                c.strokeStyle = C.line;
                c.lineWidth = 1;
                c.beginPath();
                const sub = step / 5;
                const lastTick = Math.ceil(t1 / sub);
                for (let k = Math.floor(t0 / sub); k <= lastTick; k++) {
                    const x = Math.round((k * sub - t0) * pps) + 0.5;
                    const major = k % 5 === 0;
                    c.moveTo(x, major ? 4 : RULER - 5);
                    c.lineTo(x, RULER);
                    if (major && k >= 0) c.fillText(tickText(k, sub, tenths), x + 4, 9);
                }
                c.moveTo(0, RULER - 0.5);
                c.lineTo(W, RULER - 0.5);
                c.moveTo(0, top - 0.5);
                c.lineTo(W, top - 0.5);
                c.stroke();

                // Waveform: one column per pixel, from the level with one to four buckets per pixel
                if (peaks) {
                    const level = peaks.levels[levelFor(peaks.levels, pps)];
                    const values = level.data;
                    const n = level.length;
                    const mid = RULER + WAVE / 2;
                    const half = WAVE / 2 - 3;
                    const perPixel = level.rate / pps;
                    c.fillStyle = C.wave;
                    c.beginPath();
                    let f = t0 * level.rate;
                    for (let x = 0; x < W; x++, f += perPixel) {
                        const i0 = Math.floor(f);
                        if (i0 >= n) break;
                        let i1 = Math.floor(f + perPixel);
                        if (i1 <= i0) i1 = i0 + 1;
                        if (i1 > n) i1 = n;
                        let lo = 0;
                        let hi = 0;
                        for (let i = i0 < 0 ? 0 : i0; i < i1; i++) {
                            const a = values[i * 2];
                            const b = values[i * 2 + 1];
                            if (a < lo) lo = a;
                            if (b > hi) hi = b;
                        }
                        const up = (hi / 127) * half;
                        const down = (-lo / 127) * half;
                        const height = up + down;
                        c.rect(x, mid - up - 0.25, 1, height < 0.5 ? 0.5 : height + 0.5);
                    }
                    c.fill();
                } else {
                    c.fillStyle = C.wave;
                    c.globalAlpha = 0.5;
                    c.fillRect(0, RULER + WAVE / 2 - 0.5, W, 1);
                    c.globalAlpha = 1;
                }

                // Blocks
                let drawn = 0;
                let visible = 0;
                overCount = 0;
                const merged = pps < MERGE_PPS;
                c.font = C.blockFont;
                for (let li = 0; li < laneCount; li++) {
                    const lane = data.lanes[li];
                    const segs = lane.segs;
                    const bad = lane.bad;
                    let i = lowerBound(segs, t0 - data.maxLength);
                    const end = upperBound(segs, t1);
                    if (merged) {
                        // Zoomed far out: touching blocks are one rectangle
                        const y = top + li * LANE + 4;
                        const h = LANE - 8;
                        c.fillStyle = laneFg[li] || C.muted;
                        c.globalAlpha = 0.6;
                        let a = -1e9;
                        let b = -1e9;
                        for (; i < end; i++) {
                            const seg = segs[i];
                            if (seg.end_time < t0 || seg === moving || seg === following) continue;
                            visible++;
                            const x0 = (seg.start_time - t0) * pps;
                            let x1 = (seg.end_time - t0) * pps;
                            if (x1 - x0 < 1) x1 = x0 + 1;
                            if (x0 <= b + 1) { if (x1 > b) b = x1; }
                            else { if (b > a) { c.fillRect(a, y, b - a, h); drawn++; } a = x0; b = x1; }
                            if (bad[i] || seg.id === currentId || (anySelected && sel.has(seg.id))) {
                                overSegs[overCount] = seg;
                                overLanes[overCount] = bad[i] ? -li - 1 : li;
                                overCount++;
                            }
                        }
                        if (b > a) { c.fillRect(a, y, b - a, h); drawn++; }
                        c.globalAlpha = 1;
                    } else {
                        for (; i < end; i++) {
                            const seg = segs[i];
                            if (seg.end_time < t0 || seg === moving || seg === following) continue;
                            visible++;
                            drawn++;
                            const selected = anySelected && sel.has(seg.id);
                            drawBlock(c, seg, seg.start_time, seg.end_time, li, selected, seg.id === currentId, bad[i] === 1, selected || seg === hoverSeg);
                        }
                    }
                }
                for (let j = 0; j < overCount; j++) {       // outlines on top of merged runs
                    const seg = overSegs[j];
                    const isBad = overLanes[j] < 0;
                    const li = isBad ? -overLanes[j] - 1 : overLanes[j];
                    const y = top + li * LANE + 4;
                    const h = LANE - 8;
                    const x0 = (seg.start_time - t0) * pps;
                    const w = Math.max(2, (seg.end_time - seg.start_time) * pps);
                    if (seg.id === currentId) { c.fillStyle = C.playhead; c.fillRect(x0, y + h - 3, w, 3); }
                    if (isBad) { c.strokeStyle = C.bad; c.lineWidth = 1.5; c.strokeRect(x0 + 0.5, y + 0.5, w, h - 1); }
                    if (anySelected && sel.has(seg.id)) { c.strokeStyle = C.sel; c.lineWidth = 2; c.strokeRect(x0 - 1, y - 1, w + 2, h + 2); }
                    overSegs[j] = null;
                }

                // The drag in progress, on top
                if (st && st.kind === 'resize') {
                    if (following) drawBlock(c, following, st.neighbourStart, st.neighbourEnd, drag.li, false, false, false, false);
                    drawBlock(c, moving, st.start, st.end, drag.li, true, moving.id === currentId, false, true);
                } else if (st && st.kind === 'move') {
                    c.globalAlpha = 0.92;
                    drawBlock(c, moving, st.start, st.end, st.lane, true, false, false, true);
                    c.globalAlpha = 1;
                } else if (st && st.kind === 'create') {
                    const a = xOf(st.start);
                    const w = (st.end - st.start) * pps;
                    const y = top + st.lane * LANE + 4;
                    const h = LANE - 8;
                    c.fillStyle = laneBg[st.lane] || C.lane2;
                    c.globalAlpha = 0.7;
                    if (roundRect(c, a, y, w, h, 4)) c.fill();
                    c.globalAlpha = 1;
                    c.setLineDash(DASH_NEW);
                    c.strokeStyle = C.sel;
                    c.lineWidth = 1;
                    if (roundRect(c, a + 0.5, y + 0.5, w - 1, h - 1, 4)) c.stroke();
                    c.setLineDash(DASH_NONE);
                }
                if (st && st.snap) {
                    const x = Math.round(xOf(st.snap.t)) + 0.5;
                    c.strokeStyle = C.accent;
                    c.lineWidth = 1;
                    c.setLineDash(DASH_SNAP);
                    c.beginPath();
                    c.moveTo(x, 0);
                    c.lineTo(x, H);
                    c.stroke();
                    c.setLineDash(DASH_NONE);
                    c.fillStyle = C.accent;
                    c.beginPath();
                    c.moveTo(x - 4, RULER);
                    c.lineTo(x + 4, RULER);
                    c.lineTo(x, RULER + 5);
                    c.fill();
                }

                // Trim mode: the parts that go are dimmed, the edges have handles
                if (trim) {
                    const sx = xOf(trim.start);
                    const ex = xOf(trim.end);
                    c.fillStyle = C.bg;
                    c.globalAlpha = 0.72;
                    if (sx > 0) c.fillRect(0, 0, Math.min(sx, W), H);
                    if (ex < W) c.fillRect(Math.max(ex, 0), 0, W - Math.max(ex, 0), H);
                    c.globalAlpha = 1;
                    c.strokeStyle = C.accent;
                    c.fillStyle = C.accent;
                    c.lineWidth = 2;
                    for (const x of [sx, ex]) {
                        if (x < -10 || x > W + 10) continue;
                        const hx = Math.round(x);
                        c.beginPath();
                        c.moveTo(hx, 0);
                        c.lineTo(hx, H);
                        c.stroke();
                        // The grip, in the waveform band
                        if (roundRect(c, hx - 5, RULER + 4, 10, WAVE - 8, 3)) c.fill();
                    }
                    c.fillStyle = C.bg;
                    for (const x of [sx, ex]) {
                        const hx = Math.round(x);
                        if (hx < -10 || hx > W + 10) continue;
                        c.fillRect(hx - 1.5, RULER + WAVE / 2 - 5, 1, 10);
                        c.fillRect(hx + 0.5, RULER + WAVE / 2 - 5, 1, 10);
                    }
                }

                // Playhead
                const px = Math.round(xOf(playhead)) + 0.5;
                if (px > -8 && px < W + 8) {
                    c.strokeStyle = C.playhead;
                    c.fillStyle = C.playhead;
                    c.lineWidth = 1.5;
                    c.beginPath();
                    c.moveTo(px, 0);
                    c.lineTo(px, H);
                    c.stroke();
                    c.beginPath();
                    c.moveTo(px - 5, 0);
                    c.lineTo(px + 5, 0);
                    c.lineTo(px, 7);
                    c.fill();
                }
                stats.drawn = drawn;
                stats.visible = visible;
            };

            // The overview: every lane as merged runs, kept in an offscreen
            // canvas and redrawn only when the segments change.
            const drawOverview = () => {
                const mv = mini.value;
                if (!miniCtx || !mv || !W) return;
                const w = mv.width;
                const h = mv.height;
                if (!miniCache) miniCache = document.createElement('canvas');
                if (miniDirty || miniCache.width !== w || miniCache.height !== h) {
                    miniCache.width = w;
                    miniCache.height = h;
                    const c = miniCache.getContext('2d');
                    c.setTransform(dpr, 0, 0, dpr, 0, 0);
                    c.fillStyle = C.ruler;
                    c.fillRect(0, 0, W, OVERVIEW);
                    const laneCount = Math.max(1, data.lanes.length);
                    const rowHeight = (OVERVIEW - 4) / laneCount;
                    const scale = W / duration;
                    c.globalAlpha = 0.7;
                    for (let li = 0; li < data.lanes.length; li++) {
                        const segs = data.lanes[li].segs;
                        const y = 2 + li * rowHeight;
                        c.fillStyle = laneFg[li] || C.muted;
                        let a = -1e9;
                        let b = -1e9;
                        for (let i = 0; i < segs.length; i++) {
                            const x0 = segs[i].start_time * scale;
                            let x1 = segs[i].end_time * scale;
                            if (x1 - x0 < 0.5) x1 = x0 + 0.5;
                            if (x0 <= b + 0.5) { if (x1 > b) b = x1; }
                            else { if (b > a) c.fillRect(a, y, b - a, rowHeight - 0.5); a = x0; b = x1; }
                        }
                        if (b > a) c.fillRect(a, y, b - a, rowHeight - 0.5);
                    }
                    c.globalAlpha = 1;
                    if (model.problemCount) {
                        c.fillStyle = C.bad;
                        for (let li = 0; li < data.lanes.length; li++) {
                            const lane = data.lanes[li];
                            for (let i = 0; i < lane.segs.length; i++) {
                                if (lane.bad[i]) c.fillRect(Math.round(lane.segs[i].start_time * scale) - 1, 0, 2, OVERVIEW);
                            }
                        }
                    }
                    miniDirty = false;
                }
                const c = miniCtx;
                c.setTransform(1, 0, 0, 1, 0, 0);
                c.clearRect(0, 0, w, h);
                c.drawImage(miniCache, 0, 0);
                c.setTransform(dpr, 0, 0, dpr, 0, 0);
                overviewWindow(view, W, duration, miniWin);
                c.fillStyle = C.accent;
                c.globalAlpha = 0.22;
                c.fillRect(miniWin.x, 0, miniWin.w, OVERVIEW);
                c.globalAlpha = 1;
                c.strokeStyle = C.accent;
                c.lineWidth = 1.5;
                c.strokeRect(miniWin.x + 0.75, 0.75, miniWin.w - 1.5, OVERVIEW - 1.5);
                c.fillStyle = C.playhead;
                c.fillRect(Math.round((playhead / duration) * W) - 0.5, 0, 1.5, OVERVIEW);
            };

            // ---- playback ---------------------------------------------------

            const updateCurrent = () => {
                const index = model.indexAtTime(playhead);
                const seg = index >= 0 ? model.get(index) : null;
                const id = seg && Number.isFinite(seg.end_time) && playhead < seg.end_time ? seg.id : null;
                if (id !== currentId) { currentId = id; dirty = true; }
            };

            const setPlayhead = (t) => {
                if (t === playhead) return;
                playhead = t;
                updateCurrent();
                dirty = true;
            };

            /** Move the playhead (and the audio) to a time. */
            const seek = (t) => {
                const time = clamp(t, 0, duration);
                const a = audio();
                if (session.pane) session.pane.leaveLoop(time);
                if (a) {
                    try { a.currentTime = time; } catch (e) { /* not seekable yet */ }
                }
                setPlayhead(time);
                requestFrame();
            };

            const onAudioEvent = () => { syncDuration(); requestFrame(); };
            const bindAudio = () => {
                const a = audio();
                if (a === boundAudio) return;
                const events = ['play', 'pause', 'seeked', 'timeupdate', 'loadedmetadata', 'durationchange', 'ended'];
                if (boundAudio) for (const name of events) boundAudio.removeEventListener(name, onAudioEvent);
                boundAudio = a;
                if (a) for (const name of events) a.addEventListener(name, onAudioEvent);
            };

            // ---- the frame loop ---------------------------------------------

            function frame() {
                raf = 0;
                if (destroyed) return;
                const started = performance.now();
                const a = boundAudio;
                let playing = false;
                if (a) {
                    playing = !a.paused && !a.ended;
                    const t = a.currentTime;
                    if (Number.isFinite(t) && !(drag && drag.kind === 'scrub')) setPlayhead(t);
                    if (playing && !drag && !miniDrag && !pinch && started > followAfter && W
                        && (!session.pane || session.pane.following())) {
                        const x = xOf(playhead);
                        if (x > W * 0.92 || x < 0) {
                            view.t0 = playhead - (W * 0.08) / view.pps;
                            clampView(view, W, duration);
                            dirty = true;
                        }
                    }
                }
                let scrolling = false;
                if (drag && drag.active && drag.lx != null && drag.kind !== 'pan' && W) {
                    // A drag that reaches the side scrolls the view
                    const direction = drag.lx < 24 ? -1 : drag.lx > W - 24 ? 1 : 0;
                    if (direction) {
                        const before = view.t0;
                        view.t0 += (direction * 10) / view.pps;
                        clampView(view, W, duration);
                        if (view.t0 !== before) {
                            dragMove(drag.lx, drag.ly, drag.mods, drag.cx, drag.cy);
                            scrolling = true;
                            dirty = true;
                        }
                    }
                }
                if (lanesDirty) rebuildLanes();
                if (dirty && open.value && W) {
                    dirty = false;
                    draw();
                    drawOverview();
                    stats.frameMs = performance.now() - started;
                    stats.frames++;
                }
                if (playing || scrolling) requestFrame();
            }

            // ---- selection --------------------------------------------------

            const selectSeg = (seg, mods) => {
                const index = model.indexOfId(seg.id);
                if (index >= 0 && session.pane) session.pane.selectFromTimeline(index, mods);
                touch();
            };

            const revealSeg = (seg) => {
                if (!seg || !Number.isFinite(seg.start_time) || !Number.isFinite(seg.end_time) || !W) return;
                if (revealInView(view, seg.start_time, seg.end_time, W, duration)) holdFollow();
                touch();
            };

            // ---- tooltip ----------------------------------------------------

            const showTip = (text, clientX, clientY) => {
                const el = tip.value;
                const host = root.value;
                if (!el || !host) return;
                el.textContent = text;
                el.hidden = false;
                const rect = host.getBoundingClientRect();
                const width = el.offsetWidth;
                el.style.left = `${clamp(clientX - rect.left + 12, 4, Math.max(4, rect.width - width - 4))}px`;
                el.style.top = `${Math.max(2, clientY - rect.top - 34)}px`;
            };
            const hideTip = () => { if (tip.value) tip.value.hidden = true; };
            const snapWord = (snap) => (snap ? ` · ${translate(`asrEditor.timeline.snap.${snap.kind}`)}` : '');

            const setHotLane = (index) => {
                const el = heads.value;
                if (!el) return;
                const rows = el.querySelectorAll('.asr-tl__lane');
                for (let i = 0; i < rows.length; i++) rows[i].classList.toggle('is-hot', i === index);
            };

            // ---- pointer: the timeline ----------------------------------------

            const position = (event) => {
                const rect = canvas.value.getBoundingClientRect();
                return { x: event.clientX - rect.left, y: event.clientY - rect.top };
            };
            const setCursor = (value) => { if (canvas.value) canvas.value.style.cursor = value; };
            const syncEnv = () => {
                env.pps = view.pps;
                env.playhead = playhead;
                env.duration = duration;
                env.quietNear = peaks ? quietAt : null;
            };
            const quietAt = (t, tolerance) => quietNear(peaks, t, tolerance);

            const endDragVisuals = () => {
                hideTip();
                setHotLane(-1);
                setCursor('default');
                touch();
            };

            const cancelDrag = () => {
                if (!drag) return false;
                drag = null;
                endDragVisuals();
                return true;
            };

            const onPointerDown = (event) => {
                if (event.pointerType === 'mouse' && event.button !== 0) return;
                if (lanesDirty) rebuildLanes();
                bindAudio();
                const p = position(event);
                pointers.set(event.pointerId, p);
                try { canvas.value.setPointerCapture(event.pointerId); } catch (e) { /* not capturable */ }
                const active = document.activeElement;
                if (active !== canvas.value) {
                    if (active && active.blur) active.blur();
                    canvas.value.focus({ preventScroll: true });
                }
                if (pointers.size === 2) {
                    cancelDrag();
                    const [a, b] = [...pointers.values()];
                    pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y) || 1, pps: view.pps, t: tOf((a.x + b.x) / 2) };
                    return;
                }
                if (pointers.size > 2 || pinch) return;
                const isTouch = event.pointerType !== 'mouse';
                const mods = { shift: event.shiftKey, toggle: event.ctrlKey || event.metaKey };
                if (trim) {
                    // Trim mode: a handle is dragged; the ruler seeks; anywhere else pans
                    const edge = handleAt(p.x, xOf(trim.start), xOf(trim.end), isTouch ? 18 : 8);
                    if (edge) {
                        drag = { kind: 'trim', edge, offset: p.x - xOf(trim[edge]), active: true };
                        setCursor('ew-resize');
                    } else if (p.y < RULER) {
                        drag = { kind: 'scrub', active: true };
                        seek(tOf(p.x));
                    } else {
                        drag = { kind: 'pan', x0: p.x, t0: view.t0, moved: false };
                        setCursor('grabbing');
                    }
                    return;
                }
                if (p.y < RULER) {
                    drag = { kind: 'scrub', active: true };
                    seek(tOf(p.x));
                } else if (p.y < RULER + WAVE) {
                    drag = { kind: 'pan', x0: p.x, t0: view.t0, moved: false };
                    setCursor('grabbing');
                } else {
                    const li = laneAt(p.y);
                    if (li < 0 || li >= data.lanes.length) return;
                    const segs = data.lanes[li].segs;
                    const hit = hitTest(segs, p.x, view, data.maxLength, isTouch);
                    if (hit && hit.zone !== 'body') {
                        const sel = selection();
                        if (!(sel.size === 1 && sel.has(hit.seg.id))) selectSeg(hit.seg);
                        const edgeTime = hit.zone === 'l' ? hit.seg.start_time : hit.seg.end_time;
                        drag = { kind: 'resize', li, st: beginResize(segs, hit.index, hit.zone), offset: p.x - xOf(edgeTime), x0: p.x, slop: edgeSlop(isTouch), active: false };
                        setCursor('ew-resize');
                    } else if (hit) {
                        drag = { kind: 'move', li, seg: hit.seg, index: hit.index, x0: p.x, y0: p.y, slop: dragSlop(isTouch), active: false, clickMods: mods, st: null, laneMoved: false, timeMoved: false };
                    } else {
                        drag = { kind: 'create', li, x0: p.x, slop: dragSlop(isTouch), from: round3(clamp(tOf(p.x), 0, duration)), active: false, st: null };
                    }
                }
            };

            function dragMove(x, y, mods, clientX, clientY) {
                const d = drag;
                d.lx = x;
                d.ly = y;
                d.mods = mods;
                d.cx = clientX;
                d.cy = clientY;
                if (d.kind === 'scrub') {
                    seek(tOf(x));
                    showTip(formatExact(playhead), clientX, clientY);
                    return;
                }
                if (d.kind === 'trim') {
                    setTrim(moveEdge(trim, d.edge, round3(tOf(x - d.offset)), trimDuration()));
                    const word = translate(d.edge === 'start' ? 'asrEditor.timeline.start' : 'asrEditor.timeline.end');
                    showTip(`${word} ${formatExact(trim[d.edge])}`, clientX, clientY);
                    touch();
                    return;
                }
                if (d.kind === 'pan') {
                    if (Math.abs(x - d.x0) > 3) d.moved = true;
                    if (d.moved) {
                        view.t0 = d.t0 - (x - d.x0) / view.pps;
                        clampView(view, W, duration);
                        holdFollow();
                        touch();
                    }
                    return;
                }
                syncEnv();
                if (d.kind === 'resize') {
                    if (!d.active) {
                        if (!isDrag(x - d.x0, 0, d.slop)) return;
                        d.active = true;
                    }
                    const st = resizeTo(d.st, tOf(x - d.offset), mods, env);
                    const word = translate(st.edge === 'r' ? 'asrEditor.timeline.end' : 'asrEditor.timeline.start');
                    const time = st.edge === 'r' ? st.end : st.start;
                    showTip(`${word} ${formatExact(time)} · ${(st.end - st.start).toFixed(2)} s${snapWord(st.snap)}`
                        + (st.linked ? ` · ${translate('asrEditor.timeline.neighborFollows')}` : ''), clientX, clientY);
                    touch();
                    return;
                }
                if (d.kind === 'move') {
                    if (!d.active) {
                        // Less than this is a click: it selects and changes nothing
                        if (!isDrag(x - d.x0, y - d.y0, d.slop)) return;
                        d.active = true;
                        d.st = beginMove(data.lanes[d.li].segs, d.index, d.li, tOf(d.x0) - d.seg.start_time);
                        if (!selection().has(d.seg.id)) selectSeg(d.seg);
                        setCursor('grabbing');
                    }
                    let lane = clamp(laneAt(y), 0, data.lanes.length - 1);
                    if (!d.laneMoved) {
                        if (leavesLane(y - d.y0, LANE)) d.laneMoved = true; else lane = d.li;
                    }
                    // Straight up or down: the times stay as they are
                    if (!d.timeMoved && movesInTime(x - d.x0, d.slop)) d.timeMoved = true;
                    moveOptions.alt = mods.alt;
                    moveOptions.ctrl = mods.ctrl;
                    moveOptions.keepTime = !d.timeMoved;
                    const st = moveTo(d.st, tOf(x) - d.st.grab, lane, moveOptions, env);
                    const other = st.lane !== st.fromLane;
                    setHotLane(other ? st.lane : -1);
                    showTip(`${formatExact(st.start)} – ${formatExact(st.end)}`
                        + (other ? ` · ${data.lanes[st.lane].name || translate('asrEditor.noSpeaker')}` : '') + snapWord(st.snap), clientX, clientY);
                    touch();
                    return;
                }
                if (d.kind === 'create') {
                    if (!d.active) {
                        if (!isDrag(x - d.x0, 0, d.slop)) return;
                        d.active = true;
                        d.st = beginCreate(data.lanes[d.li].segs, d.li, d.from, env);
                        if (session.pane && selection().size) session.pane.clearSelection();
                    }
                    const st = createTo(d.st, tOf(x), mods, env);
                    showTip(`${translate('asrEditor.timeline.newSegment')} ${formatExact(st.start)} – ${formatExact(st.end)}${snapWord(st.snap)}`, clientX, clientY);
                    touch();
                }
            }

            const speakerIdOfLane = (li) => {
                const first = data.lanes[li] && data.lanes[li].segs[0];
                return first ? first.sid : undefined;
            };

            const endDrag = (p) => {
                const d = drag;
                drag = null;
                endDragVisuals();
                if (d.kind === 'scrub' || d.kind === 'trim') return;
                if (d.kind === 'pan') {
                    if (!d.moved) seek(tOf(p.x));
                    return;
                }
                if (d.kind === 'resize') {
                    if (!d.active) return;
                    model.retime(changesOf(d.st));
                    return;
                }
                if (d.kind === 'move') {
                    if (!d.active) {
                        selectSeg(d.seg, d.clickMods);
                        return;
                    }
                    const changes = changesOf(d.st, (li) => data.lanes[li].name);
                    if (!changes.length) return;
                    const relaned = d.st.lane !== d.st.fromLane;
                    if (relaned) changes[0].speakerId = speakerIdOfLane(d.st.lane);
                    const id = d.seg.id;
                    if (model.retime(changes, { label: relaned ? 'speaker' : 'time' })) {
                        const index = model.indexOfId(id);
                        if (index >= 0 && session.pane) session.pane.selectFromTimeline(index);
                    }
                    return;
                }
                if (d.kind === 'create') {
                    if (!d.active) {
                        seek(tOf(p.x));
                        if (session.pane && selection().size) session.pane.clearSelection();
                        return;
                    }
                    const st = d.st;
                    if (st.end - st.start < CREATE_MIN) return;
                    const lane = data.lanes[st.lane];
                    const name = lane.name || model.speakers()[0] || translate('transcription.speaker', { number: 1 });
                    const index = model.add(st.start, st.end, name, { speakerId: speakerIdOfLane(st.lane) });
                    if (index >= 0 && session.pane) session.pane.revealIndex(index, { select: true, textRange: [0, 0] });
                }
            };

            const onPointerMove = (event) => {
                const p = position(event);
                if (pointers.has(event.pointerId)) pointers.set(event.pointerId, p);
                if (pinch) {
                    if (pointers.size >= 2) {
                        const [a, b] = [...pointers.values()];
                        const distance = Math.hypot(a.x - b.x, a.y - b.y) || 1;
                        const middle = (a.x + b.x) / 2;
                        view.pps = clamp((pinch.pps * distance) / pinch.distance, Math.min(minPpsOf(W, duration), MAX_PPS), MAX_PPS);
                        view.t0 = pinch.t - middle / view.pps;
                        clampView(view, W, duration);
                        holdFollow();
                        touch();
                    }
                    return;
                }
                if (!drag) {
                    if (event.pointerType === 'mouse') hoverAt(p.x, p.y);
                    return;
                }
                dragMove(p.x, p.y, { alt: event.altKey, ctrl: event.ctrlKey || event.metaKey }, event.clientX, event.clientY);
            };

            const onPointerEnd = (event, cancelled) => {
                pointers.delete(event.pointerId);
                try { canvas.value.releasePointerCapture(event.pointerId); } catch (e) { /* was not captured */ }
                if (pinch) {
                    if (pointers.size < 2) pinch = null;
                    return;
                }
                if (!drag) return;
                if (cancelled) cancelDrag(); else endDrag(position(event));
            };
            const onPointerUp = (event) => onPointerEnd(event, false);
            const onPointerCancel = (event) => onPointerEnd(event, true);
            const onPointerLeave = () => {
                if (hoverSeg) { hoverSeg = null; touch(); }
            };

            const hoverAt = (x, y) => {
                let cursor = 'default';
                let seg = null;
                if (trim) {
                    const edge = handleAt(x, xOf(trim.start), xOf(trim.end), 8);
                    setCursor(edge ? 'ew-resize' : y < RULER ? 'pointer' : 'grab');
                    if (hoverSeg) { hoverSeg = null; touch(); }
                    return;
                }
                if (y < RULER) cursor = 'pointer';
                else if (y < RULER + WAVE) cursor = 'grab';
                else {
                    const li = laneAt(y);
                    if (li >= 0 && li < data.lanes.length) {
                        const hit = hitTest(data.lanes[li].segs, x, view, data.maxLength, false);
                        if (hit) { seg = hit.seg; cursor = hit.zone === 'body' ? 'grab' : 'ew-resize'; }
                        else cursor = 'crosshair';
                    }
                }
                setCursor(cursor);
                if (seg !== hoverSeg) { hoverSeg = seg; touch(); }
            };

            const onDoubleClick = (event) => {
                if (trim) return;
                const p = position(event);
                const li = laneAt(p.y);
                if (li < 0 || li >= data.lanes.length) return;
                const hit = hitTest(data.lanes[li].segs, p.x, view, data.maxLength, false);
                if (!hit) return;
                selectSeg(hit.seg);
                if (session.pane) session.pane.playRange(hit.seg.start_time, hit.seg.end_time);
            };

            const onWheel = (event) => {
                event.preventDefault();
                let dx = event.deltaX;
                let dy = event.deltaY;
                if (event.deltaMode === 1) { dx *= 16; dy *= 16; }
                if (event.ctrlKey || event.metaKey) {
                    zoomAt(view, Math.exp(-clamp(dy, -60, 60) * 0.01), position(event).x, W, duration);
                } else {
                    view.t0 += (Math.abs(dx) > Math.abs(dy) ? dx : dy) / view.pps;
                    clampView(view, W, duration);
                }
                holdFollow();
                touch();
            };

            const onCanvasKeydown = (event) => {
                if (event.ctrlKey || event.metaKey || event.altKey) return;
                if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
                    event.preventDefault();
                    seek(playhead + (event.key === 'ArrowLeft' ? -1 : 1) * (event.shiftKey ? 5 : 1));
                    const x = xOf(playhead);
                    if (x < 0 || x > W) { view.t0 = playhead - W / 2 / view.pps; clampView(view, W, duration); touch(); }
                } else if (event.key === '+' || event.key === '=') {
                    event.preventDefault();
                    zoomBy(1.6);
                } else if (event.key === '-') {
                    event.preventDefault();
                    zoomBy(1 / 1.6);
                }
            };

            // Esc cancels a drag, before the pane or the dialog see the key; with
            // no drag it leaves trim mode
            const onWindowKeydown = (event) => {
                if (event.key !== 'Escape') return;
                if (drag) {
                    if (cancelDrag()) {
                        event.preventDefault();
                        event.stopPropagation();
                    }
                    return;
                }
                if (trim && !trimSending.value && !document.querySelector('[data-app-dialog]')) {
                    event.preventDefault();
                    event.stopPropagation();
                    cancelTrim();
                }
            };

            // ---- trim mode (#660) -------------------------------------------

            // The stored length first: the server checks the range against it
            const trimDuration = () => {
                const a = audio();
                const fromAudio = a && Number.isFinite(a.duration) && a.duration > 0 ? a.duration : 0;
                return props.storedDuration || props.duration || fromAudio || (peaks ? peaks.duration : 0) || duration;
            };

            const showTrimFields = () => {
                if (!trim) return;
                trimStartText.value = formatExact(trim.start);
                trimEndText.value = formatExact(trim.end);
                trimKept.value = translate('asrEditor.trim.keeps', {
                    kept: formatExact(trim.end - trim.start), total: formatExact(trimDuration()) });
                const effect = trimEffect(model.segments, trim);
                trimEffectText.value = translate('asrEditor.trim.effect', { removed: effect.removed, clipped: effect.clipped });
            };

            function setTrim(range) {
                trim = range;
                trimInvalid.value = '';
                showTrimFields();
            }

            const startTrim = () => {
                if (!props.canTrim || trim) return;
                cancelDrag();
                if (session.pane && selection().size) session.pane.clearSelection();
                hoverSeg = null;
                trimSummary.value = false;
                setTrim(fullRange(trimDuration()));
                trimming.value = true;
                if (!open.value) toggleOpen();
                zoomFit();
                nextTick(() => {
                    const field = !destroyed && root.value ? root.value.querySelector('[data-asr-trim-start]') : null;
                    if (field) field.focus({ preventScroll: true });
                });
            };

            function cancelTrim() {
                trim = null;
                trimming.value = false;
                trimInvalid.value = '';
                cancelDrag();
                touch();
                nextTick(() => {
                    const button = !destroyed && root.value ? root.value.querySelector('[data-asr-action="trim"]') : null;
                    if (button) button.focus({ preventScroll: true });
                });
            }

            /** A time typed in a field: applied when it is a time, else the field is marked. */
            const setTrimField = (edge, text) => {
                if (!trim) return;
                const t = parseTime(text);
                if (t == null) { trimInvalid.value = edge; return; }
                setTrim(moveEdge(trim, edge, t, trimDuration()));
                const x = xOf(trim[edge]);
                if (x < 0 || x > W) revealInView(view, trim[edge], trim[edge], W, duration);
                touch();
            };

            const previewTrim = () => {
                if (!trim || !session.pane) return;
                session.pane.playRanges(previewRanges(trim));
            };

            const applyTrim = async () => {
                if (!trim || trimSending.value || typeof session.requestTrim !== 'function') return;
                if (!removesSomething(trim, trimDuration())) {
                    if (session.toast) session.toast(translate('asrEditor.trim.nothingRemoved'));
                    return;
                }
                trimSending.value = true;
                try {
                    const range = { start: trim.start, end: trim.end };
                    const sent = await session.requestTrim(range, {
                        regenerateSummary: !!(props.hasSummary && trimSummary.value),
                        effect: trimEffect(model.segments, range),
                    });
                    if (sent && !destroyed) { trim = null; trimming.value = false; touch(); }
                } finally {
                    if (!destroyed) trimSending.value = false;
                }
            };

            // ---- pointer: the overview --------------------------------------

            const overviewX = (event) => event.clientX - mini.value.getBoundingClientRect().left;
            const overviewMove = (x) => {
                view.t0 = ((x - miniDrag.offset) / W) * duration;
                clampView(view, W, duration);
                holdFollow();
                touch();
            };
            const onOverviewDown = (event) => {
                if (!W) return;
                const x = overviewX(event);
                overviewWindow(view, W, duration, miniWin);
                miniDrag = { offset: x >= miniWin.x && x <= miniWin.x + miniWin.w ? x - miniWin.x : miniWin.w / 2 };
                try { mini.value.setPointerCapture(event.pointerId); } catch (e) { /* not capturable */ }
                overviewMove(x);
            };
            const onOverviewMove = (event) => { if (miniDrag) overviewMove(overviewX(event)); };
            const onOverviewEnd = () => { miniDrag = null; };
            const onOverviewWheel = (event) => {
                event.preventDefault();
                const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
                view.t0 += (delta / W) * duration * 0.2;
                clampView(view, W, duration);
                holdFollow();
                touch();
            };

            // ---- toolbar ----------------------------------------------------

            const zoomAnchor = () => {
                const x = xOf(playhead);
                return x >= 0 && x <= W ? x : W / 2;
            };
            function zoomBy(factor) {
                if (!W) return;
                zoomAt(view, factor, zoomAnchor(), W, duration);
                holdFollow();
                touch();
            }
            const zoomIn = () => zoomBy(1.6);
            const zoomOut = () => zoomBy(1 / 1.6);
            const zoomFit = () => {
                if (!W) return;
                view.pps = minPpsOf(W, duration);
                view.t0 = 0;
                clampView(view, W, duration);
                touch();
            };

            const toggleOpen = () => {
                open.value = !open.value;
                try { localStorage.setItem(OPEN_KEY, open.value ? '1' : '0'); } catch (e) { /* storage unavailable */ }
                if (open.value) nextTick(() => { sizeCanvas(); readColors(); });
            };

            const acceptSuggestion = (name) => { if (session.pane) session.pane.acceptSuggestion(name); };
            const previewSpeaker = (name) => { if (session.pane) session.pane.previewSpeaker(name); };
            // The lane's name opens the speaker picker of the pane for the whole speaker
            const renameSpeaker = (name, event) => { if (session.pane) session.pane.openLanePicker(name, event.currentTarget); };
            // A speaker without segments: the next free SPEAKER_NN, never a
            // label a voice match is known for
            // A speaker without segments is removed without a question: one undo step
            const removeSpeaker = (name) => {
                if (!model.removeSpeaker(name)) return;
                // Its button is gone with the lane: the focus goes to Add speaker
                requestAnimationFrame(() => nextTick(() => {
                    const button = !destroyed && root.value ? root.value.querySelector('[data-asr-action="add-speaker"]') : null;
                    if (button && document.activeElement === document.body) button.focus({ preventScroll: true });
                }));
            };
            const addSpeaker = () => {
                const name = model.addSpeaker(null, { reserved: Object.keys(session.voice.value || {}) });
                if (name == null) return;
                // The lanes follow with the next frame; then the new one is shown
                requestAnimationFrame(() => nextTick(() => {
                    const button = !destroyed && root.value ? root.value.querySelector('[data-asr-action="add-speaker"]') : null;
                    if (button && button.scrollIntoView) button.scrollIntoView({ block: 'nearest' });
                }));
            };

            // ---- the session's way in ---------------------------------------

            session.timeline = {
                /** The pane's selection changed; `fromTimeline` when a click here caused it. */
                selectionChanged(fromTimeline) {
                    if (!fromTimeline && session.pane) {
                        const id = session.pane.anchor();
                        const index = id == null ? -1 : model.indexOfId(id);
                        if (index >= 0) revealSeg(model.get(index));
                    }
                    touch();
                },
                refresh: touch,
            };

            const stopListening = model.onChange((event) => {
                // Typing changes the text on a block and nothing else
                if (event.type !== 'text') { lanesDirty = true; miniDirty = true; }
                touch();
            });
            // A voice match arrives after the editor opened: the headers show it
            const stopWatching = watch(() => session.voice.value, () => { lanesDirty = true; touch(); });

            let boxObserver = null;
            let themeObserver = null;
            let peaksAbort = null;
            const narrowQuery = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia('(max-width: 639px)') : null;
            const onNarrowChange = () => {
                narrow.value = isNarrow();
                layout();
                // The header column has another width now
                nextTick(() => { if (!destroyed) { sizeCanvas(); touch(); } });
            };
            const onWindowResize = () => {
                if ((window.devicePixelRatio || 1) !== dpr) { sizeCanvas(); touch(); }
            };

            onMounted(() => {
                const cv = canvas.value;
                const mv = mini.value;
                ctx = cv.getContext('2d');
                miniCtx = mv.getContext('2d');
                rebuildLanes();
                layout();
                sizeCanvas();
                readColors();

                cv.addEventListener('pointerdown', onPointerDown);
                cv.addEventListener('pointermove', onPointerMove);
                cv.addEventListener('pointerup', onPointerUp);
                cv.addEventListener('pointercancel', onPointerCancel);
                cv.addEventListener('pointerleave', onPointerLeave);
                cv.addEventListener('dblclick', onDoubleClick);
                cv.addEventListener('wheel', onWheel, { passive: false });
                cv.addEventListener('keydown', onCanvasKeydown);
                mv.addEventListener('pointerdown', onOverviewDown);
                mv.addEventListener('pointermove', onOverviewMove);
                mv.addEventListener('pointerup', onOverviewEnd);
                mv.addEventListener('pointercancel', onOverviewEnd);
                mv.addEventListener('wheel', onOverviewWheel, { passive: false });
                window.addEventListener('keydown', onWindowKeydown, true);
                window.addEventListener('resize', onWindowResize);
                window.addEventListener('speakr:ui-theme', onThemeChange);
                if (narrowQuery && narrowQuery.addEventListener) narrowQuery.addEventListener('change', onNarrowChange);
                if (typeof ResizeObserver !== 'undefined') {
                    boxObserver = new ResizeObserver(() => {
                        sizeCanvas();
                        // Drawn at once: a cleared canvas would flash until the next frame
                        if (C.bg && open.value && W) { dirty = false; draw(); drawOverview(); }
                    });
                    boxObserver.observe(box.value);
                }
                if (typeof MutationObserver !== 'undefined') {
                    themeObserver = new MutationObserver(onThemeChange);
                    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-ui-theme', 'data-theme'] });
                }
                nextTick(() => {
                    if (destroyed) return;
                    bindAudio();
                    syncDuration();
                    readColors();
                });

                // Test and console handle: document.querySelector('[data-asr-timeline-canvas]').__tl
                cv.__tl = {
                    view, stats, xOf, tOf,
                    get width() { return W; },
                    get height() { return H; },
                    get duration() { return duration; },
                    get playhead() { return playhead; },
                    get lanes() { return data.lanes.map((lane) => ({ name: lane.name, count: lane.segs.length })); },
                    get waveform() { return waveState.value; },
                    get dragging() { return !!(drag && drag.active); },
                    get segments() { return model.toJSON(); },
                    get undoDepth() { return model.undoDepth; },
                    get snap() { return drag && drag.st && drag.st.snap ? drag.st.snap.kind : null; },
                    get trim() { return trim ? { start: trim.start, end: trim.end } : null; },
                    laneY: (li) => RULER + WAVE + li * LANE + LANE / 2,
                    waveY: () => RULER + WAVE / 2,
                    setView(t0, pps) {
                        if (pps) view.pps = pps;
                        view.t0 = t0;
                        clampView(view, W, duration);
                        holdFollow();
                        dirty = false;
                        draw();
                        drawOverview();
                    },
                    /**
                     * Time `frames` frames of scrolling and zooming: each is a full
                     * redraw of both canvases, read back so that the time includes
                     * the rasterizing. Returns milliseconds: { median, p95, max }.
                     */
                    benchmark(frames = 120, { readBack = true } = {}) {
                        if (lanesDirty) rebuildLanes();
                        const saved = { t0: view.t0, pps: view.pps };
                        const times = new Float64Array(frames);
                        const span = Math.max(0, duration - W / view.pps);
                        for (let i = 0; i < frames; i++) {
                            if (i < frames / 2) view.t0 = (span * i) / frames;
                            else zoomAt(view, i % 2 ? 1.07 : 0.9, W / 2, W, duration);
                            clampView(view, W, duration);
                            const started = performance.now();
                            draw();
                            drawOverview();
                            if (readBack) { ctx.getImageData(0, 0, 1, 1); miniCtx.getImageData(0, 0, 1, 1); }
                            times[i] = performance.now() - started;
                        }
                        view.t0 = saved.t0;
                        view.pps = saved.pps;
                        touch();
                        const sorted = Array.from(times).sort((a, b) => a - b);
                        return {
                            frames, median: sorted[Math.floor(frames / 2)], p95: sorted[Math.floor(frames * 0.95)],
                            max: sorted[frames - 1], drawn: stats.drawn, visible: stats.visible,
                        };
                    },
                };

                if (props.hasAudio && session.recordingId != null) {
                    peaksAbort = typeof AbortController !== 'undefined' ? new AbortController() : null;
                    loadPeaks(session.recordingId, {
                        signal: peaksAbort ? peaksAbort.signal : null,
                        onPending: () => { if (!destroyed) waveState.value = 'loading'; },
                    }).then((loaded) => {
                        if (destroyed) return;
                        peaks = loaded;
                        waveState.value = loaded ? 'ready' : 'none';
                        syncDuration();
                        touch();
                    });
                }
                requestFrame();
            });

            onBeforeUnmount(() => {
                destroyed = true;
                if (raf) cancelAnimationFrame(raf);
                raf = 0;
                stopListening();
                stopWatching();
                for (const timer of colorTimers) clearTimeout(timer);
                if (peaksAbort) peaksAbort.abort();
                if (boxObserver) boxObserver.disconnect();
                if (themeObserver) themeObserver.disconnect();
                window.removeEventListener('keydown', onWindowKeydown, true);
                window.removeEventListener('resize', onWindowResize);
                window.removeEventListener('speakr:ui-theme', onThemeChange);
                if (narrowQuery && narrowQuery.removeEventListener) narrowQuery.removeEventListener('change', onNarrowChange);
                if (boundAudio) {
                    for (const name of ['play', 'pause', 'seeked', 'timeupdate', 'loadedmetadata', 'durationchange', 'ended']) {
                        boundAudio.removeEventListener(name, onAudioEvent);
                    }
                }
                if (canvas.value) canvas.value.__tl = null;
                if (session.timeline) session.timeline = null;
                peaks = null;
                miniCache = null;
            });

            return {
                root, box, canvas, mini, heads, tip,
                open, lanes, laneHeight, waveHeight, waveState, narrow,
                toggleOpen, zoomIn, zoomOut, zoomFit, acceptSuggestion, previewSpeaker, renameSpeaker, addSpeaker, removeSpeaker,
                trimming, trimStartText, trimEndText, trimKept, trimEffectText, trimSummary, trimSending, trimInvalid,
                startTrim, cancelTrim, setTrimField, previewTrim, applyTrim,
                rulerHeight: RULER, overviewHeight: OVERVIEW,
            };
        },
    };
}
