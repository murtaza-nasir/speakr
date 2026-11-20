/**
 * The upload composable's handling of a recording whose upload failed
 * (#650): it is saved once, uploaded once, and its other copies (the
 * crash-recovery session, the server session) go with that upload, so that
 * nothing is offered or uploaded a second time at the next start.
 *
 * The composable runs with a minimal Vue (ref/computed), a fake
 * XMLHttpRequest that plays the server, and in-memory stand-ins for the
 * IndexedDB modules.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const store = new Map();
let nextId = 1;
vi.mock('../db/failed-uploads.js', () => ({
    storeFailedUpload: vi.fn(async (data) => {
        const id = nextId++;
        store.set(id, {
            id, clientId: data.clientId, fileName: data.file?.name || data.fileName, fileSize: data.file?.size || 0,
            notes: data.notes, tags: data.tags, asrOptions: data.asrOptions, retryCount: 0,
            fileData: new ArrayBuffer(8), mimeType: data.file?.type || 'audio/webm', timestamp: Date.now(),
            fromRecorder: !!data.fromRecorder, recordingSessionStartTime: data.recordingSessionStartTime || null,
            serverSessionId: data.serverSessionId || null, ownerId: data.ownerId ? String(data.ownerId) : null,
            folderId: data.folderId || null, duration: data.duration ?? null,
        });
        return id;
    }),
    getFailedUploads: vi.fn(async () => [...store.values()].map((r) => ({ ...r }))),
    deleteFailedUpload: vi.fn(async (id) => { store.delete(id); }),
    updateRetryCount: vi.fn(async (id, retryCount) => { if (store.has(id)) store.get(id).retryCount = retryCount; }),
    triggerLocalDownload: vi.fn(() => true),
}));
// The crash-recovery copy and the remembered server session, as the tests set them.
const local = { session: null, remembered: null };
vi.mock('../db/recording-persistence.js', () => ({
    clearRecordingSessionIfStartedAt: vi.fn(async (startTime) => {
        if (local.session && local.session.startTime === startTime) { local.session = null; return true; }
        return false;
    }),
    checkForRecoverableRecording: vi.fn(async () => local.session),
}));
vi.mock('../db/server-recording-sessions.js', () => ({
    discardSession: vi.fn(async () => ({ ok: true })),
    getRememberedSession: vi.fn(() => local.remembered),
    forgetActiveSession: vi.fn(() => { local.remembered = null; }),
}));
vi.mock('../db/incognito-storage.js', () => ({}));
vi.mock('../csrf.js', () => ({
    getUploadCsrfToken: vi.fn(async () => 'token'),
    isCsrfRejection: () => false,
}));
vi.mock('../db/sliced-file-upload.js', () => ({
    shouldSliceUpload: () => false,
    uploadFileInSlices: vi.fn(),
}));
const confirmDialog = vi.fn(async () => false);
vi.mock('../utils/dialogs.js', () => ({ confirmDialog: (...args) => confirmDialog(...args) }));

const FailedUploads = await import('../db/failed-uploads.js');
const RecordingDB = await import('../db/recording-persistence.js');
const ServerSessions = await import('../db/server-recording-sessions.js');

// ---- a server behind a fake XMLHttpRequest ------------------------------

const server = {
    mode: 'ok',          // 'ok' | 'network' | 'timeout' | 'hang'
    stored: [],          // client_upload_id of each recording the server keeps
    sends: [],           // every request: { clientId, fromRecorder }
    nextRecordingId: 100,
};

class FakeXHR {
    constructor() { this.upload = {}; this.headers = {}; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    getResponseHeader(name) { return name.toLowerCase() === 'content-type' ? 'application/json' : null; }
    send(form) {
        const clientId = form.get('client_upload_id');
        server.sends.push({ clientId, fromRecorder: form.get('from_recorder') });
        const mode = server.mode;
        queueMicrotask(() => {
            if (mode === 'hang') return;
            if (mode === 'network') { this.onerror?.(); return; }
            // The request reached the server: it keeps the recording once per
            // client_upload_id and answers a repeat with the same one (#626).
            let recording = server.stored.find((r) => r.clientId === clientId);
            if (!recording) {
                recording = { clientId, id: server.nextRecordingId++ };
                server.stored.push(recording);
            }
            if (mode === 'timeout') { this.ontimeout?.(); return; }
            this.upload.onload?.();
            this.status = 202;
            this.responseText = JSON.stringify({ id: recording.id, title: 'T' });
            this.onload?.();
        });
    }
    abort() { this.onabort?.(); }
}

// ---- a minimal Vue -------------------------------------------------------

const ref = (value) => ({ value });
globalThis.Vue = {
    ref,
    computed: (fn) => ({ get value() { return fn(); } }),
    nextTick: async () => {},
    markRaw: (x) => x,
};

const { useUpload } = await import('./upload.js');

const flush = async () => {
    for (let i = 0; i < 30; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

function makeComposable({ userId = '1' } = {}) {
    const state = {
        uploadQueue: ref([]), currentlyProcessingFile: ref(null), processingProgress: ref(0), processingMessage: ref(''),
        isProcessingActive: ref(false), pollInterval: ref(null), progressPopupMinimized: ref(false),
        maxFileSizeMB: ref(250), chunkingEnabled: ref(false), chunkingMode: ref('size'), chunkingLimit: ref(20),
        maxConcurrentUploads: ref(3), recordings: ref([]), selectedRecording: ref(null), totalRecordings: ref(0),
        globalError: ref(null), selectedTagIds: ref([]), uploadLanguage: ref(''), uploadMinSpeakers: ref(''),
        uploadMaxSpeakers: ref(''), uploadHotwords: ref(''), uploadInitialPrompt: ref(''), uploadTranscriptionModel: ref(''),
        uploadPromptVariables: {}, transcriptionModelOptions: ref([]), useAsrEndpoint: ref(false),
        connectorSupportsDiarization: ref(false), asrLanguage: ref(''), asrMinSpeakers: ref(''), asrMaxSpeakers: ref(''),
        dragover: ref(false), availableTags: ref([]), uploadTagSearchFilter: ref(''), availableFolders: ref([]),
        selectedFolderId: ref(null), incognitoMode: ref(false), incognitoRecording: ref(null), incognitoProcessing: ref(false),
        videoRetentionEnabled: ref(false), keepAudioOnly: ref(false), maxAudioOnlyVideoSizeMB: ref(0),
        currentView: ref('upload'), showUploadModal: ref(false), uploadDisclaimer: ref(''), showUploadDisclaimerModal: ref(false),
        isRecording: ref(false),
    };
    const utils = {
        setGlobalError: vi.fn(), showToast: vi.fn(), formatFileSize: (n) => `${n} B`, onChatComplete: vi.fn(),
        t: (key) => key, currentUserId: () => userId,
    };
    return { state, utils, upload: useUpload(state, utils) };
}

const recordingItem = (overrides = {}) => ({
    file: new File([new Uint8Array(16)], 'recording-2026-11-20T10-00-00-000Z.webm', { type: 'audio/webm' }),
    notes: '', tags: [], asrOptions: {}, preserveOptions: true, status: 'ready', recordingId: null,
    clientId: 'client-1763600000000-abcdefg', error: null, willAutoSummarize: false,
    fromInProgressRecording: true, recordingSessionStartTime: '2026-11-20T10:00:00.000Z',
    ...overrides,
});

beforeEach(() => {
    store.clear();
    nextId = 1;
    Object.assign(server, { mode: 'ok', stored: [], sends: [], nextRecordingId: 100 });
    globalThis.XMLHttpRequest = FakeXHR;
    globalThis.navigator = { onLine: true };
    vi.clearAllMocks();
    confirmDialog.mockImplementation(async () => false);
    local.session = null;
    local.remembered = null;
});

afterEach(() => {
    delete globalThis.XMLHttpRequest;
});

describe('a recording whose upload failed (#650)', () => {
    it('is saved once with its ties, and its crash-recovery copy goes', async () => {
        const { state, upload } = makeComposable();
        server.mode = 'network';
        const item = recordingItem({ serverSessionId: 'sess-1' });
        state.uploadQueue.value.push(item);
        upload.startProcessingQueue();
        await flush();

        expect(item.status).toBe('failed');
        expect(FailedUploads.storeFailedUpload).toHaveBeenCalledTimes(1);
        const [record] = store.values();
        expect(record).toMatchObject({
            clientId: item.clientId, fromRecorder: true, recordingSessionStartTime: '2026-11-20T10:00:00.000Z',
            serverSessionId: 'sess-1', ownerId: '1',
        });
        expect(item._failedUploadId).toBe(record.id);
        // The record holds the whole recording; the chunks saved while recording would be a second copy.
        expect(RecordingDB.clearRecordingSessionIfStartedAt).toHaveBeenCalledWith('2026-11-20T10:00:00.000Z');
        expect(server.stored).toHaveLength(0);
    });

    it('is uploaded once when the connection is back, and nothing is left to upload or offer at the next start', async () => {
        const first = makeComposable();
        server.mode = 'network';
        first.state.uploadQueue.value.push(recordingItem({ serverSessionId: 'sess-1' }));
        first.upload.startProcessingQueue();
        await flush();

        // The browser reports a connection: the record is sent again.
        server.mode = 'ok';
        await first.upload.resurfaceFailedUploads();
        await flush();
        expect(server.stored).toHaveLength(1);
        expect(server.sends.map((s) => s.fromRecorder)).toEqual(['true', 'true']);
        expect(store.size).toBe(0);
        expect(RecordingDB.clearRecordingSessionIfStartedAt).toHaveBeenLastCalledWith('2026-11-20T10:00:00.000Z');
        expect(ServerSessions.discardSession).toHaveBeenCalledWith('sess-1');
        expect(first.state.uploadQueue.value.filter((i) => i.status !== 'failed')).toHaveLength(1);
        // Sent again after the connection came back: the app stays where it is.
        expect(first.state.currentView.value).toBe('upload');

        // The next two starts of the app: nothing to send.
        for (let start = 0; start < 2; start++) {
            const next = makeComposable();
            await next.upload.resurfaceFailedUploads();
            await flush();
            expect(next.state.uploadQueue.value).toHaveLength(0);
            expect(await next.upload.pendingUploadCovers({ startTime: '2026-11-20T10:00:00.000Z' })).toBe(false);
        }
        expect(server.sends).toHaveLength(2);
        expect(server.stored).toHaveLength(1);
    });

    it('is not sent twice when the page loads and the browser reports a connection at once', async () => {
        const { state, upload } = makeComposable();
        server.mode = 'network';
        state.uploadQueue.value.push(recordingItem());
        upload.startProcessingQueue();
        await flush();
        server.mode = 'hang';
        await Promise.all([upload.resurfaceFailedUploads(), upload.resurfaceFailedUploads()]);
        await flush();
        await upload.resurfaceFailedUploads();
        await flush();
        expect(server.sends).toHaveLength(2); // the failed one and one retry, in flight
        expect(state.uploadQueue.value).toHaveLength(1);
        expect(state.uploadQueue.value[0].status).toBe('uploading');
    });

    it('is stored once by the server when the first request arrived but its answer was lost', async () => {
        const { state, upload } = makeComposable();
        server.mode = 'timeout';
        state.uploadQueue.value.push(recordingItem());
        upload.startProcessingQueue();
        await flush();
        expect(server.stored).toHaveLength(1);
        expect(store.size).toBe(1);

        server.mode = 'ok';
        await upload.resurfaceFailedUploads();
        await flush();
        // The same client_upload_id: the server answers with the recording it already has.
        expect(server.sends.map((s) => s.clientId)).toEqual([server.stored[0].clientId, server.stored[0].clientId]);
        expect(server.stored).toHaveLength(1);
        expect(store.size).toBe(0);
        expect(state.recordings.value.map((r) => r.id)).toEqual([server.stored[0].id]);
    });

    it('marks an upload of a file from disk as not recorded here and stores nothing for it', async () => {
        const { state, upload } = makeComposable();
        server.mode = 'network';
        state.uploadQueue.value.push(recordingItem({ fromInProgressRecording: false, recordingSessionStartTime: null }));
        upload.startProcessingQueue();
        await flush();
        expect(FailedUploads.storeFailedUpload).not.toHaveBeenCalled();
        expect(server.sends[0].fromRecorder).toBeNull();
    });
});

describe('copies of another account (#650)', () => {
    const seed = (ownerId) => FailedUploads.storeFailedUpload({
        file: new File([new Uint8Array(4)], 'recording-a.webm', { type: 'audio/webm' }),
        clientId: `client-${ownerId}-1234567`, fromRecorder: true, ownerId, source: 'offline',
    });

    it('are deleted when another account signs in, with a notice and no question, and never uploaded', async () => {
        await seed('2');
        await seed('2');
        await seed('1');
        const { upload, utils } = makeComposable({ userId: '1' });
        await upload.resurfaceFailedUploads();
        await flush();
        expect(server.sends.map((s) => s.clientId)).toEqual(['client-1-1234567']);
        expect(confirmDialog).not.toHaveBeenCalled();
        await flush();
        expect(utils.showToast).toHaveBeenCalledWith('upload.otherAccountCopiesDeleted', 'fa-trash', 8000);
        expect([...store.values()].filter((r) => r.ownerId === '2')).toHaveLength(0);
    });

    it('include the crash-recovery copy and the remembered server session of that account', async () => {
        await seed('2');
        local.session = { startTime: 'T2', ownerId: '2', chunks: [1] };
        local.remembered = { session_id: 's2', owner_id: '2' };
        const { upload, utils } = makeComposable({ userId: '1' });
        expect(await upload.removeOtherAccountsCopies()).toBe(3);
        await flush();
        expect(local.session).toBeNull();
        expect(local.remembered).toBeNull();
        expect(store.size).toBe(0);
        expect(utils.showToast).toHaveBeenCalledTimes(1);
        expect(utils.showToast.mock.calls[0][0]).toBe('upload.otherAccountCopiesDeleted');
        // Nothing left: no second notice.
        expect(await upload.removeOtherAccountsCopies()).toBe(0);
        await flush();
        expect(utils.showToast).toHaveBeenCalledTimes(1);
    });

    it('says "1 recording" for one', async () => {
        await seed('2');
        const { upload, utils } = makeComposable({ userId: '1' });
        expect(await upload.removeOtherAccountsCopies()).toBe(1);
        await flush();
        expect(utils.showToast).toHaveBeenCalledWith('upload.otherAccountCopiesDeletedOne', 'fa-trash', 8000);
    });

    it('leave this account\'s copies and those of an earlier release alone', async () => {
        await seed('1');
        await FailedUploads.storeFailedUpload({ file: new File([new Uint8Array(4)], 'old.webm'), clientId: 'client-old-1234567' });
        local.session = { startTime: 'T1', ownerId: '1', chunks: [1] };
        local.remembered = { session_id: 's0' };  // no owner: an earlier release
        const { upload, utils } = makeComposable({ userId: '1' });
        expect(await upload.removeOtherAccountsCopies()).toBe(0);
        expect(store.size).toBe(2);
        expect(local.session).not.toBeNull();
        expect(local.remembered).not.toBeNull();
        expect(utils.showToast).not.toHaveBeenCalled();
        // Without a known account nothing is deleted.
        await seed('9');
        expect(await makeComposable({ userId: '' }).upload.removeOtherAccountsCopies()).toBe(0);
        expect(store.size).toBe(3);
    });

    it('are uploaded by the account they belong to, and a record of an earlier release by anyone', async () => {
        await seed('2');
        await FailedUploads.storeFailedUpload({ file: new File([new Uint8Array(4)], 'old.webm'), clientId: 'client-old-1234567' });
        const { upload } = makeComposable({ userId: '2' });
        await upload.resurfaceFailedUploads();
        await flush();
        expect(server.sends.map((s) => s.clientId).sort()).toEqual(['client-2-1234567', 'client-old-1234567']);
        expect(store.size).toBe(0);
    });

    it('do not hide this account\'s copy from the recovery check, nor cover another account\'s', async () => {
        await FailedUploads.storeFailedUpload({ file: new File([1], 'a.webm'), clientId: 'client-a-1234567', recordingSessionStartTime: 'T', ownerId: '1' });
        expect(await makeComposable({ userId: '1' }).upload.pendingUploadCovers({ startTime: 'T' })).toBe(true);
        expect(await makeComposable({ userId: '2' }).upload.pendingUploadCovers({ startTime: 'T' })).toBe(false);
    });
});
