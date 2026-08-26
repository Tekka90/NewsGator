<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { api, faviconUrl } from '$lib/api';
  import type { StoryDetail, StoryListItem } from '$lib/types';

  let story = $state<StoryDetail | null>(null);
  let allStories = $state<StoryListItem[]>([]);
  let mergeTarget = $state('');
  let moveTargets = $state<Record<number, string>>({});
  let error = $state('');
  let reprocessing = $state<number | null>(null);
  let reprocessMsg = $state<Record<number, string>>({});

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

  async function reprocessArticle(articleId: number) {
    reprocessing = articleId;
    delete reprocessMsg[articleId];
    try {
      const r = await api.stories.reprocessArticle(articleId);
      reprocessMsg[articleId] =
        r.content_status === 'full'
          ? `✓ full text fetched (${r.chars} chars)${r.requeued ? ' — re-summarizing' : ''}`
          : `still partial: ${r.content_warning ?? 'unknown reason'}`;
      await load();
    } catch (e) {
      reprocessMsg[articleId] = e instanceof Error ? e.message : 'Reprocess failed';
    } finally {
      reprocessing = null;
    }
  }

  /** Compact timestamp — no seconds; wraps better on narrow screens. */
  function fmt(iso: string) {
    return new Date(iso).toLocaleString(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short'
    });
  }

  /** Source logo via our own cached favicon proxy (never a third-party service). */
  function favicon(url: string): string {
    try {
      return faviconUrl(new URL(url).hostname ?? '');
    } catch {
      return '';
    }
  }

  function hideFav(e: Event) {
    (e.currentTarget as HTMLImageElement).style.visibility = 'hidden';
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
    {#if story.image_url}
      <img class="lead" src={story.image_url} alt="" loading="lazy" />
    {/if}
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
          <img class="favicon" src={favicon(article.url)} alt="" loading="lazy" onerror={hideFav} />
          <span class="srcname">{article.feed_title || 'Unknown source'}</span>
          {#if article.published_at}<span class="age">{fmt(article.published_at)}</span>{/if}
          <span class="lang">{article.language || '?'}</span>
          {#if article.content_status === 'partial'}
            <span class="badge partial" title={article.content_warning ?? ''}>partial</span>
          {/if}
        </div>
        <a class="link" href={article.url} target="_blank" rel="noopener noreferrer">{article.title}</a>
        {#if article.summary}<p class="small">{article.summary}</p>{/if}
        {#if article.content_warning}<p class="warn small">⚠ {article.content_warning}</p>{/if}
        <div class="row small">
          <button onclick={() => reprocessArticle(article.id)} disabled={reprocessing === article.id}>
            {reprocessing === article.id ? 'Reprocessing…' : 'Reprocess'}
          </button>
          {#if reprocessMsg[article.id]}<span class="small">{reprocessMsg[article.id]}</span>{/if}
          <span class="spacer"></span>
          <span>move to:</span>
          <select class="target" bind:value={moveTargets[article.id]}>
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
      <select class="target" bind:value={mergeTarget}>
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
  .row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .spacer { flex: 1; }
  h1 { font-size: 1.5rem; margin: 0.5rem 0; overflow-wrap: anywhere; }
  h2 { font-size: 1.05rem; margin-top: 0; }
  .lead {
    width: 100%; max-height: 320px; object-fit: cover;
    border-radius: 8px; margin-bottom: 0.5rem;
  }
  .summary { font-size: 1.05rem; overflow-wrap: anywhere; }
  .meta { display: flex; gap: 0.4rem 1rem; color: var(--muted); font-size: 0.85em; flex-wrap: wrap; }
  .chip {
    font-size: 0.75em; background: var(--chip-bg); color: var(--accent);
    padding: 0.1rem 0.5rem; border-radius: 999px;
  }
  .badge { font-size: 0.72em; padding: 0.1rem 0.5rem; border-radius: 999px; font-weight: 600; }
  .badge.updated { background: var(--warn-bg); color: var(--warn); }
  .badge.frozen { background: var(--frozen-bg); color: var(--frozen-text); }
  .badge.partial { background: var(--error-bg); color: var(--error); }
  .source { border-top: 1px solid var(--border); padding: 0.7rem 0; }
  .source:first-of-type { border-top: none; }
  .favicon { width: 16px; height: 16px; border-radius: 3px; flex-shrink: 0; }
  .srcname { font-weight: 600; font-size: 0.9em; }
  .lang { color: var(--faint); font-size: 0.8em; text-transform: uppercase; }
  .small { font-size: 0.88em; color: var(--text-secondary); }
  .warn { color: var(--error); }
  .age { color: var(--muted); font-size: 0.85em; margin-left: 0.4rem; }
  .revision p { margin: 0.3rem 0 0.8rem; color: var(--text-secondary); }
  .link { overflow-wrap: anywhere; }
  /* long story titles must never push the card wider than the screen */
  .target { max-width: 100%; min-width: 0; flex: 1 1 14rem; }

  @media (max-width: 700px) {
    h1 { font-size: 1.25rem; }
    .lead { max-height: 40vh; }
    .summary { font-size: 1rem; }
    .row { gap: 0.4rem; }
    .spacer { flex-basis: 100%; } /* actions start on their own line */
  }
</style>
