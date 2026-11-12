/**
 * The shared Ambrose chat's stream and history helpers (phase 2b).
 */
import { describe, expect, it } from 'vitest';
import { historyOf, readEvents, upsertSegmentSource, upsertStep } from './chat.js';
import { filterFields, filterSummary } from './search-in.js';

const streamOf = (...parts) => ({
    body: {
        getReader() {
            const chunks = parts.map((p) => new TextEncoder().encode(p));
            return { read: async () => (chunks.length ? { done: false, value: chunks.shift() } : { done: true }), cancel() {} };
        },
    },
});

describe('readEvents', () => {
    it('joins lines split across chunks and stops at the end of the stream', async () => {
        const seen = [];
        await readEvents(streamOf('data: {"delta": "Hel', 'lo"}\n\ndata: {"end_of_stream": true}\n\ndata: {"delta": "late"}\n\n'),
            (data) => { seen.push(data); return data.end_of_stream ? 'done' : undefined; });
        expect(seen).toEqual([{ delta: 'Hello' }, { end_of_stream: true }]);
    });

    it('passes the incognito route\'s [DONE]', async () => {
        const seen = [];
        await readEvents(streamOf('data: {"delta": "x"}\n\ndata: [DONE]\n\n'), (d) => { seen.push(d); return d === '[DONE]' ? 'done' : undefined; });
        expect(seen).toEqual([{ delta: 'x' }, '[DONE]']);
    });
});

describe('turn helpers', () => {
    it('updates a step by id', () => {
        const steps = [];
        upsertStep(steps, { id: 1, label: 'Searching for “budget”', status: 'running' });
        upsertStep(steps, { id: 1, status: 'done', detail: '3 matches' });
        expect(steps).toEqual([{ id: 1, label: 'Searching for “budget”', status: 'done', detail: '3 matches' }]);
    });

    it('keeps one segment source per marker', () => {
        const list = [];
        upsertSegmentSource(list, { marker: '[S1]', n: 1 });
        upsertSegmentSource(list, { marker: '[S1]', n: 1, verified: true });
        expect(list).toEqual([{ marker: '[S1]', n: 1, verified: true }]);
    });

    it('sends the history with the activity line and the numbering shown, without failed turns', () => {
        const messages = [
            { role: 'user', content: 'q1' },
            { role: 'assistant', content: 'a1', html: '<p>a1</p>', activitySummary: '1 search',
              sources: [{ n: 4, kind: 'library', recording_id: 9, title: 'Sync' }] },
            { role: 'user', content: 'q2' },
            { role: 'assistant', content: 'Error: boom', failed: true },
        ];
        expect(historyOf(messages)).toEqual([
            { role: 'user', content: 'q1' },
            { role: 'assistant', content: 'a1', activity: '1 search', sources: [{ n: 4, recording_id: 9, title: 'Sync' }] },
            { role: 'user', content: 'q2' },
        ]);
        expect(historyOf(messages, 2)).toEqual([{ role: 'user', content: 'q2' }]);
    });
});

describe('Ask page filters', () => {
    const t = (key) => ({ 'inquire.tagsCount': 'tags', 'inquire.dateRange': 'Date Range' }[key] || key);
    const filters = { selectedTags: [1, 2], selectedFolders: [], selectedSpeakers: [], selectedRecordings: [],
        dateFrom: '2026-01-01', dateTo: null };

    it('summarizes the active filters', () => {
        expect(filterSummary(filters, t)).toBe('2 tags · Date Range');
        expect(filterSummary({ ...filters, selectedTags: [], dateFrom: null }, t)).toBe('');
    });

    it('sends the keys the Ask chat route reads', () => {
        expect(filterFields(filters)).toEqual({
            filter_tags: [1, 2], filter_folders: [], filter_speakers: [], filter_recording_ids: [],
            filter_date_from: '2026-01-01', filter_date_to: null,
        });
    });
});
