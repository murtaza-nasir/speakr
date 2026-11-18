/**
 * The app's own confirm, message and input dialogs, in place of the
 * browser's confirm(), alert() and prompt(). A classic script, so that the
 * Vue app and the standalone pages (account, admin, group management, share,
 * inquire) use the same one: it sets
 *
 *   window.confirmDialog({ title?, message, confirmLabel?, cancelLabel?, danger?, alternateLabel? })
 *       -> Promise<boolean>         true when confirmed
 *       With alternateLabel, a second answer is offered on a button between
 *       Cancel and the confirming one, and the promise is 'alternate' for it.
 *   window.alertDialog({ title?, message, confirmLabel?, content?, size? })
 *       -> Promise<void>            resolved when closed
 *   window.promptDialog({ title?, message, value?, placeholder?, confirmLabel?, cancelLabel? })
 *       -> Promise<string | null>   null when cancelled
 *
 * Each also takes the message alone as a string. A destructive confirmation
 * passes `danger: true` and names its action on the button (Delete, Discard).
 * A message dialog may show an element in place of the message (`content`,
 * with a title) in a wider panel (`size`: 'md' or 'lg'); the keyboard
 * shortcuts help is one.
 *
 * The dialog is built from the modal primitives (static/css/primitives.css),
 * so it follows the themes. Esc and a click beside the panel cancel, Enter
 * confirms (on the Cancel button it presses that button, as everywhere),
 * Tab stays inside, and the element that had the focus gets it back. While
 * it is open no key reaches the page under it. It lies above the other
 * dialogs and their popovers, and under the toasts. Dialogs asked for while
 * one is open are shown one after the other.
 *
 * The page-leave warning (beforeunload) is the browser's own and cannot be
 * replaced; nothing here is used for it.
 */
(function (root) {
    'use strict';

    const FOCUSABLE = 'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

    // ---- logic, no DOM ---------------------------------------------------

    const translate = (key, fallback) => {
        try {
            const i18n = root.i18n;
            if (i18n && typeof i18n.t === 'function') {
                const value = i18n.t(key);
                if (value && value !== key) return value;
            }
        } catch (e) { /* fall back */ }
        return fallback;
    };

    const text = (value) => (value == null ? '' : String(value));

    /**
     * What a dialog shows: { kind, title, message, confirmLabel, cancelLabel,
     * alternateLabel, danger, value, placeholder, content, size }. `options` is an object or
     * the message. A message dialog has no cancel button (cancelLabel is '');
     * only a message dialog has `content` and a size other than 'sm'.
     */
    function dialogSpec(kind, options, t = translate) {
        const given = options && typeof options === 'object' ? options : { message: options };
        const type = kind === 'alert' || kind === 'prompt' ? kind : 'confirm';
        return {
            kind: type,
            title: text(given.title),
            message: text(given.message),
            confirmLabel: text(given.confirmLabel) || t('common.ok', 'OK'),
            cancelLabel: type === 'alert' ? '' : (text(given.cancelLabel) || t('common.cancel', 'Cancel')),
            alternateLabel: type === 'confirm' ? text(given.alternateLabel) : '',
            danger: type === 'confirm' && !!given.danger,
            value: type === 'prompt' ? text(given.value) : '',
            placeholder: type === 'prompt' ? text(given.placeholder) : '',
            content: type === 'alert' && given.content && typeof given.content === 'object' ? given.content : null,
            size: type === 'alert' && (given.size === 'md' || given.size === 'lg') ? given.size : 'sm',
        };
    }

    /**
     * What a key does in an open dialog: 'cancel', 'confirm', 'tab', 'block'
     * or null (left to the focused control). `on` is where the focus is:
     * 'cancel' (the Cancel button), 'confirm', 'input' or 'other'. `mod` is
     * Ctrl or Cmd held. Undo, redo and save are blocked outside the dialog's
     * own input: the browser would otherwise undo typing in a text field of
     * the page under the dialog.
     */
    function keyIntent(key, on, { composing = false, mod = false } = {}) {
        if (composing) return null;
        if (mod) return on !== 'input' && /^[zys]$/i.test(key) ? 'block' : null;
        if (key === 'Escape' || key === 'Esc') return 'cancel';
        if (key === 'Tab') return 'tab';
        // On a button, Enter presses that button: the browser does it
        if (key === 'Enter') return on === 'cancel' || on === 'confirm' ? null : 'confirm';
        return null;
    }

    /** The control Tab goes to, of `count` controls with the focus on `index` (-1: outside). */
    function trapIndex(count, index, backwards) {
        if (count <= 0) return -1;
        if (index < 0) return backwards ? count - 1 : 0;
        return (index + (backwards ? -1 : 1) + count) % count;
    }

    /** What the promise resolves to. */
    function result(kind, confirmed, value) {
        if (kind === 'alert') return undefined;
        if (kind === 'prompt') return confirmed ? text(value) : null;
        // The second answer of a confirmation with alternateLabel
        if (confirmed === 'alternate') return 'alternate';
        return !!confirmed;
    }

    // ---- the dialog ----------------------------------------------------------

    const queue = [];
    let open = null;

    const el = (tag, className, content) => {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (content != null) node.textContent = content;
        return node;
    };

    function show(spec, resolve) {
        const id = `app-dialog-${Date.now().toString(36)}`;
        const overlay = el('div', 'modal-overlay app-dialog');
        overlay.setAttribute('data-app-dialog', spec.kind);
        const panel = el('div', `modal-panel modal-panel--${spec.size} app-dialog__panel${spec.danger ? ' modal-panel--danger' : ''}`);
        panel.setAttribute('role', spec.kind === 'alert' || spec.danger ? 'alertdialog' : 'dialog');
        panel.setAttribute('aria-modal', 'true');
        panel.tabIndex = -1;

        if (spec.title) {
            const header = el('div', 'modal-header');
            const title = el('h3', 'modal-title', spec.title);
            title.id = `${id}-title`;
            header.appendChild(title);
            panel.appendChild(header);
            panel.setAttribute('aria-labelledby', title.id);
        }

        const body = el('div', 'modal-body app-dialog__body');
        const message = spec.content || el('p', 'app-dialog__message', spec.message);
        message.id = `${id}-message`;
        body.appendChild(message);
        // An element in place of the message is too long to be read out as the description
        if (!spec.content || !spec.title) panel.setAttribute(spec.title ? 'aria-describedby' : 'aria-labelledby', message.id);
        let input = null;
        if (spec.kind === 'prompt') {
            input = el('input', 'field app-dialog__input');
            input.type = 'text';
            input.value = spec.value;
            input.autocomplete = 'off';
            if (spec.placeholder) input.placeholder = spec.placeholder;
            input.setAttribute('aria-labelledby', message.id);
            input.setAttribute('data-app-dialog-input', '');
            body.appendChild(input);
        }
        panel.appendChild(body);

        const footer = el('div', 'modal-footer');
        let cancel = null;
        if (spec.cancelLabel) {
            cancel = el('button', 'btn btn--ghost', spec.cancelLabel);
            cancel.type = 'button';
            cancel.setAttribute('data-app-dialog-cancel', '');
            footer.appendChild(cancel);
        }
        let alternate = null;
        if (spec.alternateLabel) {
            alternate = el('button', 'btn btn--ghost', spec.alternateLabel);
            alternate.type = 'button';
            alternate.setAttribute('data-app-dialog-alternate', '');
            footer.appendChild(alternate);
        }
        const confirm = el('button', `btn ${spec.danger ? 'btn--danger' : 'btn--primary'}`, spec.confirmLabel);
        confirm.type = 'button';
        confirm.setAttribute('data-app-dialog-confirm', '');
        footer.appendChild(confirm);
        panel.appendChild(footer);
        overlay.appendChild(panel);

        const before = document.activeElement;
        let done = false;

        const controls = () => Array.from(panel.querySelectorAll(FOCUSABLE)).filter((node) => node.getClientRects().length);

        const close = (confirmed) => {
            if (done) return;
            done = true;
            root.removeEventListener('keydown', onKeydown, true);
            document.removeEventListener('focusin', onFocusIn, true);
            const value = input ? input.value : '';
            if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
            open = null;
            if (before && typeof before.focus === 'function' && document.contains(before)) {
                try { before.focus({ preventScroll: true }); } catch (e) { /* gone */ }
            }
            resolve(result(spec.kind, confirmed, value));
            next();
        };

        function onKeydown(event) {
            // Nothing under the dialog sees a key while it is open
            event.stopPropagation();
            const active = document.activeElement;
            // Enter on any of the buttons presses that button
            const on = active === cancel || active === alternate ? 'cancel' : active === confirm ? 'confirm' : active === input ? 'input' : 'other';
            const intent = keyIntent(event.key, on, { composing: event.isComposing, mod: event.ctrlKey || event.metaKey });
            if (intent === 'block') {
                event.preventDefault();
            } else if (intent === 'cancel') {
                event.preventDefault();
                close(false);
            } else if (intent === 'confirm') {
                event.preventDefault();
                close(true);
            } else if (intent === 'tab') {
                event.preventDefault();
                const list = controls();
                const target = list[trapIndex(list.length, list.indexOf(active), event.shiftKey)];
                if (target) target.focus();
            }
        }

        function onFocusIn(event) {
            if (!panel.contains(event.target)) {
                event.stopPropagation();
                (input || confirm).focus();
            }
        }

        // A click beside the panel cancels; a press that starts in the panel and ends outside does not
        let pressedOutside = false;
        overlay.addEventListener('mousedown', (event) => { pressedOutside = event.target === overlay; });
        overlay.addEventListener('click', (event) => { if (event.target === overlay && pressedOutside) close(false); });
        if (cancel) cancel.addEventListener('click', () => close(false));
        if (alternate) alternate.addEventListener('click', () => close('alternate'));
        confirm.addEventListener('click', () => close(true));

        root.addEventListener('keydown', onKeydown, true);
        document.addEventListener('focusin', onFocusIn, true);
        document.body.appendChild(overlay);
        open = { close, spec };
        if (input) {
            input.focus();
            input.select();
        } else {
            confirm.focus();
        }
    }

    function next() {
        if (open || !queue.length) return;
        const { spec, resolve } = queue.shift();
        show(spec, resolve);
    }

    const ask = (kind) => (options) => new Promise((resolve) => {
        const spec = dialogSpec(kind, options);
        if (typeof document === 'undefined' || !document.body) {
            resolve(result(spec.kind, false, ''));
            return;
        }
        queue.push({ spec, resolve });
        next();
    });

    root.confirmDialog = ask('confirm');
    root.alertDialog = ask('alert');
    root.promptDialog = ask('prompt');
    root.SpeakrDialog = {
        confirm: root.confirmDialog, alert: root.alertDialog, prompt: root.promptDialog,
        dialogSpec, keyIntent, trapIndex, result,
        /** True while a dialog is open. */
        isOpen: () => !!open,
    };
})(typeof window !== 'undefined' ? window : globalThis);
