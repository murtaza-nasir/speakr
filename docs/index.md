# Welcome to Speakr

Speakr is a powerful self-hosted transcription platform that helps you capture, transcribe, and understand your audio content. Whether you're recording meetings, interviews, lectures, or personal notes, Speakr transforms spoken words into valuable, searchable knowledge.

<div style="max-width: 80%; margin: 2em auto;">
  <img src="assets/images/screenshots/Main view.png" alt="Main Interface" style="border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
</div>

!!! warning "Latest Release: v0.5.9 - Major Update"
    **⚠️ IMPORTANT:** This is a **major release** with significant architectural changes. **Before upgrading:**

    - **BACKUP YOUR DATA** - Database schema changes require migration
    - **Review new environment variables** - Many features require `.env` configuration
    - **Test in development first** - Major refactoring may affect existing workflows

    **Key Environment Variables:** `ENABLE_INTERNAL_SHARING`, `SHOW_USERNAMES_IN_UI`, `USERS_CAN_DELETE`, `ENABLE_AUTO_DELETION`, `GLOBAL_RETENTION_DAYS`, `ENABLE_AUTO_EXPORT`, `ENABLE_PUBLIC_SHARING`

    See the [configuration guide](getting-started/installation.md#configuration-updates) for complete setup instructions. [View full release notes](https://github.com/murtaza-nasir/speakr/releases/tag/v0.5.9)

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
    Tags aren't just for organization - they transform content. Create a "Recipe" tag to convert cooking narration into formatted recipes. Use "Study Notes" tags to turn lecture recordings into organized outlines. Stack tags like "Client Meeting" + "Legal Review" for combined analysis. Learn more in the [Custom Prompts guide](admin-guide/prompts#creative-tag-prompt-use-cases).

## Latest Updates

!!! warning "Version 0.5.9 - Major Release"
    **⚠️ Backup your data before upgrading!** This release includes database migrations and architectural changes.

    **New Features:**
    - **Complete Internal Sharing System** - Share recordings with users with granular permissions (view/edit/reshare), personal notes, and independent status tracking
    - **Group Management & Collaboration** - Create groups with leads, group tags that auto-share recordings, custom retention policies per group
    - **Speaker Voice Profiles** - AI-powered speaker recognition with 256-dimensional embeddings (requires WhisperX ASR)
    - **Audio-Transcript Synchronization** - Click-to-jump, auto-highlight, and follow mode for interactive navigation
    - **Auto-Deletion & Retention System** - Global and group-level retention policies, tag protection, per-recording exemptions
    - **Automated Export** - Auto-export transcriptions to markdown for Obsidian, Logseq, and other note-taking apps
    - **Permission System** - Fine-grained access control with user deletion rights, public sharing permissions, role-based access
    - **Modular Architecture** - Backend refactored into blueprints, frontend composables for shared logic
    - **UI/UX Enhancements** - Compact controls, inline editing, unified toast notifications, improved badges
    - **Enhanced Internationalization** - 29 new tooltip translations across all languages (EN, DE, ES, FR, ZH)

    **Required `.env` variables:** See [configuration guide](getting-started/installation.md#configuration-updates)

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