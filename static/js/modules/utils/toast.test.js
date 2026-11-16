import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
    DEFAULT_DURATION, WARNING_MIN_DURATION, PausableTimer, bottomInset, countLabel,
    resolveType, showToast, toastDuration, toastKey, toastRole, topUnderBar,
} from './toast.js';

// A minimal stand-in for the DOM pieces showToast touches (the suite runs in
// the node environment).
class FakeElement {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.dataset = {};
        this.listeners = {};
        this.hidden = false;
        this.textContent = '';
        this.innerHTML = '';
        this.className = '';
        const props = {};
        this.style = { props, setProperty(k, v) { props[k] = v; }, removeProperty(k) { delete props[k]; } };
        const self = this;
        this.classList = {
            add: (...c) => { self.className = [...new Set([...self.className.split(' ').filter(Boolean), ...c])].join(' '); },
            remove: (...c) => { self.className = self.className.split(' ').filter(x => x && !c.includes(x)).join(' '); },
            contains: (c) => self.className.split(' ').includes(c),
        };
    }
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return k in this.attributes ? this.attributes[k] : null; }
    hasAttribute(k) { return k in this.attributes; }
    append(...nodes) { nodes.forEach(n => this.appendChild(n)); }
    appendChild(n) { n.parentNode = this; this.children.push(n); return n; }
    removeChild(n) { this.children = this.children.filter(c => c !== n); n.parentNode = null; return n; }
    contains(n) { return n === this || this.children.some(c => c.contains && c.contains(n)); }
    matches() { return false; }
    closest() { return null; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    dispatch(type, extra = {}) {
        const event = { type, stopPropagation: () => { event.stopped = true; }, preventDefault() {}, ...extra };
        (this.listeners[type] || []).forEach(fn => fn(event));
        if (!event.stopped && this.parentNode && this.parentNode.dispatch) this.parentNode.dispatch(type, event);
    }
    click() { this.dispatch('click'); }
    // The one query showToast makes: the action named in the message
    querySelector(selector) {
        if (selector !== '.toast__action') return null;
        const match = /<span class="toast__action">(.*?)<\/span>/.exec(this.innerHTML);
        if (!match) return null;
        const self = this;
        return { textContent: match[1], remove() { self.innerHTML = self.innerHTML.replace(match[0], '').trim(); } };
    }
    find(cls) {
        for (const c of this.children) {
            if (c.classList.contains(cls)) return c;
            const d = c.find && c.find(cls);
            if (d) return d;
        }
        return null;
    }
}

let container;

beforeEach(() => {
    vi.useFakeTimers();
    container = new FakeElement('div');
    globalThis.document = {
        getElementById: (id) => (id === 'toastContainer' ? container : null),
        createElement: (tag) => new FakeElement(tag),
        querySelectorAll: () => [],
        activeElement: null,
    };
    globalThis.window = { matchMedia: () => ({ matches: false }), innerHeight: 800,
        i18n: { t: (k) => (k === 'toast.dismiss' ? 'Dismiss notification' : k) } };
    globalThis.requestAnimationFrame = (fn) => fn();
});

afterEach(() => {
    vi.useRealTimers();
    delete globalThis.document;
    delete globalThis.window;
    delete globalThis.requestAnimationFrame;
});

describe('toast rules', () => {
    it('keeps errors until closed unless a non-default duration is passed', () => {
        expect(toastDuration('error')).toBe(0);
        expect(toastDuration('error', DEFAULT_DURATION)).toBe(0);
        expect(toastDuration('error', 6000)).toBe(6000);
    });

    it('keeps warnings at least the minimum, others as passed', () => {
        expect(toastDuration('warning', 3000)).toBe(WARNING_MIN_DURATION);
        expect(toastDuration('warning', 12000)).toBe(12000);
        expect(toastDuration('success', 2000)).toBe(2000);
        expect(toastDuration('info')).toBe(DEFAULT_DURATION);
        expect(toastDuration('unknown-type', 5000)).toBe(5000);
    });

    it('uses alert for errors and status for the rest', () => {
        expect(toastRole('error')).toBe('alert');
        expect(toastRole('warning')).toBe('status');
        expect(toastRole('success')).toBe('status');
        expect(toastRole('info')).toBe('status');
        expect(toastRole(undefined)).toBe('status');
    });

    it('gives an error read from its icon the time of a warning', () => {
        expect(toastDuration('error', DEFAULT_DURATION, true)).toBe(WARNING_MIN_DURATION);
        expect(toastDuration('error', 12000, true)).toBe(12000);
        expect(toastDuration('success', 3000, true)).toBe(3000);
    });

    it('uses a type that was passed and reads a missing one from the icon', () => {
        expect(resolveType('info', 'fa-exclamation-circle')).toEqual({ kind: 'info', inferred: false });
        expect(resolveType('bogus', 'fa-check')).toEqual({ kind: 'info', inferred: false });
        expect(resolveType(undefined, 'fa-exclamation-circle')).toEqual({ kind: 'error', inferred: true });
        expect(resolveType(undefined, 'fas fa-times-circle')).toEqual({ kind: 'error', inferred: true });
        expect(resolveType(undefined, 'error')).toEqual({ kind: 'error', inferred: true });
        expect(resolveType(undefined, 'fa-exclamation-triangle')).toEqual({ kind: 'warning', inferred: true });
        expect(resolveType(undefined, 'warning')).toEqual({ kind: 'warning', inferred: true });
        expect(resolveType(undefined, 'fa-check-circle')).toEqual({ kind: 'success', inferred: true });
        expect(resolveType(undefined, 'fa-check')).toEqual({ kind: 'success', inferred: true });
        expect(resolveType(undefined, 'success')).toEqual({ kind: 'success', inferred: true });
        expect(resolveType(undefined, 'fa-calendar-check')).toEqual({ kind: 'info', inferred: false });
        expect(resolveType(undefined, 'fa-list-check')).toEqual({ kind: 'info', inferred: false });
        expect(resolveType(undefined, 'fa-palette')).toEqual({ kind: 'info', inferred: false });
        expect(resolveType(undefined, undefined)).toEqual({ kind: 'info', inferred: false });
    });

    it('keeps the bottom of the reading area free', () => {
        const rect = (top, left, right) => ({ top, left, right });
        // A player across the area and a status bar under it
        expect(bottomInset([rect(744, 320, 1280), rect(776, 0, 1280)], 800, 590, 1010)).toBe(56);
        // Something beside the toasts does not move them
        expect(bottomInset([rect(700, 0, 320)], 800, 590, 1010)).toBe(0);
        // Something in the upper half is not at the bottom
        expect(bottomInset([rect(60, 320, 1280)], 800, 590, 1010)).toBe(0);
        expect(bottomInset([null, rect(900, 0, 100)], 800)).toBe(0);
        expect(bottomInset([rect(676, 0, 390), rect(732, 0, 390), rect(788, 0, 390)], 844)).toBe(168);
    });

    it('starts under the top bar while a sheet is open, higher when the stack would reach the sheet', () => {
        expect(topUnderBar(500, 60)).toBe(60);     // a short sheet: room under the top bar
        expect(topUnderBar(101, 46)).toBe(47);     // a tall sheet: one toast ends just above it
        expect(topUnderBar(101, 200)).toBe(8);     // a stack that cannot fit starts at the top edge
        expect(topUnderBar(0, 46)).toBe(8);        // the menu, a sheet of the full height
    });

    it('keys on message and type', () => {
        expect(toastKey('Saved', 'success')).toBe(toastKey('Saved', 'success'));
        expect(toastKey('Saved', 'success')).not.toBe(toastKey('Saved', 'info'));
        expect(toastKey('Saved', 'bogus')).toBe(toastKey('Saved', 'info'));
        expect(countLabel(1)).toBe('');
        expect(countLabel(3)).toBe('×3');
    });
});

describe('PausableTimer', () => {
    it('pauses with the time left and resumes from it', () => {
        const done = vi.fn();
        const t = new PausableTimer(1000, done);
        vi.advanceTimersByTime(600);
        t.pause();
        vi.advanceTimersByTime(5000);
        expect(done).not.toHaveBeenCalled();
        t.resume();
        vi.advanceTimersByTime(399);
        expect(done).not.toHaveBeenCalled();
        vi.advanceTimersByTime(1);
        expect(done).toHaveBeenCalledTimes(1);
    });

    it('restarts to the full duration', () => {
        const done = vi.fn();
        const t = new PausableTimer(1000, done);
        vi.advanceTimersByTime(900);
        t.restart();
        vi.advanceTimersByTime(900);
        expect(done).not.toHaveBeenCalled();
        vi.advanceTimersByTime(100);
        expect(done).toHaveBeenCalledTimes(1);
    });

    it('never fires with 0', () => {
        const done = vi.fn();
        new PausableTimer(0, done);
        vi.advanceTimersByTime(60000);
        expect(done).not.toHaveBeenCalled();
    });
});

describe('showToast', () => {
    it('makes the container a live region and gives each toast its role', () => {
        showToast('Saved', 'fa-check-circle', 3000, 'success');
        showToast('Failed', 'fa-exclamation-circle', 3000, 'error');
        expect(container.getAttribute('aria-live')).toBe('polite');
        expect(container.children.map(t => t.getAttribute('role'))).toEqual(['status', 'alert']);
        expect(container.children[0].className).toContain('toast--success');
    });

    it('has a close button with an accessible name that dismisses without the action', () => {
        const action = vi.fn();
        showToast('Undo?', 'fa-undo', 3000, 'info', action);
        const close = container.children[0].find('toast__close');
        expect(close.tagName).toBe('BUTTON');
        expect(close.getAttribute('aria-label')).toBe('Dismiss notification');
        close.click();
        vi.advanceTimersByTime(500);
        expect(action).not.toHaveBeenCalled();
        expect(container.children).toHaveLength(0);
    });

    it('runs the action and dismisses on a click on the toast', () => {
        const action = vi.fn();
        showToast('Open it', 'fa-info-circle', 3000, 'info', action);
        container.children[0].click();
        expect(action).toHaveBeenCalledTimes(1);
        vi.advanceTimersByTime(500);
        expect(container.children).toHaveLength(0);
    });

    it('adds an identical toast once, with a count, and restarts its timer', () => {
        showToast('Saved', 'fa-check-circle', 3000, 'success');
        vi.advanceTimersByTime(2500);
        showToast('Saved', 'fa-check-circle', 3000, 'success');
        showToast('Saved', 'fa-check-circle', 3000, 'info');
        expect(container.children).toHaveLength(2);
        const counter = container.children[0].find('toast__count');
        expect(counter.hidden).toBe(false);
        expect(counter.textContent).toBe('×2');
        vi.advanceTimersByTime(2500);
        expect(container.children[0].className).toContain('toast--success');
        vi.advanceTimersByTime(1000);
        expect(container.children.some(t => t.className.includes('toast--success'))).toBe(false);
    });

    it('holds the toast while hovered or focused', () => {
        showToast('Saved', 'fa-check-circle', 3000, 'success');
        const toast = container.children[0];
        toast.dispatch('mouseenter');
        vi.advanceTimersByTime(10000);
        expect(container.children).toHaveLength(1);
        toast.dispatch('mouseleave');
        vi.advanceTimersByTime(3200);
        expect(container.children).toHaveLength(0);

        showToast('Again', 'fa-check-circle', 3000, 'success');
        const second = container.children[0];
        second.dispatch('focusin');
        vi.advanceTimersByTime(10000);
        expect(container.children).toHaveLength(1);
        second.dispatch('focusout', { relatedTarget: null });
        vi.advanceTimersByTime(3200);
        expect(container.children).toHaveLength(0);
    });

    it('shows the type in a badge with the glyph of the type, whatever icon was passed', () => {
        showToast('Saved', 'fa-palette', 3000, 'success');
        showToast('Failed', 'fa-palette', 3000, 'error');
        showToast('Careful', 'fa-palette', 3000, 'warning');
        showToast('Note', 'fa-palette');
        const glyphs = container.children.map(t => t.find('toast__badge').children[0].className);
        expect(glyphs).toEqual(['fas fa-check', 'fas fa-xmark', 'fas fa-exclamation', 'fas fa-info']);
        expect(container.children.every(t => !t.className.includes('surface-raised'))).toBe(true);
    });

    it('reads the type of an old call from its icon', () => {
        showToast('Could not save', 'fa-exclamation-circle');
        showToast('Saved', 'success');
        expect(container.children.map(t => t.dataset.toastType)).toEqual(['error', 'success']);
        expect(container.children[0].getAttribute('role')).toBe('alert');
        // Not closed by hand like an error that was asked for, but not gone in three seconds either
        vi.advanceTimersByTime(3500);
        expect(container.children.map(t => t.dataset.toastType)).toEqual(['error']);
        vi.advanceTimersByTime(WARNING_MIN_DURATION);
        expect(container.children).toHaveLength(0);
    });

    it('makes the action named in the message a button that runs it', () => {
        const action = vi.fn();
        showToast('Renamed <span class="toast__action">Undo</span>', 'fa-check-circle', 6000, 'info', action);
        const toast = container.children[0];
        const button = toast.find('toast__action');
        expect(button.tagName).toBe('BUTTON');
        expect(button.textContent).toBe('Undo');
        expect(toast.find('toast__body').innerHTML).toBe('Renamed');
        // The body is not a second button
        expect(toast.find('toast__body').getAttribute('role')).toBeNull();
        button.click();
        expect(action).toHaveBeenCalledTimes(1);
        vi.advanceTimersByTime(500);
        expect(container.children).toHaveLength(0);
    });

    it('draws a countdown line on a toast that closes by itself, and none on one that stays', () => {
        showToast('Saved', 'fa-check-circle', 3000, 'success');
        showToast('Failed', 'fa-exclamation-circle', 3000, 'error');
        const [saved, failed] = container.children;
        expect(saved.find('toast__countdown').style.props['--toast-life']).toBe('3000ms');
        expect(failed.find('toast__countdown')).toBeNull();
    });

    it('stops the countdown line with the timer and starts it again with a repeat', () => {
        showToast('Saved', 'fa-check-circle', 3000, 'success');
        const toast = container.children[0];
        const line = toast.find('toast__countdown');
        toast.dispatch('mouseenter');
        expect(toast.className).toContain('toast-paused');
        toast.dispatch('mouseleave');
        expect(toast.className).not.toContain('toast-paused');
        showToast('Saved', 'fa-check-circle', 3000, 'success');
        expect(toast.find('toast__countdown')).not.toBe(line);
        expect(toast.children.filter(c => c.className === 'toast__countdown')).toHaveLength(1);
    });

    it('shortens every duration with the test scale', () => {
        globalThis.window.__speakrToastScale = 0.1;
        showToast('Saved', 'fa-check-circle', 3000, 'success');
        vi.advanceTimersByTime(250);
        expect(container.children).toHaveLength(1);
        vi.advanceTimersByTime(300);
        expect(container.children).toHaveLength(0);
    });

    it('keeps an error until it is closed', () => {
        showToast('Upload failed', 'fa-exclamation-circle', 3000, 'error');
        vi.advanceTimersByTime(120000);
        expect(container.children).toHaveLength(1);
    });
});
