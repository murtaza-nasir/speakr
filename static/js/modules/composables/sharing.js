/**
 * Sharing composable
 * Handles public and internal sharing of recordings
 */

export function useSharing(state, utils) {
    const {
        showShareModal, showSharesListModal, showShareDeleteModal,
        showUnifiedShareModal, recordingToShare, shareOptions,
        generatedShareLink, existingShareDetected, recordingPublicShares, isLoadingPublicShares,
        userShares, isLoadingShares, copiedShareId, shareToDelete, selectedRecording, recordings,
        internalShareUserSearch, internalShareSearchResults,
        internalShareRecording, internalSharePermissions, internalShareMaxPermissions,
        recordingInternalShares, isLoadingInternalShares,
        isSearchingUsers, allUsers, isLoadingAllUsers,
        enableInternalSharing, showUsernamesInUI
    } = state;

    const { showToast, setGlobalError } = utils;

    let userSearchTimeout = null;

    // Helper function to format share dates
    const formatShareDate = (dateString) => {
        if (!dateString) return 'Unknown date';

        try {
            const date = new Date(dateString);
            const now = new Date();
            const diffMs = now - date;
            const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

            // If today
            if (diffDays === 0) {
                return 'Today at ' + date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
            }
            // If yesterday
            else if (diffDays === 1) {
                return 'Yesterday at ' + date.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
            }
            // If within last week
            else if (diffDays < 7) {
                return date.toLocaleDateString('en-US', { weekday: 'long', hour: 'numeric', minute: '2-digit', hour12: true });
            }
            // Otherwise show full date
            else {
                return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });
            }
        } catch (e) {
            console.error('Error formatting date:', e);
            return dateString;
        }
    };

    // Helper function to get color class for username (like speaker colors)
    const getUserColorClass = (username) => {
        if (!username) return 'speaker-color-1';

        // Simple hash function to generate consistent color from username
        let hash = 0;
        for (let i = 0; i < username.length; i++) {
            hash = ((hash << 5) - hash) + username.charCodeAt(i);
            hash = hash & hash; // Convert to 32bit integer
        }

        // Map to color classes 1-8
        const colorNum = (Math.abs(hash) % 8) + 1;
        return `speaker-color-${colorNum}`;
    };

    // =========================================
    // Public Sharing
    // =========================================

    const openShareModal = async (recording) => {
        recordingToShare.value = recording;
        shareOptions.share_summary = true;
        shareOptions.share_notes = true;
        generatedShareLink.value = '';
        existingShareDetected.value = false;
        recordingPublicShares.value = [];
        showShareModal.value = true;

        // Load all public shares for this recording
        isLoadingPublicShares.value = true;
        try {
            const response = await fetch(`/api/shares`);
            if (response.ok) {
                const allShares = await response.json();
                // Filter to only shares for this recording and add share_url
                recordingPublicShares.value = allShares
                    .filter(share => share.recording_id === recording.id)
                    .map(share => ({
                        ...share,
                        share_url: `${window.location.origin}/share/${share.public_id}`
                    }));
            }
        } catch (error) {
            console.error('Error loading public shares:', error);
            recordingPublicShares.value = [];
        } finally {
            isLoadingPublicShares.value = false;
        }
    };

    const closeShareModal = () => {
        showShareModal.value = false;
        recordingToShare.value = null;
        existingShareDetected.value = false;
        recordingPublicShares.value = [];
    };

    const createShare = async (forceNew = false) => {
        const recording = recordingToShare.value || internalShareRecording.value;
        if (!recording) return;

        try {
            const response = await fetch(`/api/recording/${recording.id}/share`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    ...shareOptions,
                    force_new: forceNew
                })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to create share link');

            generatedShareLink.value = data.share_url;
            existingShareDetected.value = data.existing && !forceNew;

            // Add to the shares list (works for both share modal and unified modal)
            if (!data.existing) {
                recordingPublicShares.value.push({
                    ...data.share,
                    share_url: `${window.location.origin}/share/${data.share.public_id}`
                });
                // Update the recording's share count in the UI
                await refreshRecordingShareCounts();
            } else if (data.existing && !recordingPublicShares.value.find(s => s.id === data.share.id)) {
                // If existing but not in list, add it
                recordingPublicShares.value.push({
                    ...data.share,
                    share_url: `${window.location.origin}/share/${data.share.public_id}`
                });
            }

            if (data.existing && !forceNew) {
                showToast('Using existing share link', 'fa-link');
            } else {
                showToast('Share link created successfully!', 'fa-check-circle');
            }
        } catch (error) {
            setGlobalError(`Failed to create share link: ${error.message}`);
        }
    };

    const confirmDeletePublicShare = (share) => {
        shareToDelete.value = share;
        showShareDeleteModal.value = true;
    };

    const deletePublicShare = async () => {
        if (!shareToDelete.value) return;
        const shareId = shareToDelete.value.id;

        try {
            const response = await fetch(`/api/share/${shareId}`, { method: 'DELETE' });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to delete share');

            // Remove from the shares list (both modals use different arrays)
            recordingPublicShares.value = recordingPublicShares.value.filter(s => s.id !== shareId);
            userShares.value = userShares.value.filter(s => s.id !== shareId);

            // Update the recording's share count in the UI
            await refreshRecordingShareCounts();

            showToast('Share link deleted successfully.', 'fa-check-circle');
            showShareDeleteModal.value = false;
            shareToDelete.value = null;
        } catch (error) {
            setGlobalError(`Failed to delete share: ${error.message}`);
        }
    };

    const copyPublicShareLink = (shareUrl) => {
        navigator.clipboard.writeText(shareUrl).then(() => {
            showToast('Share link copied to clipboard!', 'fa-check-circle');
        }).catch(() => {
            setGlobalError('Failed to copy link to clipboard');
        });
    };

    const copyPublicShareLinkWithFeedback = (shareUrl, shareId) => {
        navigator.clipboard.writeText(shareUrl).then(() => {
            copiedShareId.value = shareId;
            showToast('Share link copied to clipboard!', 'fa-check-circle');

            // Reset after delay
            setTimeout(() => {
                copiedShareId.value = null;
            }, 1500);
        }).catch(() => {
            setGlobalError('Failed to copy link to clipboard');
        });
    };

    const refreshRecordingShareCounts = async () => {
        // Refresh the current recording if one is selected
        const recording = recordingToShare.value || internalShareRecording.value || selectedRecording.value;
        if (!recording) return;

        try {
            const response = await fetch(`/api/recordings/${recording.id}`);
            if (response.ok) {
                const updatedRecording = await response.json();

                // Update in recordings list
                const index = recordings.value.findIndex(r => r.id === recording.id);
                if (index !== -1) {
                    // Preserve reactivity by updating specific fields
                    recordings.value[index].public_share_count = updatedRecording.public_share_count || 0;
                    recordings.value[index].shared_with_count = updatedRecording.shared_with_count || 0;
                }

                // Update selected recording if it's the same one
                if (selectedRecording.value && selectedRecording.value.id === recording.id) {
                    selectedRecording.value.public_share_count = updatedRecording.public_share_count || 0;
                    selectedRecording.value.shared_with_count = updatedRecording.shared_with_count || 0;
                }

                // Update internal share recording if it's the same one
                if (internalShareRecording.value && internalShareRecording.value.id === recording.id) {
                    internalShareRecording.value.public_share_count = updatedRecording.public_share_count || 0;
                    internalShareRecording.value.shared_with_count = updatedRecording.shared_with_count || 0;
                }

                // Update recording to share if it's the same one
                if (recordingToShare.value && recordingToShare.value.id === recording.id) {
                    recordingToShare.value.public_share_count = updatedRecording.public_share_count || 0;
                    recordingToShare.value.shared_with_count = updatedRecording.shared_with_count || 0;
                }
            }
        } catch (error) {
            console.error('Failed to refresh recording share counts:', error);
        }
    };

    const copyShareLink = () => {
        if (!generatedShareLink.value) return;
        navigator.clipboard.writeText(generatedShareLink.value).then(() => {
            showToast('Share link copied to clipboard!');
        });
    };

    const copyIndividualShareLink = (shareId) => {
        const input = document.getElementById(`share-link-${shareId}`);
        if (!input) return;

        const button = input.nextElementSibling;
        if (!button) return;

        navigator.clipboard.writeText(input.value).then(() => {
            copiedShareId.value = shareId;
            showToast('Share link copied to clipboard!', 'fa-check');

            // Apply success state
            button.style.transition = 'background-color 0.2s ease';
            button.style.backgroundColor = 'var(--bg-success, #10b981)';

            // Revert after delay
            setTimeout(() => {
                button.style.backgroundColor = '';
                copiedShareId.value = null;
                setTimeout(() => {
                    button.style.transition = '';
                }, 200);
            }, 1500);
        }).catch(err => {
            console.error('Failed to copy share link:', err);
        });
    };

    // =========================================
    // Shares List
    // =========================================

    const openSharesList = async () => {
        isLoadingShares.value = true;
        showSharesListModal.value = true;
        try {
            const response = await fetch('/api/shares');
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to load shared items');
            userShares.value = data;
        } catch (error) {
            setGlobalError(`Failed to load shared items: ${error.message}`);
        } finally {
            isLoadingShares.value = false;
        }
    };

    const closeSharesList = () => {
        showSharesListModal.value = false;
        userShares.value = [];
    };

    const updateShare = async (share) => {
        try {
            const response = await fetch(`/api/share/${share.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    share_summary: share.share_summary,
                    share_notes: share.share_notes
                })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Failed to update share');
            showToast('Share permissions updated.', 'fa-check-circle');
        } catch (error) {
            setGlobalError(`Failed to update share: ${error.message}`);
        }
    };

    const confirmDeleteShare = (share) => {
        shareToDelete.value = share;
        showShareDeleteModal.value = true;
    };

    const cancelDeleteShare = () => {
        shareToDelete.value = null;
        showShareDeleteModal.value = false;
    };

    // =========================================
    // Internal Sharing
    // =========================================

    const loadAllUsers = async () => {
        if (!showUsernamesInUI.value) return;

        isLoadingAllUsers.value = true;
        try {
            const response = await fetch('/api/users/search?q=');
            if (!response.ok) {
                if (response.status === 403) {
                    throw new Error('Internal sharing is not enabled');
                }
                throw new Error('Failed to load users');
            }
            const data = await response.json();
            allUsers.value = data;
        } catch (error) {
            setGlobalError(`Failed to load users: ${error.message}`);
            allUsers.value = [];
        } finally {
            isLoadingAllUsers.value = false;
        }
    };

    const searchInternalShareUsers = async () => {
        const query = internalShareUserSearch.value.trim();

        // If SHOW_USERNAMES_IN_UI is enabled, filter allUsers locally
        if (showUsernamesInUI.value) {
            // Get list of user IDs that already have access
            const sharedUserIds = new Set(recordingInternalShares.value.map(share => share.user_id));

            // Filter out already-shared users
            const availableUsers = allUsers.value.filter(user => !sharedUserIds.has(user.id));

            if (query.length === 0) {
                internalShareSearchResults.value = availableUsers;
            } else {
                internalShareSearchResults.value = availableUsers.filter(user =>
                    user.username.toLowerCase().includes(query.toLowerCase()) ||
                    (user.email && user.email.toLowerCase().includes(query.toLowerCase()))
                );
            }
            return;
        }

        // Otherwise, use server-side search
        if (query.length < 2) {
            internalShareSearchResults.value = [];
            return;
        }

        clearTimeout(userSearchTimeout);
        userSearchTimeout = setTimeout(async () => {
            isSearchingUsers.value = true;
            try {
                const response = await fetch(`/api/users/search?q=${encodeURIComponent(query)}`);
                if (!response.ok) {
                    if (response.status === 403) {
                        throw new Error('Internal sharing is not enabled');
                    }
                    throw new Error('Failed to search users');
                }
                const data = await response.json();
                internalShareSearchResults.value = data;
            } catch (error) {
                setGlobalError(`Failed to search users: ${error.message}`);
                internalShareSearchResults.value = [];
            } finally {
                isSearchingUsers.value = false;
            }
        }, 300);
    };

    const openUnifiedShareModal = async (recording) => {
        internalShareRecording.value = recording;
        internalShareUserSearch.value = '';
        internalShareSearchResults.value = [];
        internalSharePermissions.value = { can_edit: false, can_reshare: false };
        recordingPublicShares.value = [];
        shareOptions.share_summary = true;
        shareOptions.share_notes = true;

        // PERMISSION CEILING: Calculate maximum permissions current user can grant
        // If viewing a shared recording (not owner), constrain to their permissions
        if (recording.is_shared && recording.share_info) {
            internalShareMaxPermissions.value = {
                can_edit: recording.share_info.can_edit || false,
                can_reshare: recording.share_info.can_reshare || false
            };
        } else {
            // Owner has unlimited permissions
            internalShareMaxPermissions.value = {
                can_edit: true,
                can_reshare: true
            };
        }

        showUnifiedShareModal.value = true;

        // Load all public shares for this recording
        isLoadingPublicShares.value = true;
        try {
            const response = await fetch(`/api/shares`);
            if (response.ok) {
                const allShares = await response.json();
                // Filter to only shares for this recording and add share_url
                recordingPublicShares.value = allShares
                    .filter(share => share.recording_id === recording.id)
                    .map(share => ({
                        ...share,
                        share_url: `${window.location.origin}/share/${share.public_id}`
                    }));
            }
        } catch (error) {
            console.error('Error loading public shares:', error);
            recordingPublicShares.value = [];
        } finally {
            isLoadingPublicShares.value = false;
        }

        // Load existing internal shares
        isLoadingInternalShares.value = true;
        try {
            const response = await fetch(`/api/recordings/${recording.id}/shares-internal`);
            if (!response.ok) {
                if (response.status === 403) {
                    throw new Error('Internal sharing is not enabled');
                }
                throw new Error('Failed to load shares');
            }
            const data = await response.json();
            recordingInternalShares.value = data.shares || [];
        } catch (error) {
            setGlobalError(`Failed to load shares: ${error.message}`);
            recordingInternalShares.value = [];
        } finally {
            isLoadingInternalShares.value = false;
        }

        // If SHOW_USERNAMES_IN_UI is enabled, load all users
        if (showUsernamesInUI.value) {
            await loadAllUsers();
            internalShareSearchResults.value = allUsers.value;
        }
    };

    const closeUnifiedShareModal = () => {
        showUnifiedShareModal.value = false;
        internalShareRecording.value = null;
        internalShareUserSearch.value = '';
        internalShareSearchResults.value = [];
        recordingInternalShares.value = [];
        recordingPublicShares.value = [];
        allUsers.value = [];
    };

    // Legacy function names for backward compatibility
    const openInternalShareModal = openUnifiedShareModal;
    const openManageInternalSharesModal = openUnifiedShareModal;
    const closeInternalShareModal = closeUnifiedShareModal;
    const closeManageInternalSharesModal = closeUnifiedShareModal;

    const reloadInternalShares = async () => {
        if (!internalShareRecording.value) return;

        isLoadingInternalShares.value = true;
        try {
            const response = await fetch(`/api/recordings/${internalShareRecording.value.id}/shares-internal`);
            if (!response.ok) {
                throw new Error('Failed to load shares');
            }
            const data = await response.json();
            recordingInternalShares.value = data.shares || [];
        } catch (error) {
            setGlobalError(`Failed to reload shares: ${error.message}`);
        } finally {
            isLoadingInternalShares.value = false;
        }
    };

    const shareWithUsername = async () => {
        if (!internalShareRecording.value) return;

        const username = internalShareUserSearch.value.trim();
        if (!username) {
            setGlobalError('Please enter a username');
            return;
        }

        isSearchingUsers.value = true;
        try {
            // Search for the exact username
            const searchResponse = await fetch(`/api/users/search?q=${encodeURIComponent(username)}`);
            if (!searchResponse.ok) {
                if (searchResponse.status === 403) {
                    throw new Error('Internal sharing is not enabled');
                }
                throw new Error('Failed to find user');
            }

            const users = await searchResponse.json();

            if (users.length === 0) {
                setGlobalError(`User "${username}" not found`);
                return;
            }

            // Use the first matching user (should be exact match from backend)
            const user = users[0];
            await createInternalShare(user.id, user.username);

            // Clear input on success
            internalShareUserSearch.value = '';
        } catch (error) {
            setGlobalError(error.message || 'Failed to share with user');
        } finally {
            isSearchingUsers.value = false;
        }
    };

    const createInternalShare = async (userId, username) => {
        if (!internalShareRecording.value) return;

        try {
            const response = await fetch(`/api/recordings/${internalShareRecording.value.id}/share-internal`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user_id: userId,
                    can_edit: internalSharePermissions.value.can_edit,
                    can_reshare: internalSharePermissions.value.can_reshare
                })
            });

            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || 'Failed to share recording');
            }

            const displayName = showUsernamesInUI.value ? username : `User #${userId}`;
            showToast(`Recording shared with ${displayName}`, 'fa-share-alt');

            // Reset permissions for next share
            internalSharePermissions.value = { can_edit: false, can_reshare: false };

            // Reload shares to show the new share in the list
            await reloadInternalShares();

            // Update the recording's share count in the UI
            await refreshRecordingShareCounts();
        } catch (error) {
            setGlobalError(`Failed to share recording: ${error.message}`);
        }
    };

    const revokeInternalShare = async (shareId, username) => {
        if (!internalShareRecording.value) return;

        try {
            const response = await fetch(`/api/internal-shares/${shareId}`, {
                method: 'DELETE'
            });

            if (!response.ok) {
                const data = await response.json();
                throw new Error(data.error || 'Failed to revoke share');
            }

            recordingInternalShares.value = recordingInternalShares.value.filter(s => s.id !== shareId);
            const displayName = showUsernamesInUI.value ? username : 'User';
            showToast(`Access revoked for ${displayName}`, 'fa-user-times');

            // Update the recording's share count in the UI
            await refreshRecordingShareCounts();
        } catch (error) {
            setGlobalError(`Failed to revoke share: ${error.message}`);
        }
    };

    return {
        // Utilities
        formatShareDate,
        getUserColorClass,

        // Public sharing
        openShareModal,
        closeShareModal,
        createShare,
        copyShareLink,
        copyPublicShareLink,
        copyPublicShareLinkWithFeedback,
        copyIndividualShareLink,
        confirmDeletePublicShare,
        deletePublicShare,
        refreshRecordingShareCounts,

        // Shares list
        openSharesList,
        closeSharesList,
        updateShare,
        confirmDeleteShare,
        cancelDeleteShare,
        deleteShare: deletePublicShare, // Alias for template compatibility
        copiedShareId,

        // Internal sharing
        loadAllUsers,
        searchInternalShareUsers,
        openUnifiedShareModal,
        closeUnifiedShareModal,
        openInternalShareModal,
        closeInternalShareModal,
        openManageInternalSharesModal,
        closeManageInternalSharesModal,
        reloadInternalShares,
        shareWithUsername,
        createInternalShare,
        revokeInternalShare
    };
}
