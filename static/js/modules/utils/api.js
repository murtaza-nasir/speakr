/**
 * API utility functions with CSRF token handling
 */

export const createApiClient = (csrfToken) => {
    const getHeaders = (contentType = 'application/json') => {
        const headers = {
            'X-CSRFToken': csrfToken.value
        };
        if (contentType) {
            headers['Content-Type'] = contentType;
        }
        return headers;
    };

    return {
        get: async (url) => {
            const response = await fetch(url, {
                headers: getHeaders()
            });
            return response;
        },

        post: async (url, data = {}) => {
            const response = await fetch(url, {
                method: 'POST',
                headers: getHeaders(),
                body: JSON.stringify(data)
            });
            return response;
        },

        postFormData: async (url, formData) => {
            const response = await fetch(url, {
                method: 'POST',
                headers: {
                    'X-CSRFToken': csrfToken.value
                },
                body: formData
            });
            return response;
        },

        delete: async (url) => {
            const response = await fetch(url, {
                method: 'DELETE',
                headers: getHeaders()
            });
            return response;
        },

        put: async (url, data = {}) => {
            const response = await fetch(url, {
                method: 'PUT',
                headers: getHeaders(),
                body: JSON.stringify(data)
            });
            return response;
        }
    };
};
