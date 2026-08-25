<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import type { Category, StoryListItem } from '$lib/types';

  let stories = $state<StoryListItem[]>([]);
  let categories = $state<Category[]>([]);
  let filter = $state<'all' | 'unread' | 'updated'>('all');
  let category = $state('');
  let sort = $state<'updated' | 'published' | 'sources'>('updated');
  let loading = $state(true);

  onMount(async () => {
    try {
      categories = await api.categories.list();
    } catch {
      /* non-admin users may not list categories */
    }
    await load();
  });

  async function load() {
    loading = true;
    stories = await api.stories.list(filter, category || undefined, sort);
    loading = false;
  }

  function ago(iso: string): string {
    const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 60) return `${mins}m ago`;
    const h = Math.floor(mins / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  }
</script>

<h1>Stories</h1>

<div class="toolbar card">
  <div class="filters">
    {#each ['all', 'unread', 'updated'] as f}
      <button
        class:active={filter === f}
        onclick={() => { filter = f as typeof filter; load(); }}
      >
        {f === 'all' ? 'All' : f === 'unread' ? 'Unread' : 'Updated'}
      </button>
    {/each}
  </div>
  {#if categories.length}
    <select bind:value={category} onchange={load}>
      <option value="">All categories</option>
      {#each categories as c (c.id)}<option value={c.name}>{c.name}</option>{/each}
    </select>
  {/if}
  <select bind:value={sort} onchange={load}>
    <option value="updated">Recently processed</option>
    <option value="published">Article date</option>
    <option value="sources">Most sources</option>
  </select>
</div>

{#if loading}
  <div class="card"><p>Loading…</p></div>
{:else}
  {#each stories as story (story.id)}
    <a class="card story" class:read={story.is_read} href="/stories/{story.id}">
      <div class="row">
        <span class="chip">{story.category}</span>
        {#if !story.is_read}<span class="badge new">NEW</span>{/if}
        {#if story.updated_since_read}<span class="badge updated">UPDATED</span>{/if}
        {#if story.is_frozen}<span class="badge frozen">archived</span>{/if}
        <span class="spacer"></span>
        <span class="age">{ago(story.published_at ?? story.last_updated_at)}</span>
      </div>
      <div class="body">
        {#if story.image_url}
          <img class="thumb" src={story.image_url} alt="" loading="lazy" />
        {/if}
        <div>
          <h2>{story.title}</h2>
          <p class="summary">{story.summary}</p>
          <div class="meta">
            <span>{story.source_count} source{story.source_count === 1 ? '' : 's'}</span>
            <span>v{story.version}</span>
          </div>
        </div>
      </div>
    </a>
  {:else}
    <div class="card">
      <p>No stories yet. Add feeds on the <a href="/feeds">Feeds page</a> — articles are
      clustered into stories automatically once the pipeline runs.</p>
    </div>
  {/each}
{/if}

<style>
  .toolbar { display: flex; gap: 0.5rem; align-items: center; }
  .filters { display: flex; gap: 0.25rem; flex: 1; }
  .filters button {
    border: 1px solid #d0d3d9; background: #fff; border-radius: 999px;
    padding: 0.25rem 0.9rem;
  }
  .filters button.active { background: #1c1e21; color: #fff; border-color: #1c1e21; }
  .story { display: block; text-decoration: none; color: inherit; }
  .story:hover { border-color: #b9bec6; }
  .story.read { opacity: 0.62; }
  .story h2 { margin: 0.35rem 0; font-size: 1.15rem; }
  .body { display: flex; gap: 0.9rem; align-items: flex-start; }
  .thumb {
    width: 120px; height: 80px; object-fit: cover;
    border-radius: 6px; flex-shrink: 0;
  }
  .summary { margin: 0; color: #444; }
  .row { display: flex; align-items: center; gap: 0.4rem; }
  .chip {
    font-size: 0.75em; background: #e8edf5; color: #294a7a;
    padding: 0.1rem 0.5rem; border-radius: 999px;
  }
  .badge { font-size: 0.72em; padding: 0.1rem 0.5rem; border-radius: 999px; font-weight: 600; }
  .badge.new { background: #e3f2e5; color: #1d6b2a; }
  .badge.updated { background: #fff3d6; color: #8a5a00; }
  .badge.frozen { background: #eee; color: #777; }
  .spacer { flex: 1; }
  .age { color: #888; font-size: 0.85em; }
  .meta { display: flex; gap: 1rem; color: #888; font-size: 0.85em; margin-top: 0.5rem; }
</style>
