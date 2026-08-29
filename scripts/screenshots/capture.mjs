#!/usr/bin/env node
// Reproducible documentation screenshots for Speakr.
//
// Usage:
//   SPEAKR_EMAIL=... SPEAKR_PASSWORD=... node capture.mjs [--file=shots/core.mjs] [--only=name1,name2] [--out=docs] [--list]
//
// Every shot renders at a fixed 1440x1080 (exact 4:3) desktop viewport
// (mobile shots: 390x844 @3x), with the theme set via localStorage before
// the app boots, animations frozen, and a fixed locale/timezone — so a
// re-run after a UI change produces directly comparable images.
//
// Output goes to ./out/ by default (review before publishing); pass
// --out=docs to write straight into docs/assets/images/screenshots/.

import { chromium } from 'playwright';
import { readdir } from 'node:fs/promises';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { makeContext, login, freezePage } from './helpers.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DOCS_OUT = path.resolve(HERE, '../../docs/assets/images/screenshots');

const args = Object.fromEntries(process.argv.slice(2).map((a) => {
    const m = a.match(/^--([^=]+)(?:=(.*))?$/);
    return m ? [m[1], m[2] ?? true] : [a, true];
}));

async function loadShots() {
    const dir = path.join(HERE, 'shots');
    const files = args.file
        ? [path.resolve(HERE, String(args.file).replace(/^shots\//, 'shots/'))]
        : (await readdir(dir)).filter((f) => f.endsWith('.mjs')).map((f) => path.join(dir, f));
    const shots = [];
    for (const file of files) {
        const mod = await import(pathToFileURL(file).href);
        for (const shot of mod.default || []) {
            shots.push({ ...shot, sourceFile: path.basename(file) });
        }
    }
    return shots;
}

async function main() {
    let shots = await loadShots();
    if (args.list) {
        for (const s of shots) console.log(`${s.sourceFile}: ${s.name} — ${s.description || ''}`);
        return;
    }
    if (args.only) {
        const wanted = String(args.only).split(',').map((s) => s.trim());
        shots = shots.filter((s) => wanted.includes(s.name));
    }
    if (!shots.length) {
        console.error('No shots selected.');
        process.exit(1);
    }

    const outDir = args.out === 'docs' ? DOCS_OUT : path.join(HERE, 'out');
    mkdirSync(outDir, { recursive: true });

    const browser = await chromium.launch();
    console.log('Logging in...');
    const storageState = await login(browser);

    let failed = 0;
    for (const shot of shots) {
        const label = `${shot.name} [dark=${shot.theme?.dark !== false} scheme=${shot.theme?.scheme || 'blue'}]`;
        process.stdout.write(`Capturing ${label} ... `);
        const context = await makeContext(browser, { ...(shot.theme || {}), mobile: !!shot.mobile, storageState });
        const page = await context.newPage();
        try {
            await shot.run(page);
            await freezePage(page);
            await page.waitForTimeout(300);
            const file = path.join(outDir, `${shot.name}.png`);
            await page.screenshot({ path: file, animations: 'disabled' });
            console.log(`ok -> ${path.relative(process.cwd(), file)}`);
        } catch (e) {
            failed++;
            console.log(`FAILED: ${e.message}`);
        } finally {
            await context.close();
        }
    }
    await browser.close();
    if (failed) {
        console.error(`${failed} shot(s) failed.`);
        process.exit(1);
    }
}

main().catch((e) => { console.error(e); process.exit(1); });
