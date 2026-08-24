<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import type { Feed } from '$lib/types';

  let feeds = $state<Feed[]>([]);
  let url = $state('');
  let error = $state('');
  let adding = $state(false);
  let importResult = $state('');
  let importing = $state(false);

  onMount(load);

  async function load() {
    feeds = await api.feeds.list();
  }

  async function importOpml(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    importing = true;
    importResult = '';
    try {
      const r = await api.feeds.importOpml(file);
      importResult = `Imported ${r.added} feed${r.added === 1 ? '' : 's'}` +
        (r.skipped_existing ? `, ${r.skipped_existing} already existed` : '') +
        (r.invalid ? `, ${r.invalid} invalid entries` : '');
      await load();
    } catch (err) {
      importResult = err instanceof Error ? err.message : 'Import failed';
    } finally {
      importing = false;
      input.value = '';
    }
  }

  async function add(e: SubmitEvent) {
    e.preventDefault();
    error = '';
    adding = true;
    try {
      await api.feeds.create({ url });
      url = '';
      await load();
    } catch (err) {
      error = err instanceof Error ? err.message : 'Failed to add feed';
    } finally {
      adding = false;
    }
  }

  async function toggle(feed: Feed) {
    await api.feeds.update(feed.id, { is_enabled: !feed.is_enabled });
    await load();
  }

  async function remove(feed: Feed) {
    if (!confirm(`Delete ${feed.title || feed.url}?`)) return;
    await api.feeds.remove(feed.id);
    await load();
  }

  function fmt(d: string | null) {
    return d ? new Date(d).toLocaleString() : 'never';
  }
</script>

<h1>Feeds</h1>

<form class="card add" onsubmit={add}>
  <input placeholder="https://example.com/feed.xml" bind:value={url} required type="url" />
  <button type="submit" disabled={adding}>{adding ? 'Adding…' : 'Add feed'}</button>
  {#if error}<span class="error">{error}</span>{/if}
</form>

<div class="card import">
  <label class="import-label">
    📥 Import OPML (subscription list export from another reader)
    <input type="file" accept=".opml,.xml" onchange={importOpml} disabled={importing} />
  </label>
  {#if importResult}<p class="small">{importResult}</p>{/if}
</div>

{#each feeds as feed (feed.id)}
  <div class="card feed">
    <div class="row">
      <strong>{feed.title || feed.url}</strong>
      <span class="badge" class:off={!feed.is_enabled}>
        {feed.is_enabled ? 'enabled' : 'disabled'}
      </span>
      {#if feed.consecutive_failures > 0}
        <span class="badge warn">{feed.consecutive_failures} failures</span>
      {/if}
      <span class="spacer"></span>
      <button onclick={() => toggle(feed)}>{feed.is_enabled ? 'Disable' : 'Enable'}</button>
      <button class="danger" onclick={() => remove(feed)}>Delete</button>
    </div>
    <div class="meta">
      <span>{feed.url}</span>
      <span>polls every {feed.poll_interval_min} min</span>
      <span>last fetched: {fmt(feed.last_fetched_at)}</span>
    </div>
    {#if feed.last_error}<p class="error small">Last error: {feed.last_error}</p>{/if}
  </div>
{:else}
  <div class="card"><p>No feeds yet — add your first one above.</p></div>
{/each}

<style>
  .add { display: flex; gap: 0.5rem; align-items: center; }
  .add input { flex: 1; }
  .row { display: flex; align-items: center; gap: 0.5rem; }
  .spacer { flex: 1; }
  .badge {
    font-size: 0.75em;
    background: #e3f2e5;
    color: #1d6b2a;
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
  }
  .badge.off { background: #eee; color: #666; }
  .badge.warn { background: #fde7e7; color: #a12727; }
  .meta {
    display: flex;
    gap: 1rem;
    color: #666;
    font-size: 0.85em;
    margin-top: 0.4rem;
    flex-wrap: wrap;
  }
  .error { color: #c00; }
  .small { font-size: 0.85em; margin: 0.4rem 0 0; }
  .danger { color: #a12727; }
  .import-label { display: block; margin: 0; cursor: pointer; }
  .import input[type='file'] { margin-top: 0.4rem; }
</style>
