# Troubleshooting

When something goes wrong with Speakr, this guide helps you identify and resolve common issues quickly. Most problems fall into a few categories - [installation issues](getting-started/installation.md), transcription failures, performance problems, or feature-specific quirks. Also check the [FAQ](faq.md) for common questions. Understanding where to look and what to check saves hours of frustration.

## Installation and Setup Issues

### Container Won't Start

When your Docker container refuses to start or immediately exits, the problem usually lies in your configuration. Check your [environment file](getting-started.md#step-3-configure-your-transcription-service) first - a single typo in API keys or mismatched quotes can prevent startup. Review the [installation guide](getting-started/installation.md) for proper setup. Run `docker-compose logs app` to see the actual error messages. Common culprits include port conflicts (another service using 8899), missing volume mounts, or incorrect file permissions on your data directory.

If you see database connection errors, ensure your database file has proper permissions. The container runs as a specific user and needs read/write access to the data directories. On Linux systems, you might need to adjust ownership with `chown -R 1000:1000 ./uploads ./instance`.

### Can't Access the Web Interface

When Speakr starts successfully but you can't reach the web interface, network configuration is usually the issue. First, verify the container is actually running with `docker ps`. Check that port 8899 is properly mapped - the docker-compose file should show `"8899:8899"` in the ports section.

Firewall rules often block access, especially on cloud servers. Ensure port 8899 is open in your firewall, security groups (AWS), or network policies. If accessing from another machine, remember that `localhost` won't work - use the server's actual IP address or hostname.

### Admin Login Fails

If you can't log in with your [admin credentials](getting-started.md#step-4-configure-admin-account), first verify you're using the exact username and password from your environment file. For user management issues, see the [admin guide](admin-guide/user-management.md). These are case-sensitive and must match exactly. Check the Docker logs for admin user creation messages - you should see "Admin user created successfully" during first startup.

Sometimes the admin user creation fails silently if the password doesn't meet requirements. Ensure your admin password is at least 8 characters long. If the admin user wasn't created, you might need to remove the database file and restart the container to trigger initialization again.

## Transcription Problems

### Transcription Never Starts

When recordings stay in "pending" status indefinitely, the background processor might have stopped. Check the logs for error messages about the [transcription service](features.md#multi-engine-support). Monitor processing status in the [vector store](admin-guide/vector-store.md) admin panel. API key issues are the most common cause - verify your OpenAI or OpenRouter API key is valid and has available credits.

Network connectivity problems can also prevent transcription. The container needs to reach external API endpoints. If you're behind a corporate proxy, you'll need to configure proxy settings in your Docker environment.

### Transcription Fails Immediately

Quick failures usually indicate API authentication problems. Double-check your API keys in the environment file. Remember that OpenAI and OpenRouter use different key formats. OpenAI keys start with "sk-" while OpenRouter keys look different. Ensure you're using the right key for your configured service.

API rate limits or insufficient credits also cause immediate failures. Log into your API provider's dashboard to check your usage and limits. Some API plans have restrictive rate limits that Speakr might exceed with large files.

### ASR Endpoint Returns 405 or 404 Errors

If you're getting "405 Method Not Allowed" or "404 Not Found" errors with the Whisper ASR webservice, check your ASR_BASE_URL configuration. The URL should not include trailing comments or descriptions - remove anything after the # symbol in your environment file. For the Whisper ASR webservice, use just the base URL like `http://whisper-asr:9000` without `/asr` at the end.

When using Docker Compose, always use container names rather than IP addresses for service communication. Instead of `http://192.168.1.132:9000`, use `http://whisper-asr-webservice:9000` where `whisper-asr-webservice` is your container name.

### Poor Transcription Quality

Transcription accuracy depends heavily on audio quality. Background noise, multiple overlapping speakers, or poor microphone placement all degrade results. The AI models work best with clear, single-speaker audio or well-separated multiple speakers.

Language mismatches cause poor results too. If you've set a specific transcription language in settings but upload audio in a different language, accuracy suffers. Either set the correct language or leave it blank for auto-detection.

For recordings with multiple speakers, using the [ASR endpoint with speaker diarization](features.md#speaker-diarization) dramatically improves usability. Learn how to [identify speakers](user-guide/transcripts.md#speaker-identification) after transcription, even if the raw transcription accuracy is similar.

### Chinese Transcription Issues

For [Chinese language transcription](features.md#language-support), model selection is critical. See the [FAQ on language support](faq.md#can-speakr-transcribe-languages-other-than-english) for more details. The large-v3 model works best with Chinese content. Smaller models like distil-large-v3 may not output Chinese characters correctly even when the language is set to "zh". If you're getting romanized output instead of Chinese characters, upgrade to the large-v3 model.

### Summary Language Doesn't Match Preference

If [summaries](features.md#automatic-summarization) revert to English when you click "Reprocess Summary" despite having [language preferences](user-guide/settings.md#language-preferences) set, this might be a model limitation. Configure [custom prompts](admin-guide/prompts.md) to enforce language requirements. Some models like Qwen3-30B don't always follow language instructions correctly. Try using a different model that better respects language directives, or ensure your custom prompt explicitly specifies the output language.

## Performance Issues

### Slow Transcription Processing

Large audio files naturally take longer to process, but excessive delays indicate problems. Check your [server resources](getting-started.md#prerequisites) and review [system statistics](admin-guide/statistics.md) for performance metrics - Speakr needs adequate CPU and RAM, especially when processing multiple recordings simultaneously. The `docker stats` command shows current resource usage.

Network speed affects transcription time since audio must upload to API services. Slow internet connections create bottlenecks, particularly for large files. Consider chunking settings if you consistently work with long recordings.

The choice of transcription model impacts speed. Whisper Large is more accurate but slower than Whisper Base. If speed matters more than perfect accuracy, consider using a smaller model through your API settings.

### Files Over 25MB Fail with OpenAI

OpenAI's Whisper API has a 25MB file size limit. For larger files, enable [chunking](features.md#audio-chunking) in your environment configuration. Learn about [chunking strategies](faq.md#whats-the-difference-between-chunking-by-size-vs-duration):
```
ENABLE_CHUNKING=true
CHUNK_LIMIT=20MB  # or use duration: CHUNK_LIMIT=1400s
CHUNK_OVERLAP_SECONDS=3
```

You can specify chunk limits either by file size (MB) or duration (seconds). For models with specific duration limits like Azure's 1500-second maximum, use duration-based chunking. The system will automatically split your recordings and reassemble the transcription.

### ASR Timeout on Long Recordings

Long recordings (over 30 minutes) may timeout during ASR processing. Increase the timeout in Admin Settings > System Settings > "ASR Timeout Seconds". For a 2-hour recording, set it to at least 7200 seconds (2 hours). Very long recordings like 3+ hour files may need longer timeouts depending on the GPU you are using for transcription (if local).

### Web Interface Feels Sluggish

Browser performance degrades with very large transcriptions. Recordings over 2 hours can generate massive amounts of text that some browsers may struggle to display smoothly. The bubble view for speaker-labeled transcriptions is particularly resource-intensive.

Clear your browser cache if the interface gradually becomes slower over time. Speakr caches data locally for performance, but this cache can become corrupted. In Chrome or Firefox, hard refresh with Ctrl+Shift+R to reload fresh assets.

## Feature-Specific Issues

### Speaker Identification Not Working

[Speaker diarization](features.md#speaker-diarization) requires the [ASR endpoint](getting-started.md#option-b-custom-asr-endpoint-configuration), not standard Whisper API. Configure speaker settings in [system settings](admin-guide/system-settings.md). Verify you've configured ASR settings correctly in your environment file. The ASR_BASE_URL should point to a valid ASR service that supports diarization.

Even with ASR enabled, you must explicitly request diarization when uploading or reprocessing recordings. Speakr should do this by default, but user settings may override this behavior. Check the speaker count settings - if you set min and max speakers to 1, diarization effectively disables. Use reasonable ranges like 2-6 speakers for most recordings.

After transcription, speakers appear as generic labels (SPEAKER_01, etc.). You must manually [identify speakers](user-guide/transcripts.md#speaker-identification) by clicking the labels and assigning names. Manage your [speaker library](user-guide/settings.md#speakers-management-tab) in account settings.

### WhisperX Shows UNKNOWN_SPEAKER

If WhisperX only shows "UNKNOWN_SPEAKER" instead of numbered speakers, ensure you're using the correct ASR_ENGINE setting. Set `ASR_ENGINE=whisperx` in your Docker environment. Also verify that your HF_TOKEN (Hugging Face token) is valid, as it's required for the speaker diarization models.

### Sharing Links Don't Work

[Sharing](user-guide/sharing.md) requires your Speakr instance to be accessible from the internet with HTTPS. See [sharing requirements](user-guide/sharing.md#requirements-for-sharing) and [security considerations](user-guide/sharing.md#security-and-privacy-considerations). Local installations or non-SSL setups cannot generate working share links. The share button will be disabled or show an error explaining the requirements.

If your instance meets the requirements but shares still fail, check that your configured URL in the environment matches reality. Mismatched URLs cause share links to point to the wrong location. The URL must be exactly what external users will use to access your instance.

### Inquire Mode Returns No Results

[Semantic search](user-guide/inquire-mode.md) requires the embedding model to be properly installed and initialized. Check the [Vector Store tab](admin-guide/vector-store.md) in admin settings and review [vector store troubleshooting](admin-guide/vector-store.md#troubleshooting-common-issues) - it should show "Available" status. If not, the sentence-transformers library might be missing or failed to load.

All recordings need processing before they're searchable. The Vector Store tab shows how many recordings are processed versus pending. Use the process button to manually trigger embedding generation if automatic processing has stalled.

Query formulation matters enormously. [Inquire Mode](user-guide/inquire-mode.md) understands context and meaning, not just keywords. Learn [effective search strategies](user-guide/inquire-mode.md#asking-effective-questions) in the user guide. Ask complete questions rather than typing isolated words. "What did we decide about the budget?" works better than just "budget decision".

## Additional Considerations

### Recording Disclaimer for Legal Compliance

In many jurisdictions, you must inform participants they're being recorded. Enable a recording disclaimer in [System Settings](admin-guide/system-settings.md#recording-disclaimer). Check the [FAQ on recording compliance](faq.md#do-i-need-to-inform-people-theyre-being-recorded). Set custom text that appears before any recording starts, such as legal notices about consent requirements. This feature is particularly important in regions with strict recording laws like Australia or California.

### Offline Deployment

Speakr can run completely offline as all dependencies are built into the Docker image. For offline deployments, use local models via Ollama for [text generation](features.md#automatic-summarization) and ensure your ASR endpoint is hosted locally. The system will work without internet access once properly configured.

### Non-Docker Installation

While Docker is the only officially supported installation method, you can attempt manual installation using npm and Python. You'll need to handle dependencies, environment setup, and configuration yourself. This approach is not recommended for regular use and you'll need to troubleshoot issues independently.

## Getting Help

### Check the Logs

Docker logs contain valuable debugging information. Use `docker-compose logs -f app` to see real-time logs. Look for ERROR or WARNING messages that correspond to when problems occurred. Python tracebacks indicate code-level issues that might require support.

For ASR issues, also check the ASR container logs: `docker-compose logs -f whisper-asr-webservice`

### System Information

When requesting help, provide your system configuration from the About tab in account settings. Include the Speakr version, configured AI model, transcription service type, and any error messages. This context helps others understand your specific setup.

### Community Support

The GitHub repository's issue tracker is your best resource for reporting bugs or requesting features. Search existing issues first - someone might have already encountered and solved your problem. When creating new issues, include specific steps to reproduce the problem.

---

Next: [FAQ](faq.md) →