# Features

Speakr combines powerful transcription capabilities with intelligent AI features to transform your audio recordings into valuable, actionable knowledge. Every feature is designed to save time and extract maximum value from your spoken content.

## Core Transcription Features

### Multi-Engine Support

Speakr supports multiple transcription engines to match your needs and budget. Use [OpenAI's Whisper API](getting-started.md#option-a-openai-whisper-configuration) for quick, cloud-based transcription with excellent accuracy. Deploy the [recommended ASR container](getting-started.md#option-b-custom-asr-endpoint-configuration) for advanced features like speaker diarization and local processing. See the [installation guide](getting-started/installation.md) for detailed setup instructions. The system automatically handles different audio formats, converting them as needed for optimal transcription quality.

### Speaker Diarization

When using the [ASR endpoint](getting-started.md#option-b-custom-asr-endpoint-configuration), Speakr automatically identifies different speakers in your recordings. If you encounter issues, check the [troubleshooting guide](troubleshooting.md#speaker-identification-not-working). Each speaker gets a unique label that you can later customize with actual names. The system remembers these speaker profiles, building a library that improves identification accuracy over time. Manage your speaker library in [account settings](user-guide/settings.md). This feature transforms multi-person meetings from walls of text into organized conversations.

### Language Support

Transcribe content in dozens of languages with automatic detection or manual selection. Major languages like English, Spanish, French, German, and Chinese receive excellent support with high accuracy. The system handles multilingual content gracefully, switching between languages as needed within the same recording.

## AI-Powered Intelligence

### Automatic Summarization

Every recording receives an AI-generated summary that captures key points, decisions, and action items. Configure this through [custom prompts](admin-guide/prompts.md). Users can also set [personal prompts](user-guide/settings.md#custom-prompts-tab) for their recordings. Summaries adapt to your content type - technical meetings get detailed technical summaries, while casual conversations receive lighter overviews. Custom prompts let you shape summaries to match your specific needs.

### Event Extraction

Speakr can automatically extract calendar-worthy events from your recordings during the summary process. When enabled in your account settings, the system identifies mentions of meetings, deadlines, appointments, and other time-sensitive items. Each detected event can be exported as an ICS file compatible with any calendar application. The feature intelligently parses relative date references and provides sensible defaults when specific times aren't mentioned. Learn more about [using event extraction](user-guide/transcripts.md#event-extraction) in your workflow.

### Interactive Chat

Transform static transcripts into dynamic conversations with the integrated chat feature. Learn how to use AI chat effectively in the [transcripts guide](user-guide/transcripts.md). Ask questions about your recordings and receive intelligent answers based on the actual content. Request custom summaries, extract specific information, or generate derivative content like emails or reports. The AI maintains context throughout the conversation, allowing complex multi-turn interactions.

### Semantic Search (Inquire Mode)

Search across all your recordings using natural language questions instead of keywords through [Inquire Mode](user-guide/inquire-mode.md). The [vector store](admin-guide/vector-store.md) must be configured for this feature to work. The semantic search understands meaning and context, finding relevant content even when exact words don't match. Ask questions like "When did we discuss the budget increase?" and get results from any recording that covered that topic, regardless of the specific terminology used.

## Organization and Management

### Tagging System

Organize recordings with a flexible [tagging system](user-guide/settings.md#tag-management-tab) that goes beyond simple labels. Tags can include [custom AI prompts](admin-guide/prompts.md) for specialized processing. Each tag can carry [custom AI prompts](admin-guide/prompts.md) and transcription settings, enabling automatic specialized processing based on content type, automatically applying specialized processing to tagged recordings. Tags use colors for visual organization and stack intelligently when multiple tags apply to the same recording.

### Speaker Management

Build and maintain a library of speaker profiles that persist across recordings. [Identify speakers](user-guide/transcripts.md#speaker-identification) after each transcription to build your library. Once identified, speakers are remembered and suggested in future recordings. The system tracks usage statistics, showing how often each speaker appears and when they were last identified. Bulk management tools help maintain a clean, organized speaker library.

### Custom Prompts

Shape AI behavior with [custom prompts](admin-guide/prompts.md) at multiple levels. Understand the [prompt hierarchy](admin-guide/prompts.md#understanding-prompt-hierarchy) for effective configuration. Personal prompts apply to all your recordings, while tag-specific prompts activate for particular content types. The hierarchical system ensures the right prompt applies to each recording, with intelligent stacking when multiple prompts are relevant.

## Sharing and Collaboration

### Secure Share Links

Generate cryptographically secure links to [share recordings](user-guide/sharing.md) with people outside your Speakr instance. Note the [requirements for sharing](user-guide/sharing.md#requirements-for-sharing). Control exactly what recipients see - include or exclude summaries and notes based on your needs. Share links work on any device without requiring accounts or authentication.

### Export Options

Export recordings in multiple formats for different purposes. Generate Word documents with complete transcripts, summaries, and notes for formal documentation. Copy formatted text to clipboards for quick sharing. Export individual components like summaries or notes for focused distribution. Download transcriptions with [customizable templates](user-guide/transcript-templates.md) that format your content for different use cases, from subtitles to interview formats.

### Real-Time Monitoring

Track all your shared recordings from a central dashboard. See what's been shared, when, and with what permissions. Modify share settings without generating new links, or instantly revoke access when shares are no longer appropriate.

## Advanced Capabilities

### Audio Chunking

Handle large audio files that exceed API limits through intelligent chunking. See the [troubleshooting guide](troubleshooting.md#files-over-25mb-fail-with-openai) for configuration details. Learn about [chunking strategies](faq.md#whats-the-difference-between-chunking-by-size-vs-duration) in the FAQ. The system automatically splits long recordings into manageable segments, processes them separately, then seamlessly reassembles the results. Configure chunk size by duration or file size to match your API provider's requirements.

### Black Hole Processing

Set up a watched directory where dropped audio files are automatically processed. Configure this in [system settings](admin-guide/system-settings.md) for automated workflows. Perfect for automation workflows or batch processing, the black hole directory monitors for new files and queues them for transcription without manual intervention.

### Custom ASR Configuration

Fine-tune transcription for specific scenarios with custom ASR settings. See [ASR configuration](troubleshooting.md#asr-endpoint-returns-405-or-404-errors) for common setup issues. Set expected speaker counts for different meeting types. Configure specialized models for technical vocabulary or specific accents. Apply these settings automatically through the tagging system.

## User Experience

### Progressive Web App

Install Speakr as a progressive web app for a native-like experience on any device. The PWA works offline for viewing existing recordings and syncs when connectivity returns. Mobile-optimized interfaces ensure smooth operation on phones and tablets.

### Dark Mode

Reduce eye strain with a full dark mode implementation that affects every interface element. The theme preference persists across sessions and devices, automatically applying your choice whenever you log in.

### Responsive Design

Access Speakr from any device with interfaces that adapt to screen size. Desktop users get a full three-panel layout with simultaneous access to recordings, transcripts, and chat. Mobile users receive a streamlined interface optimized for touch interaction and smaller screens.

## Administrative Control

### Multi-User Support

Run a single Speakr instance for your entire team with isolated user spaces. See [user management](admin-guide/user-management.md) for details. The [FAQ](faq.md#can-multiple-people-use-the-same-speakr-instance) explains the multi-user architecture. Each user maintains their own recordings, settings, and speaker libraries. Administrators manage users, monitor usage, and configure system-wide settings without accessing individual recordings.

### System Monitoring

Track system health with comprehensive statistics and metrics. Monitor transcription queues, storage usage, and processing performance. Identify bottlenecks and optimize configuration based on actual usage patterns.

### Flexible Configuration

Configure every aspect of Speakr through [environment variables](getting-started.md#step-3-configure-your-transcription-service) and [admin settings](admin-guide/index.md). Check [system settings](admin-guide/system-settings.md) for global configuration options. Set API endpoints, adjust processing limits, enable or disable features, and customize the user experience. The system adapts to your infrastructure and requirements.

---

Return to [Home](index.md) →