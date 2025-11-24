/**
 * Chat composable
 * Handles chat/inquire functionality with streaming responses
 */

import { ref, reactive, nextTick } from 'vue';

export function useChat() {
    // State
    const chatMessages = ref([]);
    const chatInput = ref('');
    const isChatLoading = ref(false);
    const chatMessagesRef = ref(null);
    const isChatExpanded = ref(false);

    // Methods
    const isChatScrolledToBottom = () => {
        if (!chatMessagesRef.value) return true;
        const { scrollTop, scrollHeight, clientHeight } = chatMessagesRef.value;
        const scrollableHeight = scrollHeight - clientHeight;
        if (scrollableHeight <= 0) return true;
        const scrollPercentage = scrollTop / scrollableHeight;
        return scrollPercentage >= 0.95; // Within bottom 5%
    };

    const scrollChatToBottom = () => {
        if (chatMessagesRef.value) {
            requestAnimationFrame(() => {
                if (chatMessagesRef.value) {
                    chatMessagesRef.value.scrollTop = chatMessagesRef.value.scrollHeight;
                }
            });
        }
    };

    const sendMessage = async (recordingId) => {
        if (!chatInput.value.trim() || isChatLoading.value) {
            return;
        }

        const message = chatInput.value.trim();

        if (!Array.isArray(chatMessages.value)) {
            chatMessages.value = [];
        }

        chatMessages.value.push({ role: 'user', content: message });
        chatInput.value = '';
        isChatLoading.value = true;

        await nextTick();
        scrollChatToBottom();

        let assistantMessage = null;

        try {
            const messageHistory = chatMessages.value
                .slice(0, -1)
                .map(msg => ({ role: msg.role, content: msg.content }));

            const response = await fetch('/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    recording_id: recordingId,
                    message: message,
                    message_history: messageHistory
                })
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.error || 'Failed to get chat response');
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            const processStream = async () => {
                let isFirstChunk = true;
                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;

                    buffer += decoder.decode(value, { stream: true });
                    const lines = buffer.split('\n');
                    buffer = lines.pop();

                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            const jsonStr = line.substring(6);
                            if (jsonStr) {
                                try {
                                    const data = JSON.parse(jsonStr);

                                    if (data.thinking) {
                                        const shouldScroll = isChatScrolledToBottom();

                                        if (isFirstChunk) {
                                            isChatLoading.value = false;
                                            assistantMessage = reactive({
                                                role: 'assistant',
                                                content: '',
                                                html: '',
                                                thinking: data.thinking,
                                                thinkingExpanded: false
                                            });
                                            chatMessages.value.push(assistantMessage);
                                            isFirstChunk = false;
                                        } else if (assistantMessage) {
                                            if (assistantMessage.thinking) {
                                                assistantMessage.thinking += '\n\n' + data.thinking;
                                            } else {
                                                assistantMessage.thinking = data.thinking;
                                            }
                                        }

                                        if (shouldScroll) {
                                            await nextTick();
                                            scrollChatToBottom();
                                        }
                                    }

                                    if (data.delta) {
                                        const shouldScroll = isChatScrolledToBottom();

                                        if (isFirstChunk) {
                                            isChatLoading.value = false;
                                            assistantMessage = reactive({
                                                role: 'assistant',
                                                content: '',
                                                html: '',
                                                thinking: '',
                                                thinkingExpanded: false
                                            });
                                            chatMessages.value.push(assistantMessage);
                                            isFirstChunk = false;
                                        }

                                        assistantMessage.content += data.delta;
                                        if (window.marked) {
                                            assistantMessage.html = window.marked.parse(assistantMessage.content);
                                        } else {
                                            assistantMessage.html = assistantMessage.content;
                                        }

                                        if (shouldScroll) {
                                            await nextTick();
                                            scrollChatToBottom();
                                        }
                                    }

                                    if (data.end_of_stream) {
                                        return;
                                    }

                                    if (data.error) {
                                        throw new Error(data.error);
                                    }
                                } catch (e) {
                                    console.error('Error parsing stream data:', e);
                                }
                            }
                        }
                    }
                }
            };

            await processStream();

        } catch (error) {
            console.error('Chat Error:', error);
            if (assistantMessage) {
                assistantMessage.content = `Error: ${error.message}`;
                assistantMessage.html = `<span class="text-red-500">Error: ${error.message}</span>`;
            } else {
                chatMessages.value.push({
                    role: 'assistant',
                    content: `Error: ${error.message}`,
                    html: `<span class="text-red-500">Error: ${error.message}</span>`
                });
            }
        } finally {
            isChatLoading.value = false;
            await nextTick();
            if (isChatScrolledToBottom()) {
                scrollChatToBottom();
            }
        }
    };

    const clearChat = () => {
        chatMessages.value = [];
        chatInput.value = '';
        isChatLoading.value = false;
    };

    const toggleThinking = (message) => {
        if (message.thinking) {
            message.thinkingExpanded = !message.thinkingExpanded;
        }
    };

    const setChatRef = (el) => {
        chatMessagesRef.value = el;
    };

    const handleChatInput = (event) => {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            // Trigger send message (caller should provide recordingId)
            return true;
        }
        return false;
    };

    return {
        // State
        chatMessages,
        chatInput,
        isChatLoading,
        chatMessagesRef,
        isChatExpanded,

        // Methods
        sendMessage,
        clearChat,
        toggleThinking,
        setChatRef,
        scrollChatToBottom,
        handleChatInput
    };
}
