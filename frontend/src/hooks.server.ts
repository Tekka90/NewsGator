// Production server hook: proxy /api to the backend container.
// In dev this is handled by vite.config.ts; adapter-node needs it here.
import { env } from '$env/dynamic/private';

const BACKEND = env.BACKEND_URL ?? 'http://localhost:8000';

// Node's bundled undici can throw `assert(!this.paused)` from a socket 'end'
// handler when the upstream SSE stream closes while its reader is
// backpressured. It surfaces as an uncaught exception that kills the whole
// frontend process; swallow only that known case.
const g = globalThis as typeof globalThis & {
  __ngUndiciGuard?: boolean;
  process: {
    on(ev: string, fn: (err: { code?: string; stack?: string; message: string }) => void): void;
    exit(code: number): never;
  };
};
if (!g.__ngUndiciGuard) {
  g.__ngUndiciGuard = true;
  g.process.on('uncaughtException', (err) => {
    if (err?.code === 'ERR_ASSERTION' && /undici/.test(err.stack ?? '')) {
      console.error('[proxy] ignored undici assertion:', err.message.split('\n')[0]);
      return;
    }
    console.error(err);
    g.process.exit(1);
  });
}

/** @type {import('@sveltejs/kit').Handle} */
export async function handle({ event, resolve }) {
  if (event.url.pathname.startsWith('/api/')) {
    const target = BACKEND + event.url.pathname + event.url.search;
    const headers = new Headers(event.request.headers);
    headers.delete('host');
    let resp: Response;
    try {
      resp = await fetch(target, {
        method: event.request.method,
        headers,
        body: ['GET', 'HEAD'].includes(event.request.method)
          ? undefined
          : await event.request.arrayBuffer(),
        signal: event.request.signal,
        // @ts-expect-error undici duplex for streaming bodies
        duplex: 'half'
      });
    } catch {
      return new Response('Backend unavailable', { status: 502 });
    }
    return new Response(resp.body, { status: resp.status, headers: resp.headers });
  }
  const resp = await resolve(event);
  // Standalone PWAs cache HTML aggressively and can relaunch on a stale page.
  // HTML is never fingerprinted, so disable caching for it; hashed _app assets
  // keep their own long-lived cache headers from the adapter.
  if ((resp.headers.get('content-type') ?? '').includes('text/html')) {
    resp.headers.set('cache-control', 'no-cache');
  }
  return resp;
}
