/**
 * Chat composable: the recording's Ambrose chat in the main app (the
 * floating panel and the mobile chat tab).
 *
 * The chat itself is the shared AssistantChat (modules/assistant/chat.js,
 * templates/components/assistant/chat.html), also used by the Ask page.
 * This module says how a recording question is sent (POST /chat, or the
 * incognito route with the transcript in the request) and keeps the Word
 * downloads. With the library tools on, the server lets Ambrose search every
 * recording the user can access; the panel sends no filters.
 *
 * With `state.chatThreads` (chat-threads.js), each recording has its own
 * thread: the shared chat shows the open recording's thread, an answer keeps
 * arriving in the thread it started in, and a toast says when one finishes
 * while another recording is open. chatMessages, chatInput and isChatLoading
 * are then the open thread's values.
 */

import { filenameFromContentDisposition } from '../utils/content-disposition.js';
import { responseWithQuestion } from '../utils/chat-turn.js';
import { useAssistantChat } from '../assistant/chat.js';

export function useChat(state, utils) {
    const {
        showChat, isChatMaximized, chatMessages, chatInput,
        isChatLoading, chatMessagesRef, chatInputRef, selectedRecording
    } = state;

    const { showToast, onChatComplete, t, processedTranscription } = utils;
    const threads = state.chatThreads || null;

    const escapeHtml = (text) => String(text == null ? '' : text).replace(/[&<>"']/g,
        (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    // An answer finished in a recording that is no longer open: say where,
    // and open it on a click.
    const announce = (thread) => {
        if (!threads || !thread || thread.dropped || thread.incognito || threads.isCurrent(thread)) return;
        const list = thread.messages || [];
        const last = list[list.length - 1];
        if (!last || last.role !== 'assistant') return;
        const listed = (state.recordings && state.recordings.value || []).find((r) => String(r.id) === String(thread.recordingId));
        const title = (listed && listed.title) || thread.title || t('common.untitled');
        const params = { name: escapeHtml(utils.assistantName || 'Ambrose'), title: escapeHtml(title) };
        const open = utils.openRecordingById ? () => utils.openRecordingById(thread.recordingId) : null;
        if (last.failed) showToast(t('chat.answerFailedIn', params), 'fa-exclamation-circle', 6000, 'info', open);
        else showToast(t('chat.answeredIn', params), 'fa-comment-dots', 6000, 'info', open);
    };

    const isIncognito = () => !!(selectedRecording.value && selectedRecording.value.incognito === true);

    const request = ({ message, history, signal }) => {
        const recording = selectedRecording.value;
        if (isIncognito()) {
            // An incognito recording exists only in the browser: the
            // transcript travels with the question. No tools.
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
            body: JSON.stringify({
                recording_id: recording.id,
                message,
                message_history: history,
            }),
        });
    };

    const assistant = useAssistantChat({
        messages: chatMessages, input: chatInput, loading: isChatLoading,
        scroller: chatMessagesRef, inputEl: chatInputRef,
        t, request, timestamps: true, toast: showToast,
        thread: threads ? () => threads.current() : undefined,
        // The recording a turn is about: its thread's, else the open one.
        openRecordingId: (thread) => {
            if (threads && thread) return thread.recordingId;
            return selectedRecording.value && !isIncognito() ? selectedRecording.value.id : null;
        },
        isDisabled: () => !selectedRecording.value || selectedRecording.value.status !== 'COMPLETED' ||
            !!(processedTranscription && processedTranscription.value && processedTranscription.value.isError),
        onDone: (thread) => {
            if (onChatComplete) onChatComplete();
            if (threads) { threads.save(); announce(thread); }
        },
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

    // Clear empties the open recording's thread only.
    const clearChat = () => {
        if (chatMessages.value.length > 0) {
            if (threads) threads.clearCurrent();
            else { assistant.stop(); chatMessages.value = []; }
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

    // The answering mark on a recording's row in the list.
    const chatAnsweringElsewhere = (id) => !!(threads && threads.isAnsweringElsewhere(id));

    return {
        assistant,
        chatAnsweringElsewhere,
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
