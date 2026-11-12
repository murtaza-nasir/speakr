/**
 * Helpers of the Regenerate summary dialog (#584).
 *
 * The dialog shows the viewer's resolved prompt (GET /recording/<id>/summary_prompt).
 * An unchanged prompt run by the recording owner sends no custom prompt, so the
 * summary task resolves the prompt exactly as for an automatic summary. An
 * edited prompt, or any prompt run by an editor, is sent as a replacing
 * custom prompt: the editor runs the prompt they see, not the owner's.
 * Added instructions are sent separately and apply to this run only.
 */

import { extractVariableNames, inferVarLabel } from './prompt-variables.js';

export const MAX_PROMPT_LENGTH = 8000;

// A textarea turns \r\n into \n, so compare prompts with normalised line ends.
export const normalizePrompt = (text) => String(text ?? '').replace(/\r\n?/g, '\n').trim();

export const promptsDiffer = (a, b) => normalizePrompt(a) !== normalizePrompt(b);

/**
 * The chip text for a prompt source. `base` is a start-from option
 * ({ kind, name }) or the resolved source ({ kind: 'tags', name: 'A, B' });
 * kind 'blank' is an empty start.
 */
export const sourceLabel = (t, base, edited) => {
    const kind = base?.kind || 'blank';
    const params = { name: base?.name || '' };
    const keys = {
        tag: 'Tag', tags: 'Tags', folder: 'Folder', user: 'User',
        admin: 'Admin', default: 'Default', blank: 'Blank',
    };
    const suffix = keys[kind] || 'Blank';
    return t(`regenerateSummary.${edited ? 'editedFrom' : 'source'}${suffix}`, params);
};

/** The label of a Start from menu entry. */
export const optionLabel = (t, option) => {
    if (option.kind === 'tag') return t('regenerateSummary.menuTag', { name: option.name || '' });
    if (option.kind === 'folder') return t('regenerateSummary.menuFolder', { name: option.name || '' });
    return sourceLabel(t, option, false);
};

/** The label of the Save link, or '' when the base cannot be saved. */
export const saveLabel = (t, target) => {
    if (!target) return '';
    if (target.kind === 'tag') return t('regenerateSummary.saveToTag', { name: target.name || '' });
    if (target.kind === 'folder') return t('regenerateSummary.saveToFolder', { name: target.name || '' });
    return t('regenerateSummary.saveToUser');
};

/** Start from entries split into those that apply to the recording and the rest. */
export const groupStartFrom = (options) => {
    const list = Array.isArray(options) ? options : [];
    return {
        applies: list.filter(o => o.applies),
        others: list.filter(o => !o.applies),
    };
};

/**
 * Variables the run will substitute: those in the prompt, then those in the
 * added instructions. Same shape as buildVariableList.
 */
export const runVariables = ({ prompt, instructions, sourceType, sourceName, instructionsName }) => {
    const acc = new Map();
    const add = (text, type, name) => {
        for (const v of extractVariableNames(text)) {
            if (!acc.has(v)) acc.set(v, { name: v, label: inferVarLabel(v), sources: [] });
            const entry = acc.get(v);
            if (!entry.sources.find(s => s.type === type && s.name === name)) entry.sources.push({ type, name });
        }
    };
    add(prompt, sourceType || 'user', sourceName || '');
    add(instructions, 'instructions', instructionsName || '');
    return Array.from(acc.values());
};

/** Variable values worth sending: non-empty strings only, as the old modal did. */
export const cleanVariables = (values) => {
    const cleaned = {};
    for (const [key, value] of Object.entries(values || {})) {
        if (value !== undefined && value !== null && String(value).trim() !== '') {
            cleaned[key] = String(value);
        }
    }
    return cleaned;
};

/**
 * The request body for a regenerate.
 *
 * prompt: the textarea; resolvedPrompt: what the server resolved for the viewer;
 * isOwner: whether the viewer owns the recording. Owner + unchanged prompt sends
 * no custom prompt, which is the request the old dialog sent for "Use default
 * prompt".
 */
export const buildRegenerateBody = ({ prompt, resolvedPrompt, isOwner, instructions, variables }) => {
    const body = {};
    if (!isOwner || promptsDiffer(prompt, resolvedPrompt)) {
        body.custom_prompt = normalizePrompt(prompt);
        body.prompt_mode = 'replace';
    }
    const extra = normalizePrompt(instructions);
    if (extra) body.additional_instructions = extra;
    if (variables && typeof variables === 'object') body.prompt_variables = cleanVariables(variables);
    return body;
};
