/**
 * The recording view's two tab bars (desktop-transcription-panel.html and
 * desktop-right-panel.html, marked data-tabs-fit) stay on one row, so the
 * middle and the right pane share one header line with one border.
 *
 * When a bar's tabs and actions do not fit, the bar steps down one level at
 * a time and is measured again:
 *   tabs-bar--fit-1  the Bubble toggle shows its icon only, and the right
 *                    pane's actions fold into its "More actions" menu;
 *   tabs-bar--fit-2  the tabs drop their icons and some padding;
 *   tabs-bar--fit-3  nothing more to drop: the tab list scrolls sideways
 *                    and fades at its end.
 * styles.css (and a theme's own stylesheet) says what each level hides.
 *
 * The bars are measured instead of switched at fixed widths because the
 * room the labels need depends on the language, the theme and the fonts
 * of the browser.
 */

export const FIT_CLASSES = ['tabs-bar--fit-1', 'tabs-bar--fit-2', 'tabs-bar--fit-3'];

const px = (value) => parseFloat(value) || 0;

/** True when the bar's tabs and actions need more room than the bar has. */
export function overflows(bar, view = globalThis) {
    const style = view.getComputedStyle(bar);
    const room = bar.clientWidth - px(style.paddingLeft) - px(style.paddingRight);
    let need = 0;
    let items = 0;
    for (const child of bar.children) {
        if (view.getComputedStyle(child).display === 'none') continue;
        // scrollWidth counts a shrunken, scrolling tab list at its full width;
        // the difference between offsetWidth and clientWidth adds its borders.
        need += child.scrollWidth + (child.offsetWidth - child.clientWidth);
        items += 1;
    }
    if (items > 1) need += px(style.columnGap) * (items - 1);
    return need > room;
}

/**
 * Put the bar on the lowest level at which it fits, or on the last level.
 * Each level keeps the classes of the levels before it.
 */
export function fitTabsBar(bar, measure = overflows) {
    if (!bar.clientWidth) return -1;
    bar.classList.remove(...FIT_CLASSES);
    let level = 0;
    while (level < FIT_CLASSES.length && measure(bar)) {
        bar.classList.add(FIT_CLASSES[level]);
        level += 1;
    }
    return level;
}

/**
 * Watch every [data-tabs-fit] bar in the document, now and when Vue adds
 * one later: a bar is fitted again when its width or the width of its tabs
 * or actions changes (another tab, another language, a dragged divider).
 */
export function installTabsBarFit(doc = document) {
    const view = doc.defaultView;
    if (doc.__speakrTabsBarFit || !view || typeof view.ResizeObserver === 'undefined') return;
    doc.__speakrTabsBarFit = true;
    const watched = new Set();
    const observer = new view.ResizeObserver((entries) => {
        const bars = new Set();
        for (const entry of entries) {
            const bar = entry.target.closest('[data-tabs-fit]');
            if (bar) bars.add(bar);
        }
        for (const bar of bars) {
            if (!bar.isConnected) {
                observer.unobserve(bar);
                for (const child of bar.children) observer.unobserve(child);
                watched.delete(bar);
                continue;
            }
            fitTabsBar(bar);
        }
    });
    const watch = (bar) => {
        if (watched.has(bar)) return;
        watched.add(bar);
        observer.observe(bar);
        for (const child of bar.children) observer.observe(child);
    };
    const scan = (node) => {
        if (node.nodeType !== 1) return;
        if (node.matches('[data-tabs-fit]')) watch(node);
        node.querySelectorAll('[data-tabs-fit]').forEach(watch);
    };
    scan(doc.body);
    new view.MutationObserver((records) => {
        for (const record of records) record.addedNodes.forEach(scan);
    }).observe(doc.body, { childList: true, subtree: true });
}
