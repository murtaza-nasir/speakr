/**
 * Pagination composable
 * Handles pagination state and navigation
 */

import { ref, computed } from 'vue';

export function usePagination() {
    // State
    const currentPage = ref(1);
    const perPage = ref(25);
    const totalRecordings = ref(0);
    const totalPages = ref(0);
    const hasNextPage = ref(false);
    const hasPrevPage = ref(false);
    const isLoadingMore = ref(false);

    // Computed
    const paginationInfo = computed(() => {
        const start = (currentPage.value - 1) * perPage.value + 1;
        const end = Math.min(currentPage.value * perPage.value, totalRecordings.value);
        return {
            start,
            end,
            total: totalRecordings.value,
            currentPage: currentPage.value,
            totalPages: totalPages.value
        };
    });

    // Methods
    const updatePagination = (pagination) => {
        if (!pagination) {
            // Reset pagination for non-paginated views
            currentPage.value = 1;
            totalPages.value = 1;
            hasNextPage.value = false;
            hasPrevPage.value = false;
            return;
        }

        currentPage.value = pagination.page;
        totalRecordings.value = pagination.total;
        totalPages.value = pagination.total_pages;
        hasNextPage.value = pagination.has_next;
        hasPrevPage.value = pagination.has_prev;
    };

    const goToPage = (page) => {
        if (page < 1 || page > totalPages.value) return;
        currentPage.value = page;
    };

    const nextPage = () => {
        if (hasNextPage.value) {
            currentPage.value++;
        }
    };

    const prevPage = () => {
        if (hasPrevPage.value) {
            currentPage.value--;
        }
    };

    const reset = () => {
        currentPage.value = 1;
        totalRecordings.value = 0;
        totalPages.value = 0;
        hasNextPage.value = false;
        hasPrevPage.value = false;
        isLoadingMore.value = false;
    };

    return {
        // State
        currentPage,
        perPage,
        totalRecordings,
        totalPages,
        hasNextPage,
        hasPrevPage,
        isLoadingMore,

        // Computed
        paginationInfo,

        // Methods
        updatePagination,
        goToPage,
        nextPage,
        prevPage,
        reset
    };
}
