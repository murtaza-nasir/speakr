// CSRF Token Management with Auto-Refresh
class CSRFManager {
    constructor() {
        this.token = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
        this.refreshPromise = null;
        this.setupFetchInterceptor();
    }

    async refreshToken() {
        // Prevent multiple simultaneous refresh requests
        if (this.refreshPromise) {
            return this.refreshPromise;
        }

        this.refreshPromise = this.performTokenRefresh();
        try {
            const newToken = await this.refreshPromise;
            return newToken;
        } finally {
            this.refreshPromise = null;
        }
    }

    async performTokenRefresh() {
        try {
            console.log('Refreshing CSRF token...');
            const response = await fetch('/api/csrf-token', {
                method: 'GET',
                credentials: 'same-origin'
            });

            if (!response.ok) {
                throw new Error(`Failed to refresh CSRF token: ${response.status}`);
            }

            const data = await response.json();
            if (data.csrf_token) {
                this.token = data.csrf_token;
                // Update the meta tag for any other code that might read it
                const metaTag = document.querySelector('meta[name="csrf-token"]');
                if (metaTag) {
                    metaTag.setAttribute('content', this.token);
                }
                console.log('CSRF token refreshed successfully');
                return this.token;
            } else {
                throw new Error('No CSRF token in response');
            }
        } catch (error) {
            console.error('Failed to refresh CSRF token:', error);
            throw error;
        }
    }

    setupFetchInterceptor() {
        const originalFetch = window.fetch;
        
        window.fetch = async (url, options = {}) => {
            // Add CSRF token to headers
            const newOptions = { ...options };
            newOptions.headers = {
                'X-CSRFToken': this.token,
                ...newOptions.headers
            };

            // Make the request
            let response = await originalFetch(url, newOptions);

            // Check if the request failed due to CSRF token expiration
            if (response.status === 400) {
                try {
                    const errorData = await response.clone().json();
                    const errorMessage = errorData.error || '';
                    
                    // Check if it's a CSRF token error
                    if (errorMessage.toLowerCase().includes('csrf') || 
                        errorMessage.toLowerCase().includes('token')) {
                        
                        console.log('CSRF token expired, attempting refresh and retry...');
                        
                        // Refresh the token
                        await this.refreshToken();
                        
                        // Retry the original request with the new token
                        newOptions.headers['X-CSRFToken'] = this.token;
                        response = await originalFetch(url, newOptions);
                        
                        if (response.ok) {
                            console.log('Request succeeded after CSRF token refresh');
                        }
                    }
                } catch (parseError) {
                    // If we can't parse the error response, just return the original response
                    console.warn('Could not parse error response for CSRF check:', parseError);
                }
            }

            return response;
        };
    }

    // Method to manually get current token
    getToken() {
        return this.token;
    }
}

// Initialize CSRF manager when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.csrfManager = new CSRFManager();
});
