/**
 * Chat state management
 */

export function createChatState(ref) {
    const showChat = ref(false);
    const isChatMaximized = ref(false);
    const chatMessages = ref([]);
    const chatInput = ref('');
    const isChatLoading = ref(false);
    const chatMessagesRef = ref(null);
    const chatInputRef = ref(null);

    return {
        showChat,
        isChatMaximized,
        chatMessages,
        chatInput,
        isChatLoading,
        chatMessagesRef,
        chatInputRef
    };
}
