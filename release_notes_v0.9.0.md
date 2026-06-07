# Release Notes - v0.9.0

The first non-patch release in the v0.8 line graduates Speakr's recording, mobile, and chrome work to a coherent v0.9 baseline. Three big user-facing themes: capturing audio is now multi-platform and properly documented, the mobile app is a first-class member of the design system, and the upload modal stops feeling like a desktop card pasted onto a phone.

## System Audio & Multi-Input Recording

- **Platform detection on click.** Speakr now detects your OS and browser before you try to capture audio and shows a per-OS help guide with the right setup steps for that combination. The capability matrix it encodes is honest about what works: Chrome / Edge on Windows or ChromeOS deliver full system audio via the share dialog; Chrome / Edge on macOS or Linux can only capture tab audio directly through the browser; Firefox and Safari can't capture audio via `getDisplayMedia` at all.
- **Three capture modes.** The Microphone, System Audio, and Mic + System buttons are now styled as a calmer hierarchy — Microphone is the everyday full-width primary button, the other two sit in a secondary tier with small blue / purple icon accents. A small amber dot appears on the System Audio / Mic + System buttons when full system audio isn't expected to work on the current platform.
- **Per-OS setup guide.** A polished help modal opens automatically when capture fails (or on demand from the inline link). It carries macOS BlackHole + Multi-Output Device instructions, the Windows "Share system audio" walkthrough, and the Linux pavucontrol routing options including a one-line `pactl load-module module-virtual-source` that exposes a Pulse monitor as a regular source Chrome will list.
- **Input device picker with secondary mixing.** A new collapsible Input devices section under the recording buttons lets you pick a primary microphone AND an optional "Also mix in" secondary device. When both are picked, Speakr captures two `getUserMedia` streams and mixes them via Web Audio into one MediaRecorder track — the canonical solution for recording your voice plus a meeting's remote participants on macOS or Linux where the browser can't capture full system audio natively. Monitor sources and virtual audio devices are badged in the dropdown.
- **Disable echo cancellation / noise suppression / auto-gain.** A new toggle next to the recording buttons turns off Chrome's default audio processing — necessary when you're routing sustained speech or music through a monitor source / virtual audio device, because the noise suppressor otherwise classifies sustained audio as noise and gates the stream to silence about a second in. The choice is persisted in `localStorage`.
- **Virtual audio device discovery.** Speakr scans installed audio inputs for known virtual routing devices (BlackHole, Loopback, Soundflower, Background Music on macOS; VB-Audio Cable, Voicemeeter, Stereo Mix on Windows; PulseAudio / PipeWire monitor sources on Linux) and surfaces them as detected. The help modal shows a green confirmation when it finds one already wired up on your system.
- **Privacy notes.** The help modal has a collapsible Privacy notes section. It calls out the trade-off honestly: any virtual audio device on any OS creates a new path for sites with mic permission to capture system audio with no screen-sharing indicator. Mitigations are listed (route through the device only while recording; audit mic permissions; use a less obvious device name where possible).

## Stats Tab

- **At-a-glance metrics.** A new Stats tab on the recording detail surface shows total length, speaker count, conversation turns, and word count as headline cards.
- **Per-speaker breakdown.** A table (desktop) or card stack (mobile) shows each speaker's speaking time, percentage of total audio, turn count, words, and words-per-minute. A slim proportion bar visualises the share.
- **Silence row.** Detected silence is shown as its own row / card with the same proportion bar and duration so you can see how much of a meeting was actually quiet.
- **Available on both desktop and mobile.** Desktop surfaces Stats in the right-rail tab strip; mobile surfaces it in the bottom-nav More overflow alongside Notes and Events. Stats only appears when the transcript has speaker diarisation with per-segment timestamps.

## Upload Modal Redesign

- **Real modal.** The upload view is now a proper modal overlay over your recordings list or open detail view, not a full-screen takeover that hides everything else. Backdrop click and Esc dismiss.
- **Progressive disclosure.** Tags, folders, prompt variables, and advanced ASR options are tucked behind a single collapsible Options group with a chip summary of current selections. Most uploads now show as a calm row instead of a wall of form fields.
- **Inline file preview.** Each queued file shows its name, size, AND duration — populated asynchronously by a hidden audio/video element with `preload="metadata"` so only container headers get read, not the whole payload. Video files get a sky-blue video glyph instead of the audio one so you can tell file types at a glance.
- **Sticky modal footer.** Cancel and Upload live in a sticky bar at the bottom of the modal panel so they stay reachable while you scroll through long queues or expand the Options group. Button is enabled only when files are queued.
- **Last-used defaults auto-restore.** After every successful upload, your form choices (tag IDs, folder, language, min/max speakers) are memo-ed to `localStorage`. The next time you add a file to the queue, those values restore automatically — only filling slots you haven't already set this session. Each summary chip has an inline × to clear individual selections.
- **Calmer recording buttons.** The Microphone / System Audio / Mic + System buttons drop the saturated red / blue / purple "stoplight" palette for a neutral background + small coloured icon accents. The Microphone button is full-width as the primary case; System / Mic+System sit on a smaller secondary tier.
- **Mobile bottom-sheet with drag-to-dismiss.** On phones the modal anchors to the bottom of the viewport, takes full width, rounds only the top corners, and slides up from the bottom. Drag the modal header down past 120 px (or 25 % of the viewport) and the sheet animates fully off-screen.
- **Keep audio only for video uploads.** When you upload a video with video retention enabled at the server, a sky-blue toggle lets you drop the video stream and keep only audio. The toggle, the video-file icon on each queue row, and the implicit-mode inline hint all share the same sky-blue palette so the visual link is obvious even when the toggle is off.

## Mobile UI

- **Bottom navigation.** Detail view on mobile uses a 56 px bottom nav with Summary, Transcript, Chat as direct tabs and Notes / Stats / Events tucked into a More overflow when present. Single tap to reach every panel.
- **Single header.** The duplicated mobile title bar is gone — the global app header carries the title, edit pen, and regenerate-title button on every viewport; the per-recording strip below carries participants, status, folder / tag / share pills, and a chevron that expands to extra metadata + the action toolbar.
- **Contextual icons in the chevron row.** The copy / download / edit pen for Summary and Notes, the follow-player and view-mode toggles for Transcript, the calendar-export for Events, and the clear-chat for Chat all share the same row as the chevron. Saves a row of vertical space and gives the actions next to the chevron the user is already aiming for.
- **Edge-to-edge content.** Each panel runs full-width to the screen edges instead of sitting inside a 16 px gutter inside another 16 px gutter — the box-within-a-box feel is gone.
- **Sticky speaker pills.** The mobile transcript now uses the same run-based structure as desktop so the speaker tablet pins to the top of the scroll area for the entire speaker's run instead of disappearing with its segment.
- **Sticky editor footer.** Summary / Notes editing on mobile gets a sticky Cancel / Save footer below the editor so the buttons stay reachable for long content. The toolbar (copy / download / edit) hides entirely while editing so the "edit pen highlights when active" inconsistency is structurally gone.
- **Audio player polish.** Speed / volume / download / fullscreen / video-toggle buttons all use the design-system `.btn--icon.btn--sm` primitive so they share visual rhythm. The volume slider popover opens upward (was opening below the button and off-screen behind the bottom nav). The slider itself is rotation-based for pixel-perfect track / thumb alignment.
- **Progress queue as a bottom sheet.** The processing queue popup is now a full-width bottom sheet anchored above the player + nav strip on mobile (was overlapping them as a 320 px desktop card). Desktop is unchanged.
- **Editor flat corners on phones.** The EasyMDE markdown editor's rounded corners flatten on mobile so it reads as one continuous editing surface from edge to edge.

## Inquire Mode

- **+ New Recording deep-link.** Clicking the New Recording button from inquire mode now opens the main app's upload modal directly via a `?upload=1` query param the main app reads on mount and acts on. Previously it dumped you on the empty list / detail view and made you hunt for the same button again.
- **Design-system polish.** Header, sidebar, chat input, message bubbles, and welcome state all switched from bespoke utility chains to the shared `.btn`, `.field`, `.empty`, `.surface-raised`, `.message.user-message` / `.message.ai-message`, and `.t-*` type primitives so inquire feels like a member of the same family as the rest of the app.
- **Active filters as chips.** The active-filters summary on the sidebar is now a wrap of accent-tinted pills (was a stack of muted bg-tertiary lines) so it reads as "this is active right now" at a glance.

## Design System Unification

- **22 modals on the same chrome.** Account, admin, and inquire pages had a long tail of bespoke modal wrappers. All of them now use `.modal-overlay` + `.modal-panel` + `.modal-header` + `.modal-body` + `.modal-footer` + `.modal-close` with consistent backdrops, animations, and close affordances.
- **Button + field primitives.** `.btn` with `--primary`, `--ghost`, `--danger`, `--icon`, `--sm`, `--lg`, `--quiet` variants and `.field` for form inputs replace the long bespoke Tailwind utility chains that used to drift between pages.
- **Native select dropdowns themed for dark mode.** Browser native `<select>` dropdowns inherit OS / UA defaults which were typically light-on-light on dark themed pages. An explicit `select option` rule using theme tokens means the folder picker, language picker, model picker, ASR options, etc. are all legible in both modes.
- **Header consolidation.** The global header is a single adaptive bar across list view (Speakr logo) and detail view (recording title + edit pen + regenerate-title + status badge + action icons + global controls). No more two-bar stack on detail.
- **Sidebar redesign.** Full-bleed list runs edge-to-edge with consistent hairline dividers between rows and section bands instead of chunky cards. Each row optionally shows a folder pill and speaker count.
- **Floating dockable chat.** The chat panel is a floating, dockable surface that can snap to any of four corners, maximise to fill the viewport, or dock into the right column. Choice is persisted across recordings.
- **Display preferences tab.** A new Display tab in account settings lets you choose whether the desktop audio player sits at the bottom (default) or top of the recording detail surface.

## Polish & Bug Fixes

- **Empty / landing state.** Two flavours of empty state: a friendly "Ready to capture" panel when the recordings list is empty, and a minimal "Select a recording" prompt when items exist but none is open. Both use the design-system primitives.
- **Auto-select neighbour on delete.** Deleting the open recording auto-selects the recording at the same index (or the new last entry) instead of dropping you on a blank surface.
- **Auto-navigate after in-app recording.** When you record via the upload modal, stop, and the upload completes, Speakr now auto-navigates to the new recording's detail view. Bulk drag-drop uploads of many files still leave you where you were.
- **Inbox / highlight icon colour.** The active states (blue inbox, yellow star) now actually paint on both global and mobile headers — the `.btn--icon` rule's `:not()` chain was beating the conditional Tailwind class in the cascade.
- **Folder dropdown anchoring.** The mobile folder picker's hidden `<select>` overlay now anchors correctly under its icon button instead of materialising at viewport (0,0) in some mobile browsers.
- **Toast positioning.** Notifications fire from a fixed `top: calc(--app-header-height + 16px); right: 16px` so they sit below the header instead of overlapping it.
- **Sticky speaker pills (desktop).** Speaker tablets in the simple-view transcript now stick to the top of the scroll area for the entire speaker's run instead of disappearing with their first segment.
- **Speaker pill outlines.** Pills get a softened 1 px outline in the speaker's identity colour. Time half uses a `color-mix` blend that keeps the speaker hue without going to grey.

## Backend & Infrastructure

- **Webhooks Phase 1–3.** Outbound webhook system with HMAC signing, exponential-backoff retry, SSRF protection (DNS resolve, IPv6 site-local rejection), event payloads for `recording.transcription.completed`, `recording.transcription.started`, `events.extracted`, `events.updated`, and the lifecycle events. Account settings → Webhooks tab carries list / edit / test / deliveries / rotate. See admin-guide/webhooks for full setup.
- **Server-side recording sessions.** Long browser recordings now stream chunks to the server in the background instead of holding everything in memory. Hours-based hard ceiling replaces the size-based auto-stop. Resume after a page refresh is supported. `RECORDING_SESSION_MAX_BYTES_PER_USER` is a per-user soft limit.
- **Security fixes.** Folder ownership is validated in the recording-session finalize path (closes IDOR follow-up to #287); tag ownership is validated in the batch PATCH endpoint (closes a parallel IDOR). `SESSION_COOKIE_SECURE` is now env-configurable.
- **Performance.** Eager-load `Recording.folder` and tag associations in v1 list responses; group admin user-list queries into two batches (counts + storage_used) instead of N+1; composite `(status, next_retry_at)` index on `webhook_delivery`; opt-in chunk-commit batching for recording sessions.
- **`/api/v1/users/me`.** New endpoint exposes the authenticated user with their group memberships.
- **PWA Web Share Target.** Install the PWA and your OS can share audio files into Speakr from the share sheet.

## Localization

Full translation coverage refreshed across the seven supported languages (English, French, German, Spanish, Russian, Simplified Chinese, Brazilian Portuguese). All new strings (system audio help, device picker, stats tab, mobile bottom nav, upload modal redesign, webhook UI, recording sessions) have been localized. The German backfill repaired 12 `errors.*` keys that were carrying Chinese text in earlier passes.

## Compatibility

Backwards compatible with v0.8.x. Database migrations run automatically on startup. Existing recordings, tags, folders, groups, and shares are unaffected. Webhook system is opt-in (no webhooks fire until you create a subscription). Recording sessions are an internal backend change with no client-visible behaviour difference except for the longer max-recording ceiling.

Browser support note: full system audio recording requires Chrome or Edge on Windows or ChromeOS. macOS and Linux can capture tab audio out of the box and full system audio via the documented virtual-audio-device workflows. Firefox and Safari can transcribe uploaded audio but cannot capture screen audio via `getDisplayMedia`.
