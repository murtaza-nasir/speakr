# Welcome to Speakr

Speakr is a powerful self-hosted transcription platform that helps you capture, transcribe, and understand your audio content. Whether you're recording meetings, interviews, lectures, or personal notes, Speakr transforms spoken words into valuable, searchable knowledge.

<div style="max-width: 80%; margin: 2em auto;">
  <img src="assets/images/screenshots/Main view.png" alt="Main Interface" style="border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
</div>

!!! info "Latest Release: v0.6.2 - UX Polish & Bug Fixes"
    **Maintenance Release** - Improved user experience and stability

    - **Standardized Modal UX** - All 20+ modals now close on backdrop click with consistent X button
    - **Markdown Support** - Recording disclaimer now supports full markdown formatting
    - **Crash Recovery** - Fixed IndexedDB errors and blank screen issues after browser crashes
    - **Processing Queue Fix** - Deleted recordings properly removed from queue
    - **Performance** - Reduced recording chunk interval to 5 seconds for 80% less overhead

    ✅ Fully backward compatible with v0.6.0. No configuration changes required. [View full release notes](https://github.com/murtaza-nasir/speakr/releases/tag/v0.6.2)

## Quick Navigation

<div class="grid cards">
  <div class="card">
    <div class="card-icon">📚</div>
    <h3>Getting Started</h3>
    <p>New to Speakr? Start here for a quick overview and setup guide.</p>
    <a href="getting-started" class="card-link">Get Started →</a>
  </div>
  
  <div class="card">
    <div class="card-icon">🚀</div>
    <h3>Installation</h3>
    <p>Step-by-step instructions for Docker and manual installation.</p>
    <a href="getting-started/installation" class="card-link">Install Now →</a>
  </div>
  
  <div class="card">
    <div class="card-icon">👤</div>
    <h3>User Guide</h3>
    <p>Learn how to <a href="user-guide/recording">record</a>, <a href="user-guide/transcripts">transcribe</a>, and manage your audio content.</p>
    <a href="user-guide/" class="card-link">Learn More →</a>
  </div>
  
  <div class="card">
    <div class="card-icon">⚙️</div>
    <h3>Admin Guide</h3>
    <p>Configure <a href="admin-guide/user-management">users</a>, <a href="admin-guide/prompts">system settings</a>, and manage your instance.</p>
    <a href="admin-guide/" class="card-link">Configure →</a>
  </div>
  
  <div class="card">
    <div class="card-icon">❓</div>
    <h3>FAQ</h3>
    <p>Find answers to commonly asked questions about Speakr.</p>
    <a href="faq" class="card-link">View FAQ →</a>
  </div>
  
  <div class="card">
    <div class="card-icon">🔧</div>
    <h3>Troubleshooting</h3>
    <p>Solutions for <a href="troubleshooting#transcription-problems">transcription issues</a> and <a href="troubleshooting#performance-issues">performance problems</a>.</p>
    <a href="troubleshooting" class="card-link">Get Help →</a>
  </div>
</div>

## Core Features

<div class="feature-grid">
  <div class="feature-card">
    <h4>🎙️ Smart Recording</h4>
    <ul>
      <li>Audio capture from mic or system</li>
      <li>Take notes while recording</li>
      <li>Generate <a href="features#automatic-summarization">smart summaries</a></li>
    </ul>
  </div>
  
  <div class="feature-card">
    <h4>🤖 AI Transcription</h4>
    <ul>
      <li><a href="features#language-support">Multi-language support</a></li>
      <li><a href="features#speaker-diarization">Speaker identification</a></li>
      <li><a href="features#speaker-management">Voice profiles with AI recognition</a></li>
      <li>Custom vocabularies</li>
    </ul>
  </div>
  
  <div class="feature-card">
    <h4>🔍 Intelligent Search</h4>
    <ul>
      <li><a href="user-guide/inquire-mode">Semantic search</a></li>
      <li>Natural language queries</li>
      <li>Cross-recording search</li>
    </ul>
  </div>
  
  <div class="feature-card">
    <h4>📊 Organization</h4>
    <ul>
      <li><a href="features#tagging-system">Smart tagging system</a></li>
      <li><a href="admin-guide/prompts">Custom AI prompts with stacking</a></li>
      <li><a href="features#speaker-management">Speaker voice profiles with auto-cleanup</a></li>
    </ul>
  </div>
  
  <div class="feature-card">
    <h4>🌍 International</h4>
    <ul>
      <li>5+ languages supported</li>
      <li>Automatic UI translation</li>
      <li>Localized summaries</li>
    </ul>
  </div>
  
  <div class="feature-card">
    <h4>🔒 Privacy First</h4>
    <ul>
      <li><a href="getting-started/installation">Self-hosting ready</a></li>
      <li><a href="troubleshooting#offline-deployment">Offline-ready</a></li>
      <li><a href="user-guide/sharing">Secure sharing</a></li>
    </ul>
  </div>
</div>

## Interactive Audio Synchronization

Experience seamless bidirectional synchronization between your audio and transcript. Click any part of the transcript to jump directly to that moment in the audio, or watch as the system automatically highlights the currently spoken text as the audio plays. Enable auto-scroll follow mode to keep the active segment centered in view, creating an effortless reading experience for even the longest recordings.

<div style="max-width: 90%; margin: 2em auto;">
  <img src="assets/images/screenshots/audio-sync-bubble-view.png" alt="Real-time audio-transcript synchronization" style="border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
  <p style="text-align: center; margin-top: 0.5rem; font-style: italic; color: #666;">Real-time transcript highlighting synchronized with audio playback, with auto-scroll follow mode</p>
</div>

Learn more about [audio synchronization features](user-guide/transcripts.md#audio-synchronization-and-follow-mode) in the user guide.

!!! tip "Transform Your Recordings with Custom Tag Prompts"
    Tags aren't just for organization - they transform content. Create a "Recipe" tag to convert cooking narration into formatted recipes. Use "Study Notes" tags to turn lecture recordings into organized outlines. Stack tags like "Client Meeting" + "Legal Review" for combined analysis. Learn more in the [Custom Prompts guide](admin-guide/prompts.md#creative-tag-prompt-use-cases).

## Latest Updates

!!! info "Version 0.6.2 - UX Polish & Bug Fixes"
    **Maintenance Release** - Improved user experience and stability

    - **Standardized Modal UX** - All 20+ modals close on backdrop click with consistent X button placement
    - **Markdown Support** - Recording disclaimer supports full markdown formatting (headings, lists, links, code blocks)
    - **Crash Recovery Fixed** - Resolved IndexedDB errors and blank screen issues after browser/tab crashes
    - **Processing Queue Fix** - Deleted recordings properly removed from queue (no more ghost entries)
    - **Recording Performance** - Reduced chunk interval to 5 seconds for 80% less IndexedDB overhead
    - **Console Cleanup** - Removed repetitive logging during recording sessions

    ✅ Fully backward compatible with v0.6.0. No configuration changes required.

!!! success "Version 0.6.1 - Offline Ready"
    - **HuggingFace Model Caching** - Embedding model persists across container restarts
    - **Offline Deployment** - Run once with internet, then works fully offline

!!! success "Version 0.6.0 - Queue Control"
    - **Multi-User Job Queue** - Fair round-robin scheduling with automatic retry for failed jobs
    - **Unified Progress Tracking** - Single view merging uploads and backend processing
    - **Media Support** - Added video format support and fixed Firefox system audio recording

!!! warning "Version 0.5.9 - Major Release"
    **⚠️ Major architectural changes** - Backup data before upgrading!

    - **Internal Sharing System** - Share recordings with granular permissions (view/edit/reshare)
    - **Group Management** - Create groups with leads, group tags, custom retention policies
    - **Speaker Voice Profiles** - AI-powered recognition with embeddings (requires WhisperX)
    - **Audio-Transcript Sync** - Click-to-jump, auto-highlight, and follow mode
    - **Auto-Deletion & Retention** - Global and group-level policies with tag protection
    - **Modular Architecture** - Backend refactored into blueprints, frontend composables

    Previous release (v0.5.8):

    - **Inline Transcript Editing** - Edit speaker assignments and text directly in the speaker identification modal
    - **Add Speaker Functionality** - Dynamically add new speakers during transcript review
    - **Enhanced Speaker Modal** - Improved UX with hover-based edit controls and real-time updates

    Previous release (v0.5.7):

    - **GPT-5 Support** - Full support for OpenAI's GPT-5 model family with automatic parameter detection
    - **Custom Summary Prompts on Reprocessing** - Experiment with different prompts when regenerating summaries
    - **PWA Enhancements** - Service worker for wake lock to prevent screen sleep on mobile

    Previous release (v0.5.6):

    - Event extraction for automatically identifying calendar-worthy events
    - Transcript templates for customizable download formats
    - Enhanced export options and improved mobile UI

## Getting Help

Need assistance? We're here to help:

<div class="help-grid">
  <div class="help-card">
    <h4>📖 Documentation</h4>
    <p>You're already here! Browse our comprehensive guides:</p>
    <ul>
      <li><a href="faq">Frequently Asked Questions</a></li>
      <li><a href="troubleshooting">Troubleshooting Guide</a></li>
      <li><a href="user-guide/">User Documentation</a></li>
      <li><a href="admin-guide/">Admin Documentation</a></li>
    </ul>
  </div>
  
  <div class="help-card">
    <h4>💬 Community</h4>
    <p>Connect with other users and get support:</p>
    <ul>
      <li><a href="https://github.com/murtaza-nasir/speakr/issues">Report Issues</a></li>
      <li><a href="https://github.com/murtaza-nasir/speakr/discussions">Join Discussions</a></li>
      <li><a href="https://github.com/murtaza-nasir/speakr">Star on GitHub</a></li>
    </ul>
  </div>
</div>

---

Ready to transform your audio into actionable insights? [Get started now](getting-started.md) →