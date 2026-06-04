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

## Designed, implementation pending

These have a complete architecture proposal and a phasing plan. Each one
is a multi-day effort and will land in its own release once started.

### Server-side recording chunks (issue #287 c/d)

Stream recording chunks to the server during recording instead of
holding the entire audio blob in browser RAM. Removes the current 200 MB
client-side cap, makes browser-crash recovery reliable on arbitrarily
long recordings, and replaces the soft-stop behaviour with a quota
based on per-user server-side storage.

**Scope:** new `recording_session` table; `/upload/session*` endpoints
for create / chunk-POST / status / finalize / abort; APScheduler-driven
cleanup of expired sessions; client rewire of the recording stack to
stream chunks with retry; resume-on-reload prompt; replacement of the
hard 200 MB cap with a soft warning + absolute hours ceiling.

**Estimated effort:** 3 phases, roughly 5-7 days of focused work.

### Webhooks (issue #275)

Push-based notifications on recording lifecycle events. Eliminates
polling for companion apps (n8n flows, home dashboards, automation
scripts).

**Scope:** per-user webhook endpoints model (multiple per user, named,
toggleable, per-event subscription); HMAC-SHA256 signing with
`Speakr-Signature` / `Speakr-Delivery-Id` headers; retry policy with
exponential backoff and auto-pause; SSRF guard against internal URLs;
account-settings UI for create/edit/test-fire/recent-deliveries; admin
overview; v1 CRUD API with OpenAPI parity.

**Event vocabulary (initial set):** `recording.created`,
`recording.transcription.started`, `recording.transcription.completed`,
`recording.transcription.failed`, `recording.summary.completed`,
`recording.summary.failed`, `recording.events.extracted`,
`recording.updated`, `recording.deleted`.

**Estimated effort:** 3 phases, roughly 2.5 days of focused work.

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
