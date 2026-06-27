# Welcome to Speakr

Speakr is a powerful self-hosted transcription platform that helps you capture, transcribe, and understand your audio content. Whether you're recording meetings, interviews, lectures, or personal notes, Speakr transforms spoken words into valuable, searchable knowledge.

<div style="max-width: 80%; margin: 2em auto;">
  <img src="assets/images/screenshots/main-view-video.png" alt="Main Interface" style="border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
</div>

!!! success "Latest Release: v0.9.4-alpha — Transcription templates, timestamp toggles, and sharing privacy"
    Transcription templates bundle a reusable initial prompt and hotwords, summarization and chat gain independent per-line timestamp toggles, and recipients of a shared recording now see only the tag or folder that granted them access. Failed uploads retry automatically across all browsers, recordings are reachable by direct `/recordings/<id>` links, and an opt-in prefix-cache prompt layout (with admin-visible cache reads) speeds up self-hosted backends.

    See the [full release notes](https://github.com/murtaza-nasir/speakr/releases/tag/v0.9.4-alpha) for details. Backwards compatible; database migrations run automatically on startup.

## Quick Navigation

<div class="grid cards">
  <div class="card">
    <h3>Getting Started</h3>
    <p>New to Speakr? Start here for a quick overview and setup guide.</p>
    <a href="getting-started" class="card-link">Get Started →</a>
  </div>
  
  <div class="card">
    <h3>Installation</h3>
    <p>Step-by-step instructions for Docker and manual installation.</p>
    <a href="getting-started/installation" class="card-link">Install Now →</a>
  </div>
  
  <div class="card">
    <h3>User Guide</h3>
    <p>Learn how to <a href="user-guide/recording">record</a>, <a href="user-guide/transcripts">transcribe</a>, and manage your audio content.</p>
    <a href="user-guide/" class="card-link">Learn More →</a>
  </div>
  
  <div class="card">
    <h3>Admin Guide</h3>
    <p>Configure <a href="admin-guide/user-management">users</a>, <a href="admin-guide/prompts">system settings</a>, and manage your instance.</p>
    <a href="admin-guide/" class="card-link">Configure →</a>
  </div>
  
  <div class="card">
    <h3>FAQ</h3>
    <p>Find answers to commonly asked questions about Speakr.</p>
    <a href="faq" class="card-link">View FAQ →</a>
  </div>
  
  <div class="card">
    <h3>Troubleshooting</h3>
    <p>Solutions for <a href="troubleshooting#transcription-problems">transcription issues</a> and <a href="troubleshooting#performance-issues">performance problems</a>.</p>
    <a href="troubleshooting" class="card-link">Get Help →</a>
  </div>
</div>

## Core Features

Speakr takes a recording from raw audio to organized, searchable, shareable knowledge. The pipeline:

<div class="feature-grid">
  <div class="feature-card">
    <h4>Capture</h4>
    <ul>
      <li><a href="user-guide/recording">Mic, system/tab audio, or both mixed</a></li>
      <li>Hours-long server-side recording sessions</li>
      <li>Drag-and-drop upload and black-hole auto-import</li>
    </ul>
  </div>

  <div class="feature-card">
    <h4>Transcribe</h4>
    <ul>
      <li><a href="features#multi-engine-support">Bring your own engine: WhisperX, OpenAI, Mistral, custom ASR</a></li>
      <li><a href="features#speaker-diarization">Speaker diarization</a> and <a href="features#speaker-management">voice profiles</a> (WhisperX backend)</li>
      <li><a href="features#language-support">Auto-detect plus 11 common languages</a></li>
      <li>Custom vocabulary and hotwords (most effective with WhisperX)</li>
    </ul>
  </div>

  <div class="feature-card">
    <h4>Understand</h4>
    <ul>
      <li><a href="features#automatic-summarization">Customizable AI summaries</a></li>
      <li>Event extraction and per-recording chat</li>
      <li><a href="user-guide/inquire-mode">Inquire Mode: semantic search across everything</a></li>
    </ul>
  </div>

  <div class="feature-card">
    <h4>Organize</h4>
    <ul>
      <li><a href="features#tagging-system">Smart tags with custom prompts, stackable</a></li>
      <li>Folders and bulk operations</li>
      <li><a href="features#retention-policies-and-auto-deletion">Retention policies and auto-deletion</a></li>
    </ul>
  </div>

  <div class="feature-card">
    <h4>Collaborate</h4>
    <ul>
      <li><a href="user-guide/sharing">Granular internal sharing and public links</a></li>
      <li>Groups with auto-share group tags</li>
      <li><a href="features#single-sign-on-sso">Multi-user with Single Sign-On (OIDC)</a></li>
    </ul>
  </div>

  <div class="feature-card">
    <h4>Automate</h4>
    <ul>
      <li><a href="user-guide/api-reference">REST API v1 with Swagger UI</a></li>
      <li><a href="features#webhooks">Signed webhooks</a> on lifecycle events</li>
      <li>n8n, Zapier, Make integration</li>
    </ul>
  </div>
</div>

## Interactive Audio Synchronization

Experience seamless bidirectional synchronization between your audio and transcript. Click any part of the transcript to jump directly to that moment in the audio, or watch as the system automatically highlights the currently spoken text as the audio plays. Enable auto-scroll follow mode to keep the active segment centered in view, creating an effortless reading experience for even the longest recordings.

<div style="max-width: 90%; margin: 2em auto;">
  <img src="assets/images/screenshots/transcript-auto-follow.png" alt="Real-time audio-transcript synchronization" style="border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
  <p style="text-align: center; margin-top: 0.5rem; font-style: italic; color: #666;">Real-time transcript highlighting synchronized with audio playback, with auto-scroll follow mode</p>
</div>

Learn more about [audio synchronization features](user-guide/transcripts.md#audio-synchronization-and-follow-mode) in the user guide.

!!! tip "Transform Your Recordings with Custom Tag Prompts"
    Tags aren't just for organization - they transform content. Create a "Recipe" tag to convert cooking narration into formatted recipes. Use "Study Notes" tags to turn lecture recordings into organized outlines. Stack tags like "Client Meeting" + "Legal Review" for combined analysis. Learn more in the [Custom Prompts guide](admin-guide/prompts.md#creative-tag-prompt-use-cases).

## Latest Updates

!!! info "Version 0.9.4-alpha - Transcription templates, timestamp toggles, and sharing privacy"
    A feature release. Backwards compatible; database migrations run automatically on startup.

    - **Transcription templates** - Save an initial prompt and hotwords together as a reusable template, then apply it from the upload modal, a tag, a folder, or your account default. The detail view shows which hints a recording actually used, and reprocessing pre-fills them.
    - **Per-feature timestamp availability** - Independent toggles make per-line timestamps available to the summarizer and to chat, each with a default or custom template format, so the AI can reference specific moments in long recordings.
    - **Sharing privacy (#314)** - Recipients of a shared recording see only the tag or folder that granted access, never the owner's other labels, and can no longer be locked into a folder filter they do not own.
    - **Upload reliability and deep links** - Failed uploads retry automatically on reconnect across all browsers (#313), and any recording is reachable at a direct `/recordings/<id>` link (#301).
    - **Prefix-cache prompts and cache visibility** - An opt-in option reshapes the title and summary prompts to reuse the transcript prefix on self-hosted prefix-caching backends, and the admin dashboard now reports prompt-cache reads. Off by default for now; it may become the default in a future release.

    Recommended for all deployments, especially any that accept uploads from untrusted users. See the [full release notes](https://github.com/murtaza-nasir/speakr/releases/tag/v0.9.3-alpha).

!!! info "Version 0.9.2-alpha - Local / S3 storage backend"
    Recording audio can now be stored in S3-compatible object storage instead of, or alongside, the local filesystem. Backwards compatible; `FILE_STORAGE_BACKEND` defaults to `local`, so existing deployments are unaffected.

    - **Pluggable backend** - Set `FILE_STORAGE_BACKEND=local` (default) or `s3`. The S3 path works with AWS S3, MinIO, Backblaze B2, Cloudflare R2, and Wasabi.
    - **Presigned delivery** - In S3 mode, audio is served to the browser via short-lived presigned URLs straight from the object store rather than streamed through the app.
    - **Migration tooling** - `scripts/migrate_local_recordings_to_s3.py` moves existing recordings into a bucket with a dry-run mode, size verification, and optional source deletion.
    - **Configuration** - See the [File Storage](admin-guide/storage.md) admin guide for the full settings reference and per-provider examples, and the [Migration Guide](admin-guide/migration-guide.md#migrating-audio-files-to-s3) for moving historical files.

    Contributed by @Daabramov (#268). See the [full release notes](https://github.com/murtaza-nasir/speakr/releases/tag/v0.9.2-alpha) for details.

!!! info "Version 0.9.1-alpha - Upload-path fixes"
    A patch release hardening the v0.9.0 upload path. Backwards compatible with v0.8.x and v0.9.0; database migrations run automatically.

    - **CSRF token expiry on upload (#310)** - The upload path uses `XMLHttpRequest`, which bypassed the fetch-based CSRF refresh, so uploads failed with HTTP 400 once the page token crossed the one-hour limit. It now refreshes the token before sending and retries once on a CSRF rejection.
    - **Inquire embeddings with auto-summarization (#305)** - Semantic-search chunks were only built in the non-summary path, so with auto-summarization enabled new recordings were never embedded. Summary completion now runs the same chunking step. Pre-existing recordings need a one-time "Re-embed all".
    - **API token modals (#308)** - An unclosed `<div>` nested the Create Token modal inside the hidden folder modal; the markup is fixed so it opens again.
    - **Stalled-upload timeout & leave-page warning** - A size-scaled `XMLHttpRequest` timeout routes a stalled upload into the recovery path instead of hanging, and the browser now warns before you leave the page with an upload still in flight.

    See the [full release notes](https://github.com/murtaza-nasir/speakr/releases/tag/v0.9.1-alpha) for the complete list.

!!! info "Version 0.9.0-alpha - Multi-platform recording, Stats tab, mobile rebuild, design-system unification"
    The first non-patch release in the v0.8 line. Three big user-facing themes: capturing audio is now multi-platform, the mobile app is a first-class member of the design system, and the upload modal stops feeling like a desktop card pasted onto a phone. Backwards compatible with v0.8.x; database migrations run automatically.

    - **System Audio & Multi-Input Recording** - Platform detection with a per-OS help guide (macOS BlackHole + Multi-Output Device, Windows "Share system audio", Linux pavucontrol + `pactl module-virtual-source`). New Input devices picker mixes a primary mic plus an optional secondary device via Web Audio into one track, with a toggle to disable Chrome's echo cancellation / noise suppression / auto-gain and virtual-audio-device discovery.
    - **Stats Tab** - New per-recording tab: total length, speaker count, turns, and words as headline cards; per-speaker time / % / turns / words / WPM breakdown; silence row. Available on desktop and mobile.
    - **Upload Modal Redesign** - Real modal overlay (not a full-screen takeover), progressive disclosure of Options behind a chip summary, inline file preview with duration probe, sticky-footer Upload action, last-used tag / folder / language auto-restore, and a mobile bottom-sheet with drag-to-dismiss.
    - **Mobile UI Rebuild** - 56 px bottom navigation, contextual icons in the chevron row, edge-to-edge content, sticky speaker pills, sticky editor Cancel / Save footer, and audio-player polish.
    - **PWA Web Share Target** - Pick Speakr from your phone's native share sheet to send a recording straight in.
    - **Webhooks** - HMAC-SHA256-signed outbound notifications on recording lifecycle events, with SSRF guard and exponential-backoff retries, managed per-user from Account settings → Webhooks.
    - **Server-side recording sessions** - Long recordings stream chunks to the server during capture; the size cap is replaced by a configurable hours-based ceiling with resume-on-reload.
    - **Design-system unification** - 22 modals on shared `.modal-*` primitives, `.btn` + `.field` everywhere, dark-mode `<select>` theming, header consolidation, sidebar redesign, floating dockable chat panel.
    - **Inquire mode** - "+ New Recording" opens the upload modal directly via `?upload=1`. Also: `GET /api/v1/users/me`, an audio-player position preference, and a localization refresh across all seven languages.

    See the [full release notes](https://github.com/murtaza-nasir/speakr/releases/tag/v0.9.0-alpha) for the complete list.

!!! info "Version 0.8.21-alpha - Security: CSRF bypass and SSO account takeover"
    Security patch release on top of v0.8.20-alpha. Tracked as a GitHub Security Advisory; reported by **@Irench1k**.

    - Fixed a CSRF bypass where the `csrf_exempt_for_api_tokens` before_request hook permanently disabled CSRF protection on the targeted view as soon as any request carried a `?token=` query parameter (CWE-287). The hook is gone; CSRF skipping is now a per-request decision driven by `load_user_from_token_headers_only()`.
    - `change_password` no longer silently sets a password on an SSO-only account, closing the chained account-takeover path.

!!! note "Earlier releases"
    The full version history (the rest of the v0.8.x line and the v0.5 to v0.7 releases) is on the [GitHub Releases page](https://github.com/murtaza-nasir/speakr/releases).

## Getting Help

Need assistance? We're here to help:

<div class="help-grid">
  <div class="help-card">
    <h4>Documentation</h4>
    <p>You're already here! Browse our comprehensive guides:</p>
    <ul>
      <li><a href="faq">Frequently Asked Questions</a></li>
      <li><a href="troubleshooting">Troubleshooting Guide</a></li>
      <li><a href="user-guide/">User Documentation</a></li>
      <li><a href="admin-guide/">Admin Documentation</a></li>
    </ul>
  </div>
  
  <div class="help-card">
    <h4>Community</h4>
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