import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
    DEFAULT_DURATION, WARNING_MIN_DURATION, PausableTimer, countLabel,
    showToast, toastDuration, toastKey, toastRole,
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
        this.style = { setProperty() {}, removeProperty() {} };
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

    it('keeps an error until it is closed', () => {
        showToast('Upload failed', 'fa-exclamation-circle', 3000, 'error');
        vi.advanceTimersByTime(120000);
        expect(container.children).toHaveLength(1);
    });
});
