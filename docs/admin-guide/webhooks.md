# Webhooks

Speakr can POST a signed JSON envelope to any HTTPS URL when a
recording's lifecycle changes. Use it to push events into automation
flows (n8n, Make, Zapier), home dashboards, or any service that
prefers push over polling `GET /api/v1/recordings`.

Webhooks are configured per-user from **Account settings → Webhooks**,
or programmatically via the `/api/v1/webhooks` API.

## Event vocabulary

| Event | Fired when | Payload `data` fields |
|---|---|---|
| `recording.created` | A recording row is created (upload arrived) | `recording_id`, `title`, `file_size`, `original_filename` |
| `recording.transcription.started` | Worker picks up a transcribe or reprocess-transcription job and the audio file is on disk | `recording_id`, `title` |
| `recording.transcription.completed` | Transcription job finished successfully | `recording_id`, `title`, `language`, `audio_duration_seconds`, `transcription_duration_seconds` |
| `recording.transcription.failed` | Transcription failed permanently (retries exhausted) | `recording_id`, `title`, `error` |
| `recording.summary.completed` | Summary generated successfully | `recording_id`, `title`, `summarization_duration_seconds` |
| `recording.summary.failed` | Summary failed permanently | `recording_id`, `title`, `error` |
| `recording.events.extracted` | Calendar event extraction finished and produced at least one event | `recording_id`, `title`, `events_count` |
| `recording.updated` | Title, participants, notes, summary, meeting_date, is_inbox, is_highlighted, or folder_id changed via `PATCH /api/v1/recordings/{id}` | `recording_id`, `title`, `fields_changed` (list of strings) |
| `recording.deleted` | Recording removed | `recording_id`, `title` |
| `webhook.test` | Synthetic event from the **Test** button or `POST /api/v1/webhooks/{id}/test` | `reason`, `webhook_id` |

All events listed above fire in the current backend. The
`recording.transcription.started` event fires only after the audio
file's existence on disk is confirmed, so subscribers don't see
misleading started→failed sequences for jobs that abort immediately
(e.g. the audio file was deleted between upload and worker pickup).

The `recording.updated` event is **not currently debounced**. Rapid
edits (e.g. notes autosave, drag-drop tag changes) emit one event per
mutation. Receivers that want to coalesce can deduplicate on
`(recording_id, fields_changed)` within a short window. A built-in
debounce is planned for a later release (see `docs/roadmap.md`).

## Envelope

Every delivery is a `POST` with a JSON body shaped like:

```json
{
  "id": "f4e6a4e1-3b9b-4a04-9d4f-0e7a5d8b3c10",
  "type": "recording.transcription.completed",
  "timestamp": "2026-06-04T15:23:11.124Z",
  "user_id": 42,
  "data": {
    "recording_id": 9173,
    "title": "Q3 planning",
    "language": "en",
    "audio_duration_seconds": 3624.7
  }
}
```

Headers:

| Header | Purpose |
|---|---|
| `Content-Type: application/json` | Body format |
| `User-Agent: Speakr-Webhook/1.0` | Identifies Speakr to receivers |
| `Speakr-Event` | The event type — useful for routing without parsing the body |
| `Speakr-Delivery-Id` | UUID echo of `data.id` — receivers use it for idempotency |
| `Speakr-Timestamp` | ISO-8601 UTC of dispatch — receivers may reject stale deliveries |
| `Speakr-Signature` | `sha256=<hex>` HMAC of the raw body with the webhook's secret |

## Signature verification

Every receiver must verify the `Speakr-Signature` header before
trusting the body. Failure to verify means anyone who guesses the URL
can forge events.

### Python

```python
import hmac
import hashlib

def verify_speakr(secret: str, raw_body: bytes, signature_header: str) -> bool:
    if not signature_header.startswith('sha256='):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    given = signature_header[len('sha256='):]
    return hmac.compare_digest(expected, given)
```

### Node.js

```js
const crypto = require('crypto');

function verifySpeakr(secret, rawBody, signatureHeader) {
    if (!signatureHeader || !signatureHeader.startsWith('sha256=')) return false;
    const expected = crypto.createHmac('sha256', secret).update(rawBody).digest('hex');
    const given = signatureHeader.slice('sha256='.length);
    try {
        return crypto.timingSafeEqual(Buffer.from(expected, 'hex'), Buffer.from(given, 'hex'));
    } catch (_) {
        return false;
    }
}
```

### Bash (for sanity-checking)

```bash
echo -n "$RAW_BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print "sha256="$2}'
```

Compare the output against the `Speakr-Signature` header value.

## Retry policy

Delivery attempts use the following backoff schedule:

| Attempt | Delay before this attempt |
|---|---|
| 1 | immediate |
| 2 | 30 s |
| 3 | 2 min |
| 4 | 10 min |
| 5 | 1 hour |

After attempt 5, status flips to `permanent_failure` and the webhook's
`consecutive_failures` counter increments. When it reaches
`WEBHOOK_AUTOPAUSE_FAILURES` (default 10) the webhook is auto-paused; the
user must manually re-enable it.

**Retryable HTTP responses:** 408, 429, 5xx, plus network errors and
timeouts.
**Non-retryable:** 2xx (success), 3xx (we disable `allow_redirects` on
purpose), 4xx other than 408/429.

A successful delivery (2xx) resets `consecutive_failures` to 0.

## SSRF guard

Webhook URLs are validated at save time and again at dispatch time:

- Scheme must be `http://` or `https://`. `http://` is rejected unless
  the webhook has `allow_http=true`.
- The hostname is resolved; if any returned address is private
  (RFC 1918, link-local, loopback, multicast, reserved), the URL is
  rejected.
- Operators can carve out internal hosts via
  `WEBHOOK_INTRANET_HOST_ALLOWLIST` — a regex matched against the
  hostname.

This prevents accidentally pointing a webhook at an internal service
(metadata endpoints, admin consoles) that should not receive Speakr
payloads.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `WEBHOOK_GLOBAL_ENABLED` | `true` | Admin kill switch. Set to `false` to disable all dispatch system-wide. |
| `WEBHOOK_MAX_PER_USER` | `10` | Hard cap on webhooks per user. |
| `WEBHOOK_DELIVERY_TIMEOUT_SECONDS` | `10` | Per-attempt HTTP timeout. |
| `WEBHOOK_MAX_ATTEMPTS` | `5` | Retry cap before `permanent_failure`. |
| `WEBHOOK_AUTOPAUSE_FAILURES` | `10` | Consecutive failures before auto-pause. |
| `WEBHOOK_DISPATCHER_INTERVAL_SECONDS` | `5` | How often the dispatcher polls for due deliveries. |
| `WEBHOOK_INTRANET_HOST_ALLOWLIST` | empty | Regex of allowed private hosts. Empty = SSRF block always applies. |

## API surface

All endpoints under `/api/v1/webhooks` require an authenticated session
or an API token. The OpenAPI schema documents every field.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/webhooks` | List the caller's webhooks. Returns `event_types` + `max_per_user` for UI rendering. |
| POST | `/api/v1/webhooks` | Create a webhook. Response includes the secret **once**; capture it. |
| GET | `/api/v1/webhooks/{id}` | Read one. Secret is never returned. |
| PATCH | `/api/v1/webhooks/{id}` | Update name / url / events / enabled / allow_http. |
| DELETE | `/api/v1/webhooks/{id}` | Delete (cascades to deliveries). |
| POST | `/api/v1/webhooks/{id}/rotate-secret` | Generate a fresh HMAC secret. Returned once. |
| POST | `/api/v1/webhooks/{id}/test` | Queue a synthetic `webhook.test` delivery. |
| GET | `/api/v1/webhooks/{id}/deliveries` | Recent deliveries (default 50, max 200). |
| GET | `/api/v1/webhooks/{id}/deliveries/{did}` | Full delivery record including the original payload. |
| POST | `/api/v1/webhooks/{id}/deliveries/{did}/replay` | Re-fire the payload as a new delivery. |

## Operational notes

- The dispatcher runs in a daemon thread inside the Speakr web process.
  In multi-worker Gunicorn setups, every worker runs its own dispatcher;
  the dispatcher polls the database with a small batch limit so the
  total outbound throughput is naturally bounded.
- `webhook_delivery` rows accumulate over time. There is no automatic
  pruning yet — operators should run a periodic delete of rows older
  than a month or two when the table grows large. A future release
  will add a retention sweep similar to the recording-session cleanup.
- Auto-paused webhooks stay in the database with `enabled=false`,
  `auto_paused=true`. The user re-enables manually after fixing their
  receiver; that also clears the `auto_paused` flag.
