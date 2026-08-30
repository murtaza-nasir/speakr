/**
 * Chat composable
 * Handles AI chat functionality with streaming responses
 */

import { filenameFromContentDisposition } from '../utils/content-disposition.js';

export function useChat(state, utils) {
    const {
        showChat, isChatMaximized, chatMessages, chatInput,
        isChatLoading, chatMessagesRef, chatInputRef, selectedRecording, csrfToken
    } = state;

    const { showToast, setGlobalError, onChatComplete, t } = utils;

    // ------------------------------------------------------------------
    // Timestamp chips: the chat prompt asks the model to cite moments as
    // bracketed transcript timestamps ([00:07:35]); render them as
    // clickable chips that seek the in-view recording's player. Unlike
    // Inquire citations there is no numbering or Sources list — chat is
    // always about the single recording in view.
    // ------------------------------------------------------------------

    const CHAT_TS_RE = /\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]/g;

    const formatChatSeconds = (s) => {
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
        return h > 0
            ? h + ':' + String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0')
            : m + ':' + String(sec).padStart(2, '0');
    };

    /** Replace [h:mm:ss] / [m:ss] in sanitized chat HTML with seek chips. */
    const decorateChatTimestamps = (html) => {
        if (!html || !/\[\d/.test(html)) return html;
        try {
            const tpl = document.createElement('template');
            tpl.innerHTML = html;
            const walker = document.createTreeWalker(tpl.content, NodeFilter.SHOW_TEXT, {
                acceptNode: (node) =>
                    node.parentElement && node.parentElement.closest('code, pre, a')
                        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
            });
            const targets = [];
            let n;
            while ((n = walker.nextNode())) {
                if (CHAT_TS_RE.test(n.nodeValue)) targets.push(n);
                CHAT_TS_RE.lastIndex = 0;
            }
            for (const node of targets) {
                const frag = document.createDocumentFragment();
                let last = 0;
                const text = node.nodeValue;
                for (const m of text.matchAll(CHAT_TS_RE)) {
                    // [a:b] is m:ss; [a:b:c] is h:mm:ss
                    const seconds = m[3] !== undefined
                        ? (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3])
                        : (+m[1]) * 60 + (+m[2]);
                    frag.appendChild(document.createTextNode(text.slice(last, m.index)));
                    const chip = document.createElement('button');
                    chip.type = 'button';
                    chip.className = 'chat-ts-chip';
                    chip.dataset.seek = String(seconds);
                    chip.title = t('chat.playFromHere') || 'Play from here';
                    chip.textContent = formatChatSeconds(seconds);
                    frag.appendChild(chip);
                    last = m.index + m[0].length;
                }
                frag.appendChild(document.createTextNode(text.slice(last)));
                node.parentNode.replaceChild(frag, node);
            }
            return tpl.innerHTML;
        } catch (_) {
            return html;
        }
    };

    const renderChatHtml = (content) => decorateChatTimestamps(window.renderMarkdownSafe(content));

    /** Delegated click handler for the chat messages containers. */
    const handleChatBubbleClick = (event) => {
        const chip = event.target.closest && event.target.closest('.chat-ts-chip');
        if (!chip) return;
        const seconds = parseInt(chip.dataset.seek, 10);
        if (!isFinite(seconds)) return;
        // The detail view's player streams from /audio/; incognito uses a
        // blob URL, so fall back to any media element on the page.
        const all = [...document.querySelectorAll('audio, video')];
        const media = all.find((el) => ((el.currentSrc || el.src || '').includes('/audio/')))
            || all.find((el) => (el.currentSrc || el.src));
        if (!media) return;
        const target = isFinite(media.duration)
            ? Math.min(seconds, Math.max(0, media.duration - 1))
            : seconds;
        media.currentTime = target;
        media.play().catch(() => {});
    };

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

    const focusChatInput = () => {
        Vue.nextTick(() => {
            if (chatInputRef.value) {
                chatInputRef.value.focus();
            }
        });
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
        focusChatInput();

        await Vue.nextTick();
        scrollChatToBottom();

        let assistantMessage = null;

        try {
            const messageHistory = chatMessages.value
                .slice(0, -1)
                .map(msg => ({ role: msg.role, content: msg.content }));

            // Check if this is an incognito recording
            const isIncognito = selectedRecording.value.incognito === true;
            let response;

            if (isIncognito) {
                // Use incognito chat endpoint - pass transcription directly
                response = await fetch('/api/recordings/incognito/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        transcription: selectedRecording.value.transcription,
                        participants: selectedRecording.value.participants || '',
                        notes: selectedRecording.value.notes || '',
                        message: message,
                        message_history: messageHistory
                    })
                });
            } else {
                // Use regular chat endpoint
                response = await fetch('/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        recording_id: selectedRecording.value.id,
                        message: message,
                        message_history: messageHistory
                    })
                });
            }

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
                            // Handle [DONE] marker from incognito endpoint
                            if (jsonStr === '[DONE]') {
                                return;
                            }
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
                                    // Handle both 'delta' (regular) and 'content' (incognito) formats
                                    const textContent = data.delta || data.content;
                                    if (textContent) {
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

                                        assistantMessage.content += textContent;
                                        assistantMessage.html = renderChatHtml(assistantMessage.content);

                                        if (shouldScroll) {
                                            await Vue.nextTick();
                                            scrollChatToBottom();
                                        }
                                    }
                                    if (data.end_of_stream) {
                                        // Provider hit its output-token limit: keep the
                                        // partial answer but tell the user it was cut off
                                        // instead of presenting it as complete (issue #349).
                                        if (data.truncated && assistantMessage && assistantMessage.content) {
                                            assistantMessage.content += `\n\n_⚠️ ${t('chat.responseTruncated')}_`;
                                            assistantMessage.html = renderChatHtml(assistantMessage.content);
                                        }
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
            // Preserve any partial assistant content that was already streamed
            // before the connection dropped (issue #282). Reverse-proxy read
            // timeouts on long-thinking responses used to wipe the visible
            // response entirely; now we keep what arrived and append an error
            // note below it so the user can still copy what they got.
            if (assistantMessage) {
                const partial = (assistantMessage.content || '').trim();
                const errSuffix = `\n\n_⚠️ Connection ended before the response completed: ${error.message}_`;
                if (partial) {
                    assistantMessage.content = partial + errSuffix;
                    assistantMessage.html = renderChatHtml(assistantMessage.content);
                } else {
                    assistantMessage.content = `Error: ${error.message}`;
                    assistantMessage.html = `<span class="text-red-500">Error: ${error.message}</span>`;
                }
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
            focusChatInput();
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
            showToast(t('chat.cleared'), 'fa-broom');
        }
    };

    const downloadChat = async () => {
        if (!selectedRecording.value || chatMessages.value.length === 0) {
            showToast(t('chat.noMessagesToDownload'), 'fa-exclamation-circle');
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
                showToast(error.error || t('chat.downloadFailed'), 'fa-exclamation-circle');
                return;
            }

            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.style.display = 'none';
            a.href = url;

            const contentDisposition = response.headers.get('Content-Disposition');
            a.download = filenameFromContentDisposition(contentDisposition, 'chat.docx');

            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);

            showToast(t('chat.downloadSuccess'));
        } catch (error) {
            console.error('Download failed:', error);
            showToast(t('chat.downloadFailed'), 'fa-exclamation-circle');
        }
    };

    const copyMessage = (text, event) => {
        const button = event.currentTarget;

        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text)
                .then(() => {
                    showToast(t('messages.copiedSuccessfully'));
                    animateCopyButton(button);
                })
                .catch(err => {
                    console.error('Copy failed:', err);
                    showToast(t('messages.copyFailed') + ': ' + err.message, 'fa-exclamation-circle');
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
                showToast(t('messages.copiedSuccessfully'));
                if (button) animateCopyButton(button);
            } else {
                showToast(t('messages.copyNotSupported'), 'fa-exclamation-circle');
            }
        } catch (err) {
            console.error('Fallback copy failed:', err);
            showToast(t('messages.copyFailed') + ': ' + err.message, 'fa-exclamation-circle');
        }
    };

    return {
        isChatScrolledToBottom,
        scrollChatToBottom,
        handleChatBubbleClick,
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
