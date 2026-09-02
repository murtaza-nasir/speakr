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
| Max recording size | Hard auto-stop at 200 MB (fixed; protects browser RAM), with a warning at 80% | No size limit or size warning; hours-based ceiling instead (`RECORDING_MAX_HOURS`, default 8h, with a warning at 80%) |
| Crash recovery | IndexedDB chunks survive tab refresh | Server-side chunks survive tab/browser/device crash |
| Finalize | Single-shot `POST /upload` | `POST /upload/session/{id}/finalize`; backend ffmpeg concat demux stitches chunks |
| Reverse-proxy chunk POSTs | One big upload (subject to body-size + read-timeout) | Many small POSTs per recording, plus a longer finalize call |

Incognito recordings are the exception: they never open a server session,
regardless of this flag. Audio from an incognito recording stays in the
browser (RAM + IndexedDB) until the explicit process-without-saving upload,
so the legacy 200 MB cap and its warning still apply to them. If a recording
did stream to the server and the user switches it to incognito in the review
pane afterwards, the server-side chunks are deleted before processing.

## Sliced uploads of files on disk

The same session machinery also carries uploads of files the user already
has on disk. A normal upload is one multipart `POST /upload` of the whole
file, so a reverse proxy whose body limit is below the file size rejects
it before Speakr sees it (Cloudflare's proxy caps request bodies at
100 MB on most plans). Any file larger than one slice is instead sent as
fixed-size byte slices through `POST /upload/session/{id}/chunks/{N}` and
finalized with `POST /upload/session/{id}/finalize-upload`, which
byte-joins the slices and runs the result through the same ingestion
pipeline as a single-shot upload.

This needs no configuration and is not covered by
`ENABLE_SERVER_RECORDING_CHUNKS`, which governs the recorder only. The
slice size is `RECORDING_SESSION_MAX_CHUNK_BYTES` (16 MB), advertised in
the create response, and files at or below that size still go up in one
request.

A few differences from a recorder session are worth knowing when reading
logs:

- Its `recording_session` row has `kind = 'sliced_upload'`, and each
  finalize route rejects the other kind with a 409.
- The slices are byte ranges of one complete container, not
  MediaRecorder timeslices, so finalize does a plain byte-join with no
  ffmpeg concat, no remux, and no container sniffing. What reaches
  ingestion is byte-identical to what the user picked, and from there it
  is treated exactly like a single-shot upload, conversion included.
- The client declares the file size when it opens the session, and
  finalize refuses to ingest slices that do not add up to it. A missing
  or short slice fails loudly rather than becoming a truncated recording.
- `finalize-upload` is synchronous: hashing, probing and any conversion
  happen in-request exactly as they do for `POST /upload`, so it holds
  the connection for as long as that takes and needs the same generous
  proxy read timeout. A retry after a lost response gets the first
  call's recording back rather than re-ingesting the file.
- An abandoned one is expired by the cleanup sweep rather than
  auto-finalized. The user still has the original file, and its slices
  are a truncated container rather than a playable partial recording.
- A failed finalize deletes the slices with it. They cannot be reused,
  since finalize only accepts a session that is still recording.

### Resuming

An upload interrupted by the network is not restarted. The browser keeps
the session id in `localStorage` under the file's identity (name, size,
last-modified), so adding the same file again continues from the slice
count the server reports rather than from zero, which is what makes the
feature usable on a link bad enough to need it.

Only a rejection the server would repeat (an oversize slice, an
exhausted quota, slices that do not add up) deletes the session, because
retrying that costs the user another full transfer for the same answer.
A dropped connection or a timeout leaves the session alone, including
when it happens waiting for `finalize-upload`: the server may be
ingesting at that moment, and the retry gets that recording back.

!!! note "Behind Cloudflare, mind the 125-second Proxy Read Timeout"

    `finalize-upload` ingests in-request, exactly as `POST /upload`
    does: hash, ffprobe, and audio extraction or codec conversion. For
    audio that is a sub-second call, but extracting the audio from a
    long video can outlast Cloudflare's 125-second Proxy Read Timeout
    (not adjustable below Enterprise). The edge then answers 524 while
    the origin finishes ingesting anyway.

    Speakr treats 524 and the rest of Cloudflare's 52x range as
    inconclusive rather than fatal, so the session survives and the
    user's retry replays the recording that landed instead of sending
    the file again. Time your worst realistic upload before putting the
    edge in front of it: if it breaches the budget, the fix is deferring
    conversion to the job queue, which `VIDEO_RETENTION=true` already
    does for videos whose original you want to keep.

Operationally this means in-progress sliced uploads sit in
`UPLOAD_FOLDER/_sessions/` for up to `RECORDING_SESSION_TTL_HOURS`
waiting for a resume that may never come, and count against
`RECORDING_SESSION_MAX_BYTES_PER_USER` until the sweep expires them.
The browser forgets a session after 24 hours regardless.

!!! warning "`RECORDING_SESSION_COMMIT_BATCH_SIZE` must stay at 1"

    Above 1, the chunk endpoint flushes its bookkeeping without
    committing and the request teardown rolls it back, so the server
    keeps asking for the same chunk index forever. A sliced upload gives
    up after a few non-advancing retries and reports it; a recorder
    session silently overwrites its first chunk and loses the recording.

## Configuration

Environment variables, all optional:

| Var | Default | Purpose |
|---|---|---|
| `ENABLE_SERVER_RECORDING_CHUNKS` | `false` | Master switch. Off keeps the legacy single-shot path. |
| `RECORDING_SESSION_TTL_HOURS` | `24` | Sessions whose `last_seen_at` is older than this are reaped. |
| `RECORDING_SESSION_MAX_BYTES_PER_USER` | `5368709120` (5 GB) | Per-user cap on in-progress (non-finalized) sessions. Soft limit: concurrent chunk uploads on different sessions can overrun by up to a few chunk-sizes (16 MB each by default). Cross-process atomic enforcement would require Redis or Postgres advisory locks; the overrun is small and bounded by worker count. |
| `RECORDING_SESSION_MAX_CHUNK_BYTES` | `16777216` (16 MB) | Per-chunk upload cap. Generous; MediaRecorder chunks are typically <1 MB. |
| `RECORDING_SESSION_ALLOWED_MIME_TYPES` | `audio/webm,audio/ogg,audio/mp4,audio/mpeg,audio/wav,audio/x-m4a,video/webm,video/mp4` | Comma-separated whitelist. The video types carry the optional tab/window/screen video capture. |
| `RECORDING_SESSION_CLEANUP_INTERVAL_SECONDS` | `3600` | How often the background thread sweeps expired sessions. Set to `0` to disable. |
| `RECORDING_MAX_HOURS` | `8` | Absolute ceiling on a single recording. Stops the recorder automatically at this duration regardless of size; a toast warns the user at 80% of the ceiling. |
| `RECORDING_VIDEO_KBPS` | `2500` | Video bitrate cap (kbps) for the opt-in tab/window/screen video capture. At the default, an hour of capture is roughly 1 GB. |

## Reverse-proxy requirements

The chunk-streaming flow exchanges many small POST requests during the
recording, plus one longer finalize call. Configure your reverse proxy
so neither is killed in flight.

### nginx / Nginx Proxy Manager

```nginx
location /upload/session/ {
    # Chunk POSTs are capped at RECORDING_SESSION_MAX_CHUNK_BYTES (16 MB),
    # whether they carry a recorder timeslice or a file slice.
    proxy_pass http://speakr_upstream;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 32m;
    proxy_read_timeout 30s;
}

location ~* ^/upload/session/.+/finalize(-upload)?$ {
    # Finalize triggers ffmpeg concat which can take tens of seconds for
    # long recordings, and finalize-upload also hashes, probes and may
    # convert the file in-request. Give it room.
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
@finalize path_regexp ^/upload/session/[^/]+/finalize(-upload)?$
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
    joined.bin              ← sliced uploads only, during finalize
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
  thread, which also removes orphaned session directories that no longer
  have a database row (for example after a database reset).

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
| POST | `/upload/session` | Create a new session. Body: `{mime_type}`, plus `{filename, total_bytes}` for a sliced file upload. |
| POST | `/upload/session/{id}/chunks/{N}` | Append chunk N (must be `chunk_count + 1`). Body is raw bytes. |
| GET | `/upload/session/{id}` | Status of an existing session. |
| POST | `/upload/session/{id}/finalize` | Request asynchronous stitch + transcribe kickoff. |
| POST | `/upload/session/{id}/finalize-upload` | Reassemble a sliced file upload and ingest it. Body is the `POST /upload` form without the file. |
| DELETE | `/upload/session/{id}` | Abort and remove the on-disk chunks. |

All endpoints require an authenticated session. The CSRF token from the
page meta tag is sent with every request.
