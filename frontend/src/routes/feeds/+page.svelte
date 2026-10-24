<script lang="ts">
  import { onMount } from 'svelte';
  import { api, faviconUrl, feedHost } from '$lib/api';
  import type { Feed } from '$lib/types';
  import FeedDiscoveryModal from '$lib/components/FeedDiscoveryModal.svelte';
  import { t, fmt, tNow } from '$lib/i18n';

  let feeds = $state<Feed[]>([]);
  let url = $state('');
  let error = $state('');
  let adding = $state(false);
  let importResult = $state('');
  let importing = $state(false);
  let importingSubs = $state(false);
  let subsResult = $state('');
  let exporting = $state(false);
  let exportError = $state('');
  let refreshing = $state(false);
  let refreshingId = $state<number | null>(null);
  // '' = server default, '0' = import everything, else days
  let backfill = $state('');
  let discoverOpen = $state(false);

  onMount(load);

  async function load() {
    feeds = await api.feeds.list();
  }

  function importSummary(added: number, skipped: number, invalid: number): string {
    const parts = [tNow('feeds.imported', { count: added })];
    if (skipped) parts.push(tNow('feeds.import_skipped', { count: skipped }));
    if (invalid) parts.push(tNow('feeds.import_invalid', { count: invalid }));
    return parts.join(tNow('feeds.list_separator'));
  }

  async function importOpml(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    importing = true;
    importResult = '';
    try {
      const r = await api.feeds.importOpml(file);
      importResult = importSummary(r.added, r.skipped_existing, r.invalid);
      await load();
    } catch (err) {
      importResult = err instanceof Error ? err.message : tNow('feeds.import_failed');
    } finally {
      importing = false;
      input.value = '';
    }
  }

  async function importSubs(e: Event) {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    importingSubs = true;
    subsResult = '';
    try {
      const r = await api.feeds.importSubscriptions(file);
      subsResult = importSummary(r.added, r.skipped, r.invalid);
      await load();
    } catch (err) {
      subsResult = err instanceof Error ? err.message : tNow('feeds.import_failed');
    } finally {
      importingSubs = false;
      input.value = '';
    }
  }

  async function exportOpml() {
    exporting = true;
    exportError = '';
    try {
      const blob = await api.feeds.exportOpml();
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = 'newsgator-subscriptions.opml';
      link.click();
      URL.revokeObjectURL(link.href);
    } catch (err) {
      exportError = err instanceof Error ? err.message : tNow('feeds.export_failed');
    } finally {
      exporting = false;
    }
  }

  async function add(e: SubmitEvent) {
    e.preventDefault();
    error = '';
    adding = true;
    try {
      await api.feeds.create({
        url,
        ...(backfill !== '' ? { backfill_days: Number(backfill) } : {})
      });
      url = '';
      backfill = '';
      await load();
    } catch (err) {
      error = err instanceof Error ? err.message : tNow('feeds.add_failed');
    } finally {
      adding = false;
    }
  }

  async function toggle(feed: Feed) {
    await api.feeds.update(feed.id, { is_enabled: !feed.is_enabled });
    await load();
  }

  async function refresh(feed: Feed) {
    refreshingId = feed.id;
    try {
      await api.feeds.refresh(feed.id);
    } finally {
      refreshingId = null;
      await load();
    }
  }

  async function refreshAll() {
    refreshing = true;
    try {
      await api.feeds.refreshAll();
    } finally {
      refreshing = false;
      await load();
    }
  }

  async function remove(feed: Feed) {
    if (!confirm(tNow('feeds.delete_confirm', { name: feed.title || feed.url }))) return;
    error = '';
    try {
      await api.feeds.remove(feed.id);
      await load();
    } catch (err) {
      error = err instanceof Error ? err.message : tNow('feeds.delete_failed');
    }
  }

  function lastFetched(d: string | null) {
    return d ? $fmt.dateTime(d) : tNow('feeds.never');
  }

  /** Broken/missing favicon → drop the img instead of a broken-image glyph. */
  function hideFav(e: Event) {
    (e.currentTarget as HTMLImageElement).remove();
  }
</script>

<h1>{$t('feeds.title')}</h1>

{#if error}<p class="error">{error}</p>{/if}

<form class="card add" onsubmit={add}>
  <input placeholder={$t('feeds.url_placeholder')} bind:value={url} required type="text" />
  <label class="backfill">
    {$t('feeds.initial_import')}
    <select bind:value={backfill}>
      <option value="">{$t('feeds.backfill_default')}</option>
      <option value="0">{$t('feeds.backfill_all')}</option>
      <option value="1">{$t('feeds.backfill_24h')}</option>
      <option value="7">{$t('feeds.backfill_7d')}</option>
      <option value="30">{$t('feeds.backfill_30d')}</option>
      <option value="365">{$t('feeds.backfill_year')}</option>
    </select>
  </label>
  <button type="submit" disabled={adding}>{adding ? $t('feeds.adding') : $t('feeds.add')}</button>
  <button type="button" class="btn-discover" onclick={() => (discoverOpen = true)}>
    ✨ {$t('feeds.discover')}
  </button>
  {#if error}<span class="error">{error}</span>{/if}
</form>

<FeedDiscoveryModal bind:open={discoverOpen} onAdded={load} />

<div class="card import">
  <label class="import-label">
    📥 {$t('feeds.import_opml')}
    <input type="file" accept=".opml,.xml" onchange={importOpml} disabled={importing} />
  </label>
  {#if importResult}<p class="small">{importResult}</p>{/if}
  <label class="import-label" style="margin-top: 0.6rem">
    ▶ {$t('feeds.import_youtube')}
    <input type="file" accept=".csv,.opml,.xml" onchange={importSubs} disabled={importingSubs} />
  </label>
  {#if subsResult}<p class="small">{subsResult}</p>{/if}
  <div class="row" style="margin-top: 0.6rem">
    <button onclick={refreshAll} disabled={refreshing}>
      {refreshing ? $t('feeds.refreshing') : `↻ ${$t('feeds.refresh_all')}`}
    </button>
    <button onclick={exportOpml} disabled={exporting}>
      {exporting ? $t('feeds.exporting') : `📤 ${$t('feeds.export_opml')}`}
    </button>
  </div>
  {#if exportError}<p class="small error">{exportError}</p>{/if}
</div>

{#each feeds as feed (feed.id)}
  {@const host = feedHost(feed)}
  <div class="card feed">
    <div class="row">
      {#if host}
        <img class="favicon" src={faviconUrl(host)} alt="" loading="lazy" onerror={hideFav} />
      {/if}
      <strong>{feed.title || feed.url}</strong>
      {#if feed.kind === 'mail'}
        <span class="badge mail">✉ {$t('feeds.badge_newsletter')}</span>
      {:else if feed.url.includes('youtube.com/feeds/videos.xml')}
        <span class="badge video">▶ YouTube</span>
      {:else if feed.kind === 'reader_api'}
        <span class="badge reader">📡 {$t('feeds.badge_reader')}</span>
      {/if}
      <span class="badge" class:off={!feed.is_enabled}>
        {feed.is_enabled ? $t('feeds.enabled') : $t('feeds.disabled')}
      </span>
      {#if feed.story_count > 0}
        <a
          class="badge counts"
          href="/?feed={feed.id}"
          title={$t('feeds.counts_title')}
        >
          {$t('feeds.story_count', { count: feed.story_count })}{#if feed.unread_story_count > 0}
            · {$t('feeds.unread_count', { count: feed.unread_story_count })}{/if}
        </a>
      {:else}
        <span class="badge counts none">{$t('feeds.no_stories')}</span>
      {/if}
      {#if feed.consecutive_failures > 0}
        <span class="badge warn">{$t('feeds.failures', { count: feed.consecutive_failures })}</span>
      {/if}
      <span class="spacer"></span>
      {#if feed.kind === 'rss'}
        <button onclick={() => refresh(feed)} disabled={!feed.is_enabled || refreshingId === feed.id}>
          {refreshingId === feed.id ? '…' : `↻ ${$t('feeds.refresh')}`}
        </button>
      {/if}
      <button onclick={() => toggle(feed)}>{feed.is_enabled ? $t('feeds.disable') : $t('feeds.enable')}</button>
      <button class="danger" onclick={() => remove(feed)}>{$t('feeds.delete')}</button>
    </div>
    <div class="meta">
      {#if feed.kind === 'mail'}
        <span>{$t('feeds.from', { email: feed.sender_email ?? '' })}</span>
        <span>{$t('feeds.emails_ingested', { count: feed.email_count })}</span>
        <span>{$t('feeds.mail_populated')}</span>
      {:else if feed.kind === 'reader_api'}
        <span>{$t('feeds.reader_virtual')}</span>
        <span>{$t('feeds.last_fetched', { date: lastFetched(feed.last_fetched_at) })}</span>
        <span>{$t('feeds.reader_managed')}</span>
      {:else}
        <span>{feed.url}</span>
        <span>{$t('feeds.polls_every', { minutes: feed.poll_interval_min })}</span>
        <span>
          {$t('feeds.initial_import_value', {
            value:
              feed.backfill_days === null
                ? $t('feeds.value_server_default')
                : feed.backfill_days === 0
                  ? $t('feeds.value_everything')
                  : $t('feeds.value_last_days', { days: feed.backfill_days })
          })}
        </span>
        <span>{$t('feeds.last_fetched', { date: lastFetched(feed.last_fetched_at) })}</span>
      {/if}
    </div>
    {#if feed.last_error}<p class="error small">{$t('feeds.last_error', { error: feed.last_error })}</p>{/if}
  </div>
{:else}
  <div class="card"><p>{$t('feeds.empty')}</p></div>
{/each}

<style>
  .add { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .add input { flex: 1; min-width: 0; }
  .btn-discover {
    background: var(--surface-soft);
    border: 1px solid var(--accent);
    color: var(--accent);
    font-weight: 500;
  }
  .btn-discover:hover {
    background: var(--chip-bg);
  }
  .backfill {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.85em;
    color: var(--text-secondary);
    white-space: nowrap;
  }
  .row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .feed strong { overflow-wrap: anywhere; min-width: 0; }
  .spacer { flex: 1; }
  .badge {
    font-size: 0.75em;
    background: var(--ok-bg);
    color: var(--ok);
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
  }
  .badge.off { background: var(--frozen-bg); color: var(--frozen-text); }
  .badge.warn { background: var(--error-bg); color: var(--error); }
  .badge.mail { background: var(--chip-bg); color: var(--accent); }
  .badge.reader { background: var(--chip-bg); color: var(--accent); }
  .badge.counts {
    background: var(--chip-bg);
    color: var(--text-secondary);
    text-decoration: none;
  }
  a.badge.counts:hover { color: var(--accent); }
  .badge.counts.none { opacity: 0.6; }
  .favicon { width: 1.1rem; height: 1.1rem; border-radius: 3px; }
  .meta {
    display: flex;
    gap: 1rem;
    color: var(--text-secondary);
    font-size: 0.85em;
    margin-top: 0.4rem;
    flex-wrap: wrap;
  }
  .error { color: var(--error); }
  .small { font-size: 0.85em; margin: 0.4rem 0 0; }
  .danger { color: var(--error); }
  .import-label { display: block; margin: 0; cursor: pointer; }
  .import input[type='file'] { margin-top: 0.4rem; max-width: 100%; }

  @media (max-width: 700px) {
    /* title + badges on one row, action buttons wrap onto their own line */
    .feed .row .spacer { flex-basis: 100%; }
  }
</style>
