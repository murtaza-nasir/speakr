/**
 * IndexedDB Failed Uploads Storage
 * Handles storing and retrying failed uploads with background sync
 */

const DB_NAME = 'SpeakrFailedUploads';
const DB_VERSION = 1;
const STORE_NAME = 'failedUploads';

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
            console.error('[FailedUploadsDB] Failed to open database:', request.error);
            reject(request.error);
        };

        request.onsuccess = () => {
            dbInstance = request.result;
            console.log('[FailedUploadsDB] Database opened successfully');
            resolve(dbInstance);
        };

        request.onupgradeneeded = (event) => {
            const db = event.target.result;

            // Create object store for failed uploads
            if (!db.objectStoreNames.contains(STORE_NAME)) {
                const objectStore = db.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
                objectStore.createIndex('timestamp', 'timestamp', { unique: false });
                objectStore.createIndex('clientId', 'clientId', { unique: false });
                console.log('[FailedUploadsDB] Object store created');
            }
        };
    });
};

/**
 * Store a failed upload for later retry
 */
// IndexedDB's structured clone cannot serialize Vue reactive proxies (tags /
// asrOptions arrive as reactive state), so objectStore.add() throws a
// DataCloneError ("Proxy object could not be cloned") and the whole record is
// lost to the download fallback. Round-trip through JSON to get plain,
// clone-safe values. fileData (an ArrayBuffer) is already cloneable as-is.
const toCloneable = (value, fallback) => {
    if (value == null) return fallback;
    try {
        return JSON.parse(JSON.stringify(value));
    } catch (_) {
        return fallback;
    }
};

export const storeFailedUpload = async (uploadData) => {
    try {
        const db = await initDB();

        const failedUpload = {
            timestamp: Date.now(),
            clientId: uploadData.clientId || `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`,
            fileName: uploadData.file?.name || uploadData.fileName || 'unknown',
            fileSize: uploadData.file?.size || uploadData.fileSize || 0,
            notes: typeof uploadData.notes === 'string' ? uploadData.notes : toCloneable(uploadData.notes, ''),
            tags: toCloneable(uploadData.tags, []),
            asrOptions: toCloneable(uploadData.asrOptions, {}),
            retryCount: uploadData.retryCount || 0,
            lastError: uploadData.error || '',
            fileData: uploadData.fileData || null, // ArrayBuffer of file
            mimeType: uploadData.file?.type || uploadData.mimeType || 'audio/webm'
        };

        // Convert File to ArrayBuffer if needed. This MUST complete before the
        // transaction is opened: an IndexedDB transaction auto-commits as soon
        // as control returns to the event loop with no pending request against
        // it, so awaiting here after opening the transaction would let it
        // finish before objectStore.add() runs (TransactionInactiveError).
        if (uploadData.file && !failedUpload.fileData) {
            failedUpload.fileData = await uploadData.file.arrayBuffer();
        }

        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);
        const request = objectStore.add(failedUpload);

        return new Promise((resolve, reject) => {
            request.onsuccess = () => {
                console.log('[FailedUploadsDB] Upload stored for retry:', failedUpload.fileName);
                resolve(request.result); // Returns the ID
            };
            request.onerror = () => {
                console.error('[FailedUploadsDB] Failed to store upload:', request.error);
                reject(request.error);
            };
        });
    } catch (error) {
        console.error('[FailedUploadsDB] Error storing failed upload:', error);
        throw error;
    }
};

/**
 * Get all failed uploads waiting to retry
 */
export const getFailedUploads = async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readonly');
        const objectStore = transaction.objectStore(STORE_NAME);

        return new Promise((resolve, reject) => {
            const request = objectStore.getAll();

            request.onsuccess = () => {
                console.log(`[FailedUploadsDB] Retrieved ${request.result.length} failed uploads`);
                resolve(request.result);
            };

            request.onerror = () => {
                console.error('[FailedUploadsDB] Failed to retrieve uploads:', request.error);
                reject(request.error);
            };
        });
    } catch (error) {
        console.error('[FailedUploadsDB] Error getting failed uploads:', error);
        return [];
    }
};

/**
 * Get a specific failed upload by ID
 */
export const getFailedUpload = async (id) => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readonly');
        const objectStore = transaction.objectStore(STORE_NAME);

        return new Promise((resolve, reject) => {
            const request = objectStore.get(id);

            request.onsuccess = () => {
                resolve(request.result);
            };

            request.onerror = () => {
                console.error('[FailedUploadsDB] Failed to get upload:', request.error);
                reject(request.error);
            };
        });
    } catch (error) {
        console.error('[FailedUploadsDB] Error getting failed upload:', error);
        return null;
    }
};

/**
 * Update retry count for a failed upload
 */
export const updateRetryCount = async (id, retryCount, error = null) => {
    try {
        const db = await initDB();

        // Read and write in a single readwrite transaction. The get() and put()
        // are chained through onsuccess (no await between them), so a request is
        // always pending against the transaction and it cannot auto-commit early
        // (TransactionInactiveError). Keeping both in one transaction also makes
        // the read-modify-write atomic against concurrent writers.
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        return new Promise((resolve, reject) => {
            const getRequest = objectStore.get(id);

            getRequest.onsuccess = () => {
                const upload = getRequest.result;
                if (!upload) {
                    console.warn('[FailedUploadsDB] Upload not found for retry count update');
                    resolve();
                    return;
                }

                upload.retryCount = retryCount;
                upload.lastRetry = Date.now();
                if (error) {
                    upload.lastError = error;
                }

                const putRequest = objectStore.put(upload);

                putRequest.onsuccess = () => {
                    console.log(`[FailedUploadsDB] Updated retry count for upload ${id}: ${retryCount}`);
                    resolve();
                };

                putRequest.onerror = () => {
                    console.error('[FailedUploadsDB] Failed to update retry count:', putRequest.error);
                    reject(putRequest.error);
                };
            };

            getRequest.onerror = () => {
                console.error('[FailedUploadsDB] Failed to read upload for retry count update:', getRequest.error);
                reject(getRequest.error);
            };
        });
    } catch (error) {
        console.error('[FailedUploadsDB] Error updating retry count:', error);
    }
};

/**
 * Delete a failed upload (after successful retry)
 */
export const deleteFailedUpload = async (id) => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        return new Promise((resolve, reject) => {
            const request = objectStore.delete(id);

            request.onsuccess = () => {
                console.log('[FailedUploadsDB] Deleted successful upload:', id);
                resolve();
            };

            request.onerror = () => {
                console.error('[FailedUploadsDB] Failed to delete upload:', request.error);
                reject(request.error);
            };
        });
    } catch (error) {
        console.error('[FailedUploadsDB] Error deleting failed upload:', error);
    }
};

/**
 * Clear all failed uploads
 */
export const clearAllFailedUploads = async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        return new Promise((resolve, reject) => {
            const request = objectStore.clear();

            request.onsuccess = () => {
                console.log('[FailedUploadsDB] Cleared all failed uploads');
                resolve();
            };

            request.onerror = () => {
                console.error('[FailedUploadsDB] Failed to clear uploads:', request.error);
                reject(request.error);
            };
        });
    } catch (error) {
        console.error('[FailedUploadsDB] Error clearing failed uploads:', error);
    }
};

/**
 * Last-resort safety net: trigger a browser-side download of the audio blob so
 * the user keeps a local copy even if neither the upload nor IndexedDB
 * persistence succeeded. Returns true if the download was triggered, false if
 * we could not build a downloadable URL (e.g. blob revoked, empty data).
 */
export const triggerLocalDownload = (file, suggestedName) => {
    if (!file || !file.size) {
        return false;
    }
    try {
        const url = URL.createObjectURL(file);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = suggestedName || file.name || `speakr-recording-${Date.now()}.webm`;
        anchor.style.display = 'none';
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
        // Revoke after a delay so the browser has time to start the download
        setTimeout(() => {
            try { URL.revokeObjectURL(url); } catch (_) { /* ignore */ }
        }, 60_000);
        console.warn('[FailedUploadsDB] Triggered local download as safety fallback');
        return true;
    } catch (error) {
        console.error('[FailedUploadsDB] Local download fallback failed:', error);
        return false;
    }
};

/**
 * Get count of failed uploads
 */
export const getFailedUploadCount = async () => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readonly');
        const objectStore = transaction.objectStore(STORE_NAME);

        return new Promise((resolve, reject) => {
            const request = objectStore.count();

            request.onsuccess = () => {
                resolve(request.result);
            };

            request.onerror = () => {
                console.error('[FailedUploadsDB] Failed to count uploads:', request.error);
                reject(request.error);
            };
        });
    } catch (error) {
        console.error('[FailedUploadsDB] Error counting failed uploads:', error);
        return 0;
    }
};
