/* Webhooks tab for the Account settings page (#275).
 *
 * Self-contained module attached when the Account template loads.
 * Talks to /api/v1/webhooks; all user-supplied strings are escaped
 * via escapeHtml() before being written into the DOM.
 */
(function () {
    const list = document.getElementById('webhook-list');
    const modal = document.getElementById('webhook-modal');
    const modalTitle = document.getElementById('webhook-modal-title');
    const idInput = document.getElementById('webhook-id-input');
    const nameInput = document.getElementById('webhook-name-input');
    const urlInput = document.getElementById('webhook-url-input');
    const allowHttpInput = document.getElementById('webhook-allow-http-input');
    const enabledInput = document.getElementById('webhook-enabled-input');
    const eventsContainer = document.getElementById('webhook-events-checkboxes');
    const form = document.getElementById('webhook-form');
    const createBtn = document.getElementById('webhook-create-btn');
    const closeBtn = document.getElementById('webhook-modal-close');
    const cancelBtn = document.getElementById('webhook-modal-cancel');
    const secretBanner = document.getElementById('webhook-secret-banner');
    const secretValueEl = document.getElementById('webhook-secret-value');
    const secretCopyBtn = document.getElementById('webhook-secret-copy');
    const secretDismissBtn = document.getElementById('webhook-secret-dismiss');

    if (!list) return; // template not on this page

    const csrfToken = () => {
        const el = document.querySelector('meta[name="csrf-token"]');
        return el ? el.getAttribute('content') : '';
    };

    let allEventTypes = [];

    async function fetchJson(url, opts = {}) {
        const headers = Object.assign(
            { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
            opts.headers || {}
        );
        const resp = await fetch(url, Object.assign({}, opts, { headers, credentials: 'same-origin' }));
        const text = await resp.text();
        let body = null;
        try { body = text ? JSON.parse(text) : null; } catch (_) { body = null; }
        if (!resp.ok) {
            const err = new Error((body && body.error) || ('HTTP ' + resp.status));
            err.status = resp.status;
            throw err;
        }
        return body;
    }

    function escapeHtml(s) {
        const d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
    }

    // Backend timestamps are naive UTC (no zone). Append 'Z' so they're parsed
    // as UTC and rendered in the viewer's timezone, not mis-read as local.
    function parseServerInstant(s) {
        if (s == null) return new Date(NaN);
        if (typeof s === 'string' && !/(?:Z|[+-]\d{2}:?\d{2})$/.test(s)) {
            s = s.replace(' ', 'T') + 'Z';
        }
        return new Date(s);
    }

    function fmt(dt) {
        if (!dt) return '—';
        try { return parseServerInstant(dt).toLocaleString(); } catch (_) { return String(dt); }
    }

    function statusBadge(wh) {
        if (wh.auto_paused) return '<span class="text-xs px-2 py-0.5 rounded bg-amber-500/15 text-amber-600">auto-paused</span>';
        if (!wh.enabled) return '<span class="text-xs px-2 py-0.5 rounded bg-gray-500/15 text-gray-500">disabled</span>';
        if (wh.consecutive_failures > 0) return '<span class="text-xs px-2 py-0.5 rounded bg-red-500/15 text-red-500">' + Number(wh.consecutive_failures) + ' consecutive failures</span>';
        return '<span class="text-xs px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-600">healthy</span>';
    }

    function deliveryStatusBadge(d) {
        const cls = {
            success: 'bg-emerald-500/15 text-emerald-600',
            pending: 'bg-blue-500/15 text-blue-500',
            failed: 'bg-amber-500/15 text-amber-600',
            permanent_failure: 'bg-red-500/15 text-red-500'
        }[d.status] || 'bg-gray-500/15 text-gray-500';
        return '<span class="text-xs px-2 py-0.5 rounded ' + cls + '">' + escapeHtml(d.status) + '</span>';
    }

    function eventChips(events) {
        if (!events || !events.length) return '<span class="text-xs text-[var(--text-muted)]">no events</span>';
        return events.map(function (e) {
            return '<span class="text-xs px-2 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)] mr-1 mb-1 inline-block">' + escapeHtml(e) + '</span>';
        }).join('');
    }

    function renderWebhookCard(wh) {
        const id = Number(wh.id);
        return ''
            + '<div class="border border-[var(--border-primary)] rounded-lg p-4 bg-[var(--bg-secondary)]" data-webhook-id="' + id + '">'
            +   '<div class="flex items-start justify-between gap-3 flex-wrap">'
            +     '<div class="flex-1 min-w-0">'
            +       '<div class="flex items-center gap-2 flex-wrap">'
            +         '<h3 class="text-sm font-semibold text-[var(--text-primary)]">' + escapeHtml(wh.name) + '</h3>'
            +         statusBadge(wh)
            +       '</div>'
            +       '<p class="mt-1 text-xs font-mono text-[var(--text-muted)] break-all">' + escapeHtml(wh.url) + '</p>'
            +       '<div class="mt-2">' + eventChips(wh.events) + '</div>'
            +       '<p class="mt-2 text-xs text-[var(--text-muted)]">Last delivery: ' + escapeHtml(fmt(wh.last_delivery_at)) + '</p>'
            +     '</div>'
            +     '<div class="flex flex-wrap items-center gap-2 shrink-0">'
            +       '<button data-action="test" class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] hover:bg-[var(--bg-button-hover)] text-[var(--text-secondary)]" title="Send a synthetic webhook.test event"><i class="fas fa-paper-plane mr-1"></i>Test</button>'
            +       '<button data-action="deliveries" class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] hover:bg-[var(--bg-button-hover)] text-[var(--text-secondary)]"><i class="fas fa-list mr-1"></i>Deliveries</button>'
            +       '<button data-action="rotate" class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] hover:bg-[var(--bg-button-hover)] text-[var(--text-secondary)]" title="Rotate HMAC secret"><i class="fas fa-key mr-1"></i>Rotate</button>'
            +       '<button data-action="edit" class="px-2 py-1 text-xs rounded bg-[var(--bg-tertiary)] hover:bg-[var(--bg-button-hover)] text-[var(--text-secondary)]"><i class="fas fa-pen mr-1"></i>Edit</button>'
            +       '<button data-action="delete" class="px-2 py-1 text-xs rounded bg-red-500/10 hover:bg-red-500/20 text-red-500"><i class="fas fa-trash mr-1"></i>Delete</button>'
            +     '</div>'
            +   '</div>'
            +   '<div class="mt-3 hidden" data-deliveries-panel>'
            +     '<div class="border-t border-[var(--border-primary)] pt-3">'
            +       '<h4 class="text-xs font-medium text-[var(--text-muted)] mb-2">Recent deliveries</h4>'
            +       '<div data-deliveries-list class="space-y-1 text-xs"></div>'
            +     '</div>'
            +   '</div>'
            + '</div>';
    }

    function showModalFor(webhook) {
        modalTitle.textContent = webhook ? 'Edit webhook' : 'New webhook';
        idInput.value = webhook ? webhook.id : '';
        nameInput.value = webhook ? webhook.name : '';
        urlInput.value = webhook ? webhook.url : '';
        allowHttpInput.checked = webhook ? !!webhook.allow_http : false;
        enabledInput.checked = webhook ? !!webhook.enabled : true;
        const subscribed = (webhook && webhook.events) || [];
        const checkboxes = allEventTypes
            .filter(function (e) { return e !== 'webhook.test'; })
            .map(function (e) {
                const checked = subscribed.indexOf(e) !== -1 ? 'checked' : '';
                return ''
                    + '<label class="inline-flex items-center gap-2 text-sm">'
                    +   '<input type="checkbox" value="' + escapeHtml(e) + '" class="rounded border-[var(--border-secondary)]" ' + checked + '>'
                    +   '<span class="font-mono text-xs">' + escapeHtml(e) + '</span>'
                    + '</label>';
            }).join('');
        eventsContainer.replaceChildren();
        eventsContainer.insertAdjacentHTML('afterbegin', checkboxes);
        modal.classList.remove('hidden');
    }

    function hideModal() { modal.classList.add('hidden'); }

    function showSecretBanner(secret) {
        secretValueEl.textContent = secret;
        secretBanner.classList.remove('hidden');
        try { secretBanner.scrollIntoView({ behavior: 'smooth', block: 'start' }); } catch (_) {}
    }

    async function loadWebhooks() {
        try {
            const body = await fetchJson('/api/v1/webhooks');
            allEventTypes = body.event_types || [];
            list.replaceChildren();
            if (!body.webhooks || body.webhooks.length === 0) {
                list.insertAdjacentHTML('beforeend', '<p class="text-sm text-[var(--text-muted)]">No webhooks configured. Add one to start receiving event notifications.</p>');
                return;
            }
            body.webhooks.forEach(function (wh) { list.insertAdjacentHTML('beforeend', renderWebhookCard(wh)); });
            list.querySelectorAll('[data-webhook-id]').forEach(function (card) {
                const id = card.getAttribute('data-webhook-id');
                card.querySelectorAll('[data-action]').forEach(function (btn) {
                    btn.addEventListener('click', function () { onCardAction(id, btn.dataset.action, card); });
                });
            });
        } catch (e) {
            list.replaceChildren();
            list.insertAdjacentHTML('beforeend', '<p class="text-sm text-red-500">Error loading webhooks: ' + escapeHtml(e.message) + '</p>');
        }
    }
    window.loadWebhooks = loadWebhooks;

    async function onCardAction(id, action, card) {
        try {
            if (action === 'edit') {
                const wh = await fetchJson('/api/v1/webhooks/' + Number(id));
                showModalFor(wh);
            } else if (action === 'delete') {
                if (!confirm('Delete this webhook? Past deliveries will be removed too.')) return;
                await fetchJson('/api/v1/webhooks/' + Number(id), { method: 'DELETE' });
                await loadWebhooks();
            } else if (action === 'test') {
                await fetchJson('/api/v1/webhooks/' + Number(id) + '/test', { method: 'POST' });
                alert('Test event queued. Check Deliveries in a few seconds.');
            } else if (action === 'rotate') {
                if (!confirm('Rotate the HMAC secret? Any receiver that already has the current secret will start failing until you update it.')) return;
                const body = await fetchJson('/api/v1/webhooks/' + Number(id) + '/rotate-secret', { method: 'POST' });
                if (body && body.secret) showSecretBanner(body.secret);
                await loadWebhooks();
            } else if (action === 'deliveries') {
                const panel = card.querySelector('[data-deliveries-panel]');
                const listEl = card.querySelector('[data-deliveries-list]');
                if (!panel.classList.contains('hidden')) { panel.classList.add('hidden'); return; }
                listEl.replaceChildren();
                listEl.insertAdjacentHTML('beforeend', '<p class="text-xs text-[var(--text-muted)]">Loading...</p>');
                panel.classList.remove('hidden');
                const body = await fetchJson('/api/v1/webhooks/' + Number(id) + '/deliveries?limit=25');
                listEl.replaceChildren();
                if (!body.deliveries || body.deliveries.length === 0) {
                    listEl.insertAdjacentHTML('beforeend', '<p class="text-xs text-[var(--text-muted)]">No deliveries yet.</p>');
                    return;
                }
                body.deliveries.forEach(function (d) {
                    const httpStatus = d.response_status ? '<span class="text-xs text-[var(--text-muted)]">HTTP ' + Number(d.response_status) + '</span>' : '';
                    const errLine = d.error_message ? '<div class="text-xs text-red-500 mt-0.5 break-all">' + escapeHtml(d.error_message) + '</div>' : '';
                    const retryLine = d.next_retry_at ? ' · next retry: ' + escapeHtml(fmt(d.next_retry_at)) : '';
                    const row = ''
                        + '<div class="flex items-center justify-between gap-2 py-1 border-b border-[var(--border-primary)] last:border-0">'
                        +   '<div class="flex-1 min-w-0">'
                        +     '<div class="flex items-center gap-2 flex-wrap">'
                        +       '<span class="font-mono text-xs text-[var(--text-secondary)]">' + escapeHtml(d.event_type) + '</span>'
                        +       deliveryStatusBadge(d)
                        +       httpStatus
                        +     '</div>'
                        +     '<div class="text-xs text-[var(--text-muted)] mt-0.5">' + escapeHtml(fmt(d.created_at)) + ' · attempts: ' + Number(d.attempt_count) + retryLine + '</div>'
                        +     errLine
                        +   '</div>'
                        +   '<button class="text-xs text-[var(--text-muted)] hover:text-[var(--text-primary)]" data-replay="' + Number(d.id) + '" title="Replay this delivery"><i class="fas fa-redo"></i></button>'
                        + '</div>';
                    listEl.insertAdjacentHTML('beforeend', row);
                });
                listEl.querySelectorAll('[data-replay]').forEach(function (btn) {
                    btn.addEventListener('click', async function () {
                        const did = btn.getAttribute('data-replay');
                        try {
                            await fetchJson('/api/v1/webhooks/' + Number(id) + '/deliveries/' + Number(did) + '/replay', { method: 'POST' });
                            // Close and reopen to refresh
                            panel.classList.add('hidden');
                            onCardAction(id, 'deliveries', card);
                        } catch (e2) {
                            alert('Replay failed: ' + e2.message);
                        }
                    });
                });
            }
        } catch (e) {
            alert('Error: ' + e.message);
        }
    }

    createBtn?.addEventListener('click', function () { showModalFor(null); });
    closeBtn?.addEventListener('click', hideModal);
    cancelBtn?.addEventListener('click', hideModal);
    modal?.addEventListener('click', function (e) { if (e.target === modal) hideModal(); });

    form?.addEventListener('submit', async function (e) {
        e.preventDefault();
        const events = Array.from(eventsContainer.querySelectorAll('input[type=checkbox]:checked')).map(function (cb) { return cb.value; });
        if (events.length === 0) { alert('Pick at least one event to subscribe to.'); return; }
        const payload = {
            name: nameInput.value.trim(),
            url: urlInput.value.trim(),
            allow_http: !!allowHttpInput.checked,
            enabled: !!enabledInput.checked,
            events: events
        };
        try {
            const id = idInput.value;
            if (id) {
                await fetchJson('/api/v1/webhooks/' + Number(id), { method: 'PATCH', body: JSON.stringify(payload) });
                hideModal();
                await loadWebhooks();
            } else {
                const body = await fetchJson('/api/v1/webhooks', { method: 'POST', body: JSON.stringify(payload) });
                hideModal();
                if (body && body.secret) showSecretBanner(body.secret);
                await loadWebhooks();
            }
        } catch (e2) {
            alert('Save failed: ' + e2.message);
        }
    });

    secretCopyBtn?.addEventListener('click', async function () {
        try {
            await navigator.clipboard.writeText(secretValueEl.textContent || '');
            secretCopyBtn.textContent = 'Copied!';
            setTimeout(function () { secretCopyBtn.textContent = 'Copy'; }, 1500);
        } catch (e) {
            alert('Could not copy to clipboard: ' + e.message);
        }
    });
    secretDismissBtn?.addEventListener('click', function () {
        secretBanner.classList.add('hidden');
        secretValueEl.textContent = '';
    });
})();
