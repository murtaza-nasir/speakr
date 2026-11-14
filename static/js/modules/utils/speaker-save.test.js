import { describe, expect, it } from 'vitest';

import { namedSpeakers, saveTranscriptWithSpeakers, withAcceptedMatches } from './speaker-save.js';

describe('namedSpeakers', () => {
    it('drops the labels without a name', () => {
        expect(namedSpeakers({ SPEAKER_00: { name: 'Dana' }, SPEAKER_01: { name: '  ' }, SPEAKER_02: {} }))
            .toEqual({ SPEAKER_00: { name: 'Dana' } });
        expect(namedSpeakers(null)).toEqual({});
    });
});

describe('saveTranscriptWithSpeakers', () => {
    const reply = (ok, body) => async () => ({ ok, json: async () => body });

    it('posts the transcript and the named speakers to update_transcript', async () => {
        const calls = [];
        const fetchImpl = async (url, options) => { calls.push({ url, body: JSON.parse(options.body), method: options.method }); return { ok: true, json: async () => ({ success: true }) }; };
        const data = await saveTranscriptWithSpeakers(7, {
            transcriptData: [{ speaker: 'SPEAKER_00', sentence: 'hi' }],
            speakerMap: { SPEAKER_00: { name: 'Dana', isMe: false }, SPEAKER_01: { name: '' } },
        }, { fetchImpl });
        expect(data).toEqual({ success: true });
        expect(calls).toEqual([{
            url: '/recording/7/update_transcript', method: 'POST',
            body: { transcript_data: [{ speaker: 'SPEAKER_00', sentence: 'hi' }], speaker_map: { SPEAKER_00: { name: 'Dana', isMe: false } }, regenerate_summary: false },
        }]);
    });

    it('rejects with the message of the server, or the fallback', async () => {
        await expect(saveTranscriptWithSpeakers(7, { transcriptData: [], speakerMap: {} }, { fetchImpl: reply(false, { error: 'No access' }) }))
            .rejects.toThrow('No access');
        await expect(saveTranscriptWithSpeakers(7, { transcriptData: [], speakerMap: {} }, { fetchImpl: reply(false, {}), fallbackMessage: 'Could not save' }))
            .rejects.toThrow('Could not save');
    });
});

describe('withAcceptedMatches', () => {
    const segments = [
        { speaker: 'Dana Lee', sentence: 'a', start_time: 0, end_time: 1, speaker_id: 4 },
        { speaker: 'SPEAKER_01', sentence: 'b', start_time: 1, end_time: 2 },
        { speaker: 'Dana Lee', sentence: 'c', start_time: 2, end_time: 3, speaker_id: 4 },
        { speaker: 'Dana Lee', sentence: 'd', start_time: 3, end_time: 4, speaker_id: 4 },
    ];
    const ids = [1, 2, 3, 4];

    it('sends the matched segments under their label, with the name in the speaker map', () => {
        const out = withAcceptedMatches(segments, ids, [{ label: 'SPEAKER_00', name: 'Dana Lee', speakerId: 4, ids: [1, 3] }]);
        expect(out.speakerMap).toEqual({ SPEAKER_00: { name: 'Dana Lee', isMe: false } });
        expect(out.transcriptData.map((s) => s.speaker)).toEqual(['SPEAKER_00', 'SPEAKER_01', 'SPEAKER_00', 'Dana Lee']);
        expect(out.transcriptData[0]).toEqual({ speaker: 'SPEAKER_00', sentence: 'a', start_time: 0, end_time: 1 });
        expect(out.transcriptData[3].speaker_id).toBe(4);
        expect(segments[0].speaker).toBe('Dana Lee');
    });

    it('leaves out a segment that was given another speaker afterwards, or removed', () => {
        const changed = segments.map((s, i) => (i === 0 ? { ...s, speaker: 'Kim' } : s));
        const out = withAcceptedMatches(changed, ids, [{ label: 'SPEAKER_00', name: 'Dana Lee', ids: [1, 3, 99] }]);
        expect(out.transcriptData.map((s) => s.speaker)).toEqual(['Kim', 'SPEAKER_01', 'SPEAKER_00', 'Dana Lee']);
    });

    it('returns null when no match still applies', () => {
        expect(withAcceptedMatches(segments, ids, [])).toBeNull();
        expect(withAcceptedMatches(segments, ids, [{ label: 'SPEAKER_00', name: 'Someone else', ids: [1] }])).toBeNull();
    });
});
