// Production server hook: proxy /api to the backend container.
// In dev this is handled by vite.config.ts; adapter-node needs it here.
import { env } from '$env/dynamic/private';

const BACKEND = env.BACKEND_URL ?? 'http://localhost:8000';

/** @type {import('@sveltejs/kit').Handle} */
export async function handle({ event, resolve }) {
  if (event.url.pathname.startsWith('/api/')) {
    const target = BACKEND + event.url.pathname + event.url.search;
    const headers = new Headers(event.request.headers);
    headers.delete('host');
    const resp = await fetch(target, {
      method: event.request.method,
      headers,
      body: ['GET', 'HEAD'].includes(event.request.method)
        ? undefined
        : await event.request.arrayBuffer(),
      // @ts-expect-error undici duplex for streaming bodies
      duplex: 'half'
    });
    return new Response(resp.body, { status: resp.status, headers: resp.headers });
  }
  return resolve(event);
}
