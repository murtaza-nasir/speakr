import { describe, it, expect } from 'vitest';
import {
    PROMPT_VAR_RE,
    extractVariableNames,
    inferVarLabel,
    buildVariableList,
} from './prompt-variables.js';

describe('extractVariableNames', () => {
    it('returns an empty array for empty / non-string inputs', () => {
        expect(extractVariableNames('')).toEqual([]);
        expect(extractVariableNames(null)).toEqual([]);
        expect(extractVariableNames(undefined)).toEqual([]);
        expect(extractVariableNames(42)).toEqual([]);
        expect(extractVariableNames({})).toEqual([]);
    });

    it('extracts a single variable', () => {
        expect(extractVariableNames('Agenda: {{agenda}}')).toEqual(['agenda']);
    });

    it('extracts multiple variables in source order', () => {
        expect(extractVariableNames('{{a}} then {{b}} then {{c}}')).toEqual(['a', 'b', 'c']);
    });

    it('deduplicates repeated variables, keeping first-seen order', () => {
        expect(extractVariableNames('{{a}} {{b}} {{a}} {{c}} {{b}}')).toEqual(['a', 'b', 'c']);
    });

    it('tolerates whitespace inside the braces', () => {
        expect(extractVariableNames('{{ agenda }} {{  location  }}')).toEqual(['agenda', 'location']);
    });

    it('accepts identifier-shaped names with digits and underscores', () => {
        expect(extractVariableNames('{{user_1}} {{_private}} {{a1b2}}')).toEqual(['user_1', '_private', 'a1b2']);
    });

    it('rejects identifiers that start with a digit', () => {
        expect(extractVariableNames('{{1bad}} {{2nd}}')).toEqual([]);
    });

    it('rejects identifiers with hyphens or other punctuation', () => {
        expect(extractVariableNames('{{has-hyphen}} {{has.dot}} {{has space}}')).toEqual([]);
    });

    it('rejects unicode identifiers (ASCII-only by design)', () => {
        // Documented limitation in src/utils/prompt_variables.py
        expect(extractVariableNames('{{café}} {{日本}}')).toEqual([]);
    });

    it('does not match single braces', () => {
        expect(extractVariableNames('{not_a_var} {also not}')).toEqual([]);
    });

    it('does not match unbalanced braces', () => {
        expect(extractVariableNames('{{unclosed}')).toEqual([]);
        expect(extractVariableNames('unclosed}}')).toEqual([]);
    });

    it('treats SSTI probe identifiers as plain names (no eval, no attribute access)', () => {
        // The frontend never substitutes — it just enumerates names. The
        // backend single-pass re.sub guarantees the same on the server.
        expect(extractVariableNames('{{__class__}} {{config}} {{__import__}}')).toEqual([
            '__class__', 'config', '__import__',
        ]);
    });
});

describe('inferVarLabel', () => {
    it('returns empty for empty input', () => {
        expect(inferVarLabel('')).toBe('');
        expect(inferVarLabel(null)).toBe('');
        expect(inferVarLabel(undefined)).toBe('');
    });

    it('capitalises and replaces underscores with spaces', () => {
        expect(inferVarLabel('agenda')).toBe('Agenda');
        expect(inferVarLabel('meeting_location')).toBe('Meeting location');
        expect(inferVarLabel('quarterly_review_notes')).toBe('Quarterly review notes');
    });

    it('handles single underscore and trims', () => {
        expect(inferVarLabel('_')).toBe('');
        expect(inferVarLabel('_private')).toBe('Private');
    });

    it('does not break on already-capitalised input', () => {
        expect(inferVarLabel('Agenda')).toBe('Agenda');
    });
});

describe('PROMPT_VAR_RE', () => {
    it('is global so matchAll yields every occurrence', () => {
        expect(PROMPT_VAR_RE.global).toBe(true);
    });

    it('is the same shape as the server-side pattern', () => {
        // Sanity check: server-side regex is r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}".
        // Identical class semantics; if these diverge, the upload form will
        // surface a different set of variables than the server substitutes.
        expect(PROMPT_VAR_RE.source).toBe('\\{\\{\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*\\}\\}');
    });
});

describe('buildVariableList — priority chain', () => {
    const input = (over = {}) => ({
        tagsWithPrompts: [],
        folder: null,
        userPrompt: '',
        adminPrompt: '',
        ...over,
    });

    it('returns empty when no source has any prompt', () => {
        expect(buildVariableList(input())).toEqual([]);
    });

    it('returns empty when sources have prompts but no variables', () => {
        expect(buildVariableList(input({
            tagsWithPrompts: [{ name: 'Plain', custom_prompt: 'Just summarise.' }],
        }))).toEqual([]);
    });

    it('uses tags when at least one tag has a prompt with variables', () => {
        const result = buildVariableList(input({
            tagsWithPrompts: [{ name: 'Meeting', custom_prompt: 'Agenda: {{agenda}}' }],
            folder: { name: 'Projects', custom_prompt: '{{project}}' },
            userPrompt: 'User: {{quarter}}',
            adminPrompt: 'Admin: {{org}}',
        }));
        expect(result.map(v => v.name)).toEqual(['agenda']);
        expect(result[0].sources).toEqual([{ type: 'tag', name: 'Meeting' }]);
    });

    it('aggregates variables across multiple tags with prompts', () => {
        const result = buildVariableList(input({
            tagsWithPrompts: [
                { name: 'TagA', custom_prompt: '{{agenda}}' },
                { name: 'TagB', custom_prompt: '{{location}}' },
                { name: 'TagC', custom_prompt: 'No vars here' },
            ],
        }));
        expect(result.map(v => v.name)).toEqual(['agenda', 'location']);
    });

    it('records multiple sources for the same variable name when both tags use it', () => {
        const result = buildVariableList(input({
            tagsWithPrompts: [
                { name: 'TagA', custom_prompt: '{{agenda}}' },
                { name: 'TagB', custom_prompt: 'Also {{agenda}}' },
            ],
        }));
        expect(result).toHaveLength(1);
        expect(result[0].sources).toEqual([
            { type: 'tag', name: 'TagA' },
            { type: 'tag', name: 'TagB' },
        ]);
    });

    it('skips tags with no custom_prompt', () => {
        const result = buildVariableList(input({
            tagsWithPrompts: [
                { name: 'TagA', custom_prompt: null },
                { name: 'TagB', custom_prompt: '' },
                { name: 'TagC', custom_prompt: '{{agenda}}' },
            ],
        }));
        expect(result.map(v => v.name)).toEqual(['agenda']);
        expect(result[0].sources).toEqual([{ type: 'tag', name: 'TagC' }]);
    });

    it('falls through to folder when no tag has a prompt', () => {
        const result = buildVariableList(input({
            tagsWithPrompts: [{ name: 'Plain', custom_prompt: null }],
            folder: { name: 'Projects', custom_prompt: '{{project}}' },
            userPrompt: 'User: {{quarter}}',
        }));
        expect(result.map(v => v.name)).toEqual(['project']);
        expect(result[0].sources).toEqual([{ type: 'folder', name: 'Projects' }]);
    });

    it('skips folder layer when any tag had a prompt, even if folder has variables', () => {
        const result = buildVariableList(input({
            tagsWithPrompts: [{ name: 'TagA', custom_prompt: '{{agenda}}' }],
            folder: { name: 'F', custom_prompt: '{{project}}' },
        }));
        expect(result.map(v => v.name)).toEqual(['agenda']);
    });

    it('falls through to user prompt when no tags and no folder prompt', () => {
        const result = buildVariableList(input({
            folder: null,
            userPrompt: 'User: {{quarter}} {{agenda}}',
            adminPrompt: 'Admin: {{org}}',
        }));
        expect(result.map(v => v.name)).toEqual(['quarter', 'agenda']);
        expect(result[0].sources).toEqual([{ type: 'user', name: 'Your default prompt' }]);
    });

    it('falls through to admin prompt only when nothing above has content', () => {
        const result = buildVariableList(input({
            adminPrompt: 'Org: {{org}}',
        }));
        expect(result.map(v => v.name)).toEqual(['org']);
        expect(result[0].sources).toEqual([{ type: 'admin', name: 'Site default prompt' }]);
    });

    it('skips admin when user prompt has any content (even if it has no variables)', () => {
        const result = buildVariableList(input({
            userPrompt: 'Plain user prompt with no variables',
            adminPrompt: '{{org}}',
        }));
        // User prompt wins as a layer; admin layer is not scanned.
        expect(result).toEqual([]);
    });

    it('skips folder when folder has no custom_prompt, falls through to user', () => {
        const result = buildVariableList(input({
            folder: { name: 'Empty', custom_prompt: null },
            userPrompt: '{{quarter}}',
        }));
        expect(result.map(v => v.name)).toEqual(['quarter']);
    });

    it('handles non-array tagsWithPrompts gracefully', () => {
        expect(buildVariableList(input({ tagsWithPrompts: null, userPrompt: '{{q}}' }))
            .map(v => v.name)).toEqual(['q']);
        expect(buildVariableList(input({ tagsWithPrompts: undefined, userPrompt: '{{q}}' }))
            .map(v => v.name)).toEqual(['q']);
    });

    it('produces label and sources fields for each entry', () => {
        const result = buildVariableList(input({
            tagsWithPrompts: [{ name: 'M', custom_prompt: '{{meeting_location}}' }],
        }));
        expect(result).toEqual([
            { name: 'meeting_location', label: 'Meeting location', sources: [{ type: 'tag', name: 'M' }] },
        ]);
    });
});
