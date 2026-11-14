import { describe, expect, it } from 'vitest';

import { createDraftStore, hashText } from './draft.js';
import { createAsrModel } from './model.js';

function memoryStorage({ failOnSet = false } = {}) {
    const data = new Map();
    return {
        data,
        getItem: (k) => (data.has(k) ? data.get(k) : null),
        setItem: (k, v) => { if (failOnSet) throw new Error('quota'); data.set(k, String(v)); },
        removeItem: (k) => { data.delete(k); },
    };
}

const transcript = JSON.stringify([
    { speaker: 'A', sentence: 'one two', start_time: 0, end_time: 2 },
    { speaker: 'B', sentence: 'three', start_time: 2, end_time: 3 },
]);

describe('hashText', () => {
    it('is stable and tells texts apart', () => {
        expect(hashText('abc')).toBe(hashText('abc'));
        expect(hashText('abc')).not.toBe(hashText('abd'));
        expect(hashText('abc')).not.toBe(hashText('abcd'));
        expect(hashText('')).toBe('0:811c9dc5');
        expect(hashText(null)).toBe(hashText(''));
    });
});

describe('the local draft', () => {
    it('restores the edits on the same transcript', () => {
        const storage = memoryStorage();
        const store = createDraftStore(storage);
        const model = createAsrModel(JSON.parse(transcript));
        model.split(0, 3);
        model.setSpeaker([2], 'Dana');
        expect(store.save(7, 'transcription', hashText(transcript), model.exportLog(), 1234)).toBe('saved');

        const draft = store.load(7, 'transcription', hashText(transcript));
        expect(draft.savedAt).toBe(1234);
        const again = createAsrModel(JSON.parse(transcript));
        again.importLog(draft.log);
        expect(again.toJSON()).toEqual(model.toJSON());
    });

    it('is kept per recording and per field', () => {
        const store = createDraftStore(memoryStorage());
        store.save(7, 'transcription', 'h', [{ label: 'x', steps: [], ids: [] }]);
        expect(store.load(8, 'transcription', 'h')).toBeNull();
        expect(store.load(7, 'transcription_translation', 'h')).toBeNull();
        expect(store.load(7, 'transcription', 'h')).not.toBeNull();
    });

    it('is dropped when the saved transcript is another one', () => {
        const storage = memoryStorage();
        const store = createDraftStore(storage);
        store.save(7, 'transcription', 'old', [{ label: 'x', steps: [], ids: [] }]);
        expect(store.load(7, 'transcription', 'new')).toBeNull();
        expect(storage.data.size).toBe(0);
    });

    it('is removed on discard and when there is nothing to keep', () => {
        const storage = memoryStorage();
        const store = createDraftStore(storage);
        store.save(7, 'transcription', 'h', [{ label: 'x', steps: [], ids: [] }]);
        expect(store.save(7, 'transcription', 'h', [])).toBe('empty');
        expect(storage.data.size).toBe(0);
        store.save(7, 'transcription', 'h', [{ label: 'x', steps: [], ids: [] }]);
        store.discard(7, 'transcription');
        expect(storage.data.size).toBe(0);
    });

    it('is not stored when it is too large, and an older one is removed', () => {
        const storage = memoryStorage();
        const store = createDraftStore(storage, { maxChars: 200 });
        store.save(7, 'transcription', 'h', [{ label: 'x', steps: [], ids: [] }]);
        const big = [{ label: 'x', steps: [{ k: 'set', i: 0, f: 'sentence', a: '', b: 'y'.repeat(500) }], ids: [1] }];
        expect(store.save(7, 'transcription', 'h', big)).toBe('too-large');
        expect(storage.data.size).toBe(0);
    });

    it('does not throw when storage fails or holds something else', () => {
        const failing = createDraftStore(memoryStorage({ failOnSet: true }));
        expect(failing.save(7, 'transcription', 'h', [{ label: 'x', steps: [], ids: [] }])).toBe('failed');
        const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() { throw new Error('denied'); } };
        const store = createDraftStore(broken);
        expect(store.load(7, 'transcription', 'h')).toBeNull();
        expect(() => store.discard(7, 'transcription')).not.toThrow();
        const storage = memoryStorage();
        storage.data.set('speakr.asrDraft.7.transcription', 'not json');
        expect(createDraftStore(storage).load(7, 'transcription', 'h')).toBeNull();
        expect(storage.data.size).toBe(0);
    });
});
