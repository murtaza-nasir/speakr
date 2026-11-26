import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// transcription-model-match.js is a classic script that sets
// window.SpeakrTranscriptionModelMatch.
const SOURCE = readFileSync(fileURLToPath(new URL('./transcription-model-match.js', import.meta.url)), 'utf8');

function load() {
    const window = {};
    vm.runInContext(SOURCE, vm.createContext({ window }));
    return window.SpeakrTranscriptionModelMatch.matchSavedModel;
}

const OPTIONS = [
    { value: 'm_11111111', label: 'Whisper large', model: 'large-v3' },
    { value: 'm_22222222', label: 'GPT-4o', model: 'gpt-4o-transcribe' },
];
const ENVIRONMENT = { value: 'env:whisper-asr:small', label: 'whisper-asr:small', model: 'small' };

describe('matchSavedModel', () => {
    const match = load();

    it('keeps a profile id as it is', () => {
        expect(match('m_22222222', OPTIONS, ENVIRONMENT)).toEqual(
            { value: 'm_22222222', label: 'GPT-4o', stale: false, synthetic: false });
    });

    it('shows a legacy model name as the profile with that model', () => {
        expect(match('large-v3', OPTIONS, ENVIRONMENT)).toEqual(
            { value: 'm_11111111', label: 'Whisper large', stale: false, synthetic: false });
        expect(match(' Large-V3 ', OPTIONS, ENVIRONMENT).value).toBe('m_11111111');
    });

    it('shows the environment model and id as the environment', () => {
        for (const saved of ['small', 'env:whisper-asr:small']) {
            const result = match(saved, OPTIONS, ENVIRONMENT);
            expect(result.stale).toBe(false);
            expect(result.synthetic).toBe(true);
            expect(result.value).toBe(saved);
            expect(result.label).toBe('whisper-asr:small');
        }
    });

    it('marks a value no model matches as stale', () => {
        expect(match('medium', OPTIONS, ENVIRONMENT).stale).toBe(true);
        expect(match('m_99999999', OPTIONS, null).stale).toBe(true);
    });

    it('accepts any value while no profile is configured', () => {
        const result = match('anything', [], ENVIRONMENT);
        expect(result.stale).toBe(false);
        expect(result.synthetic).toBe(true);
    });

    it('treats an empty value as the default', () => {
        expect(match('', OPTIONS, ENVIRONMENT)).toEqual({ value: '', label: '', stale: false, synthetic: false });
        expect(match(null, OPTIONS, ENVIRONMENT).value).toBe('');
    });
});
