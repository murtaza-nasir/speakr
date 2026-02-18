/**
 * Toast notification utility
 */

export const showToast = (message, iconClass = 'fa-info-circle', duration = 3000, type = 'info') => {
    const container = document.getElementById('toastContainer');
    if (!container) {
        console.warn('Toast container not found');
        return;
    }

    // Determine colors and styles based on type
    let bgColor, textColor, iconColor, borderColor;

    switch (type) {
        case 'success':
            bgColor = '#10b981'; // green-500
            textColor = '#ffffff';
            iconColor = '#ffffff';
            borderColor = '#059669'; // green-600
            break;
        case 'error':
            bgColor = '#ef4444'; // red-500
            textColor = '#ffffff';
            iconColor = '#ffffff';
            borderColor = '#dc2626'; // red-600
            break;
        case 'warning':
            bgColor = '#f59e0b'; // amber-500
            textColor = '#ffffff';
            iconColor = '#ffffff';
            borderColor = '#d97706'; // amber-600
            break;
        case 'info':
        default:
            bgColor = '#3b82f6'; // blue-500
            textColor = '#ffffff';
            iconColor = '#ffffff';
            borderColor = '#2563eb'; // blue-600
            break;
    }

    const toast = document.createElement('div');
    toast.className = 'toast-message px-4 py-3 rounded-lg shadow-lg flex items-center gap-3 opacity-0 min-w-[300px]';
    toast.style.backgroundColor = bgColor;
    toast.style.color = textColor;
    toast.style.border = `1px solid ${borderColor}`;

    // Handle icon class - support both old format (just icon name) and new format (full class)
    let fullIconClass = iconClass;
    if (!iconClass.includes(' ')) {
        // Old format: just the icon name like 'fa-check-circle'
        fullIconClass = `fas ${iconClass}`;
    }

    toast.innerHTML = `<i class="${fullIconClass}" style="color: ${iconColor}"></i>
        <span class="flex-1">${message}</span>
    `;

    // Make toast clickable to dismiss
    toast.style.cursor = 'pointer';

    container.appendChild(toast);

    // Trigger fly-in animation
    requestAnimationFrame(() => {
        toast.classList.remove('opacity-0');
        toast.classList.add('opacity-100', 'toast-show');
    });

    // Function to dismiss the toast
    const dismissToast = () => {
        toast.classList.remove('opacity-100', 'toast-show');
        toast.classList.add('opacity-0');
        setTimeout(() => {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, 300);
    };

    // Add click handler to dismiss toast
    toast.addEventListener('click', () => {
        clearTimeout(timeoutId);
        dismissToast();
    });

    // Auto-dismiss after duration
    const timeoutId = setTimeout(dismissToast, duration);
};
