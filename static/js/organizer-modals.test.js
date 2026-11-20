import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// organizer-modals.js is a classic script that sets window.SpeakrOrganizer.
// It is run against a minimal document: only the elements a test names exist.
const SOURCE = readFileSync(fileURLToPath(new URL('./organizer-modals.js', import.meta.url)), 'utf8');

function load(elementsById = {}) {
    const document = {
        readyState: 'complete',
        getElementById: (id) => elementsById[id] || null,
        querySelectorAll: () => [],
        querySelector: () => null,
        addEventListener: () => {},
    };
    class FakeFormData {
        constructor(form) { this.values = form.values; }
        get(name) { return name in this.values ? this.values[name] : null; }
    }
    const window = { addEventListener: () => {} };
    const context = { window, document, FormData: FakeFormData, console, fetch: () => Promise.reject() };
    vm.runInNewContext(SOURCE, context);
    return window.SpeakrOrganizer;
}

// A tag form whose shown fields are the keys of `values`
const tagForm = (values) => ({
    values,
    elements: { namedItem: (name) => (name in values ? {} : null) },
});

describe('saving a tag or folder keeps the defaults the dialog does not show', () => {
    it('leaves hidden speaker counts out of the tag payload', () => {
        const payload = load().buildTagPayload(tagForm({ name: 'T', default_language: 'cs' }));
        expect(payload.default_language).toBe('cs');
        expect('default_min_speakers' in payload).toBe(false);
        expect('default_max_speakers' in payload).toBe(false);
        expect('default_hotwords' in payload).toBe(false);
    });

    it('sends every default the tag dialog shows, an empty one as null', () => {
        const payload = load().buildTagPayload(tagForm({
            name: 'T', default_language: '', default_min_speakers: '2', default_max_speakers: '',
            default_hotwords: 'a', default_initial_prompt: '', default_transcription_model: '',
        }));
        expect(payload.default_language).toBe(null);
        expect(payload.default_min_speakers).toBe(2);
        expect(payload.default_max_speakers).toBe(null);
        expect(payload.default_hotwords).toBe('a');
    });

    it('leaves hidden folder fields out of the folder payload', () => {
        const organizer = load({
            folderName: { value: 'F' },
            folderLanguage: { value: 'de' },
        });
        const payload = organizer.buildFolderPayload();
        expect(payload.default_language).toBe('de');
        expect('default_min_speakers' in payload).toBe(false);
        expect('default_initial_prompt' in payload).toBe(false);
    });
});
