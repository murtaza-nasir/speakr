/**
 * API client utilities for making HTTP requests
 */

class APIError extends Error {
    constructor(message, status, data) {
        super(message);
        this.name = 'APIError';
        this.status = status;
        this.data = data;
    }
}

/**
 * Safely parse JSON response, handling HTML error pages gracefully
 */
async function safeJsonParse(response) {
    const contentType = response.headers.get('content-type') || '';

    // If response is not JSON, extract useful error from HTML
    if (!contentType.includes('application/json')) {
        const text = await response.text();
        // Try to extract error message from HTML title or h1
        const titleMatch = text.match(/<title>([^<]+)<\/title>/i);
        const h1Match = text.match(/<h1>([^<]+)<\/h1>/i);
        const errorMsg = titleMatch?.[1] || h1Match?.[1] ||
            `Server returned non-JSON response (status ${response.status})`;
        throw new APIError(errorMsg, response.status, { htmlResponse: true });
    }

    return response.json();
}

export async function apiRequest(url, options = {}) {
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

    const defaultOptions = {
        headers: {
            'Content-Type': 'application/json',
            ...(csrfToken && { 'X-CSRFToken': csrfToken })
        }
    };

    const mergedOptions = {
        ...defaultOptions,
        ...options,
        headers: {
            ...defaultOptions.headers,
            ...options.headers
        }
    };

    try {
        const response = await fetch(url, mergedOptions);
        const data = await safeJsonParse(response);

        if (!response.ok) {
            throw new APIError(
                data.error || 'Request failed',
                response.status,
                data
            );
        }

        return data;
    } catch (error) {
        if (error instanceof APIError) {
            throw error;
        }
        throw new APIError(error.message, 0, null);
    }
}

export async function uploadFile(url, file, onProgress = null) {
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
    const formData = new FormData();
    formData.append('audio_file', file);

    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();

        if (onProgress) {
            xhr.upload.addEventListener('progress', (e) => {
                if (e.lengthComputable) {
                    const percentComplete = (e.loaded / e.total) * 100;
                    onProgress(percentComplete);
                }
            });
        }

        xhr.addEventListener('load', () => {
            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    const data = JSON.parse(xhr.responseText);
                    resolve(data);
                } catch (e) {
                    reject(new Error('Invalid response format'));
                }
            } else {
                try {
                    const error = JSON.parse(xhr.responseText);
                    reject(new APIError(error.error || 'Upload failed', xhr.status, error));
                } catch (e) {
                    reject(new APIError('Upload failed', xhr.status, null));
                }
            }
        });

        xhr.addEventListener('error', () => {
            reject(new Error('Network error'));
        });

        xhr.open('POST', url);
        if (csrfToken) {
            xhr.setRequestHeader('X-CSRFToken', csrfToken);
        }
        xhr.send(formData);
    });
}
