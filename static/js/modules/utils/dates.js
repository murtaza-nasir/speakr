/**
 * Date comparison utility functions
 */

export const isSameDay = (date1, date2) => {
    return date1.getFullYear() === date2.getFullYear() &&
           date1.getMonth() === date2.getMonth() &&
           date1.getDate() === date2.getDate();
};

export const isToday = (date) => {
    const today = new Date();
    return isSameDay(date, today);
};

export const isYesterday = (date) => {
    const yesterday = new Date();
    yesterday.setDate(yesterday.getDate() - 1);
    return isSameDay(date, yesterday);
};

export const isThisWeek = (date) => {
    const now = new Date();
    const startOfWeek = new Date(now);
    const day = now.getDay();
    const diff = now.getDate() - day + (day === 0 ? -6 : 1); // Monday as start of week
    startOfWeek.setDate(diff);
    startOfWeek.setHours(0, 0, 0, 0);

    const endOfWeek = new Date(startOfWeek);
    endOfWeek.setDate(startOfWeek.getDate() + 6);
    endOfWeek.setHours(23, 59, 59, 999);

    return date >= startOfWeek && date <= endOfWeek;
};

export const isLastWeek = (date) => {
    const now = new Date();
    const startOfLastWeek = new Date(now);
    const day = now.getDay();
    const diff = now.getDate() - day + (day === 0 ? -6 : 1) - 7; // Previous Monday
    startOfLastWeek.setDate(diff);
    startOfLastWeek.setHours(0, 0, 0, 0);

    const endOfLastWeek = new Date(startOfLastWeek);
    endOfLastWeek.setDate(startOfLastWeek.getDate() + 6);
    endOfLastWeek.setHours(23, 59, 59, 999);

    return date >= startOfLastWeek && date <= endOfLastWeek;
};

export const isThisMonth = (date) => {
    const now = new Date();
    return date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth();
};

export const isLastMonth = (date) => {
    const now = new Date();
    const lastMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1);
    return date.getFullYear() === lastMonth.getFullYear() && date.getMonth() === lastMonth.getMonth();
};

export const getDateForSorting = (recording, sortBy) => {
    const dateStr = sortBy === 'meeting_date' ? recording.meeting_date : recording.created_at;
    if (!dateStr) return null;
    return new Date(dateStr);
};
