import { describe, expect, it, vi } from 'vitest';
import { reloadPlayers, storedFileReplaced } from './reload-players.js';

function player(src) {
    const attrs = { src };
    return {
        getAttribute: (name) => attrs[name] ?? null,
        setAttribute: (name, value) => { attrs[name] = value; },
        load: vi.fn(),
        get src() { return attrs.src; },
    };
}

const page = (...players) => ({ querySelectorAll: () => players });

describe('reloadPlayers', () => {
    it('loads every player of the recording again under a versioned address', () => {
        const main = player('/audio/7');
        const dock = player('/audio/7?v=8000');
        const other = player('/audio/70');
        expect(reloadPlayers(7, 1200, page(main, dock, other))).toBe(2);
        expect(main.src).toBe('/audio/7?v=1200');
        expect(dock.src).toBe('/audio/7?v=1200');
        expect(main.load).toHaveBeenCalledOnce();
        expect(other.src).toBe('/audio/70');
        expect(other.load).not.toHaveBeenCalled();
    });

    it('leaves a player already on that version, so the file loads once', () => {
        const main = player('/audio/7?v=1200');
        expect(reloadPlayers(7, 1200, page(main))).toBe(0);
        expect(main.load).not.toHaveBeenCalled();
    });

    it('leaves the player of a public share alone', () => {
        const shared = player('/share/audio/abc');
        expect(reloadPlayers(7, 1200, page(shared))).toBe(0);
    });
});

describe('storedFileReplaced', () => {
    it('is true when a refreshed copy names a file of another size', () => {
        expect(storedFileReplaced({ id: 7, file_size: 8000 }, { id: 7, file_size: 1200 })).toBe(true);
    });

    it('is false for the same file, another recording or an unknown size', () => {
        expect(storedFileReplaced({ id: 7, file_size: 8000 }, { id: 7, file_size: 8000 })).toBe(false);
        expect(storedFileReplaced({ id: 7, file_size: 8000 }, { id: 8, file_size: 1200 })).toBe(false);
        expect(storedFileReplaced({ id: 7 }, { id: 7, file_size: 1200 })).toBe(false);
        expect(storedFileReplaced(null, { id: 7, file_size: 1200 })).toBe(false);
    });
});
