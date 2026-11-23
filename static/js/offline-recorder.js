/**
 * The offline recorder (#650): static/offline-recorder.html, answered by the
 * service worker (static/sw.js) in place of a page that cannot be loaded.
 *
 * The page holds no user data. It records from the microphone, the system
 * audio (a screen share's audio) or both, as the in-app recorder does
 * (#697; utils/audio-capture.js), and saves the
 * recording as a record of the IndexedDB store of failed uploads
 * (modules/db/failed-uploads.js), for the account named in the entry the app
 * left at its last online load (speakr.offlineRecorder,
 * modules/db/offline-recorder-context.js). The app uploads such records once
 * at its next online load (resurfaceFailedUploads in composables/upload.js).
 *
 * While it records, the chunks are also saved as the crash-recovery copy of
 * modules/db/recording-persistence.js, unless that store already holds a
 * copy of another recording, so that a closed tab does not lose the audio:
 * the app offers that copy for recovery at its next start. Once the
 * recording is saved as a record, the copy goes.
 *
 * The page lists the records waiting for their upload (name, duration,
 * size) and lets the person delete one. It asks the server every five
 * seconds, and when the browser reports a connection, whether it is back,
 * and then loads the page that was asked for, unless a recording is under
 * way.
 *
 * Every module is loaded with this release's ?v=, the URLs the worker keeps.
 */

const SELF = new URL(import.meta.url);
const VERSION = SELF.searchParams.get('v') || '';
const Q = VERSION ? `?v=${encodeURIComponent(VERSION)}` : '';
const mod = (path) => import(`/static/js/${path}${Q}`);

const $ = (id) => document.getElementById(id);

async function loadLocale(locale) {
    const get = async (code) => {
        try {
            const response = await fetch(`/static/locales/${code}.json${Q}`);
            return response.ok ? await response.json() : null;
        } catch (e) {
            return null;
        }
    };
    const english = await get('en');
    const table = locale && locale !== 'en' ? await get(locale) : english;
    return { table, english };
}

async function main() {
    const [Model, Pending, FailedUploads, RecordingDB, Capture] = await Promise.all([
        mod('modules/utils/offline-recorder-model.js'),
        mod('modules/utils/pending-uploads.js'),
        mod('modules/db/failed-uploads.js'),
        mod('modules/db/recording-persistence.js'),
        mod('modules/utils/audio-capture.js'),
    ]);

    let rawContext = null;
    try { rawContext = localStorage.getItem(Pending.OFFLINE_CONTEXT_KEY); } catch (e) { rawContext = null; }
    const context = Pending.parseOfflineContext(rawContext);
    const locale = context ? context.locale : Pending.offlineLocale(navigator.language);
    const { table, english } = await loadLocale(locale);
    const t = Model.translator(table, english);
    // Which locale file is in use ('missing': the built-in English texts).
    document.body.dataset.orLocale = table ? locale : (english ? 'en' : 'missing');
    if (!table) console.warn('[OfflineRecorder] The locale file is not in the cache:', locale);

    document.documentElement.lang = locale;
    document.title = t('pageTitle');
    for (const el of document.querySelectorAll('[data-or-text]')) el.textContent = t(el.dataset.orText);

    const availability = Model.recorderAvailability(context, {
        mediaDevices: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
        mediaRecorder: typeof MediaRecorder !== 'undefined',
        indexedDB: typeof indexedDB !== 'undefined',
    });

    const status = $('or-status');
    const say = (text, kind = '') => {
        status.textContent = text || '';
        status.dataset.kind = kind;
    };

    const dateFormat = (() => {
        try {
            return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' });
        } catch (e) {
            return null;
        }
    })();
    const formatDate = (ms) => (dateFormat ? dateFormat.format(new Date(ms)) : new Date(ms).toLocaleString(locale));

    // ---- waiting to upload ------------------------------------------------

    const list = $('or-list');
    async function renderPending() {
        let records = [];
        try { records = await FailedUploads.getFailedUploads(); } catch (e) { records = []; }
        const rows = Model.pendingRows(records, context && context.userId, {
            t, formatDate, belongsTo: Pending.belongsTo,
        });
        list.replaceChildren();
        $('or-pending').hidden = rows.length === 0;
        for (const row of rows) {
            const li = document.createElement('li');
            li.className = 'or-item';
            li.dataset.recordId = String(row.id);
            const text = document.createElement('div');
            text.className = 'or-item-text';
            const name = document.createElement('span');
            name.className = 'or-item-name';
            name.textContent = row.name;
            const meta = document.createElement('span');
            meta.className = 'or-item-meta';
            meta.textContent = `${row.duration} · ${row.size}`;
            text.append(name, meta);
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'or-btn or-btn--quiet';
            remove.dataset.orDelete = '';
            remove.textContent = t('delete');
            remove.addEventListener('click', () => askDelete(li, row.id));
            li.append(text, remove);
            list.append(li);
        }
    }

    function askDelete(li, id) {
        if (li.querySelector('.or-confirm')) return;
        const box = document.createElement('div');
        box.className = 'or-confirm';
        box.setAttribute('role', 'group');
        const question = document.createElement('p');
        question.textContent = t('deleteQuestion');
        const yes = document.createElement('button');
        yes.type = 'button';
        yes.className = 'or-btn or-btn--danger';
        yes.dataset.orDeleteConfirm = '';
        yes.textContent = t('deleteConfirm');
        const no = document.createElement('button');
        no.type = 'button';
        no.className = 'or-btn';
        no.dataset.orDeleteCancel = '';
        no.textContent = t('deleteCancel');
        yes.addEventListener('click', async () => {
            try { await FailedUploads.deleteFailedUpload(id); } catch (e) { /* listed again below */ }
            say(t('deleted'));
            await renderPending();
        });
        no.addEventListener('click', () => box.remove());
        box.append(question, yes, no);
        li.append(box);
        no.focus();
    }

    // ---- recording ----------------------------------------------------------

    const recordButton = $('or-record');
    const stopButton = $('or-stop');
    const timer = $('or-timer');
    const left = $('or-left');
    const limitNote = $('or-limit');
    // The limits of a recording (the entry's limits, see
    // Model.recordingLimits): RECORDING_MAX_HOURS, and the smaller of the
    // in-app recorder's cap and the server's upload limit; the free storage
    // is asked for before and during a recording.
    const limits = Model.recordingLimits(context ? context.limits : {});
    const CHUNK_SECONDS = 2;
    const storage = typeof navigator !== 'undefined' ? navigator.storage : null;
    const estimateFree = async () => {
        try {
            return storage && storage.estimate ? Model.freeBytes(await storage.estimate()) : null;
        } catch (e) {
            return null;
        }
    };
    let bytes = 0;
    let largestChunk = 0;
    let free = null;
    let lastEstimate = 0;
    let warned = false;
    let stopReason = null;
    let persistAsked = false;
    let recorder = null;
    let capture = null;
    let stopWatchingShare = () => {};
    let starting = false;
    let chunks = [];
    let startedAt = 0;
    let tick = null;
    let saving = false;
    let sessionStartTime = null;
    let serverBack = false;

    const elapsed = () => (startedAt ? (Date.now() - startedAt) / 1000 : 0);
    const showTime = () => { timer.textContent = Model.formatDuration(elapsed()); };

    // Every half second: the time, the time left to the limit reached
    // first, the warning at 80 % of it, and the stop at all of it. The
    // storage is asked again every five seconds.
    function checkLimits() {
        showTime();
        if (!recorder || saving) return;
        if (Date.now() - lastEstimate > 5000) {
            lastEstimate = Date.now();
            estimateFree().then((value) => { free = value; });
        }
        const state = Model.limitState({
            elapsed: elapsed(), bytes, maxSeconds: limits.maxSeconds, maxBytes: limits.maxBytes, free, step: CHUNK_SECONDS,
            largestChunk,
        });
        if (state.secondsLeft !== null) {
            left.textContent = t('timeLeft', { time: Model.formatDuration(Math.ceil(state.secondsLeft)) });
            left.hidden = false;
        }
        if (state.warn && !warned) {
            warned = true;
            limitNote.textContent = t('limitWarning', { time: Model.formatDuration(Math.ceil(state.secondsLeft || 0)) });
            limitNote.hidden = false;
        }
        if (state.stop) {
            stopReason = state.reason;
            stop();
        }
    }

    // ---- the source: microphone, system audio or both (#697) ------------------

    // As in the in-app recorder (upload-view.html): the two sources with
    // system audio are offered where the browser can capture a screen
    // share, and disabled with a note elsewhere; the choice is remembered.
    const sourceBox = $('or-source');
    const sourceButtons = [...sourceBox.querySelectorAll('[data-or-mode]')];
    const systemAvailable = Capture.canRecordSystemAudio(navigator.mediaDevices);
    let chosen = 'microphone';
    try { chosen = localStorage.getItem(Model.SOURCE_KEY) || 'microphone'; } catch (e) { chosen = 'microphone'; }
    const currentMode = () => Capture.effectiveMode(chosen, systemAvailable);
    const showSource = () => {
        const mode = currentMode();
        for (const button of sourceButtons) {
            const on = button.dataset.orMode === mode;
            button.setAttribute('aria-checked', on ? 'true' : 'false');
            button.tabIndex = on ? 0 : -1;
            button.classList.toggle('is-active', on);
            button.disabled = button.dataset.orMode !== 'microphone' && !systemAvailable;
        }
        document.body.dataset.orMode = mode;
    };
    const choose = (mode) => {
        if (recorder || starting) return;
        chosen = mode;
        try { localStorage.setItem(Model.SOURCE_KEY, mode); } catch (e) { /* not remembered */ }
        showSource();
    };
    for (const button of sourceButtons) {
        button.addEventListener('click', () => choose(button.dataset.orMode));
        // The arrow keys move the choice within the group, as in a radio group.
        button.addEventListener('keydown', (event) => {
            const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
            if (!step) return;
            event.preventDefault();
            const enabled = sourceButtons.filter((b) => !b.disabled);
            const next = enabled[(enabled.indexOf(button) + step + enabled.length) % enabled.length];
            choose(next.dataset.orMode);
            next.focus();
        });
    }
    $('or-source-hint').hidden = systemAvailable;
    showSource();

    const setRecording = (on) => {
        recordButton.hidden = on;
        stopButton.hidden = !on;
        timer.hidden = !on;
        sourceBox.hidden = on;
        document.body.classList.toggle('or-is-recording', on);
        if (!on) left.hidden = true;
    };

    async function start() {
        if (recorder || starting || saving) return;
        starting = true;
        recordButton.disabled = true;
        try {
            await begin(currentMode());
        } finally {
            starting = false;
            recordButton.disabled = false;
        }
    }

    async function begin(mode) {
        say('');
        limitNote.hidden = true;
        delete document.body.dataset.orStopped;
        // The screen share is the first await after the click (Firefox);
        // the microphone alone is asked for after the storage.
        const shareFirst = Capture.needsDisplay(mode);
        if (!shareFirst) {
            free = await estimateFree();
            if (!Model.canStart(free)) {
                say(t('noStorage'), 'error');
                return;
            }
        }
        try {
            capture = await Capture.acquireCapture(mode, {
                mediaDevices: navigator.mediaDevices,
                AudioContext: window.AudioContext || window.webkitAudioContext || null,
            });
        } catch (e) {
            capture = null;
            say(Model.captureMessage(e, t), 'error');
            return;
        }
        if (shareFirst) {
            free = await estimateFree();
            if (!Model.canStart(free)) {
                capture.release();
                capture = null;
                say(t('noStorage'), 'error');
                return;
            }
        }
        const mimeType = Model.pickMimeType(MediaRecorder.isTypeSupported ? MediaRecorder.isTypeSupported.bind(MediaRecorder) : null);
        try {
            recorder = mimeType ? new MediaRecorder(capture.stream, { mimeType }) : new MediaRecorder(capture.stream);
        } catch (e) {
            capture.release();
            capture = null;
            say(t('noMicrophone'), 'error');
            return;
        }
        chunks = [];
        bytes = 0;
        largestChunk = 0;
        warned = false;
        stopReason = null;
        lastEstimate = Date.now();
        sessionStartTime = null;
        // The crash-recovery copy, unless it holds another recording.
        let keepCopy = false;
        try {
            const existing = await RecordingDB.checkForRecoverableRecording();
            keepCopy = !existing;
            if (keepCopy) {
                const session = await RecordingDB.startRecordingSession({
                    mode, mimeType: (recorder.mimeType || mimeType || 'audio/webm').split(';')[0],
                    ownerId: context.userId,
                });
                sessionStartTime = session ? session.startTime : null;
            }
        } catch (e) {
            keepCopy = false;
        }
        let index = 0;
        recorder.ondataavailable = (event) => {
            if (!event.data || !event.data.size) return;
            chunks.push(event.data);
            bytes += event.data.size;
            largestChunk = Math.max(largestChunk, event.data.size);
            if (keepCopy && sessionStartTime) {
                RecordingDB.saveChunk(event.data, index++)
                    .then(() => (saving ? null : RecordingDB.updateRecordingMetadata({ duration: Math.round(elapsed()) })))
                    .catch(() => {});
            }
        };
        recorder.onstop = () => { saveRecording(mode); };
        // The share ended from the browser ("Stop sharing"): the recording
        // stops and is saved, as with the Stop button.
        stopWatchingShare = Capture.onShareEnded(capture, () => stop());
        // A chunk every two seconds: the size is known to that step, and
        // the crash-recovery copy (rewritten at each chunk) stays cheap.
        try {
            recorder.start(CHUNK_SECONDS * 1000);
        } catch (e) {
            stopWatchingShare();
            capture.release();
            capture = null;
            recorder = null;
            if (sessionStartTime) {
                try { await RecordingDB.clearRecordingSessionIfStartedAt(sessionStartTime); } catch (err) { /* the app removes it */ }
            }
            say(t('noMicrophone'), 'error');
            return;
        }
        startedAt = Date.now();
        showTime();
        tick = setInterval(checkLimits, 500);
        setRecording(true);
        say(t('recording'), 'live');
    }

    function stop() {
        if (!recorder || recorder.state === 'inactive' || saving) return;
        saving = true;
        stopButton.disabled = true;
        say(t('saving'));
        recorder.stop();
    }

    async function saveRecording(mode) {
        saving = true;
        const duration = elapsed();
        clearInterval(tick);
        // Every track (the share's and the microphone's) and the AudioContext.
        stopWatchingShare();
        stopWatchingShare = () => {};
        if (capture) capture.release();
        const type = ((recorder && recorder.mimeType) || 'audio/webm').split(';')[0];
        const now = new Date();
        const file = new File(chunks, Model.recordingFileName(now, type), { type, lastModified: now.getTime() });
        recorder = null;
        capture = null;
        startedAt = 0;
        // Ask once that the browser keep this site's storage; a refusal
        // changes nothing.
        if (!persistAsked && storage && storage.persist) {
            persistAsked = true;
            try {
                if (!(storage.persisted && await storage.persisted())) await storage.persist();
            } catch (e) { /* refused or unavailable */ }
        }
        try {
            await FailedUploads.storeFailedUpload({
                file,
                clientId: Model.offlineClientId(now.getTime(), Math.random().toString(36).slice(2)),
                fromRecorder: true,
                recordingSessionStartTime: sessionStartTime,
                recordingMode: mode,
                duration: Math.round(duration * 10) / 10,
                ownerId: context.userId,
                source: 'offline',
                error: 'Recorded offline',
            });
            if (sessionStartTime) {
                try { await RecordingDB.clearRecordingSessionIfStartedAt(sessionStartTime); } catch (e) { /* the app removes it */ }
            }
            if (stopReason) {
                limitNote.textContent = Model.stoppedMessage(stopReason, limits, t);
                limitNote.hidden = false;
                document.body.dataset.orStopped = stopReason;
            }
            say(t('saved'), 'ok');
        } catch (e) {
            const downloaded = FailedUploads.triggerLocalDownload(file, file.name);
            say(t(downloaded ? 'saveFailed' : 'saveLost'), 'error');
        }
        chunks = [];
        saving = false;
        stopButton.disabled = false;
        setRecording(false);
        await renderPending();
        if (serverBack) openApp();
    }

    window.addEventListener('beforeunload', (event) => {
        if (recorder || saving) {
            event.preventDefault();
            event.returnValue = t('leaveWarning');
        }
    });

    // ---- what the page shows -------------------------------------------------

    if (availability.canRecord) {
        $('or-lead').textContent = t('lead');
        $('or-recorder').hidden = false;
        sourceBox.hidden = false;
        recordButton.addEventListener('click', start);
        stopButton.addEventListener('click', stop);
    } else {
        $('or-lead').textContent = t('leadPlain');
        const note = $('or-unavailable');
        note.textContent = t(availability.reason);
        note.hidden = false;
    }
    if (context) await renderPending();
    document.body.dataset.orReady = availability.canRecord ? 'recorder' : availability.reason;

    // ---- back online -----------------------------------------------------------

    function openApp() {
        say(t('backOpening'), 'ok');
        const target = Model.returnTarget(location.pathname);
        if (target) location.assign(target);
        else location.reload();
    }

    let asking = false;
    async function ask() {
        if (asking) return;
        asking = true;
        try {
            const response = await fetch('/static/manifest.json', { method: 'HEAD', cache: 'no-store' });
            if (!Model.serverAnswered(response.status)) return;
            serverBack = true;
            if (Model.whenServerIsBack({ recording: !!recorder, saving }) === 'open') openApp();
            else say(t('backWhileRecording'), 'ok');
        } catch (e) {
            // Still away.
        } finally {
            asking = false;
        }
    }
    window.addEventListener('online', ask);
    setInterval(() => { if (document.visibilityState === 'visible') ask(); }, 5000);
}

main().catch((error) => {
    // The page keeps its static text ("You are offline") and its link to try again.
    console.error('[OfflineRecorder]', error);
    document.body.dataset.orReady = 'failed';
});
