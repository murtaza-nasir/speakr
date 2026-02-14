# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Speakr is a self-hosted AI transcription and note-taking platform. Flask backend, Vue.js 3 frontend (inline in Jinja templates), SQLAlchemy ORM (SQLite default, PostgreSQL supported).

## Running Locally

```bash
# Create virtualenv and install deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# Required env vars for app startup
export UPLOAD_FOLDER=/tmp/speakr/uploads
export SQLALCHEMY_DATABASE_URI=sqlite:////tmp/speakr/instance/speakr.db
export TRANSCRIPTION_API_KEY=<key>
export TRANSCRIPTION_BASE_URL=https://api.openai.com/v1

# Run dev server
.venv/bin/python src/app.py --debug
```

The app exits at startup if transcription is not configured (`TRANSCRIPTION_API_KEY` + `TRANSCRIPTION_BASE_URL`, or `ASR_BASE_URL` for self-hosted WhisperX). LLM features need `TEXT_MODEL_API_KEY` and `TEXT_MODEL_BASE_URL`.

## Running Tests

Tests are standalone scripts (no pytest), run individually:

```bash
# Must set env vars so the app can import without crashing
export UPLOAD_FOLDER=/tmp/speakr_test/uploads
export SQLALCHEMY_DATABASE_URI=sqlite:////tmp/speakr_test/instance/test.db
export TRANSCRIPTION_API_KEY=test-key
export TRANSCRIPTION_BASE_URL=http://localhost:9999

mkdir -p /tmp/speakr_test/uploads /tmp/speakr_test/instance
.venv/bin/python tests/test_api_v1_speakers.py
.venv/bin/python tests/test_api_v1_upload.py
```

Tests use Flask's `app.test_client()`, run inside `with app.app_context():`, create/cleanup their own DB records, and use `unittest.mock.patch` for external services. Each test file has a `main()` that runs all tests and exits 0/1.

## Architecture

**Entry point:** `src/app.py` — creates the Flask app, registers 17+ blueprints, initializes DB, starts job queue workers, sets up file monitor.

**Key layers:**

- `src/api/` — Flask blueprints. `api_v1.py` is the REST API (token auth, OpenAPI spec at `/api/v1/docs`). Other blueprints (`recordings.py`, `speakers.py`, `shares.py`, etc.) serve the web UI.
- `src/models/` — SQLAlchemy models, all re-exported from `src/models/__init__.py`. Always import from `src.models` (e.g., `from src.models import Recording, Speaker`), not from submodules.
- `src/services/` — Business logic. Key services:
  - `job_queue.py` — Fair round-robin job queue with separate transcription/summary worker pools
  - `transcription/` — Connector architecture with auto-detection (`registry.py`). Connectors: `openai_whisper`, `openai_transcribe`, `asr_endpoint`, `azure_openai_transcribe`
  - `llm.py` — LLM calls for summaries, titles, chat, speaker identification
  - `speaker_embedding_matcher.py` — Voice profile matching (256-dim embeddings from WhisperX)
- `src/tasks/processing.py` — Background task implementations (transcription, summarization, title generation, event extraction)
- `src/config/app_config.py` — Reads env vars for transcription/LLM configuration

**Frontend:** Vue.js 3 Composition API inline in Jinja templates (`templates/`). Tailwind CSS. PWA with service worker (`static/sw.js`). 6 languages (i18n). No build step — all frontend code is in template files and `static/js/`.

**Auth:** Session-based for web UI (Flask-Login), token-based for API v1 (Bearer, X-API-Token, API-Token headers, or `?token=` query param). Token auth is in `src/utils/token_auth.py`.

**Permissions:** `has_recording_access(recording, user, require_edit=False)` in `src/app.py` — owner always has access; internal sharing adds view/edit/reshare permissions; group membership grants access to group-tagged recordings.

## Patterns to Follow

- **Lazy imports in endpoints:** Services are imported inside endpoint functions (not at module top) to avoid circular imports. Follow this pattern:
  ```python
  def my_endpoint():
      from src.services.speaker import update_speaker_usage
      ...
  ```
- **Test pattern:** Standalone scripts with `_get_or_create_test_user()`, `_create_api_token()`, manual cleanup in `finally` blocks. No conftest.py or pytest fixtures.
- **Model imports:** Always `from src.models import ModelName`, never from the submodule file directly.
- **Database migrations:** Done in `src/init_db.py` using `add_column_if_not_exists()` — no Alembic.
- **SystemSetting:** Dynamic config stored in DB. Access via `SystemSetting.get_setting(key, default)`.
