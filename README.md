<div align="center">
    <img src="static/img/icon-32x32.png" alt="Speakr Logo" width="32"/>
</div>

<h1 align="center">Speakr</h1>
<p align="center">Self-hosted AI transcription and intelligent note-taking platform</p>

<p align="center">
  <a href="https://www.gnu.org/licenses/agpl-3.0"><img alt="AGPL v3" src="https://img.shields.io/badge/License-AGPL_v3-blue.svg"></a>
  <a href="https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml"><img alt="Docker Build" src="https://github.com/murtaza-nasir/speakr/actions/workflows/docker-publish.yml/badge.svg"></a>
  <a href="https://hub.docker.com/r/learnedmachine/speakr"><img alt="Docker Pulls" src="https://img.shields.io/docker/pulls/learnedmachine/speakr"></a>
</p>

<p align="center">
  <a href="https://murtaza-nasir.github.io/speakr">Documentation</a> •
  <a href="https://murtaza-nasir.github.io/speakr/getting-started">Quick Start</a> •
  <a href="https://murtaza-nasir.github.io/speakr/screenshots">Screenshots</a> •
  <a href="https://hub.docker.com/r/learnedmachine/speakr">Docker Hub</a> •
  <a href="https://github.com/murtaza-nasir/speakr/releases">Releases</a>
</p>

---

## Overview

Speakr transforms your audio recordings into organized, searchable, and intelligent notes. Built for privacy-conscious teams and individuals, it runs entirely on your own infrastructure, ensuring your sensitive conversations remain completely private.

<div align="center">
    <img src="docs/assets/images/screenshots/Filters.png" alt="Speakr Main Interface" width="750"/>
</div>

## Key Features

- **Smart Recording & Upload** - Record directly in browser or upload existing audio files
- **AI Transcription** - High-accuracy transcription with speaker identification
- **Interactive Chat** - Ask questions about your recordings and get AI-powered answers
- **Inquire Mode** - Semantic search across all recordings using natural language
- **Internationalization** - Full support for English, Spanish, French, German, and Chinese
- **Smart Tagging** - Organize with tags that include custom AI prompts
- **Secure Sharing** - Generate secure links to share recordings
- **Beautiful Themes** - Light and dark modes with customizable color schemes

## Quick Start

### Using Docker (Recommended)

```bash
# Create project directory
mkdir speakr && cd speakr

# Download configuration (choose one):
wget https://raw.githubusercontent.com/murtaza-nasir/speakr/master/config/docker-compose.example.yml -O docker-compose.yml

# For OpenAI or similar Whisper API:
wget https://raw.githubusercontent.com/murtaza-nasir/speakr/master/config/env.whisper.example -O .env

# OR for ASR with speaker diarization (requires additional container):
wget https://raw.githubusercontent.com/murtaza-nasir/speakr/master/config/env.asr.example -O .env

# Configure your service endpoints and API keys
nano .env  # Set API endpoints (Local/OpenAI/OpenRouter/etc) and add your API keys

# Launch Speakr
docker compose up -d

# Access at http://localhost:8899
```

**Note:** ASR option requires running `onerahmet/openai-whisper-asr-webservice` container alongside Speakr. See [installation guide](https://murtaza-nasir.github.io/speakr/getting-started/installation#running-asr-service-for-speaker-diarization) for complete setup.

**[View Full Installation Guide →](https://murtaza-nasir.github.io/speakr/getting-started)**

## Documentation

Complete documentation is available at **[murtaza-nasir.github.io/speakr](https://murtaza-nasir.github.io/speakr)**

- [Getting Started](https://murtaza-nasir.github.io/speakr/getting-started) - Quick setup guide
- [User Guide](https://murtaza-nasir.github.io/speakr/user-guide/) - Learn all features
- [Admin Guide](https://murtaza-nasir.github.io/speakr/admin-guide/) - Administration and configuration
- [Troubleshooting](https://murtaza-nasir.github.io/speakr/troubleshooting) - Common issues and solutions
- [FAQ](https://murtaza-nasir.github.io/speakr/faq) - Frequently asked questions

## Latest Release (v0.5.8)

### Highlights
- **Inline Transcript Editing** - Edit speaker assignments and text directly within the speaker identification modal
- **Add Speaker Functionality** - Dynamically add new speakers during transcript review
- **Enhanced Speaker Modal** - Improved UX with hover-based edit controls and real-time updates

### Previous Release (v0.5.7)
- GPT-5 Model Support with automatic detection
- Custom Prompt Selection for summary reprocessing
- PWA Enhancements and mobile recording improvements

## Screenshots

<table align="center" border="0">
  <tr>
    <td align="center">
      <img src="docs/assets/images/screenshots/Filters.png" alt="Main Dashboard" width="400"/>
      <br><em>Advanced Filtering Dashboard</em>
    </td>
    <td align="center">
      <img src="docs/assets/images/screenshots/inquire mode.png" alt="Inquire Mode" width="400"/>
      <br><em>AI-Powered Semantic Search</em>
    </td>
  </tr>
  <tr>
    <td align="center">
      <img src="docs/assets/images/screenshots/recording view with simple transcription view and chat visible.png" alt="Transcription with Chat" width="400"/>
      <br><em>Interactive Transcription & Chat</em>
    </td>
    <td align="center">
      <img src="docs/assets/images/screenshots/Multilingual.png" alt="Multi-language Support" width="400"/>
      <br><em>Full Internationalization</em>
    </td>
  </tr>
</table>

**[View Full Screenshot Gallery →](https://murtaza-nasir.github.io/speakr/screenshots)**

## Technology Stack

- **Backend**: Python/Flask with SQLAlchemy
- **Frontend**: Vue.js 3 with Tailwind CSS
- **AI/ML**: OpenAI Whisper, OpenRouter, Ollama support
- **Database**: SQLite (default) or PostgreSQL
- **Deployment**: Docker, Docker Compose

## Roadmap

### Near-term
- Bulk operations for recordings (mass delete, export, tagging)
- Enhanced speaker profile management with voice signatures
- Improved mobile experience with PWA enhancements

### Mid-term
- Plugin system for custom integrations
- Team workspaces with shared recordings
- Automated workflow triggers

### Long-term
- End-to-end encryption option
- Enterprise SSO integration

### Reporting Issues

- [Report bugs](https://github.com/murtaza-nasir/speakr/issues)
- [Request features](https://github.com/murtaza-nasir/speakr/discussions)

## License

This project is **dual-licensed**:

1.  **GNU Affero General Public License v3.0 (AGPLv3)**
    [![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

    Speakr is offered under the AGPLv3 as its open-source license. You are free to use, modify, and distribute this software under the terms of the AGPLv3. A key condition of the AGPLv3 is that if you run a modified version on a network server and provide access to it for others, you must also make the source code of your modified version available to those users under the AGPLv3.

    * You **must** create a file named `LICENSE` (or `COPYING`) in the root of your repository and paste the full text of the [GNU AGPLv3 license](https://www.gnu.org/licenses/agpl-3.0.txt) into it.
    * Read the full license text carefully to understand your rights and obligations.

2.  **Commercial License**

    For users or organizations who cannot or do not wish to comply with the terms of the AGPLv3 (for example, if you want to integrate Speakr into a proprietary commercial product or service without being obligated to share your modifications under AGPLv3), a separate commercial license is available.

    Please contact **speakr maintainers** for details on obtaining a commercial license.

**You must choose one of these licenses** under which to use, modify, or distribute this software. If you are using or distributing the software without a commercial license agreement, you must adhere to the terms of the AGPLv3.

## Roadmap

Speakr is in active development. Planned features include a faster way to switch transcription languages on the fly.

## Contributing

We welcome contributions to Speakr! There are many ways to help:

- **Bug Reports & Feature Requests**: [Open an issue](https://github.com/murtaza-nasir/speakr/issues)
- **Discussions**: [Share ideas and ask questions](https://github.com/murtaza-nasir/speakr/discussions)
- **Documentation**: Help improve our docs
- **Translations**: Contribute translations for internationalization

### Code Contributions

All code contributions require signing a [Contributor License Agreement (CLA)](CLA.md). This one-time process ensures we can maintain our dual-license model (AGPLv3 and Commercial).

**See our [Contributing Guide](CONTRIBUTING.md) for complete details on:**
- How the CLA works and why we need it
- Step-by-step contribution process
- Development setup instructions
- Coding standards and best practices

The CLA is automatically enforced via GitHub Actions. When you submit your first PR, our bot will guide you through signing.
