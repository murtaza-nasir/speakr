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
        // Resolved once init() has loaded the translations, so code that
        // renders before then (the shared page header) can wait for them.
        this.isReady = false;
        this.ready = new Promise(resolve => { this._resolveReady = resolve; });
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

        this._markReady();
    }

    _markReady() {
        if (this.isReady) return;
        this.isReady = true;
        this._resolveReady();
        window.dispatchEvent(new CustomEvent('i18nReady', { detail: { locale: this.currentLocale } }));
    }

    /**
     * Use translations embedded in the page (window.__I18N_BOOTSTRAP, from
     * templates/includes/i18n_bootstrap.html), so the first render is already
     * translated and no locale file is fetched for it. The locale is chosen as
     * in init(). When the chosen locale is not among the embedded ones, ready
     * stays pending and the locale is fetched by init() as before.
     * @param {{locale: string, translations: Object<string, Object>}} data
     * @returns {boolean} true when the translations are ready
     */
    applyBootstrap(data) {
        if (!data || typeof data !== 'object' || !data.translations || typeof data.translations !== 'object') {
            return false;
        }
        for (const [code, table] of Object.entries(data.translations)) {
            if (table && typeof table === 'object') {
                this.translations[code] = table;
                this.loadedLocales.add(code);
            }
        }
        let savedLocale = null;
        try {
            savedLocale = localStorage.getItem('preferredLanguage');
        } catch (e) {
            savedLocale = null;
        }
        this.currentLocale = savedLocale || data.locale || this.fallbackLocale;
        if (!this.loadedLocales.has(this.currentLocale) || !this.loadedLocales.has(this.fallbackLocale)) {
            return false;
        }
        this._markReady();
        return true;
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
            // 'no-cache' forces a conditional revalidation so a browser (or
            // proxy) heuristic cache can never serve a previous release's
            // locale file against newer templates — the "Translation not
            // found" spam after upgrades (issue #357).
            const response = await fetch(`/static/locales/${locale}.json`, { cache: 'no-cache' });
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
     * The interface locale as a language tag for Intl and the toLocale*String
     * methods, so dates and numbers follow the selected language (#480).
     * Undefined, which Intl reads as the browser default, when the stored code
     * is not a valid tag: Intl throws on one, and a date should still render.
     * @returns {string|undefined}
     */
    getIntlLocale() {
        try {
            return Intl.getCanonicalLocales(this.currentLocale)[0];
        } catch (e) {
            return undefined;
        }
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
            { code: 'zh', name: 'Chinese', nativeName: '中文' },
            { code: 'ru', name: 'Russian', nativeName: 'Русский' },
            { code: 'pt-BR', name: 'Portuguese (Brazil)', nativeName: 'Português (Brasil)' },
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
            // Warn ONCE per missing key. Without this, a missing key used in a
            // component that re-renders rapidly (e.g. the chat modal during a
            // drag) floods the console with the same warning every frame.
            if (!this._warnedMissingKeys) this._warnedMissingKeys = new Set();
            if (!this._warnedMissingKeys.has(key)) {
                this._warnedMissingKeys.add(key);
                console.warn(`Translation not found for key: ${key}`);
            }
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
        return new Intl.DateTimeFormat(this.getIntlLocale(), options).format(d);
    }

    /**
     * Format number according to locale
     * @param {number} number - Number to format
     * @param {Object} options - Intl.NumberFormat options
     * @returns {string} Formatted number string
     */
    formatNumber(number, options = {}) {
        return new Intl.NumberFormat(this.getIntlLocale(), options).format(number);
    }

    /**
     * Format a percentage in the interface language: "12.5%", "12,5 %" in
     * German (#480). The value is on the 0-100 scale, as every caller holds
     * it: formatPercent(12.5) is 12.5 %, not 1250 %.
     * @param {number} percent - Percentage, 0-100 (more is allowed)
     * @param {object} options - Intl.NumberFormat options; at most one decimal by default
     * @returns {string} Formatted percentage
     */
    formatPercent(percent, options = {}) {
        const value = Number(percent);
        return this.formatNumber(Number.isFinite(value) ? value / 100 : 0,
            { maximumFractionDigits: 1, ...options, style: 'percent' });
    }

    /**
     * Format file size with appropriate unit, at most one decimal, in the
     * interface language's number format: "1.5 MB", "1,5 MB" in German (#481).
     * src/utils/localization.py format_file_size() is the server-side twin.
     * @param {number} bytes - Size in bytes
     * @returns {string} Formatted file size
     */
    formatFileSize(bytes) {
        const units = ['bytes', 'kilobytes', 'megabytes', 'gigabytes', 'terabytes'];
        const size = Math.max(Number(bytes) || 0, 0);
        let unit = 0;
        while (unit < units.length - 1 && size >= 1024 ** (unit + 1)) unit++;
        // 1023.97 KB would round to "1024 KB"; that is 1 MB.
        if (unit < units.length - 1 && Math.round(size / 1024 ** unit * 10) / 10 >= 1024) unit++;
        // No grouping: below 1 PB the count is under 1024, and German "1.023 B" reads as 1.023.
        const count = this.formatNumber(size / 1024 ** unit, { maximumFractionDigits: 1, useGrouping: false });
        return this.t(`fileSize.${units[unit]}`, { count });
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
        // Backend timestamps are naive UTC; append 'Z' for string inputs so the
        // elapsed-time diff is computed against true UTC, not a local mis-parse.
        let d;
        if (date instanceof Date) {
            d = date;
        } else {
            let s = date;
            if (typeof s === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(s) && !/(?:Z|[+-]\d{2}:?\d{2})$/.test(s)) {
                s = s.replace(' ', 'T') + 'Z';
            }
            d = new Date(s);
        }
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

// Create a fallback t function immediately
if (typeof window !== 'undefined') {
    // Ensure window.i18n exists with at least a basic t function
    window.i18n = i18n;

    // Translations embedded in the page are used at once (see applyBootstrap).
    if (window.__I18N_BOOTSTRAP) {
        i18n.applyBootstrap(window.__I18N_BOOTSTRAP);
    }
    
    // Add a fallback t function if the class method isn't ready
    if (!window.i18n.t) {
        window.i18n.t = function(key, params) {
            console.warn('i18n.t called before initialization, returning key:', key);
            return key;
        };
    }
}