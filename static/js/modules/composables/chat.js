/**
 * Chat composable
 * Handles AI chat functionality with streaming responses
 */

export function useChat(state, utils) {
    const {
        showChat, isChatMaximized, chatMessages, chatInput,
        isChatLoading, chatMessagesRef, selectedRecording, csrfToken
    } = state;

    const { showToast, setGlobalError, onChatComplete, t } = utils;

    // Helper function to check if chat is scrolled to bottom (within bottom 5%)
    const isChatScrolledToBottom = () => {
        if (!chatMessagesRef.value) return true;
        const { scrollTop, scrollHeight, clientHeight } = chatMessagesRef.value;
        const scrollableHeight = scrollHeight - clientHeight;
        if (scrollableHeight <= 0) return true;
        const scrollPercentage = scrollTop / scrollableHeight;
        return scrollPercentage >= 0.95;
    };

    // Helper function to scroll chat to bottom
    const scrollChatToBottom = () => {
        if (chatMessagesRef.value) {
            requestAnimationFrame(() => {
                if (chatMessagesRef.value) {
                    chatMessagesRef.value.scrollTop = chatMessagesRef.value.scrollHeight;
                }
            });
        }
    };

    const toggleChatMaximize = () => {
        if (isChatMaximized.value) {
            isChatMaximized.value = false;
        } else {
            isChatMaximized.value = true;
            if (!showChat.value) {
                showChat.value = true;
            }
        }
    };

    const sendChatMessage = async () => {
        if (!chatInput.value.trim() || isChatLoading.value || !selectedRecording.value || selectedRecording.value.status !== 'COMPLETED') {
            return;
        }

        const message = chatInput.value.trim();

        if (!Array.isArray(chatMessages.value)) {
            chatMessages.value = [];
        }

        chatMessages.value.push({ role: 'user', content: message });
        chatInput.value = '';
        isChatLoading.value = true;

        await Vue.nextTick();
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
                    recording_id: selectedRecording.value.id,
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
                                            assistantMessage = Vue.reactive({
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
                                            await Vue.nextTick();
                                            scrollChatToBottom();
                                        }
                                    }
                                    if (data.delta) {
                                        const shouldScroll = isChatScrolledToBottom();

                                        if (isFirstChunk) {
                                            isChatLoading.value = false;
                                            assistantMessage = Vue.reactive({
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
                                        assistantMessage.html = marked.parse(assistantMessage.content);

                                        if (shouldScroll) {
                                            await Vue.nextTick();
                                            scrollChatToBottom();
                                        }
                                    }
                                    if (data.end_of_stream) {
                                        return;
                                    }
                                    if (data.error) {
                                        if (data.budget_exceeded) {
                                            throw new Error(t('adminDashboard.tokenBudgetExceeded'));
                                        }
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
            await Vue.nextTick();
            if (isChatScrolledToBottom()) {
                scrollChatToBottom();
            }
            // Refresh token budget after chat completion
            if (onChatComplete) {
                onChatComplete();
            }
        }
    };

    const handleChatKeydown = (event) => {
        if (event.key === 'Enter') {
            if (event.ctrlKey || event.shiftKey) {
                return;
            } else {
                event.preventDefault();
                sendChatMessage();
            }
        }
    };

    const clearChat = () => {
        if (chatMessages.value.length > 0) {
            chatMessages.value = [];
            showToast('Chat cleared', 'fa-broom');
        }
    };

    const downloadChat = async () => {
        if (!selectedRecording.value || chatMessages.value.length === 0) {
            showToast('No chat messages to download.', 'fa-exclamation-circle');
            return;
        }

        try {
            const csrfTokenValue = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${selectedRecording.value.id}/download/chat`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfTokenValue
                },
                body: JSON.stringify({
                    messages: chatMessages.value
                })
            });

            if (!response.ok) {
                const error = await response.json();
                showToast(error.error || 'Failed to download chat', 'fa-exclamation-circle');
                return;
            }

            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;

            const contentDisposition = response.headers.get('Content-Disposition');
            let filename = 'chat.docx';
            if (contentDisposition) {
                const utf8Match = /filename\*=utf-8''(.+)/.exec(contentDisposition);
                if (utf8Match) {
                    filename = decodeURIComponent(utf8Match[1]);
                } else {
                    const regularMatch = /filename="(.+)"/.exec(contentDisposition);
                    if (regularMatch) {
                        filename = regularMatch[1];
                    }
                }
            }
            a.download = filename;

            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            showToast('Chat downloaded successfully!');
        } catch (error) {
            console.error('Download failed:', error);
            showToast('Failed to download chat', 'fa-exclamation-circle');
        }
    };

    const copyMessage = (text, event) => {
        const button = event.currentTarget;

        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text)
                .then(() => {
                    showToast('Copied to clipboard!');
                    animateCopyButton(button);
                })
                .catch(err => {
                    console.error('Copy failed:', err);
                    showToast('Failed to copy: ' + err.message, 'fa-exclamation-circle');
                    fallbackCopyTextToClipboard(text, button);
                });
        } else {
            fallbackCopyTextToClipboard(text, button);
        }
    };

    const animateCopyButton = (button) => {
        button.classList.add('copy-success');
        const originalContent = button.innerHTML;
        button.innerHTML = '<i class="fas fa-check"></i>';
        setTimeout(() => {
            button.classList.remove('copy-success');
            button.innerHTML = originalContent;
        }, 1500);
    };

    const fallbackCopyTextToClipboard = (text, button = null) => {
        try {
            const textArea = document.createElement("textarea");
            textArea.value = text;
            textArea.style.position = "fixed";
            textArea.style.left = "-999999px";
            textArea.style.top = "-999999px";
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            const successful = document.execCommand('copy');
            document.body.removeChild(textArea);

            if (successful) {
                showToast('Copied to clipboard!');
                if (button) animateCopyButton(button);
            } else {
                showToast('Copy failed. Your browser may not support this feature.', 'fa-exclamation-circle');
            }
        } catch (err) {
            console.error('Fallback copy failed:', err);
            showToast('Unable to copy: ' + err.message, 'fa-exclamation-circle');
        }
    };

    return {
        isChatScrolledToBottom,
        scrollChatToBottom,
        toggleChatMaximize,
        sendChatMessage,
        handleChatKeydown,
        clearChat,
        downloadChat,
        copyMessage,
        animateCopyButton,
        fallbackCopyTextToClipboard
    };
}
