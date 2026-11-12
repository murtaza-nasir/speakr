/**
 * The recording chat's threads (chat-threads.js) and the shared chat
 * writing to them: one thread per recording, kept in sessionStorage.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The answer's HTML needs a DOM; the thread logic does not.
vi.mock('../assistant/render.js', () => ({ renderAnswer: (turn) => `<p>${turn.content}</p>` }));

import {
    createChatThreads, finishedMessages, threadKey, STORAGE_PREFIX, clearThreadsOnSignOut, removeStoredThreads,
} from './chat-threads.js';
import { useAssistantChat } from '../assistant/chat.js';

/** A sessionStorage stand-in; `quota` limits the characters it holds. */
const memoryStorage = (quota = Infinity) => {
    const data = new Map();
    return {
        data,
        get length() { return data.size; },
        key: (i) => [...data.keys()][i] ?? null,
        getItem: (k) => (data.has(k) ? data.get(k) : null),
        setItem: (k, v) => {
            const used = [...data.entries()].filter(([key]) => key !== k).reduce((n, [, val]) => n + val.length, 0);
            if (used + String(v).length > quota) { const e = new Error('QuotaExceededError'); e.name = 'QuotaExceededError'; throw e; }
            data.set(k, String(v));
        },
        removeItem: (k) => { data.delete(k); },
    };
};

/** A minimal Vue: plain objects, getter/setter computeds. */
const Vue = {
    ref: (value) => ({ value }),
    reactive: (object) => object,
    computed: (arg) => (typeof arg === 'function'
        ? { get value() { return arg(); } }
        : { get value() { return arg.get(); }, set value(v) { arg.set(v); } }),
    nextTick: (fn) => Promise.resolve().then(fn),
};

/** A streamed response whose chunks the test sends one at a time; Stop aborts it as fetch does. */
const controlledStream = (signal) => {
    const queue = [];
    let wake = null;
    const push = (item) => { queue.push(item); if (wake) { wake(); wake = null; } };
    const aborted = () => { const e = new Error('aborted'); e.name = 'AbortError'; return e; };
    if (signal) signal.addEventListener('abort', () => { if (wake) { wake(); wake = null; } });
    const response = {
        ok: true,
        body: {
            getReader() {
                return {
                    read: async () => {
                        while (!queue.length) {
                            if (signal && signal.aborted) throw aborted();
                            await new Promise((r) => { wake = r; });
                        }
                        if (signal && signal.aborted) throw aborted();
                        const item = queue.shift();
                        if (item === null) return { done: true };
                        return { done: false, value: new TextEncoder().encode(item) };
                    },
                    cancel() {},
                };
            },
        },
    };
    return {
        response,
        delta: (text) => push(`data: ${JSON.stringify({ delta: text })}\n\n`),
        end: () => { push('data: {"end_of_stream": true}\n\n'); push(null); },
    };
};

const flush = () => new Promise((r) => setTimeout(r, 0));

const recA = { id: 1, title: 'Budget sync' };
const recB = { id: 2, title: 'Hiring plan' };

describe('thread keys and stored messages', () => {
    it('keys a recording by id, and every incognito recording as one', () => {
        expect(threadKey(recA)).toBe('1');
        expect(threadKey({ id: 'incognito', incognito: true })).toBe('incognito');
        expect(threadKey(null)).toBe(null);
    });

    it('leaves out an answer still arriving and its question', () => {
        const thread = { loading: true, messages: [
            { role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1', html: '<p>a1</p>' },
            { role: 'user', content: 'q2' }, { role: 'assistant', content: 'par', streaming: true },
        ] };
        expect(finishedMessages(thread)).toEqual([
            { role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1', html: '<p>a1</p>' },
        ]);
    });
});

describe('switching recordings mid-answer', () => {
    beforeEach(() => vi.stubGlobal('window', { Vue, requestAnimationFrame: (fn) => fn() }));
    afterEach(() => vi.unstubAllGlobals());

    it('keeps the answer in the thread it started in', async () => {
        const storage = memoryStorage();
        const threads = createChatThreads({ storage: () => storage, userId: 5 });
        const streams = [];
        const done = [];
        const chat = useAssistantChat({
            messages: Vue.ref([]), input: Vue.ref(''), loading: Vue.ref(false), t: (k) => k,
            thread: () => threads.current(),
            request: ({ signal }) => { const s = controlledStream(signal); streams.push(s); return Promise.resolve(s.response); },
            onDone: (thread) => { threads.save(); done.push(thread); },
        });

        const a = threads.select(recA);
        a.input = 'What was decided?';
        const sending = chat.send();
        await flush();
        streams[0].delta('The bud');
        await flush();

        const b = threads.select(recB);
        // The chat's bindings follow the recording on screen.
        expect(chat.messages.value).toEqual([]);
        expect(chat.loading.value).toBe(false);
        expect(threads.isAnsweringElsewhere(recA.id)).toBe(true);
        expect(threads.isAnsweringElsewhere(recB.id)).toBe(false);

        // B can ask while A is still answering.
        b.input = 'Who was hired?';
        const sendingB = chat.send();
        await flush();

        streams[0].delta('get.');
        streams[0].end();
        await sending;

        expect(a.messages.map((m) => [m.role, m.content])).toEqual([['user', 'What was decided?'], ['assistant', 'The budget.']]);
        expect(a.loading).toBe(false);
        expect(b.messages.map((m) => m.content)).toEqual(['Who was hired?']);
        expect(b.loading).toBe(true);
        expect(done).toEqual([a]);
        expect(threads.isCurrent(a)).toBe(false);

        // Stop acts on the thread on screen only.
        chat.stop();
        await sendingB;
        expect(b.loading).toBe(false);

        // Back to A: its conversation is there.
        threads.select(recA);
        expect(chat.messages.value.map((m) => m.content)).toEqual(['What was decided?', 'The budget.']);
    });

    it('clears the thread on screen only', () => {
        const threads = createChatThreads({ storage: () => memoryStorage(), userId: 5 });
        threads.select(recA).messages.push({ role: 'user', content: 'a' });
        threads.select(recB).messages.push({ role: 'user', content: 'b' });
        threads.clearCurrent();
        expect(threads.get('2').messages).toEqual([]);
        expect(threads.get('1').messages).toEqual([{ role: 'user', content: 'a' }]);
    });
});

describe('hasChat: the chat mark on a recording row', () => {
    it('is true once a thread has a message, and false when it is cleared', () => {
        const threads = createChatThreads({ storage: () => memoryStorage(), userId: 5 });
        expect(threads.hasChat(recA.id)).toBe(false);
        const a = threads.select(recA);
        expect(threads.hasChat(recA.id)).toBe(false);
        a.input = 'Only a draft';
        expect(threads.hasChat(recA.id)).toBe(false);
        a.messages.push({ role: 'user', content: 'What was decided?' });
        expect(threads.hasChat(recA.id)).toBe(true);
        expect(threads.hasChat(String(recA.id))).toBe(true);
        threads.select(recB);
        expect(threads.hasChat(recA.id)).toBe(true);
        expect(threads.hasChat(recB.id)).toBe(false);
        threads.select(recA);
        threads.clearCurrent();
        expect(threads.hasChat(recA.id)).toBe(false);
    });

    it('is false for incognito threads and for unknown ids', () => {
        const threads = createChatThreads({ storage: () => memoryStorage(), userId: 5 });
        const secret = threads.select({ id: 'incognito', incognito: true, title: 'Secret' });
        secret.messages.push({ role: 'user', content: 'Hi' });
        expect(threads.hasChat('incognito')).toBe(false);
        expect(threads.hasChat(null)).toBe(false);
        expect(threads.hasChat(999)).toBe(false);
    });

    it('follows restore, eviction, deletion and sign-out', () => {
        const storage = memoryStorage();
        const first = createChatThreads({ storage: () => storage, userId: 5 });
        first.select(recA).messages.push({ role: 'user', content: 'Q' }, { role: 'assistant', content: 'A' });
        first.select(recB);
        const threads = createChatThreads({ storage: () => storage, userId: 5, max: 2 });
        expect(threads.hasChat(recA.id)).toBe(false);
        threads.restore();
        expect(threads.hasChat(recA.id)).toBe(true);
        threads.drop(recA.id);
        expect(threads.hasChat(recA.id)).toBe(false);

        threads.select(recA).messages.push({ role: 'user', content: 'Q' });
        threads.select(recB);
        threads.select({ id: 3, title: 'C' });
        threads.select({ id: 4, title: 'D' });
        expect(threads.hasChat(recA.id)).toBe(false);

        threads.select(recB).messages.push({ role: 'user', content: 'Q' });
        threads.clearAll();
        expect(threads.hasChat(recB.id)).toBe(false);
    });
});

describe('least recently used threads', () => {
    it('keeps at most max threads and never drops one that is answering', () => {
        const threads = createChatThreads({ storage: () => null, userId: 5, max: 3 });
        threads.select({ id: 1 });
        threads.current().loading = true;     // answering, oldest
        threads.select({ id: 2 });
        threads.select({ id: 3 });
        threads.select({ id: 4 });            // over the limit: 2 goes, not 1
        expect(Object.keys(threads.state.threads).sort()).toEqual(['1', '3', '4']);
        threads.select({ id: 3 });            // 3 is now newer than 4
        threads.select({ id: 5 });            // 4 goes
        expect(Object.keys(threads.state.threads).sort()).toEqual(['1', '3', '5']);
    });

    it('drops the thread of a deleted recording and stops its answer', () => {
        const threads = createChatThreads({ storage: () => null, userId: 5 });
        const a = threads.select(recA);
        const abort = vi.fn();
        a.controller = { abort };
        threads.select(recB);
        threads.drop([recA.id]);
        expect(threads.get('1')).toBe(null);
        expect(abort).toHaveBeenCalled();
        expect(a.dropped).toBe(true);
    });
});

describe('sessionStorage', () => {
    it('brings threads and drafts back after a reload', () => {
        const storage = memoryStorage();
        const first = createChatThreads({ storage: () => storage, userId: 5 });
        const a = first.select(recA);
        a.messages.push({ role: 'user', content: 'q' }, { role: 'assistant', content: 'a', html: '<p>a</p>', sources: [{ n: 1 }] });
        first.select(recB).input = 'half a question';
        first.save();

        const second = createChatThreads({ storage: () => storage, userId: 5 });
        expect(second.restore()).toBe(2);
        const back = second.select(recA);
        expect(back.messages.map((m) => [m.role, m.content, m.html || ''])).toEqual([['user', 'q', ''], ['assistant', 'a', '<p>a</p>']]);
        expect(back.messages[1].sources).toEqual([{ n: 1 }]);
        expect(back.messages[1].streaming).toBe(false);
        expect(second.select(recB).input).toBe('half a question');
    });

    it('never stores an incognito thread, and drops it when another recording opens', () => {
        const storage = memoryStorage();
        const threads = createChatThreads({ storage: () => storage, userId: 5 });
        const inc = threads.select({ id: 'incognito', incognito: true });
        inc.messages.push({ role: 'user', content: 'secret' });
        threads.save();
        expect(storage.getItem(STORAGE_PREFIX + '5') || '').not.toContain('secret');
        threads.select(null);                         // a moment with nothing open keeps it
        expect(threads.get('incognito')).not.toBe(null);
        threads.select(recA);
        expect(threads.get('incognito')).toBe(null);
        threads.save();
        expect(storage.getItem(STORAGE_PREFIX + '5') || '').not.toContain('secret');
    });

    it('keeps each user apart and removes another user\'s threads', () => {
        const storage = memoryStorage();
        const morgan = createChatThreads({ storage: () => storage, userId: 5 });
        morgan.select(recA).messages.push({ role: 'user', content: 'mine' });
        morgan.save();
        expect(storage.getItem(STORAGE_PREFIX + '5')).toContain('mine');

        const other = createChatThreads({ storage: () => storage, userId: 6 });
        expect(other.restore()).toBe(0);
        expect(other.select(recA).messages).toEqual([]);
        expect(storage.getItem(STORAGE_PREFIX + '5')).toBe(null);
    });

    it('stores nothing without a signed-in user', () => {
        const storage = memoryStorage();
        const threads = createChatThreads({ storage: () => storage, userId: '' });
        threads.select(recA).messages.push({ role: 'user', content: 'x' });
        expect(threads.save()).toBe(false);
        expect(storage.length).toBe(0);
    });

    it('clears everything on sign-out', () => {
        const storage = memoryStorage();
        const threads = createChatThreads({ storage: () => storage, userId: 5 });
        threads.select(recA).messages.push({ role: 'user', content: 'x' });
        threads.save();
        storage.setItem('speakr.other', 'kept');
        let listener = null;
        clearThreadsOnSignOut(() => threads.clearAll(), { addEventListener: (_, fn) => { listener = fn; } });
        listener({ target: { getAttribute: () => '/account' } });
        expect(storage.getItem(STORAGE_PREFIX + '5')).not.toBe(null);
        listener({ target: { getAttribute: () => '/logout' } });
        expect(storage.getItem(STORAGE_PREFIX + '5')).toBe(null);
        expect(threads.count()).toBe(0);
        expect(storage.getItem('speakr.other')).toBe('kept');
        storage.setItem(STORAGE_PREFIX + '9', '{}');
        removeStoredThreads(() => storage);
        expect(storage.getItem(STORAGE_PREFIX + '9')).toBe(null);
    });

    it('leaves out the oldest threads when the quota is full', () => {
        const storage = memoryStorage(600);
        const threads = createChatThreads({ storage: () => storage, userId: 5 });
        threads.select({ id: 1 }).messages.push({ role: 'user', content: 'old '.repeat(60) });
        threads.select({ id: 2 }).messages.push({ role: 'user', content: 'new '.repeat(60) });
        expect(threads.save()).toBe(true);
        const stored = JSON.parse(storage.getItem(STORAGE_PREFIX + '5'));
        expect(stored.threads.map((t) => t.key)).toEqual(['2']);
    });

    it('carries on when storage throws or is missing', () => {
        const broken = {
            get length() { throw new Error('denied'); }, key() { throw new Error('denied'); },
            getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() { throw new Error('denied'); },
        };
        const threads = createChatThreads({ storage: () => broken, userId: 5 });
        expect(threads.restore()).toBe(0);
        threads.select(recA).messages.push({ role: 'user', content: 'x' });
        expect(threads.save()).toBe(false);
        expect(() => threads.clearAll()).not.toThrow();

        const none = createChatThreads({ storage: () => { throw new Error('SecurityError'); }, userId: 5 });
        expect(none.restore()).toBe(0);
        expect(none.save()).toBe(false);

        const garbled = memoryStorage();
        garbled.setItem(STORAGE_PREFIX + '5', '{not json');
        expect(createChatThreads({ storage: () => garbled, userId: 5 }).restore()).toBe(0);
    });

    it('drops an answer that was still arriving at reload', () => {
        const storage = memoryStorage();
        const threads = createChatThreads({ storage: () => storage, userId: 5 });
        const a = threads.select(recA);
        a.messages.push({ role: 'user', content: 'q1' }, { role: 'assistant', content: 'a1' },
            { role: 'user', content: 'q2' }, { role: 'assistant', content: 'half', streaming: true });
        a.loading = true;
        threads.save();
        const again = createChatThreads({ storage: () => storage, userId: 5 });
        again.restore();
        expect(again.select(recA).messages.map((m) => m.content)).toEqual(['q1', 'a1']);
        expect(again.get('1').loading).toBe(false);
    });
});
