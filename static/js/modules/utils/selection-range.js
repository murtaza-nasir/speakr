/**
 * Selecting recordings in the sidebar with the keyboard modifiers of a
 * desktop file list: Ctrl-click (Cmd-click on macOS) adds or removes one
 * recording, Shift-click adds every recording between the last one clicked
 * (the anchor) and this one, in the order the list shows them.
 *
 * Starting a selection with a modifier while a recording is open counts the
 * open recording as already selected and as the anchor, so Shift-clicking a
 * second recording selects the whole span between them.
 */

/**
 * @param {object} p
 * @param {Set} p.selected        ids currently selected
 * @param {Array} p.orderedIds    ids in the order shown in the list
 * @param {*} p.anchorId          id of the last recording clicked in selection mode
 * @param {*} p.openId            id of the recording open in the detail view
 * @param {*} p.id                id of the recording clicked
 * @param {boolean} p.ctrl        Ctrl or Cmd held
 * @param {boolean} p.shift       Shift held
 * @param {boolean} p.selectionMode
 * @returns {null|{selected: Set, anchorId: *, enterMode: boolean}}
 *          null when the click should open the recording as usual
 */
export function applyListClick({ selected, orderedIds, anchorId, openId, id, ctrl, shift, selectionMode }) {
    if (!selectionMode && !ctrl && !shift) return null;

    const openVisible = openId != null && orderedIds.includes(openId);
    const next = selectionMode ? new Set(selected) : new Set(openVisible ? [openId] : []);
    let anchor = selectionMode ? anchorId : (openVisible ? openId : null);

    if (shift && anchor != null && orderedIds.includes(anchor) && orderedIds.includes(id)) {
        const a = orderedIds.indexOf(anchor);
        const b = orderedIds.indexOf(id);
        for (let i = Math.min(a, b); i <= Math.max(a, b); i++) next.add(orderedIds[i]);
    } else if (shift) {
        next.add(id);
        anchor = id;
    } else if (!selectionMode && next.has(id)) {
        // Ctrl-clicking the open recording starts a selection holding it.
        anchor = id;
    } else {
        if (next.has(id)) next.delete(id);
        else next.add(id);
        anchor = id;
    }
    return { selected: next, anchorId: anchor, enterMode: !selectionMode };
}
