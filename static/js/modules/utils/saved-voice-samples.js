import { formatManualRangeTime } from '../composables/manual-voice-samples.js';

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function sampleEndpoint(speakerId, sampleId) {
    if (!Number.isSafeInteger(Number(speakerId)) || Number(speakerId) <= 0 || !uuid.test(sampleId)) throw Error('access');
    return `/speakers/${Number(speakerId)}/manual_voice_samples/${sampleId}`;
}
export function sampleLink(speakerId, sample) {
    sampleEndpoint(speakerId, sample.id);
    if (!Number.isSafeInteger(Number(sample.recording_id)) || Number(sample.recording_id) <= 0) throw Error('access');
    return `/recordings/${Number(sample.recording_id)}?voice_sample=${sample.id}&voice_speaker=${Number(speakerId)}`;
}
export function parseSampleLink(url) {
    const params = new URLSearchParams(url.search), sampleId = params.get('voice_sample'), speakerId = params.get('voice_speaker');
    if (sampleId === null && speakerId === null) return null;
    sampleEndpoint(speakerId, sampleId);
    return {sampleId, speakerId:Number(speakerId)};
}
export function rangeOverlaps(segment, range) {
    return !!range && Number.isFinite(segment?.start_time) && Number.isFinite(segment?.end_time) &&
        segment.start_time*1000 < range.end_ms && segment.end_time*1000 > range.start_ms;
}
export function rangeText(sample) {
    return `${formatManualRangeTime(sample.start_ms)}–${formatManualRangeTime(sample.end_ms)}`;
}

export function playbackErrorKey(code) {
    const keys={access:'manualVoice.playbackErrors.access',changed:'manualVoice.playbackErrors.changed',
        expired:'manualVoice.playbackErrors.expired',limit:'manualVoice.playbackErrors.limit',
        busy:'manualVoice.playbackErrors.busy',timeout:'manualVoice.playbackErrors.timeout',
        unsupported:'manualVoice.playbackErrors.unsupported',cancelled:'manualVoice.playbackErrors.cancelled',
        unavailable:'manualVoice.playbackErrors.unavailable'};
    return keys[code] || keys.unavailable;
}

export class SavedSamplePlayback {
    constructor(changed = () => {}, options = {}) {
        this.changed = changed; this.request = options.request || ((...args) => fetch(...args));
        this.sleep = options.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)));
        this.generation = 0; this.job = null; this.timer = null;
    }
    async release(job) {
        if (job) try {await this.request(`${job.endpoint}?lease=${job.lease}`, {method:'DELETE',
            headers:{'X-CSRFToken':document.querySelector('meta[name=csrf-token]')?.content || ''}, keepalive:true});} catch {}
    }
    stop() {
        this.generation++; clearTimeout(this.timer); this.timer = null;
        const job = this.job; this.job = null; void this.release(job);
    }
    async start(speakerId, sampleId) {
        this.stop(); const token = this.generation, base = sampleEndpoint(speakerId, sampleId);
        this.changed({state:'preparing'});
        let job = null;
        try {
            const response = await this.request(`${base}/playback`, {method:'POST',
                headers:{'Content-Type':'application/json','X-CSRFToken':document.querySelector('meta[name=csrf-token]')?.content || ''}, body:'{}'});
            let data = await response.json();
            if (!response.ok) throw Error(data.code || 'unavailable');
            if (!uuid.test(data.preview_id) || !/^[a-f0-9]{32}$/.test(data.lease)) throw Error('unavailable');
            job = {endpoint:`${base}/playbacks/${data.preview_id}`, lease:data.lease};
            if (token !== this.generation) {void this.release(job); return null;}
            this.job = job;
            const deadline = Date.now()+305000;
            while (data.state === 'preparing') {
                await this.sleep(250);
                if (token !== this.generation) return null;
                if (Date.now() > deadline) throw Error('timeout');
                const status = await this.request(`${job.endpoint}?lease=${job.lease}`);
                data = await status.json();
                if (!status.ok) throw Error(data.code || 'unavailable');
            }
            if (token !== this.generation) return null;
            if (data.state !== 'ready') throw Error(data.code || 'unavailable');
            if (!Number.isInteger(data.start_ms) || !Number.isInteger(data.end_ms) ||
                data.start_ms < 0 || data.end_ms <= data.start_ms || data.end_ms-data.start_ms > 300000 ||
                !Number.isSafeInteger(data.recording_id) || data.recording_id<=0 || !Number.isFinite(data.expires_at) || data.expires_at<=Date.now()) throw Error('unavailable');
            const ready = {...data, lease:job.lease, audioUrl:`${job.endpoint}/audio?lease=${job.lease}`};
            this.changed(ready);
            this.timer = setTimeout(() => {if (token === this.generation) {this.stop(); this.changed({state:'failed',code:'expired'});}}, data.expires_at-Date.now());
            return ready;
        } catch (error) {
            if (token === this.generation) {
                this.stop(); this.changed({state:'failed', code:['access','changed','expired','limit','busy','timeout','unsupported','cancelled'].includes(error.message) ? error.message : 'unavailable'});
            } else if (job && job !== this.job) void this.release(job);
            return null;
        }
    }
}
