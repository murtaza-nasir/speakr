# Tencent Cloud recording-file ASR

The `tencent_asr` connector uses Tencent's recording-file API directly; no ASR
bridge container or local GPU is needed. Configure a separate text model as
usual for summaries and chat.

```env
TRANSCRIPTION_CONNECTOR=tencent_asr
TENCENT_SECRET_ID=your-secret-id
TENCENT_SECRET_KEY=your-secret-key
TENCENT_ASR_MODEL=16k_zh_en_meeting
```

Activate Tencent Cloud ASR and use credentials with `CreateRecTask` and
`DescribeTaskStatus` permissions. Use a **recording-file recognition** package
matching the engine, or enable the appropriate pay-as-you-go service. A real-time
recognition package cannot pay for recording-file recognition. Keep credentials
in your deployment's environment/secrets, never in source control. Audio is
sent to Tencent; this is cloud transcription, not local inference.

## Engines and recognition settings

Supported engines are `16k_zh_en_meeting` (default) and `16k_zh_en_2.0`.
The meeting engine requires provider-side speaker separation; switching the UI
diarization toggle off still hides speaker labels in Speakr. Use `16k_zh_en_2.0`
to disable provider-side separation too. Language is determined by the selected
engine, not the UI language hint. Speaker count hints are not supported by these
16 kHz engines. Segments include sentence timestamps and anonymous speaker labels;
this does not identify people's names.

Comma-separated UI hotwords are sent with weight 5. Tencent accepts up to 128
words of at most 30 UTF-8 bytes each (10 Chinese characters), without `|`;
other words are left out of the request and noted in the log. Alternatively set
`TENCENT_ASR_HOTWORD_ID` to a Tencent hotword table ID. Request hotwords take
precedence. Optional
`TENCENT_ASR_TIMEOUT=1800` and `TENCENT_ASR_POLL_INTERVAL=5` control polling
in seconds (maximum timeout 10800).

## Large files and storage

The connector declares original-audio passthrough, so Speakr skips audio
preprocessing (including forced WebM conversion) and the MP3 error-retry path
for it. This applies to audio recordings; video extraction is a separate app
feature. The connector never compresses or splits audio. Files up to 5 MiB are sent
directly. Larger files use the existing Speakr S3
backend: configure `S3_BUCKET_NAME`, `S3_REGION`, `S3_ENDPOINT_URL`,
`S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`. The endpoint and signed download
URL must be reachable by Tencent over the public internet; a NAS-only address
will not work. The bucket can remain private. This uses Speakr's existing
S3-compatible client, not a separate COS SDK; verify your chosen storage
provider's compatibility before relying on it. Local recording storage can
remain selected (`FILE_STORAGE_BACKEND=local`).

Tencent COS example (use your own bucket, credentials and region):

```env
FILE_STORAGE_BACKEND=local
S3_BUCKET_NAME=your-bucket-1234567890
S3_REGION=ap-hongkong
S3_ENDPOINT_URL=https://cos.ap-hongkong.myqcloud.com
S3_ACCESS_KEY_ID=your-cos-secret-id
S3_SECRET_ACCESS_KEY=your-cos-secret-key
S3_USE_PATH_STYLE=false
S3_VERIFY_SSL=true
TENCENT_ASR_STAGING_PREFIX=recordings/speakr
```

These storage credentials need object upload, read and delete permissions.
They may differ from the ASR credentials. COS uses virtual-host addressing;
the backend disables optional AWS checksum trailers for COS compatibility.
Staging uploads explicitly set a private object ACL, even for an existing
image bucket. Check that no bucket policy independently grants public access.
There is no requirement to store audio with Alibaba or AWS.

Staging objects use the configurable `TENCENT_ASR_STAGING_PREFIX` (default
`tencent-asr/`) and 24-hour signed URLs. They are
deleted after a terminal task result. Configure a bucket lifecycle rule to
expire this prefix after at least two days: timed-out tasks, interrupted
workers, and uncertain submissions retain their objects so Tencent can finish
downloading them. These objects contain audio; storage charges may apply.
The API limits URL jobs to 1 GiB and five hours. The operator should configure
expiration and incomplete-multipart-upload cleanup on this prefix only;
Speakr does not change bucket lifecycle policies automatically.

## Failures and retries

When Tencent accepts a task, its task ID is saved with the processing job. If
polling times out, an error occurs or Speakr restarts while the task runs, the
job is retried automatically and the saved task is polled again; the audio is
not submitted a second time. Reprocessing a recording starts a new job, and the
audio is submitted again, which can create another billable task.

If no answer arrives while the audio is being submitted, Tencent may still have
accepted the task. That job is not retried automatically, and the recording
shows an error. Check the Tencent ASR console for the task before reprocessing
the recording.

A saved task older than 23 hours is not queried, since Tencent keeps results for
24 hours; the audio is submitted again on that retry. A task that failed on
Tencent's side is not submitted again automatically: check the console, then
reprocess the recording.

Staged audio is deleted when its task finishes. The bucket lifecycle rule
removes objects left after an uncertain submission or an interrupted worker.

API reference: [CreateRecTask](https://cloud.tencent.com/document/api/1093/37823).
