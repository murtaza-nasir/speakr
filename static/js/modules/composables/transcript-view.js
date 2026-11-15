/**
 * The transcript views of the recording page as a windowed list.
 *
 * A long transcript used to put every segment in the DOM: 120,000 nodes for
 * 20,000 segments, seconds to open and a slow page for as long as it was
 * shown. Here only the units near the viewport are rendered (a speaker turn
 * of the simple view, a row of bubbles, or a segment of a transcript without
 * speakers), with a spacer above and one below for the rest. The window
 * logic is in utils/transcript-window.js.
 *
 * In the templates, the list is the element with data-tv-list. Its direct
 * children are the spacer with data-tv-before, the rendered units (each with
 * data-tv-unit, its index) and the spacer with data-tv-after. The list may
 * sit in any scrolling element, or in the page itself.
 *
 * A transcript of up to RENDER_ALL_SEGMENTS segments is rendered whole, so
 * for a recording of ordinary length nothing changes.
 *
 * Anything that needs a segment on screen calls revealSegment(index): the
 * segment's element exists only while its unit is in the window.
 *
 * The browser's own find (Ctrl+F) sees only the rendered part of a windowed
 * transcript. For those transcripts Ctrl+F opens a find bar that searches
 * the whole transcript (templates/components/detail/_transcript-find.html):
 * Enter and Shift+Enter go through the matches, which are marked in the
 * rendered text with the CSS highlight API. Ctrl+F in the bar's own field
 * goes on to the browser's find.
 *
 * Printing a windowed transcript would print only the rendered part. For
 * the time of the print, the whole transcript is written into a plain
 * element (#transcript-print) that the print stylesheet shows in place of
 * the page (styles.css).
 */

import { bubbleUnits, charsPerLine, createTranscriptWindow, runUnits, segmentUnits } from '../utils/transcript-window.js';
import { buildMatcher, findMatches } from '../utils/find-replace.js';

// Pixels rendered above and below the viewport.
const OVERSCAN = 700;
const LIST = '[data-tv-list]';
const FIND_BAR = '[data-transcript-find]';
const HIGHLIGHT = 'transcript-find';
const HIGHLIGHT_CURRENT = 'transcript-find-current';
const PRINT_ID = 'transcript-print';
const PRINT_CLASS = 'print-transcript';

const scrollable = (el) => {
    const overflow = getComputedStyle(el).overflowY;
    return overflow === 'auto' || overflow === 'scroll' || overflow === 'overlay';
};

export function useTranscriptView({ processedTranscription, transcriptionViewMode, recordingId, recordingTitle, relayoutOn = [], scrollSegmentIntoView, t = (key) => key }) {
    const { shallowReactive, reactive, computed, watch, nextTick } = Vue;

    const model = createTranscriptWindow();
    // What the templates render: the units of the window for the view that
    // is shown, and the heights of the two spacers.
    const view = shallowReactive({ runs: [], rows: [], segments: [], before: 0, after: 0, windowed: false });

    // The find bar: what is searched for, and the matches as { index, start,
    // end } with the segment's index, in reading order.
    const find = reactive({ open: false, query: '', total: 0, current: -1 });
    let matches = [];
    let sentences = [];
    let paintFrame = 0;

    let kind = 'none';
    let range = { start: 0, end: 0 };
    let shownFor = null;            // the units array the window was last taken from
    let perLine = 80;
    let frame = 0;
    let observed = null;            // the list element being watched
    let observedWidth = 0;
    let anchor = null;              // { unit, top }: keeps a rendered unit in place over a re-render
    let rebase = null;              // { segment, within }: the segment to keep at the top of the view
    let lastRecording = null;

    const source = computed(() => {
        const transcript = processedTranscription.value || {};
        if (transcript.isError) return { kind: 'none', units: [] };
        if (!transcript.hasDialogue) return { kind: 'segment', units: segmentUnits(transcript.simpleSegments) };
        if (transcriptionViewMode.value === 'bubble') return { kind: 'bubbles', units: bubbleUnits(transcript.bubbleRows) };
        if (transcriptionViewMode.value === 'simple') return { kind: 'run', units: runUnits(transcript.simpleSegmentRuns) };
        return { kind: 'none', units: [] };
    });

    // ---- the list on the page -------------------------------------------

    const activeList = () => {
        for (const el of document.querySelectorAll(LIST)) {
            if (el.getClientRects().length) return el;
        }
        return null;
    };

    const scrollerOf = (list) => {
        for (let el = list.parentElement; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
            if (scrollable(el) && el.scrollHeight > el.clientHeight) return el;
        }
        // Not overflowing yet (a short window): the nearest element that can scroll
        for (let el = list.parentElement; el && el !== document.body && el !== document.documentElement; el = el.parentElement) {
            if (scrollable(el)) return el;
        }
        return null;
    };

    /** The scrolling element, the part of the page it shows and how far the list is scrolled in it. */
    const frameOf = (list) => {
        const scroller = scrollerOf(list);
        const top = scroller ? scroller.getBoundingClientRect().top : 0;
        const height = scroller ? scroller.clientHeight : window.innerHeight;
        const head = list.querySelector(':scope > [data-tv-before]') || list;
        return { scroller, top, height, y: top - head.getBoundingClientRect().top };
    };

    const scrollBy = (scroller, delta) => {
        if (Math.abs(delta) < 1) return;
        if (scroller) scroller.scrollTop += delta;
        else window.scrollBy(0, delta);
    };

    const unitElements = (list) => list.querySelectorAll(':scope > [data-tv-unit]');

    // ---- the window --------------------------------------------------------

    const show = (next) => {
        const units = model.units();
        if (next.start !== range.start || next.end !== range.end || shownFor !== units) {
            const part = next.start === 0 && next.end === units.length ? units : units.slice(next.start, next.end);
            view.runs = kind === 'run' ? part : [];
            view.rows = kind === 'bubbles' ? part : [];
            view.segments = kind === 'segment' ? part : [];
            range = next;
            shownFor = units;
        }
        const { before, after } = model.layout(range);
        view.before = before;
        view.after = after;
        view.windowed = !model.whole;
        if (find.open) schedulePaint();
    };

    /** Take the heights of the rendered units from the page. True when one changed. */
    const measure = (list) => {
        const els = unitElements(list);
        if (!els.length || els.length !== range.end - range.start || Number(els[0].dataset.tvUnit) !== range.start) return false;
        const tops = Array.from(els, (el) => el.getBoundingClientRect().top);
        const tail = list.querySelector(':scope > [data-tv-after]');
        const end = tail ? tail.getBoundingClientRect().top : els[els.length - 1].getBoundingClientRect().bottom;
        return model.measure(range.start, tops, end);
    };

    /** The first rendered unit in view that the next window keeps. */
    const anchorIn = (list, at, next) => {
        for (const el of unitElements(list)) {
            const unit = Number(el.dataset.tvUnit);
            if (unit < next.start || unit >= next.end) continue;
            const rect = el.getBoundingClientRect();
            if (rect.bottom > at.top) return { unit, top: rect.top };
        }
        return null;
    };

    const schedule = () => {
        if (!frame) frame = requestAnimationFrame(update);
    };

    const watchList = (list) => {
        if (observed === list) return;
        if (resizeObserver && observed) resizeObserver.unobserve(observed);
        observed = list;
        observedWidth = list.clientWidth;
        const next = charsPerLine(observedWidth, kind);
        if (resizeObserver) resizeObserver.observe(list);
        if (Math.abs(next - perLine) > 4) relayout();
    };

    // After the window was rendered: put the anchor back where it was (units
    // that came in above it have their real heights now, not the estimates)
    // and take the measured heights.
    const settle = () => {
        const list = activeList();
        if (!list) return;
        watchList(list);
        const at = frameOf(list);
        if (rebase) {
            const unit = model.unitOf(rebase.segment);
            // By the unit's place on the page when it is rendered: the units
            // above it in the window have their real heights, not the estimates
            const el = unit >= 0 ? list.querySelector(`:scope > [data-tv-unit="${unit}"]`) : null;
            if (el) scrollBy(at.scroller, el.getBoundingClientRect().top - (at.top - rebase.within));
            else if (unit >= 0) scrollBy(at.scroller, model.offset(unit) + rebase.within - at.y);
            rebase = null;
            anchor = null;
        } else if (anchor) {
            const el = list.querySelector(`:scope > [data-tv-unit="${anchor.unit}"]`);
            if (el) scrollBy(at.scroller, el.getBoundingClientRect().top - anchor.top);
            anchor = null;
        }
        if (measure(list)) show(range);
        schedule();
    };

    function update() {
        frame = 0;
        const list = activeList();
        if (!list) return;
        watchList(list);
        const changed = measure(list);
        const at = frameOf(list);
        const next = model.range(at.y, at.height, OVERSCAN);
        const moved = next.start !== range.start || next.end !== range.end || shownFor !== model.units();
        if (!moved && !changed) return;
        if (moved) anchor = anchorIn(list, at, next);
        show(next);
        if (moved) nextTick(settle);
    }

    /** The segment at the top of the view and how far it is scrolled, or null. */
    const topSegment = () => {
        const list = activeList();
        if (!list || model.whole) return null;
        const at = frameOf(list);
        for (const el of unitElements(list)) {
            const rect = el.getBoundingClientRect();
            if (rect.bottom <= at.top) continue;
            const unit = model.units()[Number(el.dataset.tvUnit)];
            return unit ? { segment: unit.first, within: Math.max(0, at.top - rect.top) } : null;
        }
        return null;
    };

    // New units (another recording, another view, an edit) or another width:
    // estimate again. In the same recording the segment at the top of the
    // view stays there.
    function relayout() {
        const next = source.value;
        const recording = recordingId ? recordingId() : null;
        // A place that is still to be restored (the list was replaced meanwhile) is kept
        const keep = recording !== lastRecording ? null : (rebase || (shownFor ? topSegment() : null));
        lastRecording = recording;
        kind = next.kind;
        const list = activeList();
        if (list) {
            observedWidth = list.clientWidth;
            perLine = charsPerLine(observedWidth, kind);
        }
        model.reset(next.units, perLine);
        rebase = keep && model.unitOf(keep.segment) >= 0 ? keep : null;
        anchor = null;
        const at = list ? frameOf(list) : { y: 0, height: window.innerHeight };
        const y = rebase ? model.offset(model.unitOf(rebase.segment)) + rebase.within : at.y;
        shownFor = null;
        show(model.range(y, at.height, OVERSCAN));
        nextTick(settle);
    }

    const resizeObserver = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => {
        if (!observed || !observed.isConnected) return;
        const width = observed.clientWidth;
        if (width && Math.abs(width - observedWidth) > 24) relayout();
        else schedule();
    }) : null;

    watch(source, relayout, { immediate: true });
    // The list is laid out again (the transcript tab of a phone, another
    // theme, timestamps switched on): measure it once it is on the page.
    if (relayoutOn.length) watch(relayoutOn, () => nextTick(schedule), { flush: 'post' });

    document.addEventListener('scroll', (event) => {
        const target = event.target;
        if (!observed || !observed.isConnected || target === document || (target.contains && target.contains(observed))) schedule();
    }, { capture: true, passive: true });
    window.addEventListener('resize', schedule, { passive: true });

    // ---- showing a segment ---------------------------------------------------

    const segmentElement = (list, index) => list.querySelector(`[data-segment-index="${index}"]`);

    /** Bring a segment into view; a segment outside the window is rendered first. */
    const revealSegment = (index) => {
        const list = activeList();
        if (!list || index === null || index === undefined) return;
        const el = segmentElement(list, index);
        if (el) {
            scrollSegmentIntoView(el);
            return;
        }
        const at = frameOf(list);
        const y = model.reveal(index, at.height);
        if (y === null) return;
        scrollBy(at.scroller, y - at.y);
        if (frame) cancelAnimationFrame(frame);
        update();
        // After the window is rendered and settled
        nextTick(() => nextTick(() => {
            const found = segmentElement(list, index);
            if (found) scrollSegmentIntoView(found);
        }));
    };

    // ---- find ------------------------------------------------------------------

    const highlights = () => (typeof CSS !== 'undefined' && CSS.highlights && typeof Highlight !== 'undefined' ? CSS.highlights : null);

    const clearPaint = () => {
        const registry = highlights();
        if (!registry) return;
        registry.delete(HIGHLIGHT);
        registry.delete(HIGHLIGHT_CURRENT);
    };

    /** First match in segment `index` or after it (matches are in segment order). */
    const firstMatchFrom = (index) => {
        let lo = 0;
        let hi = matches.length;
        while (lo < hi) {
            const mid = (lo + hi) >> 1;
            if (matches[mid].index < index) lo = mid + 1; else hi = mid;
        }
        return lo;
    };

    // Mark the matches in the rendered segments.
    const paint = () => {
        paintFrame = 0;
        const registry = highlights();
        if (!registry) return;
        clearPaint();
        const list = find.open && matches.length ? activeList() : null;
        if (!list) return;
        const all = [];
        const current = [];
        for (const el of list.querySelectorAll('[data-segment-index]')) {
            const index = Number(el.dataset.segmentIndex);
            let m = firstMatchFrom(index);
            if (m >= matches.length || matches[m].index !== index) continue;
            const sentence = sentences[index];
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
            let node = walker.nextNode();
            while (node && !node.data.includes(sentence)) node = walker.nextNode();
            if (!node) continue;
            const base = node.data.indexOf(sentence);
            for (; m < matches.length && matches[m].index === index; m++) {
                const range = document.createRange();
                range.setStart(node, base + matches[m].start);
                range.setEnd(node, base + matches[m].end);
                (m === find.current ? current : all).push(range);
            }
        }
        if (all.length) registry.set(HIGHLIGHT, new Highlight(...all));
        if (current.length) registry.set(HIGHLIGHT_CURRENT, new Highlight(...current));
    };
    function schedulePaint() {
        if (!paintFrame) paintFrame = requestAnimationFrame(paint);
    }

    const search = () => {
        const transcript = processedTranscription.value || {};
        sentences = find.open ? (transcript.simpleSegments || []).map((segment) => segment.sentence || '') : [];
        matches = find.open ? findMatches(sentences, buildMatcher(find.query)) : [];
        find.total = matches.length;
        find.current = -1;
        schedulePaint();
    };

    const goToTranscriptMatch = (step) => {
        if (!matches.length) return;
        if (find.current < 0) {
            // Start from what is on screen
            const top = topSegment();
            const from = firstMatchFrom(top ? top.segment : 0);
            find.current = step > 0 ? from % matches.length : (from - 1 + matches.length) % matches.length;
        } else {
            find.current = ((find.current + step) % matches.length + matches.length) % matches.length;
        }
        revealSegment(matches[find.current].index);
        schedulePaint();
    };

    const openTranscriptFind = () => {
        find.open = true;
        search();
        nextTick(() => {
            const input = document.querySelector(`${FIND_BAR} input`);
            if (input) {
                input.focus();
                input.select();
            }
        });
    };
    const closeTranscriptFind = () => {
        find.open = false;
        search();
    };
    const toggleTranscriptFind = () => (find.open ? closeTranscriptFind() : openTranscriptFind());

    const transcriptFindLabel = computed(() => {
        if (!find.query) return '';
        if (!find.total) return t('findReplace.noMatches');
        return find.current >= 0 ? `${find.current + 1} / ${find.total}` : String(find.total);
    });

    watch(() => find.query, search);
    // Another transcript, or the transcript was edited
    watch(processedTranscription, () => { if (find.open) search(); });
    watch(() => (recordingId ? recordingId() : null), () => { if (find.open) closeTranscriptFind(); });

    document.addEventListener('keydown', (event) => {
        if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey || String(event.key).toLowerCase() !== 'f') return;
        // The whole transcript is on the page: the browser's find sees all of it
        if (!view.windowed || !activeList()) return;
        // A dialog is open over the transcript
        if ([...document.querySelectorAll('.modal-overlay')].some((el) => el.getClientRects().length)) return;
        // A second Ctrl+F, in the bar's own field, goes to the browser's find
        const active = document.activeElement;
        if (find.open && active && active.closest && active.closest(FIND_BAR)) return;
        event.preventDefault();
        openTranscriptFind();
    });

    // ---- print -----------------------------------------------------------------

    const endPrint = () => {
        const el = document.getElementById(PRINT_ID);
        if (el) el.remove();
        document.documentElement.classList.remove(PRINT_CLASS);
    };
    const beginPrint = () => {
        endPrint();
        if (!view.windowed || !activeList()) return;
        const transcript = processedTranscription.value || {};
        const root = document.createElement('div');
        root.id = PRINT_ID;
        const add = (tag, text) => {
            const el = document.createElement(tag);
            el.textContent = text;
            root.appendChild(el);
        };
        const title = recordingTitle ? recordingTitle() : '';
        if (title) add('h1', title);
        let speaker = null;
        (transcript.simpleSegments || []).forEach((segment) => {
            if (transcript.hasDialogue && segment.speaker !== speaker) {
                speaker = segment.speaker;
                add('h2', speaker || '');
            }
            add('p', segment.sentence || '');
        });
        document.body.appendChild(root);
        document.documentElement.classList.add(PRINT_CLASS);
    };
    window.addEventListener('beforeprint', beginPrint);
    window.addEventListener('afterprint', endPrint);

    return {
        transcriptView: view, revealSegment, refreshTranscriptView: schedule,
        transcriptFind: find, transcriptFindLabel, openTranscriptFind, closeTranscriptFind, toggleTranscriptFind, goToTranscriptMatch,
    };
}
