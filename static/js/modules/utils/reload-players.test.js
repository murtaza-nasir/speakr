import { describe, expect, it, vi } from 'vitest';
import {
    audioAddress, holdPlayback, isAddressOf, replacedRevision, resumePlayback, revisionAfter,
} from './reload-players.js';

function player(src, { currentTime = 0, paused = true, readyState = 0, duration = NaN } = {}) {
    const attrs = { src };
    const listeners = {};
    return {
        currentTime, paused, ended: false, readyState, duration,
        getAttribute: (name) => attrs[name] ?? null,
        setAttribute: (name, value) => { attrs[name] = value; },
        addEventListener: (name, fn) => { listeners[name] = fn; },
        fire(name) { const fn = listeners[name]; delete listeners[name]; if (fn) fn(); },
        play: vi.fn(function play() { this.paused = false; return Promise.resolve(); }),
        get src() { return attrs.src; },
    };
}

const page = (...players) => ({ querySelectorAll: () => players });

describe('audioAddress', () => {
    it('puts the revision of the stored file in the address', () => {
        expect(audioAddress('/audio/7', 'ab12cd')).toBe('/audio/7?v=ab12cd');
        expect(audioAddress('/share/audio/x-Y_z', 'ab12cd')).toBe('/share/audio/x-Y_z?v=ab12cd');
    });

    it('is the plain address without a revision', () => {
        expect(audioAddress('/audio/7', null)).toBe('/audio/7');
        expect(audioAddress('/audio/7', undefined)).toBe('/audio/7');
    });
});

describe('isAddressOf', () => {
    it('matches the route with or without a version, and no other recording', () => {
        expect(isAddressOf('/audio/7', '/audio/7')).toBe(true);
        expect(isAddressOf('/audio/7?v=1', '/audio/7')).toBe(true);
        expect(isAddressOf('/audio/70', '/audio/7')).toBe(false);
        expect(isAddressOf('/share/audio/7', '/audio/7')).toBe(false);
        expect(isAddressOf(null, '/audio/7')).toBe(false);
    });
});

describe('revisionAfter', () => {
    it('takes the revision of a refreshed copy that reports one', () => {
        expect(revisionAfter('old', { id: 7, audio_revision: 'new' })).toBe('new');
        expect(revisionAfter('old', { id: 7, audio_revision: null })).toBe(null);
        expect(revisionAfter(null, { id: 7, audio_revision: 'new' })).toBe('new');
    });

    it('keeps the revision for a copy without the field (a status merge, a list row)', () => {
        expect(revisionAfter('old', { id: 7, status: 'COMPLETED' })).toBe('old');
        expect(revisionAfter('old', null)).toBe('old');
    });
});

describe('holdPlayback and resumePlayback', () => {
    it('gives a playing player its position back once the new file has its metadata, and plays it', () => {
        const main = player('/audio/7?v=old', { currentTime: 312.5, paused: false });
        const dock = player('/audio/7?v=old', { currentTime: 312.4, paused: true });
        const other = player('/audio/8?v=x', { currentTime: 3, paused: false });
        const root = page(main, dock, other);
        const held = holdPlayback('/audio/7', root);
        expect(held.time).toBe(312.5);

        // Vue sets the new address; the browser starts the new file from 0
        for (const media of [main, dock]) {
            media.setAttribute('src', '/audio/7?v=new');
            media.currentTime = 0;
            media.paused = true;
        }
        expect(resumePlayback(held)).toBe(2);
        expect(main.currentTime).toBe(0);
        main.duration = 1199;
        dock.duration = 1199;
        main.fire('loadedmetadata');
        dock.fire('loadedmetadata');
        expect(main.currentTime).toBe(312.5);
        expect(main.play).toHaveBeenCalledOnce();
        expect(dock.currentTime).toBe(312.5);
        expect(dock.play).not.toHaveBeenCalled();
        expect(other.play).not.toHaveBeenCalled();
    });

    it('keeps the position inside a shorter new file, and leaves a paused player paused', () => {
        const main = player('/audio/7', { currentTime: 80, paused: true });
        const held = holdPlayback('/audio/7', page(main));
        main.setAttribute('src', '/audio/7?v=new');
        main.readyState = 1;
        main.duration = 60;
        resumePlayback(held);
        expect(main.currentTime).toBe(60);
        expect(main.play).not.toHaveBeenCalled();
    });

    it('does nothing without a page', () => {
        expect(resumePlayback(holdPlayback('/audio/7', null))).toBe(0);
    });
});

describe('replacedRevision', () => {
    it('is the current revision when the stored file was replaced', async () => {
        expect(await replacedRevision('/audio/7', 'old', async () => 'new')).toBe('new');
        expect(await replacedRevision('/audio/7', 'old', async () => null)).toBe(null);
    });

    it('is undefined when the file is the same, the answer is unknown or the request failed', async () => {
        expect(await replacedRevision('/audio/7', 'old', async () => 'old')).toBeUndefined();
        expect(await replacedRevision('/audio/7', 'old', async () => undefined)).toBeUndefined();
        expect(await replacedRevision('/audio/7', 'old', async () => { throw new Error('offline'); })).toBeUndefined();
    });

    it('asks once at a time for a route', async () => {
        let release;
        const slow = () => new Promise((resolve) => { release = () => resolve('new'); });
        const first = replacedRevision('/audio/7', 'old', slow);
        expect(await replacedRevision('/audio/7', 'old', async () => 'new')).toBeUndefined();
        release();
        expect(await first).toBe('new');
    });
});
