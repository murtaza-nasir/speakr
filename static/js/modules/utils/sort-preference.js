/**
 * Which sort the recordings list opens with.
 *
 * A choice this browser saved (the Sort menu writes `recordingsSortBy`) wins,
 * then the account's default, then 'created_at', which has always been the
 * default. Anything unrecognised falls through, so a stale or tampered value
 * cannot leave the list in an unknown state.
 */

export const SORT_OPTIONS = ['created_at', 'meeting_date'];

export function resolveInitialSort(saved, accountDefault) {
    if (SORT_OPTIONS.includes(saved)) return saved;
    if (SORT_OPTIONS.includes(accountDefault)) return accountDefault;
    return 'created_at';
}
