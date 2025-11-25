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
export const storeFailedUpload = async (uploadData) => {
    try {
        const db = await initDB();
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        const failedUpload = {
            timestamp: Date.now(),
            clientId: uploadData.clientId || `client-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`,
            fileName: uploadData.file?.name || uploadData.fileName || 'unknown',
            fileSize: uploadData.file?.size || uploadData.fileSize || 0,
            notes: uploadData.notes || '',
            tags: uploadData.tags || [],
            asrOptions: uploadData.asrOptions || {},
            retryCount: uploadData.retryCount || 0,
            lastError: uploadData.error || '',
            fileData: uploadData.fileData || null, // ArrayBuffer of file
            mimeType: uploadData.file?.type || uploadData.mimeType || 'audio/webm'
        };

        // Convert File to ArrayBuffer if needed
        if (uploadData.file && !failedUpload.fileData) {
            failedUpload.fileData = await uploadData.file.arrayBuffer();
        }

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
        const transaction = db.transaction([STORE_NAME], 'readwrite');
        const objectStore = transaction.objectStore(STORE_NAME);

        const upload = await getFailedUpload(id);
        if (!upload) {
            console.warn('[FailedUploadsDB] Upload not found for retry count update');
            return;
        }

        upload.retryCount = retryCount;
        upload.lastRetry = Date.now();
        if (error) {
            upload.lastError = error;
        }

        return new Promise((resolve, reject) => {
            const request = objectStore.put(upload);

            request.onsuccess = () => {
                console.log(`[FailedUploadsDB] Updated retry count for upload ${id}: ${retryCount}`);
                resolve();
            };

            request.onerror = () => {
                console.error('[FailedUploadsDB] Failed to update retry count:', request.error);
                reject(request.error);
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
