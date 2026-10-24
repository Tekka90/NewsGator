<script lang="ts">
  import { goto } from '$app/navigation';
  import { api } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import { t, tNow, setLocale } from '$lib/i18n';

  let username = $state('');
  let password = $state('');
  let error = $state('');

  async function submit(e: SubmitEvent) {
    e.preventDefault();
    error = '';
    try {
      const user = await api.setup(username, password);
      $currentUser = user;
      await setLocale(user.ui_language);
      await goto('/');
    } catch (err) {
      error = err instanceof Error ? err.message : tNow('auth.setupFailed');
    }
  }
</script>

<main class="center">
  <form class="card auth" onsubmit={submit}>
    <h1>{$t('auth.welcome')}</h1>
    <p>{$t('auth.createAdminIntro')}</p>
    {#if error}<p class="error">{error}</p>{/if}
    <label>{$t('auth.username')} <input bind:value={username} required minlength="3" /></label>
    <label>
      {$t('auth.password')}
      <input type="password" bind:value={password} required minlength="8" autocomplete="new-password" />
    </label>
    <button type="submit">{$t('auth.createAdmin')}</button>
  </form>
</main>

<style>
  .center { display: flex; justify-content: center; padding-top: 6rem; }
  .auth { width: 340px; display: flex; flex-direction: column; gap: 0.6rem; }
  .error { color: var(--error); }
  button { padding: 0.5rem; }
</style>
