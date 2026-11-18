import { describe, expect, it } from 'vitest';
import {
    baseSourceKind, buildRegenerateBody, groupStartFrom, normalizePrompt, openingBase, optionLabel,
    promptsDiffer, runVariables, saveLabel, sourceLabel,
} from './regenerate-summary.js';

const t = (key, params = {}) => `${key}${params.name ? `(${params.name})` : ''}`;

describe('buildRegenerateBody', () => {
    it('sends no prompt for the owner with an unchanged prompt', () => {
        const body = buildRegenerateBody({
            prompt: 'Resolved\r\n', resolvedPrompt: 'Resolved', isOwner: true,
            instructions: '  ', variables: { a: 'x', b: '  ', c: null },
        });
        expect(body).toEqual({ prompt_variables: { a: 'x' } });
    });

    it('sends added instructions on their own when the prompt is unchanged', () => {
        const body = buildRegenerateBody({ prompt: 'P', resolvedPrompt: 'P', isOwner: true, instructions: ' Agenda ' });
        expect(body).toEqual({ additional_instructions: 'Agenda' });
    });

    it('replaces the prompt once it is edited', () => {
        const body = buildRegenerateBody({ prompt: 'Edited', resolvedPrompt: 'P', isOwner: true, instructions: 'Agenda' });
        expect(body).toEqual({ custom_prompt: 'Edited', prompt_mode: 'replace', additional_instructions: 'Agenda' });
    });

    it('always sends the prompt an editor sees', () => {
        const body = buildRegenerateBody({ prompt: 'Editor default', resolvedPrompt: 'Editor default', isOwner: false });
        expect(body).toEqual({ custom_prompt: 'Editor default', prompt_mode: 'replace' });
    });
});

describe('prompt comparison', () => {
    it('ignores line endings and outer whitespace', () => {
        expect(normalizePrompt(' a\r\nb ')).toBe('a\nb');
        expect(promptsDiffer('a\r\nb', 'a\nb\n')).toBe(false);
        expect(promptsDiffer('a', 'b')).toBe(true);
    });
});

describe('labels', () => {
    it('names the source and its edited form', () => {
        expect(sourceLabel(t, { kind: 'tag', name: 'Sales' }, false)).toBe('regenerateSummary.sourceTag(Sales)');
        expect(sourceLabel(t, { kind: 'admin' }, true)).toBe('regenerateSummary.editedFromAdmin');
        expect(sourceLabel(t, null, false)).toBe('regenerateSummary.sourceBlank');
    });

    it('labels menu entries and save targets', () => {
        expect(optionLabel(t, { kind: 'folder', name: 'Clients' })).toBe('regenerateSummary.menuFolder(Clients)');
        expect(optionLabel(t, { kind: 'user' })).toBe('regenerateSummary.sourceUser');
        expect(saveLabel(t, { kind: 'tag', name: 'Sales' })).toBe('regenerateSummary.saveToTag(Sales)');
        expect(saveLabel(t, { kind: 'user' })).toBe('regenerateSummary.saveToUser');
        expect(saveLabel(t, null)).toBe('');
    });
});

describe('groupStartFrom', () => {
    it('puts the prompts that apply first', () => {
        const groups = groupStartFrom([
            { key: 'user', applies: false }, { key: 'tag:1', applies: true }, { key: 'admin', applies: false },
        ]);
        expect(groups.applies.map(o => o.key)).toEqual(['tag:1']);
        expect(groups.others.map(o => o.key)).toEqual(['user', 'admin']);
        expect(groupStartFrom(undefined)).toEqual({ applies: [], others: [] });
    });
});

describe('runVariables', () => {
    it('lists the variables of the prompt and the instructions once each', () => {
        const vars = runVariables({
            prompt: 'For {{client_name}} on {{date}}', instructions: 'Agenda for {{client_name}} and {{room}}',
            sourceType: 'tag', sourceName: 'Sales', instructionsName: 'Added',
        });
        expect(vars.map(v => v.name)).toEqual(['client_name', 'date', 'room']);
        expect(vars[0].label).toBe('Client name');
        expect(vars[0].sources).toEqual([{ type: 'tag', name: 'Sales' }, { type: 'instructions', name: 'Added' }]);
    });
});

describe('the prompt used for the current summary', () => {
    const resolved = {
        prompt: 'Resolved', source: 'tag', source_key: 'tag:3', source_name: 'Standup',
        save_target: { kind: 'tag', id: 3, name: 'Standup' },
    };
    const used = (source, prompt) => ({
        key: 'used', kind: 'used', source, name: null, prompt, applies: true,
        save_target: { kind: 'user', id: null, name: null },
    });

    it('opens with the resolved prompt when nothing is stored', () => {
        expect(openingBase({ ...resolved, used: null })).toEqual({
            key: 'tag:3', kind: 'tag', name: 'Standup', prompt: 'Resolved', save_target: resolved.save_target,
        });
        expect(openingBase({ ...resolved, source: 'tag', source_key: 'tags', source_name: 'A, B' }).kind).toBe('tags');
    });

    it('opens with the custom prompt of the current summary', () => {
        const base = openingBase({ ...resolved, used: used('custom', 'Mine') });
        expect(base).toEqual(used('custom', 'Mine'));
        // Run unchanged, it differs from the resolved prompt, so it is sent again.
        const body = buildRegenerateBody({ prompt: base.prompt, resolvedPrompt: resolved.prompt, isOwner: true });
        expect(body).toEqual({ custom_prompt: 'Mine', prompt_mode: 'replace' });
    });

    it('opens as before when the summary came from a tag, folder or default prompt', () => {
        expect(openingBase({ ...resolved, used: used('tag', 'Older tag text') }).key).toBe('tag:3');
        expect(openingBase({ ...resolved, used: used('default', 'Built in') }).key).toBe('tag:3');
    });

    it('opens as before when the custom prompt is the resolved prompt or empty', () => {
        expect(openingBase({ ...resolved, used: used('custom', 'Resolved\r\n') }).key).toBe('tag:3');
        expect(openingBase({ ...resolved, used: used('custom', '  ') }).key).toBe('tag:3');
    });

    it('labels the entry and its edits', () => {
        expect(optionLabel(t, used('custom', 'Mine'))).toBe('regenerateSummary.sourceUsed');
        expect(sourceLabel(t, used('custom', 'Mine'), false)).toBe('regenerateSummary.sourceUsed');
        expect(sourceLabel(t, used('custom', 'Mine'), true)).toBe('regenerateSummary.editedFromUsed');
        expect(saveLabel(t, used('custom', 'Mine').save_target)).toBe('regenerateSummary.saveToUser');
    });

    it('names the stored source of a used prompt', () => {
        expect(baseSourceKind(used('folder', 'F'))).toBe('folder');
        expect(baseSourceKind({ kind: 'tags' })).toBe('tags');
        expect(baseSourceKind(null)).toBe('blank');
    });
});
