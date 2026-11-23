/**
 * The capture of a recording, for its three sources: the microphone, the
 * system audio (the audio of a shared screen, window or tab) and both.
 * Used by the offline recorder page (static/js/offline-recorder.js, #697),
 * and by the in-app recorder (composables/audio.js) for the constraints of
 * the screen share and for stopping streams and closing the AudioContext.
 * Tested by audio-capture.test.js.
 *
 * The system audio comes from getDisplayMedia(): the browser asks what to
 * share, and the share's video track is stopped at once, as the in-app
 * recorder does without video capture. For both sources the microphone and
 * the system audio are mixed with Web Audio into one stream.
 *
 * Firefox lets getDisplayMedia() run only in the transient activation of
 * the click, so it is the first await of a capture that needs it: the
 * microphone is asked for after the share.
 */

// No import: the offline page loads every module with this release's ?v=
// (the service worker keeps those URLs), and a static import would ask for
// the module without it.

export const RECORDING_MODES = ['microphone', 'system', 'both'];

/** What the in-app recorder asks of getDisplayMedia() at the click (composables/audio.js). */
export const DISPLAY_MEDIA_CONSTRAINTS = Object.freeze({ video: true, audio: true, audioSelection: 'preferred' });

/** Whether a source needs the screen share. */
export const needsDisplay = (mode) => mode === 'system' || mode === 'both';

/**
 * Whether the browser can record system audio: getDisplayMedia() exists,
 * which it does only in a secure context (HTTPS or localhost). The in-app
 * recorder's canRecordSystemAudio is the same test (app.modular.js).
 */
export function canRecordSystemAudio(mediaDevices) {
    return !!(mediaDevices && typeof mediaDevices.getDisplayMedia === 'function');
}

/** The source a recording uses: the one chosen, or the microphone where system audio is not available. */
export function effectiveMode(mode, systemAvailable) {
    return systemAvailable && RECORDING_MODES.includes(mode) ? mode : 'microphone';
}

/** Stop every track of the streams (null entries are skipped). */
export function stopStreams(streams) {
    for (const stream of streams || []) {
        if (!stream || typeof stream.getTracks !== 'function') continue;
        for (const track of stream.getTracks()) {
            try { track.stop(); } catch (e) { /* already stopped */ }
        }
    }
}

/**
 * Close an AudioContext. Browsers allow a few at a time (about six in
 * Chromium), so every capture closes its own.
 */
export function closeAudioContext(context) {
    if (!context || context.state === 'closed') return;
    try {
        const done = context.close();
        if (done && typeof done.catch === 'function') done.catch(() => {});
    } catch (e) { /* already closed */ }
}

/**
 * Why a capture failed:
 *   shareCancelled   the screen share was refused or canceled;
 *   noAudioTrack     the share has no audio (no "Share system audio" or "Share tab audio");
 *   captureFailed    the share failed otherwise (error.cause.message says why);
 *   microphoneDenied the microphone was refused;
 *   noMicrophone     no microphone, or no way to record;
 *   unavailable      the source is not available in this browser.
 */
export class CaptureError extends Error {
    constructor(reason, cause = null) {
        super(reason);
        this.name = 'CaptureError';
        this.reason = reason;
        this.cause = cause;
    }
}

/**
 * The streams of a recording from a source.
 *
 *   mode           'microphone' | 'system' | 'both'
 *   mediaDevices   navigator.mediaDevices
 *   AudioContext   the AudioContext class (needed for 'both')
 *   microphone     the audio constraints of getUserMedia (default true)
 *
 * Returns { mode, stream, displayStream, micStream, audioContext, release }:
 * stream is the one to record; release() stops every track (the share's
 * and the microphone's) and closes the AudioContext, and may be called
 * more than once. On a failure everything acquired so far is released and
 * a CaptureError is thrown.
 */
export async function acquireCapture(mode, { mediaDevices, AudioContext = null, microphone = true } = {}) {
    if (!RECORDING_MODES.includes(mode)) throw new CaptureError('unavailable');
    if (!mediaDevices || typeof mediaDevices.getUserMedia !== 'function') throw new CaptureError('noMicrophone');
    if (needsDisplay(mode) && !canRecordSystemAudio(mediaDevices)) throw new CaptureError('unavailable');

    let displayStream = null;
    let micStream = null;
    let audioContext = null;
    let released = false;
    const release = () => {
        if (released) return;
        released = true;
        stopStreams([displayStream, micStream]);
        closeAudioContext(audioContext);
    };

    // The share first: the first await after the click (Firefox).
    let systemTrack = null;
    if (needsDisplay(mode)) {
        try {
            displayStream = await mediaDevices.getDisplayMedia({ ...DISPLAY_MEDIA_CONSTRAINTS });
        } catch (error) {
            throw new CaptureError(error && error.name === 'NotAllowedError' ? 'shareCancelled' : 'captureFailed', error);
        }
        systemTrack = displayStream.getAudioTracks()[0] || null;
        if (!systemTrack) {
            release();
            throw new CaptureError('noAudioTrack');
        }
        // Only the audio is recorded.
        for (const track of displayStream.getVideoTracks()) {
            try { track.stop(); } catch (e) { /* already stopped */ }
        }
    }

    if (mode === 'microphone' || mode === 'both') {
        try {
            micStream = await mediaDevices.getUserMedia({ audio: microphone });
        } catch (error) {
            release();
            throw new CaptureError(error && error.name === 'NotAllowedError' ? 'microphoneDenied' : 'noMicrophone', error);
        }
    }

    let stream;
    if (mode === 'microphone') {
        stream = micStream;
    } else if (mode === 'system') {
        stream = makeStream([systemTrack], displayStream);
    } else {
        try {
            if (!AudioContext) throw new Error('No AudioContext');
            audioContext = new AudioContext();
            const destination = audioContext.createMediaStreamDestination();
            audioContext.createMediaStreamSource(micStream).connect(destination);
            audioContext.createMediaStreamSource(makeStream([systemTrack], displayStream)).connect(destination);
            if (audioContext.state === 'suspended' && typeof audioContext.resume === 'function') {
                audioContext.resume().catch(() => {});
            }
            stream = destination.stream;
        } catch (error) {
            release();
            throw new CaptureError('noMicrophone', error);
        }
    }
    return { mode, stream, displayStream, micStream, audioContext, release };
}

/** A MediaStream of these tracks (a stream of the same class as `like` where MediaStream is not global). */
function makeStream(tracks, like) {
    const Stream = typeof MediaStream !== 'undefined' ? MediaStream : like && like.constructor;
    return new Stream(tracks);
}

/**
 * Call onEnd once when the screen share ends from the browser (its "Stop
 * sharing" button, or the shared window closed): any track of the share
 * fires 'ended'. Returns a function that stops listening. A capture
 * without a share never calls it.
 */
export function onShareEnded(capture, onEnd) {
    const display = capture && capture.displayStream;
    if (!display) return () => {};
    const tracks = display.getTracks();
    let done = false;
    const handler = () => {
        if (done) return;
        done = true;
        stop();
        onEnd();
    };
    const stop = () => { for (const track of tracks) track.removeEventListener('ended', handler); };
    for (const track of tracks) track.addEventListener('ended', handler);
    return stop;
}
