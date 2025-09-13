/**
 * App loading overlay management
 * Prevents FOUC (Flash of Unstyled Content) during page initialization
 */

window.AppLoader = {
    initialized: false,
    readyChecks: [],

    /**
     * Initialize the loading system
     */
    init() {
        if (this.initialized) return;
        this.initialized = true;

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
        this.addReadyCheck(() => document.readyState === 'complete');

        // Check if styles are loaded
        this.addReadyCheck(() => {
            const styles = document.querySelector('link[href*="styles.css"]');
            return !styles || styles.sheet;
        });

        // Check if theme is initialized
        this.addReadyCheck(() => {
            const computed = window.getComputedStyle(document.documentElement);
            return computed.getPropertyValue('--bg-primary').trim() !== '';
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
        const overlay = document.querySelector('.app-loading-overlay');
        if (overlay) {
            overlay.classList.add('fade-out');
            setTimeout(() => {
                overlay.remove();
                document.body.classList.remove('app-loading');
            }, 300);
        } else {
            document.body.classList.remove('app-loading');
        }
    },

    /**
     * Wait for app to be ready then hide overlay
     */
    async waitForReady(timeout = 5000) {
        const startTime = Date.now();

        const checkReady = () => {
            if (this.isReady() || Date.now() - startTime > timeout) {
                this.hide();
            } else {
                requestAnimationFrame(checkReady);
            }
        };

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