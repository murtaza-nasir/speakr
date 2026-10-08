# Documentation screenshots

## Synthetic ASR editor regression tests

The backend tests inspect actual PNG pixels and use the test-only dependencies
in `tests/requirements.txt` (`pip install -r requirements.txt -r tests/requirements.txt`).
Pillow is not required by the application runtime.

The ASR fixture generates tones and invented transcripts. It overrides database,
uploads, providers and queues before importing the app; mutable data lives in
`/tmp`. Use a disposable container without database or upload mounts. No login
credentials are needed. Scripts verify the fixture readiness route and refuse
the normal application/preview ports.

From the repository root, build the branch and start the fixture:

```bash
docker build -t speakr-asr-fixture .
docker run --rm --name speakr-asr-fixture \
  -p 127.0.0.1:8911:8899 --tmpfs /tmp:rw,size=2g \
  -e ASR_SPLIT_SMOKE=1 -e ASR_SEGMENT_SMOKE=1 \
  --entrypoint python speakr-asr-fixture \
  scripts/screenshots/asr-editor-fixture.py
```

In another terminal, install the existing screenshot tooling and run from the root:

```bash
npm --prefix scripts/screenshots install
npm --prefix scripts/screenshots exec -- playwright install chromium
export SPEAKR_URL=http://127.0.0.1:8911
node scripts/screenshots/asr-editor-demo.mjs
node scripts/screenshots/asr-editor-demo.mjs --mobile
node scripts/screenshots/asr-editor-demo.mjs --without-segmenter
SPEAKR_REDUCED_MOTION=1 node scripts/screenshots/asr-workplace-smoke.mjs
node scripts/screenshots/asr-spectrum-zoom-smoke.mjs
node scripts/screenshots/asr-spectrum-zoom-edges.mjs
node scripts/screenshots/asr-speaker-picker-smoke.mjs
node scripts/screenshots/unified-workspace-smoke.mjs
node scripts/screenshots/unified-workspace-parity.mjs
```

The demo checks playback, zoom boundaries, cached pan/Fit, marker-based splitting
and saved metadata. The compatibility case removes `Intl.Segmenter` before app
bootstrap and verifies editing and saving. Workplace tests cover draft/save races,
speaker tools, recognition proposals, boundary expansion and mobile layout. Zoom
tests also cover long/fractional segments, tiny intervals, end-of-file clipping,
failed Fit retry, cancellation and late lease release.
Speaker picker tests cover opening a populated name without filtering away other
speakers, mouse/keyboard selection, free names, both split fields, saved speaker
links, metadata preservation, reopen and 320px layout. Use `--baseline` against
the pre-fix checkout to record the failing keyboard scenario.

JSON results, screenshots and finalized WebM videos go under `output/playwright/`.
Review artifacts before publishing. Reduced motion is a browser test setting;
these scenarios do not override deployed styles. For a baseline capture, run the
same fixture helper against an unmodified upstream checkout in a separate
disposable container, then run
`SPEAKR_URL=http://127.0.0.1:8912 node scripts/screenshots/asr-editor-demo.mjs --before`.
The committed examples use synthetic tones, not recorded voices.

The unified workspace scripts exercise the real application: shared drafts across
speaker/editor modes, split/save/reopen, spectrum playback, mobile panes,
autosave/summary acknowledgement, voice suggestions, short-speaker merging,
transient LLM identification, segment recognition through the HTTP fixture,
read-only/incognito flows, untimed legacy JSON and video controls. Provider
suggestions and LLM responses are deterministic test responses; they do not prove
model quality. `unified-workspace-smoke.mjs --baseline` captures the old separate
interfaces. For a full-list performance comparison, run
`unified-workspace-performance.mjs` with `SPEAKR_BASELINE_URL` and `SPEAKR_URL`
pointing to separate disposable fixtures; it compares typing, filtering,
scrolling, segment selection and the editor/speaker roundtrip across 1,200 long
utterances (12 trials, two warmups, median, 20% persistent interaction budget).
First opening is reported separately because the unified session also loads
speaker provenance from `workspace_context`. `SPEAKR_PLAYWRIGHT_MODULE` and
`SPEAKR_CHROMIUM_PATH` may select an installed runtime/browser; by default the
scripts use the screenshot package's Playwright and its bundled Chromium.

Reproducible screenshot harness for the docs (`docs/screenshots.md`, README,
user guide pages). Every shot is defined as code, so after a UI change the
whole gallery can be regenerated identically.

## Standards

- Desktop: 1440x1080 viewport (exact 4:3), device scale 1.
- Mobile: 390x844 viewport at 3x.
- Dark mode by default, with the color scheme varied across shots
  (`blue`, `emerald`, `purple`, `rose`, `amber`, `teal`) to show the themes.
- Fixed locale (en-US) and timezone (America/Chicago); animations frozen.

## Usage

```bash
cd scripts/screenshots
# Node 20+ required (nvm use 22)
npm install && npx playwright install chromium   # once
export SPEAKR_URL=https://your-dev-instance      # defaults to spdev
export SPEAKR_EMAIL=... SPEAKR_PASSWORD=...
# or: export SPEAKR_SESSION_COOKIE='session=<signed session value>'
node capture.mjs --list                          # see all defined shots
node capture.mjs --file=shots/core.mjs           # one area
node capture.mjs --only=main-view,upload-modal   # specific shots
node capture.mjs --out=docs                      # write into docs/ (publish)
```

Without `--out=docs`, images land in `./out/` for review.

## Adding shots

Add an entry to a file in `shots/` (or a new file there):

```js
export default [
  {
    name: 'main-view',                    // output filename (no extension)
    description: 'The main view: recordings list, transcript, and summary',
    theme: { dark: true, scheme: 'blue' },
    // mobile: true,                      // phone viewport instead
    run: async (page) => {                // drive the page into the state
      await go(page, '/');
      await openRecordingByTitle(page, 'Some Recording');
    },
  },
];
```

Helpers in `helpers.mjs`: `go(page, path)`, `openRecordingByTitle(page, title)`,
`clickVisible(page, selector)`, `settle(page)`. Text locators match hidden
template duplicates in this app — always use the visibility-filtering helpers.

Content on screen comes from the dev instance's data; pick recordings whose
titles/content look presentable (see existing captions in docs/screenshots.md
for the intent of each image).

## Spectrogram frequency preference and axis regression

Run the synthetic-only `asr-editor-fixture.py` in a disposable container with
`ASR_SPLIT_SMOKE=1`, mapping its port8899 to localhost8921. It uses a temporary
database and synthetic WAV files; do not mount a production database or upload directory.

```powershell
$env:SPEAKR_URL = 'http://127.0.0.1:8921'
node scripts/screenshots/asr-spectrum-frequency-ui.mjs
```

Set `SPEAKR_PLAYWRIGHT_MODULE` and `SPEAKR_CHROMIUM_PATH` if using a bundled
Playwright/runtime. `--before` is a baseline reproduction against the version
before this fix: it expects the4kHz choice to reset on modal reopen and reload.
The current run checks modal/segment/recording transitions, page reload, native
keyboard selection, actual low-rate mono caps, stereo full-range22.05kHz on a
320px screen, light/dark layouts, zoom/Fit/pan/marker and play/pause. All transcript
write requests are blocked and asserted absent. Finalized videos, PNGs and JSON
results stay local in `output/playwright/spectrum-frequency-ui/`.

## Spectrogram brightness and quiet audio

Use the same disposable synthetic fixture server, with its listening port mapped
to localhost:8921. Never mount a production database or uploads. The fixture accepts
`audio_amplitude` between 0.001 and 1; the brightness run uses 0.002 for quiet tones
and digital silence, plus 1 for a strong-signal control. The input WAV is unchanged.

```powershell
$env:SPEAKR_URL = 'http://127.0.0.1:8921'
node scripts/screenshots/asr-spectrum-brightness-ui.mjs
```

Optional `SPEAKR_PLAYWRIGHT_MODULE`/`SPEAKR_CHROMIUM_PATH` select an installed
runtime. `--before` captures the same quiet fixture against the baseline without
the control. The current run checks a real pointer drag without preparing before
release, keyboard adjustment, reset, failed-preparation applied-level labels and
retry, segment/recording/modal transitions and reload persistence, zoom/Fit/pan,
marker, play/pause and confirmed draft splitting on 1440/390/320px in dark/light themes. Transcript writes are
blocked and asserted absent. Videos, PNGs and JSON stay local in
`output/playwright/spectrum-brightness-ui/`; inspect the 0/+20 dB PNGs and finalize
the videos. Real pixel/silence/source-fingerprint assertions live in
`tests/test_segment_spectrogram.py`, cache-level identity/reuse in
`tests/test_spectrogram_cache.py`.
