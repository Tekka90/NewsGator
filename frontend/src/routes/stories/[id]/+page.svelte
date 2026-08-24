<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { api } from '$lib/api';
  import type { StoryDetail, StoryListItem } from '$lib/types';

  let story = $state<StoryDetail | null>(null);
  let allStories = $state<StoryListItem[]>([]);
  let mergeTarget = $state('');
  let moveTargets = $state<Record<number, string>>({});
  let error = $state('');

  const id = Number(page.params.id);

  onMount(async () => {
    await load();
    allStories = await api.stories.list();
  });

  async function load() {
    story = await api.stories.detail(id);
  }

  async function toggleRead() {
    if (!story) return;
    if (story.is_read) await api.stories.unread(id);
    else await api.stories.read(id);
    await load();
  }

  async function merge() {
    if (!mergeTarget) return;
    error = '';
    try {
      await api.stories.merge(id, Number(mergeTarget));
      mergeTarget = '';
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Merge failed';
    }
  }

  async function moveArticle(articleId: number) {
    const target = moveTargets[articleId];
    if (!target) return;
    error = '';
    try {
      await api.stories.moveArticle(articleId, Number(target));
      moveTargets[articleId] = '';
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Move failed';
    }
  }

  function fmt(iso: string) {
    return new Date(iso).toLocaleString();
  }
</script>

{#if story}
  <div class="row">
    <a href="/">← back</a>
    <span class="spacer"></span>
    <button onclick={toggleRead}>{story.is_read ? 'Mark unread' : 'Mark read'}</button>
  </div>

  <div class="card">
    <div class="row">
      <span class="chip">{story.category}</span>
      {#if story.updated_since_read}<span class="badge updated">UPDATED since you read it</span>{/if}
      {#if story.is_frozen}<span class="badge frozen">archived</span>{/if}
    </div>
    <h1>{story.title}</h1>
    <p class="summary">{story.summary}</p>
    <div class="meta">
      <span>first seen {fmt(story.first_seen_at)}</span>
      <span>updated {fmt(story.last_updated_at)}</span>
      <span>version {story.version}</span>
    </div>
  </div>

  {#if story.revisions.length > 1}
    <details class="card">
      <summary>What changed ({story.revisions.length} versions)</summary>
      {#each [...story.revisions].reverse() as rev (rev.version)}
        <div class="revision">
          <strong>v{rev.version}</strong> <span class="age">{fmt(rev.created_at)}</span>
          <p>{rev.summary}</p>
        </div>
      {/each}
    </details>
  {/if}

  <div class="card">
    <h2>Sources ({story.articles.length})</h2>
    {#each story.articles as article (article.id)}
      <div class="source">
        <div class="row">
          <a href={article.url} target="_blank" rel="noopener noreferrer">{article.title}</a>
          <span class="lang">{article.language || '?'}</span>
          {#if article.content_status === 'partial'}
            <span class="badge partial" title={article.content_warning ?? ''}>partial</span>
          {/if}
        </div>
        {#if article.summary}<p class="small">{article.summary}</p>{/if}
        {#if article.content_warning}<p class="warn small">⚠ {article.content_warning}</p>{/if}
        <div class="row small">
          <span>move to:</span>
          <select bind:value={moveTargets[article.id]}>
            <option value="">…</option>
            {#each allStories.filter((s) => s.id !== story!.id) as s (s.id)}
              <option value={s.id}>{s.title}</option>
            {/each}
          </select>
          <button onclick={() => moveArticle(article.id)}>Move</button>
        </div>
      </div>
    {/each}
  </div>

  <div class="card">
    <h2>Merge another story into this one</h2>
    {#if error}<p class="warn">{error}</p>{/if}
    <div class="row">
      <select bind:value={mergeTarget}>
        <option value="">Select story…</option>
        {#each allStories.filter((s) => s.id !== story!.id) as s (s.id)}
          <option value={s.id}>{s.title}</option>
        {/each}
      </select>
      <button onclick={merge} disabled={!mergeTarget}>Merge</button>
    </div>
  </div>
{:else}
  <div class="card"><p>Loading…</p></div>
{/if}

<style>
  .row { display: flex; align-items: center; gap: 0.5rem; }
  .spacer { flex: 1; }
  h1 { font-size: 1.5rem; margin: 0.5rem 0; }
  h2 { font-size: 1.05rem; margin-top: 0; }
  .summary { font-size: 1.05rem; }
  .meta { display: flex; gap: 1rem; color: #888; font-size: 0.85em; }
  .chip {
    font-size: 0.75em; background: #e8edf5; color: #294a7a;
    padding: 0.1rem 0.5rem; border-radius: 999px;
  }
  .badge { font-size: 0.72em; padding: 0.1rem 0.5rem; border-radius: 999px; font-weight: 600; }
  .badge.updated { background: #fff3d6; color: #8a5a00; }
  .badge.frozen { background: #eee; color: #777; }
  .badge.partial { background: #fde7e7; color: #a12727; }
  .source { border-top: 1px solid #eee; padding: 0.7rem 0; }
  .source:first-of-type { border-top: none; }
  .lang { color: #999; font-size: 0.8em; text-transform: uppercase; }
  .small { font-size: 0.88em; color: #555; }
  .warn { color: #a12727; }
  .age { color: #888; font-size: 0.85em; margin-left: 0.4rem; }
  .revision p { margin: 0.3rem 0 0.8rem; color: #444; }
</style>
