<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { api, faviconUrl, imageSrc } from '$lib/api';
  import ReadeckIcon from '$lib/components/ReadeckIcon.svelte';
  import ShareButton from '$lib/components/ShareButton.svelte';
  import VideoPlayer from '$lib/components/VideoPlayer.svelte';
  import { formatDuration, providerLabel } from '$lib/video';
  import StoryPicker from '$lib/components/StoryPicker.svelte';
  import type { SimilarStory, StoryDetail } from '$lib/types';
  import { t, fmt, tNow } from '$lib/i18n';

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

  async function toggleSaved() {
    if (!story) return;
    const next = !story.saved;
    const saved = await api.stories.setSaved(id, next);
    story.saved = saved.saved;
    story.saved_at = saved.saved_at;
  }

  async function merge() {
    if (!mergeSel) return;
    error = '';
    try {
      await api.stories.merge(id, mergeSel.id);
      mergeSel = null;
      await load();
    } catch (e) {
      error = e instanceof Error ? e.message : tNow('story.mergeFailed');
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
      error = e instanceof Error ? e.message : tNow('story.moveFailed');
    }
  }

  async function reprocessArticle(articleId: number) {
    reprocessing = articleId;
    delete reprocessMsg[articleId];
    try {
      const r = await api.stories.reprocessArticle(articleId);
      reprocessMsg[articleId] =
        r.content_status === 'full'
          ? tNow(r.requeued ? 'story.fullFetchedResummarizing' : 'story.fullFetched', { chars: r.chars })
          : tNow('story.stillPartial', {
              reason: r.content_warning ?? tNow('story.unknownReason')
            });
      await load();
    } catch (e) {
      reprocessMsg[articleId] = e instanceof Error ? e.message : tNow('story.reprocessFailed');
    } finally {
      reprocessing = null;
    }
  }

  /** Compact timestamp — no seconds; wraps better on narrow screens. */
  const dtOpts: Intl.DateTimeFormatOptions = { dateStyle: 'medium', timeStyle: 'short' };

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
      readeckMsg = tNow('story.readeckSaved', { ms: r.latency_ms });
      if (story) story.readeck_bookmark_id = r.bookmark_id; // grey the button
    } catch (e) {
      readeckMsg = e instanceof Error ? e.message : tNow('story.saveFailed');
    } finally {
      savingReadeck = false;
    }
  }
</script>

{#if story}
  <div class="row">
    <a href="/">{$t('story.back')}</a>
    <span class="spacer"></span>
    <button
      class="savebtn"
      class:active={story.saved}
      onclick={toggleSaved}
      aria-label={story.saved ? $t('story.removeSaved') : $t('story.saveStory')}
      title={story.saved ? $t('story.savedClickRemove') : $t('story.saveStory')}
    >
      <span class="star" aria-hidden="true">★</span>
      {story.saved ? $t('story.saved') : $t('story.save')}
    </button>
    {#if readeckEnabled}
      {@const saved = Boolean(story.readeck_bookmark_id)}
      <button
        class="iconbtn"
        class:saved
        onclick={saveReadeck}
        disabled={savingReadeck}
        aria-label={$t('story.saveToReadeck')}
        title={saved ? $t('story.readeckAlready') : $t('story.saveToReadeck')}
      >
        {#if savingReadeck}{$t('story.saving')}{:else}<ReadeckIcon size={15} /> {$t('story.saveToReadeck')}{/if}
      </button>
    {/if}
    <ShareButton storyId={id} />
    <button onclick={toggleRead}>{story.is_read ? $t('story.markUnread') : $t('story.markRead')}</button>
  </div>
  {#if readeckMsg}<p class="ok">{readeckMsg}</p>{/if}

  <div class="card">
    <div class="row">
      <span class="chip">{story.category}</span>
      {#if story.updated_since_read}<span class="badge updated">{$t('story.updatedSinceRead')}</span>{/if}
      {#if story.media_kind === 'video'}<span class="badge video">▶ {providerLabel(story.video_provider)}{#if story.video_duration} · {formatDuration(story.video_duration)}{/if}</span>{/if}
      {#if story.is_frozen}<span class="badge frozen">{$t('story.archived')}</span>{/if}
    </div>
    <h1>{story.title}</h1>
    {#if story.media_kind === 'video'}
      <div class="videowrap"><VideoPlayer videoId={story.video_id} provider={story.video_provider} url={story.video_url} embedUrl={story.video_embed_url} posterUrl={story.image_url} title={story.title} /></div>
    {:else if story.image_url}
      <img class="lead" src={imageSrc(story.image_url)} alt="" loading="lazy" />
    {/if}
    {#if story.summary}
      <details class="summary-details">
        <summary>{$t('story.summary')}</summary>
        <p class="summary">{story.summary}</p>
      </details>
    {/if}
    <div class="meta">
      <span>{$t('story.firstSeen', { date: $fmt.dateTime(story.first_seen_at, dtOpts) })}</span>
      <span>{$t('story.updatedAt', { date: $fmt.dateTime(story.last_updated_at, dtOpts) })}</span>
      <span>{$t('story.version', { version: story.version })}</span>
    </div>
  </div>

  {#if story.revisions.length > 1}
    <details class="card">
      <summary>{$t('story.whatChanged', { count: story.revisions.length })}</summary>
      {#each [...story.revisions].reverse() as rev (rev.version)}
        <div class="revision">
          <strong>v{rev.version}</strong> <span class="age">{$fmt.dateTime(rev.created_at, dtOpts)}</span>
          <p>{rev.summary}</p>
        </div>
      {/each}
    </details>
  {/if}

  <div class="card">
    <h2>{$t('story.sources', { count: story.articles.length })}</h2>
    {#each story.articles as article (article.id)}
      <div class="source">
        <div class="row">
          <img class="favicon" src={favicon(article.url)} alt="" loading="lazy" onerror={hideFav} />
          <span class="srcname" title={article.origin_feed_title && article.feed_title ? `${$t('story.via', { feed: article.feed_title })}` : ''}>
            {article.origin_feed_title || article.feed_title || $t('story.unknownSource')}
          </span>
          {#if article.published_at}<span class="age">{$fmt.dateTime(article.published_at, dtOpts)}</span>{/if}
          <span class="lang">{article.language || '?'}</span>
          {#if article.content_status === 'partial'}
            <span class="badge partial" title={article.content_warning ?? ''}>{$t('story.partial')}</span>
          {/if}
        </div>
        <a class="link" href={article.url} target="_blank" rel="noopener noreferrer">{article.title}</a>
        {#if article.summary}<p class="small">{article.summary}</p>{/if}
        {#if article.content_warning}<p class="warn small">⚠ {article.content_warning}</p>{/if}
        <div class="row small">
          <button onclick={() => reprocessArticle(article.id)} disabled={reprocessing === article.id}>
            {reprocessing === article.id ? $t('story.reprocessing') : $t('story.reprocess')}
          </button>
          {#if reprocessMsg[article.id]}<span class="small">{reprocessMsg[article.id]}</span>{/if}
          <span class="spacer"></span>
          <span>{$t('story.moveTo')}</span>
          <StoryPicker
            load={() => api.stories.similarForArticle(article.id)}
            placeholder={$t('story.filterStories')}
            bind:selected={moveSels[article.id]}
          />
          <button onclick={() => moveArticle(article.id)} disabled={!moveSels[article.id]}>
            {$t('story.move')}
          </button>
        </div>
      </div>
    {/each}
  </div>

  <div class="card">
    <h2>{$t('story.mergeTitle')}</h2>
    {#if error}<p class="warn">{error}</p>{/if}
    <div class="row">
      <StoryPicker
        load={() => api.stories.similar(id)}
        placeholder={$t('story.filterStories')}
        bind:selected={mergeSel}
      />
      <button onclick={merge} disabled={!mergeSel}>{$t('story.merge')}</button>
    </div>
  </div>
{:else}
  <div class="card"><p>{$t('story.loading')}</p></div>
{/if}

<style>
  .videowrap { margin: 0.6rem 0; }
  .row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .spacer { flex: 1; }
  .iconbtn { display: inline-flex; align-items: center; gap: 0.35rem; }
  .iconbtn.saved { color: var(--disabled-text); border-color: var(--disabled-bg); opacity: 0.75; }
  .savebtn {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .savebtn.active {
    color: var(--accent-signal);
    border-color: var(--accent-signal);
  }
  .savebtn .star {
    font-size: 1rem;
    line-height: 1;
  }
  h1 { font-size: 1.5rem; margin: 0.5rem 0; overflow-wrap: anywhere; }
  h2 { font-size: 1.05rem; margin-top: 0; }
  .lead {
    width: 100%; max-height: 320px; object-fit: cover;
    border-radius: 8px; margin-bottom: 0.5rem;
  }
  .summary-details { margin: 0.5rem 0; }
  .summary-details summary {
    cursor: pointer;
    font-weight: 500;
    color: var(--text-secondary);
    user-select: none;
  }
  .summary-details summary:hover {
    color: var(--accent);
  }
  .summary-details p.summary { margin-top: 0.5rem; }
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
