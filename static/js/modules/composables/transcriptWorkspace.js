import { serializeAsrDraft } from './asrWorkplace.js';

export function workspaceFingerprint(segments, map) {
    return payloadFingerprint(serializeAsrDraft(segments), map);
}

function payloadFingerprint(payload, map) {
    return JSON.stringify([payload, Object.keys(map).sort().map(key =>
        [key, map[key].name || '', !!map[key].isMe])]);
}

/** Owns the JSON session; speaker and ASR tools edit the same segment objects. */
export function useTranscriptWorkspace(state, utils, editor, speakers, modal) {
    const { ref, computed, watch, nextTick } = Vue;
    const mode = ref('editor'), phonePane = ref('editor'), pinned = ref([]), followUp = ref({}), loading = ref(false), retryingSummary = ref(false);
    const origins = new WeakMap(), keys = new WeakMap();
    const raw = Vue.toRaw || (value => value);
    const resolveOrigin = origin => {
        if (origin?.parent) {
            resolveOrigin(origin.parent);
            if ((origin.parent.ack || 0) !== origin.parentAck) {
                origin.before = origin.parent.before;
                origin.labels = origin.labels.filter(label => origin.parent.labels.includes(label));
                origin.parentAck = origin.parent.ack || 0;
                origin.ack = (origin.ack || 0) + 1;
            }
        }
        return origin;
    };
    const getOrigin = segment => resolveOrigin(origins.get(raw(segment)));
    const addedSpeakers = ref([]);
    let sequence = 0, generation = 0, beforeNames = [], summaryConfirmed = '', initialRevision = '';
    const active = () => state.showAsrEditorModal.value;
    const finalName = (speaker, map = state.speakerMap.value) =>
        map[speaker]?.name?.trim() || (map[speaker]?.isMe ? state.currentUserName.value || 'Me' : speaker);
    const metadata = () => state.speakerMap.value;
    const fingerprint = payload => payloadFingerprint(payload, metadata());
    const revision = () => serializeAsrDraft(state.editingSegments.value.map(s => ({...s, speaker: finalName(s.speaker)})));
    const draftSpeakerNames = computed(() => [...new Set(state.editingSegments.value.map(s => s.speaker).filter(Boolean))]);
    const syncSpeakers = () => {
        const names = [...new Set([...draftSpeakerNames.value, ...addedSpeakers.value])];
        const next = {...state.speakerMap.value};
        for (const name of names) if (!next[name]) next[name] = {name: '', isMe: false, color: speakers.getSpeakerColor(name)};
        // Keep the collection identities while typing a name: replacing the
        // entire map would invalidate every transcript row's speaker lookup.
        if (names.some(name => !state.speakerMap.value[name])) state.speakerMap.value = next;
        if (JSON.stringify(state.modalSpeakers.value) !== JSON.stringify(names)) state.modalSpeakers.value = names;
        const available = [...new Set([...names, ...names.map(n => finalName(n))])];
        if (JSON.stringify(state.availableSpeakers.value) !== JSON.stringify(available)) state.availableSpeakers.value = available;
    };
    const start = async (entry = 'editor') => {
        generation++;
        loading.value = true;
        mode.value = entry; phonePane.value = entry === 'speakers' ? 'speakers' : 'editor';
        pinned.value = []; followUp.value = {}; retryingSummary.value = false;
        addedSpeakers.value = [];
        state.speakerMap.value = {};
        state.voiceSuggestions.value = {};
        state.isAutoIdentifying.value = false;
        state.regenerateSummaryAfterSpeakerUpdate.value = entry === 'speakers';
        beforeNames = [...new Set(state.editingSegments.value.map(s => s.speaker))];
        const record = state.selectedRecording.value;
        let context = {};
        const token = generation;
        if (!record.incognito && record.can_edit !== false && !record.is_read_only) {
            let response;
            try {
                response = await fetch(`/recording/${record.id}/workspace_context`);
            } catch (error) {
                if (token !== generation || !active() || state.selectedRecording.value?.id !== record.id) return false;
                throw error;
            }
            if (token !== generation || !active() || state.selectedRecording.value?.id !== record.id) return false;
            if (!response.ok) throw Error('Unable to load speaker provenance');
            try {
                context = await response.json();
            } catch (error) {
                if (token !== generation || !active() || state.selectedRecording.value?.id !== record.id) return false;
                throw error;
            }
            if (token !== generation || !active() || state.selectedRecording.value?.id !== record.id) return false;
        }
        const embeddings = Object.fromEntries((context.voice_labels || []).map(label => [label, true]));
        const labels = context.speaker_label_map || {};
        for (const segment of state.editingSegments.value) {
            const sources = Object.keys({...embeddings, ...labels}).filter(label => label === segment.speaker || labels[label] === segment.speaker);
            origins.set(raw(segment), {before: segment.speaker, labels: sources});
        }
        syncSpeakers();
        initialRevision = revision(); summaryConfirmed = initialRevision;
        loading.value = false;
        if (!record.incognito) speakers.loadVoiceSuggestions();
        return true;
    };
    const stop = () => { generation++; loading.value = false; pinned.value = []; state.isAutoIdentifying.value = false; speakers.showAutoIdDropdown.value = false; };
    const openEditor = async index => {
        if (!active() || editor.asrEditingLocked.value) return;
        mode.value = 'editor'; phonePane.value = 'editor';
        await editor.selectAsrSegment(index);
        await nextTick(); document.querySelector('[data-testid="asr-segment-text"]')?.focus();
    };
    const back = () => { mode.value = 'speakers'; phonePane.value = 'transcript'; };
    const split = (original, parts) => {
        const origin = getOrigin(original);
        for (const part of parts) if (origin) origins.set(raw(part), {...origin, parent:origin, parentAck:origin.ack || 0});
        pinned.value = parts;
    };
    const keyOf = segment => {
        if (!keys.has(raw(segment))) keys.set(raw(segment), ++sequence);
        return keys.get(raw(segment));
    };
    const visible = computed(() => {
        const allowed = new Set(modal.visibleModalSegments.value.map(s => s.index));
        return modal.speakerModalData.value.segments.filter(s => allowed.has(s.index) || pinned.value.includes(state.editingSegments.value[s.index]));
    });
    const canNavigate = delta => {
        const current = visible.value.findIndex(s => s.index === editor.asrSelectedIndex.value);
        return !!visible.value[current + delta];
    };
    const navigate = delta => {
        const current = visible.value.findIndex(s => s.index === editor.asrSelectedIndex.value);
        const next = visible.value[current + delta];
        if (next) return editor.selectAsrSegment(next.index);
    };
    const effects = (snapshot, map) => {
        const changes = [], groups = new Map();
        snapshot.forEach((segment, index) => {
            const origin = getOrigin(state.editingSegments.value[index]);
            const name = finalName(segment.speaker, map);
            const prior = origin?.before ?? beforeNames[0];
            if (origin?.labels.length) for (const label of origin.labels) {
                changes.push({current_before: prior, final_name: name, source_label: label});
                if (!groups.has(label)) groups.set(label, new Set());
                groups.get(label).add(name);
            } else if (prior != null) changes.push({current_before: prior, final_name: name});
        });
        const invalid = [], training = {}, seconds = {};
        for (const [label, names] of groups) {
            if (names.size > 1) invalid.push(label);
            else {
                const name = [...names][0];
                // A stored merged name cannot recover per-label segment durations.
                const ambiguous = snapshot.some((s, i) => {
                    const labels = getOrigin(state.editingSegments.value[i])?.labels || [];
                    return labels.includes(label) && labels.length > 1;
                });
                if (!ambiguous && !/^SPEAKER_\d+$/i.test(name) && changes.some(c => c.source_label === label && c.current_before !== name)) {
                    training[label] = name;
                    seconds[label] = snapshot.reduce((sum, s, i) => sum + (getOrigin(state.editingSegments.value[i])?.labels.includes(label) && Number.isFinite(s.end_time-s.start_time) ? Math.max(0,s.end_time-s.start_time) : 0), 0);
                }
            }
        }
        return {assignment_changes: changes, eligible_training_assignments: training, training_seconds_by_label: seconds, invalidated_voice_labels: invalid};
    };
    const persist = async (payload, id, isCurrent, automatic = false) => {
        const snapshot = JSON.parse(payload), objects = [...state.editingSegments.value];
        const map = JSON.parse(JSON.stringify(metadata()));
        const captured = workspaceFingerprint(snapshot, map);
        const segments = snapshot.map(s => ({...s, speaker: finalName(s.speaker, map)}));
        const submittedRevision = serializeAsrDraft(segments);
        const summary = !automatic && state.regenerateSummaryAfterSpeakerUpdate.value && submittedRevision !== summaryConfirmed;
        const body = {transcript_data: segments, speaker_map: Object.fromEntries(segments.map(s => [s.speaker, {name: s.speaker}])),
            workspace_effects: effects(snapshot, map), regenerate_summary: summary};
        try {
            let data;
            if (state.selectedRecording.value.incognito) {
                data = {recording: {...state.selectedRecording.value, transcription: JSON.stringify(segments)}, follow_up: {}};
                sessionStorage.setItem('speakr_incognito_recording', JSON.stringify(data.recording));
            } else {
                try {
                    const response = await fetch(`/recording/${id}/update_transcript`, {method: 'POST',
                        headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.content},
                        body: JSON.stringify(body)});
                    data = await response.json();
                    if (!response.ok || data.persistence_status !== 'saved') throw Error(data.error || 'saveFailed');
                } catch (error) {
                    // A lost acknowledgement must not replay usage/training/summary.
                    const response = await fetch(`/api/recordings/${id}`);
                    if (!response.ok) throw error;
                    const readBack = await response.json(), record = readBack.recording || readBack;
                    const comparable = items => serializeAsrDraft(items.map(({speaker_id, ...s}) => ({...s, speaker:s.speaker.toLowerCase()})));
                    if (comparable(JSON.parse(record.transcription)) !== comparable(segments)) throw error;
                    data = {recording:record, follow_up:Object.fromEntries(['usage','training','snippets','reindex','summary','export'].map(key=>[key,'unknown']))};
                }
            }
            if (!isCurrent()) return {transcription: payload, fingerprint: captured};
            const stored = JSON.parse(data.recording.transcription);
            let acknowledged = captured;
            const normalized = snapshot.map((segment, i) => ({...stored[i], speaker: segment.speaker}));
            const applyNormalization = fingerprint(serializeAsrDraft(state.editingSegments.value)) === captured;
            if (applyNormalization) {
                const nextMap = {...state.speakerMap.value};
                stored.forEach((segment, i) => {
                    if (map[snapshot[i].speaker]?.name?.trim() || snapshot[i].speaker !== segment.speaker) nextMap[snapshot[i].speaker] = {...nextMap[snapshot[i].speaker], name:segment.speaker};
                });
                state.speakerMap.value = nextMap;
                acknowledged = workspaceFingerprint(normalized, nextMap);
            }
            objects.forEach((object, i) => {
                const origin = getOrigin(object);
                if (origin) {
                    origin.before = stored[i].speaker;
                    origin.labels = origin.labels.filter(label => !body.workspace_effects.invalidated_voice_labels.includes(label));
                    origin.ack = (origin.ack || 0) + 1;
                    origin.parent = null;
                } else origins.set(raw(object), {before: stored[i].speaker, labels: []});
            });
            beforeNames = [...new Set(stored.map(s => s.speaker))];
            followUp.value = data.follow_up || {};
            if (data.summary_queued || (summary && data.follow_up?.summary === 'unknown')) { summaryConfirmed = serializeAsrDraft(stored); utils.onChatComplete?.(); }
            Object.assign(state.selectedRecording.value, data.recording);
            if (data.summary_queued) {
                state.selectedRecording.value.status = 'SUMMARIZING';
                speakers.pollForSummaryCompletion?.(id);
            }
            const index = state.recordings.value.findIndex(r => r.id === id);
            if (index >= 0) state.recordings.value[index] = {...state.recordings.value[index], ...state.selectedRecording.value};
            if (Object.values(followUp.value).includes('failed')) utils.showToast(window.i18n.t('transcriptWorkspace.followUpFailed'), 'fa-exclamation-circle');
            return {transcription: serializeAsrDraft(normalized), fingerprint: acknowledged, applyNormalization};
        } catch (error) {
            if (isCurrent()) utils.setGlobalError(error.message);
            return false;
        }
    };
    const retrySummary = async () => {
        const record = state.selectedRecording.value;
        if (!active() || editor.asrEditingLocked.value || editor.asrDirty?.value || record.incognito || retryingSummary.value || followUp.value.summary !== 'failed') return;
        const token = generation, submittedRevision = serializeAsrDraft(JSON.parse(record.transcription));
        retryingSummary.value = true;
        try {
            const response = await fetch(`/recording/${record.id}/generate_summary`, {method:'POST',
                headers:{'Content-Type':'application/json','X-CSRFToken':document.querySelector('meta[name="csrf-token"]')?.content}, body:'{}'});
            const data = await response.json();
            if (!active() || token !== generation) return;
            if (!response.ok || !data.success) throw Error(data.error || 'Summary retry failed');
            followUp.value = {...followUp.value, summary:'queued', export:'queued'};
            summaryConfirmed = submittedRevision;
            record.status = 'SUMMARIZING';
            speakers.pollForSummaryCompletion?.(record.id);
        } catch (error) {
            if (active() && token === generation) utils.setGlobalError(error.message);
        } finally {
            if (token === generation) retryingSummary.value = false;
        }
    };
    watch(() => JSON.stringify([draftSpeakerNames.value, Object.values(state.speakerMap.value).map(s=>s.name)]), () => { if (active()) syncSpeakers(); });
    watch(modal.selectedSpeaker, () => { pinned.value = []; });
    utils.workspace = {active, loading, start, stop, split, fingerprint, persist, openEditor, back,
        showTranscript: () => { phonePane.value = 'transcript'; },
        addSpeaker: label => { addedSpeakers.value.push(label); syncSpeakers(); },
        needsSave: automatic => !automatic && state.regenerateSummaryAfterSpeakerUpdate.value && revision() !== summaryConfirmed,
        llmSnapshot: () => ({generation, revision: fingerprint(serializeAsrDraft(state.editingSegments.value))}),
        llmCurrent: token => active() && token.generation === generation && token.revision === fingerprint(serializeAsrDraft(state.editingSegments.value)),
        llmSessionCurrent: token => active() && token.generation === generation,
        llmPayload: () => ({transcript_data: JSON.parse(serializeAsrDraft(state.editingSegments.value))})};
    return {workspaceMode: mode, workspacePhonePane: phonePane, workspacePinned: pinned, workspaceFollowUp: followUp,
        retryWorkspaceSummary: retrySummary, workspaceRetryingSummary: retryingSummary,
        workspaceVisibleSegments: visible, workspaceSegmentKey: index => keyOf(state.editingSegments.value[index]),
        openSegmentEditor: openEditor, backToWorkspaceSpeakers: back, navigateWorkspaceSegment: navigate,
        canNavigateWorkspaceSegment: canNavigate, finishSplitContext: () => { pinned.value = []; },
        workspaceSpeakerName: finalName,
        formatSegmentClock: seconds => {
            if (seconds == null) return '—';
            const rounded = Math.round(seconds * 100) / 100;
            return `${Math.floor(rounded / 60)}:${(rounded % 60).toFixed(2).padStart(5, '0').replace(/\.00$/, '')}`;
        }};
}
