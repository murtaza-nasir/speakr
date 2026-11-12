/**
 * AssistantChat: the one Ambrose chat (phase 2b), used by the recording's
 * floating panel, the mobile chat tab and the Ask page. Speakr has no build
 * step, so the component is this setup function plus one Jinja partial
 * (templates/components/assistant/chat.html), shared by the main app and the
 * Ask page's app the way components/header-controls.html is.
 *
 * The host app owns the message list, the input and the loading flag (its
 * other markup reads them) and says how a question is sent; this module
 * reads the event stream (src/services/assistant_events.py), keeps each turn
 * as a timeline (steps, reasoning, answer, sources), numbers citations
 * through the whole chat (citations.js, render.js), follows the newest text
 * unless the user scrolls away, and handles Stop, copy and keys.
 *
 * The partial reads everything from the returned object, `assistant`.
 */

import { historySources, plainAnswer, priorNumbers } from './citations.js';
import { renderAnswer } from './render.js';

const NEAR_BOTTOM_PX = 48;

const hasText = (message) => !!message && typeof message.content === 'string' && message.content !== '';

/** Add or replace a step by id: 'running' first, then its result. */
export function upsertStep(steps, step) {
    if (!step || step.id === undefined) return steps;
    const at = steps.findIndex((s) => s.id === step.id);
    if (at >= 0) steps.splice(at, 1, Object.assign({}, steps[at], step));
    else steps.push(Object.assign({}, step));
    return steps;
}

/** Add or replace a segment source by its marker text. */
export function upsertSegmentSource(sources, source) {
    if (!source || typeof source.marker !== 'string' || !source.marker) return sources;
    const at = sources.findIndex((s) => s.marker === source.marker);
    if (at >= 0) sources.splice(at, 1, source);
    else sources.push(source);
    return sources;
}

/** The history a question is sent with: role, content, the activity line and the numbering shown. */
export function historyOf(messages, from = 0) {
    return (Array.isArray(messages) ? messages : []).slice(from)
        .filter((m) => (m.role === 'user' || m.role === 'assistant') && hasText(m) && !m.failed)
        .map((m) => {
            const entry = { role: m.role, content: m.content };
            if (m.role === 'assistant') {
                if (m.activitySummary) entry.activity = m.activitySummary;
                const sources = historySources(m.sources);
                if (sources) entry.sources = sources;
            }
            return entry;
        });
}

/** Read a `data: {...}` event stream, calling onEvent for each payload. */
export async function readEvents(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const handleLine = async (line) => {
        if (!line.startsWith('data: ')) return false;
        const body = line.slice(6);
        if (!body) return false;
        let data;
        if (body === '[DONE]') data = '[DONE]';
        else {
            try { data = JSON.parse(body); } catch (_) { return false; }
        }
        return (await onEvent(data)) === 'done';
    };
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
            if (await handleLine(line)) { try { reader.cancel(); } catch (_) { /* closed */ } return; }
        }
    }
    if (buffer) await handleLine(buffer);
}

/**
 * opts:
 *   messages, input, loading  refs owned by the host app
 *   scroller, inputEl         optional refs for the timeline and the textarea
 *   t                         translate
 *   request({message, history, signal, thread})  -> fetch Response (the surface's route)
 *   historyFrom()             first message sent verbatim (the Ask page's memory)
 *   onEvent(data)             surface events; return true when handled
 *   onDone(thread)            after a turn (token budget refresh)
 *   isDisabled()              composer disabled
 *   timestamps                bare [00:07:35] timestamps become seek chips (panel)
 *   openRecordingId(thread)   the recording the chat is about, if any
 *   toast(message, icon)
 *   thread()                  optional: the thread shown now (the recording
 *                             chat keeps one per recording, chat-threads.js).
 *                             A thread is an object with messages, input,
 *                             loading, status, following and controller; a
 *                             turn keeps writing to the thread it started in.
 *                             Without it, the one thread is the host's refs.
 */
export function useAssistantChat(opts) {
    const { ref, reactive, computed, nextTick } = window.Vue;
    const { messages, input, loading, t } = opts;
    const scroller = opts.scroller || ref(null);
    const inputEl = opts.inputEl || ref(null);
    const following = ref(true);
    const status = ref('');

    // The one thread over the host's refs (the Ask page), or the host's
    // current thread (the recording chat).
    const single = {
        get messages() { return messages.value; }, set messages(v) { messages.value = v; },
        get input() { return input.value; }, set input(v) { input.value = v; },
        get loading() { return loading.value; }, set loading(v) { loading.value = v; },
        get status() { return status.value; }, set status(v) { status.value = v; },
        get following() { return following.value; }, set following(v) { following.value = v; },
        controller: null,
    };
    const current = () => (opts.thread ? opts.thread() : single);
    const field = (name) => (opts.thread
        ? computed({ get: () => current()[name], set: (v) => { current()[name] = v; } })
        : ({ messages, input, loading, following, status })[name]);

    // ---------------------------------------------------------------- scrolling
    const nearBottom = () => {
        const el = scroller.value;
        if (!el) return true;
        return el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
    };
    const toBottom = () => {
        const el = scroller.value;
        if (!el) return;
        requestAnimationFrame(() => { if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight; });
    };
    // Follow the newest text while the user is at the bottom; scrolling up
    // detaches, and "Jump to latest" (or sending) follows again.
    // A thread that is not on screen keeps its own following flag and
    // never moves the timeline.
    const onScroll = () => { current().following = nearBottom(); };
    const follow = async (thread = current()) => {
        await nextTick();
        if (thread === current() && thread.following) toBottom();
    };
    const jumpToLatest = () => { current().following = true; toBottom(); };

    const focusInput = () => nextTick(() => { try { inputEl.value && inputEl.value.focus(); } catch (_) { /* gone */ } });

    // ---------------------------------------------------------------- rendering
    const renderOptions = (thread) => ({
        t, timestamps: !!opts.timestamps,
        openRecordingId: opts.openRecordingId ? opts.openRecordingId(thread) : null,
    });
    /** Render a turn, numbering its citations after those earlier in its own list. */
    const render = (turn, list = current().messages || [], thread = current()) => {
        const index = list.indexOf(turn);
        turn.html = renderAnswer(turn, priorNumbers(list, index < 0 ? list.length : index), renderOptions(thread));
    };

    /** The answer at index (or the message itself) as plain text: citations as [n] and the sources under it. */
    const messageText = (message) => {
        if (!message) return '';
        if (message.role !== 'assistant') return message.content || '';
        const list = current().messages || [];
        const index = list.indexOf(message);
        return plainAnswer(message.content, message.segmentSources,
            priorNumbers(list, index < 0 ? list.length : index), t('chat.sources'));
    };

    // ---------------------------------------------------------------- sending
    const newTurn = () => reactive({
        role: 'assistant', content: '', html: '', thinking: '', thinkingExpanded: false,
        steps: [], stepsLabel: '', activitySummary: '', agentStepCount: 0, showSteps: false,
        segmentSources: [], sources: [], streaming: true, failed: false,
    });

    const send = async () => {
        // The thread and list this question belongs to: the reply keeps
        // arriving there after another recording opens, and a reply still
        // arriving after Clear stays out of the new list.
        const thread = current();
        const text = (thread.input || '').trim();
        if (!text || thread.loading || (opts.isDisabled && opts.isDisabled())) return;
        if (!Array.isArray(thread.messages)) thread.messages = [];

        const list = thread.messages;
        const history = historyOf(list, opts.historyFrom ? opts.historyFrom() : 0);
        list.push({ role: 'user', content: text });
        thread.input = '';
        thread.loading = true;
        thread.status = '';
        thread.following = true;
        focusInput();
        follow(thread);

        let turn = null;
        const ensureTurn = () => {
            if (!turn) {
                list.push(newTurn());
                turn = list[list.length - 1];
            }
            return turn;
        };
        const controller = new AbortController();
        thread.controller = controller;

        const onEvent = async (data) => {
            if (data === '[DONE]') return 'done';
            if (typeof data !== 'object' || data === null) return undefined;
            if (opts.onEvent && opts.onEvent(data)) return undefined;
            if (data.agent_step) {
                upsertStep(ensureTurn().steps, data.agent_step);
                follow(thread);
            }
            if (data.agent_summary) {
                const s = data.agent_summary;
                const tr = ensureTurn();
                tr.stepsLabel = s.label || '';
                tr.activitySummary = s.line || '';
                tr.agentStepCount = s.steps || 0;
            }
            if (data.status && data.message) thread.status = data.message;
            if (data.thinking) {
                const tr = ensureTurn();
                tr.thinking = tr.thinking ? tr.thinking + '\n\n' + data.thinking : data.thinking;
                follow(thread);
            }
            // A segment citation arrives before the text that carries it.
            if (data.source) upsertSegmentSource(ensureTurn().segmentSources, data.source);
            const delta = data.delta || data.content;
            if (typeof delta === 'string' && delta) {
                const tr = ensureTurn();
                tr.content += delta;
                render(tr, list, thread);
                follow(thread);
            }
            if (data.end_of_stream) {
                // The provider's output-token limit: keep the partial answer,
                // say that it was cut off (#349).
                if (data.truncated && turn && turn.content) {
                    turn.content += `\n\n_⚠️ ${t('chat.responseTruncated')}_`;
                    render(turn, list, thread);
                }
                return 'done';
            }
            if (data.error) {
                throw new Error(data.budget_exceeded ? t('adminDashboard.tokenBudgetExceeded') : data.error);
            }
            return undefined;
        };

        try {
            const response = await opts.request({ message: text, history, signal: controller.signal, thread });
            if (!response.ok) {
                let body = {};
                try { body = await response.json(); } catch (_) { /* not JSON */ }
                throw new Error(body.error || t('chat.responseFailed'));
            }
            await readEvents(response, onEvent);
        } catch (error) {
            if (error && error.name === 'AbortError') {
                // Stop: keep what arrived.
            } else {
                // Keep a partial answer and say below it that the connection
                // ended (#282); with nothing yet, the error is the answer.
                const tr = ensureTurn();
                const reason = (error && error.message) || t('chat.responseFailed');
                if ((tr.content || '').trim()) {
                    tr.content = tr.content.trim() + `\n\n_⚠️ ${t('chat.connectionEnded', { error: reason })}_`;
                    render(tr, list, thread);
                } else {
                    tr.failed = true;
                    tr.content = t('chat.errorWithMessage', { error: reason });
                    const span = document.createElement('span');
                    span.className = 'text-red-500';
                    span.textContent = tr.content;
                    tr.html = span.outerHTML;
                }
            }
        } finally {
            if (turn) turn.streaming = false;
            thread.loading = false;
            thread.status = '';
            if (thread.controller === controller) thread.controller = null;
            follow(thread);
            if (thread === current()) focusInput();
            if (opts.onDone) opts.onDone(thread);
        }
    };

    /** Stop the answer arriving in the thread on screen. */
    const stop = () => { const thread = current(); if (thread.controller) thread.controller.abort(); };

    const onKeydown = (event) => {
        if (event.key !== 'Enter' || event.ctrlKey || event.shiftKey || event.isComposing) return;
        event.preventDefault();
        send();
    };

    // ---------------------------------------------------------------- timeline
    /** A marker or chip with data-seek plays the open recording from there. */
    const onTimelineClick = (event) => {
        const chip = event.target.closest && event.target.closest('[data-seek]');
        if (!chip) return;
        const seconds = parseInt(chip.dataset.seek, 10);
        if (!isFinite(seconds)) return;
        // The detail view's player streams from /audio/; incognito uses a
        // blob URL, so fall back to any media element on the page.
        const all = [...document.querySelectorAll('audio, video')];
        const media = all.find((el) => ((el.currentSrc || el.src || '').includes('/audio/')))
            || all.find((el) => (el.currentSrc || el.src));
        if (!media) return;
        media.currentTime = isFinite(media.duration) ? Math.min(seconds, Math.max(0, media.duration - 1)) : seconds;
        media.play().catch(() => {});
    };

    const isLast = (index) => index === (current().messages || []).length - 1;
    /** Copy and Download wait until the answer has finished. */
    const showActions = (message, index) => message.role === 'assistant' && hasText(message) && !message.failed &&
        !(current().loading && isLast(index));
    /** The steps list is open while a turn runs, then folds into its steps line. */
    const stepsOpen = (message) => !!message.streaming || !!message.showSteps;
    const runningLabel = computed(() => {
        const thread = current();
        const list = thread.messages || [];
        const last = list[list.length - 1];
        const steps = last && last.role === 'assistant' ? last.steps : [];
        const running = [...(steps || [])].reverse().find((s) => s.status === 'running');
        return (running && running.label) || thread.status || t('chat.thinking');
    });
    /** The thinking row: while waiting for the answer's first words. */
    const waiting = computed(() => {
        const thread = current();
        if (!thread.loading) return false;
        const list = thread.messages || [];
        const last = list[list.length - 1];
        return !(last && last.role === 'assistant' && hasText(last));
    });

    const copy = (message, event) => {
        const text = messageText(message);
        const button = event && event.currentTarget;
        const flash = () => {
            if (!button) return;
            button.classList.add('copy-success');
            const original = button.innerHTML;
            button.innerHTML = '<i class="fas fa-check"></i>';
            setTimeout(() => { button.classList.remove('copy-success'); button.innerHTML = original; }, 1500);
        };
        const fallback = () => {
            try {
                const area = document.createElement('textarea');
                area.value = text;
                area.style.position = 'fixed';
                area.style.left = '-999999px';
                area.style.top = '-999999px';
                document.body.appendChild(area);
                area.focus();
                area.select();
                const ok = document.execCommand('copy');
                document.body.removeChild(area);
                if (ok) { opts.toast && opts.toast(t('messages.copiedSuccessfully')); flash(); }
                else opts.toast && opts.toast(t('messages.copyNotSupported'), 'fa-exclamation-circle');
            } catch (err) {
                opts.toast && opts.toast(t('messages.copyFailed') + ': ' + err.message, 'fa-exclamation-circle');
            }
        };
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text)
                .then(() => { opts.toast && opts.toast(t('messages.copiedSuccessfully')); flash(); })
                .catch(fallback);
        } else {
            fallback();
        }
    };

    return reactive({
        messages: field('messages'), input: field('input'), loading: field('loading'),
        following: field('following'), status: field('status'), runningLabel, waiting,
        disabled: computed(() => !!(opts.isDisabled && opts.isDisabled())),
        setScroller: (el) => { scroller.value = el; },
        setInput: (el) => { inputEl.value = el; },
        send, stop, onKeydown, onScroll, jumpToLatest, onTimelineClick,
        showActions, stepsOpen, messageText, copy, render,
        scrollToBottom: toBottom, focusInput,
    });
}
