// The color scheme a page shows: the one saved in this browser, else the
// default from templates/includes/theme_account.html
// (window.speakrThemeDefaults: the look for new accounts on signed-out pages,
// blue for a signed-in account), else blue.

export function savedColorScheme(win = window) {
    let stored = null;
    try { stored = win.localStorage.getItem('colorScheme'); } catch (e) { /* storage blocked */ }
    const defaults = win.speakrThemeDefaults || {};
    return stored || defaults.colorScheme || 'blue';
}
