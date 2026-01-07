/**
 * UI state management
 */

export function createUIState(ref, computed) {
    // --- UI State ---
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

    // --- i18n State ---
    const currentLanguage = ref('en');
    const currentLanguageName = ref('English');
    const availableLanguages = ref([]);
    const showLanguageMenu = ref(false);

    // --- Column Resizing State ---
    const leftColumnWidth = ref(60);
    const rightColumnWidth = ref(40);
    const isResizing = ref(false);

    // --- Transcription State ---
    const transcriptionViewMode = ref('simple');
    const legendExpanded = ref(false);
    const highlightedSpeaker = ref(null);
    const processingIndicatorMinimized = ref(false);

    // --- Virtual Scroll State ---
    // For transcript panel virtual scrolling (performance optimization for long transcriptions)
    const transcriptScrollTop = ref(0);
    const transcriptContainerHeight = ref(0);
    const transcriptItemHeight = 48; // Estimated height per segment in pixels

    // --- Computed Properties ---
    const isMobileScreen = computed(() => {
        return windowWidth.value < 1024;
    });

    // --- Color Scheme Definitions ---
    const colorSchemes = {
        light: [
            { id: 'blue', name: 'Ocean Blue', description: 'Classic blue theme with professional appeal', class: '' },
            { id: 'emerald', name: 'Forest Emerald', description: 'Fresh green theme for a natural feel', class: 'theme-light-emerald' },
            { id: 'purple', name: 'Royal Purple', description: 'Elegant purple theme with sophistication', class: 'theme-light-purple' },
            { id: 'rose', name: 'Sunset Rose', description: 'Warm pink theme with gentle energy', class: 'theme-light-rose' },
            { id: 'amber', name: 'Golden Amber', description: 'Warm yellow theme for brightness', class: 'theme-light-amber' },
            { id: 'teal', name: 'Ocean Teal', description: 'Cool teal theme for tranquility', class: 'theme-light-teal' }
        ],
        dark: [
            { id: 'blue', name: 'Midnight Blue', description: 'Deep blue theme for focused work', class: '' },
            { id: 'emerald', name: 'Dark Forest', description: 'Rich green theme for comfortable viewing', class: 'theme-dark-emerald' },
            { id: 'purple', name: 'Deep Purple', description: 'Mysterious purple theme for creativity', class: 'theme-dark-purple' },
            { id: 'rose', name: 'Dark Rose', description: 'Muted pink theme with subtle warmth', class: 'theme-dark-rose' },
            { id: 'amber', name: 'Dark Amber', description: 'Warm brown theme for cozy sessions', class: 'theme-dark-amber' },
            { id: 'teal', name: 'Deep Teal', description: 'Dark teal theme for calm focus', class: 'theme-dark-teal' }
        ]
    };

    return {
        // UI
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

        // i18n
        currentLanguage,
        currentLanguageName,
        availableLanguages,
        showLanguageMenu,

        // Column Resizing
        leftColumnWidth,
        rightColumnWidth,
        isResizing,

        // Transcription
        transcriptionViewMode,
        legendExpanded,
        highlightedSpeaker,
        processingIndicatorMinimized,

        // Virtual Scroll
        transcriptScrollTop,
        transcriptContainerHeight,
        transcriptItemHeight,

        // Computed
        isMobileScreen,

        // Constants
        colorSchemes
    };
}
