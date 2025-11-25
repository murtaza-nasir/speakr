/**
 * IndexedDB Recording Persistence
 * Handles saving recording chunks to IndexedDB for crash recovery
 */

const DB_NAME = 'SpeakrRecordings';
const DB_VERSION = 1;
const STORE_NAME = 'activeRecording';

let dbInstance = null;

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
export const startRecordingSession = async (recordingData) => {
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
            duration: 0
        };

        await objectStore.put(session);
        console.log('[RecordingDB] Recording session started:', session.id);
        return session;
    } catch (error) {
        console.error('[RecordingDB] Failed to start session:', error);
        throw error;
    }
};

/**
 * Save a recording chunk to IndexedDB
 */
export const saveChunk = async (chunkBlob, chunkIndex) => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        // Get current session
        const session = await objectStore.get('current');

        if (!session) {
            console.warn('[RecordingDB] No active session found');
            return;
        }

        // Convert Blob to ArrayBuffer for storage
        const arrayBuffer = await chunkBlob.arrayBuffer();

        // Add chunk to session
        session.chunks.push({
            index: chunkIndex,
            data: arrayBuffer,
            size: chunkBlob.size,
            timestamp: Date.now()
        });

        // Update session
        await objectStore.put(session);
        console.log(`[RecordingDB] Chunk ${chunkIndex} saved (${chunkBlob.size} bytes)`);
    } catch (error) {
        console.error('[RecordingDB] Failed to save chunk:', error);
        // Don't throw - recording should continue even if persistence fails
    }
};

/**
 * Update recording metadata (notes, duration, etc.)
 */
export const updateRecordingMetadata = async (updates) => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        const session = await objectStore.get('current');

        if (!session) {
            console.warn('[RecordingDB] No active session to update');
            return;
        }

        // Merge updates
        Object.assign(session, updates);
        await objectStore.put(session);

        console.log('[RecordingDB] Metadata updated:', updates);
    } catch (error) {
        console.error('[RecordingDB] Failed to update metadata:', error);
    }
};

/**
 * Check if there's a recoverable recording
 */
export const checkForRecoverableRecording = async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readonly');
        const objectStore = transaction.objectStore(STORE_NAME);

        const session = await objectStore.get('current');

        if (!session || !session.chunks || session.chunks.length === 0) {
            return null;
        }

        // Calculate total size
        const totalSize = session.chunks.reduce((sum, chunk) => sum + chunk.size, 0);

        // Calculate approximate duration (1 second chunks)
        const duration = session.chunks.length;

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

        const session = await objectStore.get('current');

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
                duration: session.chunks.length,
                startTime: session.startTime
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
export const clearRecordingSession = async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        await objectStore.delete('current');
        console.log('[RecordingDB] Recording session cleared');
    } catch (error) {
        console.error('[RecordingDB] Failed to clear session:', error);
    }
};

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
