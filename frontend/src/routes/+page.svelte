<script lang="ts">
  import { onMount } from 'svelte';
  import { api, faviconUrl } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import ReadeckIcon from '$lib/components/ReadeckIcon.svelte';
  import ShareButton from '$lib/components/ShareButton.svelte';
  import type { Category, StoryListItem } from '$lib/types';

  type Sort = 'updated' | 'published' | 'sources';
  type Order = 'asc' | 'desc';

  let stories = $state<StoryListItem[]>([]);
  let categories = $state<Category[]>([]);
  let filter = $state<'all' | 'unread' | 'updated'>('unread');
  let category = $state('');
  let sort = $state<Sort>('published');
  let order = $state<Order>('asc');
  let loading = $state(true);
  let isMobile = $state(false);
  let readeckEnabled = $state(false);
  let readeckSaving = $state<Record<number, boolean>>({});

  // --- swipe deck state (mobile card view) ---
  let index = $state(0);
  let dx = $state(0);
  let snap = $state(false);
  let dragging = $state(false);
  let dragStartX = 0;
  let dragStartY = 0;
  let horizLock: boolean | null = null;

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
        { emoji: '🌙', title: 'All caught up', body: "It's late — the world will keep spinning without you. Go to sleep." },
        { emoji: '😴', title: 'All caught up', body: 'Nothing left to read. Your pillow is calling.' },
      ]);
    if (h < 9)
      return pick([
        { emoji: '☀️', title: 'All caught up', body: 'Fresh news, fresh day. Grab a coffee — the world can wait.' },
        { emoji: '☕', title: 'All caught up', body: 'Inbox zero, news zero. Go seize the morning.' },
      ]);
    if (h < 12)
      return pick([
        { emoji: '🌤', title: 'All caught up', body: "You're up to speed. Go make something great today." },
        { emoji: '💪', title: 'All caught up', body: 'News done. Now go do that thing you were avoiding.' },
      ]);
    if (h < 14)
      return pick([
        { emoji: '🍽', title: 'All caught up', body: 'News digested. Time for an actual lunch.' },
      ]);
    if (h < 18)
      return pick([
        { emoji: '🌤', title: 'All caught up', body: 'Nothing left to read. The afternoon is yours.' },
        { emoji: '🚶', title: 'All caught up', body: "You're done. Go outside — the sun is still up." },
      ]);
    return pick([
      { emoji: '🌆', title: 'All caught up', body: "That's the news. Go enjoy your evening." },
      { emoji: '😎', title: 'All caught up', body: "You're done reading. Go back to your life — it's a good one." },
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
    if ($currentUser?.story_sort) sort = $currentUser.story_sort;
    if ($currentUser?.story_order) order = $currentUser.story_order;
    if ($currentUser?.story_filter) filter = $currentUser.story_filter;
    void (async () => {
      try {
        categories = await api.categories.list();
      } catch {
        /* non-admin users may not list categories */
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
    stories = await api.stories.list(filter, category || undefined, sort, order);
    index = 0;
    dx = 0;
    loading = false;
  }

  /** Remember list prefs server-side so every device follows (per-user pref). */
  function savePrefs() {
    api
      .patchMe({ story_sort: sort, story_order: order, story_filter: filter })
      .then((u) => ($currentUser = u))
      .catch(() => {});
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

  function ago(iso: string): string {
    const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 60) return `${mins}m ago`;
    const h = Math.floor(mins / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  }

  // Track hero-image load per story so a swiping change never shows the
  // previous story's image while the new one downloads.
  let heroLoaded = $state(false);

  /** Preload a story's hero image so swiping to it is instant. */
  function preload(story: StoryListItem | undefined) {
    if (story?.image_url) {
      const img = new Image();
      img.src = story.image_url;
    }
  }

  /** Broken/missing favicon → drop the img, revealing the host-letter fallback. */
  function hideFav(e: Event) {
    (e.currentTarget as HTMLImageElement).remove();
  }

  async function toggleRead(story: StoryListItem) {
    if (story.is_read) {
      story.is_read = false;
      await api.stories.unread(story.id).catch(() => {});
    } else {
      story.is_read = true;
      await api.stories.read(story.id).catch(() => {});
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
    dragging = true;
    horizLock = null;
    dragStartX = e.clientX;
    dragStartY = e.clientY;
  }

  function onPointerMove(e: PointerEvent) {
    if (!dragging) return;
    const mx = e.clientX - dragStartX;
    const my = e.clientY - dragStartY;
    if (horizLock === null && (Math.abs(mx) > 8 || Math.abs(my) > 8)) {
      horizLock = Math.abs(mx) > Math.abs(my);
    }
    if (horizLock) dx = mx;
  }

  function onPointerUp() {
    if (!dragging) return;
    dragging = false;
    if (dx <= -80) commit('left');
    else if (dx >= 80) commit('right');
    else dx = 0;
    horizLock = null;
  }

  function commit(dir: 'left' | 'right') {
    const story = current;
    // left past the last story lands on the "all caught up" card; right from it comes back
    if (dir === 'left' && atEnd) { dx = 0; return; }
    if (dir === 'right' && index <= 0) { dx = 0; return; }
    dx = dir === 'left' ? -480 : 480;
    setTimeout(() => {
      if (dir === 'left' && story && !story.is_read) {
        story.is_read = true;
        api.stories.read(story.id).catch(() => {});
      }
      heroLoaded = false; // next image starts hidden until it loads
      index += dir === 'left' ? 1 : -1;
      snap = true;
      dx = 0;
      requestAnimationFrame(() => requestAnimationFrame(() => (snap = false)));
    }, 220);
  }

  // Preload adjacent story images so swipes feel instant.
  $effect(() => {
    preload(stories[index + 1]);
    preload(stories[index - 1]);
  });
</script>

<h1>Stories</h1>

{#if isMobile && (pullDist > 0 || refreshing)}
  <div class="pullhint" style:height="{refreshing ? 36 : pullDist}px">
    <span class:spinning={refreshing}>↓</span>
    <span class="pulltext">{refreshing ? 'Refreshing…' : pullDist >= 60 ? 'Release to refresh' : 'Pull to refresh'}</span>
  </div>
{/if}

<div class="toolbar card">
  <div class="filters">
    {#each ['all', 'unread', 'updated'] as f}
      <button
        class:active={filter === f}
        onclick={() => { filter = f as typeof filter; savePrefs(); load(); }}
      >
        {f === 'all' ? 'All' : f === 'unread' ? 'Unread' : 'Updated'}
      </button>
    {/each}
  </div>
  <div class="tools">
    {#if categories.length}
      <select bind:value={category} onchange={load}>
        <option value="">All categories</option>
        {#each categories as c (c.id)}<option value={c.name}>{c.name}</option>{/each}
      </select>
    {/if}
    <select bind:value={sort} onchange={() => { savePrefs(); load(); }}>
      <option value="published">Article date</option>
      <option value="updated">Processing date</option>
      <option value="sources">Source count</option>
    </select>
    <button
      class="dir"
      title={order === 'desc' ? 'Newest / most first — click to reverse' : 'Oldest / least first — click to reverse'}
      onclick={() => { order = order === 'desc' ? 'asc' : 'desc'; savePrefs(); load(); }}
    >
      {order === 'desc' ? '↓' : '↑'}
    </button>
  </div>
</div>

{#if loading}
  <div class="card"><p>Loading…</p></div>
{:else if stories.length === 0}
  <div class="card">
    <p>No stories yet. Add feeds on the <a href="/feeds">Feeds page</a> — articles are
    clustered into stories automatically once the pipeline runs.</p>
  </div>
{:else if isMobile}
  <!-- Mobile: story deck. Swipe ← marks read and opens the next story; → goes back.
       Past the last story, a final "all caught up" card closes the deck. -->
  <div class="deckmeta">
    <button class="navbtn" onclick={() => commit('right')} disabled={index === 0}>‹ Prev</button>
    {#if atEnd}
      <span>✓ done</span>
      <span class="hint">swipe → back to stories</span>
    {:else}
      <span>{index + 1} / {stories.length}</span>
      <span class="hint">swipe ← read &amp; next</span>
    {/if}
    <button class="navbtn" onclick={() => commit('left')} disabled={atEnd}>Next ›</button>
  </div>
  <div
    class="deckviewport"
    role="region"
    aria-label="Story deck — swipe left to mark read and open the next story"
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
  >
    {#if current}
      <article
        class="card deckcard"
        class:readcard={current.is_read}
        style:transform="translateX({dx}px) rotate({dx / 30}deg)"
        style:transition={dragging || snap ? 'none' : 'transform 0.22s ease-out'}
      >
      <div class="row">
        <span class="chip">{current.category}</span>
        {#if !current.is_read}<span class="badge new">NEW</span>{/if}
        {#if current.updated_since_read}<span class="badge updated">UPDATED</span>{/if}
        {#if current.is_frozen}<span class="badge frozen">archived</span>{/if}
        <span class="spacer"></span>
        {#if readeckEnabled}
          {@const saved = Boolean(current.readeck_bookmark_id)}
          <button
            class="readbtn iconbtn"
            class:saved
            title={saved ? 'Already saved to Readeck — save again' : 'Save to Readeck'}
            aria-label="Save to Readeck"
            onclick={(e) => current && saveReadeck(current, e)}
            disabled={readeckSaving[current.id]}
          >
            {#if readeckSaving[current.id]}…{:else}<ReadeckIcon size={15} />{/if}
          </button>
        {/if}
        <ShareButton storyId={current.id} iconOnly buttonClass="readbtn" />
        <span class="age">{ago(current.published_at ?? current.last_updated_at)}</span>
      </div>
      {#if current.image_url}
        {#key current.id}
          <img
            class="hero"
            class:heroloading={!heroLoaded}
            src={current.image_url}
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
        <span>{current.source_count} source{current.source_count === 1 ? '' : 's'}</span>
        <span>v{current.version}</span>
        <a href="/stories/{current.id}">Full story &amp; sources →</a>
      </div>
      </article>
    {:else}
      <!-- end of deck — the "you're done, go live your life" card -->
      <article
        class="card deckcard donecard"
        style:transform="translateX({dx}px) rotate({dx / 30}deg)"
        style:transition={dragging || snap ? 'none' : 'transform 0.22s ease-out'}
      >
        <span class="doneemoji">{doneMsg.emoji}</span>
        <h2>{doneMsg.title}</h2>
        <p>{doneMsg.body}</p>
        <button class="navbtn" onclick={() => commit('right')}>‹ Back to stories</button>
      </article>
    {/if}
  </div>
{:else}
  {#if stories.some((s) => !s.is_read)}
    <div class="bulkrow">
      <button class="linkbtn" onclick={markAllRead}>Mark all read ({stories.filter((s) => !s.is_read).length})</button>
    </div>
  {/if}
  {#each stories as story (story.id)}
    <div class="card story" class:read={story.is_read}>
      <div class="row">
        <span class="chip">{story.category}</span>
        {#if !story.is_read}<span class="badge new">NEW</span>{/if}
        {#if story.updated_since_read}<span class="badge updated">UPDATED</span>{/if}
        {#if story.is_frozen}<span class="badge frozen">archived</span>{/if}
        <span class="spacer"></span>
        {#if readeckEnabled}
          {@const saved = Boolean(story.readeck_bookmark_id)}
          <button
            class="readbtn iconbtn"
            class:saved
            title={saved ? 'Already saved to Readeck — save again' : 'Save to Readeck'}
            aria-label="Save to Readeck"
            onclick={(e) => saveReadeck(story, e)}
            disabled={readeckSaving[story.id]}
          >
            {#if readeckSaving[story.id]}…{:else}<ReadeckIcon size={15} />{/if}
          </button>
        {/if}
        <ShareButton storyId={story.id} iconOnly buttonClass="readbtn" />
        <button
          class="readbtn"
          title={story.is_read ? 'Mark unread' : 'Mark read'}
          onclick={() => toggleRead(story)}
        >
          {story.is_read ? '↺' : '✓'}
        </button>
        <span class="age">{ago(story.published_at ?? story.last_updated_at)}</span>
      </div>
      <a class="storylink" href="/stories/{story.id}">
        <div class="body">
          {#if story.image_url}
            <img class="thumb" src={story.image_url} alt="" loading="lazy" />
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
              <span>{story.source_count} source{story.source_count === 1 ? '' : 's'}</span>
              <span>v{story.version}</span>
            </div>
          </div>
        </div>
      </a>
    </div>
  {/each}
{/if}

<style>
  .toolbar { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .filters { display: flex; gap: 0.25rem; flex: 1; min-width: 0; }
  .filters button {
    border: 1px solid var(--border-strong); background: var(--surface); color: inherit; border-radius: 999px;
    padding: 0.25rem 0.9rem;
  }
  .filters button.active { background: var(--text); color: var(--bg); border-color: var(--text); }
  .tools { display: flex; gap: 0.4rem; align-items: center; }
  .tools select { max-width: 10rem; }
  @media (max-width: 700px) {
    /* two tidy rows: filters on one line, selects+dir on the next */
    .filters { flex: 1 1 100%; }
    .tools { flex: 1 1 auto; }
    .tools select { flex: 1 1 auto; min-width: 0; max-width: none; }
  }
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
  /* Already pushed to Readeck — grey it out (still re-clickable to re-save). */
  .readbtn.saved { color: var(--disabled-text); border-color: var(--disabled-bg); opacity: 0.6; }
  .bulkrow { display: flex; justify-content: flex-end; margin-bottom: 0.4rem; }
  .linkbtn {
    background: none; border: none; color: var(--accent); cursor: pointer;
    font-size: 0.85rem; text-decoration: underline; padding: 0;
  }
  .story h2 { margin: 0.35rem 0; font-size: 1.15rem; }
  .body { display: flex; gap: 0.9rem; align-items: flex-start; }
  .thumb {
    width: 120px; height: 80px; object-fit: cover;
    border-radius: 6px; flex-shrink: 0;
  }
  .summary { margin: 0; color: var(--text-secondary); }
  .row { display: flex; align-items: center; gap: 0.4rem; }
  .chip {
    font-size: 0.75em; background: var(--chip-bg); color: var(--accent);
    padding: 0.1rem 0.5rem; border-radius: 999px;
  }
  .badge { font-size: 0.72em; padding: 0.1rem 0.5rem; border-radius: 999px; font-weight: 600; }
  .badge.new { background: var(--ok-bg); color: var(--ok); }
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
    /* horizontal pan is handled by the deck; vertical scroll stays native */
    touch-action: pan-y;
    overflow: hidden;
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
  }
  .deckcard.readcard { opacity: 0.75; }
  .deckcard h2 { margin: 0; font-size: 1.3rem; line-height: 1.25; }
  .deckcard h2 a { color: inherit; text-decoration: none; }
  .deckcard .hero {
    width: 100%; max-height: 30vh; object-fit: cover; border-radius: 6px;
  }
  .deckcard .hero.heroloading { visibility: hidden; height: 0; }
  .decksummary {
    margin: 0; color: var(--text-secondary); line-height: 1.5; font-size: 1rem;
    overflow-y: auto; flex: 1;
  }
  .deckcard .meta a { margin-left: auto; color: var(--accent); }

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
