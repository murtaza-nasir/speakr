/**
 * Sharing composable
 * Handles public and internal sharing of recordings
 */

const { ref } = Vue;

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
    const t = (key, params = {}) => (utils.t ? utils.t(key, params) : key);

    let userSearchTimeout = null;

    // Shared Transcripts list (#416): what the user shared (public links,
    // people and groups) and what others shared with them.
    const sharesTab = ref('by_me');
    const sharesByMe = ref([]);
    const sharesWithMe = ref([]);
    const sharesPublicEnabled = ref(true);
    const sharesInternalEnabled = ref(false);

    // Helper function to format share dates
    const formatShareDate = (dateString) => {
        if (!dateString) return t('sharing.unknownDate');

        try {
            // Backend sends naive UTC (isoformat with no zone). new Date() parses
            // a zoneless datetime as LOCAL, which shows the raw UTC clock value
            // instead of converting it. Append 'Z' when there's no zone so it's
            // parsed as UTC; the toLocale* calls below then render it in the
            // viewer's own timezone.
            let normalized = dateString;
            if (typeof normalized === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(normalized) && !/(?:Z|[+-]\d{2}:?\d{2})$/.test(normalized)) {
                normalized = normalized.replace(' ', 'T') + 'Z';
            }
            const date = new Date(normalized);
            const now = new Date();
            const diffMs = now - date;
            const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

            // The interface locale picks the words and the 12/24-hour clock (#480).
            const locale = window.i18n?.getIntlLocale?.();
            const time = () => date.toLocaleTimeString(locale, { hour: 'numeric', minute: '2-digit' });

            // If today
            if (diffDays === 0) {
                return t('sidebar.today') + ', ' + time();
            }
            // If yesterday
            else if (diffDays === 1) {
                return t('sidebar.yesterday') + ', ' + time();
            }
            // If within last week
            else if (diffDays < 7) {
                return date.toLocaleDateString(locale, { weekday: 'long', hour: 'numeric', minute: '2-digit' });
            }
            // Otherwise show full date
            else {
                return date.toLocaleDateString(locale, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
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

        // Map to color classes 1-16
        const colorNum = (Math.abs(hash) % 16) + 1;
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
            if (!response.ok) throw new Error(data.error || t('sharing.createLinkFailed'));

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
                showToast(t('sharing.usingExistingLink'), 'fa-link');
            } else {
                showToast(t('sharing.linkCreated'), 'fa-check-circle');
            }
        } catch (error) {
            setGlobalError(t('sharing.createLinkFailedWithError', { error: error.message }));
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
            if (!response.ok) throw new Error(data.error || t('sharing.deleteLinkFailed'));

            // Remove from the shares list (both modals use different arrays)
            recordingPublicShares.value = recordingPublicShares.value.filter(s => s.id !== shareId);
            userShares.value = userShares.value.filter(s => s.id !== shareId);

            // Update the recording's share count in the UI
            await refreshRecordingShareCounts();

            showToast(t('sharing.linkDeleted'), 'fa-check-circle');
            showShareDeleteModal.value = false;
            shareToDelete.value = null;
        } catch (error) {
            setGlobalError(t('sharing.deleteLinkFailedWithError', { error: error.message }));
        }
    };

    const copyPublicShareLink = (shareUrl) => {
        navigator.clipboard.writeText(shareUrl).then(() => {
            showToast(t('sharing.linkCopied'), 'fa-check-circle');
        }).catch(() => {
            setGlobalError(t('sharing.copyLinkFailed'));
        });
    };

    const copyPublicShareLinkWithFeedback = (shareUrl, shareId) => {
        navigator.clipboard.writeText(shareUrl).then(() => {
            copiedShareId.value = shareId;
            showToast(t('sharing.linkCopied'), 'fa-check-circle');

            // Reset after delay
            setTimeout(() => {
                copiedShareId.value = null;
            }, 1500);
        }).catch(() => {
            setGlobalError(t('sharing.copyLinkFailed'));
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
            showToast(t('sharing.linkCopied'));
        });
    };

    const copyIndividualShareLink = (shareId) => {
        const input = document.getElementById(`share-link-${shareId}`);
        if (!input) return;

        const button = input.nextElementSibling;
        if (!button) return;

        navigator.clipboard.writeText(input.value).then(() => {
            copiedShareId.value = shareId;
            showToast(t('sharing.linkCopied'), 'fa-check');

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
            const response = await fetch('/api/shares/overview');
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || t('sharing.loadSharedItemsFailed'));
            userShares.value = data.public_links || [];
            sharesByMe.value = (data.shared_by_me || []).map(r => ({
                ...r, recipients: r.recipients.map(p => ({ ...p, confirming: false })),
            }));
            sharesWithMe.value = data.shared_with_me || [];
            sharesPublicEnabled.value = data.public_sharing_enabled !== false;
            sharesInternalEnabled.value = !!data.internal_sharing_enabled;
        } catch (error) {
            setGlobalError(t('sharing.loadSharedItemsFailedWithError', { error: error.message }));
        } finally {
            isLoadingShares.value = false;
        }
    };

    const closeSharesList = () => {
        showSharesListModal.value = false;
        userShares.value = [];
        sharesByMe.value = [];
        sharesWithMe.value = [];
    };

    // Revoke one person's access from the list. The first click asks, the
    // second revokes; a group share follows its tag or folder and has no
    // button here.
    const revokeShareFromList = async (entry, recipient) => {
        if (!recipient.confirming) {
            recipient.confirming = true;
            return;
        }
        try {
            const response = await fetch(`/api/internal-shares/${recipient.share_id}`, { method: 'DELETE' });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.error || t('sharing.revokeFailed'));
            entry.recipients = entry.recipients.filter(p => p.share_id !== recipient.share_id);
            if (!entry.recipients.length) {
                sharesByMe.value = sharesByMe.value.filter(r => r !== entry);
            }
            showToast(t('sharedTranscripts.accessRevoked'), 'fa-user-times');
            await refreshRecordingShareCounts();
        } catch (error) {
            recipient.confirming = false;
            setGlobalError(t('sharing.revokeFailedWithError', { error: error.message }));
        }
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
            if (!response.ok) throw new Error(data.error || t('sharing.updateFailed'));
            showToast(t('sharing.permissionsUpdated'), 'fa-check-circle');
        } catch (error) {
            setGlobalError(t('sharing.updateFailedWithError', { error: error.message }));
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
                    throw new Error(t('sharing.internalSharingDisabled'));
                }
                throw new Error(t('sharing.loadUsersFailed'));
            }
            const data = await response.json();
            allUsers.value = data;
        } catch (error) {
            setGlobalError(t('sharing.loadUsersFailedWithError', { error: error.message }));
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
                        throw new Error(t('sharing.internalSharingDisabled'));
                    }
                    throw new Error(t('sharing.searchUsersFailed'));
                }
                const data = await response.json();
                internalShareSearchResults.value = data;
            } catch (error) {
                setGlobalError(t('sharing.searchUsersFailedWithError', { error: error.message }));
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
                    throw new Error(t('sharing.internalSharingDisabled'));
                }
                throw new Error(t('sharing.loadSharesFailed'));
            }
            const data = await response.json();
            recordingInternalShares.value = data.shares || [];
        } catch (error) {
            setGlobalError(t('sharing.loadSharesFailedWithError', { error: error.message }));
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
                throw new Error(t('sharing.loadSharesFailed'));
            }
            const data = await response.json();
            recordingInternalShares.value = data.shares || [];
        } catch (error) {
            setGlobalError(t('sharing.reloadSharesFailedWithError', { error: error.message }));
        } finally {
            isLoadingInternalShares.value = false;
        }
    };

    const shareWithUsername = async () => {
        if (!internalShareRecording.value) return;

        const username = internalShareUserSearch.value.trim();
        if (!username) {
            setGlobalError(t('sharing.enterUsername'));
            return;
        }

        isSearchingUsers.value = true;
        try {
            // Search for the exact username
            const searchResponse = await fetch(`/api/users/search?q=${encodeURIComponent(username)}`);
            if (!searchResponse.ok) {
                if (searchResponse.status === 403) {
                    throw new Error(t('sharing.internalSharingDisabled'));
                }
                throw new Error(t('sharing.findUserFailed'));
            }

            const users = await searchResponse.json();

            if (users.length === 0) {
                setGlobalError(t('sharing.userNotFound', { username }));
                return;
            }

            // Use the first matching user (should be exact match from backend)
            const user = users[0];
            await createInternalShare(user.id, user.username);

            // Clear input on success
            internalShareUserSearch.value = '';
        } catch (error) {
            setGlobalError(error.message || t('sharing.shareWithUserFailed'));
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
                throw new Error(data.error || t('sharing.shareRecordingFailed'));
            }

            const displayName = showUsernamesInUI.value ? username : t('sharing.userNumber', { id: userId });
            showToast(t('sharing.sharedWithUser', { name: displayName }), 'fa-share-alt');

            // Reset permissions for next share
            internalSharePermissions.value = { can_edit: false, can_reshare: false };

            // Reload shares to show the new share in the list
            await reloadInternalShares();

            // Update the recording's share count in the UI
            await refreshRecordingShareCounts();
        } catch (error) {
            setGlobalError(t('sharing.shareRecordingFailedWithError', { error: error.message }));
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
                throw new Error(data.error || t('sharing.revokeFailed'));
            }

            recordingInternalShares.value = recordingInternalShares.value.filter(s => s.id !== shareId);
            const displayName = showUsernamesInUI.value ? username : t('sharing.userGeneric');
            showToast(t('sharing.accessRevokedFor', { name: displayName }), 'fa-user-times');

            // Update the recording's share count in the UI
            await refreshRecordingShareCounts();
        } catch (error) {
            setGlobalError(t('sharing.revokeFailedWithError', { error: error.message }));
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
        sharesTab,
        sharesByMe,
        sharesWithMe,
        sharesPublicEnabled,
        sharesInternalEnabled,
        revokeShareFromList,
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
