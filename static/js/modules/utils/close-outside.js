/**
 * Menus built on <details data-close-outside> close when the user clicks
 * outside them or presses Escape. A <details> element only closes when its
 * own summary is clicked, and focus-based closing does not work in Safari,
 * where a click does not focus the button.
 */

/** Close every open menu that does not contain `target` (all of them when target is null). */
export function closeOpenMenus(doc, target) {
    doc.querySelectorAll('details[data-close-outside][open]').forEach((menu) => {
        if (!target || !menu.contains(target)) menu.removeAttribute('open');
    });
}

/** Install the listeners once per document. */
export function installCloseOutside(doc = document) {
    if (doc.__speakrCloseOutside) return;
    doc.__speakrCloseOutside = true;
    doc.addEventListener('click', (event) => closeOpenMenus(doc, event.target));
    doc.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeOpenMenus(doc, null);
    });
}
