<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { api, streamUrl } from '$lib/api';
  import { currentUser } from '$lib/stores';

  let { children } = $props();
  let ready = $state(false);
  let queueDepth = $state(0);
  // Height of the sticky nav, exposed as --nav-h so pages can stick their own
  // toolbars right below it (stories list header, …) without hardcoding pixels.
  let navEl = $state<HTMLElement>();
  let isPublic = $derived(
    page.url.pathname === '/login' || page.url.pathname === '/setup'
  );

  $effect(() => {
    if (!navEl) return;
    const el = navEl;
    const set = () =>
      document.documentElement.style.setProperty('--nav-h', `${el.offsetHeight}px`);
    set();
    const ro = new ResizeObserver(set);
    ro.observe(el);
    return () => ro.disconnect();
  });

  onMount(async () => {
    try {
      const { setup_needed } = await api.setupNeeded();
      if (setup_needed && page.url.pathname !== '/setup') {
        await goto('/setup');
        return;
      }
      if (!setup_needed && page.url.pathname === '/setup') {
        await goto('/login');
        return;
      }
      if (!isPublic) {
        $currentUser = await api.me();
        connectActivity();
      }
    } catch {
      if (!isPublic) await goto('/login');
    } finally {
      ready = true;
    }
  });

  // SPEC §7: 'now processing' indicator — LLM queue depth via SSE
  function connectActivity() {
    // EventSource can't set headers — pass the token as a query param
    const source = new EventSource(streamUrl('/api/activity/stream'));
    source.onmessage = (msg) => {
      const payload = JSON.parse(msg.data);
      if (payload.llm_queue_depth !== undefined) queueDepth = payload.llm_queue_depth;
    };
  }

  async function logout() {
    await api.logout();
    $currentUser = null;
    await goto('/login');
  }
</script>

<svelte:head><title>NewsGator</title></svelte:head>

{#if !ready}
  <main class="center"><p>Loading…</p></main>
{:else if isPublic}
  {@render children()}
{:else}
  <div class="shell">
    <nav bind:this={navEl}>
      <strong class="brand">NewsGator</strong>
      <a href="/" class:active={page.url.pathname === '/'}>Stories</a>
      <a href="/chat" class:active={page.url.pathname.startsWith('/chat')}>Chat</a>
      <a href="/feeds" class:active={page.url.pathname.startsWith('/feeds')}>Feeds</a>
      <a href="/activity" class:active={page.url.pathname.startsWith('/activity')}>Activity</a>
      {#if $currentUser?.is_admin}
        <a href="/usage" class:active={page.url.pathname.startsWith('/usage')}>Usage</a>
      {/if}
      <a href="/settings" class:active={page.url.pathname.startsWith('/settings')}>Settings</a>
      <span class="spacer"></span>
      {#if queueDepth > 0}
        <span class="processing">⚙ {queueDepth}</span>
      {/if}
      <span class="user">{$currentUser?.username}</span>
      <button class="logoutbtn" onclick={logout} title="Log out" aria-label="Log out">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16 17 21 12 16 7" /><line x1="21" y1="12" x2="9" y2="12" />
        </svg>
      </button>
    </nav>
    <main>{@render children()}</main>
  </div>
{/if}

<style>
  /* Design tokens — every page references these so dark mode is one override block. */
  :global(:root) {
    --bg: #f6f7f9;
    --surface: #ffffff;
    --text: #1c1e21;
    --text-secondary: #444444;
    --muted: #888888;
    --faint: #999999;
    --border: #e3e5e8;
    --border-strong: #d0d3d9;
    --border-hover: #b9bec6;
    --border-input: #c9ccd1;
    --accent: #294a7a;
    --chip-bg: #e8edf5;
    --ok: #1d6b2a;
    --ok-bg: #e3f2e5;
    --warn: #8a5a00;
    --warn-bg: #fff3d6;
    --error: #a12727;
    --error-bg: #fde7e7;
    --frozen-bg: #eeeeee;
    --frozen-text: #777777;
    --code-bg: #eeeeee;
    --disabled-bg: #f0f1f3;
    --disabled-text: #555555;
    --table-border: #e0e0e0;
    --row-border: #f0f0f0;
  }
  @media (prefers-color-scheme: dark) {
    :global(:root) {
      --bg: #131417;
      --surface: #1d1f24;
      --text: #e5e7eb;
      --text-secondary: #b9bec6;
      --muted: #8b919b;
      --faint: #767c86;
      --border: #2e3138;
      --border-strong: #3a3e46;
      --border-hover: #4a505a;
      --border-input: #3f444d;
      --accent: #9db8e2;
      --chip-bg: #27334b;
      --ok: #7bd389;
      --ok-bg: #16301d;
      --warn: #e3b45e;
      --warn-bg: #362a10;
      --error: #e08a8a;
      --error-bg: #371b1b;
      --frozen-bg: #26292f;
      --frozen-text: #9aa0a8;
      --code-bg: #2a2d33;
      --disabled-bg: #23262b;
      --disabled-text: #7d838d;
      --table-border: #34383f;
      --row-border: #26292e;
    }
  }
  :global(html) {
    /* match theme-color: the iOS status-bar glass sits on the page background
       so it reads as a subtle frosted band, not a smear on the nav */
    background: var(--bg);
  }
  :global(body) {
    font-family: system-ui, sans-serif;
    margin: 0;
    background: var(--bg);
    color: var(--text);
  }
  .center {
    display: flex;
    justify-content: center;
    padding-top: 4rem;
  }
  .shell nav {
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.6rem 1.2rem;
    /* iOS standalone: keep clear of the status bar / rounded corners */
    padding-top: calc(0.6rem + env(safe-area-inset-top, 0px));
    padding-left: calc(1.2rem + env(safe-area-inset-left, 0px));
    padding-right: calc(1.2rem + env(safe-area-inset-right, 0px));
    background: #1c1e21;
    color: #fff;
    /* keep the menu visible while scrolling */
    position: sticky;
    top: 0;
    z-index: 50;
  }
  .shell nav a.active { color: #fff; font-weight: 600; }
  .logoutbtn {
    /* icon-only — the full-width "Log out" text wasted precious nav space on iOS */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid transparent;
    border-radius: 6px;
    color: #cfd3da;
    padding: 0.25rem;
    white-space: nowrap;
  }
  .logoutbtn:hover { color: #fff; border-color: #4a505a; }
  @media (max-width: 700px) {
    /* single compact line — no wrapping to a second row, no empty bands.
       Links must be allowed to shrink (flex-basis 0 + min-width 0) or the
       row overflows on narrow phones and pushes the whole page sideways. */
    .shell nav {
      flex-wrap: nowrap;
      gap: 0.4rem;
      padding: 0.45rem 0.5rem;
      padding-top: calc(0.45rem + env(safe-area-inset-top, 0px));
      font-size: 0.84rem;
    }
    .shell nav .brand { display: none; }       /* links say where you are */
    .shell nav .user { display: none; }        /* username hidden on mobile */
    .shell nav .spacer { display: none; }
    .shell nav a {
      flex: 1 1 0;
      min-width: 0;
      text-align: center;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .shell nav .processing { flex: 0 0 auto; }
    .shell nav .logoutbtn { flex: 0 0 auto; padding: 0.15rem; }
  }
  .shell nav a {
    color: #cfd3da;
    text-decoration: none;
  }
  .shell nav a:hover {
    color: #fff;
  }
  .spacer {
    flex: 1;
  }
  .user {
    color: #9aa0a8;
    font-size: 0.9em;
  }
  .processing {
    color: #ffd97a;
    font-size: 0.85em;
  }
  main {
    max-width: 960px;
    margin: 1.5rem auto;
    padding: 0 1rem;
  }
  @media (max-width: 700px) {
    main {
      margin: 0.8rem auto;
      padding: 0 0.6rem;
    }
  }
  :global(button) {
    cursor: pointer;
  }
  :global(.card) {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 0.8rem;
  }
  :global(input, select) {
    padding: 0.4rem 0.5rem;
    border: 1px solid var(--border-input);
    border-radius: 6px;
    background: var(--surface);
    color: var(--text);
    /* default size=20 (~11rem) must never push a container past the viewport */
    max-width: 100%;
    box-sizing: border-box;
  }
  :global(label) {
    display: block;
    margin: 0.5rem 0 0.2rem;
    font-size: 0.9em;
  }
</style>
