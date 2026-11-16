/**
 * The parts of the keyboard shortcuts that every signed-in page shares: the
 * registry with the user's setting and the phone layout applied, the keys
 * that are the same on every page (the help, navigation, dark mode, Escape
 * on a dialog), and the help dialog.
 *
 * The registry and its rules are in keyboard-shortcuts.js. The keys of the
 * main app are in composables/shortcuts.js.
 */

import { createShortcuts } from './keyboard-shortcuts.js';

/** Below this width the phone layout is shown, and no key is handled. */
export const PHONE_QUERY = '(max-width: 767px)';

/** The headings of the help, in order. */
export const HELP_GROUPS = ['general', 'navigation', 'list', 'recording', 'player', 'assistant', 'editor', 'video'];

/** What the server says about the account (includes/i18n_bootstrap.html). */
export function shortcutSettings(win = window) {
    const given = (win && win.speakrShortcuts) || {};
    return { enabled: given.enabled !== false, admin: !!given.admin, inquire: !!given.inquire };
}

const shown = (el) => !!(el && el.getClientRects && el.getClientRects().length);

/** The dialog that lies over the page, or null. */
export function topOverlay(doc = document) {
    let top = null;
    let topLayer = -Infinity;
    doc.querySelectorAll('.modal-overlay, [data-shortcut-scope]').forEach((el) => {
        if (!shown(el)) return;
        const view = doc.defaultView;
        const layer = view ? parseInt(view.getComputedStyle(el).zIndex, 10) || 0 : 0;
        if (layer >= topLayer) { top = el; topLayer = layer; }
    });
    return top;
}

/** The scope of the dialog over the page: its data-shortcut-scope, 'modal' without one, null without a dialog. */
export function overlayScope(doc = document) {
    const top = topOverlay(doc);
    return top ? (top.getAttribute('data-shortcut-scope') || 'modal') : null;
}

/** The Close button of the dialog over the page. */
function overlayCloseButton(doc) {
    const top = topOverlay(doc);
    if (!top || top.hasAttribute('data-shortcut-scope')) return null;
    const buttons = [...top.querySelectorAll('.modal-header .modal-close')].filter((button) => shown(button) && !button.disabled);
    return buttons.length ? buttons[buttons.length - 1] : null;
}

/** A registry with the account's setting, the phone layout and the dialogs of the page applied. */
export function createPageShortcuts({ scopes, win = window, doc = document } = {}) {
    const phoneQuery = win.matchMedia ? win.matchMedia(PHONE_QUERY) : null;
    return createShortcuts({
        enabled: () => shortcutSettings(win).enabled,
        phone: () => !!(phoneQuery && phoneQuery.matches),
        blocker: () => overlayScope(doc),
        scopes: scopes || (() => ['global']),
    });
}

const el = (doc, tag, className, text) => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
};

/** The help as an element: one section per group, a row per action. */
export function renderHelp(groups, t, doc = document) {
    const root = el(doc, 'div', 'shortcut-help');
    root.setAttribute('data-shortcut-help', '');
    const columns = el(doc, 'div', 'shortcut-help__groups');
    groups.forEach(({ group, rows }) => {
        const section = el(doc, 'section', 'shortcut-help__group');
        section.setAttribute('data-shortcut-group', group);
        section.appendChild(el(doc, 'h4', 'shortcut-help__heading', t(`shortcuts.groups.${group}`)));
        const list = el(doc, 'dl', 'shortcut-help__list');
        rows.forEach((row) => {
            const line = el(doc, 'div', 'shortcut-help__row');
            const keys = el(doc, 'dt', 'shortcut-help__keys');
            row.keys.forEach((key, index) => {
                if (index > 0) keys.appendChild(el(doc, 'span', 'shortcut-help__or', t('shortcuts.or')));
                const combo = el(doc, 'span', 'shortcut-help__combo');
                key.caps.forEach((cap, at) => {
                    if (at > 0 && key.sequence) combo.appendChild(el(doc, 'span', 'shortcut-help__then', t('shortcuts.then')));
                    combo.appendChild(el(doc, 'kbd', 'kbd', cap === 'Space' ? t('shortcuts.keys.space') : cap));
                });
                keys.appendChild(combo);
            });
            line.appendChild(keys);
            line.appendChild(el(doc, 'dd', 'shortcut-help__label', t(row.label, row.params)));
            list.appendChild(line);
        });
        section.appendChild(list);
        columns.appendChild(section);
    });
    root.appendChild(columns);
    root.appendChild(el(doc, 'p', 'shortcut-help__note', t('shortcuts.settingHint')));
    return root;
}

/** Show the help of `registry` in the app's dialog. */
export function openShortcutHelp(registry, { t, win = window, doc = document } = {}) {
    const dialogs = win.SpeakrDialog;
    if (!win.alertDialog || (dialogs && dialogs.isOpen && dialogs.isOpen())) return;
    win.alertDialog({
        title: t('shortcuts.title'),
        content: renderHelp(registry.help(HELP_GROUPS), t, doc),
        size: 'lg',
        confirmLabel: t('common.close'),
    });
}

/**
 * The keys of every page.
 *
 * @param {object} options
 * @param {(key: string, params?: object) => string} options.t
 * @param {() => void} options.openHelp
 * @param {() => void} [options.home]            go to the recordings (default: open /)
 * @param {() => void} [options.toggleDarkMode]  the function of the light and dark switch in the account menu
 */
export function globalBindings({ openHelp, home, toggleDarkMode, win = window, doc = document } = {}) {
    const settings = shortcutSettings(win);
    const go = (path) => () => { win.location.href = path; };
    const list = [
        { keys: ['?'], scope: 'global', group: 'general', label: 'shortcuts.showHelp', run: openHelp },
        // A menu built on <details data-close-outside> is closed by close-outside.js; nothing else is closed with it
        { keys: ['Escape'], scope: ['global', 'modal'], group: 'general', label: 'shortcuts.close', inInput: true,
            when: () => !!doc.querySelector('details[data-close-outside][open]'), run: () => {} },
        { keys: ['Escape'], scope: 'modal', group: 'general', label: 'shortcuts.close',
            when: () => !!overlayCloseButton(doc), run: () => { const button = overlayCloseButton(doc); if (button) button.click(); } },
        { keys: ['g r'], scope: 'global', group: 'navigation', label: 'shortcuts.goRecordings', run: home || go('/') },
    ];
    if (settings.inquire) {
        list.push({ keys: ['g a'], scope: 'global', group: 'navigation', label: 'shortcuts.goAssistant',
            params: { name: win.speakrAssistantName || 'Ambrose' }, run: go('/inquire') });
    }
    list.push({ keys: ['g s'], scope: 'global', group: 'navigation', label: 'shortcuts.goSettings', run: go('/account') });
    if (settings.admin) list.push({ keys: ['g d'], scope: 'global', group: 'navigation', label: 'shortcuts.goAdmin', run: go('/admin') });
    if (toggleDarkMode) list.push({ keys: ['D'], scope: 'global', group: 'general', label: 'shortcuts.toggleDarkMode', run: () => toggleDarkMode() });
    return list;
}

/**
 * The keys of a page outside the main app (account, admin, group
 * management, Ask Ambrose): the keys of every page and the page's own.
 * Returns { registry, openHelp }.
 */
export function installPageShortcuts({ t, toggleDarkMode, extra = [], win = window, doc = document } = {}) {
    const registry = createPageShortcuts({ win, doc });
    const openHelp = () => openShortcutHelp(registry, { t, win, doc });
    registry.add(globalBindings({ openHelp, toggleDarkMode, win, doc }));
    registry.add(extra);
    registry.install(doc);
    return { registry, openHelp };
}
