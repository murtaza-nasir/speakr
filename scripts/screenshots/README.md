# Documentation screenshots

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
