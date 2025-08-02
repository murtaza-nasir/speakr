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
            
            // Use the original fetch to avoid recursion
            const originalFetch = window.originalFetch || fetch;
            const response = await originalFetch('/api/csrf-token', {
                method: 'GET',
                credentials: 'same-origin',
                headers: {
                    'Accept': 'application/json'
                }
            });

            if (!response.ok) {
                throw new Error(`Failed to refresh CSRF token: ${response.status} ${response.statusText}`);
            }

            const contentType = response.headers.get('content-type');
            if (!contentType || !contentType.includes('application/json')) {
                const text = await response.text();
                throw new Error(`Expected JSON response but got ${contentType}. Response: ${text.substring(0, 200)}`);
            }

            const data = await response.json();
            if (data.csrf_token) {
                this.token = data.csrf_token;
                // Update the meta tag for any other code that might read it
                const metaTag = document.querySelector('meta[name="csrf-token"]');
                if (metaTag) {
                    metaTag.setAttribute('content', this.token);
                }
                
                // Update Vue.js reactive token if available
                if (window.app && window.app.csrfToken !== undefined) {
                    window.app.csrfToken = this.token;
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
        // Store original fetch if not already stored
        if (!window.originalFetch) {
            window.originalFetch = window.fetch;
        }
        
        const originalFetch = window.originalFetch;
        const self = this;
        
        window.fetch = async function(url, options = {}) {
            // Skip CSRF token for the token refresh endpoint to avoid recursion
            if (url.includes('/api/csrf-token')) {
                return originalFetch(url, options);
            }

            // Add CSRF token to headers for API requests
            const newOptions = { ...options };
            if (url.startsWith('/api/') || url.startsWith('/upload') || url.startsWith('/save') || 
                url.startsWith('/recording/') || url.startsWith('/chat') || url.startsWith('/speakers')) {
                
                newOptions.headers = {
                    'X-CSRFToken': self.token,
                    ...newOptions.headers
                };
            }

            // Make the request
            let response = await originalFetch(url, newOptions);

            // Check for CSRF token expiration
            if ((response.status === 400 || response.status === 403) && 
                (url.startsWith('/api/') || url.startsWith('/upload') || url.startsWith('/save') || 
                 url.startsWith('/recording/') || url.startsWith('/chat') || url.startsWith('/speakers'))) {
                
                try {
                    // Try to parse as JSON first
                    const responseClone = response.clone();
                    let isCSRFError = false;
                    
                    try {
                        const errorData = await responseClone.json();
                        const errorMessage = errorData.error || '';
                        isCSRFError = errorMessage.toLowerCase().includes('csrf') || 
                                     errorMessage.toLowerCase().includes('token');
                    } catch (jsonError) {
                        // If JSON parsing fails, check if it's an HTML error page
                        const textResponse = await response.clone().text();
                        isCSRFError = textResponse.toLowerCase().includes('csrf') || 
                                     textResponse.toLowerCase().includes('token') ||
                                     textResponse.includes('<!doctype') || // HTML error page
                                     textResponse.includes('<html');
                    }
                    
                    if (isCSRFError) {
                        console.log('CSRF token expired, attempting refresh and retry...');
                        
                        try {
                            // Refresh the token
                            await self.refreshToken();
                            
                            // Retry the original request with the new token
                            newOptions.headers['X-CSRFToken'] = self.token;
                            response = await originalFetch(url, newOptions);
                            
                            if (response.ok) {
                                console.log('Request succeeded after CSRF token refresh');
                            } else {
                                console.warn('Request still failed after CSRF token refresh:', response.status);
                            }
                        } catch (refreshError) {
                            console.error('Failed to refresh CSRF token during retry:', refreshError);
                            // Return the original failed response
                        }
                    }
                } catch (parseError) {
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

    // Method to manually refresh token (for periodic refresh)
    async manualRefresh() {
        try {
            await this.refreshToken();
            return true;
        } catch (error) {
            console.error('Manual CSRF token refresh failed:', error);
            return false;
        }
    }
}

// Initialize CSRF manager when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.csrfManager = new CSRFManager();
    
    // Set up periodic token refresh every 45 minutes (before 1-hour expiration)
    setInterval(() => {
        if (window.csrfManager) {
            console.log('Performing periodic CSRF token refresh...');
            window.csrfManager.manualRefresh();
        }
    }, 45 * 60 * 1000); // 45 minutes
});
