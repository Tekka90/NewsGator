<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import type { Category } from '$lib/types';

  let language = $state('');
  let saved = $state(false);
  let categories = $state<Category[]>([]);
  let newCategory = $state('');

  onMount(async () => {
    language = $currentUser?.summary_language ?? '';
    if ($currentUser?.is_admin) {
      categories = await api.categories.list();
    }
  });

  async function saveLanguage() {
    $currentUser = await api.me(); // refresh after patch below
    const updated = await fetch('/api/auth/me', {
      method: 'PATCH',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ summary_language: language })
    }).then((r) => r.json());
    $currentUser = updated;
    saved = true;
    setTimeout(() => (saved = false), 2000);
  }

  async function addCategory(e: SubmitEvent) {
    e.preventDefault();
    await api.categories.create(newCategory);
    newCategory = '';
    categories = await api.categories.list();
  }

  async function removeCategory(c: Category) {
    if (!confirm(`Delete category "${c.name}"?`)) return;
    await api.categories.remove(c.id);
    categories = await api.categories.list();
  }
</script>

<h1>Settings</h1>

<div class="card">
  <h2>Your preferences</h2>
  <label>
    Summary language (ISO code, e.g. <code>en</code>, <code>fr</code>, <code>de</code>;
    empty = global default)
    <input bind:value={language} maxlength="8" placeholder="en" />
  </label>
  <button onclick={saveLanguage}>Save</button>
  {#if saved}<span class="ok">Saved ✓</span>{/if}
</div>

{#if $currentUser?.is_admin}
  <div class="card">
    <h2>Categories (admin)</h2>
    <form class="add" onsubmit={addCategory}>
      <input bind:value={newCategory} placeholder="New category" required />
      <button type="submit">Add</button>
    </form>
    <ul>
      {#each categories as c (c.id)}
        <li>
          {c.name}
          {#if c.name !== 'Uncategorized'}
            <button class="link" onclick={() => removeCategory(c)}>delete</button>
          {/if}
        </li>
      {/each}
    </ul>
  </div>

  <div class="card">
    <h2>System (admin)</h2>
    <p>
      LLM endpoints, clustering thresholds, freeze window, retention days and vector
      backend will be configurable here in Milestones 3–7.
    </p>
  </div>
{/if}

<style>
  h2 { margin-top: 0; font-size: 1.05rem; }
  .ok { color: #1d6b2a; margin-left: 0.5rem; }
  .add { display: flex; gap: 0.5rem; }
  .link {
    background: none;
    border: none;
    color: #a12727;
    text-decoration: underline;
    padding: 0;
  }
  code { background: #eee; padding: 0 0.25rem; border-radius: 4px; }
</style>
