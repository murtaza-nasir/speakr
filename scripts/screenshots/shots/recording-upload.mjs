// Recording and upload shots.
//
// Covers the two ways audio gets into Speakr — recording it in the browser
// and uploading files — plus the modals that hang off those flows: the
// per-upload options group, multi-file queueing, the duplicate indicator,
// reprocessing, the system-audio setup guide, and the input-device picker.
//
// Everything here is non-destructive: files are queued but never uploaded,
// the reprocess modal is never confirmed, and the live recording used by
// `recording-live-notes` has its server-side chunk session aborted (the
// same DELETE the in-app Discard button issues) before the shot ends, so
// no recording and no orphaned session is left behind.

import { copyFileSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import { go, settle, clickVisible, anchorSidebar, openRecordingByTitle } from '../helpers.mjs';

/** A sample file that exists in the repo; queued but never uploaded. */
const SAMPLE_FILE = '/home/murtaza/whispertranscribe/temp/german_sample.mp3';

/** Sidebar rows framed behind the modals — tagged, foldered, real-looking. */
const BACKDROP_RECORDING = 'Attempting a Michelin Star Dish';
const SIDEBAR_ANCHOR = 'Railroad Tax on Stock Compensation Remuneration';

/** Recording that genuinely has other copies of the same audio on file. */
const DUPLICATE_RECORDING_ID = 2302;   // "Manager Introductions at Sihai Meeting"

/** Markdown typed into the notes pane while the recording runs. */
const LIVE_NOTES = `## Weekly product sync — 12 Aug

- **Ana** — onboarding rewrite is in review, ships Thursday
- **Raj** — billing migration is blocked on the vendor sandbox

### Decisions
1. Hold the pricing page redesign until after launch week
2. Move the retro to Friday, 10:00

*Follow up:* ask Priya for the churn numbers before Monday.`;

/**
 * Replace getUserMedia with an oscillator-backed MediaStream so a real
 * recording can run headless: MediaRecorder, the Web Audio analyser and
 * the visualiser all see a genuine live audio track. A slow LFO on the
 * gain keeps the visualiser bars moving instead of drawing a flat line.
 */
async function installFakeMicrophone(page) {
    await page.addInitScript(() => {
        const makeStream = () => {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            if (ctx.state === 'suspended') ctx.resume().catch(() => {});
            const dest = ctx.createMediaStreamDestination();
            // Two voices an octave apart give the FFT visualiser something
            // with structure to draw rather than a single spike.
            [180, 420].forEach((hz, i) => {
                const osc = ctx.createOscillator();
                osc.type = i ? 'triangle' : 'sine';
                osc.frequency.value = hz;
                const gain = ctx.createGain();
                gain.gain.value = 0.18;
                const lfo = ctx.createOscillator();
                lfo.frequency.value = 0.7 + i * 0.35;
                const lfoGain = ctx.createGain();
                lfoGain.gain.value = 0.12;
                lfo.connect(lfoGain);
                lfoGain.connect(gain.gain);
                osc.connect(gain);
                gain.connect(dest);
                osc.start();
                lfo.start();
            });
            window.__shotAudioCtx = ctx;
            return dest.stream;
        };
        Object.defineProperty(MediaDevices.prototype, 'getUserMedia', {
            configurable: true,
            writable: true,
            value: async () => makeStream(),
        });
    });
}

/**
 * Present a fixed pair of audio inputs to the page. Device labels are
 * empty until a microphone permission is granted, so without this the
 * input-device picker can only render its "grant permission" prompt.
 */
async function installFakeInputDevices(page, devices) {
    await page.addInitScript((list) => {
        Object.defineProperty(MediaDevices.prototype, 'enumerateDevices', {
            configurable: true,
            writable: true,
            value: async () => list.map((d) => ({
                ...d,
                kind: 'audioinput',
                toJSON() { return { ...d, kind: 'audioinput' }; },
            })),
        });
    }, devices);
}

/**
 * Make platform detection report a specific OS. The system-audio guide
 * and its capability banner are entirely platform-driven, so this is how
 * the Windows / macOS pages get captured as the user of that OS sees
 * them rather than as a Linux capture host sees them.
 */
async function spoofPlatform(page, platform) {
    await page.addInitScript((p) => {
        Object.defineProperty(navigator, 'platform', { configurable: true, get: () => p });
        try {
            Object.defineProperty(navigator, 'userAgentData', { configurable: true, get: () => undefined });
        } catch (_) { /* not present on every build */ }
    }, platform);
}

/** Seed a localStorage value before the app boots. */
async function seedStorage(page, entries) {
    await page.addInitScript((kv) => {
        try {
            for (const [k, v] of Object.entries(kv)) localStorage.setItem(k, v);
        } catch (_) { /* private mode */ }
    }, entries);
}

/**
 * Load the main view with a presentable recording open and the sidebar
 * parked on a tagged, foldered stretch of the library — so whatever modal
 * sits on top of it has a real backdrop rather than the untagged test
 * recordings that happen to be newest.
 */
async function openBackdrop(page) {
    await go(page, '/');
    await openRecordingByTitle(page, BACKDROP_RECORDING);
    await parkSidebar(page, SIDEBAR_ANCHOR);
}

/**
 * anchorSidebar() gets the right region of the list into view but lands a
 * row or two off, which leaves a sliced row along the top edge. Follow it
 * with an exact row-top alignment so the list starts on a whole row.
 */
async function parkSidebar(page, title) {
    await anchorSidebar(page, title);
    await page.evaluate((wanted) => {
        const h = [...document.querySelectorAll('h4')].find(
            (el) => el.textContent.includes(wanted) && el.closest('.overflow-y-auto'),
        );
        if (!h) return;
        const list = h.closest('.overflow-y-auto');
        const row = h.closest('.recording-row') || h.parentElement;
        list.scrollTop += row.getBoundingClientRect().top - list.getBoundingClientRect().top;
    }, title);
    await page.waitForTimeout(400);
}

/** Open the New Recording / Upload Audio modal from the header. */
async function openUploadModal(page) {
    if (!(await clickVisible(page, 'button:has-text("New Recording")'))) {
        throw new Error('New Recording button not found');
    }
    await page.waitForSelector('.modal-panel:has-text("Upload Audio")', { state: 'visible' });
    await settle(page, 600);
}

/** Queue files into the upload modal without starting the upload. */
async function queueFiles(page, files) {
    await page.setInputFiles('input[type=file]', files);
    // Wait for the async duration probe so rows show size AND duration.
    await page.waitForFunction(
        (n) => (document.body.innerText.match(/\(\d[\d.]* MB, /g) || []).length >= n,
        files.length,
        { timeout: 25000 },
    ).catch(() => {});
    await settle(page, 600);
}

/** Expand the collapsed "Options" progressive-disclosure group. */
async function expandUploadOptions(page) {
    await page.evaluate(() => {
        document.querySelectorAll('details.upload-options-group').forEach((d) => { d.open = true; });
    });
    await page.waitForTimeout(400);
}

/** Scroll an element inside the modal body into a comfortable position. */
async function scrollModalTo(page, selector, pad = 24) {
    await page.evaluate(([sel, p]) => {
        const target = document.querySelector(sel);
        const body = document.querySelector('.modal-body');
        if (!target || !body) return;
        const offset = target.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop;
        body.scrollTop = Math.min(Math.max(offset - p, 0), body.scrollHeight - body.clientHeight);
    }, [selector, pad]);
    await page.waitForTimeout(400);
}

/**
 * Pick tags in the upload modal's tag grid by name. The grid holds one
 * button per unselected tag; clicking moves it into the selected strip.
 */
async function selectUploadTags(page, names) {
    for (const name of names) {
        const ok = await page.evaluate((n) => {
            const grid = [...document.querySelectorAll('.upload-options-body .grid.grid-cols-2')]
                .find((g) => g.offsetParent);
            if (!grid) return false;
            const btn = [...grid.querySelectorAll('button')].find(
                (b) => b.innerText.trim() === n,
            );
            if (!btn) return false;
            btn.click();
            return true;
        }, name);
        if (!ok) throw new Error(`Tag "${name}" not offered in the upload tag grid`);
        await page.waitForTimeout(300);
    }
    await settle(page, 400);
}

/**
 * Choose a folder in the upload modal's folder <select> by visible name.
 * The page carries offscreen copies of the modal markup, so the on-screen
 * select is picked explicitly rather than by document order.
 */
async function selectUploadFolder(page, name) {
    const ok = await page.evaluate((n) => {
        const sel = [...document.querySelectorAll('.upload-options-body select')]
            .find((s) => s.offsetParent);
        if (!sel) return false;
        const opt = [...sel.options].find((o) => o.textContent.trim() === n);
        if (!opt) return false;
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
    }, name);
    if (!ok) throw new Error(`Folder "${name}" not found in the upload folder picker`);
    await settle(page, 400);
}

/**
 * Open the "Capturing system audio" guide and switch to one OS tab.
 * The inline link only renders on platforms without native system-audio
 * capture, so on Windows the modal is opened through the app's own
 * state flag instead.
 */
async function openSystemAudioHelp(page, osTab) {
    const viaLink = await clickVisible(page, 'button:has-text("how to enable full system audio")');
    if (!viaLink) {
        await page.evaluate(() => {
            const app = document.getElementById('app');
            const state = app && app.__vue_app__ && app.__vue_app__._instance
                ? app.__vue_app__._instance.setupState : null;
            if (!state) throw new Error('Vue setup state unavailable');
            state.showSystemAudioHelp = true;
        });
    }
    await page.waitForSelector('.modal-panel:has-text("Capturing system audio")', { state: 'visible' });
    await settle(page, 500);
    await page.evaluate((os) => {
        const panel = [...document.querySelectorAll('.modal-panel')].find(
            (p) => p.innerText.includes('Capturing system audio'),
        );
        const tab = [...panel.querySelectorAll('.tab')].find((b) => b.innerText.trim() === os);
        if (!tab) throw new Error(`OS tab ${os} not found`);
        tab.click();
    }, osTab);
    await settle(page, 500);
    // Guides are long; start each one at the top of its body.
    await page.evaluate(() => {
        const body = document.querySelector('.modal-panel .modal-body');
        if (body) body.scrollTop = 0;
    });
    await page.waitForTimeout(200);
}

/**
 * Abort the server-side chunk session opened by the live recording.
 *
 * The harness screenshots after run() returns, so the in-app Stop →
 * Discard buttons cannot be clicked before the frame is taken without
 * losing the state being documented. This issues exactly the request
 * discardRecording() issues (DELETE /upload/session/<id>), which marks
 * the session aborted and deletes its chunk directory — so the shot
 * leaves precisely what pressing Discard leaves, and nothing more. No
 * Recording row is ever created; that only happens on upload, which
 * this shot never performs.
 */
async function abandonLiveRecording(page) {
    await page.evaluate(async () => {
        let id = null;
        try {
            const raw = localStorage.getItem('speakr.serverRecordingSession');
            if (raw) id = JSON.parse(raw).session_id;
        } catch (_) { /* nothing stored */ }
        if (!id) return;
        const meta = document.querySelector('meta[name="csrf-token"]');
        await fetch(`/upload/session/${encodeURIComponent(id)}`, {
            method: 'DELETE',
            credentials: 'same-origin',
            headers: { 'X-CSRFToken': meta ? meta.getAttribute('content') : '' },
        }).catch(() => {});
        localStorage.removeItem('speakr.serverRecordingSession');
    });
}

/** Copy the sample audio to plausible per-file names for the queue shot. */
function makeMultiFileSet() {
    const dir = path.join(tmpdir(), 'speakr-shots');
    mkdirSync(dir, { recursive: true });
    return [
        'team-standup-aug-12.mp3',
        'client-call-notes.mp3',
        'product-review.mp3',
    ].map((name) => {
        const dest = path.join(dir, name);
        copyFileSync(SAMPLE_FILE, dest);
        return dest;
    });
}

export default [
    {
        name: 'recording-live-notes',
        description: 'Recording live, with markdown notes alongside',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await installFakeMicrophone(page);
            await openBackdrop(page);
            await openUploadModal(page);

            await clickVisible(page, 'button:has-text("Microphone")');
            // Instances can gate recording behind a disclaimer modal
            // ("Recording Notice"); accept it when it shows up. Matched by
            // exact on-screen text because the page also carries offscreen
            // buttons with the same label.
            await page.waitForTimeout(1000);
            await page.evaluate(() => {
                const btn = [...document.querySelectorAll('.modal-overlay button')].find(
                    (b) => b.offsetParent && b.innerText.trim() === 'Start Recording',
                );
                if (btn) btn.click();
            });

            await page.waitForSelector('.recording-notes-editor .CodeMirror', { timeout: 30000 });
            await settle(page, 800);

            // The notes pane is an EasyMDE editor mounted over the textarea,
            // so the text goes in through CodeMirror; its change handler is
            // what syncs the value back into the app's recordingNotes state.
            const typed = await page.evaluate((md) => {
                const host = document.querySelector('.recording-notes-editor .CodeMirror');
                if (!host || !host.CodeMirror) return false;
                host.CodeMirror.setValue(md);
                return true;
            }, LIVE_NOTES);
            if (!typed) throw new Error('Recording notes editor did not mount');
            await page.waitForTimeout(500);

            // Let the timer reach a plausible reading and the visualiser fill.
            await page.waitForFunction(
                () => /0:(2[0-9]|[3-9][0-9])/.test(document.body.innerText),
                { timeout: 60000 },
            ).catch(() => {});
            await settle(page, 400);
            await abandonLiveRecording(page);
        },
    },
    {
        name: 'upload-options',
        description: 'Upload options: tags, folder, and language',
        theme: { dark: true, scheme: 'emerald' },
        run: async (page) => {
            await openBackdrop(page);
            await openUploadModal(page);
            await queueFiles(page, [SAMPLE_FILE]);
            await expandUploadOptions(page);
            await selectUploadFolder(page, 'Learning');
            await selectUploadTags(page, ['Interview', 'Important']);
            await scrollModalTo(page, '.upload-options-group', 8);
        },
    },
    {
        name: 'upload-multi-file',
        description: 'Uploading several files at once',
        theme: { dark: true, scheme: 'teal' },
        run: async (page) => {
            await openBackdrop(page);
            await openUploadModal(page);
            await queueFiles(page, makeMultiFileSet());
        },
    },
    {
        name: 'upload-duplicate-detection',
        description: 'Duplicate files are detected before upload',
        theme: { dark: true, scheme: 'purple' },
        // Duplicate detection is a server-side SHA-256 match, so the UI for
        // it is the copies indicator the match produces: an amber "N copies"
        // chip on every recording sharing that hash, opening the list of
        // copies. There is no client-side pre-upload check to capture, and
        // the post-upload toast would need a throwaway upload to exist.
        run: async (page) => {
            await go(page, `/recordings/${DUPLICATE_RECORDING_ID}`, 1200);
            await parkSidebar(page, SIDEBAR_ANCHOR);
            await clickVisible(page, 'button.chip-meta--warn');
            await page.waitForSelector('.modal-panel:has-text("copies")', { state: 'visible' });
            await settle(page, 500);
        },
    },
    {
        name: 'recording-reprocess',
        description: 'Reprocessing a recording end to end',
        theme: { dark: true, scheme: 'rose' },
        run: async (page) => {
            await openBackdrop(page);
            if (!(await clickVisible(page, 'button:has(i.fa-redo-alt)'))) {
                throw new Error('Reprocess transcription button not found');
            }
            await page.waitForSelector('.modal-panel:has-text("Advanced ASR Options")', { state: 'visible' });
            await settle(page, 600);
        },
    },
    {
        name: 'system-audio-help-windows',
        description: 'Windows: the Share system audio checkbox',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await spoofPlatform(page, 'Win32');
            await openBackdrop(page);
            await openUploadModal(page);
            await openSystemAudioHelp(page, 'Windows');
        },
    },
    {
        name: 'system-audio-help-macos',
        description: 'macOS: routing system audio through a virtual device',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await spoofPlatform(page, 'MacIntel');
            await openBackdrop(page);
            await openUploadModal(page);
            await openSystemAudioHelp(page, 'macOS');
        },
    },
    {
        name: 'system-audio-help-linux',
        description: 'Linux: exposing a virtual source Chrome will list',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await openBackdrop(page);
            await openUploadModal(page);
            await openSystemAudioHelp(page, 'Linux');
        },
    },
    {
        name: 'recording-input-device-picker',
        description: 'Mixing a microphone with a second input device',
        theme: { dark: true, scheme: 'amber' },
        run: async (page) => {
            await spoofPlatform(page, 'MacIntel');
            await installFakeInputDevices(page, [
                { deviceId: 'default', groupId: 'grp-builtin', label: 'Default — MacBook Pro Microphone' },
                { deviceId: 'mic-builtin', groupId: 'grp-builtin', label: 'MacBook Pro Microphone' },
                { deviceId: 'blackhole-2ch', groupId: 'grp-virtual', label: 'BlackHole 2ch' },
            ]);
            await seedStorage(page, {
                selectedMicDeviceId: 'mic-builtin',
                selectedSecondaryDeviceId: 'blackhole-2ch',
            });
            await openBackdrop(page);
            await openUploadModal(page);
            await page.evaluate(() => {
                const d = [...document.querySelectorAll('.modal-body details.disclosure-card')]
                    .find((x) => x.innerText.includes('Input'));
                if (d) d.open = true;
            });
            await settle(page, 600);
            await scrollModalTo(page, '.modal-body details.disclosure-card', 40);
        },
    },
];
