/**
 * Toast notification utility.
 *
 * Every caller goes through showToast(message, iconClass, duration, type,
 * onClick). The pure helpers below (timing, roles, types, duplicate keys and
 * the pausable timer) carry the rules, so they can be tested without a DOM.
 *
 * A toast is a raised card in the color of its type (blue info, green
 * success, amber warning, red error; the same in every UI theme), with a
 * badge that carries the glyph of the type, the message, a repeat count, an
 * optional action button, a close button and, when it closes by itself, a
 * line that counts the time down (styles.css, "Toasts").
 */

export const DEFAULT_DURATION = 3000;
export const WARNING_MIN_DURATION = 8000;
const TYPES = ['info', 'success', 'warning', 'error'];
const FADE_MS = 200;

/** One of the four known types; anything else is shown as info. */
export const normalizeType = (type) => (TYPES.includes(type) ? type : 'info');

/**
 * How long a toast stays, in ms; 0 means until it is closed. Errors stay
 * unless the caller asked for a duration other than the default; warnings
 * stay at least WARNING_MIN_DURATION. An error whose type was read from its
 * icon (`inferred`) stays as long as a warning: its caller did not ask for a
 * message that has to be closed.
 */
export const toastDuration = (type, duration = DEFAULT_DURATION, inferred = false) => {
    const ms = Number.isFinite(duration) && duration > 0 ? duration : DEFAULT_DURATION;
    const kind = normalizeType(type);
    if (kind === 'error' && inferred) return Math.max(ms, WARNING_MIN_DURATION);
    if (kind === 'error') return ms === DEFAULT_DURATION ? 0 : ms;
    if (kind === 'warning') return Math.max(ms, WARNING_MIN_DURATION);
    return ms;
};

/** The glyph in the badge of each type (Font Awesome). */
export const TYPE_GLYPHS = {
    info: 'fa-info',
    success: 'fa-check',
    warning: 'fa-exclamation',
    error: 'fa-xmark',
};

// Whole class names only: 'fa-check' is a confirmation, 'fa-calendar-check' is not
const ICON_TYPES = [
    ['error', /(^|\s)(fa-)?(error|exclamation-circle|circle-exclamation|times-circle|circle-xmark)(\s|$)/],
    ['warning', /(^|\s)(fa-)?(warning|exclamation-triangle|triangle-exclamation)(\s|$)/],
    ['success', /(^|\s)(fa-)?(success|check|check-circle|circle-check)(\s|$)/],
];

/**
 * The type of a toast. A type that was passed is used as it is. Many callers
 * pass only an icon ('fa-exclamation-circle', 'fa-check-circle') or the name
 * of a type in the icon's place ('error'); for those the type follows from
 * the icon, so a failure is red and a confirmation green.
 * Returns { kind, inferred }.
 */
export const resolveType = (type, iconClass) => {
    if (type !== undefined && type !== null) return { kind: normalizeType(type), inferred: false };
    const icon = String(iconClass || '');
    for (const [kind, pattern] of ICON_TYPES) {
        if (pattern.test(icon)) return { kind, inferred: true };
    }
    return { kind: 'info', inferred: false };
};

/** Errors interrupt (alert); everything else is announced politely (status). */
export const toastRole = (type) => (normalizeType(type) === 'error' ? 'alert' : 'status');

/** Identical message and type count as the same toast. */
export const toastKey = (message, type) => `${normalizeType(type)}\u0000${String(message)}`;

/** The text of the repeat counter: '' for one, '×2' and up after that. */
export const countLabel = (count) => (count > 1 ? `×${count}` : '');

/**
 * A timeout that can be paused and resumed (hover and focus) and restarted
 * (a duplicate came in). With ms 0 it never fires. clock is injectable for
 * tests: { now, setTimeout, clearTimeout }.
 */
export class PausableTimer {
    constructor(ms, onDone, clock = globalThis) {
        this.ms = ms;
        this.onDone = onDone;
        this.clock = clock;
        this.handle = null;
        this.remaining = ms;
        this.startedAt = 0;
        this.paused = false;
        this.start();
    }

    start() {
        this.clear();
        if (!this.ms || this.paused) return;
        this.startedAt = this.clock.now ? this.clock.now() : Date.now();
        this.handle = this.clock.setTimeout(() => { this.handle = null; this.onDone(); }, this.remaining);
    }

    clear() {
        if (this.handle !== null) {
            this.clock.clearTimeout(this.handle);
            this.handle = null;
        }
    }

    pause() {
        if (this.paused) return;
        if (this.handle !== null) {
            const now = this.clock.now ? this.clock.now() : Date.now();
            this.remaining = Math.max(0, this.remaining - (now - this.startedAt));
        }
        this.clear();
        this.paused = true;
    }

    resume() {
        if (!this.paused) return;
        this.paused = false;
        this.start();
    }

    /** Back to the full duration, keeping a pause in place. */
    restart() {
        this.remaining = this.ms;
        this.start();
    }

    stop() {
        this.clear();
        this.ms = 0;
    }
}

const translate = (key, fallback) => {
    try {
        const i18n = typeof window !== 'undefined' ? window.i18n : null;
        if (i18n && typeof i18n.t === 'function') {
            const value = i18n.t(key);
            if (value && value !== key) return value;
        }
    } catch (e) { /* fall back */ }
    return fallback;
};

// ---- placement ---------------------------------------------------------------
//
// Desktop: bottom center of the reading area (the main column beside the
// recording list), above whatever is anchored to its bottom edge (the player,
// a composer, a status bar). Phones (the phone layout, below 768px): full
// width above the queue strip, the player strip, the tab bar and the New
// recording button; under the top bar while a sheet is open (a class on
// <body>, set by the app). The numbers go to the container as custom
// properties; the rules are in styles.css.

const PHONE_QUERY = '(max-width: 767px)';
// What a toast must not cover at the bottom of a phone screen
// (and the buttons at the foot of an open dialog)
const PHONE_BOTTOM = '[data-mobile-player], [data-mobile-bottom-nav], [data-phone-composer], [data-queue-strip], [data-phone-new], [data-bulk-bar], .modal-footer';
// The same for the reading area of a desktop page
const DESKTOP_BOTTOM = '[data-toast-avoid], .assistant-composer, .desk-statusbar, .modal-footer';
const MAX_WIDTH = 420;
const READING_AREA = '[data-toast-area], main';

const isPhone = () => typeof window !== 'undefined' && !!window.matchMedia && window.matchMedia(PHONE_QUERY).matches;

const visibleRect = (el) => {
    if (!el || !el.getBoundingClientRect) return null;
    const r = el.getBoundingClientRect();
    return r.width && r.height ? r : null;
};

/**
 * The space to keep free under the toasts: the distance from the bottom of
 * the window to the highest of `rects` that starts in the lower half of the
 * window and overlaps the span from `left` to `right`.
 */
export const bottomInset = (rects, viewportHeight, left = -Infinity, right = Infinity) => {
    let top = viewportHeight;
    for (const r of rects) {
        if (!r || r.top < viewportHeight / 2 || r.top >= viewportHeight) continue;
        if (r.right <= left || r.left >= right) continue;
        top = Math.min(top, r.top);
    }
    return Math.round(viewportHeight - top);
};

const UNDER_TOP_BAR = 60;
const TOP_EDGE = 8;

/**
 * Where the toasts start on a phone while a sheet is open: under the top
 * bar, or as much higher as the stack needs to end above the sheet (whose
 * heading and close button it would cover), down to the top edge.
 */
export const topUnderBar = (sheetTop, stackHeight) =>
    Math.max(TOP_EDGE, Math.min(UNDER_TOP_BAR, Math.floor(sheetTop - 8 - stackHeight)));

const placeContainer = (container) => {
    if (typeof window === 'undefined' || typeof document === 'undefined' || !document.querySelectorAll) return;
    const vh = window.innerHeight;
    const outside = (el) => !el.closest || !el.closest('#toastContainer');
    if (isPhone()) {
        const rects = [...document.querySelectorAll(PHONE_BOTTOM)].filter(outside).map(visibleRect);
        container.style.setProperty('--toast-bottom', `${bottomInset(rects, vh)}px`);
        container.style.removeProperty('--toast-center');
        // While a sheet is open the toasts are at the top (styles.css): under
        // the top bar, or higher when the stack would reach the sheet
        const sheet = document.querySelector('[data-phone-sheet] .ph-sheet');
        if (sheet) container.style.setProperty('--toast-top', `${topUnderBar(vh - sheet.offsetHeight, container.offsetHeight)}px`);
        else container.style.removeProperty('--toast-top');
        return;
    }
    // The main column makes room for the recording list with a padding
    const main = [...document.querySelectorAll(READING_AREA)].find(visibleRect);
    const area = main ? visibleRect(main) : null;
    const style = main && typeof getComputedStyle === 'function' ? getComputedStyle(main) : null;
    const inset = (side) => (style ? parseFloat(style[side]) || 0 : 0);
    const left = area ? Math.max(0, area.left + inset('paddingLeft')) : 0;
    const right = area ? Math.min(window.innerWidth, area.right - inset('paddingRight')) : window.innerWidth;
    const rects = [...document.querySelectorAll(DESKTOP_BOTTOM)].filter(outside).map(visibleRect);
    // Kept inside the window when the reading area is narrower than a toast
    const half = Math.min(MAX_WIDTH, window.innerWidth - 32) / 2;
    const center = Math.min(window.innerWidth - 16 - half, Math.max(16 + half, (left + right) / 2));
    container.style.setProperty('--toast-center', `${Math.round(center)}px`);
    container.style.setProperty('--toast-bottom', `${bottomInset(rects, vh, center - half, center + half)}px`);
};

// While toasts are shown the place follows the layout (the recording list
// opening, the window, the player growing); with none shown nothing listens.
let placement = null;

const followLayout = (container) => {
    if (placement || typeof window === 'undefined' || !window.addEventListener) return;
    const place = () => placeContainer(container);
    placement = { place, observer: null };
    window.addEventListener('resize', place);
    if (typeof ResizeObserver !== 'undefined' && document.querySelectorAll) {
        placement.observer = new ResizeObserver(place);
        for (const el of document.querySelectorAll(`${READING_AREA}, ${PHONE_BOTTOM}, ${DESKTOP_BOTTOM}`)) {
            placement.observer.observe(el);
        }
    }
};

const stopFollowing = (container) => {
    if (!placement || container.children.length > 0) return;
    window.removeEventListener('resize', placement.place);
    if (placement.observer) placement.observer.disconnect();
    placement = null;
};

/** Place the toasts again; for a change of layout that changes no size (a sheet, a view). */
export const placeToasts = () => {
    const container = typeof document !== 'undefined' ? document.getElementById('toastContainer') : null;
    if (container && container.children.length > 0) placeContainer(container);
};

const prepareContainer = (container) => {
    if (!container.hasAttribute('aria-live')) container.setAttribute('aria-live', 'polite');
    if (!container.hasAttribute('aria-relevant')) container.setAttribute('aria-relevant', 'additions text');
};

// Shorter times for the browser tests: window.__speakrToastScale = 0.1 makes
// every duration a tenth. Not set in the app.
const timeScale = () => {
    const scale = typeof window !== 'undefined' ? Number(window.__speakrToastScale) : NaN;
    return Number.isFinite(scale) && scale > 0 ? scale : 1;
};

/**
 * `message` is HTML: escape any user text in it. With `onClick`, a click on
 * the toast calls it before the toast goes. An element with the class
 * toast__action in the message (`<span class="toast__action">Undo</span>`)
 * becomes the button of that action.
 */
export const showToast = (message, iconClass = 'fa-info-circle', duration = DEFAULT_DURATION, type, onClick = null) => {
    const container = document.getElementById('toastContainer');
    if (!container) {
        console.warn('Toast container not found');
        return;
    }
    prepareContainer(container);

    const { kind, inferred } = resolveType(type, iconClass);
    const key = toastKey(message, kind);

    // The same message is already up: count it and give it its time again.
    for (const existing of container.children) {
        if (existing._toast && existing._toast.key === key && !existing._toast.leaving) {
            const state = existing._toast;
            state.count += 1;
            state.counter.textContent = countLabel(state.count);
            state.counter.hidden = false;
            state.timer.restart();
            state.restartCountdown();
            if (typeof onClick === 'function') state.onClick = onClick;
            return;
        }
    }

    const toast = document.createElement('div');
    toast.className = `toast-message toast--${kind}`;
    toast.setAttribute('role', toastRole(kind));
    toast.dataset.toastType = kind;

    const badge = document.createElement('span');
    badge.className = 'toast__badge';
    badge.setAttribute('aria-hidden', 'true');
    const glyph = document.createElement('i');
    glyph.className = `fas ${TYPE_GLYPHS[kind]}`;
    badge.appendChild(glyph);

    const body = document.createElement('span');
    body.className = 'toast__body';
    body.innerHTML = message;

    const counter = document.createElement('span');
    counter.className = 'toast__count';
    counter.hidden = true;

    // The action named in the message becomes a button beside it
    let action = null;
    const named = typeof body.querySelector === 'function' ? body.querySelector('.toast__action') : null;
    if (named) {
        action = document.createElement('button');
        action.type = 'button';
        action.className = 'toast__action';
        action.textContent = named.textContent;
        named.remove();
    }

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast__close';
    const closeLabel = translate('toast.dismiss', 'Dismiss notification');
    close.setAttribute('aria-label', closeLabel);
    close.title = closeLabel;
    close.innerHTML = '<i class="fas fa-xmark" aria-hidden="true"></i>';

    toast.append(badge, body, counter);
    if (action) toast.append(action);
    toast.append(close);

    // The line that counts down the time of a toast that closes by itself
    const life = Math.round(toastDuration(kind, duration, inferred) * timeScale());
    let countdown = null;
    const drawCountdown = () => {
        if (!life) return;
        const line = document.createElement('span');
        line.className = 'toast__countdown';
        line.setAttribute('aria-hidden', 'true');
        line.style.setProperty('--toast-life', `${life}ms`);
        line.appendChild(document.createElement('i'));
        if (countdown && countdown.parentNode) countdown.parentNode.removeChild(countdown);
        countdown = line;
        toast.appendChild(line);
    };
    drawCountdown();

    const state = { key, count: 1, counter, onClick, leaving: false, timer: null, restartCountdown: drawCountdown };
    toast._toast = state;

    const dismissToast = () => {
        if (state.leaving) return;
        state.leaving = true;
        state.timer.stop();
        toast.classList.remove('toast-show');
        toast.classList.add('toast-leave');
        setTimeout(() => {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
            stopFollowing(container);
        }, FADE_MS);
    };
    state.timer = new PausableTimer(life, dismissToast);
    const hold = () => { state.timer.pause(); toast.classList.add('toast-paused'); };
    const release = () => { state.timer.resume(); toast.classList.remove('toast-paused'); };

    // A click on the toast runs its action (if any) and closes it.
    toast.addEventListener('click', () => {
        if (typeof state.onClick === 'function') {
            try { state.onClick(); } catch (err) { console.error('Toast action failed:', err); }
        }
        dismissToast();
    });
    close.addEventListener('click', (event) => {
        event.stopPropagation();
        dismissToast();
    });

    if (typeof onClick === 'function') {
        toast.classList.add('toast--action');
        if (!action) {
            body.setAttribute('role', 'button');
            body.tabIndex = 0;
            body.addEventListener('keydown', (event) => {
                if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toast.click(); }
            });
        }
    }

    // Hover and keyboard focus hold the toast open.
    toast.addEventListener('mouseenter', hold);
    toast.addEventListener('mouseleave', () => { if (!toast.contains(document.activeElement)) release(); });
    toast.addEventListener('focusin', hold);
    toast.addEventListener('focusout', (event) => {
        if (!toast.contains(event.relatedTarget) && !toast.matches(':hover')) release();
    });
    toast.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { event.stopPropagation(); dismissToast(); }
    });

    container.appendChild(toast);
    placeContainer(container);
    followLayout(container);
    requestAnimationFrame(() => toast.classList.add('toast-show'));
};
