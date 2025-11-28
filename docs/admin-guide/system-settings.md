# System Settings

System Settings is where you configure the fundamental behaviors that affect every user and recording in your Speakr instance. These global parameters shape how the system operates, from technical limits to user-facing features.

![System Settings](../assets/images/screenshots/Admin system settings.png)

## Transcript Length Limit

The transcript length limit determines how much text gets sent to the AI when generating summaries or responding to chats. This seemingly simple number has a big effect on both quality and cost.

When set to "No Limit," the entire transcript goes to the AI regardless of length. This ensures the AI has complete context but can become expensive for long recordings. A two-hour meeting might generate 20,000 words of transcript, consuming significant API tokens and potentially overwhelming the AI model's context window. This limit will also be applied to the speaker auto-detection feature in the speaker identification modal.

Setting a character limit (like 50,000 characters) creates a ceiling on API consumption. The system will truncate very long transcripts, sending only the beginning portion to the AI. This keeps costs predictable but might mean the AI misses important content from later in the recording.

The sweet spot depends on your use case. For typical meetings under an hour, 50,000 characters usually captures everything. For longer sessions, you might increase this limit or train users to split recordings. Monitor your API costs and user feedback to find the right balance.

## Maximum File Size

The file size limit protects your system from being overwhelmed by massive uploads while ensuring users can work with reasonable recordings. The default 300MB accommodates several hours of compressed audio, which covers most use cases.

Raising this limit allows longer recordings but requires careful consideration. Larger files take longer to upload, consume more storage, and might timeout during processing. Your server needs enough memory to handle these files, and your storage must accommodate them. Network timeouts, browser limitations, and user patience all factor into what's practical.

If users frequently hit the limit, consider whether they really need single recordings that long. Often, splitting long sessions into logical segments produces better results - easier to review, faster to process, and more focused summaries.

## ASR Timeout Settings

The ASR timeout determines how long Speakr will wait for advanced transcription services to complete their work. The default 1,800 seconds (30 minutes) handles most recordings, but you might need to adjust based on your transcription service and typical file sizes.

Setting this too low causes longer recordings to fail even when the transcription service is working normally. The recording appears stuck in processing, then eventually fails, frustrating users who must retry or give up. Setting it too high ties up system resources waiting for services that might have actually failed.

Your optimal timeout depends on your transcription service's performance and your users' recording lengths. Monitor processing times for successful transcriptions and set the timeout comfortably above your longest normal processing time. If you regularly process multi-hour recordings, you might need 3,600 seconds or more.

## Recording Disclaimer

The recording disclaimer appears before users start any recording session, making it perfect for legal notices, policy reminders, or usage guidelines. This markdown-formatted message ensures users understand their responsibilities before creating content.

Organizations often use this for compliance requirements - reminding users about consent requirements, data handling policies, or appropriate use guidelines. Educational institutions might note that recordings are for academic purposes only. Healthcare organizations could reference HIPAA compliance requirements.

!!! info "Full Markdown Support (v0.6.2+)"
    The recording disclaimer now supports **full markdown formatting**, including:

    - **Headings** - Structure your disclaimer with `# Main Title` and `## Sections`
    - **Lists** - Bulleted and numbered lists for clear requirements
    - **Bold and Italic** - Emphasize important terms with `**bold**` or `*italic*`
    - **Links** - Reference detailed policies with `[Privacy Policy](https://yoursite.com/privacy)`
    - **Code blocks** - Include examples or technical requirements
    - **Blockquotes** - Highlight key legal notices

    Example markdown disclaimer:
    ```markdown
    ## Recording Consent Required

    By starting this recording, you agree to:

    1. Obtain consent from all participants
    2. Comply with [company privacy policy](https://example.com/privacy)
    3. Handle recordings according to **GDPR** requirements

    > **Important**: Recordings containing sensitive information must be deleted within 30 days.
    ```

Keep disclaimers concise and relevant. Users see this message frequently, so lengthy legal text becomes an ignored click-through. Focus on the most important points, and link to detailed policies if needed. The markdown support lets you format the message clearly for better readability and comprehension.

## System-Wide Impact

Every setting on this page affects all users immediately. Changes take effect as soon as you save them, without requiring system restarts or user logouts. This immediate application means you should test changes carefully and communicate significant modifications to your users.

The refresh button reloads settings from the database, useful if multiple admins might be making changes or if you want to ensure you're seeing the latest values. The interface shows when each setting was last updated, helping you track changes over time.

## Troubleshooting Common Issues

When recordings fail consistently, check if they're hitting your configured limits. The error logs will indicate if files are too large or if processing is timing out. Users might not realize their recordings exceed limits, especially if they're uploading existing content rather than recording directly.

If API costs spike unexpectedly, review your transcript length limit. A single user uploading many long recordings could dramatically increase consumption if no limit is set. The combination of user activity and system settings determines your actual costs.

Processing backlogs might indicate your timeout is too high. If the system waits 30 minutes for each failed transcription attempt, a series of problematic files could block the queue for hours. Balance patience for slow processing with the need to fail fast when services are actually down.

## Environment Variable Configuration

Beyond the UI-configurable settings above, several environment variables in your `.env` file control fundamental system behaviors. These require instance restart to take effect.

### Collaboration & Sharing

**ENABLE_INTERNAL_SHARING**: Controls user-to-user sharing capabilities. Set to `true` to enable internal sharing features, allowing users to share recordings with specific colleagues. Required for group functionality. Default: `false`.

**SHOW_USERNAMES_IN_UI**: Controls username visibility in the interface. When `true`, usernames are displayed throughout the UI when sharing and collaborating. When `false`, usernames are hidden - users must know each other's usernames to share recordings (they type the username manually). Default: `false`.

**ENABLE_PUBLIC_SHARING**: Controls whether public share links can be created. When `true`, authorized users can generate secure links for external sharing. When `false`, only internal sharing is available. Default: `false`.

### User Permissions

**USERS_CAN_DELETE**: Determines whether regular users can delete their own recordings. When `true`, users see delete buttons for their recordings. When `false`, only administrators can delete recordings. This helps prevent accidental data loss and maintains content retention for compliance. Default: `true`.

### Retention & Auto-Deletion

**ENABLE_AUTO_DELETION**: Enables the automated retention system. When `true`, recordings older than the retention period are automatically processed for deletion. Default: `false`.

**DEFAULT_RETENTION_DAYS**: Global retention period in days for recordings without tag-specific retention. Set to `0` to disable auto-deletion. Tag-level retention policies can override this default. Default: `0` (disabled).

**DELETION_MODE**: Controls what gets deleted: `audio_only` removes audio files but preserves transcriptions and metadata, while `full_recording` removes everything. Audio-only mode maintains searchable records while saving storage space. Default: `audio_only`.

For detailed retention configuration, see the [Retention & Auto-Deletion](retention.md) guide.

### Background Processing Queues

Speakr uses separate job queues for transcription and summarization to prevent slow ASR processing from blocking quick summary generation.

**JOB_QUEUE_WORKERS**: Number of workers for transcription jobs (ASR processing). These are slow jobs that can take 5-30 minutes. Default: `2`.

**SUMMARY_QUEUE_WORKERS**: Number of workers for summary jobs (LLM API calls). These are fast jobs that typically complete in under a minute. Default: `2`.

**JOB_MAX_RETRIES**: How many times a failed job will be retried before being marked as failed. Default: `3`.

Jobs are persisted to the database and survive application restarts. If Speakr restarts while jobs are processing, they automatically resume from where they left off.

### Configuration Example

```bash
# Enable collaboration features
ENABLE_INTERNAL_SHARING=true
SHOW_USERNAMES_IN_UI=true
ENABLE_PUBLIC_SHARING=false

# User permissions
USERS_CAN_DELETE=false  # Only admins can delete

# Retention policy
ENABLE_AUTO_DELETION=true
DEFAULT_RETENTION_DAYS=90
DELETION_MODE=audio_only

# Processing queue
JOB_QUEUE_WORKERS=2
JOB_MAX_RETRIES=3
```

After modifying environment variables, restart your Speakr instance for changes to take effect:
```bash
docker compose restart
```

---

Next: [Default Prompts](prompts.md) →