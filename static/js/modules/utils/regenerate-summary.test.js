import { describe, expect, it } from 'vitest';
import {
    buildRegenerateBody, groupStartFrom, normalizePrompt, optionLabel, promptsDiffer,
    runVariables, saveLabel, sourceLabel,
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
