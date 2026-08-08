import { describe, expect, it, vi } from 'vitest';

import {
    acquireMicrophoneStream,
    buildMicrophoneConstraints,
    isUnavailableAudioInputError
} from './audio.js';

const stream = (name) => ({ name, getTracks: () => [] });
const mediaError = (name) => Object.assign(new Error(name), { name });

describe('microphone acquisition helpers', () => {
    it('builds exact device constraints for a selected input', () => {
        expect(buildMicrophoneConstraints('usb-mic', false)).toEqual({
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            sampleRate: 48000,
            deviceId: { exact: 'usb-mic' }
        });
    });

    it('omits deviceId for the system default and can disable processing', () => {
        expect(buildMicrophoneConstraints('', true)).toEqual({
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: false,
            sampleRate: 48000
        });
    });

    it.each(['NotFoundError', 'OverconstrainedError'])('recognizes %s as unavailable input errors', (name) => {
        expect(isUnavailableAudioInputError(mediaError(name))).toBe(true);
    });

    it('does not classify permission or hardware errors as stale selections', () => {
        expect(isUnavailableAudioInputError(mediaError('NotAllowedError'))).toBe(false);
        expect(isUnavailableAudioInputError(mediaError('NotReadableError'))).toBe(false);
    });

    it('returns an exact selected stream without retrying', async () => {
        const selectedStream = stream('selected');
        const getUserMedia = vi.fn().mockResolvedValue(selectedStream);

        await expect(acquireMicrophoneStream({ getUserMedia }, {
            deviceId: 'usb-mic',
            disableAudioProcessing: false
        })).resolves.toEqual({
            stream: selectedStream,
            requestedDeviceId: 'usb-mic',
            usedFallback: false,
            originalError: null
        });
        expect(getUserMedia).toHaveBeenCalledOnce();
        expect(getUserMedia).toHaveBeenCalledWith({
            audio: expect.objectContaining({ deviceId: { exact: 'usb-mic' } })
        });
    });

    it.each(['NotFoundError', 'OverconstrainedError'])('retries %s once with the default input', async (name) => {
        const originalError = mediaError(name);
        const fallbackStream = stream('fallback');
        const getUserMedia = vi.fn()
            .mockRejectedValueOnce(originalError)
            .mockResolvedValueOnce(fallbackStream);

        await expect(acquireMicrophoneStream({ getUserMedia }, {
            deviceId: 'missing-mic',
            disableAudioProcessing: true
        })).resolves.toEqual({
            stream: fallbackStream,
            requestedDeviceId: 'missing-mic',
            usedFallback: true,
            originalError
        });
        expect(getUserMedia).toHaveBeenCalledTimes(2);
        expect(getUserMedia.mock.calls[0][0].audio.deviceId).toEqual({ exact: 'missing-mic' });
        expect(getUserMedia.mock.calls[1][0].audio).not.toHaveProperty('deviceId');
    });

    it.each(['NotAllowedError', 'SecurityError', 'NotReadableError'])('does not retry %s', async (name) => {
        const error = mediaError(name);
        const getUserMedia = vi.fn().mockRejectedValue(error);

        await expect(acquireMicrophoneStream({ getUserMedia }, {
            deviceId: 'usb-mic'
        })).rejects.toBe(error);
        expect(getUserMedia).toHaveBeenCalledOnce();
    });

    it('does not retry a failure when the system default was requested', async () => {
        const error = mediaError('NotFoundError');
        const getUserMedia = vi.fn().mockRejectedValue(error);

        await expect(acquireMicrophoneStream({ getUserMedia }, {
            deviceId: ''
        })).rejects.toBe(error);
        expect(getUserMedia).toHaveBeenCalledOnce();
    });

    it('propagates a failed fallback without looping', async () => {
        const fallbackError = mediaError('NotReadableError');
        const getUserMedia = vi.fn()
            .mockRejectedValueOnce(mediaError('NotFoundError'))
            .mockRejectedValueOnce(fallbackError);

        await expect(acquireMicrophoneStream({ getUserMedia }, {
            deviceId: 'missing-mic'
        })).rejects.toBe(fallbackError);
        expect(getUserMedia).toHaveBeenCalledTimes(2);
    });
});
