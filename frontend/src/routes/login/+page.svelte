<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { api, authHeaders, getToken } from '$lib/api';
  import { currentUser } from '$lib/stores';

  let username = $state('');
  let password = $state('');
  let error = $state('');
  let debug = $state('');

  // Temporary PWA session diagnostic: shows whether the stored token survived
  // an app restart and whether the server accepts it.
  onMount(async () => {
    const stored = !!getToken();
    let server = '';
    try {
      const r = await fetch('/api/auth/session-debug', {
        credentials: 'include',
        headers: authHeaders()
      });
      const d = await r.json();
      server = `server sees: ${d.presented}, valid=${d.token_valid}`;
    } catch {
      server = 'session-debug unreachable';
    }
    debug = `token stored: ${stored} · ${server}`;
  });

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    error = '';
    try {
      $currentUser = await api.login(username, password);
      await goto('/');
    } catch (err) {
      error = err instanceof Error ? err.message : 'Login failed';
    }
  }
</script>

<main class="center">
  <form class="card auth" onsubmit={submit}>
    <h1>NewsGator — Sign in</h1>
    {#if error}<p class="error">{error}</p>{/if}
    <label>Username <input bind:value={username} required autocomplete="username" /></label>
    <label>
      Password
      <input type="password" bind:value={password} required autocomplete="current-password" />
    </label>
    <button type="submit">Sign in</button>
    {#if debug}<p class="debug">{debug}</p>{/if}
  </form>
</main>

<style>
  .center { display: flex; justify-content: center; padding-top: 6rem; }
  .auth { width: 320px; display: flex; flex-direction: column; gap: 0.6rem; }
  .error { color: #c00; }
  .debug { color: #888; font-size: 0.75em; margin: 0.3rem 0 0; }
  button { padding: 0.5rem; }
</style>
