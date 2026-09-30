# Tests with the documented settings

Issue #409 reached users because every test ran with a fully configured
environment. These tests start from what the docs tell a new user to do.

| Layer | Where | What it checks | When it runs |
|---|---|---|---|
| 1 | `tests/test_documented_defaults.py` | Speakr configured only from `config/env.whisperx.example`, no admin settings, against a fake ASR service that answers with whisperx-asr-service's model rules. Every path that sends audio (voice embedding check, upload, reprocess, bulk reprocess, API v1, incognito, upload join), across service versions and Speakr settings. | Every run of the normal suite |
| 2 | `tests/contract/` | The vendored model rules (`whisperx_model_rules.py`) agree with the service's own `resolve_model_name`. | CI job `e2e-whisperx-defaults` (needs `WHISPERX_REPO`) |
| 3 | `tests/e2e/test_whisperx_defaults_e2e.py` | Both services as real processes with their documented settings: Speakr under gunicorn, whisperx-asr-service on stand-in ML packages. Startup check, upload with speakers and embeddings, upload join, clean logs. | CI job `e2e-whisperx-defaults` (needs `SPEAKR_E2E=1`) |

No layer needs a GPU, secrets or model downloads.

## Running layers 2 and 3 locally

```bash
git clone https://github.com/murtaza-nasir/whisperx-asr-service.git /tmp/wx
python -m venv /tmp/wxv && /tmp/wxv/bin/pip install -r /tmp/wx/tests/requirements-ci.txt
/tmp/wxv/bin/python /tmp/wx/tests/e2e/stub_server.py --port 9000 \
  --env-file /tmp/wx/tests/defaults/minimal.env > /tmp/wx.log 2>&1 &

WHISPERX_REPO=/tmp/wx WHISPERX_PYTHON=/tmp/wxv/bin/python pytest tests/contract -q
SPEAKR_E2E=1 WHISPERX_URL=http://127.0.0.1:9000 WHISPERX_LOG=/tmp/wx.log pytest tests/e2e -q -rxs
```

To test the service setup behind #409 (no `PRELOAD_MODEL` in the service's
`.env`), start the stub server with `--set PRELOAD_MODEL=`.

`KEEP_E2E_LOGS=1` keeps Speakr's log and database after the run.
