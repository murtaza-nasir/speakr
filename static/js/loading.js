/**
 * App loading overlay management
 * Prevents FOUC (Flash of Unstyled Content) during page initialization
 */

window.AppLoader = {
    initialized: false,
    readyChecks: [],
    initTime: Date.now(),

    /**
     * Initialize the loading system
     */
    init() {
        if (this.initialized) return;
        this.initialized = true;
        this.initTime = Date.now();

        // Add loading class to body
        document.body.classList.add('app-loading');

        // Create loading overlay if it doesn't exist
        if (!document.querySelector('.app-loading-overlay')) {
            this.createOverlay();
        }

        // Set up ready checks
        this.setupReadyChecks();
    },

    /**
     * Create the loading overlay element
     */
    createOverlay() {
        const overlay = document.createElement('div');
        overlay.className = 'app-loading-overlay';
        overlay.innerHTML = `
            <div class="app-loading-content">
                <div class="app-loading-spinner"></div>
                <div class="app-loading-text">Loading...</div>
            </div>
        `;
        document.body.appendChild(overlay);
    },

    /**
     * Add a ready check condition
     */
    addReadyCheck(checkFn) {
        this.readyChecks.push(checkFn);
    },

    /**
     * Setup default ready checks
     */
    setupReadyChecks() {
        // Check if DOM is ready
        this.addReadyCheck(() => {
            return document.readyState === 'complete' || document.readyState === 'interactive';
        });

        // Check if styles are loaded (optional - don't block on this)
        this.addReadyCheck(() => {
            try {
                const styles = document.querySelector('link[href*="styles.css"]');
                // If stylesheet isn't found or loaded, continue anyway after 2 seconds
                return !styles || styles.sheet || (Date.now() - this.initTime) > 2000;
            } catch (e) {
                console.warn('Error checking stylesheet:', e);
                return true; // Don't block on stylesheet errors
            }
        });

        // Check if theme is initialized (optional - don't block on this)
        this.addReadyCheck(() => {
            try {
                const computed = window.getComputedStyle(document.documentElement);
                const bgPrimary = computed.getPropertyValue('--bg-primary').trim();
                // Accept if property exists or if 2 seconds have passed
                return bgPrimary !== '' || (Date.now() - this.initTime) > 2000;
            } catch (e) {
                console.warn('Error checking CSS variables:', e);
                return true; // Don't block on CSS variable errors
            }
        });
    },

    /**
     * Check if all conditions are met
     */
    isReady() {
        if (this.readyChecks.length === 0) return true;
        return this.readyChecks.every(check => {
            try {
                return check();
            } catch (e) {
                return false;
            }
        });
    },

    /**
     * Hide the loading overlay
     */
    hide() {
        try {
            // Remove app-loading class immediately to show content
            document.body.classList.remove('app-loading');

            // Find all loading overlays (might be multiple)
            const overlays = document.querySelectorAll('.app-loading-overlay');

            if (overlays.length > 0) {
                overlays.forEach(overlay => {
                    // Force display none immediately in Firefox
                    overlay.style.display = 'none';

                    // Then do graceful removal
                    overlay.classList.add('fade-out');
                    setTimeout(() => {
                        try {
                            overlay.remove();
                        } catch (e) {
                            console.warn('Could not remove overlay:', e);
                        }
                    }, 100);
                });
            }

            console.log('Loader hidden successfully');
        } catch (error) {
            console.error('Error hiding loader:', error);
            // Force hide everything as last resort
            document.body.classList.remove('app-loading');
            const overlays = document.querySelectorAll('.app-loading-overlay');
            overlays.forEach(o => {
                o.style.display = 'none';
                try { o.remove(); } catch (e) {}
            });
        }
    },

    /**
     * Wait for app to be ready then hide overlay
     */
    async waitForReady(timeout = 5000) {
        const startTime = Date.now();
        let hideExecuted = false;

        const safeHide = () => {
            if (!hideExecuted) {
                hideExecuted = true;
                try {
                    this.hide();
                } catch (error) {
                    console.error('Error hiding loader:', error);
                    // Force hide even if error occurs
                    const overlay = document.querySelector('.app-loading-overlay');
                    if (overlay) overlay.remove();
                    document.body.classList.remove('app-loading');
                }
            }
        };

        const checkReady = () => {
            try {
                const elapsed = Date.now() - startTime;
                if (this.isReady()) {
                    console.log('App ready, hiding loader');
                    safeHide();
                } else if (elapsed > timeout) {
                    console.warn('Loader timeout reached, forcing hide');
                    safeHide();
                } else {
                    requestAnimationFrame(checkReady);
                }
            } catch (error) {
                console.error('Error in checkReady:', error);
                safeHide();
            }
        };

        // Hard timeout as absolute failsafe - hide after 10 seconds no matter what
        setTimeout(() => {
            if (!hideExecuted) {
                console.warn('Hard timeout reached (10s), forcing loader hide');
                safeHide();
            }
        }, 10000);

        // Start checking after a minimum display time
        setTimeout(checkReady, 300);
    }
};

// Auto-initialize on script load
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => AppLoader.init());
} else {
    AppLoader.init();
}