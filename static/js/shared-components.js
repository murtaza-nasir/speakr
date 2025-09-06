// Shared UI Components and Functionality
// This file contains reusable Vue composition functions and utilities
// that can be used across multiple pages (index, inquire, admin, etc.)

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
            { id: 'blue', name: 'Ocean Blue', description: 'Classic blue theme with professional appeal', accent: '#3b82f6', hover: '#2563eb' },
            { id: 'emerald', name: 'Forest Emerald', description: 'Fresh green theme for a natural feel', accent: '#10b981', hover: '#059669' },
            { id: 'purple', name: 'Royal Purple', description: 'Elegant purple theme with sophistication', accent: '#8b5cf6', hover: '#7c3aed' },
            { id: 'rose', name: 'Sunset Rose', description: 'Warm pink theme with gentle energy', accent: '#f43f5e', hover: '#e11d48' },
            { id: 'amber', name: 'Golden Amber', description: 'Warm yellow theme for brightness', accent: '#f59e0b', hover: '#d97706' },
            { id: 'teal', name: 'Ocean Teal', description: 'Cool teal theme for tranquility', accent: '#06b6d4', hover: '#0891b2' }
        ],
        dark: [
            { id: 'blue', name: 'Midnight Blue', description: 'Deep blue for focused night work', accent: '#60a5fa', hover: '#3b82f6' },
            { id: 'emerald', name: 'Emerald Night', description: 'Rich green for comfortable viewing', accent: '#34d399', hover: '#10b981' },
            { id: 'purple', name: 'Deep Purple', description: 'Luxurious purple for creative sessions', accent: '#a78bfa', hover: '#8b5cf6' },
            { id: 'rose', name: 'Crimson', description: 'Bold red-pink for energetic work', accent: '#fb7185', hover: '#f43f5e' },
            { id: 'amber', name: 'Golden Hour', description: 'Warm amber for reduced eye strain', accent: '#fbbf24', hover: '#f59e0b' },
            { id: 'teal', name: 'Electric Cyan', description: 'Vibrant cyan for modern aesthetics', accent: '#22d3ee', hover: '#06b6d4' }
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
        const savedScheme = localStorage.getItem('colorScheme') || 'blue';
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
        const scheme = colorSchemes[isDarkMode.value ? 'dark' : 'light'].find(s => s.id === schemeId);
        if (window.showToast && scheme) {
            window.showToast(`Applied ${scheme.name} theme`, 'fa-palette');
        }
    };
    
    const resetColorScheme = () => {
        applyColorScheme('blue');
        if (window.showToast) {
            const defaultScheme = colorSchemes[isDarkMode.value ? 'dark' : 'light'].find(s => s.id === 'blue');
            window.showToast(`Reset to default ${defaultScheme?.name || 'Ocean Blue'} theme`, 'fa-undo');
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
    
    const openSharesList = async () => {
        isLoadingShares.value = true;
        showSharesListModal.value = true;
        try {
            const response = await fetch('/api/shares');
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to load shared items');
            userShares.value = data;
        } catch (error) {
            if (window.setGlobalError) {
                window.setGlobalError(`Failed to load shared items: ${error.message}`);
            } else {
                console.error('Failed to load shared items:', error);
            }
        } finally {
            isLoadingShares.value = false;
        }
    };
    
    const closeSharesList = () => {
        showSharesListModal.value = false;
    };
    
    const copyShareLink = async (shareId) => {
        const url = `${window.location.origin}/share/${shareId}`;
        try {
            await navigator.clipboard.writeText(url);
            if (window.showToast) {
                window.showToast('Share link copied to clipboard', 'fa-link');
            }
        } catch (err) {
            if (window.setGlobalError) {
                window.setGlobalError('Failed to copy link to clipboard');
            }
        }
    };
    
    const deleteShare = async (shareId) => {
        if (!confirm('Are you sure you want to delete this share?')) return;
        
        try {
            const response = await fetch(`/api/shares/${shareId}`, {
                method: 'DELETE',
                headers: {
                    'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.getAttribute('content')
                }
            });
            
            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to delete share');
            }
            
            userShares.value = userShares.value.filter(share => share.id !== shareId);
            if (window.showToast) {
                window.showToast('Share deleted successfully', 'fa-trash');
            }
        } catch (error) {
            if (window.setGlobalError) {
                window.setGlobalError(`Failed to delete share: ${error.message}`);
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
    
    // Close menu when clicking outside
    Vue.onMounted(() => {
        const handleClickOutside = (e) => {
            const userMenuButton = e.target.closest('button[class*="flex items-center gap"]');
            const userMenuDropdown = e.target.closest('div[class*="absolute right-0"]');
            const isUserMenuButtonClick = userMenuButton && userMenuButton.querySelector('i.fa-user-circle');
            
            if (!isUserMenuButtonClick && !userMenuDropdown) {
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