/**
 * The look of the offline recorder (static/offline-recorder.html) before its
 * first paint: light or dark mode, the colour scheme and the UI theme saved
 * in this browser, by the same rules as includes/theme_bootstrap.html and
 * includes/ui_theme.html. A theme with a stylesheet of its own gets it,
 * under this release's ?v= (the URLs the service worker keeps). A classic
 * script in <head>; the page allows no inline script.
 */
(function () {
    'use strict';
    var THEMES = ['classic', 'workspace', 'focus', 'desk', 'cards'];
    var SCHEMES = ['emerald', 'purple', 'rose', 'amber', 'teal'];
    var root = document.documentElement;
    var mode = null, scheme = null, theme = null;
    try {
        mode = localStorage.getItem('darkMode');
        scheme = localStorage.getItem('colorScheme');
        theme = localStorage.getItem('uiTheme');
    } catch (e) { /* storage unavailable: the defaults */ }
    var dark = mode !== null ? mode === 'true'
        : !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    root.classList.toggle('dark', dark);
    if (SCHEMES.indexOf(scheme) !== -1) root.classList.add((dark ? 'theme-dark-' : 'theme-light-') + scheme);
    if (THEMES.indexOf(theme) === -1) theme = 'default';
    root.setAttribute('data-ui-theme', theme);
    if (theme !== 'default') {
        var script = document.currentScript;
        var version = '';
        try { version = new URL(script.src, location.href).searchParams.get('v') || ''; } catch (e) { version = ''; }
        var link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = '/static/css/themes/' + theme + '.css' + (version ? '?v=' + encodeURIComponent(version) : '');
        // After the page's stylesheets, as in the app. The theme sets the
        // font, the page background and its tokens; the rules of
        // offline-recorder.css name only the page's own classes.
        document.head.appendChild(link);
    }
})();
