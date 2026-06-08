# Roadmap

Active areas of work and the longer-running pieces that have a design
proposal but not yet a shipped implementation. This page is updated as
items move from "planned" to "shipping" to "shipped".

If a feature you care about is not on this list, the right place to ask
is a GitHub discussion or issue. Roadmap order reflects design readiness
and community demand, not strict priority.

## Shipped in v0.9.0

The first non-patch release in the v0.8 line. Everything in this
section is released and available. Full detail in the
[v0.9.0 release notes](https://github.com/murtaza-nasir/speakr/blob/master/release_notes_v0.9.0.md).

### Recording and capture

- **Multi-platform system audio capture.** Platform detection on click
  with a per-OS help guide (macOS BlackHole + Multi-Output Device,
  Windows "Share system audio", Linux pavucontrol + a one-line
  `pactl load-module module-virtual-source`). An honest capability
  matrix flags when full system audio isn't expected to work on the
  current browser / OS.
- **Multi-input recording.** A new Input devices picker lets you choose
  a primary microphone AND an optional "Also mix in" secondary device;
  Speakr mixes both streams via Web Audio into one track — the canonical
  way to record your voice plus a meeting's remote participants where the
  browser can't capture full system audio natively. Includes a toggle to
  disable Chrome's echo cancellation / noise suppression / auto-gain and
  virtual-audio-device discovery.
- **Server-side recording sessions (issue #287 c/d).** Long browser
  recordings stream chunks to the server during recording instead of
  holding everything in memory. The size-based cap is replaced by a
  configurable hours-based ceiling, and a page refresh prompts you to
  resume / finalize whatever was already uploaded.
  `RECORDING_SESSION_MAX_BYTES_PER_USER` is a per-user soft limit. Full
  setup, env-var reference, reverse-proxy guidance, and on-disk layout
  in [Recording Sessions](admin-guide/recording-sessions.md).
- **Failed-upload safety net.** When an upload fails, the audio blob is
  persisted to IndexedDB *and* offered as a browser download as a
  defense-in-depth fallback, so the recording never silently disappears
  (issues #297, #287).

### Interface

- **Upload-modal redesign.** The upload view is a real modal overlay
  (not a full-screen takeover) with progressive disclosure of Options
  behind a chip summary, inline file preview with duration probe, a
  sticky-footer Upload action, last-used tag / folder / language
  auto-restore with clearable chips, and a mobile bottom-sheet with
  drag-to-dismiss.
- **Mobile UI rebuild.** The detail view on mobile is now a first-class
  member of the design system: 56 px bottom navigation, contextual icons
  in the chevron row, edge-to-edge content, sticky speaker pills, a
  sticky editor Cancel / Save footer, and audio-player polish.
- **Stats tab.** A per-recording tab showing total length, speaker count,
  turns, and word count as headline cards, plus a per-speaker
  time / % / turns / words / WPM breakdown and a silence row. Available
  on desktop and mobile.
- **Inquire `?upload=1` deep-link.** The "+ New Recording" button in
  inquire mode now opens the main app's upload modal directly via a
  query param instead of dropping you on the empty list.
- **Audio-player position preference.** A new Display tab in account
  settings lets you choose whether the desktop audio player sits at the
  bottom (default) or top of the recording detail surface.
- **Design-system unification.** 22 bespoke modals moved onto shared
  `.modal-*` primitives; `.btn` + `.field` primitives across the app;
  native `<select>` dropdowns themed for dark mode; header consolidation;
  sidebar redesign; floating dockable chat panel.

### Backend and API

- **Webhooks (issue #275).** HMAC-SHA256-signed outbound notifications on
  recording lifecycle events. Each user manages their own webhook
  endpoints from Account settings → Webhooks (or programmatically via
  `/api/v1/webhooks`), with an SSRF guard against private IPs and
  exponential-backoff retries that auto-pause after repeated failures.
  Event types, signature-verification examples in Python / Node / bash,
  retry schedule, and env-var reference in
  [Webhooks](admin-guide/webhooks.md).
- **`GET /api/v1/users/me` (issue #281).** Companion apps and automation
  flows can identify the current user — and their group memberships —
  without scraping internal endpoints.
- **PWA Web Share Target (issue #285).** Install the PWA and pick Speakr
  from your phone's native share sheet to send a recording straight in.
- **Auto speaker labelling honoring the user threshold.** Automatic
  speaker labelling now respects the user-configured confidence
  threshold rather than a hard-coded default.

**Known follow-up — debounce `recording.updated`.** Rapid edits
(notes autosave, retitling, tag changes) currently emit one
`recording.updated` event per mutation. A 30s per-recording debounce
window was in the original design and is planned for a later release.
Receivers that want to deduplicate today can group on
`(recording_id, fields_changed)` within a short window.

## Open ideas (not yet designed)

Feature requests that are on the radar but have not been worked through
in detail yet:

- **Watch-folder targeting.** Map a watch directory to a specific
  recording folder so files picked up by that folder are auto-routed
  (discussion #276).
- **Read-only watch folders.** Process audio files in place without
  moving them, useful for preservation projects and externally-managed
  storage (discussion #277).
- **Worker priority / fallback routing for hybrid transcription setups.**
  Schema changes required for proper worker identity tracking (issue
  #255).
- **File attachments on recordings.** Attach slides, related docs, or
  other supporting material to a recording (issue #174).

## How to influence priority

- Open a feature request issue or a discussion describing the use case
  and the workflow it would enable. The richer the use case, the easier
  it is to design for.
- Reactions on existing issues / discussions help signal demand.
- For larger features, a design proposal in a discussion that you have
  thought through is the fastest path. Several items now shipped in
  v0.9.0 — webhooks and server-side recording sessions among them —
  reached design-and-build because someone cared enough to articulate
  what they needed.

## How releases are versioned

Speakr is in alpha. The version scheme is `v0.MINOR.PATCH`, with
releases through v0.8.x carrying an `-alpha` suffix:

- `MINOR` increments for larger feature batches. v0.9.0 was the first
  such bump in the v0.8 line — a coherent baseline rolling up the
  recording, mobile, and design-system work rather than another patch.
- `PATCH` increments for bug fixes, security patches, and small targeted
  features on top of the current minor.
- Security patches always ship as their own release, separate from
  feature work, so the security advisory record stays narrowly scoped.
