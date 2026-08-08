import { describe, expect, it } from 'vitest';

import {
    findExternalAudioInputCandidates,
    getAudioInputCandidateSignature,
    isVirtualAudioDeviceLabel,
    normalizeAudioInputDevices,
    shouldAutoOfferExternalAudioInput
} from './platform.js';

const input = (deviceId, label, groupId = '') => ({
    kind: 'audioinput',
    deviceId,
    label,
    groupId
});

describe('audio input device helpers', () => {
    it('normalizes audio inputs and preserves grouping metadata', () => {
        expect(normalizeAudioInputDevices([
            input('mic-1', 'Built-in microphone', 'phone'),
            { kind: 'videoinput', deviceId: 'camera-1', label: 'Camera' }
        ])).toEqual([{
            kind: 'audioinput',
            deviceId: 'mic-1',
            label: 'Built-in microphone',
            groupId: 'phone',
            isVirtual: false
        }]);
    });

    it('recognizes the existing virtual and monitor device labels', () => {
        expect(isVirtualAudioDeviceLabel('BlackHole 2ch')).toBe(true);
        expect(isVirtualAudioDeviceLabel('Monitor Source')).toBe(true);
        expect(isVirtualAudioDeviceLabel('USB Audio Device')).toBe(false);
    });

    it('does not offer another device for a single built-in microphone', () => {
        const devices = [input('phone-mic', 'Built-in microphone', 'phone')];
        expect(findExternalAudioInputCandidates(devices, 'phone-mic')).toEqual([]);
    });

    it('ignores blank labels and browser pseudo-devices', () => {
        const devices = [
            input('phone-mic', 'Built-in microphone', 'phone'),
            input('hidden', '', 'usb'),
            input('default', 'Default', 'usb'),
            input('communications', 'Communications', 'usb')
        ];
        expect(findExternalAudioInputCandidates(devices, 'phone-mic')).toEqual([]);
    });

    it.each([
        'USB Audio Device',
        'Wireless GO II RX',
        'Bluetooth Headset',
        'Logitech Webcam Microphone',
        'External Audio Interface'
    ])('offers a likely external input labelled %s', (label) => {
        const devices = [
            input('phone-mic', 'Built-in microphone', 'phone'),
            input('external-mic', label, 'external')
        ];
        expect(findExternalAudioInputCandidates(devices, 'phone-mic'))
            .toMatchObject([{ deviceId: 'external-mic', label }]);
    });

    it('does not offer a distinct-group device without an external label hint', () => {
        // The internal/generic exclusion regexes are English-only, so a bare
        // "different groupId" fallback would promote untranslated internal
        // routes (earpiece, speakerphone) to candidates on non-English
        // devices. Only explicit external hints may trigger the prompt.
        const devices = [
            input('phone-mic', 'Primary microphone', 'phone'),
            input('interface', 'Scarlett 2i2', 'interface')
        ];
        expect(findExternalAudioInputCandidates(devices, 'phone-mic')).toEqual([]);
    });

    it('does not infer an external input from group data without an active group', () => {
        expect(findExternalAudioInputCandidates([
            input('interface', 'Scarlett 2i2', 'interface')
        ], '')).toEqual([]);
    });

    it('excludes internal and virtual alternatives', () => {
        const devices = [
            input('phone-mic', 'Primary microphone', 'phone'),
            input('rear-mic', 'Rear mic', 'rear'),
            input('earpiece', 'Headset earpiece', 'headset'),
            input('speakerphone', 'Speakerphone', 'speaker'),
            input('blackhole', 'BlackHole 2ch', 'virtual')
        ];
        expect(findExternalAudioInputCandidates(devices, 'phone-mic')).toEqual([]);
    });

    it('excludes the currently active input', () => {
        const devices = [input('usb-mic', 'USB Audio Device', 'usb')];
        expect(findExternalAudioInputCandidates(devices, 'usb-mic')).toEqual([]);
    });

    it.each(['Android', 'iOS'])('allows the automatic external-input offer on %s', (os) => {
        expect(shouldAutoOfferExternalAudioInput({ os })).toBe(true);
    });

    it.each(['Windows', 'macOS', 'Linux', 'ChromeOS', 'unknown'])(
        'leaves microphone selection to the browser on desktop platform %s',
        (os) => {
            expect(shouldAutoOfferExternalAudioInput({ os })).toBe(false);
        }
    );

    it('produces a stable candidate signature regardless of enumeration order', () => {
        const first = [input('b', 'USB B'), input('a', 'USB A')];
        const second = [input('a', 'USB A'), input('b', 'USB B')];
        expect(getAudioInputCandidateSignature(first)).toBe('a|b');
        expect(getAudioInputCandidateSignature(second)).toBe('a|b');
        expect(getAudioInputCandidateSignature([input('a', 'USB A')])).toBe('a');
    });
});
