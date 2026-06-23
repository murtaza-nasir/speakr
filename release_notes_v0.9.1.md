# Release Notes - v0.9.1-alpha

A patch release that hardens the upload path introduced in v0.9.0 and fixes two bugs reported against it. No new features, no configuration changes.

## Bug Fixes

- **Uploads no longer fail with an expired CSRF token.** The file/recording upload path uses `XMLHttpRequest` for progress reporting, which bypassed the fetch-based CSRF refresh. Once the page's token crossed Flask-WTF's one-hour limit (after a long recording, a backgrounded tab, or laptop sleep) the upload returned HTTP 400 and the retry kept failing with the same stale token. The upload path now refreshes the token immediately before sending and retries once on a CSRF rejection, mirroring the existing fetch interceptor. Fixes #310. (Thanks to @fcatuhe, PR #302.)
- **Inquire embeddings are now generated when auto-summarization is enabled.** Semantic-search chunks were only built in the non-summary completion path, so with auto-summarization on, new recordings completed without ever being embedded and stayed invisible to Inquire mode until an admin ran "Re-embed all". Summary completion now runs the same chunking step. Recordings created before this release still need a one-time re-embed; new uploads embed automatically. Fixes #305. (Thanks to @checkmeck for the diagnosis.)
- **API token modals open again.** An unclosed `<div>` in the Account page nested the Create Token and token-secret modals inside the hidden folder modal, so clicking Create Token did nothing. Fixes the markup so the token modals render. (Thanks to @fcatuhe, PR #308.)

## Upload Resilience

- **Stalled uploads now fail into the recovery path instead of hanging.** `XMLHttpRequest` had no timeout set, so a dropped-but-not-closed socket fired neither `ontimeout` nor `onerror` and the upload hung forever. A size-scaled timeout (a ten-minute floor plus one minute per 10 MB) now routes a stall through the same failure and recovery handling as any other upload error. (Thanks to @jjsmackay, PR #306.)
- **A warning now appears before leaving the page during an in-flight upload.** Closing or reloading the tab mid-upload triggers the browser's "leave site?" prompt when any upload is still queued, ready, or uploading. (Thanks to @jjsmackay, PR #307.)

## Compatibility

Backwards compatible with v0.8.x and v0.9.0. Database migrations run automatically on startup. Upgrade with the usual `docker compose pull && docker compose up -d`.
