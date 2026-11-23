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

### One instance for every browser test

CI runs `tests/e2e/test_browser_smoke.py tests/e2e/test_*_ui.py` against one instance, in alphabetical order. Two rules keep the files independent of that order:

- `run_speakr.py` starts the instance with `RATELIMIT_ENABLED=false`. Every request of the suite comes from 127.0.0.1, so the per-IP limits (1000 requests an hour, ten sign-ins a minute) are reached by the suite itself and answer 429 to whichever test comes next. No browser test relies on a real limit (`test_share_window_ui.py` fakes its 429 with `page.route`); the limits are covered by the Python tests. The published image's smoke test (`smoke_env.py`) keeps the limits.
- A file leaves no rows in the admin's processing queue or inbox behind: a finished job is a row of the queue on every page, and the phone layout test measures the page. `clear_finished_jobs()` in `browser_checks.py` removes the completed and failed jobs; the offline recorder and trim tests call it when they end (the offline recorder test also deletes what it uploaded, and the toast test removes its upload and jobs itself), and the phone layout test calls it before it measures.
- A file that changes the account's look puts back what it found: the theme and the dark mode as a freshly loaded page shows them.
- `login()` signs each account in through the form once per run and reuses that session afterwards; `check_common()` measures its Tailwind probe in rem, so it holds in themes with another root font size.

## Group permission browser test

`test_group_permission_ui.py` covers who can create groups (#576) in the browser: the `permission_create_groups` radio buttons in System settings, the "Can create groups" toggle in the edit-user dialog, the "Members can create groups" toggle in the group dialog, the Group Management entry in the account menu, and group creation, duplicate names, members added by exact username, group admins and deletion on `/group-management`. Setup goes through the app's HTTP API as the seeded admin (internal sharing on, four users with generated passwords, one group); every user signs in through the login form, and the settings are restored afterwards. The checks for failed responses, JavaScript errors and template text are in `browser_checks.py`, shared with the smoke test; a step that expects a refusal (a duplicate name, an unknown username) registers it. It runs with the same gate and instance as the smoke test, in the same `browser-smoke` job:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_browser_smoke.py tests/e2e/test_group_permission_ui.py -q --noconftest -rs
```

With `SPEAKR_E2E_SHOTS` set, a screenshot of each main step is saved in that directory.

## Segment editor browser test

`test_asr_editor_ui.py` covers the segment transcript editor in the browser: the transcript pane (speaker chips, compact times, text edited in place), Enter to split and Backspace to merge, speakers from the picker and the number keys, a voice-match suggestion accepted (in the pane and in the lane header of the timeline, with Undo in the message) and saved through the route of the speaker dialog, two labels matched to one person merged into one speaker, a speaker renamed and merged from the name in its lane header (with the voice-match control beside it left alone), a speaker added with the button in the header cell of the overview and saved only when it has a segment, a speaker without segments removed from its lane header (with undo and redo, and no such button on a lane with segments), selection with merge and delete, typed times and the problem count, undo and redo, find and replace as one undo step, the question before unsaved changes are discarded (the app's own dialog), the summary regenerated after a save when its checkbox is on (with the save request intercepted), the local draft, playback keys, and a phone-width layout. The timeline above the pane is driven with the mouse on its canvas: a click on the ruler seeks, a click on a block selects its row and a selected row is brought into view, an edge is dragged (with its touching neighbor, alone with Alt, snapping to the playhead and to other edges, cancelled with Esc), a click on a block that changes nothing, a block is dragged along its lane and to another lane, and empty lane space is dragged to add a segment, each checked as one undo step and against the saved transcript; on the phone layout a segment is added with touch events. The tests read the timeline's state through the handle the component puts on its canvas (`document.querySelector('[data-asr-timeline-canvas]').__tl`). A last test stores a transcript of 20,000 segments (about ten hours) and checks that only the rows in view are in the DOM, that the end of the list can be reached, that an edit there is saved, and that a full redraw of the timeline takes less than a frame (16 ms; it is built for under 4), printing the measured times.

The recordings it opens come from `seed_recordings.py`, which `run_speakr.py` runs when it starts the instance (two finished recordings of the admin, each with a short WAV file, a tone in bursts where the transcript has speech, and a transcript of twelve segments; nothing here runs an ASR service). On an instance without them, such as the published image in `docker-publish.yml`, the tests are skipped. Each test first stores the seeded transcript again through the app's own save route, and the long transcript is replaced by the seeded one afterwards. It runs with the same gate and instance as the smoke test, in the same `browser-smoke` job:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_asr_editor_ui.py -q --noconftest -rs -s
```

The waveform of the timeline is computed by the instance with ffmpeg (`GET /audio/<id>/peaks`), so ffmpeg must be installed where Speakr runs; `test.yml` installs it in the `browser-smoke` job.

## Trim browser test

`test_trim_ui.py` covers trim mode in the timeline of the segment editor (#660): the Trim button, the start handle dragged on the waveform, an end time typed in its field (and a text that is not a time), the counts of removed and shortened segments, Preview, Esc and Cancel, a trim refused while edits are unsaved, the app's own question before the cut, and the cut itself, checked against the stored recording (its length and its transcript moved to start at 0:00) and the player on the page. A second test takes screenshots of trim mode in all six themes and at phone width, where a handle is dragged with touch events. It cuts the third seeded recording (`TRIM_TITLE` in `seed_recordings.py`), which no other test uses, so the other tests keep their 90-second audio. Afterwards it removes the admin's finished jobs (a finished trim is a row in the processing queue, which changes the phone layout), and it signs in once (the login form allows ten requests a minute), so the other files pass when it runs before them. It runs with the same gate, instance and CI job as the segment editor test.

## Recording header browser test

`test_header_fixes_ui.py` covers the pen beside the title, which puts the cursor in the title field with the title selected (#686), and the share dialog with public sharing switched off and on in System Settings: off, it says that no public link can be created and has no button for one (#685). The setting is put back afterwards.

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

## Phone layout browser test

`test_phone_layout_ui.py` covers the recording page and the recording list below 768 px (`templates/components/phone/`, `static/css/phone.css`, `composables/phone-layout.js`). At 390 px it checks the one top bar of a recording (back, title, star, More) and that the page header with the account menu is not drawn, renaming from Actions, the details line with both dates and with one date when the meeting and the upload fall on the same day, the Info sheet, the actions in the sheet behind More for a recording of one's own, one shared without rights, a failed one and an incognito one, the four fixed tabs with Events and Speaker stats as sheets, their two buttons in the row above the summary (in view without scrolling and in the same place with a summary of many screens and without one), the pinned bar of the transcript (it stays under the top bar while 20,000 segments scroll, the page stays small, the switch shows the translation), find from the pinned bar on a long and on a short transcript, the player strip and the player sheet (play, skip, speed, the scrubber, no volume control), the composer on the assistant's tab only, one line high and growing, and above the keyboard when the visible height shrinks, and the list (search that keeps the list open, filter chips, the further filters, the menu, selection mode, New recording opening the upload dialog over the list). At 360 and 430 px it checks that nothing scrolls sideways and that icon buttons are 44 px targets. One pass per UI theme (the theme is saved in the admin's account for the pass and restored) checks the same structure, the pinned bar and the touch targets in all six. A second pass per theme opens a recording without a summary, a transcript, notes and chat messages and checks that the empty Summary, Transcript, Notes and assistant tabs have the mark, the title and the hint at the same height (within 1 px of the Notes tab), centered and in the same sizes. Two desktop tests check that the Transcription / Translation switch stays in view while a long transcript scrolls, and that an incognito recording has no download control.

A recording's state (another meeting date, shared without rights, failed, events, a translation, a long or a missing summary, no transcript, no notes) is given to the page by changing the response of `GET /api/recordings/<id>` in the browser, and incognito mode by changing `GET /api/config` and putting a recording in `sessionStorage`, so the instance's data is not changed apart from the long transcript, which is replaced by the seeded one afterwards. The test signs in once and starts every further browser context from those cookies.

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_phone_layout_ui.py -q --noconftest -rs
```

The markup side of the same layout (every desktop action is also on the phone, the structure is shared by the themes, no download control for an incognito recording) is in `tests/test_phone_layout_markup.py`, which needs no browser.

## Toast and processing queue browser test

`test_toast_queue_ui.py` covers the notifications (`static/js/modules/utils/toast.js`) and the processing queue (`templates/components/queue/`, `static/css/queue.css`, the part "THE QUEUE ON THE PAGE" of `app.modular.js`).

Toasts: each of the four types with its fixed color, badge, role and time (three seconds, eight for a warning, an error until it is closed, read from the countdown line and watched with the real timers), the hold on hover and on keyboard focus with the countdown line stopped, Escape, the repeat count that starts the time again, a stack at the bottom center of the reading area above the player (and at the bottom edge with the player at the top), the type read from the icon of an old call, and in every UI theme, light and dark, that a toast has no page color, a border and a shadow, and a contrast of 4.5:1 or more for its text, glyph, close button, count and action.

Queue: the text of the indicator for mixed states with the failures in red, the docked panel with its four groups in order and the list made shorter, a real number ("3 of 8", a measured percent) or a moving bar with the elapsed time and never an invented number, a job stepped through queued, converting, transcribing in parts, titling, summarizing and finished, the cancel of a waiting job, of a running job ("Canceling…") and of a running first transcription (asked about in the app's own dialog; a dialog of the browser fails the test), retry, delete, Clear failed, Clear finished, a finished row that opens its recording, the empty state, the list that still scrolls in a low window with the panel docked, the pill and the panel under the top bar while the list is collapsed, the one clock of the elapsed times (a second that passes changes only the counters; the timer is stopped while the panel is minimized or the tab is in the background), the strip and the sheet of the phone layout with the toasts above the strip and under the top bar while the sheet is open, one pass per UI theme on the desktop (light and dark) and on a phone, and the card of a canceled first transcription in the recording view.

The answer of `GET /api/recordings/job-queue-status` and the answers of the cancel, retry and clear routes are given to the page by the test, and a recording that the queue names but the instance does not have is answered with a copy of a seeded one, so the instance's data stays as it is. Two tests use the instance itself: an upload whose request is held back in the browser is canceled (`POST /upload/cancel` is the instance's own), and a real upload is followed through the queue of the instance. No transcription service runs on the test instance, so its transcription fails within seconds, or still waits or runs when the test reaches it. Each end is checked: a job that waits or runs is canceled from its row and the card of the canceled transcription is read in the recording view; a failed job is listed under Failed with its reason and deleted from its row, which removes the recording of a new upload with it. What was found is printed (`-s`), and nothing of the upload is left on the instance. The position of the desktop player (an account setting, `top` for a new account) is set per test by changing `data-audio-player-position` in the page as it is loaded.

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_toast_queue_ui.py -q --noconftest -rs -s
```

The rules behind both are unit-tested without a browser in `static/js/modules/utils/toast.test.js`, `queue-view.test.js` and `queue-data.test.js`.

## Keyboard shortcuts browser test

`test_keyboard_shortcuts_ui.py` covers the keyboard shortcuts (`static/js/modules/utils/keyboard-shortcuts.js`, `utils/shortcut-help.js`, `composables/shortcuts.js`; the keys are listed in `docs/user-guide/keyboard-shortcuts.md`). It checks the help (opened with `?` and from the account menu, its groups and rows read from the page and compared with the English strings, shown in the app's own dialog and closed with Esc), one pass per UI theme in light and dark (contrast of 4.5:1 or more for the labels, headings and keys, two columns, the panel inside the window, the frame of the list), `j`, `k`, Enter and `o` in the recording list, the star and the inbox mark set through the app's own routes on the open recording and on the framed row (and set back), `#` asking in the delete dialog without deleting, New recording and search, the tab and view keys (the fourth tab with events given to the page), the summary editor, archive and unarchive, play with `p` and with Space (handled while nothing has the focus, left to the browser on a focused button and in the list), skip and speed read from the audio element, copy read from the clipboard, the segment editor keeping its own keys and its own help, no key firing while text is typed in the search field, the notes editor and the chat field, Enter and Space on a focused button, one Esc closing one thing (the delete dialog, the help and the chat panel each before the queue panel), the Identify speakers dialog closed by Esc while unchanged and left open while a name field is changed, Ctrl+F on a transcript that is not rendered whole, `g` then a letter on the main page, the account page, the admin page and the Ask Ambrose page with each page's own help, the account setting turned off and on with its checkbox under Account, Preferences, and a phone width where no key is handled and the account menu has no help entry.

The queue's answer and a transcript of 400 segments are given to the page by the test, so the instance's data stays as it is apart from the star, the inbox mark, the archive mark and the account setting, which are set back. Its contexts block the service worker, because answers are changed on their way to the browser.

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_keyboard_shortcuts_ui.py -q --noconftest -rs
```

The registry's rules (the guards for typing, modifiers and input methods, sequences, scopes, the setting, the help generated from the table) are unit-tested without a browser in `static/js/modules/utils/keyboard-shortcuts.test.js` and `shortcut-help.test.js`, and the setting on the server in `tests/test_cov_auth.py`.

## App dialog browser test

The app does not use the browser's `confirm()`, `alert()` or `prompt()`: `static/js/app-dialog.js` shows the question in the app's own dialog (`window.confirmDialog`, `alertDialog`, `promptDialog`), in the Vue app and on the pages that are not part of it. `test_app_dialog_ui.py` covers one of those pages: on the account page a saved speaker is deleted from the Speakers tab. Cancel, Esc and a click beside the panel keep the speaker and the focus returns to the button; Tab stays in the dialog; the danger button that names the action deletes. The message and the input dialog are shown once each. The same question over the segment editor (discarding unsaved changes) is in `test_asr_editor_ui.py`. Both fail when a dialog of the browser appears. It runs with the same gate, instance and CI job as the smoke test:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_app_dialog_ui.py -q --noconftest -rs
```

## Service worker browser test

`test_service_worker_ui.py` covers the service worker (`static/sw.js`, served at `/sw.js` with the scope `/`; the page's side is `static/js/service-worker-client.js`). Each test starts a browser profile of its own, without a worker and without caches, from the cookies of one sign-in. It checks that the worker controls the app after the first load, that `navigator.serviceWorker.ready` resolves and that exactly one registration exists, also on `/account` and `/admin`, and none in a browser that is not signed in; that Cache Storage holds files under `/static/` only after browsing the pages, a recording, the API and the incognito endpoints; that a recording page loaded again without a network is the worker's "You are offline" page and not the recording, and that the app is back when the network is; that static files are answered without a network while API requests fail; that sign-out empties Cache Storage, `sessionStorage` and the IndexedDB stores `SpeakrRecordings` and `SpeakrFailedUploads`, with the app's own question first when the browser holds copies that did not reach the server (Cancel keeps them and the session); that a browser with the registration of an earlier release at `/static/` and its cache (`Speakr-cache-<version>`, with a page and an API response in it) ends with one worker at scope `/` and without that cache; and that no notification permission is requested and no push subscription is made. The network is turned off with the browser context's offline mode. The earlier release's worker file is given to the browser by answering `/static/sw.js` in the browser context; the test is skipped where Playwright does not route worker scripts.

A service worker needs a secure context. `http://127.0.0.1` and `http://localhost` count as one, so the test runs against the same instance and in the same `browser-smoke` job as the smoke test:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 \
  pytest tests/e2e/test_service_worker_ui.py -q --noconftest -rs
```

The worker's decision for each kind of request is unit-tested in `static/js/sw.test.js`, the page's side in `static/js/service-worker-client.test.js`, and the `/sw.js` route and the mark the server leaves at sign-out in `tests/test_service_worker.py`.

## Offline recorder browser test

`test_offline_recorder_ui.py` covers recording without a connection and the upload that follows it (#650), in Chromium with a fake microphone (`--use-fake-device-for-media-stream --use-fake-ui-for-media-stream`). A recording made in the app whose upload fails (the browser offline, the upload refused while the browser reports a connection, the answer of a request that reached the server lost) is uploaded once, and two reloads later the server holds it once and no recovery is offered. With the server down after one online visit, the worker answers the offline recorder: it records, lists the waiting recordings, deletes one after its question, returns to the app when the server answers (not during a recording), and the app uploads the recording once. Sign-out with a waiting recording asks first, with the count, and deletes it; it removes the recorder's files and its account entry, and the offline page then records nothing and shows no user data. When another account signs in on the browser, the waiting recording and the crash-recovery copy of the first account are deleted with a notice, nothing reaches either account, and the first account finds nothing when it signs in again. The account entry expires (`REMEMBER_COOKIE_DAYS`): an expired entry gives no Record button, a fresh one records, and each signed-in load renews it. A recording stops by itself, after a warning at 80 %, at its maximum length or size (limits written into the entry by the test), and is uploaded once with its length; too little storage (a stubbed `navigator.storage.estimate()`) refuses a start or stops a recording with a save. Screenshots of these states are taken in light and dark at 1280 and 390 px. The recorder is checked in the six UI themes, light and dark, at desktop and phone width, and in the seven languages; with `SPEAKR_E2E_SHOTS` set, a screenshot of each is saved.

The server is stopped and started through the control port of `run_speakr.py` (`--control-port`, `SPEAKR_CONTROL_URL`): to the browser, a stopped server is a server that is down, with `navigator.onLine` still true. The scenarios that need it are skipped without it; the others use the browser context's offline mode, which also reaches the worker's requests in Chromium.

```bash
python tests/e2e/run_speakr.py --port 8899 --control-port 8898 &
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_CONTROL_URL=http://127.0.0.1:8898 \
  pytest tests/e2e/test_offline_recorder_ui.py -q --noconftest -rs
```

The recorder's decisions are unit-tested in `static/js/modules/utils/offline-recorder-model.test.js`, the ties between a failed upload and its other copies in `static/js/modules/utils/pending-uploads.test.js` and `static/js/modules/composables/upload.test.js`, and the worker's part in `static/js/sw.test.js`.

## Translation payload browser test

`test_i18n_payload_ui.py` covers the translations a page carries (discussion #611): a page in another language than English embeds that language only. On the public share page for a German browser it checks German text, one embedded table and no request for a locale file. With French saved in the browser and two keys taken out of the French file (the file is answered in the browser context), it checks that French is fetched, that English is fetched once from the versioned address for the missing keys, that the two labels end in English, and that the keys themselves are never shown. It also checks the sign-in page of a German browser, and the main page of an English account (English embedded as before; with German saved in the browser, one request for the German file and none for English).

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 \
  pytest tests/e2e/test_i18n_payload_ui.py -q --noconftest -rs
```

## Audit log browser test

`test_audit_log_ui.py` covers the Audit log tab of the admin page: the table with the newest events first, a row opened to its details, the filters (group of events, result, kind of access, recording id, dates), paging with Older and Newer, the two export links with the filters of the page, "View activity" on a row of the user list, the notice that is shown until an administrator dismisses it, and the tab in each of the six UI themes, light and dark, with the contrast of a row measured. The events it reads are made by the test through the app: the sign-in of the admin, one refused sign-in from a second browser context, and 55 setting changes for a second page. The setting of the notice is restored afterwards. It runs with the same gate and instance as the smoke test, in the same `browser-smoke` job:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 SPEAKR_E2E_SHOTS=/tmp/shots \
  pytest tests/e2e/test_audit_log_ui.py -q --noconftest -rs
```

## Language models admin browser test

`test_llm_admin_ui.py` covers three flows of Admin, System settings, Language models that only run in the browser. "Discover models" asks a provider for its model ids, served by `fake_llm_server.py`, and choosing one fills the Model name field. The API key field has an eye button that shows the value being typed and hides it again; a saved key is never sent to the browser. The Extra Body field is checked before a save: invalid JSON (`{bad json`) and a JSON value that is not an object (`[1,2]`) show the validation message and send no POST to `/admin/llm-config/models`; a JSON object passes the check and is saved. At the model limit the panel hides **Add model**, shows the translated limit message, and the API refuses one more model. The discovery step persists nothing, and every model the other steps save is deleted through the admin API at the end of the run. It runs with the same gate and instance as the smoke test:

```bash
SPEAKR_BROWSER_SMOKE=1 SPEAKR_BASE_URL=http://127.0.0.1:8899 \
  pytest tests/e2e/test_llm_admin_ui.py -q --noconftest -rs
```
