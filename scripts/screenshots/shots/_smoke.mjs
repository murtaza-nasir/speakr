// Harness smoke test: one trivial shot. Not part of the documentation set.
import { go } from '../helpers.mjs';

export default [
    {
        name: 'smoke-main',
        description: 'Harness smoke test: the main library view after login',
        theme: { dark: true, scheme: 'blue' },
        run: async (page) => {
            await go(page, '/');
        },
    },
];
