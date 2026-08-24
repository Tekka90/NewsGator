<script lang="ts">
  import { goto } from '$app/navigation';
  import { api } from '$lib/api';
  import { currentUser } from '$lib/stores';

  let username = $state('');
  let password = $state('');
  let error = $state('');

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
  </form>
</main>

<style>
  .center { display: flex; justify-content: center; padding-top: 6rem; }
  .auth { width: 320px; display: flex; flex-direction: column; gap: 0.6rem; }
  .error { color: #c00; }
  button { padding: 0.5rem; }
</style>
