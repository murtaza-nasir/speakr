/**
 * Platform detection + audio capability matrix.
 *
 * Used by the upload modal's system-audio path to (a) show platform-
 * aware hints / warnings BEFORE the user clicks, (b) route capture
 * failures to a contextual help modal, and (c) surface virtual audio
 * devices (BlackHole, VB-Cable, Loopback, PulseAudio monitor sources)
 * as alternative microphone inputs when the user has installed one.
 *
 * The capability matrix encodes what browsers / OS combinations can
 * actually do — getDisplayMedia({audio:true}) is supported as an API
 * everywhere modern, but the underlying OS gates whether system audio
 * actually flows through. Only Chrome / Edge on Windows + Chrome OS
 * deliver true OS-wide system audio. macOS and Linux Chrome can only
 * capture audio when sharing a TAB (chrome.tabCapture-style behaviour
 * exposed via getDisplayMedia). Firefox and Safari can't capture any
 * audio through getDisplayMedia.
 */

const KNOWN_VIRTUAL_DEVICE_HINTS = [
    // macOS
    { match: /blackhole/i,          os: 'macOS',   name: 'BlackHole' },
    { match: /loopback/i,           os: 'macOS',   name: 'Loopback' },
    { match: /soundflower/i,        os: 'macOS',   name: 'Soundflower' },
    { match: /background music/i,   os: 'macOS',   name: 'Background Music' },
    { match: /existential audio/i,  os: 'macOS',   name: 'BlackHole' },
    // Windows
    { match: /vb[- ]?(audio|cable)/i, os: 'Windows', name: 'VB-Audio Cable' },
    { match: /voicemeeter/i,          os: 'Windows', name: 'Voicemeeter' },
    { match: /stereo mix/i,           os: 'Windows', name: 'Stereo Mix' },
    { match: /what u hear/i,          os: 'Windows', name: 'What U Hear' },
    // Linux
    { match: /monitor of /i,        os: 'Linux',   name: 'PulseAudio Monitor' },
    { match: /pipewire.*monitor/i,  os: 'Linux',   name: 'PipeWire Monitor' },
    { match: /pulse.*monitor/i,     os: 'Linux',   name: 'PulseAudio Monitor' },
];

/**
 * Inspect the current user agent + platform string and return a small
 * normalized descriptor used everywhere else in the audio capture flow.
 * Cheap, sync, safe to call repeatedly.
 */
export function detectPlatform() {
    if (typeof navigator === 'undefined') {
        return { os: 'unknown', browser: 'unknown' };
    }
    const ua = navigator.userAgent || '';
    const platform = (navigator.userAgentData?.platform || navigator.platform || '').toLowerCase();

    let os = 'unknown';
    if (/iphone|ipad|ipod/i.test(ua) || (/mac/.test(platform) && navigator.maxTouchPoints > 1)) {
        os = 'iOS';
    } else if (/mac/i.test(platform) || /mac os x/i.test(ua)) {
        os = 'macOS';
    } else if (/win/i.test(platform) || /windows/i.test(ua)) {
        os = 'Windows';
    } else if (/cros|chrome os/i.test(ua) || /cros/i.test(platform)) {
        os = 'ChromeOS';
    } else if (/android/i.test(ua)) {
        os = 'Android';
    } else if (/linux/i.test(platform) || /linux/i.test(ua)) {
        os = 'Linux';
    }

    let browser = 'unknown';
    // Order matters: Edge UA contains "Chrome", so check Edge first.
    if (/edg(e|ios|a)?\//i.test(ua))      browser = 'Edge';
    else if (/firefox|fxios/i.test(ua))   browser = 'Firefox';
    else if (/chrome|crios/i.test(ua))    browser = 'Chrome';
    else if (/safari/i.test(ua))          browser = 'Safari';
    else if (/opera|opr\//i.test(ua))     browser = 'Opera';

    return { os, browser };
}

/**
 * Returns a structured capability report describing what audio capture
 * is realistically available on the current browser + OS — for use by
 * the UI to show pre-emptive hints and to know whether to route a
 * failed capture to the help modal.
 *
 * Flags:
 *  - supportsGetUserMedia: classic microphone capture
 *  - supportsGetDisplayMedia: the API exists at all
 *  - supportsTabAudio: getDisplayMedia({audio:true}) is expected to
 *    deliver audio when the user picks "Share tab"
 *  - supportsWindowAudio: ... when picking a window
 *  - supportsSystemAudio: ... full OS-wide audio (Windows / ChromeOS
 *    only via the "Share system audio" checkbox)
 *  - virtualDeviceHint: a string the help modal can show — name of the
 *    virtual audio routing tool that's idiomatic on this OS
 */
export function getAudioCapabilities() {
    const p = detectPlatform();
    const hasGUM = typeof navigator !== 'undefined'
        && navigator.mediaDevices
        && typeof navigator.mediaDevices.getUserMedia === 'function';
    const hasGDM = typeof navigator !== 'undefined'
        && navigator.mediaDevices
        && typeof navigator.mediaDevices.getDisplayMedia === 'function';

    let supportsTabAudio = false;
    let supportsWindowAudio = false;
    let supportsSystemAudio = false;
    let virtualDeviceHint = null;

    if (hasGDM) {
        // Firefox: getDisplayMedia({audio:true}) returns a stream but no
        // audio track on any platform. Effectively no audio capture.
        if (p.browser === 'Firefox') {
            // intentionally all false
        }
        // Safari: same story
        else if (p.browser === 'Safari') {
            // intentionally all false
        }
        // Chrome / Edge: matrix below
        else if (p.browser === 'Chrome' || p.browser === 'Edge') {
            supportsTabAudio = true;
            if (p.os === 'Windows' || p.os === 'ChromeOS') {
                supportsWindowAudio = true;
                supportsSystemAudio = true;
            }
            // macOS / Linux: only tab audio works in practice.
        }
    }

    if (p.os === 'macOS')   virtualDeviceHint = 'BlackHole';
    if (p.os === 'Windows') virtualDeviceHint = 'VB-Audio Cable';
    if (p.os === 'Linux')   virtualDeviceHint = 'PulseAudio monitor source';

    return {
        platform: p,
        supportsGetUserMedia: hasGUM,
        supportsGetDisplayMedia: hasGDM,
        supportsTabAudio,
        supportsWindowAudio,
        supportsSystemAudio,
        virtualDeviceHint,
    };
}

/**
 * Scan enumerateDevices() for installed virtual audio routing devices.
 * Returns an array of { deviceId, label, virtualToolName, os } entries
 * that the upload UI can surface as alternative microphone inputs (the
 * user routes system audio through them and then captures via the
 * regular microphone path).
 *
 * Returns [] when permission hasn't been granted yet (labels are blank
 * pre-permission, so we can't pattern-match). The UI should retry this
 * after the user grants mic permission once.
 */
export async function enumerateVirtualAudioDevices() {
    if (typeof navigator === 'undefined'
        || !navigator.mediaDevices
        || typeof navigator.mediaDevices.enumerateDevices !== 'function') {
        return [];
    }
    let devices;
    try {
        devices = await navigator.mediaDevices.enumerateDevices();
    } catch (_) {
        return [];
    }
    const inputs = devices.filter(d => d.kind === 'audioinput' && d.label);
    const matches = [];
    for (const d of inputs) {
        for (const hint of KNOWN_VIRTUAL_DEVICE_HINTS) {
            if (hint.match.test(d.label)) {
                matches.push({
                    deviceId: d.deviceId,
                    label: d.label,
                    virtualToolName: hint.name,
                    os: hint.os,
                });
                break;
            }
        }
    }
    return matches;
}
