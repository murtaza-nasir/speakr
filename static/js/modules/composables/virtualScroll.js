/**
 * Virtual Scrolling Composable
 *
 * Renders only visible items plus a buffer for smooth scrolling.
 * Critical for handling long transcriptions (4500+ segments) without UI lag.
 *
 * Usage:
 *   const { visibleItems, spacerBefore, spacerAfter, onScroll, scrollToIndex } = useVirtualScroll({
 *       items: segmentsRef,
 *       itemHeight: 48,
 *       containerRef: scrollContainerRef,
 *       overscan: 5
 *   });
 */

export function useVirtualScroll(options) {
    const { ref, computed, watch, onMounted, onUnmounted } = Vue;

    const {
        items,              // Ref to the full array of items
        itemHeight = 48,    // Height of each item in pixels (fixed height mode)
        containerRef,       // Ref to the scrollable container element
        overscan = 5,       // Number of items to render outside viewport
        keyField = null     // Optional field to use for unique keys
    } = options;

    // Internal state
    const scrollTop = ref(0);
    const containerHeight = ref(0);
    const isInitialized = ref(false);

    // Calculate visible range based on scroll position
    const visibleRange = computed(() => {
        if (!isInitialized.value || !items.value) {
            return { start: 0, end: Math.min(20, items.value?.length || 0) };
        }

        const totalItems = items.value.length;
        if (totalItems === 0) {
            return { start: 0, end: 0 };
        }

        // Calculate first visible item
        const firstVisible = Math.floor(scrollTop.value / itemHeight);

        // Calculate number of items that fit in viewport
        const visibleCount = Math.ceil(containerHeight.value / itemHeight);

        // Add overscan for smooth scrolling
        const start = Math.max(0, firstVisible - overscan);
        const end = Math.min(totalItems, firstVisible + visibleCount + overscan);

        return { start, end };
    });

    // Slice of items to actually render
    const visibleItems = computed(() => {
        if (!items.value || items.value.length === 0) {
            return [];
        }

        const { start, end } = visibleRange.value;

        // Map items with their original indices for proper data binding
        return items.value.slice(start, end).map((item, localIndex) => ({
            ...item,
            _virtualIndex: start + localIndex,
            _originalIndex: start + localIndex
        }));
    });

    // Spacer height before visible items (for scroll position)
    const spacerBefore = computed(() => {
        return visibleRange.value.start * itemHeight;
    });

    // Spacer height after visible items
    const spacerAfter = computed(() => {
        if (!items.value) return 0;
        const remainingItems = items.value.length - visibleRange.value.end;
        return Math.max(0, remainingItems * itemHeight);
    });

    // Total height of all items (for scroll container)
    const totalHeight = computed(() => {
        if (!items.value) return 0;
        return items.value.length * itemHeight;
    });

    // Handle scroll events
    const onScroll = (event) => {
        scrollTop.value = event.target.scrollTop;
    };

    // Initialize container height observer
    let resizeObserver = null;

    const initializeContainer = () => {
        if (!containerRef.value) return;

        // Get initial height
        containerHeight.value = containerRef.value.clientHeight;
        isInitialized.value = true;

        // Watch for container size changes
        resizeObserver = new ResizeObserver((entries) => {
            for (const entry of entries) {
                containerHeight.value = entry.contentRect.height;
            }
        });
        resizeObserver.observe(containerRef.value);
    };

    // Scroll to a specific index
    const scrollToIndex = (index, behavior = 'smooth') => {
        if (!containerRef.value || !items.value) return;

        const targetIndex = Math.max(0, Math.min(index, items.value.length - 1));
        const targetScrollTop = targetIndex * itemHeight;

        containerRef.value.scrollTo({
            top: targetScrollTop,
            behavior
        });
    };

    // Scroll to make an index visible (centered if possible)
    const scrollToIndexIfNeeded = (index) => {
        if (!containerRef.value || !items.value) return;

        const { start, end } = visibleRange.value;

        // Check if index is already visible (with some margin)
        if (index >= start + overscan && index < end - overscan) {
            return; // Already visible
        }

        // Center the index in the viewport
        const targetIndex = Math.max(0, index - Math.floor(containerHeight.value / itemHeight / 2));
        scrollToIndex(targetIndex, 'smooth');
    };

    // Reset scroll state (call when modal opens or items change completely)
    const reset = () => {
        scrollTop.value = 0;
        isInitialized.value = false;
        // Re-initialize after a tick to allow DOM to render
        Vue.nextTick(() => {
            if (containerRef.value) {
                containerRef.value.scrollTop = 0;
                initializeContainer();
            }
        });
    };

    // Watch for containerRef changes and initialize
    watch(containerRef, (newRef) => {
        if (newRef) {
            initializeContainer();
        }
    }, { immediate: true });

    // Cleanup on unmount
    onUnmounted(() => {
        if (resizeObserver) {
            resizeObserver.disconnect();
        }
    });

    return {
        // Data
        visibleItems,
        visibleRange,

        // Spacer heights for virtual scroll container
        spacerBefore,
        spacerAfter,
        totalHeight,

        // Event handlers
        onScroll,

        // Navigation
        scrollToIndex,
        scrollToIndexIfNeeded,

        // Control
        reset,

        // State (for debugging/testing)
        scrollTop,
        containerHeight,
        isInitialized
    };
}

/**
 * Helper to generate a unique key for virtual scroll items
 */
export function getVirtualItemKey(item, prefix = 'vs') {
    const index = item._originalIndex ?? item._virtualIndex ?? 0;
    const time = item.startTime ?? item.start_time ?? '';
    return `${prefix}-${index}-${time}`;
}
