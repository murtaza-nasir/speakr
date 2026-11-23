# Audit log

The audit log is a record of sign-ins, of access to recordings, of changes, of shares and of administrative actions. It is kept in one database table, `audit_event`, and is shown to site administrators on the **Audit log** tab of the admin page. The log is on by default, also after an upgrade from a release without it.

The content of a recording is never part of the log. The data that is stored, and the data that is not, are listed on this page. The place of the log among the other stored data is described in [Privacy and regulated data](privacy.md).

## Content of a row

| Column | Content |
|---|---|
| `id` | A number that is never given out again. A removed row is visible as a gap. |
| `created_at` | Time of the event, in UTC |
| `event`, `event_group` | Name of the event (see [Events](#events)) and its group |
| `actor_kind` | `session` (browser), `api_token`, `sso_token`, `public_link`, `system` (a scheduled job) or `anonymous` (no account, for example a refused sign-in) |
| `actor_user_id`, `actor_token_id` | Account and API token of the request, by id |
| `ip`, `user_agent` | Client address, subject to `audit_log_store_ip`, and the first 160 characters of the user agent. Both are empty for `incognito.used`. |
| `recording_id`, `recording_owner_id` | Recording and its owner at the time of the event |
| `target_user_id`, `group_id` | Account or group that the event concerns |
| `outcome` | `success`, `denied` or `failed` |
| `details` | A few values of the event, from a fixed list of keys per event |

The following is never stored in a row: titles, file names, transcript, summary and note text, search queries, chat messages, names of API tokens and of groups, the address part of a public link, email addresses, passwords and error text. Accounts, recordings, tokens, shares and groups are named by id. A username is stored in two events only, `admin.user.created` and `admin.user.deleted`, so that the rows of an account can still be attributed after its deletion.

A row has no link to the recording, the account or the group it names. The rows therefore remain after a deletion, until they are removed by the retention period.

## Events

### Sign-in and session

| Event | Recorded when | Details |
|---|---|---|
| `auth.login.succeeded` | A sign-in with a password or through SSO succeeded | `method`, `remember` |
| `auth.login.failed` | A sign-in was refused. One row per minute for each address and account. | `method`, and `reason`: `bad_password`, `unknown_account`, `no_local_password`, `locked`, `password_login_disabled`, `unverified_email` or `sso_error`. The address that was typed is not stored. |
| `auth.login.locked` | The limit of wrong passwords was reached and sign-in was locked | `minutes` |
| `auth.logout` | A signed-in user signed out | |
| `auth.registered` | An account was created through the registration page | |
| `auth.password.changed` | A user changed the password of the own account | |
| `auth.password.reset_requested` | A password reset was requested. One row per five minutes for each address and account. | |
| `auth.password.reset` | A password was set through a reset link | |
| `auth.sso.linked`, `auth.sso.unlinked` | An account was linked to, or unlinked from, the SSO provider | |
| `token.created`, `token.changed`, `token.revoked` | An API token was created, renamed or narrowed, or revoked | `token_id`, `scopes`, `expires`, `sso_client_id` |
| `token.first_used` | An API token was used for the first time | `token_id` |

For every later request with an API token, the id of the token is stored in `actor_token_id`.

### Access to a recording

| Event | Recorded when | Details |
|---|---|---|
| `recording.viewed` | A recording was opened in the web interface, or read with `GET /api/v1/recordings/{id}`. One row per 30 minutes for each account or token and recording. | |
| `recording.content.read` | The transcript, summary, notes, speakers or events of a recording were read through API v1. One row per 30 minutes for each token, recording and part. | `part` |
| `recording.media.played` | The audio or video was requested for playback, or a speaker sample was played. One row per 30 minutes. | |
| `recording.media.downloaded` | The audio or video was requested as a download. One row per 5 minutes. | |
| `recording.downloaded` | The transcript, summary, notes, chat or calendar events were downloaded as a file | `part`, `format` |
| `recording.exported` | Existing recordings were written to the export folder | `count` |

With `recording.viewed`, the transcript, the summary and the notes were delivered to the browser. Text copied or saved in the browser afterwards is not visible to the server and is not recorded.

A request for a recording that exists and that the account has no access to is recorded as the same event with the outcome `denied`, once per minute. A request for a recording that does not exist is not recorded.

With media on S3 storage, a request is answered with a redirect to the storage. The redirect is recorded as one play or download.

### Changes

| Event | Recorded when | Details |
|---|---|---|
| `recording.edited` | The title, participants, notes, summary, transcript, translation, speakers, meeting date or retention exemption of a recording were changed | `fields`: the names of the changed fields |
| `recording.deleted` | A recording was deleted | `reason`: `user`, `retention`, `account_deleted`, `ownerless`, `upload_canceled` or `merged` |
| `recording.audio.deleted` | The audio of a recording was deleted and the text was kept | `reason`: `user` or `retention` |
| `recording.reprocessed` | A new transcription, summary, title or translation was started | `kind` |
| `recording.job.canceled` | A waiting or running job of a recording was canceled | `kind` |
| `recording.trimmed` | A trim of the audio and the transcript of a recording was started | `start` and `end` (seconds of the audio before the trim), `removed_segments` |

A deletion by a retention rule is recorded with the actor `system`.

### Sharing

| Event | Recorded when | Details |
|---|---|---|
| `share.internal.created`, `share.internal.changed`, `share.internal.revoked` | A recording was shared with another account, the permissions of the share were changed, or the share was revoked | `share_id`, `can_edit`, `can_reshare`, the earlier values on a change, `cascade` for a share that was revoked together with another one |
| `share.public.created`, `share.public.changed`, `share.public.revoked` | A public link was created, its options were changed, or it was revoked | `share_id`, `share_summary`, `share_notes`, `expires` |
| `share.public.opened` | The page of a public link was opened. One row per 30 minutes for each link and visitor. | `share_id` |
| `share.public.media.played` | The audio of a public link was requested. One row per 30 minutes for each link and visitor. | `share_id` |

The address part of a public link gives access to the recording and is never stored in the log. A visitor of a public link has no account, so the row holds the client address and the user agent only.

Rows with `imported` in the details were copied from the table `share_audit_log` of earlier releases (see [Upgrade](#upgrade)).

### Administration

| Event | Recorded when | Details |
|---|---|---|
| `admin.user.created`, `admin.user.deleted` | An administrator created or deleted an account | `username`, `is_admin`, `recordings_deleted` |
| `admin.user.changed` | An administrator changed an account | `fields`: the names of the changed fields |
| `admin.user.password_set` | An administrator set the password of an account | `self` |
| `admin.user.unlocked` | An administrator ended the sign-in lock of an account | |
| `admin.user.role_changed` | The administrator role was given or taken away | `old`, `new` |
| `admin.setting.changed` | A system setting was saved or cleared | `key`. For settings that hold a switch, a number or a fixed choice: `old` and `new`. For every other setting, only the key. A key that is not a setting of the application is stored as `other`. For a [user default](system-settings.md#user-defaults): `old`, `new` and `users_updated`, the number of existing accounts that got the new default. |
| `admin.llm_model.created` | An administrator added a configured language model | `model`, the id of the model, and `fields`, the names of the fields that were set. Labels, API keys and field values are never stored. |
| `admin.llm_model.changed` | An administrator changed a configured language model | `model` and `fields`, the names of the fields that changed. Labels, API keys and field values are never stored. |
| `admin.llm_model.deleted` | An administrator deleted an unassigned language model | `model` |
| `admin.llm_role.assigned` | A language model was assigned to the text, chat or translation role | `role`, `model`, and `old`, the previous model id when there was one |
| `admin.llm_role.reverted` | A role was returned to its environment configuration | `role` and `old`, the model id it used before |
| `admin.retention.run` | Automatic deletion was started by hand | Counts |
| `admin.inquire.processed` | An administrator started the processing of recordings for Ask Ambrose on the admin page. The recordings of all accounts are processed. | `processed`, `failed`, `total`, `force` |
| `group.created`, `group.changed`, `group.deleted` | A group was created, changed or deleted | `fields` |
| `group.member.added`, `group.member.role_changed`, `group.member.removed` | A member was added to a group, changed in role, or removed | `role`, `old_role` |
| `audit.viewed`, `audit.exported` | The audit log was read (one row per 30 minutes for each administrator) or exported | `filters`, `count`, `format` |
| `audit.pruned` | Rows past the retention period were removed | `count`, `cutoff` |
| `audit.write_failed` | Events could not be stored (see [Failures](#failures)) | `count` |

### Incognito

| Event | Recorded when | Details |
|---|---|---|
| `incognito.used` | An incognito upload was requested. The outcome is `success`, `denied` (incognito mode is off, or the transcription budget is used up) or `failed`. | None |

One row is written for each incognito upload, also for one that is refused or fails, with the account, the time and the outcome. The client address, the user agent and the id of an API token are not stored in this row, whatever the value of `audit_log_store_ip`. Nothing about the file, its size, its duration or its content is stored. Incognito chat and summary requests are not recorded.

## Events that are not recorded

- Lists of recordings, searches, and questions to Ask Ambrose or to the chat of a recording
- The creation of a recording by an upload
- Changes of tags, folders, and of the personal inbox, star and archive state
- Shares made by a group tag or a group folder
- A sign-in through the "Remember me" cookie, and a request with an invalid API token
- The status requests that are sent on a timer from an open page, and the waveform of a recording
- The statistics pages of a site administrator. They hold counts and sizes per account, and the five latest failed recordings by id, with the username, the time and the stored error text, without a title or a file name.
- Text copied or saved in the browser after a recording was opened

## Settings

The settings are changed under **System settings** or in `.env`. A value in `.env` applies until an administrator saves a value in System settings.

| Setting | Variable in `.env` | Default | Effect |
|---|---|---|---|
| `audit_log_enabled` | `AUDIT_LOG_ENABLED` | `true` | `false`: no event is recorded, with the exceptions below. |
| `audit_log_retention_days` | `AUDIT_LOG_RETENTION_DAYS` | `365` | Days after which a row is removed. `0`: rows are kept without limit. |
| `audit_log_store_ip` | `AUDIT_LOG_STORE_IP` | `full` | `full`: the client address is stored. `truncated`: the `/24` network of an IPv4 address or the `/48` network of an IPv6 address is stored. `off`: no address is stored. |
| `audit_log_record_views` | `AUDIT_LOG_RECORD_VIEWS` | `true` | `false`: `recording.viewed` and `recording.media.played` are not recorded. Downloads, exports, reads through API v1 and the events of public links are recorded in either case. |

While the log is off, four kinds of events are still recorded, so that turning the log off is itself on record: a change of one of the `audit_log_*` settings, the reading and export of the log, the removal of old rows, and the `audit.write_failed` row. Every other event, including `incognito.used`, is not recorded while the log is off.

A change of `audit_log_store_ip` applies to new rows. Addresses in existing rows are not changed.

Rows past the retention period are removed once a day, at 2:00 in the time of the server. A period of 2190 days is six years. The period required for a deployment is decided by its compliance office.

The client address is the address received by the application. Behind a reverse proxy, `TRUSTED_PROXY_HOPS` must be set, otherwise the address of the proxy is stored.

## The Audit log tab

The tab is shown to site administrators. Group administrators have no access to it.

- **Table.** The newest events are listed first, in the order in which they were written. An event of a read or a sign-in is written up to a second after it happened, so two events of the same second can be listed in either order; the time of each event is exact. A row is opened with a click and then shows the details, the affected account, the owner of the recording and the user agent. A deleted account is shown as "Deleted user" with its id.
- **Refresh.** With **Refresh**, the page on screen is read again with the same filters. Events queued in the server process that answers the request are written before the list is read, so the list is complete up to the moment of the request for that process.
- **Filters.** From, To, User (a username or an id; events by that account and events that concern it), Events (a group of events), Recording ID, Result and Access by. Dates are days in the time zone of the browser.
- **Paging.** The result is paged with **Older** and **Newer**, 50 rows at a time.
- **Export.** With **Export CSV** and **Export JSONL**, the rows of the current filter are downloaded as a file, with the columns of the table unchanged. In the CSV file, a text cell that begins with `=`, `+`, `-` or `@` is preceded by an apostrophe, so that it is not read as a formula in a spreadsheet. An export is a snapshot: queued events of the serving process are written first, and the file ends at the highest matching event id at the start of the export. An event written during the download is in the next export.
- **View activity.** With the button of this name in a row of the user list, the tab is opened with the User filter set to that account.

Administrators have no page on which the content of a recording of another user is shown. The events of one recording are found with the Recording ID filter.

### Ids that are used again

On SQLite, the id of a deleted recording, account, token or share can be given to a new one. Rows of the log are therefore read together with the deletion events: the history of a recording consists of the rows after the last `recording.deleted` row with its id, and the history of an account consists of the rows after the last `admin.user.deleted` row with its id. On PostgreSQL, ids are not given out again.

## Access for an external system

The log can be collected by a monitoring or archive system through two routes of the admin page. Both require a session or a full-access API token of a site administrator. A token with scopes is refused.

```bash
# One page, newest first: events, has_older, has_newer
curl -H "Authorization: Bearer $TOKEN" \
  "https://speakr.example.org/admin/audit-log?limit=500&from=2026-11-01&group=access"

# The next page: the id of the last row of the page before
curl -H "Authorization: Bearer $TOKEN" \
  "https://speakr.example.org/admin/audit-log?limit=500&before_id=18234"

# The whole filtered result as a file, oldest first
curl -H "Authorization: Bearer $TOKEN" -o audit.jsonl \
  "https://speakr.example.org/admin/audit-log/export?format=jsonl&from=2026-11-01"
```

The parameters are `from` and `to` (a date or an ISO 8601 date and time in UTC; `to` is exclusive), `user`, `group`, `event`, `recording_id`, `outcome`, `actor_kind`, `before_id`, `after_id` and `limit` (1 to 500).

A copy kept on another host is the protection against a later change of the table. No row is changed or deleted through a route of the application, but a person with access to the database can do both. This gap is closed by a scheduled export to a system outside the reach of the server administrators.

## Failures

Events of reads and sign-ins are written by a background writer, about once per second, so that a page is never delayed by the log. Events of changes, shares and administrative actions are written in the same database transaction as the action: such a row exists exactly when the action took effect.

Each worker process of the server has its own queue. Before the list or an export is read, only the queue of the process that answers the request is written. An event of a read or a sign-in queued in another worker process can therefore arrive up to about a second later, and is listed after a refresh.

A batch that cannot be written is tried again after 1, 5, 15 and 60 seconds. After that, every event of the batch is written to the application log as one line that begins with `AUDIT-FALLBACK`, at the level `ERROR`, and an `audit.write_failed` row with the number of events is added with the next batch that is written. An event for which the queue of the writer is full is written to the application log in the same way. The line holds the same values as the row, including the client address when addresses are stored. These lines should be kept by the collector of the container log.

After a kill of the server process, up to one second of read and sign-in events can be missing. Events of changes are not affected.

## Upgrade

- **The log is on after the upgrade.** From the first start, events are recorded with the client address, for 365 days. Both are changed with the settings above. This is stated in a notice on the Audit log tab until an administrator dismisses it.
- **Internal shares.** In earlier releases, changes of internal shares were recorded in the table `share_audit_log`, whose rows were deleted with the recording and with the account that made the change. At the first start after the upgrade, these rows are copied once into `audit_event` (migration `0009_copy_share_audit_log_to_audit_event`), with time, actor, target account, recording, address and permission flags. The free-text note of the old rows is not copied. The old table is no longer written and is removed in a later release. Until then its remaining rows, with the address and the note, are still deleted with the recording, while the copies in `audit_event` remain.
- **Size.** As an estimate, a row takes about 350 bytes including its indexes, and about 150 MB per year can be expected for an installation with 40 active users per day.
