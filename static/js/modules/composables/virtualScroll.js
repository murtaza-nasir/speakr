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
 *
 * With variableHeight: true, itemHeight is only the estimate for rows that
 * have not been rendered yet. Rendered rows (found with rowSelector, each
 * carrying its item index in rowIndexAttr) are measured after every render
 * and whenever they resize, and their real heights place the rows. When rows
 * above the viewport turn out taller or shorter, the scroll position moves by
 * the difference, so the visible rows stay where they are. beforeMeasure(rows)
 * runs before each measurement, e.g. to size textareas to their text.
 */

import { buildOffsets, indexAtOffset, applyMeasurements } from '../utils/row-offsets.js';

export function useVirtualScroll(options) {
    const { ref, computed, watch, onMounted, onUnmounted } = Vue;

    const {
        items,              // Ref to the full array of items
        itemHeight = 48,    // Height of each item in pixels (fixed height mode)
        containerRef,       // Ref to the scrollable container element
        overscan = 5,       // Number of items to render outside viewport
        keyField = null,    // Optional field to use for unique keys
        variableHeight = false,
        rowSelector = '[data-virtual-index]',
        rowIndexAttr = 'virtualIndex',
        beforeMeasure = null
    } = options;

    // Measured heights (variable-height mode); `heightsVersion` re-runs the
    // offset computation after a measurement.
    const measured = new Map();
    const heightsVersion = ref(0);
    const offsets = computed(() => {
        void heightsVersion.value;
        const count = items.value ? items.value.length : 0;
        return buildOffsets(count, itemHeight, variableHeight ? measured : new Map());
    });
    const topOf = (index) => (variableHeight ? offsets.value[index] : index * itemHeight);

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

        let firstVisible;
        let lastVisible;
        if (variableHeight) {
            firstVisible = indexAtOffset(offsets.value, scrollTop.value);
            lastVisible = indexAtOffset(offsets.value, scrollTop.value + containerHeight.value);
        } else {
            // Calculate first visible item
            firstVisible = Math.floor(scrollTop.value / itemHeight);
            // Calculate number of items that fit in viewport
            lastVisible = firstVisible + Math.ceil(containerHeight.value / itemHeight);
        }

        // Add overscan for smooth scrolling
        const start = Math.max(0, firstVisible - overscan);
        const end = Math.min(totalItems, lastVisible + overscan);

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
        return topOf(visibleRange.value.start);
    });

    // Spacer height after visible items
    const spacerAfter = computed(() => {
        if (!items.value) return 0;
        if (variableHeight) {
            return Math.max(0, offsets.value[items.value.length] - offsets.value[visibleRange.value.end]);
        }
        const remainingItems = items.value.length - visibleRange.value.end;
        return Math.max(0, remainingItems * itemHeight);
    });

    // Total height of all items (for scroll container)
    const totalHeight = computed(() => {
        if (!items.value) return 0;
        return topOf(items.value.length);
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
        const targetScrollTop = topOf(targetIndex);

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
        let targetIndex;
        if (variableHeight) {
            targetIndex = indexAtOffset(offsets.value, Math.max(0, topOf(index) - containerHeight.value / 2));
        } else {
            targetIndex = Math.max(0, index - Math.floor(containerHeight.value / itemHeight / 2));
        }
        scrollToIndex(targetIndex, 'smooth');
    };

    // ---- Measuring rendered rows (variable-height mode).
    let rowObserver = null;
    let measureFrame = 0;
    const measureRendered = () => {
        measureFrame = 0;
        const container = containerRef.value;
        if (!variableHeight || !container) return;
        const rows = Array.from(container.querySelectorAll(rowSelector));
        if (beforeMeasure) beforeMeasure(rows);
        const updates = [];
        rows.forEach((row) => {
            const index = Number(row.dataset[rowIndexAttr]);
            if (Number.isFinite(index)) updates.push([index, row.getBoundingClientRect().height]);
        });
        const { changed, shiftAbove } = applyMeasurements(
            measured, offsets.value, itemHeight, container.scrollTop, updates);
        if (!changed) return;
        heightsVersion.value++;
        if (shiftAbove) {
            container.scrollTop += shiftAbove;
            scrollTop.value = container.scrollTop;
        }
        if (rowObserver) {
            rowObserver.disconnect();
            rows.forEach((row) => rowObserver.observe(row));
        }
    };
    const scheduleMeasure = () => {
        if (!measureFrame) measureFrame = requestAnimationFrame(measureRendered);
    };
    if (variableHeight) {
        if (window.ResizeObserver) rowObserver = new ResizeObserver(scheduleMeasure);
        watch(visibleItems, () => Vue.nextTick(scheduleMeasure), { flush: 'post' });
        // Inserted or removed rows shift every index below them.
        watch(() => (items.value ? items.value.length : 0), () => {
            measured.clear();
            heightsVersion.value++;
        });
    }

    // Reset scroll state (call when modal opens or items change completely)
    const reset = () => {
        scrollTop.value = 0;
        if (variableHeight) {
            measured.clear();
            heightsVersion.value++;
        }
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
        if (rowObserver) rowObserver.disconnect();
        if (measureFrame) cancelAnimationFrame(measureFrame);
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
        measureRendered,

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
