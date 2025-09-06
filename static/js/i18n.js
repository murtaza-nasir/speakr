/**
 * Lightweight i18n (internationalization) system for Speakr
 * Handles loading and managing translations with template variable support
 */

class I18n {
    constructor() {
        this.translations = {};
        this.currentLocale = 'en';
        this.fallbackLocale = 'en';
        this.loadedLocales = new Set();
    }

    /**
     * Initialize i18n with default locale
     * @param {string} locale - Initial locale code (e.g., 'en', 'es', 'fr', 'zh')
     */
    async init(locale = 'en') {
        // Get saved locale from localStorage or use browser language
        const savedLocale = localStorage.getItem('preferredLanguage');
        const browserLocale = navigator.language.split('-')[0];
        
        this.currentLocale = savedLocale || locale || browserLocale || 'en';
        
        // Load the initial locale
        await this.loadLocale(this.currentLocale);
        
        // Load fallback locale if different
        if (this.currentLocale !== this.fallbackLocale) {
            await this.loadLocale(this.fallbackLocale);
        }
    }

    /**
     * Load translations for a specific locale
     * @param {string} locale - Locale code to load
     */
    async loadLocale(locale) {
        if (this.loadedLocales.has(locale)) {
            return; // Already loaded
        }

        try {
            const response = await fetch(`/static/locales/${locale}.json`);
            if (!response.ok) {
                throw new Error(`Failed to load locale: ${locale}`);
            }
            
            const translations = await response.json();
            this.translations[locale] = translations;
            this.loadedLocales.add(locale);
            
            console.log(`Loaded locale: ${locale}`);
        } catch (error) {
            console.error(`Error loading locale ${locale}:`, error);
            
            // If failed to load requested locale and it's not the fallback, try fallback
            if (locale !== this.fallbackLocale) {
                console.log(`Failed to load ${locale}, will use ${this.fallbackLocale} as fallback`);
                // Don't change currentLocale - keep user's preference
                // Just ensure fallback translations are available
                await this.loadLocale(this.fallbackLocale);
            }
        }
    }

    /**
     * Change the current locale
     * @param {string} locale - New locale code
     */
    async setLocale(locale) {
        await this.loadLocale(locale);
        this.currentLocale = locale;
        localStorage.setItem('preferredLanguage', locale);
        
        // Dispatch custom event for locale change
        window.dispatchEvent(new CustomEvent('localeChanged', { detail: { locale } }));
    }

    /**
     * Get the current locale
     * @returns {string} Current locale code
     */
    getLocale() {
        return this.currentLocale;
    }

    /**
     * Get available locales
     * @returns {Array} List of available locale codes
     */
    getAvailableLocales() {
        return [
            { code: 'en', name: 'English', nativeName: 'English' },
            { code: 'es', name: 'Spanish', nativeName: 'Español' },
            { code: 'fr', name: 'French', nativeName: 'Français' },
            { code: 'zh', name: 'Chinese', nativeName: '中文' }
        ];
    }

    /**
     * Translate a key with optional parameters
     * @param {string} key - Translation key (e.g., 'common.save' or 'nav.upload')
     * @param {Object} params - Optional parameters for template replacement
     * @param {string} locale - Optional specific locale (defaults to current)
     * @returns {string} Translated text
     */
    t(key, params = {}, locale = null) {
        const targetLocale = locale || this.currentLocale;
        
        // Get translation from current locale or fallback
        let translation = this.getNestedTranslation(targetLocale, key);
        
        if (!translation && targetLocale !== this.fallbackLocale) {
            translation = this.getNestedTranslation(this.fallbackLocale, key);
        }
        
        if (!translation) {
            console.warn(`Translation not found for key: ${key}`);
            return key; // Return the key itself as fallback
        }
        
        // Replace template variables
        return this.interpolate(translation, params);
    }

    /**
     * Get nested translation value from object
     * @param {string} locale - Locale to search in
     * @param {string} key - Dot-separated key path
     * @returns {string|null} Translation value or null
     */
    getNestedTranslation(locale, key) {
        if (!this.translations[locale]) {
            return null;
        }
        
        const keys = key.split('.');
        let value = this.translations[locale];
        
        for (const k of keys) {
            if (value && typeof value === 'object' && k in value) {
                value = value[k];
            } else {
                return null;
            }
        }
        
        return typeof value === 'string' ? value : null;
    }

    /**
     * Replace template variables in translation string
     * @param {string} text - Text with placeholders like {{variable}}
     * @param {Object} params - Parameters to replace
     * @returns {string} Interpolated text
     */
    interpolate(text, params) {
        return text.replace(/\{\{(\w+)\}\}/g, (match, key) => {
            return params.hasOwnProperty(key) ? params[key] : match;
        });
    }

    /**
     * Handle pluralization
     * @param {string} key - Base translation key
     * @param {number} count - Count for pluralization
     * @param {Object} params - Additional parameters
     * @returns {string} Translated text with proper pluralization
     */
    tc(key, count, params = {}) {
        const pluralKey = count === 1 ? key : `${key}Plural`;
        return this.t(pluralKey, { ...params, count });
    }

    /**
     * Format date according to locale
     * @param {Date|string} date - Date to format
     * @param {Object} options - Intl.DateTimeFormat options
     * @returns {string} Formatted date string
     */
    formatDate(date, options = {}) {
        const d = date instanceof Date ? date : new Date(date);
        return new Intl.DateTimeFormat(this.currentLocale, options).format(d);
    }

    /**
     * Format number according to locale
     * @param {number} number - Number to format
     * @param {Object} options - Intl.NumberFormat options
     * @returns {string} Formatted number string
     */
    formatNumber(number, options = {}) {
        return new Intl.NumberFormat(this.currentLocale, options).format(number);
    }

    /**
     * Format file size with appropriate unit
     * @param {number} bytes - Size in bytes
     * @returns {string} Formatted file size
     */
    formatFileSize(bytes) {
        const units = ['bytes', 'kilobytes', 'megabytes', 'gigabytes'];
        const unitValues = [1, 1024, 1048576, 1073741824];
        
        let unitIndex = 0;
        for (let i = unitValues.length - 1; i >= 0; i--) {
            if (bytes >= unitValues[i]) {
                unitIndex = i;
                break;
            }
        }
        
        const value = Math.round(bytes / unitValues[unitIndex] * 10) / 10;
        return this.t(`fileSize.${units[unitIndex]}`, { count: value });
    }

    /**
     * Format duration with appropriate unit
     * @param {number} seconds - Duration in seconds
     * @returns {string} Formatted duration
     */
    formatDuration(seconds) {
        if (seconds < 60) {
            return this.tc('duration.seconds', Math.round(seconds), { count: Math.round(seconds) });
        } else if (seconds < 3600) {
            const minutes = Math.round(seconds / 60);
            return this.tc('duration.minutes', minutes, { count: minutes });
        } else {
            const hours = Math.round(seconds / 3600 * 10) / 10;
            return this.tc('duration.hours', hours, { count: hours });
        }
    }

    /**
     * Format relative time (e.g., "2 hours ago")
     * @param {Date|string} date - Date to format
     * @returns {string} Formatted relative time
     */
    formatRelativeTime(date) {
        const d = date instanceof Date ? date : new Date(date);
        const now = new Date();
        const diffSeconds = Math.floor((now - d) / 1000);
        
        if (diffSeconds < 60) {
            return this.t('time.justNow');
        } else if (diffSeconds < 3600) {
            const minutes = Math.floor(diffSeconds / 60);
            return minutes === 1 
                ? this.t('time.minuteAgo')
                : this.t('time.minutesAgo', { count: minutes });
        } else if (diffSeconds < 86400) {
            const hours = Math.floor(diffSeconds / 3600);
            return hours === 1
                ? this.t('time.hourAgo')
                : this.t('time.hoursAgo', { count: hours });
        } else if (diffSeconds < 604800) {
            const days = Math.floor(diffSeconds / 86400);
            return days === 1
                ? this.t('time.dayAgo')
                : this.t('time.daysAgo', { count: days });
        } else if (diffSeconds < 2592000) {
            const weeks = Math.floor(diffSeconds / 604800);
            return weeks === 1
                ? this.t('time.weekAgo')
                : this.t('time.weeksAgo', { count: weeks });
        } else if (diffSeconds < 31536000) {
            const months = Math.floor(diffSeconds / 2592000);
            return months === 1
                ? this.t('time.monthAgo')
                : this.t('time.monthsAgo', { count: months });
        } else {
            const years = Math.floor(diffSeconds / 31536000);
            return years === 1
                ? this.t('time.yearAgo')
                : this.t('time.yearsAgo', { count: years });
        }
    }
}

// Create global i18n instance
const i18n = new I18n();

// Export for use in Vue components
if (typeof window !== 'undefined') {
    window.i18n = i18n;
}