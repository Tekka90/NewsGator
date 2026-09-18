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

  // Tabbed browser state: 0 = Story Details, 1..N = Source tabs
  let selectedTabIndex = $state(0);
  let loadedTabIndices = $state<number[]>([]);

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
    selectedTabIndex = 0;
    loadedTabIndices = [];
  }

  function selectTab(index: number) {
    selectedTabIndex = index;
    if (index > 0 && !loadedTabIndices.includes(index)) {
      loadedTabIndices.push(index);
    }
  }

  function sourceTitle(article: { feed_title?: string; title: string }) {
    if (article.feed_title && article.feed_title.trim().length > 0) {
      return article.feed_title;
    }
    if (article.title && article.title.trim().length > 0) {
      return article.title;
    }
    return 'Source';
  }

  function articleHost(url: string) {
    try {
      return new URL(url).hostname;
    } catch {
      return url;
    }
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
  <!-- Horizontal Tab Bar matching macOS reference pattern -->
  <div class="tabs-header">
    <div class="tabs-scroll">
      <button
        class="tab-btn"
        class:active={selectedTabIndex === 0}
        onclick={() => selectTab(0)}
        title="Story overview, metadata, revisions and actions"
      >
        <svg class="tab-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
          <polyline points="14 2 14 8 20 8"></polyline>
          <line x1="16" y1="13" x2="8" y2="13"></line>
          <line x1="16" y1="17" x2="8" y2="17"></line>
        </svg>
        <span>Story Details</span>
      </button>

      {#each story.articles as article, index (article.id)}
        {@const tabIdx = index + 1}
        <button
          class="tab-btn"
          class:active={selectedTabIndex === tabIdx}
          onclick={() => selectTab(tabIdx)}
          title={article.title}
        >
          {#if article.url}
            <img class="favicon" src={favicon(article.url)} alt="" loading="lazy" onerror={hideFav} />
          {:else}
            <span class="globe-icon">🌐</span>
          {/if}
          <span class="tab-title">{sourceTitle(article)}</span>
        </button>
      {/each}
    </div>

    {#if story.articles.length > 0}
      <span class="tab-sources-count">
        {story.articles.length} source{story.articles.length === 1 ? '' : 's'}
      </span>
    {/if}
  </div>

  <!-- Tab 0: Story Details (Preserves all existing content & functionality) -->
  <div class="tab-pane" class:hidden-pane={selectedTabIndex !== 0}>
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
      {#each story.articles as article, index (article.id)}
        <div class="source">
          <div class="row">
            <img class="favicon" src={favicon(article.url)} alt="" loading="lazy" onerror={hideFav} />
            <span class="srcname">{article.feed_title || 'Unknown source'}</span>
            {#if article.published_at}<span class="age">{fmt(article.published_at)}</span>{/if}
            <span class="lang">{article.language || '?'}</span>
            {#if article.content_status === 'partial'}
              <span class="badge partial" title={article.content_warning ?? ''}>partial</span>
            {/if}
            <span class="spacer"></span>
            <button class="view-tab-btn" onclick={() => selectTab(index + 1)} title="View embedded source web page">
              View in tab →
            </button>
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
  </div>

  <!-- Tabs 1..N: One tab per source (Live embedded web page in-app) -->
  {#each story.articles as article, index (article.id)}
    {@const tabIdx = index + 1}
    {#if loadedTabIndices.includes(tabIdx)}
      <div class="tab-pane source-browser-pane" class:hidden-pane={selectedTabIndex !== tabIdx}>
        <div class="card source-browser-card">
          <div class="source-nav-bar">
            <div class="nav-left">
              <button class="nav-back-btn" onclick={() => selectTab(0)} title="Return to Story Details">
                ← Details
              </button>
              <img class="favicon" src={favicon(article.url)} alt="" loading="lazy" onerror={hideFav} />
              <span class="source-feed-name">{sourceTitle(article)}</span>
            </div>
            <div class="nav-center">
              <span class="source-host-pill" title={article.url}>{articleHost(article.url)}</span>
            </div>
            <div class="nav-right">
              <a
                class="open-browser-btn"
                href={article.url}
                target="_blank"
                rel="noopener noreferrer"
                title="Open original article in new browser tab"
              >
                Open in browser ↗
              </a>
            </div>
          </div>

          <div class="source-article-headline">
            <a class="link" href={article.url} target="_blank" rel="noopener noreferrer">{article.title}</a>
          </div>

          <div class="iframe-container">
            {#if article.url}
              <iframe
                src={article.url}
                title={article.title}
                class="source-iframe"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                loading="lazy"
              ></iframe>
            {:else}
              <div class="source-unavailable">
                <p>Source link unavailable</p>
              </div>
            {/if}
          </div>

          <div class="source-hint-row">
            <span>Note: If a publisher restricts in-frame viewing, use <strong>Open in browser ↗</strong>.</span>
          </div>
        </div>
      </div>
    {/if}
  {/each}
{:else}
  <div class="card"><p>Loading…</p></div>
{/if}

<style>
  /* Tab Bar matching macOS reference pattern */
  .tabs-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.2rem;
    margin-bottom: 0.75rem;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: var(--nav-h, 0px);
    background: var(--bg);
    z-index: 15;
  }
  .tabs-scroll {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    overflow-x: auto;
    scrollbar-width: thin;
    -webkit-overflow-scrolling: touch;
    flex: 1;
    min-width: 0;
    padding: 0.15rem 0;
  }
  .tabs-scroll::-webkit-scrollbar {
    height: 4px;
  }
  .tabs-scroll::-webkit-scrollbar-thumb {
    background: var(--border-strong);
    border-radius: 2px;
  }
  .tab-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.35rem 0.7rem;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text-secondary);
    font-size: 0.82rem;
    font-weight: 500;
    white-space: nowrap;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s, color 0.15s;
    flex-shrink: 0;
  }
  .tab-btn:hover {
    border-color: var(--border-hover);
    color: var(--text);
  }
  .tab-btn.active {
    background: var(--chip-bg);
    border-color: var(--accent);
    color: var(--accent);
    font-weight: 600;
  }
  .tab-icon {
    flex-shrink: 0;
  }
  .tab-title {
    max-width: 160px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .tab-sources-count {
    font-size: 0.75rem;
    color: var(--muted);
    white-space: nowrap;
    padding-left: 0.5rem;
    flex-shrink: 0;
  }
  .globe-icon {
    font-size: 0.85rem;
    line-height: 1;
  }

  /* Panes */
  .tab-pane {
    display: block;
  }
  .hidden-pane {
    display: none !important;
  }

  /* Embedded Source Browser */
  .source-browser-card {
    display: flex;
    flex-direction: column;
    padding: 0.75rem;
  }
  .source-nav-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
  }
  .nav-left, .nav-right {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .nav-center {
    display: flex;
    align-items: center;
    max-width: 50%;
    overflow: hidden;
  }
  .nav-back-btn {
    padding: 0.25rem 0.55rem;
    font-size: 0.8rem;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    cursor: pointer;
  }
  .nav-back-btn:hover {
    background: var(--chip-bg);
    border-color: var(--accent);
  }
  .source-feed-name {
    font-weight: 600;
    font-size: 0.85rem;
    color: var(--text);
  }
  .source-host-pill {
    font-size: 0.75rem;
    color: var(--muted);
    background: var(--chip-bg);
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .open-browser-btn {
    font-size: 0.8rem;
    color: var(--accent);
    text-decoration: none;
    padding: 0.25rem 0.55rem;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--surface);
    white-space: nowrap;
    transition: background 0.15s, border-color 0.15s;
  }
  .open-browser-btn:hover {
    background: var(--chip-bg);
    border-color: var(--accent);
  }
  .source-article-headline {
    padding: 0.5rem 0;
    font-size: 0.95rem;
    font-weight: 600;
  }
  .iframe-container {
    width: 100%;
    height: 72vh;
    min-height: 480px;
    border-radius: 6px;
    overflow: hidden;
    background: #ffffff;
    border: 1px solid var(--border);
    position: relative;
  }
  .source-iframe {
    width: 100%;
    height: 100%;
    border: none;
    display: block;
    background: #ffffff;
  }
  .source-unavailable {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--muted);
    font-size: 0.9rem;
  }
  .source-hint-row {
    font-size: 0.75rem;
    color: var(--muted);
    padding-top: 0.5rem;
    display: flex;
    justify-content: flex-end;
  }
  .view-tab-btn {
    font-size: 0.78rem;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--accent);
    cursor: pointer;
    transition: background 0.15s;
  }
  .view-tab-btn:hover {
    background: var(--chip-bg);
  }

  /* Existing Styles */
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
    .iframe-container { height: 60vh; min-height: 380px; }
    .nav-center { display: none; }
    .tab-sources-count { display: none; }
  }
</style>

