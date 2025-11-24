/**
 * UI State composable
 * Handles all UI-related state (theme, sidebar, modals, etc.)
 */

import { ref, computed, watch, onMounted } from 'vue';

export function useUI() {
    // State
    const browser = ref('unknown');
    const isSidebarCollapsed = ref(false);
    const searchTipsExpanded = ref(false);
    const isUserMenuOpen = ref(false);
    const isDarkMode = ref(false);
    const currentColorScheme = ref('blue');
    const showColorSchemeModal = ref(false);
    const windowWidth = ref(window.innerWidth);
    const mobileTab = ref('transcript');
    const isMetadataExpanded = ref(false);
    const currentLanguage = ref('en');
    const currentLanguageName = ref('English');
    const availableLanguages = ref([]);
    const showLanguageMenu = ref(false);

    // Computed
    const isMobile = computed(() => windowWidth.value < 768);
    const isTablet = computed(() => windowWidth.value >= 768 && windowWidth.value < 1024);
    const isDesktop = computed(() => windowWidth.value >= 1024);

    const colorSchemes = [
        { name: 'blue', label: 'Blue', primary: '#3b82f6', hover: '#2563eb' },
        { name: 'purple', label: 'Purple', primary: '#8b5cf6', hover: '#7c3aed' },
        { name: 'green', label: 'Green', primary: '#10b981', hover: '#059669' },
        { name: 'orange', label: 'Orange', primary: '#f59e0b', hover: '#d97706' },
        { name: 'pink', label: 'Pink', primary: '#ec4899', hover: '#db2777' },
        { name: 'red', label: 'Red', primary: '#ef4444', hover: '#dc2626' }
    ];

    // Methods
    const detectBrowser = () => {
        const userAgent = navigator.userAgent.toLowerCase();
        if (userAgent.indexOf('firefox') > -1) browser.value = 'firefox';
        else if (userAgent.indexOf('chrome') > -1 && userAgent.indexOf('edge') === -1) browser.value = 'chrome';
        else if (userAgent.indexOf('safari') > -1 && userAgent.indexOf('chrome') === -1) browser.value = 'safari';
        else if (userAgent.indexOf('edge') > -1) browser.value = 'edge';
        else browser.value = 'unknown';
    };

    const toggleSidebar = () => {
        isSidebarCollapsed.value = !isSidebarCollapsed.value;
        localStorage.setItem('sidebarCollapsed', isSidebarCollapsed.value.toString());
    };

    const toggleDarkMode = () => {
        isDarkMode.value = !isDarkMode.value;
        document.documentElement.classList.toggle('dark', isDarkMode.value);
        localStorage.setItem('darkMode', isDarkMode.value ? 'enabled' : 'disabled');
    };

    const setColorScheme = (scheme) => {
        currentColorScheme.value = scheme;
        document.documentElement.setAttribute('data-color-scheme', scheme);
        localStorage.setItem('colorScheme', scheme);
    };

    const loadUIPreferences = () => {
        // Load dark mode
        const savedDarkMode = localStorage.getItem('darkMode');
        if (savedDarkMode === 'enabled') {
            isDarkMode.value = true;
            document.documentElement.classList.add('dark');
        }

        // Load color scheme
        const savedScheme = localStorage.getItem('colorScheme');
        if (savedScheme && colorSchemes.find(s => s.name === savedScheme)) {
            setColorScheme(savedScheme);
        }

        // Load sidebar state
        const savedSidebar = localStorage.getItem('sidebarCollapsed');
        if (savedSidebar === 'true') {
            isSidebarCollapsed.value = true;
        }
    };

    const handleResize = () => {
        windowWidth.value = window.innerWidth;
    };

    const toggleUserMenu = () => {
        isUserMenuOpen.value = !isUserMenuOpen.value;
    };

    const closeUserMenu = () => {
        isUserMenuOpen.value = false;
    };

    const setMobileTab = (tab) => {
        mobileTab.value = tab;
    };

    const toggleMetadata = () => {
        isMetadataExpanded.value = !isMetadataExpanded.value;
    };

    // Initialize
    onMounted(() => {
        detectBrowser();
        loadUIPreferences();
        window.addEventListener('resize', handleResize);
    });

    return {
        // State
        browser,
        isSidebarCollapsed,
        searchTipsExpanded,
        isUserMenuOpen,
        isDarkMode,
        currentColorScheme,
        showColorSchemeModal,
        windowWidth,
        mobileTab,
        isMetadataExpanded,
        currentLanguage,
        currentLanguageName,
        availableLanguages,
        showLanguageMenu,

        // Computed
        isMobile,
        isTablet,
        isDesktop,
        colorSchemes,

        // Methods
        toggleSidebar,
        toggleDarkMode,
        setColorScheme,
        loadUIPreferences,
        toggleUserMenu,
        closeUserMenu,
        setMobileTab,
        toggleMetadata
    };
}
