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
      $currentUser = await api.setup(username, password);
      await goto('/');
    } catch (err) {
      error = err instanceof Error ? err.message : 'Setup failed';
    }
  }
</script>

<main class="center">
  <form class="card auth" onsubmit={submit}>
    <h1>Welcome to NewsGator</h1>
    <p>Create the admin account to get started.</p>
    {#if error}<p class="error">{error}</p>{/if}
    <label>Username <input bind:value={username} required minlength="3" /></label>
    <label>
      Password
      <input type="password" bind:value={password} required minlength="8" autocomplete="new-password" />
    </label>
    <button type="submit">Create admin</button>
  </form>
</main>

<style>
  .center { display: flex; justify-content: center; padding-top: 6rem; }
  .auth { width: 340px; display: flex; flex-direction: column; gap: 0.6rem; }
  .error { color: var(--error); }
  button { padding: 0.5rem; }
</style>
