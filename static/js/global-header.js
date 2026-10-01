/**
 * The page header outside the main app (account, admin, group management).
 *
 * The controls on the right of the header are one template,
 * components/header-controls.html, shared with the main app. The main app
 * renders it inside its own Vue app; this file mounts a small Vue app on
 * page-header.html that supplies the same names, so every page shows the
 * same header and menu. Actions that open a dialog only the main app has
 * (a new recording, the shared transcripts list, the colour scheme) open the
 * main app with that dialog.
 */

import { useNotifications } from './modules/composables/notifications.js';

const SCHEMES = ['blue', 'emerald', 'purple', 'rose', 'amber', 'teal'];

function applyTheme(dark) {
    const root = document.documentElement;
    root.classList.toggle('dark', dark);
    SCHEMES.forEach(s => root.classList.remove(`theme-light-${s}`, `theme-dark-${s}`));
    const scheme = localStorage.getItem('colorScheme') || 'blue';
    if (scheme !== 'blue') root.classList.add(`theme-${dark ? 'dark' : 'light'}-${scheme}`);
}

function ensureVue() {
    if (window.Vue) return Promise.resolve();
    return new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = '/static/vendor/js/vue.global.js'; // the build saves the production Vue under this name
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
    });
}

function waitForTranslations(timeoutMs) {
    const i18n = window.i18n;
    if (!i18n || i18n.isReady || !i18n.ready) return Promise.resolve();
    return Promise.race([i18n.ready, new Promise(resolve => setTimeout(resolve, timeoutMs))]);
}

async function mountGlobalHeader() {
    const el = document.getElementById('global-header');
    if (!el || el.dataset.mounted) return;
    el.dataset.mounted = '1';
    await ensureVue();
    const { createApp, ref, onMounted } = window.Vue;

    // Mount with the translations in place, so the labels appear once and
    // already translated. A page that never initialises i18n still gets its
    // header after a short wait; the labels then update when it does.
    await waitForTranslations(1500);
    const localeVersion = ref(0);
    window.addEventListener('localeChanged', () => { localeVersion.value++; });
    window.addEventListener('i18nReady', () => { localeVersion.value++; });
    const t = (key, params) => {
        localeVersion.value; // dependency, so labels follow the locale
        const i18n = window.i18n;
        return i18n && i18n.t ? i18n.t(key, params || {}) : key;
    };

    const app = createApp({
        setup() {
            const notifications = useNotifications({}, { t });
            const isUserMenuOpen = ref(false);
            const tokenBudget = ref(null);
            const isDarkMode = ref(document.documentElement.classList.contains('dark'));

            const toggleDarkMode = () => {
                isDarkMode.value = !isDarkMode.value;
                localStorage.setItem('darkMode', isDarkMode.value);
                applyTheme(isDarkMode.value);
            };
            const openInMainApp = (query) => { window.location.href = `/?${query}`; };

            onMounted(async () => {
                notifications.startNotificationPolling();
                document.addEventListener('click', (event) => {
                    if (!isUserMenuOpen.value) return;
                    if (event.target.closest('[data-user-menu-toggle], [data-user-menu-dropdown]')) return;
                    isUserMenuOpen.value = false;
                });
                try {
                    const response = await fetch('/api/user/token-budget');
                    if (response.ok) tokenBudget.value = await response.json();
                } catch (_) { /* the indicator stays hidden */ }
            });

            return {
                t,
                ...notifications,
                isUserMenuOpen,
                tokenBudget,
                isDarkMode,
                toggleDarkMode,
                switchToUploadView: () => openInMainApp('upload=1'),
                openSharesList: () => openInMainApp('open=shares'),
                openColorSchemeModal: () => openInMainApp('open=appearance'),
                // Installing the PWA is offered from the main app only.
                showInstallButton: false,
                isPWAInstalled: true,
                promptInstall: () => {},
            };
        },
    });
    app.config.compilerOptions.delimiters = ['${', '}'];
    // A template that fails to compile would leave an empty header; keep
    // the server-rendered one instead and say why in the console.
    const serverRendered = el.innerHTML;
    try {
        app.mount(el);
    } catch (error) {
        console.error('[global-header] could not mount:', error);
    }
    if (!el.hasAttribute('data-v-app') || el.childElementCount === 0) {
        console.error('[global-header] mount produced no header; restoring the server-rendered one');
        try { app.unmount(); } catch (_) { /* not mounted */ }
        el.innerHTML = serverRendered;
        el.setAttribute('data-v-app', '');
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountGlobalHeader);
} else {
    mountGlobalHeader();
}
