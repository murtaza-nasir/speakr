/**
 * Saving a transcript together with speaker names: one request used by the
 * Identify Speakers dialog and by the segment editor.
 *
 * POST /recording/<id>/update_transcript renames the labels in `speakerMap`
 * ({ label: { name, isMe } }) in `transcriptData`, stores the transcript,
 * links the names to saved speakers and trains their voice profiles from the
 * recording's voice embeddings. A voice match accepted in either place is
 * recorded this way.
 */

/** Only the entries of a speaker map that have a name. */
export function namedSpeakers(speakerMap) {
    const out = {};
    for (const [label, info] of Object.entries(speakerMap || {})) {
        if (info && info.name && String(info.name).trim() !== '') out[label] = info;
    }
    return out;
}

/**
 * Send the request. Resolves to the response body ({ recording, summary_queued, ... });
 * rejects with an Error carrying the server's message (or `fallbackMessage`).
 */
export async function saveTranscriptWithSpeakers(recordingId, { transcriptData, speakerMap, regenerateSummary = false }, { fallbackMessage = 'Failed to update transcript', fetchImpl } = {}) {
    const doFetch = fetchImpl || fetch;
    const csrfToken = typeof document !== 'undefined'
        ? document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') : undefined;
    const response = await doFetch(`/recording/${recordingId}/update_transcript`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({
            transcript_data: transcriptData,
            speaker_map: namedSpeakers(speakerMap),
            regenerate_summary: !!regenerateSummary,
        }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || fallbackMessage);
    return data;
}

/**
 * The transcript to send when voice matches were accepted in the segment
 * editor. The editor already shows the matched name; the server expects the
 * diarization label, which it renames itself and whose voice it then adds to
 * that person's profile. So each segment renamed by an accepted match (and
 * still carrying that name) goes out under its label again.
 *
 * segments: the stored form of the model's segments, in order; ids: their
 * ids in the same order; matches: model.acceptedMatches().
 * Returns { transcriptData, speakerMap }, or null when no match still applies.
 */
export function withAcceptedMatches(segments, ids, matches) {
    const labelOf = new Map();   // segment id -> label
    const speakerMap = {};
    const position = new Map(ids.map((id, i) => [id, i]));
    for (const match of matches || []) {
        let used = false;
        for (const id of match.ids || []) {
            const i = position.get(id);
            if (i === undefined || segments[i].speaker !== match.name) continue;
            labelOf.set(id, match.label);
            used = true;
        }
        if (used) speakerMap[match.label] = { name: match.name, isMe: false };
    }
    if (!labelOf.size) return null;
    const transcriptData = segments.map((seg, i) => {
        const label = labelOf.get(ids[i]);
        if (!label) return seg;
        const { speaker_id, ...rest } = seg;
        return { ...rest, speaker: label };
    });
    return { transcriptData, speakerMap };
}
