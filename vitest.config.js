import { defineConfig } from 'vitest/config';

export default defineConfig({
    test: {
        include: ['static/js/**/*.test.js'],
        environment: 'node',
        globals: false,
    },
});
