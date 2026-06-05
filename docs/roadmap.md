# Roadmap

Active areas of work and the longer-running pieces that have a design
proposal but not yet a shipped implementation. This page is updated as
items move from "planned" to "shipping" to "shipped".

If a feature you care about is not on this list, the right place to ask
is a GitHub discussion or issue. Roadmap order reflects design readiness
and community demand, not strict priority.

## Planned for the next release (v0.8.21-alpha)

These are landed locally and waiting on release:

- **Web Share Target for the PWA.** Pick Speakr from your phone's native
  share sheet to send a recording straight in (issue #285).
- **`GET /api/v1/users/me`.** Companion apps and automation flows can
  identify the current user without scraping internal endpoints
  (issue #281).
- **Failed-upload safety net.** When an upload fails, the audio blob is
  persisted to IndexedDB *and* offered as a browser download as a
  defense-in-depth fallback, so the recording never silently
  disappears (issues #297, #287).
- **"New Recording" navigation guard.** Confirmation prompt before
  abandoning an unsaved in-app recording (issue #287).
- **Misleading "Enable Chunking" notification fixed.** Reverse-proxy 413s
  now produce a clear "increase client_max_body_size" message instead
  (issue #283).
- **Chat partial-response preservation.** A reverse-proxy timeout during
  long extended-thinking streams no longer wipes the visible response;
  the partial text stays with a short note appended (issue #282).
- **Multi-file upload responsiveness.** Vue reactivity no longer wraps
  the binary File payload, eliminating the UI freeze when 10+ files
  are queued at once (issue #280).
- **German translation parity.** 200+ entries from the community-supplied
  list applied; all seven locales remain at full parity (issue #286).
- **Auto-created admin marked email-verified.** First-run admin creation
  no longer requires an email-verification round-trip
  (issue #288, PR #289 by checkmeck).

## Built, awaiting release

Both of the features that used to live in this section have been
implemented and are waiting on the next tagged release:

### Server-side recording chunks (issue #287 c/d) — built

Streams recording chunks to the server during recording. The 200 MB
cap is replaced by a configurable hours-based ceiling. Crash recovery
on reload prompts the user to finalize whatever was already uploaded.
Off by default until enabled with `ENABLE_SERVER_RECORDING_CHUNKS=true`.

Full setup, env-var reference, reverse-proxy guidance, and on-disk
layout in [Recording Sessions](admin-guide/recording-sessions.md).

### Webhooks (issue #275) — built

Push-based notifications on recording lifecycle events. Each user
manages their own webhook endpoints from Account settings → Webhooks
(or programmatically via `/api/v1/webhooks`). HMAC-SHA256 signatures,
SSRF guard against private IPs, exponential-backoff retries with
auto-pause after 10 consecutive failures.

Event types, signature verification examples in Python/Node/bash,
retry schedule, and env-var reference in
[Webhooks](admin-guide/webhooks.md).

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
  thought through is the fastest path. The "Designed, implementation
  pending" items above became designed-and-pending because someone
  cared enough to articulate what they needed.

## How releases are versioned

Speakr is in alpha. The version scheme is `v0.MAJOR.MINOR-alpha` where:

- `MAJOR` increments for feature batches.
- `MINOR` increments for patch releases (bug fixes, security patches,
  small targeted features).
- Security patches always ship as their own release, separate from
  feature work, so the security advisory record stays narrowly scoped.
