import adapter from '@sveltejs/adapter-node';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter(),
    csrf: {
      // Self-hosted, single-origin app. Session auth uses a SameSite=Lax +
      // HttpOnly cookie, which already prevents cross-site POSTs from carrying
      // credentials. SvelteKit's Origin header check 403s legit multipart
      // uploads (OPML import) when the computed request origin differs from
      // the browser-visible one behind the adapter/proxy. Disable it.
      checkOrigin: false
    }
  }
};

export default config;
