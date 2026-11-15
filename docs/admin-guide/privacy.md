# Privacy and regulated data

This page is written for an administrator who plans a deployment for regulated data, such as student records under FERPA or health information under HIPAA. Every statement was checked against the code of this release. The deployment itself, the surrounding systems and the people with access are outside the scope of the page.

## Responsibility for compliance

Software alone does not make a deployment compliant. Compliance depends on the configuration, the hosting, the contracts with every service that receives data, and the practices of the people involved. The decision belongs to the compliance office of the institution. Nothing on this page is legal advice, and Speakr is not certified for FERPA, HIPAA or any other regulation.

## Stored data

### Database

All text is kept in the application database (SQLite by default, PostgreSQL as an option). The columns are stored as plain text, without encryption by the application.

| Data | Table |
|---|---|
| Title, participants, notes, transcript, translation, summary, original file name, error text, speaker embeddings, resolved hotwords and initial prompt | `recording` |
| Transcript passages and their embeddings, when Ask Ambrose is enabled (`ENABLE_INQUIRE_MODE=true`) | `transcript_chunk` |
| Speaker names, voice embeddings and short quotes of up to 200 characters | `speaker`, `speaker_voice_sample`, `speaker_snippet` |
| Calendar events extracted from a transcript | `event` |
| Job parameters (including hotwords and initial prompt) and error text of a job | `processing_job` |
| Public links and internal shares | `share`, `internal_share` |
| Creation, change and revocation of internal shares, with actor, target user, time and IP address | `share_audit_log` |
| Webhook addresses, signing secrets in plain text, and every delivered payload | `webhook`, `webhook_delivery` |
| Token and audio-minute counts per user and day | `token_usage`, `transcription_usage` |
| API tokens as SHA-256 hashes, with name, scopes, expiry and time of last use | `api_token` |
| Accounts: username, email address, bcrypt password hash, prompts and preferences | `user` |
| Failed sign-in counters per account and client address | `login_failure` |

Service keys (LLM, transcription, embeddings, SMTP, SSO) are read from environment variables and are not written to the database.

### Files

| Location | Content |
|---|---|
| `UPLOAD_FOLDER` (default `/data/uploads`), or an S3 bucket when `FILE_STORAGE_BACKEND=s3` | Audio and video of every recording |
| `UPLOAD_FOLDER/_sessions/<session id>/` | Chunks of a recording in progress and of a sliced upload. The folder is removed when the recording is finalized. An abandoned session is cleaned up after `RECORDING_SESSION_TTL_HOURS` (default 24). |
| `UPLOAD_FOLDER/_peaks/` | Waveform data per recording, removed together with the audio |
| `UPLOAD_FOLDER/_staging/` (or `FILE_STORAGE_STAGING_DIR`) | Files in conversion before they reach the storage backend |
| The temporary folder of the container (`/tmp`) | Converted audio and chunks during processing. The files are deleted when a job ends. After a crash or a kill of the process, files can remain until the container is recreated. |
| `AUTO_EXPORT_DIR` (default `/data/exports`), only with `ENABLE_AUTO_EXPORT=true` | Transcript and summary of each recording as a file |
| `AUTO_PROCESS_WATCH_DIR`, only with `ENABLE_AUTO_PROCESSING=true` | Audio placed there for automatic import |
| `HF_HOME` (`/data/instance/huggingface`) | The local embedding model, no user data |

With `PRESERVE_TEMP_AUDIO=true`, a debug copy of each converted file is kept in the temporary folder. The variable must stay unset in a deployment for regulated data.

### Deletion and retention

When a recording is deleted, by its owner, through the API or by a retention rule, the following is removed: the media file, the waveform data, the transcript passages and embeddings, the public links and internal shares, the tag assignments, the speaker quotes, the processing jobs, the share audit rows of the recording, and a job still held by a WhisperX transcription service.

The following remains after the deletion of a recording:

- A tombstone row with the recording id, the user id and the time, for the changes feed of the API. Tombstones are pruned after `RECORDING_TOMBSTONE_DAYS`.
- The rows in `webhook_delivery` of every event sent for the recording. The title is part of each payload, and the original file name is part of the `recording.created` payload. A `recording.deleted` event with the title is queued for every subscribed webhook. Delivery rows are not pruned by the application.
- Speaker profiles and voice embeddings learned from the recording, until the speaker is deleted. A speaker profile without any recording is removed by the cleanup of orphaned speakers.
- The exported file, when automatic export is enabled. The file is renamed as deleted and its content is kept.
- The usage counts in `token_usage` and `transcription_usage`.
- Log lines already written, and any copy held by an external service that received the audio or the text.
- The media file itself, in the case of a storage error during a deletion by a user. The error is logged with the storage path.

With the option to delete only the audio, the media file and the waveform data are removed. Transcript, summary, notes, tags and shares remain.

Automatic retention is off by default. An administrator enables it with `ENABLE_AUTO_DELETION=true` and `GLOBAL_RETENTION_DAYS`, and selects with `DELETION_MODE` whether the whole recording (`full_recording`) or only its audio (`audio_only`) is removed. Details are given in [Retention](retention.md).

## Logs

All log output is written to the standard output of the container. No log file is created by the application, and no HTTP access log is configured for the application server. The retention of the output depends on the Docker logging driver and on any log collector of the host.

| `LOG_LEVEL` | Content of the log |
|---|---|
| `ERROR`, `WARNING` | Failures, with ids. Error text from a transcription service or an LLM provider is included and can quote parts of a request. |
| `INFO` (default) | In addition: ids, lengths, counts, durations, model names and service addresses. No transcript, summary, title, prompt, hotword or search query. Identifiers are present: usernames in group administration, group and tag names, original file names during upload and conversion, and recipient addresses of sent email. |
| `DEBUG` | In addition: titles, summaries, prompts, model replies, search queries, hotwords and speaker names. |

`DEBUG` is unsuitable for a deployment with regulated data, except for a short diagnosis on a test instance.

During an incognito request, only metadata is logged at every level, including `DEBUG`: user id, sizes, lengths, durations, language code, connector name and status codes. Every other log record created during the request, including the records of the HTTP and LLM client libraries, is replaced by a line with its length and source location. An error of a provider is logged as its class, status code and message length, without the message.

## Outbound connections

No telemetry, analytics or update check is contained in the application. The following connections are made by the server.

| Destination | Data sent | Control |
|---|---|---|
| Transcription service | Audio, language, initial prompt, hotwords, speaker count, and voice samples of earlier chunks for some connectors | Required. The address is set with `ASR_BASE_URL` or `TRANSCRIPTION_BASE_URL` and the connector with `TRANSCRIPTION_CONNECTOR`. With a local service, the audio stays on the network of the institution. |
| LLM for titles, summaries, event extraction and speaker identification | Transcript and prompts, with recording details such as participants and notes where the prompt includes them | `TEXT_MODEL_BASE_URL`. Without a text model, these features are unavailable. |
| LLM for chat and for translation | Transcript, question, chat history; text to translate | `CHAT_MODEL_BASE_URL` and `TRANSLATION_MODEL_BASE_URL`. When unset, the text model is used. |
| Embedding API | Transcript passages and search queries | Only with `ENABLE_INQUIRE_MODE=true` and `EMBEDDING_BASE_URL` set. Without `EMBEDDING_BASE_URL`, embeddings are computed inside the container. |
| Hugging Face Hub | No user data. The local embedding model is downloaded on first use. | Only with Ask Ambrose and local embeddings. The model can be placed in `HF_HOME` in advance, and the download is prevented with `HF_HUB_OFFLINE=1`. |
| Webhook addresses of users | Recording id, title, original file name, language, durations and error text, depending on the event. No transcript or summary. | All deliveries are stopped with `WEBHOOK_GLOBAL_ENABLED=false`. |
| SMTP server | Verification and password reset messages. For users with the completion email enabled: a message with the recording title in the subject, and a shortened error text after a failure. | No mail is sent without `SMTP_HOST`. |
| SSO provider (OIDC) | Discovery, token and user information requests during sign-in | `ENABLE_SSO` |
| S3 storage | Audio and video | Only with `FILE_STORAGE_BACKEND=s3` |
| Transcription service, at startup | A bundled test clip for the check of voice embeddings. No user data. | Sent only to the configured transcription service |

Requests to these services are made over the scheme of the configured address. With an `http://` address, audio and text cross the network without encryption.

Scripts, styles and fonts of the web interface are downloaded once, with checksums, when the Docker image is built, and are served by the instance. At runtime nothing is loaded from a third party, and scripts, styles, fonts and connections are limited to the origin of the instance through the default `Content-Security-Policy` header.

Push subscriptions of browsers can be stored, but no push message is sent by the code of this release.

## Browser storage

| Store | Content | Lifetime |
|---|---|---|
| Session cookie, and a remember cookie after "Remember me" | Signed session data | The session cookie ends with the browser session. The remember cookie lasts `REMEMBER_COOKIE_DAYS` (default 30). |
| `localStorage` | Interface preferences, the id of the last selected recording, and the defaults of the upload dialog | Until cleared in the browser |
| `sessionStorage` | Chat threads of the open recordings, and the result of an incognito recording | Until the tab is closed |
| IndexedDB `SpeakrRecordings` | Audio chunks and notes of a recording in progress, for recovery after a crash. Incognito recordings are included. | Until the recording is uploaded or discarded |
| IndexedDB `SpeakrFailedUploads` | The file of an upload that failed, for a later retry | Until the retry succeeds or the entry is removed |
| Cache Storage of the service worker (`Speakr-cache-<version>`) | The application files, visited pages, and the responses of `GET` requests under `/api/`, which include recording lists, transcripts and summaries | Until the cache is replaced with the next release, or until the site data is cleared. The cache is not cleared at sign-out. |

On a shared computer, site data must be cleared after use, or a separate browser profile must be used for each person.

## Incognito mode

Incognito mode is off by default and is enabled with `ENABLE_INCOGNITO_MODE=true`.

1. The audio is sent to `POST /api/recordings/incognito` and written to a temporary file with a random name. The file name given by the user is not used on the server and is not logged.
2. The file is converted when another format is required for the transcription service, and it is sent to the transcription service. With a WhisperX service with asynchronous jobs, the job and its result are deleted on the service after the result is fetched and after every failure.
3. A title is generated by the text model. A summary is generated when the user asks for one.
4. The temporary file and every converted copy are deleted before the response is sent, also after an error.
5. Transcript, title and summary are returned in the response and are kept in the `sessionStorage` of the browser tab. They are gone when the tab is closed.
6. Chat questions are sent to `/api/recordings/incognito/chat` together with the transcript, and a new summary is requested from `/api/recordings/incognito/summary` in the same way. Nothing from these requests is stored.

No row is written to the database for an incognito request. The usage tables are included: the tokens and audio minutes of incognito requests are not counted, and monthly budgets are not applied to them.

No webhook, email or notification is produced. Only metadata is logged, as described under [Logs](#logs). Error responses contain a fixed message without the text of the provider.

The audio and the text are still sent to the transcription service and the LLM. The data on those services is not protected by incognito mode, so regulated data requires local services or services under a suitable agreement.

A person who copies or downloads text from an incognito result is responsible for the place where that text is kept afterwards.

Limits of incognito mode:

- During a recording in the browser, the audio chunks are kept in IndexedDB for crash recovery until processing. After a browser crash they remain until the recovery dialog is answered.
- A temporary file can remain in the container after a crash or a kill of the server process during the request.
- A result whose deletion failed stays on the transcription service until its expiry there. A failed deletion is logged with the job id.

## Access control

- **Public links.** Anyone with a link can read the transcript and play the audio, and can read the summary and the notes when the owner includes them. An expiry date is optional. An administrator stops the creation of links with `ENABLE_PUBLIC_SHARING=false`, or withdraws the permission from individual users. Links that already exist are still served after the switch and must be revoked individually or left to expire.
- **Internal sharing.** Sharing between accounts is enabled with `ENABLE_INTERNAL_SHARING`. With `SHOW_USERNAMES_IN_UI` and `user_search_scope` (`everyone`, `groups` or `exact`), an administrator limits the usernames that a user can see and find.
- **Administrators.** A site administrator has no route to read the recordings of another user. An administrator can set the password of any account, and a person with access to the server or the database can read everything.
- **API tokens.** Tokens are stored as SHA-256 hashes. Scopes and an expiry date are optional, and a token without a date never expires. A token in the `?token=` query parameter is still accepted on `GET` requests and then appears in the access log of a reverse proxy. The `Authorization` header must be used.
- **Sessions.** Cookies are `HttpOnly` and `SameSite=Lax`. The `Secure` flag is set only with `SESSION_COOKIE_SECURE=true`. No idle timeout exists.
- **Passwords.** At least 8 characters with upper case, lower case, digit and special character are required. After 5 wrong passwords for one account from one address, sign-in from that address is refused for 15 minutes. Both numbers are system settings (`login_max_failed_attempts`, `login_lockout_minutes`).
- **Single sign-on.** With `ENABLE_SSO=true`, sign-in is delegated to an OIDC provider, and the password form is removed for everyone except administrators with `SSO_DISABLE_PASSWORD_LOGIN=true`.

## Checklist for a fully local deployment

1. Run the transcription service on hardware of the institution, and set `ASR_BASE_URL` (or `TRANSCRIPTION_BASE_URL`) to it. See [WhisperX Setup](whisperx-setup.md).
2. Run the LLM locally, and set `TEXT_MODEL_BASE_URL` to it. Leave `CHAT_MODEL_BASE_URL` and `TRANSLATION_MODEL_BASE_URL` unset or point them to local services.
3. Leave `EMBEDDING_BASE_URL` unset or point it to a local service. With Ask Ambrose and local embeddings, place the model in `HF_HOME` and set `HF_HUB_OFFLINE=1`.
4. Use HTTPS, or a private network that only these hosts share, between Speakr and each of these services.
5. Set `WEBHOOK_GLOBAL_ENABLED=false`, unless webhooks to internal systems are approved.
6. Leave `SMTP_HOST` unset, or use the mail server of the institution. Recording titles appear in completion emails.
7. Keep `FILE_STORAGE_BACKEND=local`, or use an S3 service of the institution.
8. Terminate TLS at a reverse proxy, set `SESSION_COOKIE_SECURE=true`, and set `TRUSTED_PROXY_HOPS` to the number of proxies. Turn off or protect the access log of the proxy.
9. Place the data volume (`/data/uploads`, `/data/instance`), the database and the backups on encrypted disks or volumes. No data is encrypted by the application.
10. Keep `LOG_LEVEL` at `INFO` or higher, and set a size limit and a retention period for the container logs.
11. Set `ENABLE_PUBLIC_SHARING=false` and revoke existing public links.
12. Set `ALLOW_REGISTRATION=false`. Use `ENABLE_SSO=true` with `SSO_DISABLE_PASSWORD_LOGIN=true` when the institution has an identity provider.
13. Define retention with `ENABLE_AUTO_DELETION`, `GLOBAL_RETENTION_DAYS` and `DELETION_MODE`, in line with the records policy of the institution.
14. Leave `ENABLE_AUTO_EXPORT`, `ENABLE_AUTO_PROCESSING` and `PRESERVE_TEMP_AUDIO` unset unless their folders are covered by the same protection.
15. Apply the retention and the encryption of the live system to its backups. Recordings deleted later are still present in an older backup.
16. Keep a list of the people with access to the host, the Docker daemon, the database and the backups. Each of them can read all recordings.
17. Instruct users on shared computers to clear site data, or give each user a separate browser profile.

## Known limits

- **No access audit log.** No record is kept of who viewed, played, downloaded or exported a recording, of sign-ins and failed sign-ins, or of actions by administrators. The only records are the share audit table for internal shares (its rows are deleted together with the recording), the time of last use of each API token, and the temporary counters of failed sign-ins. An access record required by a regulation must come from the reverse proxy or another system.
- **No encryption at rest in the application.** Database content, media files, webhook signing secrets and exported files are stored as plain data.
- **Content in the browser.** API responses are kept in the cache of the service worker and are not removed at sign-out.
- **Public links after the switch-off.** Existing links are served until they are revoked or expire.
- **Titles and file names after deletion.** They are kept in the webhook delivery rows.
- **Identifiers and provider error text in the default log.** See [Logs](#logs).
- **No idle timeout** for sessions, and no expiry for API tokens without a date.
- **No usage counts for incognito requests.**
