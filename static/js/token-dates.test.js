import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// token-dates.js is a classic script that sets window.SpeakrTokenDates.
const SOURCE = readFileSync(fileURLToPath(new URL('./token-dates.js', import.meta.url)), 'utf8');

function load() {
    const window = {};
    vm.runInContext(SOURCE, vm.createContext({ window, Date, Intl }));
    return window.SpeakrTokenDates;
}

const NOW = new Date('2026-11-13T15:00:00Z');
const DAY = 24 * 60 * 60 * 1000;

describe('formatTokenDate', () => {
    it('shows date and time for a value within the last 7 days', () => {
        const { formatTokenDate } = load();
        const date = new Date(NOW.getTime() - 2 * 60 * 60 * 1000);
        const out = formatTokenDate(date, 'en-US', NOW);
        expect(out.text).toBe(date.toLocaleString('en-US', {
            year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit',
        }));
        expect(out.text).not.toBe(date.toLocaleDateString('en-US'));
        expect(out.title).toBe(date.toLocaleString('en-US'));
    });

    it('tells apart two tokens from the same day', () => {
        const { formatTokenDate } = load();
        const a = formatTokenDate(new Date('2026-11-13T09:05:00Z'), 'en-US', NOW);
        const b = formatTokenDate(new Date('2026-11-13T11:40:00Z'), 'en-US', NOW);
        expect(a.text).not.toBe(b.text);
    });

    it('shows the date only for a value 7 days old or older', () => {
        const { formatTokenDate } = load();
        for (const age of [7 * DAY, 30 * DAY]) {
            const date = new Date(NOW.getTime() - age);
            const out = formatTokenDate(date, 'en-US', NOW);
            expect(out.text).toBe(date.toLocaleDateString('en-US'));
            expect(out.title).toBe(date.toLocaleString('en-US'));
        }
    });

    it('follows the locale', () => {
        const { formatTokenDate } = load();
        const date = new Date(NOW.getTime() - DAY);
        expect(formatTokenDate(date, 'de-DE', NOW).text).toBe(date.toLocaleString('de-DE', {
            year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit',
        }));
    });

    it('gives empty strings for an invalid date', () => {
        const { formatTokenDate } = load();
        expect(formatTokenDate(new Date(NaN), 'en-US', NOW)).toEqual({ text: '', title: '' });
        expect(formatTokenDate(null, 'en-US', NOW)).toEqual({ text: '', title: '' });
    });
});
