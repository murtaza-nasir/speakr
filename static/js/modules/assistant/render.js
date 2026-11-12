/**
 * An Ambrose answer as HTML (phase 2b): sanitized markdown with numbered
 * citation markers and one Sources list (citations.js), for the recording
 * panel and the Ask page alike.
 *
 * A segment marker seeks the open player; a link to another recording opens
 * it at the cited time ("n @ m:ss"). In the panel, bare timestamps such as
 * [00:07:35] stay seek chips (the fallback when the model cites no segment).
 * Every text the model wrote is set with textContent only.
 */

import { PLACEHOLDER_RE, formatClock, numberAnswer, sourceTitle } from './citations.js';

const TS_RE = /\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]/g;
// Notes about earlier answers that a model may echo back
const LEAKED_NOTE_RE = /^\[(citations in this answer|citations were displayed|research performed|note about the assistant)/i;
const SOURCES_HEADING_RE = /^(sources?|references?|quellen|fuentes|fontes|источники|来源)\s*:?$/i;

const textNodes = (root, test) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode: (node) => (node.parentElement && node.parentElement.closest('code, pre, a, button')
            ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const out = [];
    let node;
    while ((node = walker.nextNode())) if (test(node.nodeValue)) out.push(node);
    return out;
};

const replaceInText = (node, re, make) => {
    const frag = document.createDocumentFragment();
    const text = node.nodeValue;
    let last = 0;
    for (const m of text.matchAll(re)) {
        frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        frag.appendChild(make(m));
        last = m.index + m[0].length;
    }
    frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
};

function seekButton(className, seconds, t) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = className;
    if (seconds !== null && seconds !== undefined && isFinite(seconds)) {
        el.dataset.seek = String(Math.max(0, Math.floor(seconds)));
        el.title = t('chat.playFromHere');
    } else {
        el.disabled = true;
    }
    return el;
}

function recordingHref(recordingId, seconds) {
    return '/recordings/' + recordingId + (seconds !== null && seconds !== undefined ? '?t=' + Math.floor(seconds) : '');
}

function marker(occurrence, opts) {
    const { t } = opts;
    if (occurrence.kind === 'library') {
        // The open recording itself, cited as a link: seek, do not navigate.
        if (opts.openRecordingId != null && occurrence.recording_id === opts.openRecordingId && occurrence.seconds != null) {
            const el = seekButton('chat-cite', occurrence.seconds, t);
            el.textContent = String(occurrence.n);
            return el;
        }
        const a = document.createElement('a');
        a.className = 'chat-cite chat-cite--link';
        a.href = recordingHref(occurrence.recording_id, occurrence.seconds);
        a.title = sourceTitle(occurrence);
        a.textContent = occurrence.seconds != null
            ? occurrence.n + ' @ ' + formatClock(occurrence.seconds) : String(occurrence.n);
        return a;
    }
    const el = seekButton('chat-cite' + (occurrence.verified ? '' : ' chat-cite--unverified'), occurrence.seconds, t);
    el.textContent = String(occurrence.n);
    if (el.dataset.seek) el.setAttribute('aria-label', t('chat.playFromHere') + ': ' + formatClock(occurrence.seconds));
    return el;
}

function sourcesList(sources, opts) {
    const { t } = opts;
    const wrap = document.createElement('div');
    // citation-refs: the Ask page's (and its themes') Sources styling
    wrap.className = 'chat-sources citation-refs';
    const label = document.createElement('div');
    label.className = 'chat-sources__label citation-refs__label';
    label.textContent = t('chat.sources');
    wrap.appendChild(label);
    const ol = document.createElement('ol');
    for (const source of sources) {
        const li = document.createElement('li');
        li.value = source.n;
        if (source.kind === 'library') {
            const a = document.createElement('a');
            a.href = recordingHref(source.recording_id, null);
            a.className = 'chat-sources__title';
            a.textContent = sourceTitle(source);
            li.appendChild(a);
        } else {
            if (source.start_time !== null && source.start_time !== undefined) {
                const time = seekButton('chat-ts-chip', source.start_time, t);
                time.textContent = formatClock(source.start_time);
                li.appendChild(time);
            }
            if (source.speaker) {
                const who = document.createElement('span');
                who.className = 'chat-sources__speaker';
                who.textContent = source.speaker;
                li.appendChild(who);
            }
            if (source.quote) {
                const quote = document.createElement('span');
                quote.className = 'chat-sources__quote';
                quote.setAttribute('dir', 'auto');
                quote.textContent = '“' + source.quote + '”';
                li.appendChild(quote);
            }
            if (!source.verified) {
                const note = document.createElement('span');
                note.className = 'chat-sources__unverified';
                note.textContent = t('chat.quoteNotFound');
                li.appendChild(note);
            }
        }
        ol.appendChild(li);
    }
    wrap.appendChild(ol);
    return wrap;
}

/** Remove a trailing Sources section the model wrote itself, when it holds only citations. */
function dropWrittenSources(root) {
    const top = [...root.children];
    for (let i = top.length - 1; i >= 0; i--) {
        const el = top[i];
        const headingish = /^H[1-6]$/.test(el.tagName) ||
            (el.tagName === 'P' && el.children.length === 1 && el.firstElementChild &&
             el.firstElementChild.tagName === 'STRONG' &&
             el.firstElementChild.textContent.trim() === el.textContent.trim());
        if (!headingish) continue;
        if (!SOURCES_HEADING_RE.test(el.textContent.trim())) continue;
        const tail = top.slice(i + 1);
        const onlyCitations = tail.every((node) => {
            const clone = node.cloneNode(true);
            clone.querySelectorAll('.chat-cite').forEach((c) => c.remove());
            return clone.textContent.replace(/[\d\s.,;:()[\]@–—-]+/g, '') === '';
        });
        if (onlyCitations) {
            tail.forEach((node) => node.remove());
            el.remove();
        }
        break;
    }
}

function timestampChips(root, t) {
    for (const node of textNodes(root, (v) => { TS_RE.lastIndex = 0; return TS_RE.test(v); })) {
        replaceInText(node, TS_RE, (m) => {
            // [a:b] is m:ss; [a:b:c] is h:mm:ss
            const seconds = m[3] !== undefined ? (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]) : (+m[1]) * 60 + (+m[2]);
            const chip = seekButton('chat-ts-chip', seconds, t);
            chip.textContent = formatClock(seconds);
            return chip;
        });
    }
}

/**
 * The HTML for an answer, numbered after `prior` (citations.priorNumbers).
 * Sets message.sources to the numbering shown. opts: {t, timestamps,
 * openRecordingId}.
 */
export function renderAnswer(message, prior, opts) {
    const { text, occurrences, sources } = numberAnswer(message.content, message.segmentSources, prior);
    message.sources = sources;
    const html = window.renderMarkdownSafe(text);
    try {
        const tpl = document.createElement('template');
        tpl.innerHTML = html;
        for (const node of textNodes(tpl.content, (v) => v.includes('⁣'))) {
            replaceInText(node, PLACEHOLDER_RE, (m) => {
                const occurrence = occurrences[Number(m[1])];
                return occurrence ? marker(occurrence, opts) : document.createTextNode('');
            });
        }
        [...tpl.content.querySelectorAll('p')].forEach((p) => {
            const txt = p.textContent.trim();
            if (LEAKED_NOTE_RE.test(txt) && txt.endsWith(']')) p.remove();
        });
        dropWrittenSources(tpl.content);
        if (opts.timestamps) timestampChips(tpl.content, opts.t);
        if (sources.length) tpl.content.appendChild(sourcesList(sources, opts));
        return tpl.innerHTML;
    } catch (_) {
        return html;
    }
}
