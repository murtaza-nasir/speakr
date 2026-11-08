// The active UI theme as a reactive value for a Vue app
// (docs/advanced/ui-themes.md). The slot partials (templates/themes/slot.html)
// switch on uiTheme, so a theme chosen with setUiTheme, in the picker or in
// another tab takes effect without a reload.
//
// The theme itself is applied by templates/includes/ui_theme.html: before the
// first paint it sets data-ui-theme on <html>, and window.speakrApplyUiTheme
// switches the attribute and the stylesheet and raises 'speakr:ui-theme'.

export const UI_THEME_EVENT = 'speakr:ui-theme';
export const DEFAULT_UI_THEME = 'default';

export function currentUiTheme(doc) {
    const id = doc && doc.documentElement ? doc.documentElement.getAttribute('data-ui-theme') : null;
    return id || DEFAULT_UI_THEME;
}

export function useUiTheme(ref, win = window) {
    const uiTheme = ref(currentUiTheme(win.document));
    win.addEventListener(UI_THEME_EVENT, (event) => {
        const id = event && event.detail ? event.detail.id : null;
        uiTheme.value = id || DEFAULT_UI_THEME;
    });
    const setUiTheme = (id) => {
        if (typeof win.speakrApplyUiTheme === 'function') return win.speakrApplyUiTheme(id);
        return uiTheme.value;
    };
    return { uiTheme, setUiTheme };
}
