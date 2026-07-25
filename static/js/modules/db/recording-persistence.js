/**
 * IndexedDB Recording Persistence
 * Handles saving recording chunks to IndexedDB for crash recovery
 */

const DB_NAME = 'SpeakrRecordings';
const DB_VERSION = 1;
const STORE_NAME = 'activeRecording';

let dbInstance = null;

/**
 * Helper to promisify IDBRequest
 */
const promisifyRequest = (request) => {
    return new Promise((resolve, reject) => {
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
    });
};

/**
 * Serialize read-modify-write mutations of the single 'current' session record.
 *
 * saveChunk/updateRecordingMetadata/pruneOldChunks each do get -> mutate -> put
 * with awaits in between. Without serialization, two that overlap (common at
 * startup, where the first chunk's write is still opening the DB when the next
 * chunk arrives) clobber each other's writes — dropping or REORDERING chunks in
 * IndexedDB. On crash recovery the scrambled order yields a WebM whose EBML
 * header isn't first, which ffmpeg then rejects (issue #340). Chaining every
 * mutation through one promise guarantees they apply in call order, one at a
 * time. Order is fixed at call time because the .then() is attached
 * synchronously, so callers enqueue in the order MediaRecorder emits chunks.
 */
let _sessionWriteChain = Promise.resolve();
const serializeSessionWrite = (task) => {
    const result = _sessionWriteChain.then(task, task);
    // Keep the chain alive after a rejection so one failed write doesn't wedge
    // every later write; callers still see their own rejection via `result`.
    _sessionWriteChain = result.catch(() => {});
    return result;
};

/**
 * Initialize IndexedDB
 */
export const initDB = () => {
    return new Promise((resolve, reject) => {
        if (dbInstance) {
            resolve(dbInstance);
            return;
        }

        const request = indexedDB.open(DB_NAME, DB_VERSION);

        request.onerror = () => {
            console.error('[RecordingDB] Failed to open database:', request.error);
            reject(request.error);
        };

        request.onsuccess = () => {
            dbInstance = request.result;
            console.log('[RecordingDB] Database opened successfully');
            resolve(dbInstance);
        };

        request.onupgradeneeded = (event) => {
            const db = event.target.result;

            // Create object store for active recording
            if (!db.objectStoreNames.contains(STORE_NAME)) {
                const objectStore = db.createObjectStore(STORE_NAME, { keyPath: 'id' });
                objectStore.createIndex('timestamp', 'timestamp', { unique: false });
                console.log('[RecordingDB] Object store created');
            }
        };
    });
};

/**
 * Save recording metadata and initialize session
 */
export const startRecordingSession = (recordingData) => serializeSessionWrite(async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        const session = {
            id: 'current',
            timestamp: Date.now(),
            startTime: new Date().toISOString(),
            mode: recordingData.mode,
            notes: recordingData.notes || '',
            tags: recordingData.tags || [],
            asrOptions: recordingData.asrOptions || {},
            chunks: [],
            mimeType: recordingData.mimeType || 'audio/webm',
            duration: 0,
            // Incognito state travels with the crash-recovery copy so a
            // recovered recording reopens in the same mode instead of
            // silently becoming a normal (permanently stored) one.
            incognito: !!recordingData.incognito
        };

        await promisifyRequest(objectStore.put(session));
        console.log('[RecordingDB] Recording session started:', session.id);
        return session;
    } catch (error) {
        console.error('[RecordingDB] Failed to start session:', error);
        throw error;
    }
});

/**
 * Save a recording chunk to IndexedDB
 */
export const saveChunk = (chunkBlob, chunkIndex) => serializeSessionWrite(async () => {
    try {
        // Do async prep work BEFORE creating transaction to avoid auto-close
        const db = await initDB();
        const arrayBuffer = await chunkBlob.arrayBuffer();

        // Now create transaction and do all DB operations quickly
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        // Get current session
        const session = await promisifyRequest(objectStore.get('current'));

        if (!session) {
            console.warn('[RecordingDB] No active session found');
            return;
        }

        // Add chunk to session
        session.chunks.push({
            index: chunkIndex,
            data: arrayBuffer,
            size: chunkBlob.size,
            timestamp: Date.now()
        });

        // Update session - must happen before transaction auto-closes
        await promisifyRequest(objectStore.put(session));
        // Chunk saved silently to avoid spam (happens every 5 seconds)
    } catch (error) {
        console.error('[RecordingDB] Failed to save chunk:', error);
        // Don't throw - recording should continue even if persistence fails
    }
});

/**
 * Update recording metadata (notes, duration, etc.)
 */
export const updateRecordingMetadata = (updates) => serializeSessionWrite(async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        const session = await promisifyRequest(objectStore.get('current'));

        if (!session) {
            console.warn('[RecordingDB] No active session to update');
            return;
        }

        // Merge updates
        Object.assign(session, updates);
        await promisifyRequest(objectStore.put(session));
        // Metadata updated silently to avoid spam (happens every 5 seconds)
    } catch (error) {
        console.error('[RecordingDB] Failed to update metadata:', error);
    }
});

/**
 * Prune the IndexedDB buffer to the last `keepLast` chunks.
 *
 * Storage dedupe (#287 task 5): when server chunking is on and the server is
 * keeping up, the server is the durable copy, so the browser only needs a
 * small rolling buffer for the in-flight gap instead of every chunk (which
 * would blow the IndexedDB quota on hours-long recordings and double-store
 * everything). The caller MUST only prune when the upload backlog is smaller
 * than keepLast, so a not-yet-uploaded chunk is never dropped from the local
 * fallback. Non-fatal on error — pruning is an optimization, not correctness.
 */
export const pruneOldChunks = (keepLast = 5) => serializeSessionWrite(async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);
        const session = await promisifyRequest(objectStore.get('current'));
        if (!session || !Array.isArray(session.chunks) || session.chunks.length <= keepLast) {
            return;
        }
        session.chunks = session.chunks.slice(-keepLast);
        await promisifyRequest(objectStore.put(session));
    } catch (error) {
        // Pruning is best-effort; recording continues regardless.
    }
});

/**
 * Check if there's a recoverable recording
 */
export const checkForRecoverableRecording = async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readonly');
        const objectStore = transaction.objectStore(STORE_NAME);

        const session = await promisifyRequest(objectStore.get('current'));

        if (!session || !session.chunks || session.chunks.length === 0) {
            return null;
        }

        // Calculate total size
        const totalSize = session.chunks.reduce((sum, chunk) => sum + chunk.size, 0);

        // Real elapsed seconds are tracked on the session (updated every chunk
        // via updateRecordingMetadata). Fall back to an estimate from the chunk
        // count at the MediaRecorder timeslice (5s) — NOT 1s, which made a
        // 20s recording read as "4s".
        const duration = session.duration || (session.chunks.length * 5);

        console.log('[RecordingDB] Found recoverable recording:', {
            chunks: session.chunks.length,
            size: totalSize,
            duration: duration,
            startTime: session.startTime
        });

        return {
            ...session,
            totalSize,
            duration: duration
        };
    } catch (error) {
        console.error('[RecordingDB] Failed to check for recoverable recording:', error);
        return null;
    }
};

/**
 * Recover recording from IndexedDB
 */
export const recoverRecording = async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readonly');
        const objectStore = transaction.objectStore(STORE_NAME);

        const session = await promisifyRequest(objectStore.get('current'));

        if (!session || !session.chunks || session.chunks.length === 0) {
            console.warn('[RecordingDB] No recording to recover');
            return null;
        }

        // Convert chunks back to Blobs
        const chunks = session.chunks.map(chunk => {
            return new Blob([chunk.data], { type: session.mimeType });
        });

        console.log(`[RecordingDB] Recovered ${chunks.length} chunks`);

        return {
            chunks,
            metadata: {
                mode: session.mode,
                notes: session.notes,
                tags: session.tags,
                asrOptions: session.asrOptions,
                mimeType: session.mimeType,
                duration: session.duration || (session.chunks.length * 5),
                startTime: session.startTime,
                incognito: !!session.incognito
            }
        };
    } catch (error) {
        console.error('[RecordingDB] Failed to recover recording:', error);
        return null;
    }
};

/**
 * Clear recording session (after successful upload or discard)
 */
export const clearRecordingSession = () => serializeSessionWrite(async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        await promisifyRequest(objectStore.delete('current'));
        console.log('[RecordingDB] Recording session cleared');
    } catch (error) {
        console.error('[RecordingDB] Failed to clear session:', error);
    }
});

/**
 * Get database size information
 */
export const getDatabaseSize = async () => {
    try {
        if (!navigator.storage || !navigator.storage.estimate) {
            return null;
        }

        const estimate = await navigator.storage.estimate();
        return {
            usage: estimate.usage,
            quota: estimate.quota,
            percentage: ((estimate.usage / estimate.quota) * 100).toFixed(2)
        };
    } catch (error) {
        console.error('[RecordingDB] Failed to get database size:', error);
        return null;
    }
};
