/**
 * Dates on the API token cards of the account page. A classic script: it
 * sets window.SpeakrTokenDates.
 *
 * A value from the last 7 days is shown with its time, so two tokens made
 * on the same day can be told apart; an older value shows the date only.
 * The tooltip always holds the full date and time. Both use the locale of
 * the interface (window.i18n.getIntlLocale()) through Intl, as the rest of
 * the page does.
 */
(function (root) {
    'use strict';

    const RECENT_MS = 7 * 24 * 60 * 60 * 1000;
    const DATE_TIME = {
        year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit',
    };

    /**
     * { text, title } for a Date. text is the date and time when the value
     * is less than 7 days before now (or in the future), else the date only;
     * title is the full date and time. An invalid Date gives empty strings.
     */
    function formatTokenDate(date, locale, now) {
        if (!(date instanceof Date) || isNaN(date.getTime())) return { text: '', title: '' };
        const reference = now instanceof Date ? now.getTime() : Date.now();
        const recent = reference - date.getTime() < RECENT_MS;
        return {
            text: recent ? date.toLocaleString(locale, DATE_TIME) : date.toLocaleDateString(locale),
            title: date.toLocaleString(locale),
        };
    }

    root.SpeakrTokenDates = { formatTokenDate, RECENT_MS };
})(typeof window !== 'undefined' ? window : globalThis);
