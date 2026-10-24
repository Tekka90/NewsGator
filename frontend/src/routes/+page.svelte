<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { api, faviconUrl, imageSrc } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import VideoPlayer from '$lib/components/VideoPlayer.svelte';
  import { formatDuration, providerLabel } from '$lib/video';
  import ReadeckIcon from '$lib/components/ReadeckIcon.svelte';
  import ShareButton from '$lib/components/ShareButton.svelte';
  import PresetBar from '$lib/components/PresetBar.svelte';
  import { t } from '$lib/i18n';
  import {
    adjustSavedCounts,
    defaultPresets,
    pickActivePreset,
    SORT_LABELS,
    supportsHistory,
    type ListQuery
  } from '$lib/presets';
  import type {
    Category,
    FeedOption,
    PresetOrder,
    PresetSort,
    StoryListItem,
    StoryPreset
  } from '$lib/types';

  let stories = $state<StoryListItem[]>([]);
  let categories = $state<Category[]>([]);
  let feedOptions = $state<FeedOption[]>([]);
  // Story views (presets): the active one supplies criteria + sort/order; the
  // sort dropdown / order toggle / feed / category below are session overrides
  // that never rewrite it and reset whenever a view is selected.
  let presets = $state<StoryPreset[]>(defaultPresets());
  let compact = $state(false);
  let activeId = $state<string | null>(null);
  let activePreset = $derived(presets.find((p) => p.id === activeId) ?? null);
  let sortOverride = $state<PresetSort | null>(null);
  let orderOverride = $state<PresetOrder | null>(null);
  let sort = $derived<PresetSort>(sortOverride ?? activePreset?.sort ?? 'published');
  let order = $derived<PresetOrder>(orderOverride ?? activePreset?.order ?? 'asc');
  let savedOnly = $derived(activePreset?.criteria.status === 'saved');
  let category = $state('');
  // 0 = all feeds (transient — not a persisted per-user pref)
  let feedId = $state(0);
  // Mobile filter sheet — three selects + the direction toggle don't fit next
  // to the pills on a phone (the feed select alone overflows with long feed
  // titles), so advanced filters live in a bottom sheet on touch devices.
  let filterSheetOpen = $state(false);
  // Badge on the Filters button: how many advanced filters are non-default.
  let advFilters = $derived((feedId ? 1 : 0) + (category ? 1 : 0));
  let loading = $state(true);
  let isMobile = $state(false);
  let readeckEnabled = $state(false);
  let readeckSaving = $state<Record<number, boolean>>({});
  let saving = $state<Record<number, boolean>>({});
  let presetCounts = $state<Record<string, number>>({});
  // Sticky title + filter bar — its height is measured at click time so a
  // just-read story can be scrolled out exactly behind it.
  let pagehead = $state<HTMLElement>();

  // --- swipe deck state (mobile card view) ---
  let index = $state(0);
  let dx = $state(0);
  let dy = $state(0);
  let snap = $state(false);
  let dragging = $state(false);
  let dragStartX = 0;
  let dragStartY = 0;
  let axisLock: 'x' | 'y' | null = null;
  let startScrollY = 0;
  let swipeDownAllowed = false;

  // Read stories kept behind the first unread card: `anchorIndex` is where the strict
  // (unread) list starts, so swiping right from it walks back through what was read.
  const HISTORY_PAGE = 10;
  let anchorIndex = $state(0);
  let historyDone = $state(true);
  let historyLoading = false;
  let historyEnabled = $derived(isMobile && supportsHistory(activePreset, sort));

  let current = $derived(index < stories.length ? stories[index] : null);
  // Past the last card = the "all caught up" end card
  let atEnd = $derived(stories.length > 0 && index >= stories.length);

  /** Time-of-day-aware end-of-deck message. Generated locally so it is instant
      and works offline in the PWA — no LLM round-trip at the end of a session. */
  function pickDoneMessage(now = new Date()): { emoji: string; title: string; body: string } {
    const h = now.getHours();
    const pick = <T,>(arr: T[]): T => arr[Math.floor(Math.random() * arr.length)];
    if (h >= 22 || h < 5)
      return pick([
        { emoji: '🌙', title: 'stories.doneTitle', body: 'stories.done_night1' },
        { emoji: '😴', title: 'stories.doneTitle', body: 'stories.done_night2' },
      ]);
    if (h < 9)
      return pick([
        { emoji: '☀️', title: 'stories.doneTitle', body: 'stories.done_early1' },
        { emoji: '☕', title: 'stories.doneTitle', body: 'stories.done_early2' },
      ]);
    if (h < 12)
      return pick([
        { emoji: '🌤', title: 'stories.doneTitle', body: 'stories.done_morning1' },
        { emoji: '💪', title: 'stories.doneTitle', body: 'stories.done_morning2' },
      ]);
    if (h < 14)
      return pick([
        { emoji: '🍽', title: 'stories.doneTitle', body: 'stories.done_noon1' },
      ]);
    if (h < 18)
      return pick([
        { emoji: '🌤', title: 'stories.doneTitle', body: 'stories.done_afternoon1' },
        { emoji: '🚶', title: 'stories.doneTitle', body: 'stories.done_afternoon2' },
      ]);
    return pick([
      { emoji: '🌆', title: 'stories.doneTitle', body: 'stories.done_evening1' },
      { emoji: '😎', title: 'stories.doneTitle', body: 'stories.done_evening2' },
    ]);
  }

  let doneMsg = $state(pickDoneMessage());
  // Fresh message each time the deck runs out of cards
  $effect(() => {
    if (atEnd) doneMsg = pickDoneMessage();
  });

  // --- pull-to-refresh (standalone PWA has no browser chrome) ---
  let pullDist = $state(0);
  let refreshing = $state(false);
  let pullStartY = 0;
  let pulling = false;

  function atTop(): boolean {
    // layout+visual viewport tolerance — iOS rubber-banding makes exact 0 rare
    return (window.scrollY ?? document.documentElement.scrollTop ?? 0) <= 2;
  }

  function onPullStart(e: TouchEvent) {
    if (!isMobile || refreshing || !atTop()) return;
    const t = e.touches[0];
    if (!t) return;
    pulling = true;
    pullStartY = t.clientY;
  }

  function onPullMove(e: TouchEvent) {
    if (!pulling) return;
    const t = e.touches[0];
    if (!t) return;
    const dy = t.clientY - pullStartY;
    if (dy > 0 && atTop()) pullDist = Math.min(dy * 0.5, 90);
  }

  async function onPullEnd() {
    if (!pulling) return;
    pulling = false;
    if (pullDist >= 60 && !refreshing) {
      refreshing = true;
      await load();
      refreshing = false;
    }
    pullDist = 0;
  }

  onMount(() => {
    // Swipe deck on touch-first devices (iPhone, iPad, Android, touch laptops) —
    // a finger can't "scroll-hover" a list comfortably, and UA-sniffing iPadOS
    // is unreliable since it reports as macOS. Mouse/keyboard keeps the list.
    const mq = matchMedia('(pointer: coarse)');
    const onMq = () => (isMobile = mq.matches);
    onMq();
    mq.addEventListener('change', onMq);
    // Per-user list prefs live server-side — shared across devices
    // ?feed=N pre-selects the feed filter (linked from the Feeds page counts)
    const qFeed = Number(page.url.searchParams.get('feed'));
    if (qFeed > 0) feedId = qFeed;
    void (async () => {
      try {
        const doc = await api.presets.get();
        presets = doc.presets;
        compact = doc.compact;
        activeId = pickActivePreset(doc.presets, doc.last_preset_id, $currentUser?.story_filter)?.id ?? null;
      } catch {
        activeId = pickActivePreset(presets, null, $currentUser?.story_filter)?.id ?? null;
      }
      try {
        categories = await api.categories.list();
      } catch {
        /* non-admin users may not list categories */
      }
      try {
        feedOptions = await api.stories.feedOptions();
      } catch {
        /* filter dropdown simply stays hidden */
      }
      // Optional Readeck feature — the endpoint 404s when not configured.
      try {
        const s = await api.settings.get();
        readeckEnabled = Boolean(s.values.readeck_base_url && s.values.readeck_token);
      } catch {
        readeckEnabled = false;
      }
      await load();
    })();
    // pull-to-refresh on touch devices (no browser chrome in standalone PWA)
    window.addEventListener('touchstart', onPullStart, { passive: true });
    window.addEventListener('touchmove', onPullMove, { passive: true });
    window.addEventListener('touchend', onPullEnd);
    window.addEventListener('touchcancel', onPullEnd);
    return () => {
      mq.removeEventListener('change', onMq);
      window.removeEventListener('touchstart', onPullStart);
      window.removeEventListener('touchmove', onPullMove);
      window.removeEventListener('touchend', onPullEnd);
      window.removeEventListener('touchcancel', onPullEnd);
    };
  });

  async function load() {
    loading = true;
    if (!activeId) {
      stories = [];
      loading = false;
      return;
    }
    const [fetchedStories, counts] = await Promise.all([
      api.stories.listPreset({
        preset: activeId,
        sort: sortOverride ?? undefined,
        order: orderOverride ?? undefined,
        category: category || undefined,
        feedId: feedId || undefined
      }),
      api.presets.counts().catch(() => null)
    ]);
    stories = fetchedStories;
    if (counts) presetCounts = counts.counts;
    index = 0;
    anchorIndex = 0;
    historyDone = true;
    if (historyEnabled) await loadInitialHistory(fetchedStories);
    dx = 0;
    dy = 0;
    loading = false;
  }

  function historyQuery(extra: Partial<ListQuery>): ListQuery {
    return {
      preset: activeId ?? 'all',
      sort: sortOverride ?? undefined,
      order: orderOverride ?? undefined,
      category: category || undefined,
      feedId: feedId || undefined,
      includeRead: true,
      limit: HISTORY_PAGE,
      ...extra
    };
  }

  /** Puts up to one page of read stories in front of the unread ones. When everything is
      read there is no anchor: take the tail of the relaxed list via the flipped order. */
  async function loadInitialHistory(strict: StoryListItem[]) {
    try {
      let older: StoryListItem[];
      if (strict.length > 0) {
        older = await api.stories.listPreset(historyQuery({ before: strict[0].id }));
      } else {
        const flipped: PresetOrder = order === 'desc' ? 'asc' : 'desc';
        older = (await api.stories.listPreset(historyQuery({ order: flipped }))).reverse();
      }
      stories = [...older, ...strict];
      index = anchorIndex = older.length;
      historyDone = older.length < HISTORY_PAGE;
    } catch {
      /* history is a convenience: the unread deck still works without it */
    }
  }

  /** Called while swiping back: fetches the next page of older read stories near the edge. */
  async function loadMoreHistory() {
    if (!historyEnabled || historyDone || historyLoading || stories.length === 0) return;
    historyLoading = true;
    try {
      const older = await api.stories.listPreset(historyQuery({ before: stories[0].id }));
      if (older.length) {
        stories = [...older, ...stories];
        index += older.length;
        anchorIndex += older.length;
      }
      if (older.length < HISTORY_PAGE) historyDone = true;
    } catch {
      /* retried on the next swipe */
    } finally {
      historyLoading = false;
    }
  }

  function backToUnread() {
    heroLoaded = false;
    index = anchorIndex;
  }

  $effect(() => {
    if (index <= 3) void loadMoreHistory();
  });

  /** Selecting a view applies its criteria + sort/order and drops every manual override. */
  function selectPreset(p: StoryPreset) {
    activeId = p.id;
    sortOverride = null;
    orderOverride = null;
    category = '';
    feedId = 0;
    // last_preset_id is synced across devices; story_filter keeps older clients in step for built-ins
    api
      .patchMe({
        last_preset_id: p.id,
        ...(p.kind === 'builtin' ? { story_filter: p.id as 'all' } : {})
      })
      .then((u) => ($currentUser = u))
      .catch(() => {});
    void load();
  }

  function setSort(value: PresetSort) {
    sortOverride = value === activePreset?.sort ? null : value;
    void load();
  }

  function toggleOrder() {
    orderOverride = order === 'desc' ? 'asc' : 'desc';
    void load();
  }

  async function saveReadeck(story: StoryListItem, e: Event) {
    e.preventDefault();
    e.stopPropagation();
    readeckSaving[story.id] = true;
    try {
      const r = await api.stories.saveToReadeck(story.id);
      story.readeck_bookmark_id = r.bookmark_id; // persisted server-side
    } catch {
      /* error surfaces via activity feed; keep list quiet */
    } finally {
      readeckSaving[story.id] = false;
    }
  }

  function ago(iso: string, tr: (key: string, params?: Record<string, string | number>) => string): string {
    const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 60) return tr('stories.agoMinutes', { n: mins });
    const h = Math.floor(mins / 60);
    if (h < 24) return tr('stories.agoHours', { n: h });
    return tr('stories.agoDays', { n: Math.floor(h / 24) });
  }

  // Track hero-image load per story so a swiping change never shows the
  // previous story's image while the new one downloads.
  let heroLoaded = $state(false);

  /** Preload a story's hero image so swiping to it is instant. */
  function preload(story: StoryListItem | undefined) {
    if (story?.image_url) {
      const img = new Image();
      img.src = imageSrc(story.image_url);
    }
  }

  /** Broken/missing favicon → drop the img, revealing the host-letter fallback. */
  function hideFav(e: Event) {
    (e.currentTarget as HTMLImageElement).remove();
  }

  /** Desktop list: after marking a story read, glide down so the greyed-out
      card slides up behind the sticky header and the next story takes its place. */
  function scrollPastStory(id: number) {
    if (isMobile) return;
    const card = document.querySelector(`[data-story="${id}"]`);
    const next = card?.nextElementSibling;
    if (!(next instanceof HTMLElement) || !next.classList.contains('story')) return;
    const headerH =
      (document.querySelector('.shell nav')?.getBoundingClientRect().height ?? 0) +
      (pagehead?.getBoundingClientRect().height ?? 0);
    const top = next.getBoundingClientRect().top + window.scrollY - headerH - 4;
    // only ever scroll down — never yank the page back up
    if (top > window.scrollY) window.scrollTo({ top, behavior: 'smooth' });
  }

  async function toggleRead(story: StoryListItem) {
    if (story.is_read) {
      story.is_read = false;
      await api.stories.unread(story.id).catch(() => {});
    } else {
      story.is_read = true;
      scrollPastStory(story.id);
      await api.stories.read(story.id).catch(() => {});
    }
  }

  async function toggleSaved(story: StoryListItem, e?: Event) {
    e?.preventDefault();
    e?.stopPropagation();
    if (saving[story.id]) return;
    saving = { ...saving, [story.id]: true };
    const next = !story.saved;
    try {
      const saved = await api.stories.setSaved(story.id, next);
      story.saved = saved.saved;
      story.saved_at = saved.saved_at;
      stories = [...stories];
      presetCounts = adjustSavedCounts(presets, presetCounts, saved.saved ? 1 : -1);
      if (savedOnly && !saved.saved) {
        const removedIndex = stories.findIndex((s) => s.id === story.id);
        stories = stories.filter((s) => s.id !== story.id);
        if (removedIndex >= 0 && index > removedIndex) index -= 1;
        if (index >= stories.length) index = stories.length;
      }
    } catch {
      /* quiet inline toggle */
    } finally {
      saving = { ...saving, [story.id]: false };
    }
  }

  async function markAllRead() {
    const unread = stories.filter((s) => !s.is_read);
    await Promise.all(unread.map((s) => api.stories.read(s.id).catch(() => {})));
    unread.forEach((s) => (s.is_read = true));
  }

  // --- swipe deck handlers ---
  // Horizontal drag navigates cards; vertical stays native scroll (touch-action: pan-y).
  function onPointerDown(e: PointerEvent) {
    if ((e.target as Element | null)?.closest('[data-no-swipe]')) return;
    dragging = true;
    axisLock = null;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
    startScrollY = window.scrollY ?? document.documentElement.scrollTop ?? 0;
    swipeDownAllowed =
      (e.target as Element | null)?.closest('a, button, input, select, textarea, .decksummary, [data-no-swipe]') ===
      null;
  }

  function onPointerMove(e: PointerEvent) {
    if (!dragging) return;
    const mx = e.clientX - dragStartX;
    const my = e.clientY - dragStartY;
    if (axisLock === null && (Math.abs(mx) > 8 || Math.abs(my) > 8)) {
      axisLock = Math.abs(mx) > Math.abs(my) ? 'x' : 'y';
    }
    if (axisLock === 'x') {
      dx = mx;
      dy = 0;
    } else if (axisLock === 'y' && swipeDownAllowed) {
      dx = 0;
      dy = Math.max(my, 0);
    }
  }

  function onPointerUp() {
    if (!dragging) return;
    dragging = false;
    if (axisLock === 'x') {
      if (dx <= -80) commit('left');
      else if (dx >= 80) commit('right');
      else dx = 0;
    } else if (
      axisLock === 'y' &&
      swipeDownAllowed &&
      dy >= 90 &&
      Math.abs((window.scrollY ?? document.documentElement.scrollTop ?? 0) - startScrollY) < 4
    ) {
      commit('down');
    } else {
      dx = 0;
      dy = 0;
    }
    axisLock = null;
    swipeDownAllowed = false;
  }

  function commit(dir: 'left' | 'right' | 'down') {
    const story = current;
    if (dir === 'down') {
      if (!story) {
        dy = 0;
        return;
      }
      const bounce = Math.min(Math.max(window.innerHeight * 0.16, 110), 180);
      dy = bounce;
      setTimeout(() => {
        void toggleSaved(story);
        snap = true;
        dy = 0;
        requestAnimationFrame(() => requestAnimationFrame(() => (snap = false)));
      }, 140);
      return;
    }
    // left past the last story lands on the "all caught up" card; right from it comes back
    if (dir === 'left' && atEnd) { dx = 0; return; }
    if (dir === 'right' && index <= 0) { dx = 0; return; }
    // fly fully off-screen: a fixed 480px only covers phones — on wide iPad
    // screens the card must clear the whole viewport (and visibly travel over
    // the page gutters, not clip at the 960px column edge)
    const fly = Math.max(window.innerWidth, 480);
    dx = dir === 'left' ? -fly : fly;
    setTimeout(() => {
      if (dir === 'left' && story && !story.is_read) {
        story.is_read = true;
        api.stories.read(story.id).catch(() => {});
      }
      heroLoaded = false; // next image starts hidden until it loads
      index += dir === 'left' ? 1 : -1;
      snap = true;
      dx = 0;
      dy = 0;
      requestAnimationFrame(() => requestAnimationFrame(() => (snap = false)));
    }, 220);
  }

  // Preload adjacent story images so swipes feel instant.
  $effect(() => {
    preload(stories[index + 1]);
    preload(stories[index - 1]);
  });
</script>

<div class="pagehead" bind:this={pagehead}>
  <h1>
    {$t('stories.title')}
    {#if !loading && stories.length > 0}
      <!-- stays visible while scrolling (pagehead is sticky) — on mobile this
           doubles as the deck position, which used to scroll away with deckmeta -->
      <span class="count" title={$t('stories.countTitle')}>
        {#if !isMobile}
          {stories.length}
        {:else if index < anchorIndex}
          {$t('stories.readCount', { n: anchorIndex - index })}
        {:else}
          {Math.min(index - anchorIndex + 1, stories.length - anchorIndex)} / {stories.length - anchorIndex}
        {/if}
      </span>
    {/if}
  </h1>

  {#if isMobile && (pullDist > 0 || refreshing)}
    <div class="pullhint" style:height="{refreshing ? 36 : pullDist}px">
      <span class:spinning={refreshing}>↓</span>
      <span class="pulltext">{refreshing ? $t('stories.refreshing') : pullDist >= 60 ? $t('stories.releaseToRefresh') : $t('stories.pullToRefresh')}</span>
    </div>
  {/if}

  <div class="toolbar card">
    <div class="filters">
      <PresetBar {presets} {activeId} counts={presetCounts} {compact} onselect={selectPreset} />
    </div>
    {#if isMobile}
      <button
        class="filterbtn"
        onclick={() => (filterSheetOpen = true)}
        aria-haspopup="dialog"
        title={$t('stories.filtersTitle')}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M22 3H2l8 9.46V19l4 2v-8.54z" />
        </svg>
        {$t('stories.filters')}
        {#if advFilters}<span class="fbadge">{advFilters}</span>{/if}
      </button>
    {:else}
      <div class="tools">
        {#if feedOptions.length}
          <select bind:value={feedId} onchange={load} title={$t('stories.feedFilterTitle')}>
            <option value={0}>{$t('stories.allFeeds')}</option>
            {#each feedOptions as f (f.id)}<option value={f.id}>{f.kind === 'mail' ? '✉ ' : ''}{f.title || f.url}</option>{/each}
          </select>
        {/if}
        {#if categories.length}
          <select bind:value={category} onchange={load}>
            <option value="">{$t('stories.allCategories')}</option>
            {#each categories as c (c.id)}<option value={c.name}>{c.name}</option>{/each}
          </select>
        {/if}
        <select value={sort} onchange={(e) => setSort(e.currentTarget.value as PresetSort)} aria-label={$t('stories.sortBy')}>
          {#each Object.keys(SORT_LABELS) as value (value)}<option {value}>{$t('stories.sort_' + value)}</option>{/each}
        </select>
        <button
          class="dir"
          title={order === 'desc' ? $t('stories.orderDescTitle') : $t('stories.orderAscTitle')}
          onclick={toggleOrder}
        >
          {order === 'desc' ? '↓' : '↑'}
        </button>
      </div>
    {/if}
  </div>
</div>

{#if filterSheetOpen}
  <!-- iOS-style bottom sheet for the advanced filters (touch devices only).
       Backdrop + z-index pattern follows ShareButton.svelte; changes apply
       immediately, the sheet stays open until Done/backdrop. -->
  <button class="sheetbg" aria-label={$t('stories.closeFilters')} onclick={() => (filterSheetOpen = false)} tabindex="-1"></button>
  <div class="filtersheet" role="dialog" aria-label={$t('stories.storyFilters')}>
    {#if feedOptions.length}
      <label>
        {$t('stories.feed')}
        <select bind:value={feedId} onchange={load}>
          <option value={0}>{$t('stories.allFeeds')}</option>
          {#each feedOptions as f (f.id)}<option value={f.id}>{f.kind === 'mail' ? '✉ ' : ''}{f.title || f.url}</option>{/each}
        </select>
      </label>
    {/if}
    {#if categories.length}
      <label>
        {$t('stories.category')}
        <select bind:value={category} onchange={load}>
          <option value="">{$t('stories.allCategories')}</option>
          {#each categories as c (c.id)}<option value={c.name}>{c.name}</option>{/each}
        </select>
      </label>
    {/if}
    <label>
      {$t('stories.sortBy')}
      <select value={sort} onchange={(e) => setSort(e.currentTarget.value as PresetSort)}>
        {#each Object.keys(SORT_LABELS) as value (value)}<option {value}>{$t('stories.sort_' + value)}</option>{/each}
      </select>
    </label>
    <button
      class="orderbtn"
      onclick={toggleOrder}
    >
      {order === 'desc' ? $t('stories.orderDesc') : $t('stories.orderAsc')}
    </button>
    <button class="done" onclick={() => (filterSheetOpen = false)}>{$t('stories.done')}</button>
  </div>
{/if}

{#if loading}
  <div class="card"><p>{$t('stories.loading')}</p></div>
{:else if stories.length === 0}
  <div class="card">
    <p>
      {#if savedOnly}
        {$t('stories.emptySaved')}
      {:else}
        {$t('stories.emptyBefore')} <a href="/feeds">{$t('stories.emptyLink')}</a> {$t('stories.emptyAfter')}
      {/if}
    </p>
  </div>
{:else if isMobile}
  <!-- Mobile: story deck. Swipe ← marks read and opens the next story; → goes back.
       Swipe ↓ toggles saved; past the last story, a final "all caught up" card closes the deck. -->
  <div class="deckmeta">
    <button class="navbtn" onclick={() => commit('right')} disabled={index === 0}>{$t('stories.prev')}</button>
    {#if index < anchorIndex}
      <button class="navbtn" onclick={backToUnread}>{$t('stories.backToUnread')}</button>
    {:else if atEnd}
      <span>{$t('stories.doneLabel')}</span>
      <span class="hint">{$t('stories.swipeBack')}</span>
    {:else}
      <span class="hint">{$t('stories.swipeHint')}</span>
    {/if}
    <button class="navbtn" onclick={() => commit('left')} disabled={atEnd}>{$t('stories.next')}</button>
  </div>
  <div
    class="deckviewport"
    role="region"
    aria-label={$t('stories.deckLabel')}
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
  >
    {#if current}
      <article
        class="card deckcard"
        class:readcard={current.is_read}
        style:transform="translate({dx}px, {dy}px) rotate({dx / 30}deg)"
        style:transition={dragging || snap ? 'none' : 'transform 0.22s ease-out'}
      >
      <div class="row">
        <span class="chip">{current.category}</span>
        {#if !current.is_read}<span class="dot" title={$t('stories.unreadStory')}></span>{/if}
        {#if current.updated_since_read}<span class="badge updated">{$t('stories.updatedBadge')}</span>{/if}
        {#if current.media_kind === 'video'}<span class="badge video">▶ {providerLabel(current.video_provider)}{#if current.video_duration} · {formatDuration(current.video_duration)}{/if}</span>{/if}
        {#if current.is_frozen}<span class="badge frozen">{$t('stories.archived')}</span>{/if}
        <span class="spacer"></span>
        <button
          class="readbtn starbtn"
          class:active={current.saved}
          title={current.saved ? $t('stories.savedRemoveTitle') : $t('stories.saveStory')}
          aria-label={current.saved ? $t('stories.removeSaved') : $t('stories.saveStory')}
          onclick={(e) => current && toggleSaved(current, e)}
          disabled={saving[current.id]}
        >
          {#if saving[current.id]}…{:else}★{/if}
        </button>
        {#if readeckEnabled}
          {@const savedToReadeck = Boolean(current.readeck_bookmark_id)}
          <button
            class="readbtn iconbtn"
            class:saved={savedToReadeck}
            title={savedToReadeck ? $t('stories.readeckSavedTitle') : $t('stories.saveToReadeck')}
            aria-label={$t('stories.saveToReadeck')}
            onclick={(e) => current && saveReadeck(current, e)}
            disabled={readeckSaving[current.id]}
          >
            {#if readeckSaving[current.id]}…{:else}<ReadeckIcon size={15} />{/if}
          </button>
        {/if}
        <ShareButton storyId={current.id} iconOnly buttonClass="readbtn" />
        <span class="age">{ago(current.published_at ?? current.last_updated_at, $t)}</span>
      </div>
      {#if current.media_kind === 'video'}
        {#key current.id}
          <div class="deckvideo"><VideoPlayer videoId={current.video_id} provider={current.video_provider} url={current.video_url} embedUrl={current.video_embed_url} posterUrl={current.image_url} title={current.title} /></div>
        {/key}
      {:else if current.image_url}
        {#key current.id}
          <img
            class="hero"
            class:heroloading={!heroLoaded}
            src={imageSrc(current.image_url)}
            alt=""
            onload={() => (heroLoaded = true)}
            onerror={() => (heroLoaded = true)}
          />
        {/key}
      {/if}
      <h2><a href="/stories/{current.id}">{current.title}</a></h2>
      <p class="decksummary">{current.summary}</p>
      <div class="meta">
        {#each current.source_hosts.slice(0, 4) as host}
          <span class="fav" title={host}
            >{host[0]}<img src={faviconUrl(host)} alt="" loading="lazy" onerror={hideFav} /></span
          >
        {/each}
        <span>{$t('stories.sources', { count: current.source_count })}</span>
        <span>v{current.version}</span>
        <a href="/stories/{current.id}">{$t('stories.fullStory')}</a>
      </div>
      </article>
    {:else}
      <!-- end of deck — the "you're done, go live your life" card -->
      <article
        class="card deckcard donecard"
        style:transform="translate({dx}px, {dy}px) rotate({dx / 30}deg)"
        style:transition={dragging || snap ? 'none' : 'transform 0.22s ease-out'}
      >
        <span class="doneemoji">{doneMsg.emoji}</span>
        <h2>{$t(doneMsg.title)}</h2>
        <p>{$t(doneMsg.body)}</p>
        <button class="navbtn" onclick={() => commit('right')}>{$t('stories.backToStories')}</button>
      </article>
    {/if}
  </div>
{:else}
  {#if stories.some((s) => !s.is_read)}
    <div class="bulkrow">
      <button class="linkbtn" onclick={markAllRead}>{$t('stories.markAllRead', { count: stories.filter((s) => !s.is_read).length })}</button>
    </div>
  {/if}
  {#each stories as story (story.id)}
    <div class="card story" class:read={story.is_read} data-story={story.id}>
      <div class="row">
        <span class="chip">{story.category}</span>
        {#if !story.is_read}<span class="dot" title={$t('stories.unreadStory')}></span>{/if}
        {#if story.updated_since_read}<span class="badge updated">{$t('stories.updatedBadge')}</span>{/if}
        {#if story.media_kind === 'video'}<span class="badge video">▶ {providerLabel(story.video_provider)}{#if story.video_duration} · {formatDuration(story.video_duration)}{/if}</span>{/if}
        {#if story.is_frozen}<span class="badge frozen">{$t('stories.archived')}</span>{/if}
        <span class="spacer"></span>
        <button
          class="readbtn starbtn"
          class:active={story.saved}
          title={story.saved ? $t('stories.savedRemoveTitle') : $t('stories.saveStory')}
          aria-label={story.saved ? $t('stories.removeSaved') : $t('stories.saveStory')}
          onclick={(e) => toggleSaved(story, e)}
          disabled={saving[story.id]}
        >
          {#if saving[story.id]}…{:else}★{/if}
        </button>
        {#if readeckEnabled}
          {@const savedToReadeck = Boolean(story.readeck_bookmark_id)}
          <button
            class="readbtn iconbtn"
            class:saved={savedToReadeck}
            title={savedToReadeck ? $t('stories.readeckSavedTitle') : $t('stories.saveToReadeck')}
            aria-label={$t('stories.saveToReadeck')}
            onclick={(e) => saveReadeck(story, e)}
            disabled={readeckSaving[story.id]}
          >
            {#if readeckSaving[story.id]}…{:else}<ReadeckIcon size={15} />{/if}
          </button>
        {/if}
        <ShareButton storyId={story.id} iconOnly buttonClass="readbtn" />
        <button
          class="readbtn"
          title={story.is_read ? $t('stories.markUnread') : $t('stories.markRead')}
          onclick={() => toggleRead(story)}
        >
          {story.is_read ? '↺' : '✓'}
        </button>
        <span class="age">{ago(story.published_at ?? story.last_updated_at, $t)}</span>
      </div>
      <a class="storylink" href="/stories/{story.id}">
        <div class="body">
          {#if story.image_url}
            <img class="thumb" src={imageSrc(story.image_url)} alt="" loading="lazy" />
          {/if}
          <div>
            <h2>{story.title}</h2>
            <p class="summary">{story.summary}</p>
            <div class="meta">
              {#each story.source_hosts.slice(0, 4) as host}
                <span class="fav" title={host}
                  >{host[0]}<img
                    src={faviconUrl(host)}
                    alt=""
                    loading="lazy"
                    onerror={hideFav}
                  /></span
                >
              {/each}
              <span>{$t('stories.sources', { count: story.source_count })}</span>
              <span>v{story.version}</span>
              <span class="source-action">{$t('stories.readStory')}</span>
            </div>
          </div>
        </div>
      </a>
    </div>
  {/each}
{/if}

<style>
  .deckvideo { margin: 0.5rem 0; }
  /* Sticky header: the title + filter bar stay put while stories scroll behind.
     top: --nav-h (measured in the layout) keeps it tucked right under the nav.
     z-index 20: above story cards, below the share menu (31) / sheet (40) and
     the nav (50). The negative margins + matching padding stretch the opaque
     background over main's gutters so cards never peek through. */
  .pagehead {
    position: sticky;
    top: var(--nav-h, 0px);
    z-index: 20;
    background: var(--bg);
    margin: -1.5rem -1rem 0;
    padding: 1.5rem 1rem 0;
  }
  .pagehead h1 { margin-top: 0; display: flex; align-items: center; gap: 0.5rem; }
  .count {
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--muted);
    background: var(--chip-bg);
    border-radius: 999px;
    padding: 0.1rem 0.55rem;
  }
  @media (max-width: 700px) {
    .pagehead { margin: -0.8rem -0.6rem 0; padding: 0.8rem 0.6rem 0; }
  }
  .toolbar { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .filters { display: flex; min-width: 0; max-width: 100%; }
  .tools { display: flex; gap: 0.4rem; align-items: center; }
  .tools select { max-width: 10rem; }
  @media (max-width: 700px) {
    /* touch layout: pills + a single Filters button share one row; the
       advanced filters live in the bottom sheet (filterSheetOpen) */
    .filters { flex: 1 1 0; }
    .filterbtn { flex-shrink: 0; }
  }
  .filterbtn {
    border: 1px solid var(--border-strong); background: var(--surface); color: inherit;
    border-radius: 999px; padding: 0.25rem 0.8rem;
    display: inline-flex; align-items: center; gap: 0.35rem; cursor: pointer;
  }
  .fbadge {
    background: var(--accent); color: var(--bg); border-radius: 999px;
    font-size: 0.72em; font-weight: 700; padding: 0 0.4rem; line-height: 1.5;
  }
  .sheetbg {
    position: fixed; inset: 0; z-index: 40;
    background: rgb(0 0 0 / 0.45); border: none; padding: 0; cursor: default;
  }
  .filtersheet {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 41;
    display: flex; flex-direction: column; gap: 0.7rem;
    max-height: 80dvh; overflow-y: auto;
    padding: 1rem 1rem calc(1rem + env(safe-area-inset-bottom));
    background: var(--surface); border: 1px solid var(--border); border-bottom: none;
    border-radius: 14px 14px 0 0; box-shadow: 0 -8px 32px rgb(0 0 0 / 0.35);
  }
  .filtersheet label {
    display: flex; flex-direction: column; gap: 0.25rem;
    font-size: 0.85rem; color: var(--muted);
  }
  /* 1rem minimum: smaller font sizes make iOS auto-zoom on focus */
  .filtersheet select {
    width: 100%; font-size: 1rem; padding: 0.55rem 0.6rem; color: var(--text);
  }
  .filtersheet .orderbtn, .filtersheet .done { padding: 0.6rem; }
  .filtersheet .done { font-weight: 600; }
  .dir {
    border: 1px solid var(--border-strong); background: var(--surface); color: inherit; border-radius: 6px;
    padding: 0.25rem 0.6rem; font-size: 1rem; line-height: 1; cursor: pointer;
  }
  .story { display: block; text-decoration: none; color: inherit; }
  .story:hover { border-color: var(--border-hover); }
  .story.read { opacity: 0.62; }
  .storylink { text-decoration: none; color: inherit; display: block; }
  .readbtn {
    border: 1px solid var(--border-strong); background: var(--surface); border-radius: 6px;
    width: 1.7rem; height: 1.7rem; padding: 0; line-height: 1; cursor: pointer;
    color: var(--ok); font-size: 0.95rem; flex-shrink: 0;
  }
  .readbtn:hover { border-color: var(--ok); }
  .story.read .readbtn { color: var(--faint); }
  .iconbtn { display: inline-flex; align-items: center; justify-content: center; gap: 0.3rem; }
  .starbtn {
    color: var(--muted);
    font-size: 1rem;
  }
  .starbtn:hover {
    border-color: var(--accent-signal);
    color: var(--accent-signal);
  }
  .starbtn.active {
    color: var(--accent-signal);
    border-color: var(--accent-signal);
  }
  /* Already pushed to Readeck — grey it out (still re-clickable to re-save). */
  .readbtn.saved { color: var(--disabled-text); border-color: var(--disabled-bg); opacity: 0.6; }
  .bulkrow { display: flex; justify-content: flex-end; margin-bottom: 0.4rem; }
  .linkbtn {
    background: none; border: none; color: var(--accent); cursor: pointer;
    font-size: 0.85rem; text-decoration: underline; padding: 0;
  }
  .story h2 {
    margin: 0.35rem 0;
    font-size: 1.25rem;
    letter-spacing: -0.035em;
    line-height: 1.2;
    font-weight: 750;
  }
  .source-action {
    margin-left: auto;
    color: var(--accent);
    font-weight: 700;
    font-size: 0.88rem;
  }
  .body { display: flex; gap: 0.9rem; align-items: flex-start; }
  .thumb {
    width: 120px; height: 80px; object-fit: cover;
    border-radius: var(--radius-sm); flex-shrink: 0;
  }
  .summary { margin: 0; color: var(--text-secondary); line-height: 1.45; }
  .row { display: flex; align-items: center; gap: 0.45rem; }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--accent-signal);
    display: inline-block;
    flex-shrink: 0;
    box-shadow: 0 0 6px rgba(242, 184, 39, 0.5);
  }
  .chip {
    font-size: 0.75em; background: var(--chip-bg); color: var(--accent);
    font-weight: 600;
    padding: 0.12rem 0.55rem; border-radius: var(--radius-pill);
  }
  .badge { font-size: 0.72em; padding: 0.1rem 0.5rem; border-radius: var(--radius-pill); font-weight: 600; }
  .badge.updated { background: var(--warn-bg); color: var(--warn); }
  .badge.frozen { background: var(--frozen-bg); color: var(--frozen-text); }
  .spacer { flex: 1; }
  .age { color: var(--muted); font-size: 0.85em; }
  .meta { display: flex; gap: 1rem; color: var(--muted); font-size: 0.85em; margin-top: 0.5rem; align-items: center; }
  .fav {
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px; border-radius: 4px; overflow: hidden; flex-shrink: 0;
    background: var(--chip-bg); color: var(--accent); font-size: 0.72em; font-weight: 700;
    text-transform: uppercase;
  }
  .fav img { position: absolute; inset: 0; width: 100%; height: 100%; }
  .toolbar { flex-wrap: wrap; }

  /* --- mobile story deck --- */
  .deckmeta {
    display: flex; align-items: center; gap: 0.7rem;
    color: var(--muted); font-size: 0.85em; margin: 0.2rem 0 0.5rem;
  }
  .deckmeta .hint { flex: 1; text-align: center; }
  .navbtn {
    border: 1px solid var(--border-strong); background: var(--surface); color: inherit; border-radius: 999px;
    padding: 0.25rem 0.8rem;
  }
  .navbtn:disabled { opacity: 0.4; }
  .deckviewport {
    /* horizontal pan is handled by the deck; vertical scroll stays native.
       NO overflow clipping here: the flying card must slide OVER the grey
       page gutters beside the centered 960px column on wide touch screens
       (iPad), not disappear under them. Horizontal page overflow is clipped
       at the html level in +layout.svelte instead. */
    touch-action: pan-y;
    padding: 0.2rem;
  }
  .deckcard {
    margin-bottom: 0;
    user-select: none;
    -webkit-user-select: none;
    min-height: 55vh;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    will-change: transform;
    border-radius: var(--radius-card);
    box-shadow: var(--gator-shadow);
  }
  .deckcard.readcard { opacity: 0.75; }
  .deckcard h2 {
    margin: 0;
    font-size: 1.35rem;
    letter-spacing: -0.035em;
    line-height: 1.22;
    font-weight: 750;
  }
  .deckcard h2 a { color: inherit; text-decoration: none; }
  .deckcard .hero {
    width: 100%; max-height: 30vh; object-fit: cover; border-radius: var(--radius-sm);
  }
  .deckcard .hero.heroloading { visibility: hidden; height: 0; }
  .decksummary {
    margin: 0; color: var(--text-secondary); line-height: 1.5; font-size: 1rem;
    overflow-y: auto; flex: 1;
  }
  .deckcard .meta a { margin-left: auto; color: var(--accent); font-weight: 700; }

  /* end-of-deck "all caught up" card */
  .donecard {
    align-items: center;
    justify-content: center;
    text-align: center;
    gap: 0.4rem;
  }
  .donecard .doneemoji { font-size: 3rem; line-height: 1; }
  .donecard h2 { margin: 0; font-size: 1.4rem; }
  .donecard p { margin: 0 0 0.8rem; color: var(--text-secondary); max-width: 22rem; }

  /* pull-to-refresh indicator */
  .pullhint {
    display: flex; align-items: center; justify-content: center; gap: 0.4rem;
    overflow: hidden; color: var(--muted); font-size: 0.85rem;
    transition: height 0.15s ease-out;
  }
  .pullhint span:first-child {
    display: inline-block; font-size: 1.1rem;
    transition: transform 0.15s ease-out;
  }
  .pullhint .spinning { animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
