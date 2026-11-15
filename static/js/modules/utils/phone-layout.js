/**
 * The phone layout of the recording page and of the recording list
 * (templates/components/phone/). Pure logic, no DOM and no Vue; the state is
 * in composables/phone-layout.js.
 */

/** Below this width the app shows the phone layout (isMobile in composables/ui.js). */
export const PHONE_MAX_WIDTH = 767;

const valid = (date) => date instanceof Date && !isNaN(date.getTime());

/** Whether two dates fall on the same day of the viewer's calendar. */
export function sameCalendarDay(a, b) {
    if (!valid(a) || !valid(b)) return false;
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/**
 * A date in few characters: "Nov 12", with the year when it is not this
 * year, and with the time of day when `time` is set ("Nov 12, 10:02 AM").
 */
export function formatCompactDate(date, { locale, now = new Date(), time = false } = {}) {
    if (!valid(date)) return '';
    const options = { month: 'short', day: 'numeric' };
    if (date.getFullYear() !== now.getFullYear()) options.year = 'numeric';
    if (time) {
        options.hour = 'numeric';
        options.minute = '2-digit';
    }
    // `locale` is the interface locale, given by the caller. A tag
    // the browser does not know throws; English is used then.
    const format = (tag) => date.toLocaleString(tag, options);
    try {
        return format(locale);
    } catch (e) {
        return format('en');
    }
}

/**
 * The dates of the details line. A recording has the date of the meeting
 * and the date it was uploaded; the line shows both, and one when they fall
 * on the same day or when only one is known.
 *
 *   meeting    the meeting date with its time, or ''
 *   uploaded   the upload date, or '' when it is the day of the meeting
 *   only       'uploaded' when there is no meeting date (the upload date
 *              then carries the time), else ''
 */
export function detailDates(meeting, uploaded, { locale, now = new Date() } = {}) {
    const hasMeeting = valid(meeting);
    const hasUploaded = valid(uploaded);
    if (!hasMeeting && !hasUploaded) return { meeting: '', uploaded: '', only: '' };
    if (!hasMeeting) return { meeting: '', uploaded: formatCompactDate(uploaded, { locale, now, time: true }), only: 'uploaded' };
    const meetingText = formatCompactDate(meeting, { locale, now, time: true });
    if (!hasUploaded || sameCalendarDay(meeting, uploaded)) return { meeting: meetingText, uploaded: '', only: '' };
    return { meeting: meetingText, uploaded: formatCompactDate(uploaded, { locale, now }), only: '' };
}

/** The speaker of the segment at `index`, or ''. */
export function speakerAt(segments, index) {
    if (!Array.isArray(segments) || typeof index !== 'number' || index < 0 || index >= segments.length) return '';
    const segment = segments[index];
    return (segment && typeof segment.speaker === 'string' && segment.speaker) || '';
}

/** The time `delta` seconds from `current`, kept inside the recording. */
export function skipTarget(current, delta, duration) {
    const from = isFinite(current) ? current : 0;
    const to = from + delta;
    const end = isFinite(duration) && duration > 0 ? duration : Infinity;
    return Math.max(0, Math.min(to, end));
}

/**
 * The height of the composer for the height of its text: one line at
 * least, and no more than `max` (about five lines), after which it scrolls.
 */
export function composerHeight(scrollHeight, { min = 44, max = 124 } = {}) {
    const height = isFinite(scrollHeight) ? Math.ceil(scrollHeight) : min;
    return Math.max(min, Math.min(height, max));
}

/**
 * Whether the recording list is the screen shown on a phone: when it was
 * opened, and whenever there is nothing else to show (no recording open
 * and no recording in progress).
 */
export function phoneListShown({ isPhone, sidebarCollapsed, view, hasRecording }) {
    if (!isPhone) return false;
    if (!sidebarCollapsed) return true;
    if (view === 'recording') return false;
    return !(view === 'detail' && hasRecording);
}

/** How many of the filters behind the search field's button are set. */
export function advancedFilterCount({ tags = [], speakers = [], datePreset = '', dateRange = {}, audioRemoved = false,
    needsTranscription = false, needsSummary = false, needsSpeakers = false } = {}) {
    return (tags.length ? 1 : 0) + (speakers.length ? 1 : 0)
        + (datePreset || (dateRange && (dateRange.start || dateRange.end)) ? 1 : 0)
        + (audioRemoved ? 1 : 0) + (needsTranscription ? 1 : 0) + (needsSummary ? 1 : 0) + (needsSpeakers ? 1 : 0);
}
