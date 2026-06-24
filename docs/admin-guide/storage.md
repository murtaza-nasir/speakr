# File Storage

Speakr stores recording audio on one of two backends, selected with a single environment variable:

- **`local`** (default): audio is written to the filesystem under the upload directory, exactly as in earlier releases.
- **`s3`**: audio is stored as objects in S3-compatible object storage (AWS S3, MinIO, Backblaze B2, Cloudflare R2, Wasabi, or any service that speaks the S3 API).

The backend is set with `FILE_STORAGE_BACKEND`. Local remains the default, so an existing deployment is unaffected until it opts in.

## When to use object storage

Object storage is worth adopting when one or more of the following applies:

- You want recording audio in durable offsite or cloud storage (Backblaze B2, Cloudflare R2, Wasabi, AWS S3) rather than only on local disk.
- You run more than one application replica and want to avoid a shared writable filesystem.
- You want audio served directly from the object store instead of streamed through the application, to keep playback and download bandwidth off the app server.
- You have found a network filesystem mount (NFS or SMB) operationally fragile and prefer access over HTTP with retries.

If your goal is simply to keep audio on a local NAS, a mounted share used as the upload directory is simpler and gives most of the same capacity and redundancy. Running a self-hosted object store backed by the same disks adds an access layer without adding durability.

## Configuration

All settings are read from the environment. Only `FILE_STORAGE_BACKEND` is required for local; the `S3_*` values are required when the backend is `s3`.

| Variable | Default | Description |
|----------|---------|-------------|
| `FILE_STORAGE_BACKEND` | `local` | Storage backend: `local` or `s3`. |
| `FILE_STORAGE_KEY_PREFIX` | `recordings` | Key prefix (path) for stored objects within the backend. |
| `FILE_STORAGE_STAGING_DIR` | `<uploads>/_staging` | Local directory for staging uploads and conversion before the final store. |
| `S3_BUCKET_NAME` | (none) | Target bucket. Required when the backend is `s3`. |
| `S3_REGION` | (none) | Region for the bucket. Required by most providers. |
| `S3_ENDPOINT_URL` | (none) | Service endpoint. Leave unset for AWS S3; set it for MinIO and other S3-compatible providers. |
| `S3_ACCESS_KEY_ID` | (none) | Access key. |
| `S3_SECRET_ACCESS_KEY` | (none) | Secret key. |
| `S3_SESSION_TOKEN` | (none) | Optional session token for temporary credentials. |
| `S3_USE_PATH_STYLE` | `false` | Use path-style addressing. Required for MinIO; leave `false` for providers that use virtual-hosted-style. |
| `S3_VERIFY_SSL` | `true` | Verify TLS certificates. Set `false` only for a local MinIO without TLS. |
| `S3_PRESIGN_TTL_SECONDS` | `900` | Lifetime of presigned URLs for owner playback and download (15 minutes). |
| `S3_PRESIGN_PUBLIC_TTL_SECONDS` | `300` | Lifetime of presigned URLs for public share links (5 minutes). |

S3 support requires the `boto3` package, which is included in the default Docker image. If you build from source, install `boto3>=1.34.0`.

## How audio is delivered

In S3 mode, audio endpoints return a `302` redirect to a short-lived presigned URL rather than streaming the file through the application. This keeps playback and download bandwidth off the app server while remaining compatible with `<audio>` tags and direct downloads. URLs expire according to the TTL settings above.

!!! warning "The presigned URL must be reachable by the browser"
    Because the browser is redirected to the object store, the value of `S3_ENDPOINT_URL` (or the AWS endpoint for your region) must be resolvable and reachable from the client, not only from the application container. If you place the app behind a reverse proxy, the object store endpoint must also be reachable by clients.

    When the app origin and the object store origin differ, the bucket must allow cross-origin requests from your Speakr web origin. See the CORS section of the [Migration Guide](migration-guide.md#migrating-audio-files-to-s3) for example AWS S3 and MinIO rules.

## Provider examples

The exact endpoint and region come from your provider's console. The following are representative starting points.

**AWS S3**

```bash
FILE_STORAGE_BACKEND=s3
S3_BUCKET_NAME=speakr-audio
S3_REGION=us-east-1
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
# No endpoint or path-style needed for AWS S3.
```

**MinIO (self-hosted)**

```bash
FILE_STORAGE_BACKEND=s3
S3_BUCKET_NAME=speakr-audio
S3_REGION=us-east-1
S3_ENDPOINT_URL=https://minio.example.com
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_USE_PATH_STYLE=true
S3_VERIFY_SSL=true        # false only if MinIO has no TLS
```

**Backblaze B2 (S3-compatible)**

```bash
FILE_STORAGE_BACKEND=s3
S3_BUCKET_NAME=speakr-audio
S3_REGION=us-west-004
S3_ENDPOINT_URL=https://s3.us-west-004.backblazeb2.com
S3_ACCESS_KEY_ID=...      # B2 application keyID
S3_SECRET_ACCESS_KEY=...  # B2 applicationKey
```

**Cloudflare R2**

```bash
FILE_STORAGE_BACKEND=s3
S3_BUCKET_NAME=speakr-audio
S3_REGION=auto
S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
```

**Wasabi**

```bash
FILE_STORAGE_BACKEND=s3
S3_BUCKET_NAME=speakr-audio
S3_REGION=us-east-1
S3_ENDPOINT_URL=https://s3.us-east-1.wasabisys.com
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
```

## Operational notes

- **Mixed storage.** Speakr reads from both backends at once. After switching to `s3`, existing local recordings continue to be served from local storage and only new uploads go to the bucket. This allows a gradual transition.
- **Reprocessing and transcription.** Workers materialize an object to a temporary local file for transcription, then remove the temporary file. Large files therefore incur a download on each processing run.
- **Retention and deletion.** Retention policies and manual deletion remove the underlying object from the bucket, the same as they remove local files.
- **Backups.** With `local`, back up the `uploads` directory along with the database and `.env`. With `s3`, audio durability is handled by your provider and the `uploads` directory no longer holds recording audio. The database and `.env` still require backup.

## Migrating existing recordings

Switching the backend only affects new uploads. To move historical recordings into a bucket and update their stored locators, follow the phased process in the [Migration Guide](migration-guide.md#migrating-audio-files-to-s3), which covers path normalization, the migration script with its `--dry-run` and verification options, duration backfill, and rollback.
