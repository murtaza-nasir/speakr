// Shared UI Components and Functionality
// This file contains reusable Vue composition functions and utilities
// that can be used across multiple pages (index, inquire, admin, etc.)

// Translate with an English fallback (i18n may not be ready yet).
function sharedT(key, fallback, params) {
    if (window.i18n && typeof window.i18n.t === 'function') {
        const value = window.i18n.t(key, params || {});
        if (value && value !== key) return value;
    }
    return fallback;
}

// Dark Mode Composition
function useDarkMode() {
    const isDarkMode = Vue.ref(false);
    
    const toggleDarkMode = () => {
        isDarkMode.value = !isDarkMode.value;
        if (isDarkMode.value) {
            document.documentElement.classList.add('dark');
            localStorage.setItem('darkMode', 'true');
        } else {
            document.documentElement.classList.remove('dark');
            localStorage.setItem('darkMode', 'false');
        }
        if (window.speakrSaveTheme) window.speakrSaveTheme('darkMode');
    };
    
    const initializeDarkMode = () => {
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        const savedMode = localStorage.getItem('darkMode');
        if (savedMode === 'true' || (savedMode === null && prefersDark)) {
            isDarkMode.value = true;
            document.documentElement.classList.add('dark');
        } else {
            isDarkMode.value = false;
            document.documentElement.classList.remove('dark');
        }
    };
    
    return {
        isDarkMode,
        toggleDarkMode,
        initializeDarkMode
    };
}

// Color Scheme Composition
function useColorScheme() {
    const showColorSchemeModal = Vue.ref(false);
    const currentColorScheme = Vue.ref('blue');
    const isDarkMode = Vue.ref(false);
    
    const colorSchemes = {
        light: [
            { id: 'blue', accent: '#3b82f6', hover: '#2563eb' },
            { id: 'emerald', accent: '#10b981', hover: '#059669' },
            { id: 'purple', accent: '#8b5cf6', hover: '#7c3aed' },
            { id: 'rose', accent: '#f43f5e', hover: '#e11d48' },
            { id: 'amber', accent: '#f59e0b', hover: '#d97706' },
            { id: 'teal', accent: '#06b6d4', hover: '#0891b2' }
        ],
        dark: [
            { id: 'blue', accent: '#60a5fa', hover: '#3b82f6' },
            { id: 'emerald', accent: '#34d399', hover: '#10b981' },
            { id: 'purple', accent: '#a78bfa', hover: '#8b5cf6' },
            { id: 'rose', accent: '#fb7185', hover: '#f43f5e' },
            { id: 'amber', accent: '#fbbf24', hover: '#f59e0b' },
            { id: 'teal', accent: '#22d3ee', hover: '#06b6d4' }
        ]
    };
    
    const applyColorScheme = (schemeId) => {
        const schemes = isDarkMode.value ? colorSchemes.dark : colorSchemes.light;
        const scheme = schemes.find(s => s.id === schemeId);
        if (scheme) {
            // Remove all theme classes
            const allThemeClasses = [
                ...colorSchemes.light.map(s => `theme-light-${s.id}`),
                ...colorSchemes.dark.map(s => `theme-dark-${s.id}`)
            ].filter(c => !c.includes('blue')); // blue is the default, no class needed
            
            document.documentElement.classList.remove(...allThemeClasses);
            
            // Apply new theme class if not blue (default)
            if (schemeId !== 'blue') {
                const themeClass = `theme-${isDarkMode.value ? 'dark' : 'light'}-${schemeId}`;
                document.documentElement.classList.add(themeClass);
            }
            
            // Don't set CSS variables - let the theme classes handle all colors
            localStorage.setItem('colorScheme', schemeId);
            currentColorScheme.value = schemeId;
        }
    };
    
    const initializeColorScheme = (darkMode) => {
        isDarkMode.value = darkMode;
        const savedScheme = localStorage.getItem('colorScheme') || (window.speakrThemeDefaults || {}).colorScheme || 'blue';
        currentColorScheme.value = savedScheme;
        applyColorScheme(savedScheme);
    };
    
    // Watch for dark mode changes and reapply color scheme
    Vue.watch(() => isDarkMode.value, (newValue) => {
        applyColorScheme(currentColorScheme.value);
    });
    
    const openColorSchemeModal = () => {
        showColorSchemeModal.value = true;
    };
    
    const closeColorSchemeModal = () => {
        showColorSchemeModal.value = false;
    };
    
    const selectColorScheme = (schemeId) => {
        applyColorScheme(schemeId);
        if (window.speakrSaveTheme) window.speakrSaveTheme('colorScheme');
        const scheme = colorSchemes[isDarkMode.value ? 'dark' : 'light'].find(s => s.id === schemeId);
        if (window.showToast && scheme) {
            window.showToast(sharedT('messages.colorSchemeApplied', 'Color scheme applied'), 'fa-palette');
        }
    };
    
    const resetColorScheme = () => {
        applyColorScheme('blue');
        if (window.speakrSaveTheme) window.speakrSaveTheme('colorScheme');
        if (window.showToast) {
            window.showToast(sharedT('messages.colorSchemeReset', 'Color scheme reset to default'), 'fa-undo');
        }
    };
    
    return {
        showColorSchemeModal,
        currentColorScheme,
        colorSchemes,
        openColorSchemeModal,
        closeColorSchemeModal,
        selectColorScheme,
        resetColorScheme,
        applyColorScheme,
        initializeColorScheme
    };
}

// Shared Transcripts Modal Composition
function useSharesModal() {
    const showSharesListModal = Vue.ref(false);
    const userShares = Vue.ref([]);
    const isLoadingShares = Vue.ref(false);
    
    // The Shared Transcripts dialog lives in the main app (#416), as on the
    // other pages that use the shared header.
    const openSharesList = () => {
        window.location.href = "/?open=shares";
    };

    const closeSharesList = () => {
        showSharesListModal.value = false;
    };
    
    const copyShareLink = async (shareId) => {
        const url = `${window.location.origin}/share/${shareId}`;
        try {
            await navigator.clipboard.writeText(url);
            if (window.showToast) {
                window.showToast(sharedT('sharing.linkCopied', 'Link copied'), 'fa-link');
            }
        } catch (err) {
            if (window.setGlobalError) {
                window.setGlobalError(sharedT('sharing.linkCopyFailed', 'Couldn\'t copy link to clipboard'));
            }
        }
    };
    
    const deleteShare = async (shareId) => {
        if (!confirm(sharedT('dialogs.deleteShare.message', 'Delete this share link? The link stops working.'))) return;
        
        try {
            const response = await fetch(`/api/shares/${shareId}`, {
                method: 'DELETE',
                headers: {
                    'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.getAttribute('content')
                }
            });
            
            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || sharedT('errors.serverErrorStatus', `HTTP ${response.status}`, { status: response.status }));
            }
            
            userShares.value = userShares.value.filter(share => share.id !== shareId);
            if (window.showToast) {
                window.showToast(sharedT('sharing.deleted', 'Share deleted'), 'fa-trash');
            }
        } catch (error) {
            if (window.setGlobalError) {
                window.setGlobalError(sharedT('sharing.deleteFailed', `Failed to delete share: ${error.message}`, { error: error.message }));
            }
        }
    };
    
    return {
        showSharesListModal,
        userShares,
        isLoadingShares,
        openSharesList,
        closeSharesList,
        copyShareLink,
        deleteShare
    };
}

// User Menu Composition  
function useUserMenu() {
    const isUserMenuOpen = Vue.ref(false);
    
    const toggleUserMenu = () => {
        isUserMenuOpen.value = !isUserMenuOpen.value;
    };
    
    const closeUserMenu = () => {
        isUserMenuOpen.value = false;
    };
    
    // Close menu when clicking outside. Use the two purpose-built
    // data attributes (data-user-menu-toggle on the trigger button,
    // data-user-menu-dropdown on the dropdown panel) instead of class
    // pattern matching — the previous class-substring matcher
    // ('flex items-center gap...') broke as soon as the trigger
    // button was rewritten to use design-system primitives (.btn,
    // .btn--ghost, etc.) because the substring no longer matched and
    // every click read as "outside the menu", so the dropdown was
    // closed in the same tick it was opened.
    Vue.onMounted(() => {
        const handleClickOutside = (e) => {
            const userMenuToggle = e.target.closest('[data-user-menu-toggle]');
            const userMenuDropdown = e.target.closest('[data-user-menu-dropdown]');
            if (!userMenuToggle && !userMenuDropdown) {
                isUserMenuOpen.value = false;
            }
        };

        document.addEventListener('click', handleClickOutside);

        Vue.onUnmounted(() => {
            document.removeEventListener('click', handleClickOutside);
        });
    });
    
    return {
        isUserMenuOpen,
        toggleUserMenu,
        closeUserMenu
    };
}

// Export for use in Vue components
window.SharedComponents = {
    useDarkMode,
    useColorScheme,
    useSharesModal,
    useUserMenu
};