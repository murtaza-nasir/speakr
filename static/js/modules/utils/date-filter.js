/**
 * Date filter values of the recordings list (#631).
 *
 * The date inputs of the filter panel hold a YYYY-MM-DD string, or '' when
 * empty. A value outside the range the server accepts (1900-01-01 to
 * 2100-12-31), or a day that does not exist, is not sent: the server would
 * refuse it with 400. The inputs carry the same min and max, so the browser
 * shows such a value as invalid.
 */

export const DATE_FILTER_MIN = '1900-01-01';
export const DATE_FILTER_MAX = '2100-12-31';

/** True for a real calendar day from DATE_FILTER_MIN to DATE_FILTER_MAX. */
export const isValidDateInput = (value) => {
    if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    if (value < DATE_FILTER_MIN || value > DATE_FILTER_MAX) return false;
    const [year, month, day] = value.split('-').map(Number);
    const d = new Date(Date.UTC(year, month - 1, day));
    return d.getUTCFullYear() === year && d.getUTCMonth() === month - 1 && d.getUTCDate() === day;
};
