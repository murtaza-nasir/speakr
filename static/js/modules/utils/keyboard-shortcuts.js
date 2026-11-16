/**
 * Keyboard shortcuts: one registry and one keydown listener per page.
 *
 * A binding is data:
 *
 *   { keys: ['j'], scope: 'list', group: 'list', label: 'shortcuts.nextRecording',
 *     run(event) { ... }, when: () => true, repeat: false, inInput: false, help: true }
 *
 *   keys    one or more key specs; any of them triggers the binding.
 *           'j'        the letter without Shift
 *           'D'        the letter with Shift (Caps Lock is ignored)
 *           '?' '/'    a character as typed (with AltGr where the layout needs it)
 *           'Escape' 'Enter' 'Space'   a named key
 *           'g r'      a sequence: g, released, then r
 *   scope   where the key applies (a name, or several). The page says which
 *           scopes are active (`scopes`); a dialog over the page replaces
 *           them with its own (`blocker`).
 *   group   the heading the key is listed under in the help.
 *   label   locale key of the action.
 *   run     what the key does: the function of the button it stands for.
 *   when    (event) => false while that button is absent or disabled
 *           (optional). For one key, the first binding whose `when` holds
 *           is the one run.
 *   repeat  true when the key repeats while held (moving in a list).
 *   inInput true for a key that also applies while a text field has the
 *           focus (Escape on a panel).
 *   help    false keeps a binding out of the help (a second meaning of a
 *           key that is already listed).
 *
 * A binding without `run` is a reference row: a key handled elsewhere in the
 * app (the segment editor, Ctrl+F), listed in the help and never handled
 * here.
 *
 * The help is generated from the same table (`help()`), so it cannot list a
 * key that does not exist.
 */

/** How long a sequence waits for its second key. */
export const SEQUENCE_MS = 1500;

/** Keys that belong to a focused link or button. */
const CONTROL_KEYS = new Set(['Enter', 'Space', 'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
    'Delete', 'Backspace', 'Home', 'End', 'PageUp', 'PageDown']);
const CONTROL_TAGS = new Set(['A', 'BUTTON', 'SUMMARY']);
const CONTROL_ROLES = new Set(['button', 'link', 'menuitem', 'tab', 'slider', 'checkbox', 'switch', 'option', 'radio']);
const MODIFIER_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'CapsLock', 'NumLock', 'ScrollLock',
    'Fn', 'FnLock', 'Hyper', 'Super', 'OS', 'Dead', 'Unidentified', 'Process']);

const ARROWS = { ArrowLeft: '\u2190', ArrowRight: '\u2192', ArrowUp: '\u2191', ArrowDown: '\u2193' };

const tagOf = (el) => (el && el.tagName ? String(el.tagName).toUpperCase() : '');

/** True when typing goes into `el`: an input, textarea, select or editable element. */
export function isTypingTarget(el) {
    const tag = tagOf(el);
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || !!(el && el.isContentEditable);
}

/** True when `el` is a link, a button or another control that keeps Enter, Space, Tab and the arrows. */
export function isControlTarget(el) {
    if (CONTROL_TAGS.has(tagOf(el))) return true;
    const role = el && typeof el.getAttribute === 'function' ? el.getAttribute('role') : null;
    return !!role && CONTROL_ROLES.has(role);
}

/**
 * The name of the key of a keydown event, as written in a binding, or null
 * for a modifier pressed alone. A letter follows Shift and not Caps Lock:
 * 'd' without Shift, 'D' with it.
 */
export function keyToken(event) {
    const key = event && event.key;
    if (!key || MODIFIER_KEYS.has(key)) return null;
    if (key === ' ' || key === 'Spacebar') return 'Space';
    if (key === 'Esc') return 'Escape';
    if (key.length === 1 && key.toLowerCase() !== key.toUpperCase()) {
        return event.shiftKey ? key.toUpperCase() : key.toLowerCase();
    }
    return key;
}

/**
 * True for a character such as # that the keyboard layout types with AltGr.
 * Windows reports AltGr as Ctrl+Alt, so the event looks like a shortcut of
 * the browser; it is a typed character. Letters are never accepted this
 * way, and neither is anything with Cmd held.
 */
export function isAltGraphCharacter(event) {
    const key = event && event.key;
    if (!key || key.length !== 1 || key === ' ' || key.toLowerCase() !== key.toUpperCase()) return false;
    if (event.metaKey) return false;
    return typeof event.getModifierState === 'function' && !!event.getModifierState('AltGraph');
}

/** The keys of a spec as shown in the help: 'g r' -> ['g', 'r'], 'D' -> ['Shift', 'D'], 'Escape' -> ['Esc']. */
export function keyCaps(spec) {
    const sequence = String(spec).split(' ').filter(Boolean);
    const caps = [];
    sequence.forEach((key) => {
        if (key === 'Escape') caps.push('Esc');
        else if (ARROWS[key]) caps.push(ARROWS[key]);
        else if (key.includes('+')) caps.push(...key.split('+'));
        else if (key.length === 1 && key.toLowerCase() !== key.toUpperCase() && key === key.toUpperCase()) caps.push('Shift', key);
        else caps.push(key);
    });
    return { caps, sequence: sequence.length > 1 };
}

/**
 * @param {object} options
 * @param {() => boolean} [options.enabled]   the user's setting; nothing is handled while false
 * @param {() => boolean} [options.phone]     the phone layout is shown; nothing is handled while true
 * @param {() => string[]} [options.scopes]   the scopes of the page that are active now
 * @param {() => (string|null)} [options.blocker]  the scope of a dialog over the page, or null
 * @param {() => number} [options.now]        a clock in milliseconds
 */
export function createShortcuts({
    enabled = () => true, phone = () => false, scopes = () => ['global'], blocker = () => null, now = () => Date.now(),
} = {}) {
    const bindings = [];
    let pending = null; // { key, at }
    let target = null;

    const add = (list) => {
        (list || []).forEach((binding) => {
            if (!binding || !Array.isArray(binding.keys) || !binding.keys.length) return;
            bindings.push({ repeat: false, inInput: false, ...binding });
        });
    };

    const activeScopes = () => {
        const over = blocker();
        return new Set(over ? [over] : scopes());
    };
    const inScope = (binding, active) => (Array.isArray(binding.scope) ? binding.scope : [binding.scope]).some((name) => active.has(name));
    const usable = (binding, active, event) => typeof binding.run === 'function' && inScope(binding, active)
        && (typeof binding.when !== 'function' || !!binding.when(event));

    /** Handle one keydown event. Returns the binding that was run, 'pending' for the first key of a sequence, or null. */
    const handle = (event) => {
        if (!event || event.defaultPrevented) return null;
        if (!enabled() || phone()) { pending = null; return null; }
        // A key of an input method that is composing text is not a key of the app
        if (event.isComposing || event.keyCode === 229) return null;
        const token = keyToken(event);
        if ((event.ctrlKey || event.metaKey || event.altKey) && !isAltGraphCharacter(event)) { pending = null; return null; }
        if (!token) return null;

        const typing = isTypingTarget(event.target);
        if (!typing && CONTROL_KEYS.has(token) && isControlTarget(event.target)) { pending = null; return null; }

        const first = pending && now() - pending.at <= SEQUENCE_MS ? pending.key : null;
        pending = null;
        const spec = first ? `${first} ${token}` : token;
        const active = activeScopes();

        const match = bindings.find((binding) => binding.keys.includes(spec) && usable(binding, active, event)
            && (!typing || binding.inInput) && (!event.repeat || binding.repeat));
        if (match) {
            event.preventDefault();
            match.run(event);
            return match;
        }
        // The second key of a sequence is used up, whether it meant anything or not
        if (first) {
            if (!typing) event.preventDefault();
            return null;
        }
        if (typing || event.repeat) return null;
        const prefix = `${token} `;
        if (bindings.some((binding) => usable(binding, active, event) && binding.keys.some((key) => key.startsWith(prefix)))) {
            pending = { key: token, at: now() };
            event.preventDefault();
            return 'pending';
        }
        return null;
    };

    /**
     * The help: [{ group, rows: [{ label, params, keys: [{ caps, sequence }] }] }] in the
     * order of `groups`, then of the table. Bindings with one label in one
     * group share a row.
     */
    const help = (groups) => {
        const order = groups && groups.length ? groups : [...new Set(bindings.map((binding) => binding.group))];
        return order.map((group) => {
            const rows = [];
            bindings.filter((binding) => binding.group === group && binding.help !== false && binding.label).forEach((binding) => {
                let row = rows.find((item) => item.label === binding.label);
                if (!row) { row = { label: binding.label, params: binding.params, keys: [] }; rows.push(row); }
                binding.keys.forEach((key) => {
                    const caps = keyCaps(key);
                    if (!row.keys.some((item) => item.caps.join(' ') === caps.caps.join(' '))) row.keys.push(caps);
                });
            });
            return { group, rows };
        }).filter((entry) => entry.rows.length > 0);
    };

    const onKeydown = (event) => { handle(event); };
    const cancel = () => { pending = null; };

    /** Listen on `doc` (once). */
    const install = (doc) => {
        if (target || !doc || !doc.addEventListener) return;
        target = doc;
        doc.addEventListener('keydown', onKeydown);
        const view = doc.defaultView;
        if (view && view.addEventListener) view.addEventListener('blur', cancel);
    };
    const uninstall = () => {
        if (!target) return;
        target.removeEventListener('keydown', onKeydown);
        const view = target.defaultView;
        if (view && view.removeEventListener) view.removeEventListener('blur', cancel);
        target = null;
    };

    return { add, handle, help, install, uninstall, bindings: () => bindings.slice(), pending: () => (pending ? pending.key : null) };
}
