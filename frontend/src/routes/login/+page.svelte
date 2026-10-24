<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { api, authHeaders, getToken } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import { t, tNow, setLocale } from '$lib/i18n';

  let username = $state('');
  let password = $state('');
  let error = $state('');
  let debug = $state('');

  // Temporary PWA session diagnostic + auto-skip: if a valid stored token
  // survived the restart, go straight to the app instead of asking for a
  // password again (iOS PWAs can relaunch on the stale /login route).
  onMount(async () => {
    const stored = !!getToken();
    let server = '';
    let valid = false;
    try {
      const r = await fetch('/api/auth/session-debug', {
        credentials: 'include',
        headers: authHeaders()
      });
      const d = await r.json();
      valid = !!d.token_valid;
      server = `server sees: ${d.presented}, valid=${d.token_valid}`;
    } catch {
      server = 'session-debug unreachable';
    }
    debug = `token stored: ${stored} · ${server}`;
    if (valid) {
      // Token is good — never show the login screen, jump into the app.
      try {
        $currentUser = await api.me();
      } catch {
        /* fall through to login form */
      }
      if ($currentUser) {
        await goto('/');
        return;
      }
    }
  });

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    error = '';
    try {
      const user = await api.login(username, password);
      $currentUser = user;
      await setLocale(user.ui_language);
      await goto('/');
    } catch (err) {
      error = err instanceof Error ? err.message : tNow('auth.loginFailed');
    }
  }
</script>

<main class="center">
  <form class="card auth" onsubmit={submit}>
    <h1>{$t('auth.signInTitle')}</h1>
    {#if error}<p class="error">{error}</p>{/if}
    <label>{$t('auth.username')} <input bind:value={username} required autocomplete="username" /></label>
    <label>
      {$t('auth.password')}
      <input type="password" bind:value={password} required autocomplete="current-password" />
    </label>
    <button type="submit">{$t('auth.signIn')}</button>
    {#if debug}<p class="debug">{debug}</p>{/if}
  </form>
</main>

<style>
  .center { display: flex; justify-content: center; padding-top: 6rem; }
  .auth { width: 320px; display: flex; flex-direction: column; gap: 0.6rem; }
  .error { color: var(--error); }
  .debug { color: var(--muted); font-size: 0.75em; margin: 0.3rem 0 0; }
  button { padding: 0.5rem; }
</style>
