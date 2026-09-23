<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { api, streamUrl } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import '$lib/theme.css';

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
  :global(html) {
    /* match theme-color: the iOS status-bar glass sits on the page background
       so it reads as a subtle frosted band, not a smear on the nav */
    background: var(--bg);
    /* the swipe deck lets cards fly off-screen sideways (over the gutters
       around the centered column); clip that at the html level — on html
       (not body) the viewport stays the scroll container, so position:sticky
       keeps working; `clip` never creates a scroll container at all */
    overflow-x: hidden;
    overflow-x: clip;
  }
  :global(body) {
    font-family: var(--font-sans);
    margin: 0;
    background: var(--bg);
    color: var(--text);
    -webkit-font-smoothing: antialiased;
  }
  .center {
    display: flex;
    justify-content: center;
    padding-top: 4rem;
  }
  .shell nav {
    display: flex;
    align-items: center;
    gap: 0.8rem;
    padding: 0.65rem 1.2rem;
    /* iOS standalone: keep clear of the status bar / rounded corners */
    padding-top: calc(0.65rem + env(safe-area-inset-top, 0px));
    padding-left: calc(1.2rem + env(safe-area-inset-left, 0px));
    padding-right: calc(1.2rem + env(safe-area-inset-right, 0px));
    background: var(--gator-forest-dark);
    color: #fff;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    /* keep the menu visible while scrolling */
    position: sticky;
    top: 0;
    z-index: 50;
  }
  .shell nav .brand {
    font-weight: 800;
    letter-spacing: -0.03em;
    font-size: 1.05rem;
    color: #fff;
    margin-right: 0.2rem;
  }
  .shell nav a {
    color: #d1ded6;
    text-decoration: none;
    font-size: 0.92rem;
    font-weight: 500;
    padding: 0.28rem 0.75rem;
    border-radius: var(--radius-pill);
    transition: background 0.15s ease, color 0.15s ease;
  }
  .shell nav a:hover {
    color: #fff;
    background: rgba(255, 255, 255, 0.1);
  }
  .shell nav a.active {
    color: #fff;
    background: var(--gator-forest);
    font-weight: 700;
  }
  .logoutbtn {
    /* icon-only — the full-width "Log out" text wasted precious nav space on iOS */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid transparent;
    border-radius: var(--radius-pill);
    color: #d1ded6;
    padding: 0.3rem;
    white-space: nowrap;
    transition: color 0.15s ease, background 0.15s ease;
  }
  .logoutbtn:hover {
    color: #fff;
    background: rgba(255, 255, 255, 0.12);
  }
  @media (max-width: 700px) {
    /* single compact line — no wrapping to a second row, no empty bands.
       Links must be allowed to shrink (flex-basis 0 + min-width 0) or the
       row overflows on narrow phones and pushes the whole page sideways. */
    .shell nav {
      flex-wrap: nowrap;
      gap: 0.25rem;
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
      padding: 0.28rem 0.35rem;
    }
    .shell nav .processing { flex: 0 0 auto; }
    /* too easy to fat-finger on touch — logout lives on the Settings page */
    .shell nav .logoutbtn { display: none; }
  }
  .spacer {
    flex: 1;
  }
  .user {
    color: #a4bea9;
    font-size: 0.88em;
  }
  .processing {
    color: var(--gator-gold);
    font-size: 0.85em;
    font-weight: 600;
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
    font-family: inherit;
    cursor: pointer;
  }
  :global(.card) {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: var(--gator-shadow-card);
    padding: 1.15rem;
    margin-bottom: 0.85rem;
  }
  :global(input, select) {
    font-family: inherit;
    padding: 0.45rem 0.6rem;
    border: 1px solid var(--border-input);
    border-radius: var(--radius-sm);
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
