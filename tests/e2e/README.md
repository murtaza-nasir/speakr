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

## Browser smoke test

`test_browser_smoke.py` logs in through the login form and opens `/`, `/account`, `/admin`, `/inquire` and a recording (when one exists) in headless Chromium. It fails on any same-origin response of 400 or higher, any JavaScript error, template text left in the page (`${` or `{{`), or a header whose user menu does not open with its entries. The API-level tests never render a page; this is the check for regressions such as a missing vendor file or a template that Vue cannot compile.

It runs in two places:

- `test.yml`, job `browser-smoke`: Speakr from source via `run_speakr.py`, after `PRODUCTION=1 python scripts/download_offline_deps.py`, exactly as the image build downloads the vendor files.
- `docker-publish.yml`, job `smoke-lite`: the lite image, built for amd64 and started with `smoke_env.py`'s settings, before either image is pushed.

By hand (Speakr running at the URL, with Playwright installed):

```bash
python tests/e2e/smoke_env.py > smoke.env                      # for docker run --env-file
python tests/e2e/run_speakr.py --port 8899 &                   # or: from source
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 \
  pytest tests/e2e/test_browser_smoke.py -q --noconftest -rs
```

`--noconftest` keeps the repository's `conftest.py`, which imports the app, out of a run that only needs Playwright.

## Group permission browser test

`test_group_permission_ui.py` covers who can create groups (#576) in the browser: the `permission_create_groups` radio buttons in System settings, the "Can create groups" toggle in the edit-user dialog, the "Members can create groups" toggle in the group dialog, the Group Management entry in the account menu, and group creation, duplicate names, members added by exact username, group admins and deletion on `/group-management`. Setup goes through the app's HTTP API as the seeded admin (internal sharing on, four users with generated passwords, one group); every user signs in through the login form, and the settings are restored afterwards. The checks for failed responses, JavaScript errors and template text are in `browser_checks.py`, shared with the smoke test; a step that expects a refusal (a duplicate name, an unknown username) registers it. It runs with the same gate and instance as the smoke test, in the same `browser-smoke` job:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_browser_smoke.py tests/e2e/test_group_permission_ui.py -q --noconftest -rs
```

With `SPEAKR_E2E_SHOTS` set, a screenshot of each main step is saved in that directory.

## Segment editor browser test

`test_asr_editor_ui.py` covers the segment transcript editor in the browser: the transcript pane (speaker chips, compact times, text edited in place), Enter to split and Backspace to merge, speakers from the picker and the number keys, a voice-match suggestion accepted (in the pane and in the lane header of the timeline, with Undo in the message) and saved through the route of the speaker dialog, two labels matched to one person merged into one speaker, a speaker renamed and merged from the name in its lane header (with the voice-match control beside it left alone), a speaker added under the last lane and saved only when it has a segment, selection with merge and delete, typed times and the problem count, undo and redo, find and replace as one undo step, the question before unsaved changes are discarded (the app's own dialog), the summary regenerated after a save when its checkbox is on (with the save request intercepted), the local draft, playback keys, and a phone-width layout. The timeline above the pane is driven with the mouse on its canvas: a click on the ruler seeks, a click on a block selects its row and a selected row is brought into view, an edge is dragged (with its touching neighbor, alone with Alt, snapping to the playhead and to other edges, cancelled with Esc), a click on a block that changes nothing, a block is dragged along its lane and to another lane, and empty lane space is dragged to add a segment, each checked as one undo step and against the saved transcript; on the phone layout a segment is added with touch events. The tests read the timeline's state through the handle the component puts on its canvas (`document.querySelector('[data-asr-timeline-canvas]').__tl`). A last test stores a transcript of 20,000 segments (about ten hours) and checks that only the rows in view are in the DOM, that the end of the list can be reached, that an edit there is saved, and that a full redraw of the timeline takes less than a frame (16 ms; it is built for under 4), printing the measured times.

The recordings it opens come from `seed_recordings.py`, which `run_speakr.py` runs when it starts the instance (two finished recordings of the admin, each with a short WAV file, a tone in bursts where the transcript has speech, and a transcript of twelve segments; nothing here runs an ASR service). On an instance without them, such as the published image in `docker-publish.yml`, the tests are skipped. Each test first stores the seeded transcript again through the app's own save route, and the long transcript is replaced by the seeded one afterwards. It runs with the same gate and instance as the smoke test, in the same `browser-smoke` job:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_asr_editor_ui.py -q --noconftest -rs -s
```

The waveform of the timeline is computed by the instance with ffmpeg (`GET /audio/<id>/peaks`), so ffmpeg must be installed where Speakr runs; `test.yml` installs it in the `browser-smoke` job.

## Transcript view browser test

`test_transcript_view_ui.py` covers the transcript on the recording page with a long transcript. The page renders only the part of a transcript that is near the viewport (`static/js/modules/composables/transcript-view.js`); the test stores 20,000 segments and checks that the page stays small (the segments in the DOM and the number of elements in the page) at the top, in the middle and at the end, that scrolling by the wheel moves the text by exactly that much, that the find bar (Ctrl+F) goes to a match far down the transcript and marks it, that a click on a far row seeks the player, that the playing segment is followed, that the bubble view shows the place that was being read, that the segment editor opens over a page that is still rendered and edits cheaply (style and layout time, read from the browser), and a phone-width layout. A last test checks that a short transcript is still rendered whole, with the browser's own find left alone. The 20,000 segments are spread over the seeded audio file's 90 seconds, so a segment far down the list has a time the player can seek to.

It uses the recordings of `seed_recordings.py` like the segment editor test, and the same gate, instance and CI job:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_transcript_view_ui.py -q --noconftest -rs -s
```

With the seeded recordings on the instance, the smoke test's recording checks (the detail view, the chat panel layout) run as well instead of being skipped.

## Share page browser test

`test_share_page_ui.py` covers the public share page (`/share/<public_id>`) in a browser that is not signed in. The page shows the transcript the way the recording page does and renders only the part near the viewport (`static/js/share-page.js`, with `composables/transcript-view.js`). The test shares a recording with 20,000 segments and checks the size of the page at the top, in the middle and at the end, the wheel, the find bar (Ctrl+F and its button), a click on a line playing the audio from there, the playing line being followed until the reader scrolls and again after Follow, the bubble view, Copy, the print of the whole transcript, a phone width in light and dark, and the dark mode button. It also checks what the page receives (the fields of `#share-data`, no request beyond the page, its files and the audio, the `noindex` and content security policy headers, no service worker) and that text in shared notes that looks like a template expression is shown as text. A short transcript is rendered whole. The link is made through the app's own route with `X-Forwarded-Proto: https`, as a proxy would report it, since links are made only over https. The server side of the same page is in `tests/test_share_page_payload.py`.

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_share_page_ui.py -q --noconftest -rs -s
```

## Speaker dialog browser test

`test_speaker_dialog_ui.py` covers the Identify speakers dialog with 20,000 segments. Its transcript pane renders only the rows near its viewport (`composables/speaker-modal.js`, the list named `speakers` of `composables/transcript-view.js`). The test checks the time to open and the size of the dialog, one speaker's rows alone and lit among all rows, previous and next turn, a typed name on that speaker's rows with the style and layout time of a keystroke read from the browser, a click on a row playing from there, the pane's find bar (Ctrl+F), a voice-match suggestion (the suggestions request is answered in the browser), a line given to another speaker, the summary checkbox and the save (intercepted so that no summary is asked of the instance), a phone width and a short transcript.

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_speaker_dialog_ui.py -q --noconftest -rs -s
```

## Share window browser test

`test_share_window_ui.py` covers the `user_search_scope` setting where it is used (D#519): the select in Admin, System settings saves each of the three levels, and at each level the share window of a recording finds the right people and nobody else, shows its hints, and shares with a search result and with a full username or email address. The message for the search limit (HTTP 429) is checked with the response replaced in the browser. The person sharing is a user who is not a site admin (an admin may name every account) and who was given the recording with the right to share it on; setup and cleanup go through the app's HTTP API as the seeded admin, as in the group permission test.

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_share_window_ui.py -q --noconftest -rs
```

## App dialog browser test

The app does not use the browser's `confirm()`, `alert()` or `prompt()`: `static/js/app-dialog.js` shows the question in the app's own dialog (`window.confirmDialog`, `alertDialog`, `promptDialog`), in the Vue app and on the pages that are not part of it. `test_app_dialog_ui.py` covers one of those pages: on the account page a saved speaker is deleted from the Speakers tab. Cancel, Esc and a click beside the panel keep the speaker and the focus returns to the button; Tab stays in the dialog; the danger button that names the action deletes. The message and the input dialog are shown once each. The same question over the segment editor (discarding unsaved changes) is in `test_asr_editor_ui.py`. Both fail when a dialog of the browser appears. It runs with the same gate, instance and CI job as the smoke test:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_app_dialog_ui.py -q --noconftest -rs
```
