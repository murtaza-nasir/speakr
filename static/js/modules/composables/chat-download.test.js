/**
 * The recording chat's Word downloads in chat.js: the whole chat, and one
 * response with the question before it ("scope": "response").
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useChat } from './chat.js';

const question = { role: 'user', content: 'What was decided?' };
const answer = { role: 'assistant', content: 'The **budget**.', thinking: 'reasoning' };
const followUp = { role: 'user', content: 'And the date?' };
const second = { role: 'assistant', content: 'Friday.', thinking: '' };

let anchor;
let toasts;

const setup = (messages, disposition = null) => {
    toasts = [];
    anchor = { style: {}, click: vi.fn() };
    vi.stubGlobal('document', {
        querySelector: () => ({ getAttribute: () => 'csrf-token' }),
        createElement: () => anchor,
        body: { appendChild: vi.fn(), removeChild: vi.fn() },
    });
    vi.stubGlobal('window', { URL: { createObjectURL: () => 'blob:x', revokeObjectURL: vi.fn() } });
    vi.stubGlobal('fetch', vi.fn(async () => ({
        ok: true,
        blob: async () => ({}),
        headers: { get: () => disposition },
    })));
    const state = {
        showChat: { value: true }, isChatMaximized: { value: false },
        chatMessages: { value: messages }, chatInput: { value: '' },
        isChatLoading: { value: false }, chatMessagesRef: { value: null },
        chatInputRef: { value: null }, selectedRecording: { value: { id: 7 } },
        csrfToken: { value: 'csrf-token' },
    };
    const utils = {
        showToast: (message) => toasts.push(message),
        setGlobalError: vi.fn(), onChatComplete: vi.fn(), t: (key) => key,
    };
    return useChat(state, utils);
};

const sentBody = () => JSON.parse(fetch.mock.calls[0][1].body);

describe('recording chat downloads', () => {
    beforeEach(() => vi.unstubAllGlobals());
    afterEach(() => vi.unstubAllGlobals());

    it('sends the whole chat without a scope', async () => {
        const chat = setup([question, answer, followUp, second]);
        await chat.downloadChat();
        expect(fetch.mock.calls[0][0]).toBe('/recording/7/download/chat');
        expect(sentBody()).toEqual({ messages: [question, answer, followUp, second] });
        expect(anchor.download).toBe('chat.docx');
    });

    it('sends one response, its question and its thinking with scope response', async () => {
        const chat = setup([question, answer, followUp, second]);
        await chat.downloadChatResponse(1);
        expect(fetch.mock.calls[0][0]).toBe('/recording/7/download/chat');
        expect(sentBody()).toEqual({ messages: [question, answer], scope: 'response' });
        expect(toasts).toEqual(['chat.downloadSuccess']);
    });

    it('names the file from the response header, else chat-response.docx', async () => {
        let chat = setup([question, answer], "attachment; filename*=UTF-8''chat-%E9%80%B1-response.docx");
        await chat.downloadChatResponse(1);
        expect(anchor.download).toBe('chat-週-response.docx');
        chat = setup([question, answer]);
        await chat.downloadChatResponse(1);
        expect(anchor.download).toBe('chat-response.docx');
    });

    it('sends nothing for a turn that is not an answer with text', async () => {
        const chat = setup([question, answer]);
        await chat.downloadChatResponse(0);
        expect(fetch).not.toHaveBeenCalled();
        expect(toasts).toEqual(['chat.noMessagesToDownload']);
    });
});
