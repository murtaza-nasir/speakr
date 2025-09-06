# Frequently Asked Questions

These are the questions that come up most often when people start using Speakr. The answers here will save you time and help you understand how to get the most from the platform.

## General Questions

### What is Speakr exactly?

Speakr is a self-hosted web application that transforms your audio recordings into organized, searchable, and intelligent notes. It combines [transcription](features.md#core-transcription-features), [AI summarization](features.md#automatic-summarization), [speaker identification](features.md#speaker-diarization), and [semantic search](user-guide/inquire-mode.md) into a single platform you control completely. If you self-host an ASR model like a Whisper endpoint or the recommended ASR service, and an openAI compatible API for LLMs, your data never leaves your infrastructure, giving you complete privacy and control.

### How is Speakr different from other transcription services?

The key difference is self-hosting - you run Speakr on your own server, keeping complete control of your data. Beyond privacy, Speakr integrates transcription with AI-powered features like [intelligent summarization](features.md#automatic-summarization), [interactive chat](user-guide/transcripts.md) with your recordings, and [semantic search](user-guide/inquire-mode.md) across all your content. It's not just about converting speech to text; it's about making that text useful and accessible.

### What audio formats does Speakr support?

Speakr handles most common audio formats including MP3, WAV, M4A, OGG, FLAC, and more. The system uses FFmpeg internally to process audio, so essentially any format FFmpeg supports will work. Video files containing audio tracks are also supported - Speakr will extract and process the audio component.

### Can multiple people use the same Speakr instance?

Yes, Speakr is designed as a multi-user system. Each user has their own account with separate recordings, settings, and speaker libraries. Admins can [create and manage user accounts](admin-guide/user-management.md). See [system statistics](admin-guide/statistics.md) to monitor usage, monitor usage, and configure system-wide settings. Users can't see each other's recordings unless explicitly shared through [share links](user-guide/sharing.md). Learn about [sharing security](user-guide/sharing.md#security-and-privacy-considerations).

## Installation and Setup

### What are the minimum system requirements?

Speakr runs comfortably on modest hardware. You need at least 2GB of RAM, though 4GB is recommended for better performance. CPU requirements depend on your usage - a dual-core processor handles single-user instances fine, while busy multi-user installations benefit from more cores. Storage needs depend on your recording volume, but start with at least 20GB free space for the application and initial recordings.

### Do I need to know Docker to install Speakr?

Basic Docker knowledge helps but isn't essential. The [quick start guide](getting-started.md) provides exact commands to copy and run. For production deployments, see the [installation guide](getting-started/installation.md). You'll need to install Docker and Docker Compose on your server, create a configuration file with your API keys, then run a single command to start everything. The hardest part is usually getting your API keys from OpenAI or OpenRouter.

### Can I run Speakr on a Raspberry Pi?

Yes, Speakr can run on a Raspberry Pi 4 or newer with at least 4GB of RAM. Performance won't match a full server, especially for transcription processing, but it's perfectly usable for personal use. The ARM-compatible Docker images work out of the box. Just be patient with longer processing times for large recordings.

### How do I backup my Speakr data?

Your Speakr data consists of three essential components: the SQLite database in the `instance/` directory, audio files and transcriptions in the `uploads/` directory, and your configuration in the `.env` file. To create a complete backup, stop the container first to ensure database consistency, then backup all three directories:

```bash
docker compose down
tar czf speakr_backup_$(date +%Y%m%d).tar.gz uploads/ instance/ .env
docker compose up -d
```

Regular automated backups are highly recommended for production use.

## Transcription and AI Features

### How accurate is the transcription?

Transcription accuracy depends on several factors - audio quality, speaker clarity, background noise, and technical vocabulary. See the [troubleshooting guide](troubleshooting.md#poor-transcription-quality) for tips. Configure [custom prompts](admin-guide/prompts.md) for technical vocabulary. With good audio, expect 90-95% accuracy for clear English speech. Accuracy decreases with heavy accents, multiple overlapping speakers, or poor recording quality. The ASR endpoint with speaker diarization often provides better practical usability even if raw accuracy is similar.

### What's the difference between Whisper API and ASR endpoints?

Whisper API provides basic transcription - converting speech to text without speaker identification. The [recommended ASR container](getting-started.md#option-b-custom-asr-endpoint-configuration) (`onerahmet/openai-whisper-asr-webservice`) offers advanced features like [speaker diarization](features.md#speaker-diarization), which identifies and labels different speakers in the conversation. Learn to [manage speakers](user-guide/transcripts.md#speaker-identification) after transcription. Diarization is essential for meetings with multiple participants, while Whisper API works fine for single-speaker recordings like dictations or podcasts.

### Can Speakr transcribe languages other than English?

Yes, Speakr supports multiple languages through its transcription services. Whisper models handle dozens of languages with varying accuracy - major languages like Spanish, French, German, and Chinese work well, while less common languages may have reduced accuracy. Set your preferred language in [account settings](user-guide/settings.md#language-preferences) or leave it blank for automatic detection. See [language support details](features.md#language-support).

### How long can my recordings be?

There's no hard limit on recording length, but practical considerations apply. Very long recordings (over 2-3 hours) take longer to process, use more API credits, and can make the interface sluggish. The file upload limit defaults to 300MB, which accommodates several hours of compressed audio. For very long content like all-day workshops, consider splitting into logical segments.

### What AI model generates the summaries?

Summary generation uses the language model configured in your [environment file](getting-started.md#step-3-configure-your-transcription-service). Customize summaries with [AI prompts](admin-guide/prompts.md) - through a local LLM endpoint or a Cloud provider like OpenAI or OpenRouter. The model choice affects [summary quality](features.md#automatic-summarization), cost, and processing speed. Monitor performance in [system statistics](admin-guide/statistics.md).

## Privacy and Security

### Is my data really private?

When self-hosted properly, your audio and transcriptions never leave your server. However, the transcription and summarization APIs (OpenAI, OpenRouter) do process your content on their servers. For complete privacy, you'd need to use local models for both transcription and summarization, which requires significant computational resources.

### Can I use Speakr for confidential business meetings?

Yes, with appropriate precautions. Self-hosting keeps data under your control, but consider your API provider's data policies. OpenAI and OpenRouter have different data retention and usage policies. For maximum security, use local transcription and summarization models, though this requires powerful hardware and technical expertise.

### Are share links secure?

[Share links](user-guide/sharing.md) use cryptographically secure random tokens that are impossible to guess. Manage shared recordings from the [sharing dashboard](user-guide/sharing.md#managing-your-shared-recordings). However, anyone with the link can access the shared content without authentication. Treat share links like passwords - only send them through secure channels and revoke access when no longer needed. For sensitive content, consider alternative sharing methods that require authentication.

### Who can see my recordings?

Only you can see your recordings by default. [Admin users](admin-guide/user-management.md) cannot directly view other users' recordings through the interface, though they can monitor [usage patterns](admin-guide/statistics.md), though they have database access that could theoretically allow it. Shared recordings are accessible to anyone with the share link. Other users on the same Speakr instance cannot see your recordings unless you explicitly share them.

## Features and Functionality

### What is Inquire Mode?

[Inquire Mode](user-guide/inquire-mode.md) is Speakr's semantic search feature that lets you find information across all your recordings using natural language questions. The [vector store](admin-guide/vector-store.md) must be configured for this to work. Instead of searching for exact keywords, you can ask questions like "What did we decide about the marketing budget?" and get relevant excerpts from any recording that discussed that topic. It uses AI embeddings to understand meaning and context.

### How do speaker profiles work?

When you [identify speakers](user-guide/transcripts.md#speaker-identification) in a transcription by clicking on generic labels (SPEAKER_01, etc.) and assigning names, Speakr saves these as speaker profiles. Manage them in [account settings](user-guide/settings.md#speakers-management-tab). In future updates, we intend to add functionality to allow recordings can then use these profiles to automatically suggest speaker identities based on voice characteristics. Over time, you build a library of recognized speakers that makes multi-person transcriptions much more useful.

### Can I edit transcriptions after they're generated?

Yes, transcriptions are fully editable. Click the Edit button above any transcription to make corrections. See the [transcripts guide](user-guide/transcripts.md#editing-transcriptions) for editing options. This is particularly useful for fixing misrecognized technical terms, proper names, or correcting speaker assignments. Your edits are preserved - they won't be lost if you regenerate the summary or use the [chat feature](user-guide/transcripts.md). Export edited transcripts using [various formats](user-guide/transcripts.md).

### What export formats are available?

Speakr can export recordings in multiple formats. Copy transcriptions directly to your clipboard for pasting into other applications. Learn about [export options](features.md#export-options) and [sharing](user-guide/sharing.md). Download complete recordings as Word documents (.docx) including transcription, summary, and notes. [Share links](user-guide/sharing.md) provide read-only web access. Configure what's visible in [share settings](user-guide/sharing.md#creating-a-share-link). The chat history can also be exported for documentation purposes.

## Troubleshooting

### Why is transcription taking so long?

Several factors affect transcription speed - file size, API service load, network speed, and model selection. Large files naturally take longer. API services can slow down during peak usage. Slow internet connections create bottlenecks uploading audio. Using larger, more accurate models like Whisper Large takes longer than smaller models.

### My recordings are stuck in "pending" status

This usually means the background processor has stopped or encountered an error. Check the Docker logs for error messages. See the [troubleshooting guide](troubleshooting.md#transcription-never-starts) for details. Monitor processing in [vector store](admin-guide/vector-store.md). Common causes include invalid API keys, exceeded API quotas, or network connectivity issues. Restarting the container often resolves temporary issues. Check your API provider's dashboard for usage limits or billing problems.

### Why can't I share recordings?

[Sharing](user-guide/sharing.md) requires your Speakr instance to be accessible from the internet with HTTPS/SSL encryption. Check [sharing requirements](user-guide/sharing.md#requirements-for-sharing) and [troubleshooting](troubleshooting.md#sharing-links-dont-work). Local installations or non-HTTPS setups cannot generate working share links. The system disables sharing features when these requirements aren't met. To enable sharing, deploy Speakr on a public server with a domain name and SSL certificate.

### The interface is slow with large transcriptions

Browsers struggle displaying very large amounts of text, especially in the bubble view with speaker diarization. For recordings over 2 hours, consider using the simple view instead of bubble view. Clear your browser cache if performance degrades over time. Breaking very long recordings into segments improves both performance and usability.

## Best Practices

### How should I organize my recordings?

Develop a consistent [tagging system](user-guide/settings.md#tag-management-tab) early. Create tags for different projects, meeting types, or clients. Tags can include [custom prompts](admin-guide/prompts.md) for specialized processing. Use descriptive titles that will help you find recordings months later. Add notes immediately after recordings while context is fresh. Regular maintenance - archiving old recordings and cleaning up test files - keeps your library manageable.

### Do I need to inform people they're being recorded?

Legal requirements vary by jurisdiction. Many regions require explicit consent from all parties being recorded. Speakr includes a configurable [recording disclaimer](admin-guide/system-settings.md#recording-disclaimer) feature. See [compliance considerations](troubleshooting.md#recording-disclaimer-for-legal-compliance). Set appropriate legal text that displays before recordings start. Consult local laws to ensure compliance - this is especially important in regions with strict recording laws like the EU, California, or Australia.

### What's the best audio quality for transcription?

Record in quiet environments when possible. Use a good microphone positioned close to speakers. For meetings, place the recording device centrally where all participants are clearly audible. Avoid background music or TV noise. Higher quality audio not only improves transcription accuracy but also reduces processing time and API costs.

### How can I maximize transcription accuracy?

Speak clearly and avoid talking over others. Minimize background noise and echo. For technical content, consider adding a custom vocabulary or glossary to your [prompts](admin-guide/prompts.md). Users can set [personal prompts](user-guide/settings.md#custom-prompts-tab) for their recordings. Use the appropriate [language setting](user-guide/settings.md#language-preferences) rather than relying on auto-detection. Review [language support](features.md#language-support) for best results. For multi-speaker recordings, use the [ASR endpoint](getting-started.md#option-b-custom-asr-endpoint-configuration) with appropriate speaker count settings. [Identify speakers](user-guide/transcripts.md#speaker-identification) after transcription for best results.

For Chinese transcription specifically, use the large-v3 model as smaller models may not output Chinese characters correctly. For other languages, test different models to find the best accuracy for your specific language and accent.

### What's the difference between chunking by size vs duration?

Chunking by file size (e.g., CHUNK_LIMIT=20MB) works well for consistent bitrate audio. Chunking by duration (e.g., CHUNK_LIMIT=1400s) is better when your transcription service has time limits, like Azure's 1500-second maximum. Duration-based chunking ensures no chunk exceeds the time limit regardless of file compression or quality.

---

Return to [Home](index.md) →