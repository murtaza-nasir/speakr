// Tailwind CSS configuration for the stylesheet compiled at build time
// (scripts/build_css.sh locally, the css-stage of the Dockerfile for images).
// The output, static/css/tailwind.css, is linked from every page template.
//
// Only complete class names that appear literally in the files below are
// compiled. A class name assembled at runtime from parts (for example
// `bg-${color}-500`) is absent from the output; each full class name has to
// be written out instead (see static/js/modules/utils/errorDisplay.js).

/** @type {import('tailwindcss').Config} */
export default {
    content: [
        './templates/**/*.html',
        './static/js/**/*.js',
        './src/**/*.py',
    ],
    // The theme is applied with the "dark" class on <html> (applyTheme in the
    // templates, global-header.js), not with the system preference.
    darkMode: 'class',
    theme: {
        extend: {
            maxHeight: {
                '85vh': '85vh',
                '90vh': '90vh',
            },
            colors: {
                primary: 'var(--bg-primary)',
                secondary: 'var(--bg-secondary)',
                accent: 'var(--bg-accent)',
            },
        },
    },
    plugins: [],
};
