/**
 * Prompt-template variable helpers.
 *
 * Mirrors the server-side logic in `src/utils/prompt_variables.py` so the
 * upload form and reprocess modal surface exactly the variables that the
 * summary task would substitute at run time.
 *
 * Identifier rule: ASCII letter or underscore, then letters/digits/underscores.
 * The frontend never substitutes — it only enumerates names so the user can
 * fill values. The backend does the actual substitution.
 */

export const PROMPT_VAR_RE = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;

export const extractVariableNames = (text) => {
    if (!text || typeof text !== 'string') return [];
    const names = [];
    for (const match of text.matchAll(PROMPT_VAR_RE)) {
        if (!names.includes(match[1])) names.push(match[1]);
    }
    return names;
};

export const inferVarLabel = (name) => {
    if (!name) return '';
    const cleaned = name.replace(/_/g, ' ').trim();
    if (!cleaned) return '';
    return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
};

/**
 * Build the variable list for a given priority chain. Returns an array of
 * `{ name, label, sources: [{ type, name }] }` in the order names are first
 * seen.
 *
 * Priority chain (matches `generate_summary_only_task`):
 *   tags (any tag with a prompt wins as a layer) → folder → user default → admin default
 * Only the first non-empty layer is scanned. Lower layers are skipped because
 * their prompt would not run at summarisation time.
 *
 * `tagsWithPrompts` is `[{ name, custom_prompt }, ...]`. `folder` is
 * `{ name, custom_prompt }` or null. `userPrompt` and `adminPrompt` are
 * raw strings or null/empty.
 */
export const buildVariableList = ({ tagsWithPrompts, folder, userPrompt, adminPrompt }) => {
    const acc = new Map();
    const addVars = (text, sourceLabel, sourceType) => {
        for (const name of extractVariableNames(text)) {
            if (!acc.has(name)) {
                acc.set(name, { name, label: inferVarLabel(name), sources: [] });
            }
            const entry = acc.get(name);
            if (!entry.sources.find(s => s.name === sourceLabel && s.type === sourceType)) {
                entry.sources.push({ type: sourceType, name: sourceLabel });
            }
        }
    };

    let anyTagHasPrompt = false;
    if (Array.isArray(tagsWithPrompts)) {
        for (const tag of tagsWithPrompts) {
            if (tag && tag.custom_prompt) {
                addVars(tag.custom_prompt, tag.name, 'tag');
                anyTagHasPrompt = true;
            }
        }
    }

    let folderHasPrompt = false;
    if (!anyTagHasPrompt && folder && folder.custom_prompt) {
        addVars(folder.custom_prompt, folder.name, 'folder');
        folderHasPrompt = true;
    }

    let userPromptHasContent = false;
    if (!anyTagHasPrompt && !folderHasPrompt && userPrompt) {
        addVars(userPrompt, 'Your default prompt', 'user');
        userPromptHasContent = true;
    }

    if (!anyTagHasPrompt && !folderHasPrompt && !userPromptHasContent && adminPrompt) {
        addVars(adminPrompt, 'Site default prompt', 'admin');
    }

    return Array.from(acc.values());
};
