<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { api, faviconUrl } from '$lib/api';
  import ReadeckIcon from '$lib/components/ReadeckIcon.svelte';
  import ShareButton from '$lib/components/ShareButton.svelte';
  import StoryPicker from '$lib/components/StoryPicker.svelte';
  import type { SimilarStory, StoryDetail } from '$lib/types';

  let story = $state<StoryDetail | null>(null);
  let mergeSel = $state<SimilarStory | null>(null);
  let moveSels = $state<Record<number, SimilarStory | null>>({});
  let error = $state('');
  let reprocessing = $state<number | null>(null);
  let reprocessMsg = $state<Record<number, string>>({});
  let readeckEnabled = $state(false);
  let savingReadeck = $state(false);
  let readeckMsg = $state('');

  const id = Number(page.params.id);

  onMount(async () => {
    await load();
    // Optional feature — the endpoint 404s when Readeck isn't configured.
    try {
      const s = await api.settings.get();
      readeckEnabled = Boolean(s.values.readeck_base_url && s.values.readeck_token);
    } catch {
      readeckEnabled = false; // non-admin or settings unavailable
    }
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
    if (!mergeSel) return;
    error = '';
    try {
      await api.stories.merge(id, mergeSel.id);
      mergeSel = null;
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Merge failed';
    }
  }

  async function moveArticle(articleId: number) {
    const target = moveSels[articleId];
    if (!target) return;
    error = '';
    try {
      await api.stories.moveArticle(articleId, target.id);
      moveSels[articleId] = null;
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

  async function saveReadeck() {
    savingReadeck = true;
    readeckMsg = '';
    try {
      const r = await api.stories.saveToReadeck(id);
      readeckMsg = `✓ saved to Readeck (${r.latency_ms} ms)`;
      if (story) story.readeck_bookmark_id = r.bookmark_id; // grey the button
    } catch (e) {
      readeckMsg = e instanceof Error ? e.message : 'Save failed';
    } finally {
      savingReadeck = false;
    }
  }
</script>

{#if story}
  <div class="row">
    <a href="/">← back</a>
    <span class="spacer"></span>
    {#if readeckEnabled}
      {@const saved = Boolean(story.readeck_bookmark_id)}
      <button
        class="iconbtn"
        class:saved
        onclick={saveReadeck}
        disabled={savingReadeck}
        aria-label="Save to Readeck"
        title={saved ? 'Already saved to Readeck — save again' : 'Save to Readeck'}
      >
        {#if savingReadeck}Saving…{:else}<ReadeckIcon size={15} /> Save to Readeck{/if}
      </button>
    {/if}
    <ShareButton storyId={id} />
    <button onclick={toggleRead}>{story.is_read ? 'Mark unread' : 'Mark read'}</button>
  </div>
  {#if readeckMsg}<p class="ok">{readeckMsg}</p>{/if}

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
          <StoryPicker
            load={() => api.stories.similarForArticle(article.id)}
            placeholder="Filter stories…"
            bind:selected={moveSels[article.id]}
          />
          <button onclick={() => moveArticle(article.id)} disabled={!moveSels[article.id]}>
            Move
          </button>
        </div>
      </div>
    {/each}
  </div>

  <div class="card">
    <h2>Merge another story into this one</h2>
    {#if error}<p class="warn">{error}</p>{/if}
    <div class="row">
      <StoryPicker
        load={() => api.stories.similar(id)}
        placeholder="Filter stories…"
        bind:selected={mergeSel}
      />
      <button onclick={merge} disabled={!mergeSel}>Merge</button>
    </div>
  </div>
{:else}
  <div class="card"><p>Loading…</p></div>
{/if}

<style>
  .row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .spacer { flex: 1; }
  .iconbtn { display: inline-flex; align-items: center; gap: 0.35rem; }
  .iconbtn.saved { color: var(--disabled-text); border-color: var(--disabled-bg); opacity: 0.75; }
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

  @media (max-width: 700px) {
    h1 { font-size: 1.25rem; }
    .lead { max-height: 40vh; }
    .summary { font-size: 1rem; }
    .row { gap: 0.4rem; }
    .spacer { flex-basis: 100%; } /* actions start on their own line */
  }
</style>
