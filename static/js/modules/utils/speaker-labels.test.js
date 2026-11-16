import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { SPEAKER_WORDS, isGenericSpeakerLabel, isRealSpeakerName, normalizeLabel, realSpeakers } from './speaker-labels.js';
import { isPlaceholderSpeaker, nextSpeakerLabel } from '../asr-editor/helpers.js';

// The table the Python rule (src/utils/speaker_labels.py) is tested against.
const cases = JSON.parse(readFileSync(new URL('../../../../tests/fixtures/speaker_label_cases.json', import.meta.url), 'utf8'));
const locale = (code) => JSON.parse(readFileSync(new URL(`../../../locales/${code}.json`, import.meta.url), 'utf8'));

describe('isGenericSpeakerLabel', () => {
    it('has a table to check', () => {
        expect(cases.generic.length).toBeGreaterThan(40);
        expect(cases.names.length).toBeGreaterThan(40);
    });

    it.each(cases.generic)('%j is a diarization label', (label) => {
        expect(isGenericSpeakerLabel(label)).toBe(true);
        expect(isRealSpeakerName(label)).toBe(false);
    });

    it.each(cases.names)('%j is kept as a name', (name) => {
        expect(isGenericSpeakerLabel(name)).toBe(false);
    });

    it('is false for values that are not text', () => {
        for (const value of [null, undefined, 0, 7, {}, []]) expect(isGenericSpeakerLabel(value)).toBe(false);
        expect(isRealSpeakerName(null)).toBe(false);
        expect(isRealSpeakerName('  ')).toBe(false);
        expect(isRealSpeakerName('Dana')).toBe(true);
    });

    it('normalizes width, case and spacing', () => {
        expect(normalizeLabel('  Speaker   1 ')).toBe('speaker 1');
        expect(normalizeLabel('ＳＰＥＡＫＥＲ＿００')).toBe('speaker_00');
    });

    it('covers the labels the segment editor creates', () => {
        expect(isGenericSpeakerLabel(nextSpeakerLabel([]))).toBe(true);
        expect(isGenericSpeakerLabel(nextSpeakerLabel(['SPEAKER_99']))).toBe(true);
        expect(isPlaceholderSpeaker('SPEAKER_07')).toBe(true);
    });

    it('covers the default speaker name of every language', () => {
        for (const code of ['en', 'de', 'es', 'fr', 'pt-BR', 'ru', 'zh']) {
            const template = locale(code).transcription.speaker;
            const word = template.replace('{{number}}', '').trim().toLowerCase();
            expect(SPEAKER_WORDS).toContain(word);
            expect(isGenericSpeakerLabel(template.replace('{{number}}', '1'))).toBe(true);
            expect(isGenericSpeakerLabel(template.replace('{{number}}', '12'))).toBe(true);
        }
    });
});

describe('realSpeakers', () => {
    it('drops saved speakers whose name is a label', () => {
        const saved = [{ id: 1, name: 'Dana' }, { id: 2, name: 'SPEAKER_00' }, { id: 3, name: 'Speaker 1' }, { id: 4, name: '' }, null, { id: 5, name: 'Kim' }];
        expect(realSpeakers(saved).map((s) => s.id)).toEqual([1, 5]);
        expect(realSpeakers(null)).toEqual([]);
        expect(realSpeakers({})).toEqual([]);
    });
});
