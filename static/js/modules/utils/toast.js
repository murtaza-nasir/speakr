/**
 * Toast notification utility.
 *
 * Every caller goes through showToast(message, iconClass, duration, type,
 * onClick). The pure helpers below (timing, roles, duplicate keys and the
 * pausable timer) carry the rules, so they can be tested without a DOM.
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
 * stay at least WARNING_MIN_DURATION.
 */
export const toastDuration = (type, duration = DEFAULT_DURATION) => {
    const ms = Number.isFinite(duration) && duration > 0 ? duration : DEFAULT_DURATION;
    const kind = normalizeType(type);
    if (kind === 'error') return ms === DEFAULT_DURATION ? 0 : ms;
    if (kind === 'warning') return Math.max(ms, WARNING_MIN_DURATION);
    return ms;
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

// Phones: the container sits above whatever is anchored to the bottom of the
// screen (the player, the tab bar, the processing queue).
const BOTTOM_ANCHORED = '[data-mobile-player], [data-mobile-bottom-nav], .progress-popup';
const PHONE_QUERY = '(max-width: 640px)';

const placeContainer = (container) => {
    if (typeof window === 'undefined' || !window.matchMedia || !window.matchMedia(PHONE_QUERY).matches) {
        container.style.removeProperty('--toast-bottom');
        return;
    }
    const vh = window.innerHeight;
    let top = vh;
    for (const el of document.querySelectorAll(BOTTOM_ANCHORED)) {
        if (el.closest('#toastContainer')) continue;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height || r.top < vh / 2 || r.top >= vh) continue;
        top = Math.min(top, r.top);
    }
    container.style.setProperty('--toast-bottom', `${Math.round(vh - top)}px`);
};

const prepareContainer = (container) => {
    if (!container.hasAttribute('aria-live')) container.setAttribute('aria-live', 'polite');
    if (!container.hasAttribute('aria-relevant')) container.setAttribute('aria-relevant', 'additions text');
};

/**
 * `message` is HTML: escape any user text in it. With `onClick`, a click on
 * the toast calls it before the toast goes.
 */
export const showToast = (message, iconClass = 'fa-info-circle', duration = DEFAULT_DURATION, type = 'info', onClick = null) => {
    const container = document.getElementById('toastContainer');
    if (!container) {
        console.warn('Toast container not found');
        return;
    }
    prepareContainer(container);
    placeContainer(container);

    const kind = normalizeType(type);
    const key = toastKey(message, kind);

    // The same message is already up: count it and give it its time again.
    for (const existing of container.children) {
        if (existing._toast && existing._toast.key === key && !existing._toast.leaving) {
            const state = existing._toast;
            state.count += 1;
            state.counter.textContent = countLabel(state.count);
            state.counter.hidden = false;
            state.timer.restart();
            if (typeof onClick === 'function') state.onClick = onClick;
            return;
        }
    }

    const toast = document.createElement('div');
    toast.className = `toast-message surface-raised toast--${kind}`;
    toast.setAttribute('role', toastRole(kind));
    toast.dataset.toastType = kind;

    // Old callers pass just the icon name ('fa-check-circle'), newer ones the
    // full class.
    const fullIconClass = String(iconClass || 'fa-info-circle').includes(' ') ? iconClass : `fas ${iconClass || 'fa-info-circle'}`;

    const icon = document.createElement('i');
    icon.className = `toast__icon ${fullIconClass}`;
    icon.setAttribute('aria-hidden', 'true');

    const body = document.createElement('span');
    body.className = 'toast__body';
    body.innerHTML = message;

    const counter = document.createElement('span');
    counter.className = 'toast__count';
    counter.hidden = true;

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast__close';
    const closeLabel = translate('toast.dismiss', 'Dismiss notification');
    close.setAttribute('aria-label', closeLabel);
    close.title = closeLabel;
    close.innerHTML = '<i class="fas fa-xmark" aria-hidden="true"></i>';

    toast.append(icon, body, counter, close);

    const state = { key, count: 1, counter, onClick, leaving: false, timer: null };
    toast._toast = state;

    const dismissToast = () => {
        if (state.leaving) return;
        state.leaving = true;
        state.timer.stop();
        toast.classList.remove('toast-show');
        setTimeout(() => { if (toast.parentNode) toast.parentNode.removeChild(toast); }, FADE_MS);
    };
    state.timer = new PausableTimer(toastDuration(kind, duration), dismissToast);

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
        body.setAttribute('role', 'button');
        body.tabIndex = 0;
        body.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toast.click(); }
        });
    }

    // Hover and keyboard focus hold the toast open.
    toast.addEventListener('mouseenter', () => state.timer.pause());
    toast.addEventListener('mouseleave', () => { if (!toast.contains(document.activeElement)) state.timer.resume(); });
    toast.addEventListener('focusin', () => state.timer.pause());
    toast.addEventListener('focusout', (event) => {
        if (!toast.contains(event.relatedTarget) && !toast.matches(':hover')) state.timer.resume();
    });
    toast.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') { event.stopPropagation(); dismissToast(); }
    });

    container.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add('toast-show'));
};
