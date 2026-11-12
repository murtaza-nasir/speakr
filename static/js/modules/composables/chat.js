/**
 * Chat composable: the recording's Ambrose chat in the main app (the
 * floating panel and the mobile chat tab).
 *
 * The chat itself is the shared AssistantChat (modules/assistant/chat.js,
 * templates/components/assistant/chat.html), also used by the Ask page.
 * This module says how a recording question is sent (POST /chat, or the
 * incognito route with the transcript in the request), offers the "Search
 * in" filters when the library tools are on (window.speakrAssistantLibrary,
 * never for incognito), and keeps the Word downloads.
 */

import { filenameFromContentDisposition } from '../utils/content-disposition.js';
import { responseWithQuestion } from '../utils/chat-turn.js';
import { useAssistantChat } from '../assistant/chat.js';
import { useSearchIn } from '../assistant/search-in.js';

export function useChat(state, utils) {
    const {
        showChat, isChatMaximized, chatMessages, chatInput,
        isChatLoading, chatMessagesRef, chatInputRef, selectedRecording
    } = state;

    const { showToast, onChatComplete, t, processedTranscription } = utils;

    const isIncognito = () => !!(selectedRecording.value && selectedRecording.value.incognito === true);
    const libraryTools = () => window.speakrAssistantLibrary === true;

    // The "Search in" filters: the same state as the Ask page's filter
    // column, here in a popover; only when the library tools are on.
    const searchIn = libraryTools() ? useSearchIn({ t }) : null;

    const request = ({ message, history, signal }) => {
        const recording = selectedRecording.value;
        if (isIncognito()) {
            // An incognito recording exists only in the browser: the
            // transcript travels with the question. No tools, no filters.
            return fetch('/api/recordings/incognito/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                signal,
                body: JSON.stringify({
                    transcription: recording.transcription,
                    participants: recording.participants || '',
                    notes: recording.notes || '',
                    message,
                    message_history: history.map((m) => ({ role: m.role, content: m.content })),
                }),
            });
        }
        return fetch('/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            signal,
            body: JSON.stringify(Object.assign({
                recording_id: recording.id,
                message,
                message_history: history,
            }, searchIn ? searchIn.fields() : {})),
        });
    };

    const assistant = useAssistantChat({
        messages: chatMessages, input: chatInput, loading: isChatLoading,
        scroller: chatMessagesRef, inputEl: chatInputRef,
        t, request, timestamps: true, toast: showToast,
        openRecordingId: () => (selectedRecording.value && !isIncognito() ? selectedRecording.value.id : null),
        isDisabled: () => !selectedRecording.value || selectedRecording.value.status !== 'COMPLETED' ||
            !!(processedTranscription && processedTranscription.value && processedTranscription.value.isError),
        showSearchIn: () => !!searchIn && !isIncognito(),
        onDone: () => { if (onChatComplete) onChatComplete(); },
    });

    const isChatScrolledToBottom = () => assistant.following;
    const scrollChatToBottom = () => assistant.scrollToBottom();
    const handleChatBubbleClick = (event) => assistant.onTimelineClick(event);
    const chatMessageText = (message) => assistant.messageText(message);
    const sendChatMessage = () => assistant.send();
    const handleChatKeydown = (event) => assistant.onKeydown(event);

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

    const clearChat = () => {
        if (chatMessages.value.length > 0) {
            assistant.stop();
            chatMessages.value = [];
            showToast(t('chat.cleared'), 'fa-broom');
        }
    };

    const downloadChat = async () => {
        if (!selectedRecording.value || chatMessages.value.length === 0) {
            showToast(t('chat.noMessagesToDownload'), 'fa-exclamation-circle');
            return;
        }
        await postChatDownload(chatMessages.value.map((m) => ({ ...m, content: chatMessageText(m) })));
    };

    // One response and the question before it, as chat-<title>-response.docx.
    const downloadChatResponse = async (index) => {
        const messages = responseWithQuestion(chatMessages.value, index);
        if (!selectedRecording.value || messages.length === 0) {
            showToast(t('chat.noMessagesToDownload'), 'fa-exclamation-circle');
            return;
        }
        await postChatDownload(messages.map((m) => ({ ...m, content: chatMessageText(m) })), 'response');
    };

    // scope 'response' marks a one-response download; it only changes the filename.
    const postChatDownload = async (messages, scope) => {
        try {
            const csrfTokenValue = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(`/recording/${selectedRecording.value.id}/download/chat`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfTokenValue
                },
                body: JSON.stringify(scope ? { messages, scope } : { messages })
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
            a.download = filenameFromContentDisposition(contentDisposition, scope ? 'chat-response.docx' : 'chat.docx');

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

    // Copy any text with the chat's feedback (other views use this too).
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
        assistant,
        searchIn,
        isChatScrolledToBottom,
        scrollChatToBottom,
        handleChatBubbleClick,
        chatMessageText,
        toggleChatMaximize,
        sendChatMessage,
        handleChatKeydown,
        clearChat,
        downloadChat,
        downloadChatResponse,
        copyMessage,
        animateCopyButton,
        fallbackCopyTextToClipboard
    };
}
