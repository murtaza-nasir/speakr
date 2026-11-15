/**
 * The app's own dialogs for the modules: confirmDialog, alertDialog and
 * promptDialog of static/js/app-dialog.js (a classic script on every page,
 * which sets them on window). Each returns a promise.
 *
 * Should that script be missing from a page, the browser's dialog is asked
 * instead, so that nothing is ever discarded or deleted without the question.
 */

const messageOf = (options) => (options && typeof options === 'object' ? options.message : options);
const own = (name) => (typeof window !== 'undefined' && typeof window[name] === 'function' ? window[name] : null);
const native = (name) => (typeof window !== 'undefined' && typeof window[name] === 'function' ? window[name].bind(window) : null);

/** { title?, message, confirmLabel?, cancelLabel?, danger? } or the message. Resolves to true when confirmed. */
export function confirmDialog(options) {
    const dialog = own('confirmDialog');
    if (dialog) return dialog(options);
    const ask = native('confirm');
    return Promise.resolve(ask ? !!ask(messageOf(options)) : false);
}

/** { title?, message, confirmLabel? } or the message. Resolves when closed. */
export function alertDialog(options) {
    const dialog = own('alertDialog');
    if (dialog) return dialog(options);
    const tell = native('alert');
    if (tell) tell(messageOf(options));
    return Promise.resolve();
}

/** { title?, message, value?, placeholder?, confirmLabel?, cancelLabel? } or the message. Resolves to the text, or null. */
export function promptDialog(options) {
    const dialog = own('promptDialog');
    if (dialog) return dialog(options);
    const ask = native('prompt');
    return Promise.resolve(ask ? ask(messageOf(options), (options && options.value) || '') : null);
}
