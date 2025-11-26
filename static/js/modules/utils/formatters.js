/**
 * Formatting utility functions
 */

export const formatFileSize = (bytes) => {
    if (bytes == null || bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    if (bytes < 0) bytes = 0;
    const i = bytes === 0 ? 0 : Math.max(0, Math.floor(Math.log(bytes) / Math.log(k)));
    const size = i === 0 ? bytes : parseFloat((bytes / Math.pow(k, i)).toFixed(2));
    return size + ' ' + sizes[i];
};

export const formatDisplayDate = (dateString) => {
    if (!dateString) return '';
    try {
        let date = new Date(dateString);

        if (isNaN(date.getTime())) {
            if (/^\d{4}-\d{2}-\d{2}$/.test(dateString)) {
                date = new Date(dateString + 'T00:00:00');
            } else {
                return dateString;
            }
        }

        if (isNaN(date.getTime())) {
            return dateString;
        }

        return date.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
    } catch (e) {
        console.error("Error formatting date:", e);
        return dateString;
    }
};

export const formatShortDate = (dateString) => {
    if (!dateString) return '';
    try {
        let date = new Date(dateString);

        if (isNaN(date.getTime())) {
            if (/^\d{4}-\d{2}-\d{2}$/.test(dateString)) {
                date = new Date(dateString + 'T00:00:00');
            } else {
                return dateString;
            }
        }

        if (isNaN(date.getTime())) {
            return dateString;
        }

        const now = new Date();
        const isCurrentYear = date.getFullYear() === now.getFullYear();

        if (isCurrentYear) {
            return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        } else {
            return date.toLocaleDateString(undefined, { year: '2-digit', month: 'short', day: 'numeric' });
        }
    } catch (e) {
        console.error("Error formatting short date:", e);
        return dateString;
    }
};

export const formatStatus = (status, t) => {
    if (!status || status === 'COMPLETED') return '';
    const statusMap = {
        'PENDING': t('status.queued'),
        'QUEUED': t('status.queued'),
        'PROCESSING': t('status.processing'),
        'TRANSCRIBING': t('status.transcribing'),
        'SUMMARIZING': t('status.summarizing'),
        'FAILED': t('status.failed'),
        'UPLOADING': t('status.uploading')
    };
    return statusMap[status] || status.charAt(0).toUpperCase() + status.slice(1).toLowerCase();
};

export const getStatusClass = (status) => {
    switch(status) {
        case 'PENDING': return 'status-pending';
        case 'QUEUED': return 'status-pending';
        case 'PROCESSING': return 'status-processing';
        case 'SUMMARIZING': return 'status-summarizing';
        case 'COMPLETED': return '';
        case 'FAILED': return 'status-failed';
        default: return 'status-pending';
    }
};

export const formatTime = (seconds) => {
    const minutes = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
};

export const formatDuration = (totalSeconds) => {
    if (totalSeconds == null || totalSeconds < 0) return 'N/A';

    if (totalSeconds < 1) {
        return `${totalSeconds.toFixed(2)} seconds`;
    }

    totalSeconds = Math.round(totalSeconds);

    if (totalSeconds < 60) {
        return `${totalSeconds} sec`;
    }

    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;

    let parts = [];
    if (hours > 0) {
        parts.push(`${hours} hr`);
    }
    if (minutes > 0) {
        parts.push(`${minutes} min`);
    }
    if (hours === 0 && seconds > 0) {
        parts.push(`${seconds} sec`);
    }

    return parts.join(' ');
};

export const formatProcessingDuration = (seconds) => {
    if (!seconds && seconds !== 0) return null;
    if (seconds < 60) return `${seconds}s`;
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
};
