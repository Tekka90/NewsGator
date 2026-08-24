<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import { currentUser } from '$lib/stores';

  let { children } = $props();
  let ready = $state(false);
  let isPublic = $derived(
    page.url.pathname === '/login' || page.url.pathname === '/setup'
  );

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
      }
    } catch {
      if (!isPublic) await goto('/login');
    } finally {
      ready = true;
    }
  });

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
    <nav>
      <strong>NewsGator</strong>
      <a href="/">Stories</a>
      <a href="/feeds">Feeds</a>
      <a href="/settings">Settings</a>
      <span class="spacer"></span>
      <span class="user">{$currentUser?.username}</span>
      <button onclick={logout}>Log out</button>
    </nav>
    <main>{@render children()}</main>
  </div>
{/if}

<style>
  :global(body) {
    font-family: system-ui, sans-serif;
    margin: 0;
    background: #f6f7f9;
    color: #1c1e21;
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
    background: #1c1e21;
    color: #fff;
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
  main {
    max-width: 960px;
    margin: 1.5rem auto;
    padding: 0 1rem;
  }
  :global(button) {
    cursor: pointer;
  }
  :global(.card) {
    background: #fff;
    border: 1px solid #e3e5e8;
    border-radius: 8px;
    padding: 1rem;
    margin-bottom: 0.8rem;
  }
  :global(input, select) {
    padding: 0.4rem 0.5rem;
    border: 1px solid #c9ccd1;
    border-radius: 6px;
  }
  :global(label) {
    display: block;
    margin: 0.5rem 0 0.2rem;
    font-size: 0.9em;
  }
</style>
