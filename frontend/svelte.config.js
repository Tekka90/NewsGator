import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter(),
    csrf: {
      // Browsers legitimately send `Origin: null` (file-picked OPML uploads,
      // privacy modes, sandboxed contexts). Trust it — the session cookie is
      // SameSite=Lax, so real cross-site posts can't carry auth anyway.
      trustedOrigins: ['null']
    }
  }
};

export default config;
