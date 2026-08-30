/**
 * Shared driver for the Tag / Folder editor modals
 * (templates/includes/organizer_modals.html).
 *
 * Two consumers:
 *  - account.html (full management UI). Its inline script owns the modals'
 *    open/edit/submit lifecycle and uses only the shared helpers exported
 *    here (payload builders, template-dropdown filler), so the request
 *    payloads can never drift between pages.
 *  - index.html (the main app). No management UI of its own, so this module
 *    runs in STANDALONE mode there: `openTagCreate` / `openFolderCreate`
 *    open the same modals in creation mode, submit with the full field set,
 *    and hand the created entity back through `onSaved` so the caller (the
 *    upload dialog) can apply it in place.
 *
 * Standalone mode self-arms only on pages WITHOUT the account grids, so the
 * two sets of submit handlers can never double-fire.
 */
(function () {
    'use strict';

    const isAccountPage = () => !!document.getElementById('tagsGrid');

    function csrfHeaders() {
        const headers = { 'Content-Type': 'application/json' };
        const token = document.querySelector('meta[name=csrf-token]');
        if (token) headers['X-CSRFToken'] = token.getAttribute('content');
        return headers;
    }

    // ------------------------------------------------------------------
    // Shared helpers (used by BOTH the account page and standalone mode)
    // ------------------------------------------------------------------

    /**
     * Fill a template <select> from an API listing. opts.selected sets an
     * explicit value (edit flows); otherwise the current value is preserved
     * (repopulation). opts.markDefault appends " (Default)".
     */
    async function loadTemplateOptionsInto(selectId, url, defaultLabel, opts = {}) {
        const select = document.getElementById(selectId);
        if (!select) return;
        try {
            const response = await fetch(url);
            if (!response.ok) return;
            const templates = await response.json();
            const previous = select.value;
            select.textContent = '';
            const defaultOpt = document.createElement('option');
            defaultOpt.value = '';
            defaultOpt.textContent = defaultLabel;
            select.appendChild(defaultOpt);
            templates.forEach((template) => {
                const option = document.createElement('option');
                option.value = template.id;
                option.textContent = template.name + (opts.markDefault && template.is_default ? ' (Default)' : '');
                if (template.description) option.title = template.description;
                select.appendChild(option);
            });
            const wanted = opts.selected !== undefined && opts.selected !== null
                ? String(opts.selected) : previous;
            if (wanted) select.value = wanted;
        } catch (error) {
            console.error('Error loading template options for ' + selectId + ':', error);
        }
    }

    /** Assemble the tag create/update payload from the tag modal form. */
    function buildTagPayload(tagForm) {
        const formData = new FormData(tagForm);

        const isProtected = document.getElementById('tagProtectFromDeletion')?.checked || false;
        let retentionDays = null;
        if (isProtected) {
            retentionDays = -1; // protected / infinite retention
        } else {
            const retentionInput = formData.get('retention_days');
            retentionDays = retentionInput ? parseInt(retentionInput) : null;
        }

        const namingTemplateId = formData.get('naming_template_id');
        const exportTemplateId = formData.get('export_template_id');
        const tagData = {
            name: formData.get('name'),
            color: formData.get('color'),
            custom_prompt: formData.get('custom_prompt') || null,
            naming_template_id: namingTemplateId ? parseInt(namingTemplateId) : null,
            export_template_id: exportTemplateId ? parseInt(exportTemplateId) : null,
            default_language: formData.get('default_language') || null,
            default_min_speakers: formData.get('default_min_speakers') ? parseInt(formData.get('default_min_speakers')) : null,
            default_max_speakers: formData.get('default_max_speakers') ? parseInt(formData.get('default_max_speakers')) : null,
            default_hotwords: formData.get('default_hotwords') || null,
            default_initial_prompt: formData.get('default_initial_prompt') || null,
            default_transcription_model: formData.get('default_transcription_model') || null,
            retention_days: retentionDays,
            is_auto_process: document.getElementById('tagAutoProcess')?.checked || false,
        };

        const groupId = formData.get('group_id');
        if (groupId) {
            tagData.group_id = parseInt(groupId);
            tagData.auto_share_on_apply = document.getElementById('tagAutoShareOnApply')?.checked || false;
            tagData.share_with_group_lead = document.getElementById('tagShareWithGroupLead')?.checked || false;
        } else {
            tagData.group_id = null;
        }
        return tagData;
    }

    /** Assemble the folder create/update payload from the folder modal form. */
    function buildFolderPayload() {
        const val = (id) => {
            const el = document.getElementById(id);
            return el ? el.value.trim() || null : null;
        };
        const intVal = (id) => {
            const el = document.getElementById(id);
            return el && el.value ? parseInt(el.value) : null;
        };

        const folderData = {
            name: (document.getElementById('folderName')?.value || '').trim(),
            color: document.getElementById('folderColor')?.value,
            custom_prompt: val('folderCustomPrompt'),
            default_language: val('folderLanguage'),
            default_min_speakers: intVal('folderMinSpeakers'),
            default_max_speakers: intVal('folderMaxSpeakers'),
            default_hotwords: val('folderHotwords'),
            default_initial_prompt: val('folderInitialPrompt'),
            default_transcription_model: val('folderTranscriptionModel'),
            naming_template_id: intVal('folderNamingTemplate'),
            export_template_id: intVal('folderExportTemplate'),
        };

        const groupIdEl = document.getElementById('folderGroupId');
        if (groupIdEl && groupIdEl.value) {
            folderData.group_id = parseInt(groupIdEl.value);
            const autoShareEl = document.getElementById('folderAutoShareOnApply');
            folderData.auto_share_on_apply = autoShareEl ? autoShareEl.checked : true;
            const shareLeadEl = document.getElementById('folderShareWithGroupLead');
            folderData.share_with_group_lead = shareLeadEl ? shareLeadEl.checked : true;
        }

        const protect = document.getElementById('folderProtectFromDeletion');
        const retention = document.getElementById('folderRetentionDays');
        if (protect && protect.checked) {
            folderData.retention_days = -1;
        } else if (retention && retention.value) {
            folderData.retention_days = parseInt(retention.value);
        }
        return folderData;
    }

    function switchOrganizerModalTab(navId, tabClass, contentClass, targetTabId) {
        const nav = document.getElementById(navId);
        if (!nav) return;
        nav.querySelectorAll('.' + tabClass).forEach((btn) => {
            if (btn.dataset.tab === targetTabId) {
                btn.classList.add('active', 'border-[var(--border-focus)]', 'text-[var(--text-accent)]');
                btn.classList.remove('border-transparent', 'text-[var(--text-muted)]');
            } else {
                btn.classList.remove('active', 'border-[var(--border-focus)]', 'text-[var(--text-accent)]');
                btn.classList.add('border-transparent', 'text-[var(--text-muted)]');
            }
        });
        document.querySelectorAll('.' + contentClass).forEach((panel) => {
            panel.classList.toggle('hidden', panel.id !== targetTabId);
        });
    }

    // ------------------------------------------------------------------
    // Standalone creation mode (main app: inline create from upload dialog)
    // ------------------------------------------------------------------

    let standaloneArmed = false;
    let activeSubmit = null; // { form, handler } while a standalone modal is open

    function adminGroups() {
        try {
            const holder = document.getElementById('organizerModals');
            return JSON.parse(holder?.dataset.adminGroups || '[]');
        } catch (_) {
            return [];
        }
    }

    /** Translate a key via the global i18n bundle, falling back to English. */
    function tr(key, fallback, params) {
        if (window.i18n && typeof window.i18n.t === 'function') {
            const translated = window.i18n.t(key, params);
            if (translated && translated !== key) return translated;
        }
        return fallback;
    }

    function armStandalone() {
        if (standaloneArmed || isAccountPage()) return;
        standaloneArmed = true;

        // The main app only translates Vue-rendered text; apply the included
        // modal markup's data-i18n attributes here (i18n is initialized well
        // before any user click can reach this).
        if (window.i18n && typeof window.i18n.t === 'function') {
            const root = document.getElementById('organizerModals');
            root?.querySelectorAll('[data-i18n]').forEach((el) => {
                const key = el.getAttribute('data-i18n');
                const translated = window.i18n.t(key);
                if (translated && translated !== key) el.textContent = translated;
            });
            root?.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
                const key = el.getAttribute('data-i18n-placeholder');
                const translated = window.i18n.t(key);
                if (translated && translated !== key) el.placeholder = translated;
            });
        }
        // Relocalize the language dropdowns now that i18n is ready (the
        // initial DOMContentLoaded population can predate i18n.init).
        populateLanguageSelects();

        // Modal tab strips (the account page binds its own copies of these).
        document.getElementById('tagModalTabs')?.addEventListener('click', (e) => {
            const btn = e.target.closest('.tag-modal-tab');
            if (btn && btn.dataset.tab) {
                switchOrganizerModalTab('tagModalTabs', 'tag-modal-tab', 'tag-modal-tab-content', btn.dataset.tab);
            }
        });
        document.getElementById('folderModalTabs')?.addEventListener('click', (e) => {
            const btn = e.target.closest('.folder-modal-tab');
            if (btn && btn.dataset.tab) {
                switchOrganizerModalTab('folderModalTabs', 'folder-modal-tab', 'folder-modal-tab-content', btn.dataset.tab);
            }
        });

        // Close buttons and group-section visibility toggles.
        document.getElementById('closeTagModalBtn')?.addEventListener('click', () => hideModal('tagModal'));
        document.getElementById('cancelTagBtn')?.addEventListener('click', () => hideModal('tagModal'));
        document.getElementById('closeFolderModalBtn')?.addEventListener('click', () => hideModal('folderModal'));
        document.getElementById('cancelFolderBtn')?.addEventListener('click', () => hideModal('folderModal'));
        document.getElementById('tagGroupId')?.addEventListener('change', function () {
            document.getElementById('tagGroupSettingsSection')?.classList.toggle('hidden', !this.value);
        });
        document.getElementById('folderGroupId')?.addEventListener('change', function () {
            document.getElementById('folderGroupSettingsSection')?.classList.toggle('hidden', !this.value);
        });
    }

    function hideModal(id) {
        document.getElementById(id)?.classList.add('hidden');
        if (activeSubmit) {
            activeSubmit.form.removeEventListener('submit', activeSubmit.handler);
            activeSubmit = null;
        }
    }

    function populateGroupSelect(selectId, sectionId, tabBtnId) {
        const groups = adminGroups();
        const select = document.getElementById(selectId);
        if (select) {
            while (select.options.length > 1) select.remove(1);
            groups.forEach((group) => {
                const option = document.createElement('option');
                option.value = group.id;
                option.textContent = group.name;
                select.appendChild(option);
            });
            select.value = '';
        }
        // Sharing tab / group section only make sense with admin groups.
        const tabBtn = tabBtnId && document.getElementById(tabBtnId);
        if (tabBtn) tabBtn.classList.toggle('hidden', groups.length === 0);
        const section = sectionId && document.getElementById(sectionId);
        if (section) section.classList.toggle('hidden', groups.length === 0);
    }

    function bindSubmit(form, handler) {
        if (activeSubmit) activeSubmit.form.removeEventListener('submit', activeSubmit.handler);
        activeSubmit = { form, handler };
        form.addEventListener('submit', handler);
    }

    /** Open the tag modal in creation mode (standalone pages). */
    function openTagCreate(opts = {}) {
        armStandalone();
        const form = document.getElementById('tagForm');
        const overlay = document.getElementById('tagModal');
        if (!form || !overlay) return;
        form.reset();
        document.getElementById('tagId').value = '';
        if (opts.name) document.getElementById('tagName').value = opts.name;
        const title = document.getElementById('tagModalTitle');
        if (title) title.textContent = tr('editTagModal.createTitle', 'Create Tag');
        const save = document.getElementById('saveTagBtn');
        if (save) {
            save.textContent = '';
            const icon = document.createElement('i');
            icon.className = 'fas fa-plus mr-2';
            save.appendChild(icon);
            save.appendChild(document.createTextNode(' ' + tr('editTagModal.createTitle', 'Create Tag')));
        }
        populateGroupSelect('tagGroupId', 'tagGroupSelectionSection', 'tagTabSharingBtn');
        document.getElementById('tagGroupSettingsSection')?.classList.add('hidden');
        switchOrganizerModalTab('tagModalTabs', 'tag-modal-tab', 'tag-modal-tab-content', 'tagTabTranscription');
        loadTemplateOptionsInto('tagNamingTemplate', '/api/naming-templates', 'No template (use user default or AI title)', { selected: '' });
        loadTemplateOptionsInto('tagExportTemplate', '/api/export-templates', 'No template (use user default)', { selected: '', markDefault: true });

        bindSubmit(form, async (e) => {
            e.preventDefault();
            try {
                const response = await fetch('/api/tags', {
                    method: 'POST',
                    headers: csrfHeaders(),
                    body: JSON.stringify(buildTagPayload(form)),
                });
                if (!response.ok) {
                    const errorData = await response.json().catch(() => ({}));
                    throw new Error(errorData.error || ('HTTP ' + response.status));
                }
                const saved = await response.json();
                hideModal('tagModal');
                if (opts.onSaved) opts.onSaved(saved);
            } catch (error) {
                alert('Could not create tag: ' + error.message);
            }
        });
        overlay.classList.remove('hidden');
        setTimeout(() => document.getElementById('tagName')?.focus(), 50);
    }

    /** Open the folder modal in creation mode (standalone pages). */
    function openFolderCreate(opts = {}) {
        armStandalone();
        const form = document.getElementById('folderForm');
        const overlay = document.getElementById('folderModal');
        if (!form || !overlay) return;
        form.reset();
        document.getElementById('folderId').value = '';
        if (opts.name) document.getElementById('folderName').value = opts.name;
        const title = document.getElementById('folderModalTitle');
        if (title) title.textContent = tr('folderManagement.createFolderTitle', 'Create Folder');
        const save = document.getElementById('saveFolderBtn');
        if (save) {
            save.textContent = '';
            const icon = document.createElement('i');
            icon.className = 'fas fa-plus mr-2';
            save.appendChild(icon);
            const label = document.createElement('span');
            label.textContent = tr('folderManagement.createFolderTitle', 'Create Folder');
            save.appendChild(document.createTextNode(' '));
            save.appendChild(label);
        }
        populateGroupSelect('folderGroupId', 'folderGroupSelectionSection', 'folderTabSharingBtn');
        document.getElementById('folderGroupSettingsSection')?.classList.add('hidden');
        switchOrganizerModalTab('folderModalTabs', 'folder-modal-tab', 'folder-modal-tab-content', 'folderTabTranscription');
        loadTemplateOptionsInto('folderNamingTemplate', '/api/naming-templates', 'No template (use user default or AI title)', { selected: '' });
        loadTemplateOptionsInto('folderExportTemplate', '/api/export-templates', 'No template (use user default)', { selected: '', markDefault: true });

        bindSubmit(form, async (e) => {
            e.preventDefault();
            const name = (document.getElementById('folderName')?.value || '').trim();
            if (!name) return;
            try {
                const response = await fetch('/api/folders', {
                    method: 'POST',
                    headers: csrfHeaders(),
                    body: JSON.stringify(buildFolderPayload()),
                });
                if (!response.ok) {
                    const errorData = await response.json().catch(() => ({}));
                    throw new Error(errorData.error || ('HTTP ' + response.status));
                }
                const saved = await response.json();
                hideModal('folderModal');
                if (opts.onSaved) opts.onSaved(saved);
            } catch (error) {
                alert('Could not create folder: ' + error.message);
            }
        });
        overlay.classList.remove('hidden');
        setTimeout(() => document.getElementById('folderName')?.focus(), 50);
    }

    // ------------------------------------------------------------------
    // Default Language selects: localized dropdown instead of free-text
    // codes, fed from the shared list (asr-languages.js) on both pages.
    // ------------------------------------------------------------------

    function populateLanguageSelects() {
        if (!window.SpeakrASRLanguages) return;
        const emptyLabel = tr('form.autoDetect', 'Auto detect');
        window.SpeakrASRLanguages.populateSelect(document.getElementById('tagLanguage'), emptyLabel);
        window.SpeakrASRLanguages.populateSelect(document.getElementById('folderLanguage'), emptyLabel);
    }

    /** Select a stored language code, keeping unknown legacy codes intact. */
    function setLanguageValue(selectId, value) {
        const select = document.getElementById(selectId);
        if (!select) return;
        if (window.SpeakrASRLanguages) {
            if (!select.options.length) populateLanguageSelects();
            window.SpeakrASRLanguages.setValue(select, value);
        } else {
            select.value = value || '';
        }
    }

    // ------------------------------------------------------------------
    // Speaker count entry (#362): Range / Exact toggle in the tag and
    // folder editors. Canonical storage stays min/max (a single count N is
    // min == max == N), so the payload builders are untouched: the single
    // input just syncs both hidden range fields. The toggle follows the
    // user's persisted preference, except that opening an entity whose
    // stored min != max forces range mode for that open so data is never
    // silently collapsed.
    // ------------------------------------------------------------------

    function _speakerEls(prefix) {
        const root = document.querySelector(`.org-speaker-controls[data-speaker-prefix="${prefix}"]`);
        if (!root) return null;
        return {
            root,
            single: root.querySelector('.org-speaker-single'),
            range: root.querySelector('.org-speaker-range'),
            exactInput: document.getElementById(prefix + 'SpeakersExact'),
            minInput: document.getElementById(prefix + 'MinSpeakers'),
            maxInput: document.getElementById(prefix + 'MaxSpeakers'),
            buttons: root.querySelectorAll('.org-speaker-mode'),
        };
    }

    function _prefMode() {
        const holder = document.getElementById('organizerModals');
        return holder?.dataset.speakerCountMode === 'single' ? 'single' : 'range';
    }

    function _applySpeakerMode(els, mode) {
        els.single.classList.toggle('hidden', mode !== 'single');
        els.range.classList.toggle('hidden', mode === 'single');
        els.buttons.forEach((btn) => {
            const active = btn.dataset.mode === mode;
            btn.classList.toggle('bg-[var(--bg-accent)]', active);
            btn.classList.toggle('text-[var(--text-accent)]', active);
            btn.classList.toggle('font-medium', active);
            btn.classList.toggle('text-[var(--text-muted)]', !active);
        });
    }

    /** Sync display state from the (authoritative) min/max inputs. */
    function refreshSpeakerControls() {
        for (const prefix of ['tag', 'folder']) {
            const els = _speakerEls(prefix);
            if (!els) continue;
            const mn = els.minInput.value, mx = els.maxInput.value;
            els.exactInput.value = (mn && mn === mx) ? mn : (mx || mn || '');
            // Stored range (min != max) wins over the preference for this open.
            const mode = (mn && mx && mn !== mx) ? 'range' : _prefMode();
            _applySpeakerMode(els, mode);
        }
    }

    function armSpeakerControls() {
        const holder = document.getElementById('organizerModals');
        if (!holder) return;
        for (const prefix of ['tag', 'folder']) {
            const els = _speakerEls(prefix);
            if (!els) continue;
            els.exactInput.addEventListener('input', () => {
                els.minInput.value = els.exactInput.value;
                els.maxInput.value = els.exactInput.value;
            });
            els.buttons.forEach((btn) => btn.addEventListener('click', () => {
                const mode = btn.dataset.mode;
                _applySpeakerMode(els, mode);
                if (mode === 'single') {
                    // Entering single mode collapses an existing range to its max.
                    const v = els.maxInput.value || els.minInput.value || '';
                    els.exactInput.value = v;
                    els.minInput.value = v;
                    els.maxInput.value = v;
                }
                holder.dataset.speakerCountMode = mode;
                fetch('/api/user/preferences', {
                    method: 'POST',
                    headers: csrfHeaders(),
                    body: JSON.stringify({ speaker_count_mode: mode }),
                }).catch(() => {});
            }));
        }
        // Refresh whenever a modal opens (account edit/create and standalone
        // create all toggle the overlay's hidden class).
        for (const id of ['tagModal', 'folderModal']) {
            const overlay = document.getElementById(id);
            if (!overlay) continue;
            new MutationObserver(() => {
                if (!overlay.classList.contains('hidden')) refreshSpeakerControls();
            }).observe(overlay, { attributes: true, attributeFilter: ['class'] });
        }
        refreshSpeakerControls();
    }

    function _initShared() {
        populateLanguageSelects();
        armSpeakerControls();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _initShared);
    } else {
        _initShared();
    }
    // Repopulate with relocalized labels once i18n resolves the locale
    // (initial population can run before i18n.init finishes). Values are
    // preserved across repopulation.
    window.addEventListener('localeChanged', populateLanguageSelects);

    window.SpeakrOrganizer = {
        loadTemplateOptionsInto,
        buildTagPayload,
        buildFolderPayload,
        openTagCreate,
        openFolderCreate,
        setLanguageValue,
        populateLanguageSelects,
    };
    // The account page's inline script calls this by its bare name.
    window.loadTemplateOptionsInto = loadTemplateOptionsInto;
})();
