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

Files up to 5 MiB are sent directly. Larger files use the existing Speakr S3
backend: configure `S3_BUCKET_NAME`, `S3_REGION`, `S3_ENDPOINT_URL`,
`S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`. The endpoint and signed download
URL must be reachable by Tencent over the public internet; a NAS-only address
will not work. The bucket can remain private. This uses Speakr's existing
S3-compatible client, not a separate COS SDK; verify your chosen storage
provider's compatibility before relying on it. Local recording storage can
remain selected (`FILE_STORAGE_BACKEND=local`).

Staging objects use the `tencent-asr/` prefix and 24-hour signed URLs. They are
deleted after a terminal task result. Configure a bucket lifecycle rule to
expire this prefix after at least two days: timed-out tasks, interrupted
workers, and uncertain submissions retain their objects so Tencent can finish
downloading them. These objects contain audio; storage charges may apply.
The API limits URL jobs to 1 GiB and five hours.

## Failures and retries

Transient polling failures retry the same task. **Submission is never retried
by the connector.** Task IDs are logged and included in timeout errors. There
is no automatic resume across worker restarts: check the Tencent ASR console
before manually retrying a failed/timeout recording, since a new transcription
can create another billable task. A network timeout during submission can mean
Tencent accepted it even though no task ID reached Speakr.

API reference: [CreateRecTask](https://cloud.tencent.com/document/api/1093/37823).
