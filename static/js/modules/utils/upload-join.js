/**
 * Joining several uploaded files into one recording: ordering and totals
 * for the upload dialog. Pure functions over upload-queue items
 * ({ clientId, status, file, duration }), so they can be tested alone.
 */

export const MAX_JOIN_FILES = 20;

const VIDEO_EXT = /\.(mp4|mov|mkv|avi|webm|m4v|wmv|flv|ts|mts|mpeg|mpg|ogv|vob|asf)$/i;

export function isVideoFileName(name) {
    return VIDEO_EXT.test(name || '');
}

const nameCollator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

/**
 * The order files are joined in by default: by the file's modified time,
 * then by name with numbers compared as numbers ("part2" before "part10").
 * Recorders and phones that split a long meeting number and timestamp the
 * pieces, so this is usually already right.
 */
export function compareForJoin(a, b) {
    const ta = a.file?.lastModified || 0;
    const tb = b.file?.lastModified || 0;
    if (ta !== tb) return ta - tb;
    return nameCollator.compare(a.file?.name || '', b.file?.name || '');
}

/**
 * Reorder the queued (not yet uploaded) items of `queue` into join order,
 * leaving every other item where it is. Returns a new array.
 */
export function sortQueuedForJoin(queue) {
    const queued = queue.filter(item => item.status === 'queued').sort(compareForJoin);
    let next = 0;
    return queue.map(item => (item.status === 'queued' ? queued[next++] : item));
}

/**
 * Move the queued item `clientId` by `delta` places among the queued items
 * (other items keep their positions). Returns a new array, or the same one
 * when the move is not possible.
 */
export function moveQueuedItem(queue, clientId, delta) {
    const slots = [];
    queue.forEach((item, i) => { if (item.status === 'queued') slots.push(i); });
    const from = slots.findIndex(i => queue[i].clientId === clientId);
    const to = from + delta;
    if (from < 0 || to < 0 || to >= slots.length) return queue;
    return placeQueuedItem(queue, clientId, to);
}

/**
 * Put the queued item `clientId` at position `to` among the queued items
 * (a drop from drag and drop). Returns a new array.
 */
export function placeQueuedItem(queue, clientId, to) {
    const slots = [];
    queue.forEach((item, i) => { if (item.status === 'queued') slots.push(i); });
    const queued = slots.map(i => queue[i]);
    const from = queued.findIndex(item => item.clientId === clientId);
    if (from < 0 || to < 0 || to >= queued.length || from === to) return queue;
    const [moved] = queued.splice(from, 1);
    queued.splice(to, 0, moved);
    const result = queue.slice();
    slots.forEach((slot, n) => { result[slot] = queued[n]; });
    return result;
}

/** Total duration in seconds, or null while any file's duration is unknown. */
export function totalJoinDuration(items) {
    if (!items.length || items.some(item => !item.duration)) return null;
    return items.reduce((sum, item) => sum + item.duration, 0);
}

/** An id for one join; the server accepts 8-64 of [A-Za-z0-9_-]. */
export function newJoinGroupId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().replace(/-/g, '');
    return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`;
}
