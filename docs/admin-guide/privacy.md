# Privacy and regulated data

This page is written for an administrator who plans a deployment for regulated data, such as student records under FERPA or health information under HIPAA. The data that is stored, logged, cached, deleted and sent to other services in this release is listed here. The deployment itself, the surrounding systems and the people with access are outside the scope of the page.

## Responsibility for compliance

No compliance with FERPA, HIPAA or any other law or regulation is claimed for Speakr, and Speakr is not certified under any of them. A deployment is not made compliant by software alone. Compliance depends on the configuration, the hosting, the contracts with every service to which data is sent, and the practices of the people involved, and it is assessed by the compliance office of the institution.

The features that support a deployment with regulated data are local transcription and language models, incognito mode, automatic retention, the [audit log](audit-log.md), and the settings for sessions, API tokens and webhooks. Their configuration, and everything outside the application, remains the responsibility of the operator. Nothing on this page is legal advice.

## Stored data

### Database

All text is kept in the application database (SQLite by default, PostgreSQL as an option) as plain text. No column is encrypted by the application.

Earlier releases stored the characters of a transcript outside ASCII as `\uXXXX` escapes. About 30 seconds after the first start after the upgrade, such transcripts are rewritten once in the background with the characters themselves. The words of the transcript, its `updated_at` and plain-text transcripts are not changed, and the log line holds counts only. The setting `transcript_unicode_rewrite_done` is stored when the rewrite is complete, and an interrupted rewrite is resumed at the next start.

| Data | Table |
|---|---|
| Title, participants, notes, transcript, translation, summary, original file name, storage path of the media, error text, prompt variables, resolved hotwords and initial prompt, name of the exported file. Voice embeddings of every voice of the recording, named or not, under its diarization label, when embeddings are returned by the transcription service. | `recording` |
| Transcript passages, their embeddings and the name of the main speaker of each passage when that speaker is named, when Ask Ambrose is enabled (`ENABLE_INQUIRE_MODE=true`) | `transcript_chunk` |
| Speakers that a user named: name, email address and aliases when entered, voice embeddings, and short quotes of up to 200 characters. Nothing is stored here for an unnamed voice (see [Diarization labels](#diarization-labels)). | `speaker`, `speaker_voice_sample`, `speaker_snippet` |
| Calendar events extracted from a transcript: title, description, location and attendees | `event` |
| Job parameters (including hotwords and initial prompt) and error text of a job | `processing_job` |
| Public links, with the address part of each link, and internal shares | `share`, `internal_share` |
| Personal notes of a user on a recording shared with that user, with the inbox, star and archive state | `shared_recording_state` |
| Sessions of recordings made in the browser and of uploads in slices. While a session is in progress: the file name of the upload, or the title, notes and options entered when the recording was finished. Afterwards: ids, sizes, times, the status and error text, without the file name, the title and the notes (see [Recording sessions](#recording-sessions)). | `recording_session` |
| Parts of an upload that is joined into one recording, with the original file name, for one day. Ids of uploads named by the browser, for one day. | `upload_join_part`, `client_upload` |
| References of a recording to other systems: reference, address and label | `recording_external_ref` |
| Tags and folders with their names, prompts, default hotwords and default initial prompts. Groups, memberships and permissions. | `tag`, `folder`, `recording_tags`, `group`, `group_membership`, `permission_grant` |
| Templates of a user for transcripts, titles, exports and initial prompts | `transcript_template`, `naming_template`, `export_template`, `initial_prompt_template` |
| Name and filters of an Ask Ambrose session: tags, speaker names, dates and recording ids | `inquire_session` |
| The [audit log](audit-log.md): sign-ins, access to recordings, changes, shares and administrative actions, each with time, account, client address and user agent. Ids and codes only; no title, file name or content. | `audit_event` |
| Changes of internal shares recorded by earlier releases, with the client address and a note. The table is no longer written. Its rows were copied to `audit_event` without the note. | `share_audit_log` |
| Webhook addresses and signing secrets in plain text. For every delivery: the payload, the first 2000 characters of the answer of the receiver, and error text. | `webhook`, `webhook_delivery` |
| Token and audio-second counts per user and day. The counts of incognito requests are included and are not marked as such. | `token_usage`, `transcription_usage` |
| API tokens as SHA-256 hashes, with name, scopes, expiry and time of last use | `api_token` |
| Accounts: username, email address, bcrypt password hash, SSO subject, name, job title and company when entered, prompts, hotwords and preferences, a pending token for email verification or password reset, and the time of the last use of the account | `user` |
| Failed sign-in counters per account and client address. An IPv6 address is stored as its `/64` network. | `login_failure` |
| Id of each deleted recording with the user id, the time and the reason, for the changes feed of the API | `recording_tombstone` |
| Notices shown in the interface, as a message key with parameters | `notification` |
| Push subscriptions of browsers: endpoint and keys. A subscription can be stored through the API only (see [Outbound connections](#outbound-connections)). | `push_subscriptions` |
| System settings, among them the default prompts set by an administrator | `system_setting` |
| Fingerprint of the voice embedding model and the embedding of a bundled test clip. No user data. | `voice_embedding_space` |

For the last use of an account, only the time of the last request is kept, in one column of the `user` table. The time is updated at most once every five minutes, and no history of requests, addresses or routes is stored for it. Requests that are sent on a timer from an open tab are not counted. The time is shown to site administrators in the [user list](user-management.md#monitoring-usage-patterns), and it is stored in the same way for requests in incognito mode.

Service keys (LLM, transcription, embeddings, SMTP, SSO) are read from environment variables and are not written to the database.

### Diarization labels

A diarization label is the placeholder of a voice that has not been named, such as `SPEAKER_00`, `Speaker 1`, `spk_0` or `Unknown Speaker`. A label is kept in the transcript, where an unnamed turn is shown under it. A label is never stored as a person: no row in `speaker`, `speaker_voice_sample` or `speaker_snippet` is written for a label, and labels are left out of `recording.participants`, of `transcript_chunk.speaker_name` and of the speaker filters stored in `inquire_session`. The rule is defined in `src/utils/speaker_labels.py`.

When a recording is transcribed or reprocessed, the labels in the result of the transcription service are stored in one form: `SPEAKER_00`, `SPEAKER_01` and so on. A letter (`A`), a number (`0`) and a numbered label in another form (`S01`, `SPEAKER_0`, `Speaker 1`) are converted. Two kinds of label are stored as received: a label of the unknown family (`Unknown`, `UNKNOWN_SPEAKER`), and text without a number, which is taken for a name supplied by the service. Transcripts stored by earlier releases are not rewritten.

In earlier releases a label could be stored as a person. At the first start after the upgrade, such entries are removed once (migration `0008_remove_generic_speaker_labels`):

- Saved speakers whose name is a label are deleted, together with their voice samples and quotes. In transcripts, the link from a segment to such a speaker is cleared; the text and the visible label of the segment are not changed.
- Labels are removed from the participants of recordings. The `updated_at` of these recordings is set, so that the new value is read by API clients.
- The speaker name of transcript passages is cleared where it is a label. The text of the passage is not changed.
- Labels are removed from the speaker filters of Ask Ambrose sessions.
- A saved speaker named like a label is kept when an email address or aliases were entered for it. Such a speaker is not listed anywhere; its id is written to the log, and it can be renamed or deleted through the API by that id.

The log line of the cleanup holds counts and ids, never a name. The counts can be read beforehand with a command that has read-only access to the database. The recordings whose stored transcript still holds labels in another form are counted by the same command.

```bash
docker compose exec app python scripts/generic_speakers_dry_run.py

# A copy or another database
python scripts/generic_speakers_dry_run.py --database-uri sqlite:////path/to/copy.db
```

### Files

| Location | Content |
|---|---|
| `UPLOAD_FOLDER` (default `/data/uploads`), or an S3 bucket when `FILE_STORAGE_BACKEND=s3` | Audio and video of every recording. The path of a file, and the key of an S3 object, end with the original file name. |
| `UPLOAD_FOLDER/_sessions/<session id>/` | Chunks of a recording made in the browser, and slices of a large upload, while in progress. The folder is removed when the recording is finalized. A session without a request for `RECORDING_SESSION_TTL_HOURS` (default 24) is handled by the hourly cleanup: a recording with chunks is finished and placed in the library of the user as a recovered recording, and an empty session or an unfinished upload in slices is discarded. |
| `UPLOAD_FOLDER/_peaks/` | Waveform data per recording, removed together with the audio |
| `UPLOAD_FOLDER/_staging/` (or `FILE_STORAGE_STAGING_DIR`) | Files in conversion before they are moved to the storage backend |
| The temporary folder of the container (`/tmp`) | Converted audio and chunks of ordinary recordings during processing. The files are deleted when a job ends. After a crash or a kill of the process, files can remain until the container is recreated. The result of a transcription service is processed in memory and is not written to a file by any connector. A file `vibevoice_last_response.json` written there by an earlier release holds the transcript of the last VibeVoice transcription before the upgrade; it is gone when the container is recreated. |
| `INCOGNITO_TEMP_DIR` (default `speakr-incognito` in the temporary folder) | The files of incognito requests in progress (see [Incognito mode](#incognito-mode)) |
| `AUTO_EXPORT_DIR` (default `/data/exports`), only with `ENABLE_AUTO_EXPORT=true` | Transcript and summary of each recording as a file |
| `AUTO_PROCESS_WATCH_DIR`, only with `ENABLE_AUTO_PROCESSING=true` | Audio placed there for automatic import |
| `HF_HOME` (`/data/instance/huggingface`) | The local embedding model, no user data |

With `PRESERVE_TEMP_AUDIO=true`, a debug copy of each converted file is kept in the temporary folder. The variable must stay unset in a deployment for regulated data.

### Deletion and retention

When a recording is deleted, by its owner, through the API or by a retention rule, the following is removed: the media file, the waveform data, the transcript passages and embeddings, the calendar events, the public links and internal shares, the personal notes of other users on the recording, the tag assignments, the speaker quotes, the processing jobs, the session of a recording that was made in the browser or uploaded in slices, the rows of the recording in the old table `share_audit_log`, and a job still held by a WhisperX transcription service.

The following remains after the deletion of a recording:

- A tombstone row with the recording id, the user id, the time and the reason, for the changes feed of the API. Tombstones are removed after `RECORDING_TOMBSTONE_DAYS` (default 90).
- The rows in `webhook_delivery` of every event sent for the recording. The title is part of each payload, and the original file name is part of the `recording.created` payload. A `recording.deleted` event with the title is queued for every subscribed webhook. Delivery rows are removed `webhook_delivery_retention_days` days after their creation (default 30; with 0 they are kept without limit). With `webhook_omit_titles=true` (default `false`), the title and the original file name are left out of every payload.
- Speaker profiles and voice embeddings learned from the recording, until the speaker is deleted. A speaker profile without any recording is removed by the cleanup of orphaned speakers.
- The exported file, when automatic export is enabled. By default the file is renamed with the `[deleted]_` prefix and its content is kept. With `auto_export_delete_with_recording=true` (default `false`), the file is deleted together with the recording. Files renamed before the setting was turned on are not removed afterwards.
- The usage counts in `token_usage` and `transcription_usage`. These rows hold numbers per user and day, without a recording id.
- The rows of the [audit log](audit-log.md) that name the recording: who opened, played, downloaded, changed, shared and deleted it, and when. The rows hold the id of the recording and of its owner, without a title or any content. Share events are among them: the rows of `audit_event` have no link to the recording or to an account, so they remain after the deletion of either. They are removed after `audit_log_retention_days` days (default 365; with 0 they are kept without limit). The same applies after the deletion of an account: the rows that name the account remain, by id, and the username is kept in the row of its deletion.
- Log lines already written, and any copy held by an external service to which the audio or the text was sent.
- The media file itself, in the case of a storage error during a deletion by a user. The error is logged with the id of the recording and the storage path without the original file name.

With the option to delete only the audio, the media file and the waveform data are removed. Transcript, summary, notes, tags and shares remain.

### Recording sessions

For a recording made in the browser, and for a large upload sent in slices, a row is kept in `recording_session`. Text of the user is held in the row only while it is needed:

- The file name of an upload in slices is cleared when the upload is finalized, when it fails, when it is canceled, and when an abandoned session is expired by the hourly cleanup.
- The title, notes and options entered at the end of a browser recording are cleared when the audio has been assembled, when the session is canceled, and when it is expired. After a failed assembly they are kept for the repeat of the job, until the recording is deleted.
- When a recording is deleted, the row of its session is deleted with it. A session whose audio is still being assembled at that moment keeps its row, without the text and without the link to the recording, until it is expired by the hourly cleanup.

A session that has ended without a deletion of its recording keeps its row with ids, sizes, times, the status and error text. These rows are removed together with the account.

In earlier releases the text stayed in the row after the end of the session and after the deletion of the recording. At the first start after the upgrade, it is cleared once in every session that is finalized, canceled or expired, in every failed upload in slices, and in every failed session whose recording no longer exists (migration `0010_clear_ended_session_text`). No row is deleted by this step, and the log line holds a count only.

Tombstones, webhook delivery rows, abandoned recording sessions and leftover files of incognito requests are removed by one hourly cleanup. With `RECORDING_SESSION_CLEANUP_INTERVAL_SECONDS=0` that cleanup is off, and none of the four is removed. Audit log rows are removed by a separate daily run.

Automatic retention is off by default. An administrator enables it with `ENABLE_AUTO_DELETION=true` and `GLOBAL_RETENTION_DAYS`, and selects with `DELETION_MODE` whether the whole recording (`full_recording`) or only its audio (`audio_only`) is removed. Details are given in [Retention](retention.md).

## Logs

All log output is written to the standard output of the container. No log file is created by the application, and no HTTP access log is configured for the application server. The retention of the output depends on the Docker logging driver and on any log collector of the host.

| `LOG_LEVEL` | Content of the log |
|---|---|
| `ERROR`, `WARNING` | Failures, with ids. An error of a transcription service, an LLM provider or the embedding API is logged as its class, provider, HTTP status, error code and length, without its text. |
| `INFO` (default) | In addition: ids, lengths, counts, durations, model names and service addresses. No transcript, summary, title, prompt, hotword or search query. Users, groups, tags, folders, speakers, naming templates and recordings are named by id. A file is named by a label with an identifier, without its original file name; the labels are listed below the table. |
| `DEBUG` | In addition: titles, summaries, prompts, model replies, search queries, hotwords, usernames, group, tag and speaker names, original file names, recipient addresses of sent email, and the full text and traceback of provider errors. |

`DEBUG` is unsuitable for a deployment with regulated data, except for a short diagnosis on a test instance.

The storage path of a recording ends with the original file name. At `INFO` and above, a file is named by one of the following labels. No label holds the original file name, and no label consists of the extension alone.

| File | Label | Identifier |
|---|---|---|
| Upload in the staging folder | `<stored file 20261115101500_0123456789ab.mp3>` | Timestamp and the first 12 characters of the random id |
| Stored media of a recording, named by its storage path | `<stored file local://recordings/2026/11/42/20261115101500.mp3>` | Directory part with the id of the recording, and the timestamp at the start of the name |
| Stored media named by its local path, and the files derived from it (converted, compressed and extracted audio, chunks) | `<stored file 20261115101500.mp3 of recording 42>`, `<stored file 20261115101500.mp3 chunk 3 of recording 42>` | Timestamp, chunk number and id of the recording |
| Converted file in the staging folder | `<stored file 20261115101500_0123456789ab.mp3>` or `<staged file 0123456789ab.mp3>` | Timestamp of the source file, where its name has one, and the first 12 characters of the random id |
| Download copy of an S3 object | `<s3 copy ab12cd34.mp3 of recording 42>` | Random part of the temporary file name |
| Any other file: an input of the watch folder, an export file, a temporary file | `<file 4f3a1c9b2d.m4a>` | Keyed digest of the file name |

The keyed digest is the first 10 hexadecimal characters of an HMAC-SHA-256 of the file name. The key is derived from `SECRET_KEY`, so the same file name has the same label in every worker process and after a restart, and a guessed file name cannot be confirmed from a label without the key. After a change of `SECRET_KEY` the labels differ from the earlier ones.

In the lines of a job of the queue, every label ends with the id of the recording. After an upload is stored, one line holds the staging label, the id of the recording and the label of the stored media: `Stored upload <stored file 20261115101500_0123456789ab.mp3> of recording 42 as <stored file local://recordings/2026/11/42/20261115101502.mp3>`. A staging object of the FunASR connector is named by its folder, the digest of its name and the extension.

Errors of the operating system, the database and FFmpeg are logged with their text at `WARNING` and `ERROR`. In the upload, conversion, probe, chunking, watch folder, export, storage deletion and FFmpeg code, a file named in such text is replaced by its label. This covers storage paths and keys, names of staged and stored files, quoted paths, absolute paths that end in a file name with an extension, and paths below the upload, staging, watch and export folders. In the traceback of an error logged above `DEBUG`, the same replacement is applied to the exception text; the lines of the frames, with file, line number and source line, are unchanged. A relative file name with spaces, written without quotes, is not recognized. In other code a file path can appear in the message of an error line.

### Error text shown to the user

The error text of a failed job is stored in `processing_job.error_message` and on the recording, shown to the owner in place of the transcript or the summary, and sent in the failure webhook and the failure email. The text is built from the error of the provider as follows:

- The failed step and the HTTP status are kept.
- From a structured error body, only the message field of the provider (`error`, `error.message`, `message`, `detail`, `msg`, `error_description`, `description` or `reason`) is kept, cut to 300 characters, with its error code and type. The other fields of the body are dropped.
- An unstructured body, such as a traceback, is kept in the bounded form in which credentials and directories are already replaced.
- Every passage of 20 or more characters, and every quoted string of 4 or more characters, that also occurs in the transcript, the summary, the notes, the title, the participants, the original file name, the initial prompt or the hotwords of the recording is replaced by `[content removed]`.
- The result is limited to 500 characters, with both ends kept.

A repetition of fewer than 20 characters outside quotation marks is not detected. Text taken from the audio itself, such as a transcribed passage in the error of a transcription service, is not detected either.

### Audit log fallback

An audit event that cannot be written to the database after four further attempts, or for which the queue of the writer is full, is written to the log as one line that begins with `AUDIT-FALLBACK`, at the level `ERROR`. The line holds the values of the audit row: event name, time, ids, outcome, the user agent and, unless `audit_log_store_ip` is `off`, the client address. It holds no title, file name or content. See [Failures](audit-log.md#failures).

### Incognito requests

During an incognito request, only metadata is logged at every level, including `DEBUG`: user id, sizes, lengths, durations, language code, connector name and status codes. Every other log record created during the request, including the records of the HTTP and LLM client libraries, is replaced by a line with its length and source location. An error of a provider is logged as its class, status code and message length, without the message. The lines of the audit log, among them an `AUDIT-FALLBACK` line, are written unchanged; for an incognito upload such a line holds the account, the time and the outcome.

## Outbound connections

No telemetry, analytics or update check is contained in the application. The following connections are made by the server.

| Destination | Data sent | Control |
|---|---|---|
| Transcription service | Audio, language, initial prompt, hotwords, speaker count, and voice samples of earlier chunks for some connectors. With asynchronous jobs of a WhisperX service, also a key built from the recording id, or from the account id for an incognito request, and a hash of the audio. | Required. The address is set with `ASR_BASE_URL` or `TRANSCRIPTION_BASE_URL` and the connector with `TRANSCRIPTION_CONNECTOR`. With a local service, the audio stays on the network of the institution. For a hosted connector, the address of its vendor is the default. |
| LLM for titles, summaries, event extraction and speaker identification | Transcript and prompts, with recording details such as participants and notes where the prompt includes them | `TEXT_MODEL_BASE_URL`. The default of the variable is `https://openrouter.ai/api/v1`. Without a text model, these features are unavailable. |
| LLM for chat and for translation | Transcript, question, chat history; text to translate | `CHAT_MODEL_BASE_URL` and `TRANSLATION_MODEL_BASE_URL`. When unset, the text model is used. |
| Embedding API | Transcript passages and search queries | Only with `ENABLE_INQUIRE_MODE=true` and `EMBEDDING_BASE_URL` set. Without `EMBEDDING_BASE_URL`, embeddings are computed inside the container. |
| Hugging Face Hub | No user data. The local embedding model is downloaded on first use. | Only with Ask Ambrose and local embeddings. The model can be placed in `HF_HOME` in advance, and the download is prevented with `HF_HUB_OFFLINE=1`. |
| Webhook addresses of users | Recording id, title, original file name, language, durations and error text, depending on the event. No transcript or summary. | All deliveries are stopped with `WEBHOOK_GLOBAL_ENABLED=false`. With `webhook_omit_titles=true`, the title and the original file name are left out. |
| SMTP server | Verification and password reset messages. For users with the completion email enabled: a message with the recording title in the subject, and a shortened error text after a failure. | No mail is sent without `SMTP_HOST`. |
| SSO provider (OIDC) | Discovery, token and user information requests during sign-in | `ENABLE_SSO` |
| S3 storage | Audio and video, under a key that ends with the original file name | Only with `FILE_STORAGE_BACKEND=s3` |
| Transcription service, at startup | A bundled test clip for the check of voice embeddings. No user data. | Sent only to the configured transcription service. The check is turned off with `DISABLE_VOICE_EMBEDDING_CHECK=true`. |

Requests to these services are made over the scheme of the configured address. With an `http://` address, audio and text cross the network without encryption.

At startup, one warning per service is written to the log when an `http://` address is configured for the transcription service (`ASR_BASE_URL`, `TRANSCRIPTION_BASE_URL`), for the text, chat or translation model, or for the embedding service, and the host of the address is another machine. No warning is written for a loopback address (`localhost`, `127.0.0.0/8`, `::1`), for a host name without a dot, which is the form of a service name on the same Docker Compose network, or for `host.docker.internal`. An address in a private range (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) is reported, because the content still crosses a network. Only the service, the variable and the host are named in the warning. Webhook addresses are set by each user per webhook, and an `http://` address is refused there unless the user allows it for that webhook, so no webhook address is checked at startup.

Scripts, styles and fonts of the web interface are downloaded once, with checksums, when the Docker image is built, and are served by the instance. At runtime nothing is loaded from a third party, and scripts, styles, fonts and connections are limited to the origin of the instance through the default `Content-Security-Policy` header.

Push subscriptions of browsers can be stored through the API, but no push message is sent by the code of this release, and no push subscription is created and no notification permission is requested by the web interface.

## Browser storage

| Store | Content | Lifetime |
|---|---|---|
| Session cookie, and a remember cookie after "Remember me" | Signed session data, with the time of the last request when an idle timeout is set | The session cookie ends with the browser session, or earlier through an idle timeout (see [Access control](#access-control)). The remember cookie lasts `REMEMBER_COOKIE_DAYS` (default 30) and is not accepted while an idle timeout is set. |
| `localStorage` | Interface preferences, the id of the last selected recording, and the defaults of the upload dialog. For an ordinary upload in slices that has not finished: the file name, size and modification time of the file with the id of its session, for at most 24 hours. The id of a recording session in progress. For the offline recorder (`speakr.offlineRecorder`): the id of the account signed in at the last online load, the interface language, whether incognito mode is the default, the date it expires and the limits of a recording, with no name, address or recording. | Until cleared in the browser. The entry of an upload is removed when the upload ends, and at sign-out; the entry of the offline recorder is written again at each signed-in online load, is not used after `REMEMBER_COOKIE_DAYS` (default 30) from that load, and is removed at sign-out. |
| `sessionStorage` | Chat threads of the open recordings, and the result of an incognito recording | Until the tab is closed, or until sign-out |
| IndexedDB `SpeakrRecordings` | Audio chunks and notes of an ordinary recording in progress, for recovery after a crash. Nothing of an incognito recording is written. | Until the recording is uploaded or discarded, or until sign-out |
| IndexedDB `SpeakrFailedUploads` | The file of an ordinary upload that failed, for a later retry, and the recordings made on the offline recorder. Each entry has the id of the account it was made under, the start time of its recovery copy and the id of its server session, so that its upload removes those copies. An incognito upload is never stored. | Until the upload succeeds or the entry is deleted, or until sign-out |
| Cache Storage of the service worker (`speakr-static-<version>`) | The application files under `/static/` only: scripts, styles, fonts, images and translation files, among them the files of the offline recorder (`/static/offline-recorder.html`). No page, no response of `/api/`, `/audio/`, `/recording/`, `/share/` or `/admin`, and nothing of an incognito request is stored. | Until the next release is opened, until sign-out, or until the site data is cleared |
| Cookie `speakr_signed_out` | The value `1`, without user data. It is set by the server in the answer to a sign-out and is removed by the sign-in page once the browser has been cleared. | Seconds; at most the browser session |

### Service worker

The service worker is served at `/sw.js` and is registered on the signed-in pages with the scope `/`. It is not registered for a visitor who is not signed in, such as the reader of a public link. A secure context is required by browsers: `https`, or `http` on `localhost` or `127.0.0.1`. Over plain `http` on another address no service worker exists, and the interface is used without it.

- Application files under `/static/` are kept in Cache Storage. A file of the current release is taken from there first.
- A page is always requested from the server. When the server cannot be reached, or a reverse proxy answers 502 or 504, the offline recorder is shown: the static file `/static/offline-recorder.html`, with no user data, answered with status 503 and a Content-Security-Policy that allows only its own scripts. Without the recorder's files in the cache, a fixed page with the text "You are offline" is shown, which is part of the worker file. Pages of the server are never stored, because they contain user data and CSRF tokens.
- The recorder's files are stored when the signed-in app has loaded (a message from the page to the worker): the page, its scripts and styles, the theme stylesheets and the translation files of English and of the interface language. Nothing is fetched at install.
- The offline recorder saves a recording in IndexedDB `SpeakrFailedUploads`, for the account named in the `localStorage` entry `speakr.offlineRecorder`. Without that entry (before the first signed-in online visit, after a sign-out), once the entry is older than `REMEMBER_COOKIE_DAYS` (default 30), and when incognito mode is on by default, it records nothing. A recording stops at the first of `RECORDING_MAX_HOURS`, the in-app recorder's 200 MB, the `max_file_size_mb` upload limit and the browser's free storage (`navigator.storage.estimate()`). It lists only the waiting recordings of that account, by date, duration and size, and shows nothing of the library.
- Every other request is passed to the server untouched: all requests under `/api/`, `/audio/`, `/recording/`, `/share/` and `/admin`, sign-in and sign-out, uploads, every request that is not a `GET`, media requests, and requests to another origin. No API response and no content of a recording is stored.
- When a new release is opened, the cache of the release before it is deleted. A cache left by an earlier release (`Speakr-cache-<version>`) and its registration for `/static/` are removed at the first visit after the upgrade.
- Push messages, system notifications and background sync are not implemented.

### Sign-out

At sign-out, the following is removed from the browser: every entry in Cache Storage, the whole `sessionStorage` of the tab, with the result of an incognito recording and its file name, the content of the IndexedDB stores `SpeakrRecordings` and `SpeakrFailedUploads`, with the recordings made on the offline recorder, the `localStorage` entry of uploads in slices that have not finished, with their file names, sizes and modification times, and the `localStorage` entry of the offline recorder. The removal is carried out on the sign-in page that follows the sign-out, so nothing is removed when a sign-out is canceled.

When the browser holds a recording that was not finished, uploads that failed, or an upload in slices that has not finished and can still be resumed, a question is shown before the sign-out. The recording and the failed uploads exist nowhere else, and an unfinished upload in slices cannot be resumed after the sign-out. With **Cancel**, the copies and the session are kept. The slices already received by the server are discarded by the hourly cleanup once the session of the upload has expired (see [Files](#files)).

Not removed at sign-out: the other content of `localStorage`, which is the interface preferences, the defaults of the upload dialog, the id of the last selected recording and the id of a recording session in progress, and the `sessionStorage` of other open tabs of the same browser. The end of a session through an idle timeout is not a sign-out, and nothing is removed from the browser then. A recording that waits in `SpeakrFailedUploads` is uploaded only for the account it was made under: when another account signs in in the same browser, the recordings of the first account are deleted, with its crash-recovery copy in `SpeakrRecordings` and the pointer to its recording session on the server, and a notice gives their number. Entries saved by a release before this rule have no account and are uploaded by whoever is signed in. On a shared computer, every person must sign out after use and close the other tabs, or a separate browser profile must be used for each person.

## Incognito mode

Incognito mode is off by default and is enabled with `ENABLE_INCOGNITO_MODE=true`.

1. The audio is sent to `POST /api/recordings/incognito`. For a user over the monthly transcription budget, the request is refused with `403` before anything is saved. Otherwise the audio is written to a temporary file with a random name, in a folder of its own under `INCOGNITO_TEMP_DIR` that only the service account can read. Of the file name given by the user, only the extension is used on the server. The name is not written to disk or to the log, and it is returned to the browser with the result.
2. The file is converted when another format is required for the transcription service, and it is sent to the transcription service. With a WhisperX service with asynchronous jobs, the job and its result are deleted on the service after the result is fetched and after every failure.
3. A title is generated by the text model. A summary is generated when the user asks for one.
4. The folder of the request, with the temporary file, every converted copy and the chunks, is removed before the response is sent, also after an error. The copies of the audio that are written for a transcription with the VibeVoice and Tencent connectors are placed in the same folder and are deleted when the transcription ends.
5. Transcript, title and summary are returned in the response and are kept in the `sessionStorage` of the browser tab, with the file name and the file size. They are gone when the tab is closed.
6. Chat questions are sent to `/api/recordings/incognito/chat` together with the transcript, and a new summary is requested from `/api/recordings/incognito/summary` in the same way.

The text of an incognito result can be copied in the interface. The download buttons for the transcript, the summary, the notes and the chat are not shown for an incognito recording. A person who copies text from an incognito result is responsible for the place where that text is kept afterwards.

No webhook, email or notification is produced. Only metadata is logged, as described under [Logs](#incognito-requests). An error response contains a fixed message, or the standard explanation of a known kind of error, without the text of the provider.

The audio and the text are still sent to the transcription service and the LLM. The data on those services is not protected by incognito mode, so regulated data requires local services or services under a suitable agreement.

### Database rows of an incognito request

No content of an incognito request is stored on the server, and no recording row exists. The following is written to the database:

- **Usage rows.** An incognito request is counted against the monthly budgets of the user. The same rows as for an ordinary request are written to `token_usage` and `transcription_usage`: user, date, kind of operation or connector, model name, token and second counts, request count and cost. These rows hold no title, no file name, no content and no recording id, and requests in incognito mode are not marked in them.
- **One audit row.** For each incognito upload, also one that is refused or fails, one `incognito.used` row is written to the [audit log](audit-log.md). The row holds the account, the time and the outcome (`success`, `denied` or `failed`). The client address, the user agent and the id of an API token are not stored in this row, whatever the value of `audit_log_store_ip`. Nothing about the file, its size, its duration or its content is part of the row, and no row is written for an incognito chat or summary request. The row is subject to the master switch of the audit log: with `audit_log_enabled=false` it is not written.
- **Last use of the account.** The time of the last use is updated as for any other request and has no mark of incognito mode (see [Database](#database)).

The budget checks of an ordinary request apply. For a user over the monthly token budget, no title, summary or chat answer is requested from the LLM: the transcript is returned with the default title and without a summary, and a chat or summary request is refused.

### Limits of incognito mode

- A recording made in the browser in incognito mode is held in the memory of the tab only. It is lost when the tab or the browser is closed, when the page is reloaded, or after a browser crash before processing. This is stated in a notice under the incognito switch of the recorder. The limit of 200 MB for a recording held in the browser applies as before.
- When incognito is turned on for a recording that started as an ordinary one, during the recording or in the review pane, the copy saved in IndexedDB up to that moment is deleted, and nothing more is written, also after the switch is turned off again. A copy saved by an earlier release of Speakr stays in the browser until the recovery dialog is answered.
- After a crash or a kill of the server process during the request, the folder of the request remains until it is removed at the next start or by the hourly cleanup, once nothing was written to it for `incognito_temp_max_age_hours` (default 6). The value must be longer than the longest incognito request.
- A result whose deletion failed stays on the transcription service until its expiry there. In the log, such a failure appears only as a replaced record without the job id.

## Access control

- **Public links.** Anyone with a link can read the transcript and play the audio, and can read the summary and the notes when the owner includes them. An expiry date is optional. An administrator stops public sharing with `ENABLE_PUBLIC_SHARING=false`, or withdraws the permission from individual users. From that moment the page and the audio of every existing link of the instance, or of that user, are answered with `404`, the same answer as for an unknown link. The links are kept, and they are served again when public sharing is turned on again or the permission is returned. In the share list of the owner, and in the API, such a link is reported with `active: false`.
- **Internal sharing.** Sharing between accounts is enabled with `ENABLE_INTERNAL_SHARING`. With `SHOW_USERNAMES_IN_UI` and `user_search_scope` (`everyone`, `groups` or `exact`), an administrator limits the usernames that a user can see and find. With `user_search_scope` unset, the scope is `everyone` when usernames are shown and `exact` otherwise.
- **Administrators.** A site administrator has no route to read the transcript, the summary, the notes or the media of a recording of another user. No title, file name or tag name of a recording of another user is returned to a site administrator either: the admin pages hold counts and sizes per account, and the five latest failed recordings of the instance are listed on the statistics page by id, with the username, the time and the stored error text (see [Error text shown to the user](#error-text-shown-to-the-user)). The processing of recordings for Ask Ambrose on the admin page covers the recordings of all accounts. In its answer, a recording that failed is named by its id and the kind of the error, and each run is recorded in the audit log as `admin.inquire.processed`, with counts. A site administrator can also delete a recording of another user by its id through API v1, change its exemption from retention, and cancel its running job; these actions are recorded. An administrator can set the password of any account, and a person with access to the server or the database can read everything. Each password set by an administrator, each change of an account, a role or a system setting, and each reading or export of the audit log is recorded in the [audit log](audit-log.md).
- **API tokens.** Tokens are stored as SHA-256 hashes. Scopes and an expiry date are optional, and a token without a date never expires. A token in the `?token=` query parameter is refused on every method by default (`api_query_tokens_enabled=false`), and a token must be sent in a header. With `api_query_tokens_enabled=true`, a token in the query parameter is accepted on `GET` and `HEAD` requests and then appears in the access log of a reverse proxy.
- **Sessions.** Cookies are `HttpOnly` and `SameSite=Lax`. The `Secure` flag is set only with `SESSION_COOKIE_SECURE=true`. A warning is logged when `APP_BASE_URL` is an https address, or a request is received over HTTPS, while the flag is off. With `session_idle_timeout_minutes` above 0, a user must sign in again after that time without a request from the browser session; requests that are sent on a timer from an open tab are not counted, and API tokens are not affected. The default is 0, without an idle timeout. See [Session idle timeout](system-settings.md#session-idle-timeout).
- **Passwords.** At least 8 characters with upper case, lower case, digit and special character are required. After 5 wrong passwords for one account from one address, sign-in from that address is refused for 15 minutes. Both numbers are system settings (`login_max_failed_attempts`, `login_lockout_minutes`).
- **Single sign-on.** With `ENABLE_SSO=true`, sign-in is delegated to an OIDC provider, and the password form is removed for everyone except administrators with `SSO_DISABLE_PASSWORD_LOGIN=true`.

## Checklist for a fully local deployment

1. Run the transcription service on hardware of the institution, and set `ASR_BASE_URL` (or `TRANSCRIPTION_BASE_URL`) to it. See [WhisperX Setup](whisperx-setup.md).
2. Run the LLM locally, and set `TEXT_MODEL_BASE_URL` to it explicitly, because the default is a hosted service. Leave `CHAT_MODEL_BASE_URL` and `TRANSLATION_MODEL_BASE_URL` unset or point them to local services.
3. Leave `EMBEDDING_BASE_URL` unset or point it to a local service. With Ask Ambrose and local embeddings, place the model in `HF_HOME` and set `HF_HUB_OFFLINE=1`.
4. Use HTTPS, or a private network that only these hosts share, between Speakr and each of these services. Read the warnings about plain `http://` addresses in the log after the first start.
5. Set `WEBHOOK_GLOBAL_ENABLED=false`, unless webhooks to internal systems are approved. For approved webhooks, set `webhook_omit_titles=true`, and set `webhook_delivery_retention_days` in line with the records policy of the institution.
6. Leave `SMTP_HOST` unset, or use the mail server of the institution. Recording titles appear in completion emails.
7. Keep `FILE_STORAGE_BACKEND=local`, or use an S3 service of the institution.
8. Terminate TLS at a reverse proxy, set `SESSION_COOKIE_SECURE=true`, and set `TRUSTED_PROXY_HOPS` to the number of proxies. Turn off or protect the access log of the proxy. Set `session_idle_timeout_minutes` to the idle time allowed by the institution, and leave `api_query_tokens_enabled` at `false`, so that no token is accepted in a URL.
9. Place the data volume (`/data/uploads`, `/data/instance`), the database and the backups on encrypted disks or volumes. No data is encrypted by the application.
10. Keep `LOG_LEVEL` at `INFO` or higher, and set a size limit and a retention period for the container logs. At `INFO`, a file is named without its original file name; the remaining case, the text of a system error, is described under [Logs](#logs).
11. Set `ENABLE_PUBLIC_SHARING=false`. Existing public links are then answered with `404`.
12. Set `ALLOW_REGISTRATION=false`. Use `ENABLE_SSO=true` with `SSO_DISABLE_PASSWORD_LOGIN=true` when the institution has an identity provider.
13. Define retention with `ENABLE_AUTO_DELETION`, `GLOBAL_RETENTION_DAYS` and `DELETION_MODE`, in line with the records policy of the institution.
14. Leave `ENABLE_AUTO_EXPORT`, `ENABLE_AUTO_PROCESSING` and `PRESERVE_TEMP_AUDIO` unset unless their folders are covered by the same protection. With automatic export in use, set `auto_export_delete_with_recording=true`.
15. Apply the retention and the encryption of the live system to its backups. Recordings deleted later are still present in an older backup.
16. Keep a list of the people with access to the host, the Docker daemon, the database and the backups. Each of them can read all recordings.
17. Set `audit_log_retention_days` to the period required by the institution, and decide on `audit_log_store_ip` and `audit_log_record_views`. Export the [audit log](audit-log.md#access-for-an-external-system) on a schedule to a system outside the reach of the server administrators, and keep the `AUDIT-FALLBACK` lines of the container log.
18. Instruct users on shared computers to sign out after use and to close the other tabs, or give each user a separate browser profile. The data removed at sign-out is listed under [Sign-out](#sign-out).

## Known limits

- **Access that is not recorded in the audit log.** Lists of recordings, searches, questions to Ask Ambrose or to the chat of a recording, shares made by a group tag or folder, and text that is copied or saved in the browser after a recording was opened are not recorded. Up to one second of read and sign-in events can be missing after a kill of the server process. The full list is given under [Events that are not recorded](audit-log.md#events-that-are-not-recorded).
- **The audit log can be changed in the database.** No row is changed or deleted through a route of the application, but the table is not protected against a person with access to the database. A copy on another system is made with the export.
- **No encryption at rest in the application.** Database content, media files, webhook signing secrets and exported files are stored as plain data.
- **Content in the browser.** Audio of an ordinary recording in progress and files of failed ordinary uploads are kept in IndexedDB, and chat threads in `sessionStorage`, as listed under [Browser storage](#browser-storage). The file name, size and modification time of an unfinished upload in slices are kept in `localStorage` for at most 24 hours. All of these are removed at sign-out, but not when a session ends through an idle timeout or when the browser is closed without a sign-out. The id of the last selected recording stays in `localStorage` after a sign-out.
- **Titles and file names after deletion.** They are kept in the webhook delivery rows for `webhook_delivery_retention_days`, unless `webhook_omit_titles` is on.
- **Exported files after deletion.** They are kept under a `[deleted]_` name unless `auto_export_delete_with_recording` is on.
- **System error text in the default log.** Errors of the operating system, the database and FFmpeg are logged with their text, as described under [Logs](#logs).
- **Short repetitions in error text shown to the user.** See [Error text shown to the user](#error-text-shown-to-the-user).
- **No idle timeout by default** for sessions, and no expiry for API tokens without a date. An idle timeout is set with `session_idle_timeout_minutes`.
- **Database rows of incognito requests.** The usage counts and one audit row per upload are stored, as listed under [Database rows of an incognito request](#database-rows-of-an-incognito-request).
