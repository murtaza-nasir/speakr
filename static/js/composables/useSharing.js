/**
 * Sharing composable
 * Handles public and internal sharing functionality
 */

import { ref } from 'vue';
import { apiRequest } from '../utils/apiClient.js';

export function useSharing() {
    // State
    const showShareModal = ref(false);
    const showInternalShareModal = ref(false);
    const shareUrl = ref('');
    const shareSettings = ref({
        shareSummary: true,
        shareNotes: true
    });
    const internalShareSettings = ref({
        userId: null,
        canEdit: false,
        canReshare: false
    });
    const isLoadingShare = ref(false);
    const shareError = ref(null);

    // Methods
    const openShareModal = async (recording) => {
        showShareModal.value = true;
        shareError.value = null;
        isLoadingShare.value = true;

        try {
            const data = await apiRequest(`/api/recording/${recording.id}/share`);

            if (data.exists) {
                shareUrl.value = data.share_url;
                shareSettings.value = {
                    shareSummary: data.share.share_summary,
                    shareNotes: data.share.share_notes
                };
            } else {
                shareUrl.value = '';
            }
        } catch (error) {
            shareError.value = error.message;
        } finally {
            isLoadingShare.value = false;
        }
    };

    const createShare = async (recordingId) => {
        isLoadingShare.value = true;
        shareError.value = null;

        try {
            const data = await apiRequest(`/api/recording/${recordingId}/share`, {
                method: 'POST',
                body: JSON.stringify(shareSettings.value)
            });

            shareUrl.value = data.share_url;
            return data;
        } catch (error) {
            shareError.value = error.message;
            throw error;
        } finally {
            isLoadingShare.value = false;
        }
    };

    const updateShare = async (shareId) => {
        isLoadingShare.value = true;
        shareError.value = null;

        try {
            const data = await apiRequest(`/api/share/${shareId}`, {
                method: 'PUT',
                body: JSON.stringify(shareSettings.value)
            });

            return data;
        } catch (error) {
            shareError.value = error.message;
            throw error;
        } finally {
            isLoadingShare.value = false;
        }
    };

    const deleteShare = async (shareId) => {
        isLoadingShare.value = true;
        shareError.value = null;

        try {
            await apiRequest(`/api/share/${shareId}`, {
                method: 'DELETE'
            });

            shareUrl.value = '';
        } catch (error) {
            shareError.value = error.message;
            throw error;
        } finally {
            isLoadingShare.value = false;
        }
    };

    const copyShareUrl = async () => {
        try {
            await navigator.clipboard.writeText(shareUrl.value);
            return true;
        } catch (error) {
            console.error('Failed to copy:', error);
            return false;
        }
    };

    const openInternalShareModal = (recording) => {
        showInternalShareModal.value = true;
        shareError.value = null;
        internalShareSettings.value = {
            userId: null,
            canEdit: false,
            canReshare: false
        };
    };

    const shareInternally = async (recordingId) => {
        isLoadingShare.value = true;
        shareError.value = null;

        try {
            const data = await apiRequest(`/api/recordings/${recordingId}/share-internal`, {
                method: 'POST',
                body: JSON.stringify({
                    user_id: internalShareSettings.value.userId,
                    can_edit: internalShareSettings.value.canEdit,
                    can_reshare: internalShareSettings.value.canReshare
                })
            });

            return data;
        } catch (error) {
            shareError.value = error.message;
            throw error;
        } finally {
            isLoadingShare.value = false;
        }
    };

    const revokeInternalShare = async (shareId) => {
        isLoadingShare.value = true;
        shareError.value = null;

        try {
            await apiRequest(`/api/internal-shares/${shareId}`, {
                method: 'DELETE'
            });
        } catch (error) {
            shareError.value = error.message;
            throw error;
        } finally {
            isLoadingShare.value = false;
        }
    };

    const closeShareModal = () => {
        showShareModal.value = false;
        showInternalShareModal.value = false;
        shareUrl.value = '';
        shareError.value = null;
    };

    return {
        // State
        showShareModal,
        showInternalShareModal,
        shareUrl,
        shareSettings,
        internalShareSettings,
        isLoadingShare,
        shareError,

        // Methods
        openShareModal,
        createShare,
        updateShare,
        deleteShare,
        copyShareUrl,
        openInternalShareModal,
        shareInternally,
        revokeInternalShare,
        closeShareModal
    };
}
