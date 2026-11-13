/**
 * Which releases the What's new dialog shows: every release with notes in the
 * locale files after the one the account last saw, up to the running release,
 * newest first. An account that has never seen the dialog gets every release
 * up to the running one. Release keys look like '0_10_21' (version.py release_key).
 */
export const WHATS_NEW_MAX_RELEASES = 5;

const KEY_RE = /^\d+_\d+_\d+$/;
const isKey = (key) => KEY_RE.test(String(key || ''));
const parts = (key) => String(key).split('_').map(Number);

export function compareReleaseKeys(a, b) {
    const x = parts(a);
    const y = parts(b);
    for (let i = 0; i < 3; i++) {
        if (x[i] !== y[i]) return x[i] - y[i];
    }
    return 0;
}

export function releasesToShow(available, current, seen, max = WHATS_NEW_MAX_RELEASES) {
    if (!isKey(current)) return [];
    return [...new Set(available || [])]
        .filter(isKey)
        .filter((key) => compareReleaseKeys(key, current) <= 0)
        .filter((key) => !isKey(seen) || compareReleaseKeys(key, seen) > 0)
        .sort((a, b) => compareReleaseKeys(b, a))
        .slice(0, max);
}

/** '0_10_21' -> '0.10.21' */
export const releaseVersion = (key) => String(key).split('_').join('.');
