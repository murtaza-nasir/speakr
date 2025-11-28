/**
 * API Tokens Management Composable
 * Handles API token operations for user authentication
 */

const { ref, computed } = Vue;

export function useTokens({ showToast, setGlobalError }) {
    // State
    const tokens = ref([]);
    const isLoadingTokens = ref(false);
    const showCreateTokenModal = ref(false);
    const showTokenSecretModal = ref(false);
    const newTokenSecret = ref('');
    const newTokenData = ref(null);
    const tokenForm = ref({
        name: '',
        expires_in_days: 0  // 0 = no expiration
    });

    // Computed
    const hasTokens = computed(() => tokens.value.length > 0);

    const activeTokens = computed(() => {
        return tokens.value.filter(token => !token.revoked && !isTokenExpired(token));
    });

    const expiredOrRevokedTokens = computed(() => {
        return tokens.value.filter(token => token.revoked || isTokenExpired(token));
    });

    // Helper methods
    const isTokenExpired = (token) => {
        if (!token.expires_at) return false;
        const expiryDate = new Date(token.expires_at);
        return expiryDate < new Date();
    };

    const formatTokenDate = (dateString) => {
        if (!dateString) return 'Never';
        const date = new Date(dateString);
        return date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
    };

    const getTokenStatus = (token) => {
        if (token.revoked) return 'revoked';
        if (isTokenExpired(token)) return 'expired';
        return 'active';
    };

    const getTokenStatusClass = (token) => {
        const status = getTokenStatus(token);
        const baseClasses = 'px-2 py-1 text-xs font-semibold rounded';

        switch (status) {
            case 'active':
                return `${baseClasses} bg-green-100 text-green-800`;
            case 'expired':
                return `${baseClasses} bg-yellow-100 text-yellow-800`;
            case 'revoked':
                return `${baseClasses} bg-red-100 text-red-800`;
            default:
                return `${baseClasses} bg-gray-100 text-gray-800`;
        }
    };

    // API methods
    const loadTokens = async () => {
        isLoadingTokens.value = true;
        try {
            const response = await fetch('/api/tokens', {
                method: 'GET',
                headers: {
                    'Content-Type': 'application/json'
                }
            });

            if (!response.ok) {
                throw new Error('Failed to load tokens');
            }

            const data = await response.json();
            tokens.value = data.tokens || [];
        } catch (error) {
            console.error('Error loading tokens:', error);
            setGlobalError('Failed to load API tokens: ' + error.message);
        } finally {
            isLoadingTokens.value = false;
        }
    };

    const createToken = async () => {
        if (!tokenForm.value.name || tokenForm.value.name.trim() === '') {
            showToast('Please enter a token name', 'error');
            return;
        }

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

            const response = await fetch('/api/tokens', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    name: tokenForm.value.name,
                    expires_in_days: parseInt(tokenForm.value.expires_in_days) || 0
                })
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'Failed to create token');
            }

            const data = await response.json();

            // Store the plaintext token to show to user (only shown once)
            newTokenSecret.value = data.token;
            newTokenData.value = {
                id: data.id,
                name: data.name,
                created_at: data.created_at,
                expires_at: data.expires_at
            };

            // Add to tokens list (without the plaintext token)
            tokens.value.unshift({
                id: data.id,
                name: data.name,
                created_at: data.created_at,
                last_used_at: data.last_used_at,
                expires_at: data.expires_at,
                revoked: data.revoked
            });

            // Reset form
            tokenForm.value = {
                name: '',
                expires_in_days: 0
            };

            // Close create modal and show secret modal
            showCreateTokenModal.value = false;
            showTokenSecretModal.value = true;

            showToast('API token created successfully', 'success');
        } catch (error) {
            console.error('Error creating token:', error);
            showToast('Failed to create token: ' + error.message, 'error');
        }
    };

    const revokeToken = async (tokenId, tokenName) => {
        if (!confirm(`Are you sure you want to revoke the token "${tokenName}"? This action cannot be undone and any applications using this token will lose access.`)) {
            return;
        }

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

            const response = await fetch(`/api/tokens/${tokenId}`, {
                method: 'DELETE',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                }
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'Failed to revoke token');
            }

            // Remove from local list
            tokens.value = tokens.value.filter(t => t.id !== tokenId);

            showToast('Token revoked successfully', 'success');
        } catch (error) {
            console.error('Error revoking token:', error);
            showToast('Failed to revoke token: ' + error.message, 'error');
        }
    };

    const updateTokenName = async (tokenId, newName) => {
        if (!newName || newName.trim() === '') {
            showToast('Token name cannot be empty', 'error');
            return;
        }

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');

            const response = await fetch(`/api/tokens/${tokenId}`, {
                method: 'PATCH',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({ name: newName })
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'Failed to update token');
            }

            const data = await response.json();

            // Update local token
            const token = tokens.value.find(t => t.id === tokenId);
            if (token) {
                token.name = data.name;
            }

            showToast('Token name updated', 'success');
        } catch (error) {
            console.error('Error updating token:', error);
            showToast('Failed to update token: ' + error.message, 'error');
        }
    };

    const copyTokenToClipboard = async (token) => {
        try {
            await navigator.clipboard.writeText(token);
            showToast('Token copied to clipboard', 'success');
        } catch (error) {
            console.error('Error copying token:', error);
            showToast('Failed to copy token to clipboard', 'error');
        }
    };

    const openCreateTokenModal = () => {
        tokenForm.value = {
            name: '',
            expires_in_days: 0
        };
        showCreateTokenModal.value = true;
    };

    const closeCreateTokenModal = () => {
        showCreateTokenModal.value = false;
        tokenForm.value = {
            name: '',
            expires_in_days: 0
        };
    };

    const closeTokenSecretModal = () => {
        showTokenSecretModal.value = false;
        newTokenSecret.value = '';
        newTokenData.value = null;
    };

    return {
        // State
        tokens,
        isLoadingTokens,
        showCreateTokenModal,
        showTokenSecretModal,
        newTokenSecret,
        newTokenData,
        tokenForm,

        // Computed
        hasTokens,
        activeTokens,
        expiredOrRevokedTokens,

        // Methods
        isTokenExpired,
        formatTokenDate,
        getTokenStatus,
        getTokenStatusClass,
        loadTokens,
        createToken,
        revokeToken,
        updateTokenName,
        copyTokenToClipboard,
        openCreateTokenModal,
        closeCreateTokenModal,
        closeTokenSecretModal
    };
}
