import { describe, it, expect, vi } from 'vitest';
import {
    RECORDING_MODES, DISPLAY_MEDIA_CONSTRAINTS, needsDisplay, canRecordSystemAudio, effectiveMode,
    stopStreams, closeAudioContext, CaptureError, acquireCapture, onShareEnded,
} from './audio-capture.js';

// ---- fakes of the media objects -----------------------------------------------

class FakeTrack {
    constructor(kind, label = kind) {
        this.kind = kind;
        this.label = label;
        this.readyState = 'live';
        this.listeners = {};
        this.stop = vi.fn(() => { this.readyState = 'ended'; });
    }
    addEventListener(name, fn) { (this.listeners[name] ||= new Set()).add(fn); }
    removeEventListener(name, fn) { this.listeners[name]?.delete(fn); }
    /** What the browser does when the share ends: the track ends and says so. */
    end() { this.readyState = 'ended'; for (const fn of [...(this.listeners.ended || [])]) fn(); }
}

class FakeStream {
    constructor(tracks = []) { this.tracks = tracks; }
    getTracks() { return this.tracks.slice(); }
    getAudioTracks() { return this.tracks.filter((t) => t.kind === 'audio'); }
    getVideoTracks() { return this.tracks.filter((t) => t.kind === 'video'); }
}
globalThis.MediaStream = FakeStream;

class FakeAudioContext {
    static made = [];
    constructor() {
        this.state = 'running';
        this.sources = [];
        this.close = vi.fn(async () => { this.state = 'closed'; });
        FakeAudioContext.made.push(this);
    }
    createMediaStreamDestination() {
        this.destination = { stream: new FakeStream([new FakeTrack('audio', 'mixed')]) };
        return this.destination;
    }
    createMediaStreamSource(stream) {
        const source = { stream, connect: vi.fn() };
        this.sources.push(source);
        return source;
    }
}

const mediaError = (name) => Object.assign(new Error(name), { name });

function devices({ display = true, displayAudio = true, displayError = null, micError = null } = {}) {
    const calls = [];
    const made = {};
    const md = {
        getUserMedia: vi.fn(async (constraints) => {
            calls.push('getUserMedia');
            if (micError) throw micError;
            made.mic = new FakeStream([new FakeTrack('audio', 'mic')]);
            made.micConstraints = constraints;
            return made.mic;
        }),
    };
    if (display) {
        md.getDisplayMedia = vi.fn(async (constraints) => {
            calls.push('getDisplayMedia');
            if (displayError) throw displayError;
            made.displayConstraints = constraints;
            made.display = new FakeStream([new FakeTrack('video', 'screen'), ...(displayAudio ? [new FakeTrack('audio', 'system')] : [])]);
            return made.display;
        });
    }
    return { md, calls, made };
}

const allStopped = (stream) => stream.getTracks().every((t) => t.stop.mock.calls.length > 0);

// ---- tests ----------------------------------------------------------------------------

describe('the sources of a recording', () => {
    it('are the three of the in-app recorder, two of them with a screen share', () => {
        expect(RECORDING_MODES).toEqual(['microphone', 'system', 'both']);
        expect(RECORDING_MODES.map(needsDisplay)).toEqual([false, true, true]);
    });

    it('offer system audio only where getDisplayMedia exists', () => {
        expect(canRecordSystemAudio(undefined)).toBe(false);
        expect(canRecordSystemAudio({ getUserMedia() {} })).toBe(false);
        expect(canRecordSystemAudio({ getUserMedia() {}, getDisplayMedia() {} })).toBe(true);
    });

    it('fall back to the microphone where system audio is not available or the choice is unknown', () => {
        expect(effectiveMode('both', true)).toBe('both');
        expect(effectiveMode('system', true)).toBe('system');
        expect(effectiveMode('system', false)).toBe('microphone');
        expect(effectiveMode('both', false)).toBe('microphone');
        expect(effectiveMode('loud', true)).toBe('microphone');
        expect(effectiveMode(null, true)).toBe('microphone');
    });

    it('ask the share for its audio as the in-app recorder does', () => {
        expect(DISPLAY_MEDIA_CONSTRAINTS).toEqual({ video: true, audio: true, audioSelection: 'preferred' });
        expect(Object.isFrozen(DISPLAY_MEDIA_CONSTRAINTS)).toBe(true);
    });
});

describe('stopStreams and closeAudioContext', () => {
    it('stop every track and skip what is missing', () => {
        const a = new FakeStream([new FakeTrack('audio'), new FakeTrack('video')]);
        const b = new FakeStream([new FakeTrack('audio')]);
        b.tracks[0].stop.mockImplementation(() => { throw new Error('already'); });
        stopStreams([a, null, undefined, b, {}]);
        expect(allStopped(a)).toBe(true);
        expect(b.tracks[0].stop).toHaveBeenCalled();
        expect(() => stopStreams(null)).not.toThrow();
    });

    it('close a context once and tolerate a closed or failing one', async () => {
        const ctx = new FakeAudioContext();
        closeAudioContext(ctx);
        await Promise.resolve();
        expect(ctx.close).toHaveBeenCalledTimes(1);
        closeAudioContext(ctx);
        expect(ctx.close).toHaveBeenCalledTimes(1);
        closeAudioContext({ state: 'running', close: () => Promise.reject(new Error('no')) });
        closeAudioContext({ state: 'running', close: () => { throw new Error('no'); } });
        closeAudioContext(null);
    });
});

describe('acquireCapture', () => {
    it('records the microphone alone without a share or an AudioContext', async () => {
        const { md, calls, made } = devices();
        const capture = await acquireCapture('microphone', { mediaDevices: md, AudioContext: FakeAudioContext });
        expect(calls).toEqual(['getUserMedia']);
        expect(made.micConstraints).toEqual({ audio: true });
        expect(capture.stream).toBe(made.mic);
        expect(capture.displayStream).toBe(null);
        expect(capture.audioContext).toBe(null);
        capture.release();
        expect(allStopped(made.mic)).toBe(true);
    });

    it('records the system audio of the share, its video stopped at once', async () => {
        const { md, calls, made } = devices();
        const capture = await acquireCapture('system', { mediaDevices: md, AudioContext: FakeAudioContext });
        expect(calls).toEqual(['getDisplayMedia']);
        expect(made.displayConstraints).toEqual(DISPLAY_MEDIA_CONSTRAINTS);
        expect(made.display.getVideoTracks()[0].stop).toHaveBeenCalled();
        expect(made.display.getAudioTracks()[0].stop).not.toHaveBeenCalled();
        expect(capture.stream.getTracks()).toEqual(made.display.getAudioTracks());
        expect(capture.micStream).toBe(null);
        expect(capture.audioContext).toBe(null);
        capture.release();
        expect(allStopped(made.display)).toBe(true);
    });

    it('mixes the microphone and the system audio, the share asked for first', async () => {
        FakeAudioContext.made = [];
        const { md, calls, made } = devices();
        const capture = await acquireCapture('both', { mediaDevices: md, AudioContext: FakeAudioContext });
        expect(calls).toEqual(['getDisplayMedia', 'getUserMedia']);
        const [ctx] = FakeAudioContext.made;
        expect(capture.audioContext).toBe(ctx);
        expect(capture.stream).toBe(ctx.destination.stream);
        expect(ctx.sources).toHaveLength(2);
        expect(ctx.sources[0].stream).toBe(made.mic);
        expect(ctx.sources[1].stream.getTracks()).toEqual(made.display.getAudioTracks());
        for (const source of ctx.sources) expect(source.connect).toHaveBeenCalledWith(ctx.destination);
        capture.release();
        capture.release();
        expect(allStopped(made.display) && allStopped(made.mic)).toBe(true);
        expect(ctx.close).toHaveBeenCalledTimes(1);
    });

    it('resumes a suspended context', async () => {
        class Suspended extends FakeAudioContext {
            constructor() { super(); this.state = 'suspended'; this.resume = vi.fn(async () => { this.state = 'running'; }); }
        }
        const { md } = devices();
        const capture = await acquireCapture('both', { mediaDevices: md, AudioContext: Suspended });
        expect(capture.audioContext.resume).toHaveBeenCalled();
        capture.release();
    });

    it('tells a canceled share from a failed one', async () => {
        for (const [error, reason] of [[mediaError('NotAllowedError'), 'shareCancelled'], [mediaError('NotReadableError'), 'captureFailed']]) {
            const { md, calls } = devices({ displayError: error });
            const failure = await acquireCapture('both', { mediaDevices: md, AudioContext: FakeAudioContext }).catch((e) => e);
            expect(failure).toBeInstanceOf(CaptureError);
            expect(failure.reason).toBe(reason);
            expect(failure.cause).toBe(error);
            expect(calls).toEqual(['getDisplayMedia']);
        }
    });

    it('stops a share without audio and asks for no microphone', async () => {
        const { md, calls, made } = devices({ displayAudio: false });
        const failure = await acquireCapture('both', { mediaDevices: md, AudioContext: FakeAudioContext }).catch((e) => e);
        expect(failure.reason).toBe('noAudioTrack');
        expect(allStopped(made.display)).toBe(true);
        expect(calls).toEqual(['getDisplayMedia']);
    });

    it('stops the share when the microphone is refused', async () => {
        for (const [error, reason] of [[mediaError('NotAllowedError'), 'microphoneDenied'], [mediaError('NotFoundError'), 'noMicrophone']]) {
            const { md, made } = devices({ micError: error });
            const failure = await acquireCapture('both', { mediaDevices: md, AudioContext: FakeAudioContext }).catch((e) => e);
            expect(failure.reason).toBe(reason);
            expect(allStopped(made.display)).toBe(true);
        }
        const { md } = devices({ micError: mediaError('NotAllowedError') });
        expect((await acquireCapture('microphone', { mediaDevices: md }).catch((e) => e)).reason).toBe('microphoneDenied');
    });

    it('releases everything when the mixing fails', async () => {
        const { md, made } = devices();
        class Broken extends FakeAudioContext {
            createMediaStreamDestination() { throw new Error('no destination'); }
        }
        FakeAudioContext.made = [];
        const failure = await acquireCapture('both', { mediaDevices: md, AudioContext: Broken }).catch((e) => e);
        expect(failure.reason).toBe('noMicrophone');
        expect(allStopped(made.display) && allStopped(made.mic)).toBe(true);
        expect(FakeAudioContext.made[0].close).toHaveBeenCalled();
        const again = devices();
        expect((await acquireCapture('both', { mediaDevices: again.md }).catch((e) => e)).reason).toBe('noMicrophone');
        expect(allStopped(again.made.display) && allStopped(again.made.mic)).toBe(true);
    });

    it('refuses a source the browser does not have, before asking for anything', async () => {
        const { md, calls } = devices({ display: false });
        expect((await acquireCapture('system', { mediaDevices: md }).catch((e) => e)).reason).toBe('unavailable');
        expect((await acquireCapture('both', { mediaDevices: md }).catch((e) => e)).reason).toBe('unavailable');
        expect((await acquireCapture('other', { mediaDevices: md }).catch((e) => e)).reason).toBe('unavailable');
        expect((await acquireCapture('microphone', { mediaDevices: undefined }).catch((e) => e)).reason).toBe('noMicrophone');
        expect(calls).toEqual([]);
    });
});

describe('onShareEnded', () => {
    it('calls back once when the share ends from the browser, and stops listening', async () => {
        const { md, made } = devices();
        const capture = await acquireCapture('both', { mediaDevices: md, AudioContext: FakeAudioContext });
        const onEnd = vi.fn();
        onShareEnded(capture, onEnd);
        made.display.getAudioTracks()[0].end();
        made.display.getVideoTracks()[0].end();
        expect(onEnd).toHaveBeenCalledTimes(1);
        expect(made.display.getTracks().every((t) => !t.listeners.ended || t.listeners.ended.size === 0)).toBe(true);
    });

    it('can be stopped before, and does nothing without a share', async () => {
        const { md, made } = devices();
        const capture = await acquireCapture('system', { mediaDevices: md });
        const onEnd = vi.fn();
        const stop = onShareEnded(capture, onEnd);
        stop();
        made.display.getAudioTracks()[0].end();
        expect(onEnd).not.toHaveBeenCalled();
        const mic = await acquireCapture('microphone', { mediaDevices: devices().md });
        expect(() => onShareEnded(mic, onEnd)()).not.toThrow();
        expect(() => onShareEnded(null, onEnd)()).not.toThrow();
    });
});
