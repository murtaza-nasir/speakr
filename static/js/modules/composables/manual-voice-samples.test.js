import {beforeEach, afterEach, it, expect, vi} from 'vitest';
import {useManualVoiceSamples, secondsToMilliseconds, formatManualRangeTime, parseManualRangeTime} from './manual-voice-samples.js';

let state, ui, watchers;
const ref = value => ({value});
const reply = (data, ok = true) => ({ok, json:async () => data});
const ready = () => ({state:'ready', range:{start_ms:72500, end_ms:94000}, speech_ms:19200, space_id:3});
const deferred = () => {let resolve; const promise = new Promise(r => resolve = r); return {resolve, promise};};
beforeEach(async () => {
    vi.useFakeTimers(); watchers = [];
    vi.stubGlobal('Vue', {ref, computed:fn => ({get value() {return fn();}}), watch:(fn, cb) => watchers.push(cb)});
    vi.stubGlobal('document', {querySelector:() => ({getAttribute:() => 'csrf'})});
    vi.stubGlobal('window', {});
    state = {selectedRecording:ref({id:9, audio_available:true, can_edit:true, duration:180}), showAsrEditorModal:ref(true),
        modalAudioCurrentTime:ref(72.5004), editingSegments:ref([{start_time:1, end_time:25, sentence:'Dirty text', speaker:'A'}]),
        speakerMap:ref({A:{name:'Unsaved name'}})};
    vi.stubGlobal('fetch', vi.fn(async (url, opts) => {
        if (url === '/speakers') return reply([{id:5,name:'Person'}]);
        if (url.endsWith('/prepare')) return reply({job_id:'job', state:'preparing'});
        if (opts?.method === 'DELETE') return reply({state:'cancelled'});
        if (url.includes('/preparations/')) return reply(ready());
        if (opts?.method === 'POST') return reply({id:'sample'});
        return reply({samples:[]});
    }));
    ui = useManualVoiceSamples(state);
    await ui.openManualVoice();
    ui.manualSpeakerId.value = '5'; ui.manualStart.value = '00:01:12.500'; ui.manualEnd.value = '00:01:34.000';
    await ui.changeManualContext();
});
afterEach(() => {ui.closeManualVoice(); vi.useRealTimers(); vi.unstubAllGlobals();});

it('rounds source seconds once and parses precise displayed milliseconds across hour boundaries', () => {
    expect(secondsToMilliseconds(72.5004)).toBe(72500);
    expect(formatManualRangeTime(secondsToMilliseconds(3599.9996))).toBe('01:00:00.000');
    expect(parseManualRangeTime('01:00:00.000')).toBe(3600000);
    for (const text of ['00:00:01.1234','00:60:01.000','false','72.5','-01:00:00.000']) expect(parseManualRangeTime(text)).toBeNull();
});
it('finishes loading people when the range changes before the response arrives', async () => {
    const late = deferred();
    ui.manualSpeakers.value = [];
    fetch.mockImplementationOnce(() => late.promise);
    const pending = ui.openManualVoice();
    ui.manualEnd.value = '00:01:35.000'; await ui.changeManualContext();
    late.resolve(reply([{id:5,name:'Person'}])); await pending;
    expect(ui.manualSpeakers.value).toEqual([{id:5,name:'Person'}]);
    expect(ui.manualLoading.value).toBe(false);
    expect(ui.canPrepareManualVoice.value).toBe(true);
});
it('does not let a closed opening replace people or finish loading a newer opening', async () => {
    const old = deferred(), fresh = deferred();
    ui.manualSpeakers.value = [];
    fetch.mockImplementationOnce(() => old.promise).mockImplementationOnce(() => fresh.promise);
    const oldPending = ui.openManualVoice();
    ui.closeManualVoice(); const freshPending = ui.openManualVoice();
    old.resolve(reply([{id:7,name:'Stale'}])); await oldPending;
    expect(ui.manualSpeakers.value).toEqual([]);
    expect(ui.manualLoading.value).toBe(true);
    fresh.resolve(reply([{id:5,name:'Person'}])); await freshPending;
    expect(ui.manualSpeakers.value).toEqual([{id:5,name:'Person'}]);
    expect(ui.manualLoading.value).toBe(false);
});
it('discards pending people after the recording changes and closes the panel', async () => {
    const late = deferred(); ui.manualSpeakers.value = [];
    fetch.mockImplementationOnce(() => late.promise);
    const pending = ui.openManualVoice();
    state.selectedRecording.value.id = 10; watchers[0]();
    late.resolve(reply([{id:5,name:'Person'}])); await pending;
    expect(ui.manualVoiceOpen.value).toBe(false);
    expect(ui.manualSpeakers.value).toEqual([]);
});
it('does not focus a newer opening when an older sample-list response completes late', async () => {
    const late = deferred(), focus = vi.fn();
    vi.stubGlobal('document', {querySelector:() => ({focus, getAttribute:() => 'csrf'})});
    fetch.mockClear();
    fetch.mockResolvedValueOnce(reply([{id:5,name:'Person'}])).mockImplementationOnce(() => late.promise);
    const oldPending = ui.openManualVoice();
    await vi.waitFor(() => expect(fetch.mock.calls.some(([url]) => url === '/speakers/5/manual_voice_samples')).toBe(true));
    ui.closeManualVoice(); await ui.openManualVoice();
    expect(focus).toHaveBeenCalledTimes(1);
    late.resolve(reply({samples:[]})); await oldPending;
    expect(focus).toHaveBeenCalledTimes(1);
});
it('sends the integer displayed range, exposes private clip, and only commits after separate action without changing dirty draft', async () => {
    const draft = JSON.stringify([state.editingSegments.value, state.speakerMap.value, state.selectedRecording.value]);
    await ui.prepareManualVoice();
    const request = fetch.mock.calls.find(([url]) => url.endsWith('/prepare'));
    expect(JSON.parse(request[1].body)).toEqual({speaker_id:5,start_ms:72500,end_ms:94000});
    expect(request[1].headers['X-CSRFToken']).toBe('csrf');
    expect(ui.canCommitManualVoice.value).toBe(true);
    expect(ui.manualPreparedAudioUrl.value).toBe('/recordings/9/manual_voice_samples/preparations/job/audio');
    expect(fetch.mock.calls.filter(([url,opts]) => url === '/speakers/5/manual_voice_samples' && opts?.method === 'POST')).toHaveLength(0);
    expect(await ui.commitManualVoice()).toBe(true);
    expect(JSON.stringify([state.editingSegments.value,state.speakerMap.value,state.selectedRecording.value])).toBe(draft);
    expect(ui.manualJob.value.state).toBe('committed');
});
it.each(['range','speaker','recording','close'])('invalidates a late prepare handshake after %s change and cancels accepted UUID', async change => {
    const late = deferred();
    fetch.mockImplementationOnce(() => late.promise);
    const pending = ui.prepareManualVoice();
    if (change === 'range') {ui.manualEnd.value = '00:01:35.000'; await ui.changeManualContext();}
    if (change === 'speaker') {ui.manualSpeakerId.value = ''; await ui.changeManualContext();}
    if (change === 'recording') {state.selectedRecording.value.id = 10; watchers[0]();}
    if (change === 'close') ui.closeManualVoice();
    late.resolve(reply({job_id:'late',state:'preparing'})); await pending;
    expect(ui.manualJob.value).toBeNull(); expect(ui.canCommitManualVoice.value).toBe(false);
    expect(fetch.mock.calls.some(([url,opts]) => url.endsWith('/late') && opts?.method === 'DELETE')).toBe(true);
});
it('ignores a late ready poll after changed range even if fetch abort is not honored', async () => {
    const late = deferred();
    fetch.mockImplementation(async (url,opts) => url.endsWith('/prepare') ? reply({job_id:'job'}) : opts?.method === 'DELETE' ? reply({}) : late.promise);
    const pending = ui.prepareManualVoice(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    ui.manualStart.value = '00:01:13.000'; const invalidate = ui.changeManualContext();
    late.resolve(reply(ready())); await pending; await invalidate;
    expect(ui.manualJob.value).toBeNull(); expect(ui.manualPreparedAudioUrl.value).toBe('');
});
it('handles cancel/deadline without polling or permitting commit', async () => {
    fetch.mockImplementation(async (url,opts) => url.endsWith('/prepare') ? reply({job_id:'job'}) : opts?.method === 'DELETE' ? reply({}) : reply({state:'preparing'}));
    await ui.prepareManualVoice(); await vi.advanceTimersByTimeAsync(300001);
    expect(ui.manualError.value).toBe('timeout'); expect(ui.canCommitManualVoice.value).toBe(false);
    const calls = fetch.mock.calls.length; await vi.advanceTimersByTimeAsync(10000); expect(fetch).toHaveBeenCalledTimes(calls);
});
it('does not let a late ready response revive a timed-out preparation when abort is ignored', async () => {
    const late = deferred();
    fetch.mockImplementation(async (url,opts) => url.endsWith('/prepare') ? reply({job_id:'job'}) : opts?.method === 'DELETE' ? reply({}) : late.promise);
    const pending = ui.prepareManualVoice(); await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    await vi.advanceTimersByTimeAsync(300001); late.resolve(reply(ready())); await pending;
    expect(ui.manualError.value).toBe('timeout'); expect(ui.manualJob.value.state).toBe('failed'); expect(ui.canCommitManualVoice.value).toBe(false);
});
it('expires a ready result and requires a new explicit preparation', async () => {
    await ui.prepareManualVoice(); await vi.advanceTimersByTimeAsync(900001);
    expect(ui.manualError.value).toBe('expired'); expect(ui.canCommitManualVoice.value).toBe(false);
    expect(ui.canPrepareManualVoice.value).toBe(true); expect(await ui.commitManualVoice()).toBe(false);
});
it('uses server terminal expiry rather than extending TTL from the first ready poll', async () => {
    const expiresAt = Date.now() + 2000;
    fetch.mockImplementation(async (url,opts) => url.endsWith('/prepare') ? reply({job_id:'job'}) : opts?.method === 'DELETE' ? reply({}) : reply({...ready(), expires_at:new Date(expiresAt).toISOString()}));
    await ui.prepareManualVoice(); expect(ui.manualJob.value.expiresAt).toBe(expiresAt);
    await vi.advanceTimersByTimeAsync(2001); expect(ui.manualError.value).toBe('expired'); expect(ui.canCommitManualVoice.value).toBe(false);
});
it('shows a safe private playback failure and does not allow committing its unavailable result', async () => {
    await ui.prepareManualVoice(); ui.onManualPreparedError();
    expect(ui.manualError.value).toBe('unavailable'); expect(ui.canCommitManualVoice.value).toBe(false);
    expect(ui.canPrepareManualVoice.value).toBe(true);
});
it('rejects ready results with different boundaries or missing diagnostic fields', async () => {
    fetch.mockImplementation(async url => url.endsWith('/prepare') ? reply({job_id:'job'}) : reply({...ready(),range:{start_ms:0,end_ms:94000}}));
    await ui.prepareManualVoice(); expect(ui.manualError.value).toBe('provider'); expect(ui.canCommitManualVoice.value).toBe(false);
});
it('allows exactly-once receipt retry after a lost commit response', async () => {
    await ui.prepareManualVoice(); fetch.mockRejectedValueOnce(new Error('connection lost'));
    expect(await ui.commitManualVoice()).toBe(false); expect(ui.canCommitManualVoice.value).toBe(true);
    expect(await ui.commitManualVoice()).toBe(true);
    const posts = fetch.mock.calls.filter(([url,opts]) => url === '/speakers/5/manual_voice_samples' && opts?.method === 'POST');
    expect(posts.map(([,opts]) => JSON.parse(opts.body))).toEqual([{job_id:'job'},{job_id:'job'}]);
});
it('bounds playback independently at end and pauses preview when fields change', async () => {
    const audio = {currentTime:0, pause:vi.fn(), play:vi.fn(async () => {})}; ui.manualSourceAudio.value = audio;
    await ui.playManualRange(); expect(audio.currentTime).toBe(72.5); expect(ui.manualRangePlaying.value).toBe(true);
    audio.currentTime = 94; await vi.advanceTimersByTimeAsync(25);
    expect(audio.pause).toHaveBeenCalled(); expect(ui.manualRangePlaying.value).toBe(false);
    await ui.playManualRange(); ui.manualEnd.value = '00:01:35.000'; await ui.changeManualContext(); expect(ui.manualRangePlaying.value).toBe(false);
});
it('guards malformed bounds/read-only/incognito audio while permitting independent preview without a profile', () => {
    ui.manualSpeakerId.value = ''; expect(ui.canListenManualRange.value).toBe(true); expect(ui.canPrepareManualVoice.value).toBe(false);
    ui.manualSpeakerId.value = '5'; state.selectedRecording.value.can_edit = false; expect(ui.canPrepareManualVoice.value).toBe(false);
    state.selectedRecording.value.can_edit = true; state.selectedRecording.value.incognito = true; expect(ui.canListenManualRange.value).toBe(false);
    state.selectedRecording.value.incognito = false; ui.manualEnd.value = '00:10:00.000'; expect(ui.canPrepareManualVoice.value).toBe(false);
});
it('removes UUID sample and hides source links when deleted or access revoked', async () => {
    expect(ui.manualSourceLink({recording_id:9,source_available:false})).toBe('');
    expect(ui.manualSourceLink({recording_id:null,source_available:false})).toBe('');
    expect(ui.manualSourceLink({recording_id:9,source_available:true})).toBe('/recordings/9');
    await ui.deleteManualVoice({id:'uuid-sample'});
    expect(fetch.mock.calls.some(([url,opts]) => url === '/speakers/5/manual_voice_samples/uuid-sample' && opts?.method === 'DELETE')).toBe(true);
});
it('offers safe error and local check URL without exposing raw provider text or paths', async () => {
    fetch.mockResolvedValueOnce(reply({code:'space',check_url:'/admin#voice-embeddings',error:'/private/path: secret provider response'},false));
    await ui.prepareManualVoice(); expect(ui.manualError.value).toBe('space'); expect(ui.manualCheckUrl.value).toBe('/admin#voice-embeddings');
    expect(JSON.stringify(ui.manualJob.value)).not.toContain('secret');
});
