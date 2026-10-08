import { describe, expect, it, vi } from 'vitest';
import { splitAsrSegment } from './asr-segment-split.js';

const segment = (sentence, fields = {}) => ({
    sentence, speaker: 'Анна', start_time: 10, end_time: 18, ...fields
});

describe('splitAsrSegment', () => {
    it('keeps transcription imports usable without Intl.Segmenter and disables only splitting', async () => {
        const descriptor = Object.getOwnPropertyDescriptor(Intl, 'Segmenter');
        try {
            Object.defineProperty(Intl, 'Segmenter', { ...descriptor, value: undefined });
            vi.resetModules();
            const compatible = await import('./asr-segment-split.js');
            const transcription = await import('../composables/transcription.js');
            expect(transcription.useTranscription).toBeTypeOf('function');
            expect(compatible.supportsAsrSegmentSplit()).toBe(false);
            expect(compatible.splitAsrSegment(segment('hello world'), 6, 6))
                .toEqual({ error: 'unsupported' });
        } finally {
            Object.defineProperty(Intl, 'Segmenter', descriptor);
            vi.resetModules();
        }
    });
    it('turns two voices into adjacent segments without changing outer times or neighbours', () => {
        const source = segment('Привет. Добрый день.', { confidence: 0.9 });
        const { parts } = splitAsrSegment(source, 8, 8);
        expect(parts.map(p => p.sentence)).toEqual(['Привет.', 'Добрый день.']);
        expect(parts.map(p => p.speaker)).toEqual(['Анна', 'Анна']);
        expect(parts[0].start_time).toBe(10);
        expect(parts[1].end_time).toBe(18);
        expect(parts[0].end_time).toBe(parts[1].start_time);
        expect(parts[0].end_time).toBeCloseTo(12.95, 2);
        expect(parts[0].confidence).toBe(0.9);
        expect(source.sentence).toBe('Привет. Добрый день.');
    });

    it('preserves punctuation, outer whitespace, and internal line breaks', () => {
        const text = '  Первая\nфраза! \n\t Вторая?  ';
        const p = text.indexOf('Вторая');
        expect(splitAsrSegment(segment(text), p, p).parts.map(p => p.sentence))
            .toEqual(['  Первая\nфраза!', 'Вторая?  ']);
    });

    it('uses code point lengths for proportional time', () => {
        const { parts } = splitAsrSegment(segment('😀 ab', { start_time: 0, end_time: 9 }), 3, 3);
        expect(parts[0].end_time).toBe(3);
    });

    it.each(['😀x', '👍🏽x', '👨‍👩‍👦x', 'е\u0301x'])('rejects a cut inside a grapheme: %s', text => {
        expect(splitAsrSegment(segment(text), 1, 1).error).toBe('cursor');
    });

    it.each([[0, 0], [3, 3], [1, 2], [-1, -1], [1.5, 1.5]])('rejects invalid selection %s,%s', (start, end) => {
        expect(splitAsrSegment(segment('abc'), start, end).error).toBe('cursor');
    });

    it.each([[' abc', 1], ['abc ', 3], ['\n\t ', 1]])('rejects whitespace-only parts: %s', (text, p) => {
        expect(splitAsrSegment(segment(text), p, p).error).toBe('cursor');
    });

    it.each([
        { start_time: null }, { end_time: '' }, { start_time: -1 },
        { start_time: NaN }, { end_time: Infinity }, { end_time: 10 },
        { end_time: 10.01 }, { start_time: '10' }
    ])('rejects invalid timestamps without changing the source: %j', fields => {
        const source = segment('abc', fields);
        expect(splitAsrSegment(source, 1, 1).error).toBe('time');
        expect(source.sentence).toBe('abc');
    });

    it('bounds rounding inside very short fractional intervals', () => {
        const { parts } = splitAsrSegment(segment('a' + 'b'.repeat(100), {
            start_time: 1.005, end_time: 1.026
        }), 1, 1);
        expect(parts[0].end_time).toBeGreaterThan(1.005);
        expect(parts[0].end_time).toBeLessThan(1.026);
        expect(parts[0].end_time).toBe(parts[1].start_time);
    });

    it('keeps a two-hundredths interval splittable despite floating point representation', () => {
        expect(splitAsrSegment(segment('ab', { start_time: 10, end_time: 10.02 }), 1, 1).parts).toHaveLength(2);
    });

    it('does not copy stale word alignment into the two new sentences', () => {
        const source = segment('ab', { words: [{ word: 'ab', start: 10, end: 18 }] });
        const { parts } = splitAsrSegment(source, 1, 1);
        expect(parts.every(part => !('words' in part))).toBe(true);
        expect(source.words).toHaveLength(1);
    });
});
