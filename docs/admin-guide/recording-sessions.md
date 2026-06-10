# Server-side recording sessions

Server-side recording sessions let the in-browser recorder stream audio
chunks to Speakr as they are produced, rather than buffering the entire
recording in browser RAM and uploading it at Stop. This unlocks longer
recordings, makes crash recovery reliable, and removes the legacy
client-side size cap.

Off by default; opt in with `ENABLE_SERVER_RECORDING_CHUNKS=true`. It is planned to become the default in an upcoming release once it has had wider testing.

## What changes when it is on

| Aspect | Off (legacy) | On (server sessions) |
|---|---|---|
| Where audio lives during recording | Browser RAM + IndexedDB | Server disk (`UPLOAD_FOLDER/_sessions/<id>/`) |
| Max recording size | `MAX_RECORDING_MB` hard auto-stop (default 200 MB) | Soft warning at the same threshold; hours-based ceiling instead (`RECORDING_MAX_HOURS`, default 8h) |
| Crash recovery | IndexedDB chunks survive tab refresh | Server-side chunks survive tab/browser/device crash |
| Finalize | Single-shot `POST /upload` | `POST /upload/session/{id}/finalize`; backend ffmpeg concat demux stitches chunks |
| Reverse-proxy chunk POSTs | One big upload (subject to body-size + read-timeout) | Many small POSTs per recording, plus a longer finalize call |

## Configuration

Environment variables, all optional:

| Var | Default | Purpose |
|---|---|---|
| `ENABLE_SERVER_RECORDING_CHUNKS` | `false` | Master switch. Off keeps the legacy single-shot path. |
| `RECORDING_SESSION_TTL_HOURS` | `24` | Sessions whose `last_seen_at` is older than this are reaped. |
| `RECORDING_SESSION_MAX_BYTES_PER_USER` | `5368709120` (5 GB) | Per-user cap on in-progress (non-finalized) sessions. Soft limit: concurrent chunk uploads on different sessions can overrun by up to a few chunk-sizes (16 MB each by default). Cross-process atomic enforcement would require Redis or Postgres advisory locks; the overrun is small and bounded by worker count. |
| `RECORDING_SESSION_MAX_CHUNK_BYTES` | `16777216` (16 MB) | Per-chunk upload cap. Generous; MediaRecorder chunks are typically <1 MB. |
| `RECORDING_SESSION_ALLOWED_MIME_TYPES` | `audio/webm,audio/ogg,audio/mp4,audio/mpeg,audio/wav,audio/x-m4a` | Comma-separated whitelist. |
| `RECORDING_SESSION_CLEANUP_INTERVAL_SECONDS` | `3600` | How often the background thread sweeps expired sessions. Set to `0` to disable. |
| `RECORDING_MAX_HOURS` | `8` | Absolute ceiling on a single recording. Stops the recorder automatically at this duration regardless of size. |

## Reverse-proxy requirements

The chunk-streaming flow exchanges many small POST requests during the
recording, plus one longer finalize call. Configure your reverse proxy
so neither is killed in flight.

### nginx / Nginx Proxy Manager

```nginx
location /upload/session/ {
    # Chunk POSTs are small; keep timeouts modest.
    proxy_pass http://speakr_upstream;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 32m;
    proxy_read_timeout 30s;
}

location ~* ^/upload/session/.+/finalize$ {
    # Finalize triggers ffmpeg concat which can take tens of seconds
    # for long recordings. Give it room.
    proxy_pass http://speakr_upstream;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}
```

If you do not split per-location, set a single `proxy_read_timeout 600s`
on the parent block. The chunk POSTs are short and won't be affected by
the larger timeout; finalize will get the headroom it needs.

### Caddy

```
@finalize path_regexp ^/upload/session/[^/]+/finalize$
handle @finalize {
    reverse_proxy speakr:8899 {
        transport http {
            response_header_timeout 10m
        }
    }
}
```

### Apache (mod_proxy)

```apache
ProxyTimeout 600
LimitRequestBody 33554432
```

## How storage is laid out

```
UPLOAD_FOLDER/_sessions/
  <uuid-1>/
    session.json
    chunk-000001.bin
    chunk-000002.bin
    ...
  <uuid-2>/
    ...
```

- `session.json` is a JSON copy of the database row, written defensively
  for the case where the database is wiped but the disk survives.
- Chunks are stored under generic `.bin` extensions; format is determined
  by the database `mime_type` column and validated when stitching.
- Aborted sessions are torn down synchronously when the user clicks
  Discard. Sessions that go quiet for longer than
  `RECORDING_SESSION_TTL_HOURS` are reaped by the background cleanup
  thread.

## Crash recovery

The recording client persists a small marker in `localStorage`
(`speakr.serverRecordingSession`) on session creation. On page reload,
Speakr checks the marker against the server: if the session is still in
the `recording` state with at least one chunk on disk, the user is
prompted to finalize the in-progress recording or abort it.

A full client-side resume of the open MediaRecorder is not possible
because the underlying audio track does not survive a tab reload. The
user-visible result is therefore: prompt → finalize the chunks already
on the server, or discard them.

## Operational health

- **Disk usage**: monitor `UPLOAD_FOLDER/_sessions/` size. Long-running
  cleanup gaps or stuck `recording` rows can let it grow. The cleanup
  thread logs a summary line on every sweep that reaps at least one
  session.
- **ffmpeg availability**: the stitch worker shells out to `ffmpeg`. If
  the binary is missing, finalize fails with a clear "ffmpeg binary not
  found on server PATH" error on the affected recording. Docker images
  ship ffmpeg by default; bare-metal installs need to ensure it is on
  PATH for the Speakr user.
- **Per-user quota**: when `_user_bytes_in_progress` >= the configured
  cap, the API returns 507 on `POST /upload/session` and on chunk
  uploads that would exceed the cap. Surfaced to the user as a quota
  banner.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/upload/session` | Create a new session. Body: `{mime_type}`. |
| POST | `/upload/session/{id}/chunks/{N}` | Append chunk N (must be `chunk_count + 1`). Body is raw bytes. |
| GET | `/upload/session/{id}` | Status of an existing session. |
| POST | `/upload/session/{id}/finalize` | Request asynchronous stitch + transcribe kickoff. |
| DELETE | `/upload/session/{id}` | Abort and remove the on-disk chunks. |

All endpoints require an authenticated session. The CSRF token from the
page meta tag is sent with every request.
