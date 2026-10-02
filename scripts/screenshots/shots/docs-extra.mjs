// Shots for the user-guide pages that the other files do not cover:
// account tabs, admin statistics, the API docs page, events, notes, the
// public share page, tag stacking in the upload dialog, and the recorder
// with one or two sources (desktop) and an external microphone (phone).

import { go, settle, clickVisible, blurEmails } from '../helpers.mjs';
import { accountTab, adminTab } from './settings-admin.mjs';
import { openWithSidebar, ANCHOR } from './detail.mjs';
import { openRecording as openMobileRecording, openUploadSheet, tap } from './mobile.mjs';
import {
    installFakeMicrophone, installFakeInputDevices, openBackdrop, openUploadModal, expandUploadOptions,
    scrollModalTo, selectUploadTags, abandonLiveRecording, seedStorage,
} from './recording-upload.mjs';

/** The existing public link of a presentable recording on the dev instance. */
const SHARE_PATH = '/share/W7r5KRtAViQu9bP8VL4-_A';

/** Switch a recording's right column to one of its tabs (Summary, Notes, Events, Stats). */
async function rightTab(page, label) {
    const ok = await page.evaluate((l) => {
        const btn = [...document.querySelectorAll('button')].find(
            (b) => b.offsetParent && b.innerText.trim().split(/\s+/)[0] === l,
        );
        if (!btn) return false;
        btn.click();
        return true;
    }, label);
    if (!ok) throw new Error(`${label} tab not found`);
    await settle(page, 800);
}

/**
 * Screen sharing that hands back a live audio track (and a small canvas
 * video track, as a real share does), so "Microphone + System" records in
 * a headless browser.
 */
async function installFakeScreenShare(page) {
    await page.addInitScript(() => {
        Object.defineProperty(MediaDevices.prototype, 'getDisplayMedia', {
            configurable: true,
            writable: true,
            value: async () => {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                if (ctx.state === 'suspended') ctx.resume().catch(() => {});
                const dest = ctx.createMediaStreamDestination();
                [260, 640].forEach((hz, i) => {
                    const osc = ctx.createOscillator();
                    osc.type = i ? 'sawtooth' : 'sine';
                    osc.frequency.value = hz;
                    const gain = ctx.createGain();
                    gain.gain.value = 0.12;
                    const lfo = ctx.createOscillator();
                    lfo.frequency.value = 1.1 + i * 0.4;
                    const lfoGain = ctx.createGain();
                    lfoGain.gain.value = 0.1;
                    lfo.connect(lfoGain);
                    lfoGain.connect(gain.gain);
                    osc.connect(gain);
                    gain.connect(dest);
                    osc.start();
                    lfo.start();
                });
                const canvas = document.createElement('canvas');
                canvas.width = 64;
                canvas.height = 36;
                canvas.getContext('2d').fillRect(0, 0, 64, 36);
                const video = canvas.captureStream(1).getVideoTracks()[0];
                return new MediaStream([...dest.stream.getAudioTracks(), video]);
            },
        });
    });
}

/** Start a recording from the open upload modal and let it run for a while. */
async function recordFor(page, buttonText) {
    if (!(await clickVisible(page, `button:has-text(${JSON.stringify(buttonText)})`))) {
        throw new Error(`${buttonText} button not found`);
    }
    await page.waitForTimeout(1000);
    await page.evaluate(() => {
        const btn = [...document.querySelectorAll('.modal-overlay button')].find(
            (b) => b.offsetParent && b.innerText.trim() === 'Start Recording',
        );
        if (btn) btn.click();
    });
    // Let the timer and the visualiser run (the page shows other times too,
    // so the timer text cannot be matched reliably).
    await page.waitForTimeout(20000);
    await settle(page, 400);
}

/** A headless browser refuses the screen wake lock; grant a no-op one. */
async function allowWakeLock(page) {
    await page.addInitScript(() => {
        const sentinel = { released: false, release: async () => {}, addEventListener() {}, removeEventListener() {} };
        try {
            Object.defineProperty(navigator, 'wakeLock', { configurable: true, get: () => ({ request: async () => sentinel }) });
        } catch (_) { /* not configurable on this build */ }
    });
}

export default [
    {
        name: 'admin-statistics',
        description: 'System statistics in the admin dashboard',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await go(page, '/admin');
            await adminTab(page, 'System Statistics');
            await blurEmails(page);
        },
    },
    {
        name: 'api-swagger-ui',
        description: 'Interactive API documentation at /api/v1/docs',
        theme: { dark: false, scheme: 'blue' },
        run: async (page) => {
            await go(page, '/api/v1/docs');
            await page.waitForSelector('.swagger-ui .opblock', { timeout: 30000 });
            await settle(page, 800);
        },
    },
    {
        name: 'event-extraction',
        description: 'Calendar events extracted from a meeting',
        theme: { dark: true, scheme: 'teal' },
        run: async (page) => {
            await go(page, '/');
            await openWithSidebar(page, 'Fortnightly Team Meeting at ABC Manufacturing', ANCHOR.interviews);
            await rightTab(page, 'Events');
        },
    },
    {
        name: 'main-view-notes-tab',
        description: 'The Notes tab of a recording',
        theme: { dark: true, scheme: 'purple' },
        run: async (page) => {
            await go(page, '/');
            await openWithSidebar(page, 'Job Cuts, Internet Regulations, and Apple Subscriptions', ANCHOR.learning);
            await rightTab(page, 'Notes');
        },
    },
    {
        name: 'settings-account-info',
        description: 'Account information',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await go(page, '/account');
            await accountTab(page, 'account');
            await blurEmails(page);
        },
    },
    {
        name: 'settings-shared-transcripts',
        description: 'Your public share links',
        theme: { dark: true, scheme: 'emerald' },
        run: async (page) => {
            await go(page, '/account');
            await accountTab(page, 'shares');
        },
    },
    {
        name: 'settings-speakers-management',
        description: 'Managing saved speakers and their voice profiles',
        theme: { dark: true, scheme: 'rose' },
        run: async (page) => {
            await go(page, '/account');
            await accountTab(page, 'speakers');
        },
    },
    {
        name: 'settings-about',
        description: 'The About tab: version and system configuration',
        theme: { dark: true, scheme: 'amber' },
        run: async (page) => {
            await go(page, '/account');
            await accountTab(page, 'about');
        },
    },
    {
        name: 'share-link',
        description: 'A recording opened from a public share link',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await go(page, SHARE_PATH);
            await settle(page, 800);
        },
    },
    {
        name: 'upload-tag-stacking',
        description: 'Several tags on one upload, applied in order',
        theme: { dark: true, scheme: 'emerald' },
        run: async (page) => {
            await openBackdrop(page);
            await openUploadModal(page);
            await expandUploadOptions(page);
            await selectUploadTags(page, ['Interview', 'Important']);
            await scrollModalTo(page, '.upload-options-body', 24);
        },
    },
    {
        name: 'recording-single-source',
        description: 'Recording from the microphone',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await allowWakeLock(page);
            await installFakeMicrophone(page);
            await openBackdrop(page);
            await openUploadModal(page);
            await recordFor(page, 'Microphone');
            await abandonLiveRecording(page);
        },
    },
    {
        name: 'recording-dual-source',
        description: 'Recording the microphone and system audio together',
        theme: { dark: true, scheme: 'purple' },
        run: async (page) => {
            await allowWakeLock(page);
            await installFakeMicrophone(page);
            await installFakeScreenShare(page);
            await openBackdrop(page);
            await openUploadModal(page);
            await recordFor(page, 'Microphone + System');
            await abandonLiveRecording(page);
        },
    },
    {
        name: 'recording-external-microphone-mobile',
        description: 'Choosing an external microphone on a phone',
        theme: { dark: true, scheme: 'teal' },
        mobile: true,
        run: async (page) => {
            await installFakeInputDevices(page, [
                { deviceId: 'default', groupId: 'grp-phone', label: 'Default — Phone microphone' },
                { deviceId: 'usb-mic', groupId: 'grp-usb', label: 'USB Audio Device' },
            ]);
            await seedStorage(page, { selectedMicDeviceId: 'usb-mic' });
            await go(page, '/');
            await openMobileRecording(page, 'Attempting a Michelin Star Dish Challenge');
            await openUploadSheet(page);
            await page.evaluate(() => {
                const d = [...document.querySelectorAll('details.disclosure-card')].find((x) => x.innerText.includes('Input'));
                if (d) { d.open = true; d.scrollIntoView({ block: 'center' }); }
            });
            await settle(page, 600);
        },
    },
];
