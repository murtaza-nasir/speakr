/**
 * Transcript pane of the segment editor: a Vue component over the editing
 * model (model.js).
 *
 * The model is not reactive. The pane renders only the rows in view (plus a
 * few around them) and re-renders when `tick` is bumped: after an operation,
 * an undo, a selection change. Typing does not bump it: the text field
 * already shows what was typed, the model is told, and the row is measured
 * again by its ResizeObserver, so no other row is touched.
 *
 * Its own component (not part of the page's root template), so its renders
 * do not re-render the page and the page's do not re-render the rows.
 * Template: templates/components/asr-editor-pane.html.
 */

import { PROBLEM } from './model.js';
import {
    anchorAt, createHeightIndex, estimateRowHeight, scrollTopFor, scrollTopToReveal, visibleRange,
} from './virtual-list.js';
import {
    bestSuggestion, buildSpeakerOptions, formatClock, formatExact, isTypingTarget, longestSegments,
    nextSelection, parseTime, speakerColorClass,
} from './helpers.js';

const OVERSCAN = 6;
const NUDGE = 0.1;
const FOLLOW_KEY = 'speakr.asrEditor.follow';

const translate = (key, params) => (window.i18n ? window.i18n.t(key, params) : key);
const percent = (value) => (window.i18n && window.i18n.formatPercent ? window.i18n.formatPercent(value) : `${Math.round(Number(value) || 0)}%`);
const isMac = () => /Mac|iPhone|iPad/.test((typeof navigator !== 'undefined' && navigator.platform) || '');
const isNarrow = () => typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(max-width: 639px)').matches;
const fieldSizing = typeof CSS !== 'undefined' && CSS.supports && CSS.supports('field-sizing', 'content');

export function createAsrEditorPane() {
    const { ref, shallowRef, computed, reactive, onMounted, onBeforeUnmount, onUpdated, nextTick } = Vue;

    return {
        name: 'AsrEditorPane',
        template: '#asr-editor-pane-template',
        delimiters: ['${', '}'],
        props: {
            // The editing session of the open recording (see composables/asr-editor.js). Not reactive.
            session: { type: Object, required: true },
            findOpen: { type: Boolean, default: false },
        },
        emits: ['toggle-find'],

        setup(props) {
            const session = props.session;
            const model = session.model;

            const root = ref(null);
            const scroller = ref(null);
            const popEl = ref(null);
            const pickerInput = ref(null);

            const tick = ref(0);
            const range = shallowRef({ start: 0, end: 0 });
            const topPad = ref(0);
            const totalHeight = ref(0);
            const canUndo = ref(model.canUndo);
            const canRedo = ref(model.canRedo);
            const problemCount = ref(model.problemCount);
            const rowCount = ref(model.length);
            const selectionCount = ref(0);
            const looping = ref(false);
            const follow = ref(true);
            const narrow = ref(isNarrow());
            try { follow.value = localStorage.getItem(FOLLOW_KEY) !== '0'; } catch (e) { /* storage unavailable */ }

            // Not reactive: read during render, after a bump of `tick`.
            let selected = new Set();
            let anchorId = null;
            let activeId = null;
            let flashId = null;
            let flashTimer = null;
            let typing = false;
            let caret = { id: null, pos: 0 };
            let viewAnchor = { id: null, within: 0 };
            let findFocus = null;       // id of the row whose text shows a find match
            let destroyed = false;

            const bump = () => { tick.value++; };
            // The timeline (timeline.js) shows the selection and the loop too
            let selectingFromTimeline = false;
            const tellTimeline = () => { if (session.timeline) session.timeline.selectionChanged(selectingFromTimeline); };
            const colorOf = (name) => speakerColorClass(session.colorMap, name);

            // ---- layout: heights, window, scroll position -------------------

            const heights = createHeightIndex();
            const measured = new Map();          // segment id -> rendered height
            let charsPerLine = 80;

            const estimate = (seg) => {
                const known = measured.get(seg.id);
                return known !== undefined ? known : estimateRowHeight(seg.sentence.length, charsPerLine, narrow.value ? { padding: 52, min: 76 } : {});
            };

            const rebuildHeights = () => {
                const segs = model.segments;
                heights.reset(segs.length, (i) => estimate(segs[i]));
                rowCount.value = segs.length;
            };

            const rememberAnchor = () => {
                const el = scroller.value;
                if (!el || !heights.count) { viewAnchor = { id: null, within: 0 }; return; }
                const a = anchorAt(heights, el.scrollTop);
                viewAnchor = { id: model.segments[a.row] ? model.segments[a.row].id : null, within: a.within };
            };

            const updateWindow = () => {
                const el = scroller.value;
                if (!el) return;
                const next = visibleRange(heights, el.scrollTop, el.clientHeight, OVERSCAN);
                const current = range.value;
                if (next.start !== current.start || next.end !== current.end) range.value = next;
                topPad.value = heights.offset(next.start);
                totalHeight.value = heights.total();
                rememberAnchor();
            };

            // After rows were added, removed or moved: rebuild the offsets and
            // keep the row at the top of the view where it was.
            const relayout = () => {
                const el = scroller.value;
                const keep = viewAnchor;
                rebuildHeights();
                if (el && keep.id != null) {
                    const i = model.indexOfId(keep.id);
                    if (i >= 0) el.scrollTop = scrollTopFor(heights, { row: i, within: keep.within });
                }
                updateWindow();
            };

            let scrollFrame = 0;
            const onScroll = () => {
                if (scrollFrame) return;
                scrollFrame = requestAnimationFrame(() => {
                    scrollFrame = 0;
                    updateWindow();
                });
            };

            const sizeText = (box, force = false) => {
                if (fieldSizing || !box) return;
                const key = `${box.value.length}:${box.clientWidth}`;
                if (!force && box.dataset.sizedFor === key) return;
                box.style.height = 'auto';
                box.style.height = `${box.scrollHeight}px`;
                box.dataset.sizedFor = key;
            };

            const observed = new Map();          // segment id -> row element
            const applyHeights = (entries) => {
                const el = scroller.value;
                if (!el || destroyed) return;
                const before = anchorAt(heights, el.scrollTop);
                let changed = false;
                for (const entry of entries) {
                    const rowEl = entry.target;
                    if (!rowEl.isConnected) continue;
                    const id = Number(rowEl.dataset.id);
                    const i = model.indexOfId(id);
                    if (i < 0) continue;
                    const size = entry.borderBoxSize && entry.borderBoxSize[0] ? entry.borderBoxSize[0].blockSize : rowEl.offsetHeight;
                    if (!size) continue;
                    measured.set(id, size);
                    if (heights.set(i, size)) changed = true;
                }
                if (!changed) return;
                const top = scrollTopFor(heights, before);
                if (Math.abs(top - el.scrollTop) >= 1) el.scrollTop = top;
                updateWindow();
            };
            const rowObserver = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(applyHeights) : null;

            // After every render: size the text fields and watch the rows.
            const syncRows = () => {
                const el = scroller.value;
                if (!el) return;
                const present = new Set();
                const fallback = [];
                for (const rowEl of el.querySelectorAll('.asr-seg')) {
                    const id = Number(rowEl.dataset.id);
                    present.add(id);
                    sizeText(rowEl.querySelector('textarea'));
                    if (observed.get(id) !== rowEl) {
                        if (rowObserver) {
                            if (observed.has(id)) rowObserver.unobserve(observed.get(id));
                            rowObserver.observe(rowEl);
                        } else {
                            fallback.push({ target: rowEl });
                        }
                        observed.set(id, rowEl);
                    }
                }
                for (const [id, rowEl] of observed) {
                    if (!present.has(id)) {
                        if (rowObserver) rowObserver.unobserve(rowEl);
                        observed.delete(id);
                    }
                }
                if (fallback.length) applyHeights(fallback);
            };

            let widthObserver = null;
            const onResize = () => {
                const el = scroller.value;
                if (!el) return;
                narrow.value = isNarrow();
                const textWidth = Math.max(160, el.clientWidth - (narrow.value ? 64 : 290));
                const next = Math.max(20, Math.floor(textWidth / 7.4));
                if (Math.abs(next - charsPerLine) > 4) {
                    // Another width: the measured heights no longer hold.
                    charsPerLine = next;
                    measured.clear();
                    relayout();
                    for (const rowEl of el.querySelectorAll('.asr-seg')) sizeText(rowEl.querySelector('textarea'), true);
                } else {
                    updateWindow();
                }
            };

            // ---- revealing and focusing rows --------------------------------

            const reveal = (index, { center = false } = {}) => {
                const el = scroller.value;
                if (!el || index < 0 || index >= heights.count) return;
                let top;
                if (center) top = Math.max(0, heights.offset(index) - Math.max(0, (el.clientHeight - heights.height(index)) / 3));
                else top = scrollTopToReveal(heights, index, el.scrollTop, el.clientHeight, 12);
                if (top != null) {
                    el.scrollTop = top;
                    updateWindow();
                }
            };

            // Reveal a row, then again once its neighbours have been measured.
            const revealSettled = (index, options) => {
                reveal(index, options);
                return nextTick().then(() => new Promise((resolve) => requestAnimationFrame(() => {
                    if (!destroyed) reveal(index);
                    resolve();
                })));
            };

            const rowElement = (id) => (scroller.value ? scroller.value.querySelector(`.asr-seg[data-id="${id}"]`) : null);

            const focusText = (id, start, end = start) => {
                const index = model.indexOfId(id);
                if (index < 0) return Promise.resolve();
                reveal(index);
                return nextTick().then(() => {
                    const rowEl = rowElement(id);
                    const box = rowEl && rowEl.querySelector('textarea');
                    if (!box) return;
                    box.focus({ preventScroll: true });
                    const at = Math.min(start == null ? box.value.length : start, box.value.length);
                    const to = Math.min(end == null ? at : end, box.value.length);
                    try { box.setSelectionRange(at, to); } catch (e) { /* not focusable yet */ }
                    caret = { id, pos: at };
                });
            };

            const focusList = () => {
                if (scroller.value) scroller.value.focus({ preventScroll: true });
            };

            const flash = (id) => {
                flashId = id;
                clearTimeout(flashTimer);
                flashTimer = setTimeout(() => { flashId = null; bump(); }, 2500);
                bump();
            };

            // ---- selection ---------------------------------------------------

            const setSelection = (ids, anchor = null) => {
                selected = ids instanceof Set ? ids : new Set(ids);
                anchorId = anchor != null ? anchor : (selected.size ? [...selected][selected.size - 1] : null);
                selectionCount.value = selected.size;
                model.endBurst();
                bump();
                tellTimeline();
            };

            const selectedIndices = () => [...selected].map((id) => model.indexOfId(id)).filter((i) => i >= 0).sort((a, b) => a - b);
            const pruneSelection = () => {
                let changed = false;
                for (const id of [...selected]) if (model.indexOfId(id) < 0) { selected.delete(id); changed = true; }
                if (changed) selectionCount.value = selected.size;
                if (anchorId != null && model.indexOfId(anchorId) < 0) anchorId = null;
            };
            const clearSelection = () => setSelection(new Set());

            const selectIndex = (index, { shift = false, toggle = false } = {}) => {
                const ids = model.segments.map((s) => s.id);
                const next = nextSelection(ids, selected, anchorId, index, { shift, toggle });
                selected = next.selected;
                anchorId = next.anchor;
                selectionCount.value = selected.size;
                model.endBurst();
                bump();
                tellTimeline();
            };

            // The single segment commands act on: the only selected one, else
            // the one being played.
            const currentIndex = () => {
                const list = selectedIndices();
                if (list.length === 1) return list[0];
                if (list.length === 0 && activeId != null) return model.indexOfId(activeId);
                return -1;
            };

            // ---- model changes -----------------------------------------------

            const updateFlags = () => {
                canUndo.value = model.canUndo;
                canRedo.value = model.canRedo;
                problemCount.value = model.problemCount;
            };

            const stopListening = model.onChange((event) => {
                updateFlags();
                if (typing) return;
                if (event.structural) {
                    pruneSelection();
                    relayout();
                }
                bump();
            });

            const afterHistory = (entry) => {
                if (!entry) return;
                const id = entry.ids.find((candidate) => model.indexOfId(candidate) >= 0);
                if (id == null) return;
                setSelection(new Set([id]), id);
                revealSettled(model.indexOfId(id));
            };
            const undo = () => afterHistory(model.undo());
            const redo = () => afterHistory(model.redo());

            // ---- playback ----------------------------------------------------

            const audio = () => (session.audio ? session.audio() : null);
            const playhead = () => { const a = audio(); return a && Number.isFinite(a.currentTime) ? a.currentTime : null; };
            const playheadInside = (seg) => {
                const t = playhead();
                return t != null && Number.isFinite(seg.start_time) && Number.isFinite(seg.end_time) && t > seg.start_time && t < seg.end_time ? t : undefined;
            };

            let loopRange = null;       // { start, end } while a segment is looped
            let queue = null;           // [{ start, end }] while a speaker is previewed
            let monitorFrame = 0;

            const stopMonitor = () => {
                if (monitorFrame) cancelAnimationFrame(monitorFrame);
                monitorFrame = 0;
            };
            const monitor = () => {
                monitorFrame = 0;
                const a = audio();
                if (!a || destroyed || (!loopRange && !queue)) return;
                if (!a.paused) {
                    const t = a.currentTime;
                    if (queue) {
                        if (t >= queue[0].end || t < queue[0].start - 0.5) {
                            queue.shift();
                            if (queue.length) a.currentTime = queue[0].start;
                            else { queue = null; a.pause(); }
                        }
                    } else if (loopRange && (t >= loopRange.end || t < loopRange.start - 0.05)) {
                        a.currentTime = loopRange.start;
                    }
                }
                if (loopRange || queue) monitorFrame = requestAnimationFrame(monitor);
            };
            const startMonitor = () => { if (!monitorFrame) monitorFrame = requestAnimationFrame(monitor); };

            const stopLoop = () => {
                loopRange = null;
                looping.value = false;
                if (session.timeline) session.timeline.refresh();
            };

            const playFromTime = (time) => {
                const a = audio();
                if (!a || !Number.isFinite(time)) return;
                queue = null;
                if (loopRange && (time < loopRange.start || time >= loopRange.end)) stopLoop();
                a.currentTime = time;
                const playing = a.play();
                if (playing && playing.catch) playing.catch(() => {});
            };

            const togglePlay = () => {
                const a = audio();
                if (!a) return;
                if (a.paused) { const p = a.play(); if (p && p.catch) p.catch(() => {}); } else a.pause();
            };

            const toggleLoop = () => {
                if (loopRange) { stopLoop(); return; }
                const index = currentIndex();
                const seg = index >= 0 ? model.get(index) : null;
                const a = audio();
                if (!a || !seg || !Number.isFinite(seg.start_time) || !(seg.end_time > seg.start_time)) return;
                queue = null;
                loopRange = { start: seg.start_time, end: seg.end_time };
                looping.value = true;
                if (session.timeline) session.timeline.refresh();
                a.currentTime = seg.start_time;
                const p = a.play();
                if (p && p.catch) p.catch(() => {});
                startMonitor();
            };

            // Play a few of the speaker's longest segments, to check a voice match by ear.
            const previewSpeaker = (name) => {
                const a = audio();
                const parts = longestSegments(model.segments, name);
                if (!a || !parts.length) return;
                stopLoop();
                queue = parts;
                a.currentTime = parts[0].start;
                const p = a.play();
                if (p && p.catch) p.catch(() => {});
                startMonitor();
            };

            const onTimeUpdate = () => {
                const t = playhead();
                const index = t == null ? -1 : model.indexAtTime(t);
                const seg = index >= 0 ? model.get(index) : null;
                const id = seg && Number.isFinite(seg.end_time) && t < seg.end_time + 0.25 ? seg.id : null;
                if (id === activeId) return;
                activeId = id;
                bump();
                const a = audio();
                if (id != null && follow.value && a && !a.paused && !queue && !isTypingTarget(document.activeElement)) reveal(index);
            };

            const toggleFollow = () => {
                follow.value = !follow.value;
                try { localStorage.setItem(FOLLOW_KEY, follow.value ? '1' : '0'); } catch (e) { /* storage unavailable */ }
                if (follow.value && activeId != null) reveal(model.indexOfId(activeId));
            };

            let boundAudio = null;
            const bindAudio = () => {
                const a = audio();
                if (a === boundAudio) return;
                if (boundAudio) {
                    boundAudio.removeEventListener('timeupdate', onTimeUpdate);
                    boundAudio.removeEventListener('seeked', onTimeUpdate);
                }
                boundAudio = a;
                if (a) {
                    a.addEventListener('timeupdate', onTimeUpdate);
                    a.addEventListener('seeked', onTimeUpdate);
                }
            };

            // ---- editing commands -------------------------------------------

            const splitAt = (index, offset) => {
                const seg = model.get(index);
                if (!seg) return;
                const second = model.split(index, offset, { time: playheadInside(seg) });
                if (second < 0) return;
                const id = model.get(second).id;
                setSelection(new Set([id]), id);
                focusText(id, 0);
            };

            const mergeIntoPrevious = (index) => {
                const result = model.mergeWithPrevious(index);
                if (!result) return;
                const id = model.get(result.index).id;
                setSelection(new Set([id]), id);
                focusText(id, result.caret);
            };

            const mergeIntoNext = (index) => {
                const seg = model.get(index);
                const at = seg ? seg.sentence.length : 0;
                const result = model.mergeWithNext(index);
                if (!result) return;
                const id = model.get(result.index).id;
                setSelection(new Set([id]), id);
                focusText(id, Math.min(at, result.caret));
            };

            const mergeSelection = () => {
                const list = selectedIndices();
                if (list.length < 2) return;
                const result = model.merge(list);
                if (!result) return;
                const id = model.get(result.index).id;
                setSelection(new Set([id]), id);
                revealSettled(result.index);
            };

            const removeIndices = (list) => {
                if (!list.length) return;
                const after = Math.min(list[0], model.length - list.length - 1);
                model.remove(list);
                const seg = model.get(Math.max(0, after));
                setSelection(seg ? new Set([seg.id]) : new Set(), seg ? seg.id : null);
                focusList();
            };
            const removeSelection = () => removeIndices(selectedIndices());

            const setEdgeToPlayhead = (edge, index = currentIndex()) => {
                const t = playhead();
                if (index < 0 || t == null) return;
                const id = model.get(index).id;
                const moved = model.moveEdge(index, edge, t, { linked: true });
                if (moved >= 0 && moved !== index) revealSettled(moved);
                if (moved >= 0) setSelection(new Set([id]), id);
            };

            const nudge = (edge, delta) => {
                const index = currentIndex();
                const seg = index >= 0 ? model.get(index) : null;
                const value = seg ? seg[`${edge}_time`] : null;
                if (!Number.isFinite(value)) return;
                const moved = model.moveEdge(index, edge, value + delta, { linked: true, burst: true });
                if (moved >= 0 && moved !== index) reveal(moved);
            };

            const splitAtPlayhead = () => {
                const index = currentIndex();
                const seg = index >= 0 ? model.get(index) : null;
                if (!seg) return;
                const t = playheadInside(seg);
                if (t === undefined) {
                    session.toast(translate('asrEditor.playheadOutside'));
                    return;
                }
                const share = (t - seg.start_time) / (seg.end_time - seg.start_time);
                // The nearest word boundary to the same share of the text.
                let offset = Math.round(seg.sentence.length * share);
                const space = seg.sentence.indexOf(' ', offset);
                const before = seg.sentence.lastIndexOf(' ', offset);
                if (space >= 0 && (before < 0 || space - offset <= offset - before)) offset = space;
                else if (before >= 0) offset = before;
                splitAt(index, offset);
            };

            const addSegment = (afterIndex = null) => {
                const list = selectedIndices();
                const base = afterIndex != null ? afterIndex : (list.length === 1 ? list[0] : -1);
                let start;
                let end;
                let speaker;
                if (base >= 0) {
                    const seg = model.get(base);
                    const next = model.get(base + 1);
                    start = Number.isFinite(seg.end_time) ? seg.end_time : (seg.start_time || 0);
                    end = next && Number.isFinite(next.start_time) && next.start_time > start ? next.start_time : start + 2;
                    speaker = seg.speaker;
                } else {
                    const t = playhead();
                    const last = model.get(model.length - 1);
                    if (t != null && t > 0) {
                        start = t;
                        const at = model.indexAtTime(t);
                        speaker = at >= 0 ? model.get(at).speaker : null;
                    } else {
                        start = last && Number.isFinite(last.end_time) ? last.end_time : 0;
                        speaker = last ? last.speaker : null;
                    }
                    end = start + 2;
                }
                if (!speaker) speaker = model.speakers()[0] || translate('transcription.speaker', { number: 1 });
                const index = model.add(start, end, speaker);
                const id = model.get(index).id;
                setSelection(new Set([id]), id);
                revealSettled(index).then(() => focusText(id, 0));
            };

            const goToNextProblem = () => {
                const list = selectedIndices();
                const index = model.nextProblem(list.length ? list[0] : -1);
                if (index < 0) return;
                const id = model.get(index).id;
                setSelection(new Set([id]), id);
                flash(id);
                revealSettled(index, { center: true });
                focusList();
            };

            const assignSpeakerByKey = (number) => {
                const name = model.speakers()[number - 1];
                const list = selectedIndices();
                if (name && list.length) model.setSpeaker(list, name);
            };

            // ---- popovers: speaker picker, times, segment menu, shortcuts ----

            // scope 'speaker': the picker was opened from a lane header of the
            // timeline and changes every segment of popover.speaker
            const popover = reactive({ kind: null, id: null, style: {}, sheet: false, speaker: '', targets: [], scope: '' });
            const pickerQuery = ref('');
            const pickerIndex = ref(0);
            const pickerAll = ref(false);
            const timeStart = ref('');
            const timeEnd = ref('');
            const timeLinked = ref(true);
            const timeError = ref(false);
            let popoverReturn = null;

            const placePopover = (anchorEl, width, height) => {
                popover.sheet = isNarrow();
                if (popover.sheet || !anchorEl) { popover.style = {}; return; }
                const rect = anchorEl.getBoundingClientRect();
                const vw = window.innerWidth;
                const vh = window.innerHeight;
                const left = Math.max(8, Math.min(rect.left, vw - width - 8));
                const below = vh - rect.bottom - 12;
                const above = rect.top - 12;
                const style = { left: `${left}px`, width: `${width}px` };
                if (below >= Math.min(height, 220) || below >= above) {
                    style.top = `${rect.bottom + 4}px`;
                    style.maxHeight = `${Math.max(160, below)}px`;
                } else {
                    style.bottom = `${vh - rect.top + 4}px`;
                    style.maxHeight = `${Math.max(160, above)}px`;
                }
                popover.style = style;
            };

            const closePopover = (returnFocus = true) => {
                if (!popover.kind) return;
                popover.kind = null;
                const back = popoverReturn;
                popoverReturn = null;
                if (returnFocus && back && back.isConnected) back.focus({ preventScroll: true });
                else if (returnFocus) focusList();
            };

            const openPopover = (kind, anchorEl, width, height, extra = {}) => {
                popoverReturn = anchorEl || null;
                Object.assign(popover, { kind, id: null, speaker: '', targets: [], scope: '' }, extra);
                placePopover(anchorEl, width, height);
                nextTick(() => {
                    const el = popEl.value;
                    if (!el) return;
                    const first = el.querySelector('[data-autofocus]') || el.querySelector('button, input');
                    if (first && !(popover.sheet && kind === 'picker')) first.focus({ preventScroll: true });
                    else el.focus({ preventScroll: true });
                });
            };

            const suggestionOf = (name) => bestSuggestion(session.voice.value, name, model.speakers());
            const suggestionLabel = (match) => translate('asrEditor.suggestion', { name: match.name, percent: percent(Math.round(match.similarity)) });

            const acceptSuggestion = (label) => {
                const match = suggestionOf(label);
                if (!match) return;
                // When the name is already in the recording this merges the two
                // speakers: one name and one speaker id for both, in one step.
                const changed = model.renameSpeaker(label, match.name, {
                    speakerId: match.speaker_id,
                    also: match.same,
                    match: { label, name: match.name, speakerId: match.speaker_id },
                });
                if (popover.kind === 'picker') closePopover();
                if (!changed) return;
                // The toast takes the rename back with one click, as long as
                // it is still the last edit.
                const rev = model.rev;
                session.toast(translate(match.merge ? 'asrEditor.suggestionMerged' : 'asrEditor.suggestionAccepted', { label, name: match.name }), {
                    action: () => { if (model.rev === rev) undo(); },
                    actionLabel: translate('asrEditor.undoAction'),
                });
            };

            const openPicker = (row, event) => {
                const index = model.indexOfId(row.id);
                if (index < 0) return;
                const inSelection = selected.has(row.id) && selected.size > 1;
                if (!inSelection) setSelection(new Set([row.id]), row.id);
                pickerQuery.value = '';
                pickerIndex.value = 0;
                pickerAll.value = false;
                openPopover('picker', event.currentTarget, 280, 360, {
                    id: row.id, speaker: row.speaker, targets: inSelection ? [...selected] : [row.id],
                });
            };

            const openPickerForSelection = (event) => {
                const list = selectedIndices();
                if (!list.length) return;
                pickerQuery.value = '';
                pickerIndex.value = 0;
                pickerAll.value = false;
                const speakersOfSelection = new Set(list.map((i) => model.get(i).speaker));
                openPopover('picker', event.currentTarget, 280, 360, {
                    id: null, speaker: speakersOfSelection.size === 1 ? [...speakersOfSelection][0] : '', targets: [...selected],
                });
            };

            // The picker for a whole speaker, opened from its lane header in the
            // timeline: the choice renames the speaker in every segment.
            const openLanePicker = (name, anchorEl) => {
                if (!model.speakers().includes(name) && !(name === '' && model.segments.some((seg) => !seg.speaker))) return;
                pickerQuery.value = '';
                pickerIndex.value = 0;
                pickerAll.value = false;
                openPopover('picker', anchorEl, 280, 360, { id: null, speaker: name, targets: [], scope: 'speaker' });
            };

            const pickerHeading = computed(() => {
                if (popover.kind !== 'picker' || popover.scope !== 'speaker') return '';
                return popover.speaker ? translate('asrEditor.renameSpeaker', { name: popover.speaker }) : translate('asrEditor.setSpeaker');
            });

            const pickerOptions = computed(() => {
                void tick.value;
                if (popover.kind !== 'picker') return [];
                const whole = popover.scope === 'speaker';
                // Saved speakers whose voice matches the speaker being changed come first.
                const voice = (popover.speaker && (session.voice.value || {})[popover.speaker]) || [];
                const options = buildSpeakerOptions({
                    recording: model.speakers(), saved: session.savedSpeakers.value || [], voice, query: pickerQuery.value,
                });
                let last = null;
                // For a whole speaker, its own name is no choice, another
                // speaker of the recording is a merge and says so, and the
                // number keys choose nothing (a merge takes a deliberate choice).
                const offered = whole ? options.filter((option) => !(option.kind === 'recording' && option.name === popover.speaker)) : options;
                return offered.map((option) => {
                    const merge = whole && option.kind === 'recording';
                    const group = option.kind === 'recording' ? 'recording' : (option.kind === 'new' ? 'new' : 'saved');
                    const header = group !== last && group !== 'new' ? translate(group === 'recording' ? 'asrEditor.pickerRecording' : 'asrEditor.pickerSaved') : null;
                    last = group;
                    return {
                        ...option,
                        key: whole ? null : option.key,
                        merge,
                        header,
                        label: option.kind === 'new' ? translate('asrEditor.pickerUseName', { name: option.name })
                            : (merge ? translate('asrEditor.suggestionMergeInto', { name: option.name }) : option.name),
                        note: option.kind === 'voice' ? `${translate('speakerModal.voiceMatch')} ${percent(Math.round(option.similarity))}` : '',
                        color: option.kind === 'recording' ? colorOf(option.name) : '',
                        current: option.name === popover.speaker,
                    };
                });
            });

            const choosePickerOption = (option) => {
                if (!option) return;
                const options = option.id != null ? { speakerId: option.id } : {};
                if (popover.scope === 'speaker') {
                    const from = popover.speaker;
                    // The offered voice match, chosen from the list: accepted
                    // as with its Accept button, so that the match is recorded
                    const best = option.kind === 'voice' ? suggestionOf(from) : null;
                    if (best && best.name === option.name && best.speaker_id === option.id) {
                        acceptSuggestion(from);
                        return;
                    }
                    // Merged speakers share the link to a saved speaker too
                    if (option.merge && model.speakerIdOf(option.name) != null) options.speakerId = model.speakerIdOf(option.name);
                    const changed = model.renameSpeaker(from, option.name, options);
                    closePopover();
                    if (!changed) return;
                    // As after an accepted voice match: the message takes the
                    // change back with one click while it is the last edit.
                    const rev = model.rev;
                    session.toast(translate(option.merge ? 'asrEditor.suggestionMerged' : 'asrEditor.suggestionAccepted', {
                        label: from || translate('asrEditor.noSpeaker'), name: option.name,
                    }), {
                        action: () => { if (model.rev === rev) undo(); },
                        actionLabel: translate('asrEditor.undoAction'),
                    });
                    return;
                }
                if (pickerAll.value && popover.speaker) {
                    model.renameSpeaker(popover.speaker, option.name, options);
                } else {
                    const list = popover.targets.map((id) => model.indexOfId(id)).filter((i) => i >= 0);
                    model.setSpeaker(list, option.name, options);
                }
                closePopover();
            };

            const onPickerKeydown = (event) => {
                const options = pickerOptions.value;
                if (event.key === 'ArrowDown') {
                    event.preventDefault();
                    pickerIndex.value = options.length ? (pickerIndex.value + 1) % options.length : 0;
                } else if (event.key === 'ArrowUp') {
                    event.preventDefault();
                    pickerIndex.value = options.length ? (pickerIndex.value - 1 + options.length) % options.length : 0;
                } else if (event.key === 'Enter') {
                    event.preventDefault();
                    choosePickerOption(options[Math.min(pickerIndex.value, options.length - 1)]);
                } else if (!pickerQuery.value && /^[1-9]$/.test(event.key) && !event.ctrlKey && !event.metaKey && !event.altKey) {
                    const option = options.find((o) => o.key === Number(event.key));
                    if (option) {
                        event.preventDefault();
                        choosePickerOption(option);
                    }
                }
            };
            const onPickerInput = () => { pickerIndex.value = 0; };

            const openTimes = (id, anchorEl) => {
                const index = model.indexOfId(id);
                const seg = index >= 0 ? model.get(index) : null;
                if (!seg) return;
                setSelection(new Set([id]), id);
                timeStart.value = formatExact(seg.start_time);
                timeEnd.value = formatExact(seg.end_time);
                timeError.value = false;
                openPopover('times', anchorEl || rowElement(id), 260, 240, { id });
            };

            const applyTimes = () => {
                const index = model.indexOfId(popover.id);
                const start = parseTime(timeStart.value);
                const end = parseTime(timeEnd.value);
                if (index < 0 || start == null || end == null) {
                    timeError.value = true;
                    return;
                }
                const seg = model.get(index);
                const id = seg.id;
                let at = index;
                if (start !== seg.start_time && end !== seg.end_time) {
                    at = model.setTimes(index, start, end);
                } else if (start !== seg.start_time) {
                    at = model.moveEdge(index, 'start', start, { linked: timeLinked.value });
                } else if (end !== seg.end_time) {
                    at = model.moveEdge(index, 'end', end, { linked: timeLinked.value });
                }
                closePopover();
                if (at >= 0 && at !== index) revealSettled(model.indexOfId(id));
            };

            const timesFromPlayhead = (edge) => {
                const t = playhead();
                if (t == null) return;
                if (edge === 'start') timeStart.value = formatExact(t); else timeEnd.value = formatExact(t);
            };

            const openMenu = (row, event) => {
                // "Split at cursor" uses the cursor of this row's text.
                const rowEl = rowElement(row.id);
                const box = rowEl && rowEl.querySelector('textarea');
                if (box && document.activeElement === box) caret = { id: row.id, pos: box.selectionStart || 0 };
                if (!selected.has(row.id) || selected.size > 1) setSelection(new Set([row.id]), row.id);
                openPopover('menu', event.currentTarget, 240, 380, { id: row.id });
            };

            const menuItems = computed(() => {
                void tick.value;
                if (popover.kind !== 'menu') return [];
                const index = model.indexOfId(popover.id);
                const seg = index >= 0 ? model.get(index) : null;
                if (!seg) return [];
                const hasAudio = !!audio();
                const hasCaret = caret.id === seg.id;
                return [
                    { action: 'play', icon: 'fa-play', label: translate('help.playFromHere'), disabled: !hasAudio },
                    { action: 'split', icon: 'fa-scissors', label: translate('asrEditor.splitAtCursor'), disabled: !hasCaret && playheadInside(seg) === undefined },
                    { action: 'merge-prev', icon: 'fa-arrow-up', label: translate('asrEditor.mergeWithPrevious'), disabled: index === 0 },
                    { action: 'merge-next', icon: 'fa-arrow-down', label: translate('asrEditor.mergeWithNext'), disabled: index >= model.length - 1 },
                    { action: 'start', icon: 'fa-right-to-bracket', label: translate('asrEditor.setStartToPlayhead'), disabled: !hasAudio },
                    { action: 'end', icon: 'fa-right-from-bracket', label: translate('asrEditor.setEndToPlayhead'), disabled: !hasAudio },
                    { action: 'times', icon: 'fa-clock', label: translate('asrEditor.editTimes') },
                    { action: 'add', icon: 'fa-plus', label: translate('help.addSegmentBelow') },
                    { action: 'delete', icon: 'fa-trash', label: translate('help.deleteSegment'), danger: true },
                ];
            });

            const menuAction = (action) => {
                const id = popover.id;
                const index = model.indexOfId(id);
                const anchorEl = popoverReturn;
                closePopover(false);
                if (index < 0) return;
                const seg = model.get(index);
                switch (action) {
                    case 'play': playFromTime(seg.start_time); focusList(); break;
                    case 'split':
                        if (caret.id === id) splitAt(index, caret.pos); else splitAtPlayhead();
                        break;
                    case 'merge-prev': mergeIntoPrevious(index); break;
                    case 'merge-next': mergeIntoNext(index); break;
                    case 'start': setEdgeToPlayhead('start', index); focusList(); break;
                    case 'end': setEdgeToPlayhead('end', index); focusList(); break;
                    case 'times': openTimes(id, anchorEl && anchorEl.isConnected ? anchorEl : null); break;
                    case 'add': addSegment(index); break;
                    case 'delete': removeIndices([index]); break;
                    default:
                }
            };

            const onMenuKeydown = (event) => {
                if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
                const items = [...(popEl.value ? popEl.value.querySelectorAll('button:not(:disabled)') : [])];
                if (!items.length) return;
                event.preventDefault();
                const at = items.indexOf(document.activeElement);
                const next = event.key === 'ArrowDown' ? (at + 1) % items.length : (at - 1 + items.length) % items.length;
                items[next].focus();
            };

            const shortcutList = computed(() => {
                const mod = isMac() ? '⌘' : 'Ctrl';
                const row = (keys, key) => ({ keys, label: translate(`asrEditor.keys.${key}`) });
                return [
                    row(['Enter'], 'split'),
                    row(['Backspace'], 'mergeBack'),
                    row(['Esc'], 'leaveText'),
                    row(['↑', '↓'], 'moveSelection'),
                    row(['Shift', translate('asrEditor.keys.click')], 'range'),
                    row([mod, translate('asrEditor.keys.click')], 'toggle'),
                    row(['1', '…', '9'], 'speaker'),
                    row(['Space'], 'play'),
                    row(['L'], 'loop'),
                    row(['['], 'setStart'),
                    row([']'], 'setEnd'),
                    row(['Alt', '←', '→'], 'nudgeStart'),
                    row(['Alt', 'Shift', '←', '→'], 'nudgeEnd'),
                    row([translate('asrEditor.keys.drag')], 'dragBlock'),
                    row(['Alt', translate('asrEditor.keys.drag')], 'dragAlone'),
                    row([mod, translate('asrEditor.keys.drag')], 'dragNoSnap'),
                    row([mod, translate('asrEditor.keys.wheel')], 'zoom'),
                    row(['←', '→'], 'seek'),
                    row(['S'], 'splitPlayhead'),
                    row(['M'], 'merge'),
                    row(['T'], 'times'),
                    row(['Delete'], 'remove'),
                    row([mod, 'Z'], 'undo'),
                    row(isMac() ? [mod, 'Shift', 'Z'] : [mod, 'Y'], 'redo'),
                    row([mod, 'F'], 'find'),
                    row([mod, 'S'], 'save'),
                ];
            });
            const openHelp = (event) => openPopover('help', event.currentTarget, 360, 520);

            // ---- row events ----------------------------------------------------

            const rememberCaret = (row, event) => {
                caret = { id: row.id, pos: event.target.selectionStart || 0 };
                if (event.type === 'mouseup') findFocus = null;
            };

            const onRowMouseDown = (row, event) => {
                // Shift or Ctrl/Cmd on another row is a selection click, not a
                // text selection.
                if ((event.shiftKey || event.ctrlKey || event.metaKey) && !(selected.size === 1 && selected.has(row.id))) {
                    event.preventDefault();
                }
            };

            const onRowClick = (row, event) => {
                const index = model.indexOfId(row.id);
                if (index < 0) return;
                const shift = event.shiftKey;
                const toggle = event.ctrlKey || event.metaKey;
                // A plain click into the text keeps the cursor there. The text
                // may have focus although the click is reported on the row
                // (the pointer came up beside it), so the focus decides.
                const active = document.activeElement;
                const inText = isTypingTarget(event.target)
                    || (isTypingTarget(active) && event.currentTarget.contains(active));
                if (!(!shift && !toggle && selected.size === 1 && selected.has(row.id))) selectIndex(index, { shift, toggle });
                // Otherwise keys go to the list.
                if (!inText || shift || toggle) focusList();
            };

            const onTextFocus = (row, event) => {
                rememberCaret(row, event);
                if (!selected.has(row.id)) setSelection(new Set([row.id]), row.id);
            };

            const onTextBlur = (row, event) => {
                rememberCaret(row, event);
                findFocus = null;
                model.endBurst();
            };

            const onTextInput = (row, event) => {
                const box = event.target;
                const index = model.indexOfId(row.id);
                if (index < 0) return;
                findFocus = null;
                let value = box.value;
                if (value.includes('\n') && !model.get(index).sentence.includes('\n')) {
                    // Pasted line breaks: a segment is one line.
                    const at = box.selectionStart;
                    value = value.replace(/\s*\n\s*/g, ' ');
                    box.value = value;
                    try { box.setSelectionRange(Math.min(at, value.length), Math.min(at, value.length)); } catch (e) { /* ignore */ }
                }
                typing = true;
                try {
                    model.setText(index, value);
                } finally {
                    typing = false;
                }
                caret = { id: row.id, pos: box.selectionStart || 0 };
                sizeText(box, true);
            };

            const onTextKeydown = (row, event) => {
                if (event.isComposing || event.keyCode === 229) return;
                const box = event.target;
                const index = model.indexOfId(row.id);
                if (index < 0) return;
                const plain = !event.ctrlKey && !event.metaKey && !event.altKey;
                if (event.key === 'Enter' && plain && findFocus === row.id && session.findStep) {
                    // The text shows a find match: Enter goes to the next
                    // match (Shift+Enter to the previous one), as in the find bar.
                    event.preventDefault();
                    session.findStep(event.shiftKey ? -1 : 1);
                    return;
                }
                if (!['Shift', 'Control', 'Alt', 'Meta'].includes(event.key)) findFocus = null;
                if (event.key === 'Enter' && plain) {
                    if (event.shiftKey) {
                        // No line breaks inside a segment, unless it has them already.
                        if (!box.value.includes('\n')) event.preventDefault();
                        return;
                    }
                    event.preventDefault();
                    splitAt(index, box.selectionStart);
                } else if (event.key === 'Backspace' && plain && !event.shiftKey && box.selectionStart === 0 && box.selectionEnd === 0) {
                    if (index > 0) {
                        event.preventDefault();
                        mergeIntoPrevious(index);
                    }
                } else if (event.key === 'Escape') {
                    event.preventDefault();
                    event.stopPropagation();
                    box.blur();
                    focusList();
                } else if (event.key === 'ArrowUp' && plain && !event.shiftKey && box.selectionStart === 0 && box.selectionEnd === 0 && index > 0) {
                    event.preventDefault();
                    const id = model.get(index - 1).id;
                    setSelection(new Set([id]), id);
                    focusText(id, null);
                } else if (event.key === 'ArrowDown' && plain && !event.shiftKey && box.selectionStart === box.value.length && box.selectionEnd === box.value.length && index < model.length - 1) {
                    event.preventDefault();
                    const id = model.get(index + 1).id;
                    setSelection(new Set([id]), id);
                    focusText(id, 0);
                }
            };

            // ---- keyboard ------------------------------------------------------

            const isTopmost = () => {
                const el = root.value;
                if (!el) return false;
                // Another dialog shown above the editor takes the keys. Dialogs
                // that are in the page but hidden do not count.
                const shown = [...document.querySelectorAll('.modal-overlay')]
                    .filter((overlay) => overlay.getClientRects().length > 0 && getComputedStyle(overlay).visibility !== 'hidden');
                const top = shown[shown.length - 1];
                return !top || top.contains(el);
            };

            const moveSelection = (step, extend) => {
                const list = selectedIndices();
                let from;
                if (!list.length) from = activeId != null ? model.indexOfId(activeId) - step : (step > 0 ? -1 : model.length);
                else if (extend && anchorId != null) {
                    const anchorIndex = model.indexOfId(anchorId);
                    from = list[0] < anchorIndex ? list[0] : list[list.length - 1];
                    if (list.length === 1) from = list[0];
                } else from = step > 0 ? list[list.length - 1] : list[0];
                const to = Math.min(model.length - 1, Math.max(0, from + step));
                if (to < 0 || to >= model.length) return;
                if (extend && list.length) selectIndex(to, { shift: true });
                else selectIndex(to);
                reveal(to);
            };

            const onKeydown = (event) => {
                if (destroyed || !isTopmost()) return;
                const key = event.key;
                const lowerKey = (key || '').toLowerCase();
                const mod = event.ctrlKey || event.metaKey;
                const target = event.target;
                const typingHere = isTypingTarget(target);
                const inRows = !!(target && target.classList && target.classList.contains('asr-seg__text'));

                if (mod && !event.altKey && (lowerKey === 'z' || lowerKey === 'y')) {
                    // Other text fields (find, times, the picker) keep their own undo.
                    if (typingHere && !inRows) return;
                    event.preventDefault();
                    if (lowerKey === 'y' || event.shiftKey) redo(); else undo();
                    return;
                }
                if (popover.kind) {
                    if (key === 'Escape') {
                        event.preventDefault();
                        event.stopPropagation();
                        closePopover();
                    }
                    return;
                }
                if (typingHere || mod) return;
                // Space and Enter on a focused control belong to that control.
                const tag = target && target.tagName ? target.tagName.toUpperCase() : '';
                const onControl = tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag === 'SUMMARY';
                if (onControl && (key === ' ' || key === 'Enter')) return;
                // Arrow keys belong to a slider or a checkbox that has focus (the volume).
                if (tag === 'INPUT' && key.startsWith('Arrow')) return;

                if (event.altKey) {
                    if (key === 'ArrowLeft' || key === 'ArrowRight') {
                        event.preventDefault();
                        nudge(event.shiftKey ? 'end' : 'start', key === 'ArrowLeft' ? -NUDGE : NUDGE);
                    }
                    return;
                }

                let handled = true;
                if (key === ' ') togglePlay();
                else if (lowerKey === 'l') toggleLoop();
                else if (key === '[') setEdgeToPlayhead('start');
                else if (key === ']') setEdgeToPlayhead('end');
                else if (lowerKey === 's') splitAtPlayhead();
                else if (lowerKey === 'm') mergeSelection();
                else if (lowerKey === 't') { const i = currentIndex(); if (i >= 0) openTimes(model.get(i).id, null); }
                else if (key === 'Delete') removeSelection();
                else if (/^[1-9]$/.test(key)) assignSpeakerByKey(Number(key));
                else if (key === 'ArrowDown') moveSelection(1, event.shiftKey);
                else if (key === 'ArrowUp') moveSelection(-1, event.shiftKey);
                else if (key === 'Enter') {
                    const list = selectedIndices();
                    if (list.length === 1) focusText(model.get(list[0]).id, null); else handled = false;
                } else if (key === 'Escape') {
                    if (selected.size) clearSelection(); else handled = false;
                } else if (key === '?') {
                    openPopover('help', root.value ? root.value.querySelector('[data-asr-help]') : null, 360, 520);
                } else handled = false;
                if (handled) event.preventDefault();
            };

            // ---- rows for the template ------------------------------------------

            const problemText = (mask) => {
                const parts = [];
                if (mask & PROBLEM.OVERLAP) parts.push(translate('asrEditor.problemOverlap'));
                if (mask & PROBLEM.DURATION) parts.push(translate('asrEditor.problemDuration'));
                if (mask & PROBLEM.ORDER) parts.push(translate('asrEditor.problemOrder'));
                return parts.join(' ');
            };

            const rows = computed(() => {
                void tick.value;
                const { start, end } = range.value;
                const segs = model.segments;
                const out = [];
                const matches = new Map();   // speaker -> its voice match, looked up once per render
                const matchOf = (name) => {
                    if (!matches.has(name)) matches.set(name, suggestionOf(name));
                    return matches.get(name);
                };
                for (let i = start; i < Math.min(end, segs.length); i++) {
                    const seg = segs[i];
                    const previous = segs[i - 1];
                    const continued = !!previous && previous.speaker === seg.speaker;
                    const mask = model.problemAt(i);
                    // A voice match of an unnamed speaker is marked on each of its
                    // chips and offered in full once, on the speaker's first segment
                    // (and in the picker).
                    const match = matchOf(seg.speaker);
                    const suggestion = match && model.firstIdOfSpeaker(seg.speaker) === seg.id ? match : null;
                    out.push({
                        id: seg.id,
                        index: i,
                        number: i + 1,
                        speaker: seg.speaker,
                        color: colorOf(seg.speaker),
                        // The segment itself: the template reads its text at
                        // render time. A copy taken here would be stale after
                        // typing (which does not recompute the rows) and a
                        // later render would write the old text back.
                        seg,
                        clock: formatClock(seg.start_time),
                        exact: `${formatExact(seg.start_time)} – ${formatExact(seg.end_time)}`,
                        continued,
                        selected: selected.has(seg.id),
                        active: seg.id === activeId,
                        flash: seg.id === flashId,
                        problem: mask,
                        problemText: mask ? problemText(mask) : '',
                        match: match ? suggestionLabel(match) : '',
                        suggestion,
                        suggestionLabel: suggestion ? suggestionLabel(suggestion) : '',
                        suggestionMerge: suggestion && suggestion.merge ? translate('asrEditor.suggestionMergeInto', { name: suggestion.name }) : '',
                    });
                }
                return out;
            });

            const pickerSuggestion = computed(() => {
                void tick.value;
                if (popover.kind !== 'picker' || !popover.speaker) return null;
                const match = suggestionOf(popover.speaker);
                return match ? {
                    label: suggestionLabel(match), speaker: popover.speaker,
                    merge: match.merge ? translate('asrEditor.suggestionMergeInto', { name: match.name }) : '',
                } : null;
            });

            const draftOffer = computed(() => {
                const draft = session.draft.value;
                if (!draft) return null;
                let when = '';
                try {
                    when = new Date(draft.savedAt).toLocaleString(window.i18n?.getIntlLocale?.(), { dateStyle: 'medium', timeStyle: 'short' });
                } catch (e) { /* keep empty */ }
                return { when };
            });

            // ---- the session's way in --------------------------------------------

            session.pane = {
                /** Show a segment; optionally select it, highlight it, or put the cursor in its text. */
                revealIndex(index, { select = true, highlight = false, textRange = null } = {}) {
                    const seg = model.get(index);
                    if (!seg) return;
                    if (select) setSelection(new Set([seg.id]), seg.id);
                    if (highlight) flash(seg.id);
                    revealSettled(index, { center: true }).then(() => {
                        if (!textRange || destroyed) return;
                        focusText(seg.id, textRange[0], textRange[1]).then(() => { findFocus = seg.id; });
                    });
                },
                scrollTop: () => (scroller.value ? scroller.value.scrollTop : 0),
                setScrollTop(top) {
                    if (!scroller.value) return;
                    scroller.value.scrollTop = top;
                    updateWindow();
                },
                clearHighlight() { flashId = null; bump(); },
                refresh: bump,

                // For the timeline (timeline.js)
                selection: () => selected,
                anchor: () => (anchorId != null && selected.has(anchorId) ? anchorId : (selected.size ? selected.values().next().value : null)),
                /** A click on a block: select it here as a click on its row would, and show the row. */
                selectFromTimeline(index, { shift = false, toggle = false } = {}) {
                    if (index < 0 || index >= model.length) return;
                    selectingFromTimeline = true;
                    try { selectIndex(index, { shift, toggle }); } finally { selectingFromTimeline = false; }
                    reveal(index);
                },
                clearSelection,
                loopRange: () => loopRange,
                /** The playhead was moved by hand: a loop it left is over. */
                leaveLoop(time) {
                    queue = null;
                    if (loopRange && (time < loopRange.start || time >= loopRange.end)) stopLoop();
                },
                following: () => follow.value,
                /** Play one stretch of the audio and stop at its end. */
                playRange(start, end) {
                    this.playRanges([{ start, end }]);
                },
                /** Play stretches one after the other and stop after the last (the preview of a trim). */
                playRanges(ranges) {
                    const a = audio();
                    const parts = (ranges || []).filter((r) => r && Number.isFinite(r.start) && r.end > r.start)
                        .map((r) => ({ start: r.start, end: r.end }));
                    if (!a || !parts.length) return;
                    stopLoop();
                    queue = parts;
                    a.currentTime = parts[0].start;
                    const p = a.play();
                    if (p && p.catch) p.catch(() => {});
                    startMonitor();
                },
                acceptSuggestion,
                previewSpeaker,
                openLanePicker,
            };

            onMounted(() => {
                const el = scroller.value;
                rebuildHeights();
                if (el && typeof ResizeObserver !== 'undefined') {
                    widthObserver = new ResizeObserver(onResize);
                    widthObserver.observe(el);
                }
                onResize();
                updateWindow();
                window.addEventListener('keydown', onKeydown);
                bindAudio();
                nextTick(() => {
                    syncRows();
                    if (session.initialIndex != null) {
                        session.pane.revealIndex(session.initialIndex, { highlight: true });
                    } else if (session.initialScrollTop) {
                        session.pane.setScrollTop(session.initialScrollTop);
                    }
                });
            });

            onUpdated(() => {
                syncRows();
                bindAudio();
            });

            onBeforeUnmount(() => {
                destroyed = true;
                stopListening();
                stopMonitor();
                clearTimeout(flashTimer);
                if (scrollFrame) cancelAnimationFrame(scrollFrame);
                window.removeEventListener('keydown', onKeydown);
                if (boundAudio) {
                    boundAudio.removeEventListener('timeupdate', onTimeUpdate);
                    boundAudio.removeEventListener('seeked', onTimeUpdate);
                }
                if (rowObserver) rowObserver.disconnect();
                if (widthObserver) widthObserver.disconnect();
                if (session.pane) session.pane = null;
            });

            return {
                root, scroller, popEl, pickerInput,
                rows, rowCount, topPad, totalHeight, onScroll,
                canUndo, canRedo, problemCount, selectionCount, looping, follow, narrow,
                undo, redo, addSegment, goToNextProblem, toggleLoop, toggleFollow,
                mergeSelection, removeSelection, clearSelection,
                onRowMouseDown, onRowClick, onTextFocus, onTextBlur, onTextInput, onTextKeydown, rememberCaret,
                playFrom: (row) => { const i = model.indexOfId(row.id); if (i >= 0) { playFromTime(model.get(i).start_time); focusList(); } },
                openPicker, openPickerForSelection, openMenu, openHelp, openTimesFor: (row, event) => openTimes(row.id, event.currentTarget),
                popover, closePopover,
                pickerQuery, pickerIndex, pickerAll, pickerOptions, pickerHeading, pickerSuggestion, choosePickerOption, onPickerKeydown, onPickerInput,
                timeStart, timeEnd, timeLinked, timeError, applyTimes, timesFromPlayhead,
                menuItems, menuAction, onMenuKeydown, shortcutList,
                previewSpeaker, acceptSuggestion,
                draftOffer,
                restoreDraft: () => session.restoreDraft(),
                discardDraft: () => session.discardDraft(),
            };
        },
    };
}
