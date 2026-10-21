export function validSegmentRange(segment) {
    return Number.isFinite(segment?.start_time) && Number.isFinite(segment?.end_time) &&
        segment.start_time >= 0 && segment.end_time > segment.start_time;
}

export function spectrogramWindow(start, end, length = Math.min(60, Math.max(0.25, end - start)), center = null) {
    const left = Math.max(0, start - (end - start < 0.25 ? 0.125 : 0));
    const right = Math.max(end, left + 0.25);
    const size = Math.min(60, Math.max(0.25, length), right - left);
    const offset = center === null ? left : Math.max(left, Math.min(right - size, center - size / 2));
    return { start: offset, end: offset + size };
}

export function plotTime(window, x, width) {
    if (!Number.isFinite(width) || width <= 0) return null;
    return Math.round((window.start + Math.max(0, Math.min(1, x / width)) * (window.end - window.start)) * 100) / 100;
}

export function markerPercent(window, time) {
    if (!Number.isFinite(time) || time < window.start || time > window.end || window.end <= window.start) return null;
    return (time - window.start) / (window.end - window.start) * 100;
}

// The native scrollbar is bounded; temporal scale stays independent of its CSS extent.
export function stripGeometry(range, span, viewportWidth) {
    const viewport = Math.max(1, Number(viewportWidth) || 1);
    const length = Math.max(0, range.end - range.start);
    const width = Math.min(1000000, Math.max(viewport, length / span * viewport));
    return {width, maxScroll: Math.max(0, width - viewport)};
}

export function viewportWindow(range, span, center = range.start + span / 2) {
    const size = Math.min(span, range.end - range.start);
    const start = Math.max(range.start, Math.min(range.end - size, center - size / 2));
    return {start, end: start + size};
}

export function scrollWindow(range, span, scrollLeft, viewportWidth) {
    const {maxScroll} = stripGeometry(range, span, viewportWidth);
    const size = Math.min(span, range.end - range.start);
    const offset = maxScroll ? Math.max(0, Math.min(1, scrollLeft / maxScroll)) : 0;
    const start = range.start + offset * Math.max(0, range.end - range.start - size);
    return {start, end: start + size};
}

export function scrollPosition(range, window, viewportWidth) {
    const span = window.end - window.start;
    const {maxScroll} = stripGeometry(range, span, viewportWidth);
    const travel = range.end - range.start - span;
    return travel > 0 ? Math.max(0, Math.min(maxScroll, (window.start - range.start) / travel * maxScroll)) : 0;
}

export function tileProjection(tile, range, window, viewportWidth) {
    const pixelsPerSecond = viewportWidth / (window.end - window.start);
    return {
        left: scrollPosition(range, window, viewportWidth) + (tile.start - window.start) * pixelsPerSecond,
        width: (tile.end - tile.start) * pixelsPerSecond
    };
}

export function pngDimensions(buffer) {
    const bytes = new Uint8Array(buffer);
    const signature = [137,80,78,71,13,10,26,10];
    if (bytes.length < 24 || signature.some((value,index)=>bytes[index]!==value) ||
        bytes[12]!==73 || bytes[13]!==72 || bytes[14]!==68 || bytes[15]!==82) return null;
    const view = new DataView(buffer);
    return {width:view.getUint32(16),height:view.getUint32(20)};
}
