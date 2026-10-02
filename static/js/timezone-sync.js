// Report this browser's timezone for titles the server writes (#412).
//
// Dates on pages are converted in the browser and do not need this. Titles
// from naming templates are written by the server, often with no browser
// involved, so the server keeps each user's zone. In 'auto' mode (the
// default) the zone reported here is saved; a zone chosen in Account settings
// is kept. Sent once per browser session, or again when the zone changes.
(function () {
    var zone;
    try {
        zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch (e) {
        return;
    }
    if (!zone) return;
    var key = 'speakr.timezoneReported';
    try {
        if (sessionStorage.getItem(key) === zone) return;
    } catch (e) { /* storage unavailable: report anyway */ }
    var meta = document.querySelector('meta[name="csrf-token"]');
    fetch('/api/user/timezone', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': meta ? meta.getAttribute('content') : ''
        },
        body: JSON.stringify({ timezone: zone })
    }).then(function (r) {
        if (r.ok) {
            try { sessionStorage.setItem(key, zone); } catch (e) { /* ignore */ }
        }
    }).catch(function () { /* offline or logged out: try again next page */ });
})();
