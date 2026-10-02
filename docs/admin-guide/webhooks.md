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
| `recording.updated` | A change a user can see, from the web app or the API (see below) | `recording_id`, `title`, `fields_changed` (list of strings), `updated_at` |
| `recording.deleted` | Recording removed | `recording_id`, `title` |
| `webhook.test` | Synthetic event from the **Test** button or `POST /api/v1/webhooks/{id}/test` | `reason`, `webhook_id` |

All events listed above fire in the current backend. The
`recording.transcription.started` event fires only after the audio
file's existence on disk is confirmed, so subscribers don't see
misleading started→failed sequences for jobs that abort immediately
(e.g. the audio file was deleted between upload and worker pickup).

`recording.updated` fires once per saved change, whether the change was made in the web
app, through API v1 or by a speaker rename or merge. `fields_changed` uses these words:

| Word | Changed |
|---|---|
| `title`, `participants`, `notes`, `summary`, `meeting_date`, `folder_id` | that field |
| `transcript` | the transcript text or its speaker names |
| `speakers` | which name each diarization label shows |
| `tags` | a tag was added, removed or reordered |
| `external_refs` | the webhook owner's external references |
| `events` | extracted calendar events changed |
| `is_inbox`, `is_highlighted`, `is_archived` | the owner's flags |
| `audio` | the audio was removed |
| `deletion_exempt`, `prompt_variables` | that setting |

Status changes during processing fire no `recording.updated`; the lifecycle events above
cover them. Changes a user who received a shared recording makes to their own flags fire
nothing for the owner.

Bursts are merged: a `recording.updated` with the same `recording_id` and `fields_changed`
as a delivery that has not been attempted yet replaces that delivery's `data`, so a notes
autosave sends one delivery with the latest state.

Since v0.10.11-alpha, edits made in the web app fire `recording.updated` too. Before, only
`PATCH /api/v1/recordings/{id}` did, so receivers subscribed to this event get more of them.

## Envelope

Every delivery is a `POST` with a JSON body shaped like:

```json
{
  "id": "f4e6a4e1-3b9b-4a04-9d4f-0e7a5d8b3c10",
  "type": "recording.transcription.completed",
  "timestamp": "2026-06-04T15:23:11.124000Z",
  "occurred_at": "2026-06-04T15:23:11.124000Z",
  "user_id": 42,
  "data": {
    "recording_id": 9173,
    "title": "Q3 planning",
    "language": "en",
    "audio_duration_seconds": 3624.7,
    "transcription_duration_seconds": 212,
    "updated_at": "2026-06-04T15:23:10.981233Z"
  }
}
```

- `id` is one UUID per event; every webhook that receives the event gets the same `id`.
- `timestamp` and `occurred_at` are the time the event happened (UTC, microseconds). They do
  not change on retries: the body is stored once and every attempt sends the same bytes.
- `data` of every `recording.*` event carries `updated_at`, the recording's last change as
  in the API, and `external_refs`, the webhook owner's references (both absent for
  `recording.deleted`).
- The body is `json.dumps(envelope, separators=(',', ':'), ensure_ascii=False)` in UTF-8.
  Verify the signature over the raw bytes you received, never over re-serialised JSON.

`language` is the language the transcription service reported for that recording,
as an ISO 639-1 code. It reflects what was detected or used for that audio, not the
user's current transcription-language preference. Fields are omitted when Speakr does
not have a value, so `language` is absent for backends that report none, and for
recordings transcribed before v0.10.6-alpha, which is when Speakr began storing it.


Headers:

| Header | Purpose |
|---|---|
| `Content-Type: application/json` | Body format |
| `User-Agent: Speakr-Webhook/1.0` | Identifies Speakr to receivers |
| `Speakr-Event` | The event type, for routing without parsing the body |
| `Speakr-Delivery-Id` | The envelope `id`; use it for idempotency |
| `Speakr-Signature-V2` | `t=<send time, Unix seconds>,v1=<hex>`: HMAC-SHA256 of `"<t>." + raw body`. Verify this one when it is present. |
| `Speakr-Signature` | `sha256=<hex>`: HMAC-SHA256 of the raw body |
| `Speakr-Attempt` | `1` for the first attempt, `2` for the first retry, and so on |
| `Speakr-Timestamp` | ISO 8601 UTC send time. **Not signed**: do not use it for freshness checks. |

## Signature verification

Every receiver must verify a signature before trusting the body. Without it, anyone who
guesses the URL can forge events.

Verify `Speakr-Signature-V2` when it is present. Its signature covers the send time `t`, so
a receiver can refuse old deliveries (for example `|now - t| > 300` seconds) while every
retry still passes: each attempt is signed with its own send time over the same body.
During a secret rotation the header carries two `v1=` values; accept a match with either.

### Python (V2)

```python
import hmac
import hashlib
import time

def verify_speakr_v2(secret: str, raw_body: bytes, header: str, window: int = 300) -> bool:
    parts = [p.split('=', 1) for p in header.split(',') if '=' in p]
    try:
        t = int(next(v for k, v in parts if k == 't'))
    except (StopIteration, ValueError):
        return False
    if abs(time.time() - t) > window:
        return False
    expected = hmac.new(secret.encode(), f'{t}.'.encode() + raw_body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in parts if k == 'v1')
```

### Node.js (V2)

```js
const crypto = require('crypto');

function verifySpeakrV2(secret, rawBody, header, windowSeconds = 300) {
    if (!header) return false;
    const parts = header.split(',').map(p => p.split('='));
    const t = Number((parts.find(([k]) => k === 't') || [])[1]);
    if (!Number.isInteger(t) || Math.abs(Date.now() / 1000 - t) > windowSeconds) return false;
    const expected = crypto.createHmac('sha256', secret)
        .update(Buffer.concat([Buffer.from(`${t}.`), rawBody])).digest();
    return parts.filter(([k]) => k === 'v1').some(([, v]) => {
        const given = Buffer.from(v || '', 'hex');
        return given.length === expected.length && crypto.timingSafeEqual(expected, given);
    });
}
```

### Python (V1, `Speakr-Signature`)

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

### Node.js (V1)

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

### Idempotency

Answer `2xx` to a `Speakr-Delivery-Id` you have already processed. Any other `4xx` counts as
a permanent failure, so refusing a repeat with `409` would stop the retries of a delivery
you never finished.

### Secret rotation

`POST /api/v1/webhooks/{id}/rotate-secret` returns the new secret once. The previous secret
stays valid for `WEBHOOK_SECRET_GRACE_HOURS` (default 24): until then `Speakr-Signature-V2`
carries a second `v1=` value signed with it, so a receiver can switch to the new secret
without missing deliveries. `Speakr-Signature` uses the new secret at once. The webhook's
`previous_secret_valid_until` shows when the grace ends; the old secret itself is never
returned.

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
`WEBHOOK_AUTOPAUSE_FAILURES` (default 10) the webhook is auto-paused. An
auto-paused webhook gets one trial delivery every
`WEBHOOK_TRIAL_INTERVAL_SECONDS` (default 3600); a successful trial resumes it.
Re-enabling it by hand also clears the pause.

A replay (`POST /api/v1/webhooks/{id}/deliveries/{did}/replay`) is a new
delivery: a new `id`, a new `timestamp` and `occurred_at`, and `replayed_from`
with the old `id`.

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
| `WEBHOOK_TRIAL_INTERVAL_SECONDS` | `3600` | Trial delivery interval for an auto-paused webhook; `0` disables trials. |
| `WEBHOOK_SECRET_GRACE_HOURS` | `24` | How long the previous secret still signs `Speakr-Signature-V2` after a rotation; `0` ends it at once. |

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
| POST | `/api/v1/webhooks/{id}/rotate-secret` | Generate a fresh HMAC secret. Returned once; the previous one stays valid for the grace period. |
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
