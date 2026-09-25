<script lang="ts">
  import { api, faviconUrl } from '$lib/api';
  import type { DiscoveredFeed } from '$lib/types';

  let {
    open = $bindable(false),
    onAdded
  }: {
    open: boolean;
    onAdded?: () => void | Promise<void>;
  } = $props();

  const THEMES = [
    { id: 'Tech & AI', icon: '💻', label: 'Tech & AI' },
    { id: 'General News', icon: '📰', label: 'General News' },
    { id: 'Politics', icon: '🏛️', label: 'Politics' },
    { id: 'Science & Nature', icon: '🔬', label: 'Science & Nature' },
    { id: 'Academic Research', icon: '📚', label: 'Research' },
    { id: 'Gaming & Esports', icon: '🎮', label: 'Gaming' },
    { id: 'Business & Finance', icon: '📈', label: 'Business & Finance' },
    { id: 'Environment & Climate', icon: '🌍', label: 'Climate & Earth' },
    { id: 'Health & Medicine', icon: '🩺', label: 'Health' },
    { id: 'Cybersecurity', icon: '🔒', label: 'Cybersecurity' },
    { id: 'Culture & Arts', icon: '🎬', label: 'Culture & Arts' },
    { id: 'Sports & Athletics', icon: '⚽', label: 'Sports' },
    { id: 'Design & Hardware', icon: '🎨', label: 'Design & Hardware' },
    { id: 'Local & Regional', icon: '🏙️', label: 'Local & Regional' }
  ];

  let location = $state('');
  let selectedThemes = $state<string[]>([]);
  let customQuery = $state('');

  let researching = $state(false);
  let researchStage = $state('Formulating search queries…');
  let error = $state('');
  let results = $state<DiscoveredFeed[] | null>(null);
  let selectedUrls = $state<Set<string>>(new Set());

  let subscribing = $state(false);
  let subscribeStatus = $state('');
  let expandedUrls = $state<Set<string>>(new Set());

  function toggleExpand(url: string) {
    const next = new Set(expandedUrls);
    if (next.has(url)) {
      next.delete(url);
    } else {
      next.add(url);
    }
    expandedUrls = next;
  }

  function toggleTheme(themeId: string) {
    if (selectedThemes.includes(themeId)) {
      selectedThemes = selectedThemes.filter((t) => t !== themeId);
    } else {
      selectedThemes = [...selectedThemes, themeId];
    }
  }

  async function handleResearch(proposeMore = false) {
    error = '';
    subscribeStatus = '';
    researching = true;
    researchStage = 'Formulating search queries…';

    const excluded = proposeMore && results ? results.map((f) => f.url) : [];
    if (!proposeMore) {
      results = null;
      selectedUrls = new Set();
    }

    let timer: ReturnType<typeof setInterval> | null = null;
    const stages = [
      { at: 2500, label: 'Searching regional web sources…' },
      { at: 6500, label: 'Probing candidate feeds…' },
      { at: 11000, label: 'Detecting paywalls & access…' },
      { at: 16000, label: 'Synthesizing recommendations…' }
    ];
    const startTime = Date.now();
    timer = setInterval(() => {
      const elapsed = Date.now() - startTime;
      for (let i = stages.length - 1; i >= 0; i--) {
        if (elapsed >= stages[i].at) {
          researchStage = stages[i].label;
          break;
        }
      }
    }, 400);

    try {
      const resp = await api.feeds.discover({
        location: location.trim(),
        themes: selectedThemes,
        query: customQuery.trim(),
        excluded_urls: excluded
      });
      if (timer) clearInterval(timer);
      researchStage = 'Complete!';
      await new Promise((resolve) => setTimeout(resolve, 200));

      if (proposeMore && results) {
        const existingUrls = new Set(results.map((f) => f.url));
        const newFeeds = resp.feeds.filter((f) => !existingUrls.has(f.url));
        if (newFeeds.length === 0) {
          error = 'No additional feeds found for these criteria.';
        } else {
          results = [...results, ...newFeeds];
          const nextSelected = new Set(selectedUrls);
          for (const nf of newFeeds) {
            nextSelected.add(nf.url);
          }
          selectedUrls = nextSelected;
          subscribeStatus = `Found ${newFeeds.length} additional feed${newFeeds.length === 1 ? '' : 's'}.`;
        }
      } else {
        results = resp.feeds;
        // Pre-select all discovered feeds
        selectedUrls = new Set(resp.feeds.map((f) => f.url));
        if (resp.feeds.length === 0) {
          error = 'No active feeds discovered for these criteria. Try adjusting location or themes.';
        }
      }
    } catch (err) {
      if (timer) clearInterval(timer);
      error = err instanceof Error ? err.message : 'Discovery research failed';
    } finally {
      researching = false;
    }
  }

  function toggleSelection(url: string) {
    const next = new Set(selectedUrls);
    if (next.has(url)) {
      next.delete(url);
    } else {
      next.add(url);
    }
    selectedUrls = next;
  }

  function selectAll() {
    if (results) {
      selectedUrls = new Set(results.map((f) => f.url));
    }
  }

  function deselectAll() {
    selectedUrls = new Set();
  }

  async function handleSubscribe() {
    if (!results || selectedUrls.size === 0) return;
    subscribing = true;
    subscribeStatus = '';
    const toAdd = results.filter((f) => selectedUrls.has(f.url));
    let addedCount = 0;
    let failedCount = 0;

    for (const feed of toAdd) {
      try {
        await api.feeds.create({
          url: feed.url,
          title: feed.title
        });
        addedCount++;
      } catch (err) {
        // Ignore 409 already subscribed
        if (err instanceof Error && err.message.includes('409')) {
          addedCount++;
        } else {
          failedCount++;
        }
      }
    }

    subscribing = false;
    subscribeStatus = `Added ${addedCount} feed${addedCount === 1 ? '' : 's'}` +
      (failedCount > 0 ? ` (${failedCount} failed)` : '');

    if (onAdded) {
      await onAdded();
    }

    setTimeout(() => {
      open = false;
      reset();
    }, 1200);
  }

  function reset() {
    location = '';
    selectedThemes = [];
    customQuery = '';
    results = null;
    error = '';
    subscribeStatus = '';
  }

  function close() {
    open = false;
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape' && open) {
      close();
    }
  }

  function extractHost(feed: DiscoveredFeed): string {
    if (feed.site_url) {
      try {
        return new URL(feed.site_url).hostname;
      } catch {}
    }
    try {
      return new URL(feed.url).hostname;
    } catch {
      return '';
    }
  }
</script>

<svelte:window onkeydown={onKeydown} />

{#if open}
  <button class="backdrop" onclick={close} aria-label="Close dialog" tabindex="-1"></button>

  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
    <div class="header">
      <h2 id="modal-title">
        <span class="sparkle">✨</span> Discover Feeds
      </h2>
      <button class="closebtn" onclick={close} aria-label="Close">✕</button>
    </div>

    {#if !results}
      <!-- Input Formulation Step -->
      <form class="body" onsubmit={(e) => { e.preventDefault(); handleResearch(); }}>
        <p class="subtitle">
          Find high-quality RSS and Atom feeds tailored to your interests and location with LLM research.
        </p>

        <div class="field">
          <label for="discovery-location">
            <strong>Location</strong>
            <span class="hint">(optional — defaults to worldwide)</span>
          </label>
          <div class="input-wrap">
            <span class="prefix">📍</span>
            <input
              id="discovery-location"
              type="text"
              placeholder="e.g. France, Tokyo, Silicon Valley, Nordic..."
              bind:value={location}
              disabled={researching}
            />
          </div>
        </div>

        <div class="field">
          <span class="label">
            <strong>Themes & Categories</strong>
            <span class="hint">(select one or more)</span>
          </span>
          <div class="theme-grid">
            {#each THEMES as theme (theme.id)}
              {@const selected = selectedThemes.includes(theme.id)}
              <button
                type="button"
                class="chip"
                class:selected
                onclick={() => toggleTheme(theme.id)}
                disabled={researching}
              >
                <span class="chip-icon">{theme.icon}</span>
                <span class="chip-text">{theme.label}</span>
              </button>
            {/each}
          </div>
        </div>

        <div class="field">
          <label for="discovery-custom">
            <strong>Custom Request or Focus</strong>
            <span class="hint">(optional)</span>
          </label>
          <input
            id="discovery-custom"
            type="text"
            placeholder="e.g. 'cycling news in France', 'AI research blogs', 'indie game dev'..."
            bind:value={customQuery}
            disabled={researching}
          />
        </div>

        {#if error}
          <div class="error-banner">⚠ {error}</div>
        {/if}

        {#if researching}
          <div class="progress-box">
            <div class="progress-labels">
              <span class="stage-text">✨ {researchStage}</span>
            </div>
            <div class="progress-track indeterminate">
              <div class="progress-fill"></div>
            </div>
          </div>
        {/if}

        <div class="actions">
          <button type="button" class="btn-secondary" onclick={close} disabled={researching}>
            Cancel
          </button>
          <button type="submit" class="btn-primary" disabled={researching}>
            {#if researching}
              <span class="spinner"></span> Researching feeds…
            {:else}
              ✨ Research Feeds
            {/if}
          </button>
        </div>
      </form>
    {:else}
      <!-- Results Candidate Step -->
      <div class="body results-view">
        {#if researching}
          <div class="progress-box">
            <div class="progress-labels">
              <span class="stage-text">✨ {researchStage}</span>
            </div>
            <div class="progress-track indeterminate">
              <div class="progress-fill"></div>
            </div>
          </div>
        {/if}

        <div class="results-header">
          <div>
            <strong>Found {results.length} Candidate Feed{results.length === 1 ? '' : 's'}</strong>
            <span class="results-sub">Selected {selectedUrls.size} of {results.length}</span>
          </div>
          <div class="quick-select">
            <button type="button" class="link-btn" onclick={selectAll}>Select all</button>
            <span class="sep">·</span>
            <button type="button" class="link-btn" onclick={deselectAll}>Deselect all</button>
          </div>
        </div>

        <div class="feed-candidates">
          {#each results as feed (feed.url)}
            {@const host = extractHost(feed)}
            {@const isChecked = selectedUrls.has(feed.url)}
            <div
              class="candidate-card"
              class:checked={isChecked}
              onclick={() => toggleSelection(feed.url)}
              role="button"
              tabindex="0"
              onkeydown={(e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggleSelection(feed.url); } }}
            >
              <div class="card-check">
                <input
                  type="checkbox"
                  checked={isChecked}
                  onclick={(e) => e.stopPropagation()}
                  onchange={() => toggleSelection(feed.url)}
                />
              </div>

              <div class="card-content">
                <div class="card-top">
                  {#if host}
                    <img
                      class="favicon"
                      src={faviconUrl(host)}
                      alt=""
                      loading="lazy"
                      onerror={(e) => (e.currentTarget as HTMLElement).remove()}
                    />
                  {/if}
                  <h3 class="card-title">{feed.title}</h3>
                </div>

                <div class="card-meta-row">
                  {#if feed.access_level === 'paywalled'}
                    <span class="badge badge-paywall" title="Subscription or paid paywall detected">🔒 Paywall</span>
                  {:else if feed.access_level === 'free_full'}
                    <span class="badge badge-free" title="100% Free with full-text articles">🟢 Free (Full)</span>
                  {:else}
                    <span class="badge badge-excerpt" title="Free to access, RSS delivers summary excerpts">🟡 Free (Excerpts)</span>
                  {/if}

                  {#if feed.geographic_scope === 'local'}
                    <span class="badge badge-scope">📍 Local</span>
                  {:else if feed.geographic_scope === 'regional'}
                    <span class="badge badge-scope">🗺️ Regional</span>
                  {:else if feed.geographic_scope === 'national'}
                    <span class="badge badge-scope">🏛️ National</span>
                  {/if}
                </div>

                {#if feed.description}
                  <p class="card-desc">{feed.description}</p>
                {/if}

                {#if feed.match_reason}
                  <div class="card-reason">
                    <span class="reason-pill">🎯 {feed.match_reason}</span>
                  </div>
                {/if}

                <div class="card-url" title={feed.url}>
                  <code>{feed.url}</code>
                </div>

                {#if feed.sample_articles && feed.sample_articles.length > 0}
                  {@const isExpanded = expandedUrls.has(feed.url)}
                  <div class="card-samples-toggle">
                    <button
                      type="button"
                      class="preview-btn"
                      onclick={(e) => { e.stopPropagation(); toggleExpand(feed.url); }}
                    >
                      <span class="chevron">{isExpanded ? '▼' : '▶'}</span>
                      {isExpanded ? 'Hide latest articles' : `Preview latest articles (${feed.sample_articles.length})`}
                    </button>
                  </div>

                  {#if isExpanded}
                    <div class="card-samples-list">
                      {#each feed.sample_articles as article}
                        <div class="sample-item">
                          <span class="sample-icon">📄</span>
                          <div class="sample-text">
                            <span class="sample-title">{article.title}</span>
                            {#if article.published_at}
                              <span class="sample-date">{article.published_at}</span>
                            {/if}
                          </div>
                        </div>
                      {/each}
                    </div>
                  {/if}
                {/if}
              </div>
            </div>
          {/each}
        </div>

        {#if subscribeStatus}
          <div class="ok-banner">{subscribeStatus}</div>
        {/if}
        {#if error}
          <div class="error-banner">⚠ {error}</div>
        {/if}

        <div class="actions results-actions">
          <button
            type="button"
            class="btn-secondary"
            onclick={() => { results = null; error = ''; subscribeStatus = ''; }}
            disabled={subscribing || researching}
          >
            ← Search Again
          </button>
          <button
            type="button"
            class="btn-secondary"
            onclick={() => handleResearch(true)}
            disabled={subscribing || researching}
          >
            {#if researching}
              <span class="spinner"></span> Searching…
            {:else}
              ✨ Propose More
            {/if}
          </button>
          <button
            type="button"
            class="btn-primary"
            onclick={handleSubscribe}
            disabled={subscribing || researching || selectedUrls.size === 0}
          >
            {#if subscribing}
              Subscribing…
            {:else}
              Subscribe to {selectedUrls.size} Feed{selectedUrls.size === 1 ? '' : 's'}
            {/if}
          </button>
        </div>
      </div>
    {/if}
  </div>
{/if}

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(11, 24, 18, 0.55);
    backdrop-filter: blur(4px);
    z-index: 90;
    border: none;
    cursor: default;
  }

  .modal {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: 90vw;
    max-width: 680px;
    max-height: 85vh;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-card);
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.25);
    z-index: 100;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1.2rem 1.4rem;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }

  .header h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 700;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .sparkle {
    color: var(--accent-signal);
  }

  .closebtn {
    background: none;
    border: none;
    font-size: 1.2rem;
    cursor: pointer;
    color: var(--muted);
    padding: 0.3rem 0.5rem;
    border-radius: var(--radius-sm);
  }
  .closebtn:hover {
    color: var(--text);
    background: var(--surface-soft);
  }

  .body {
    padding: 1.4rem;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 1.2rem;
  }

  .subtitle {
    margin: 0;
    font-size: 0.92rem;
    color: var(--text-secondary);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
  }

  .field label, .field .label {
    font-size: 0.9rem;
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
  }

  .hint {
    font-size: 0.8rem;
    color: var(--muted);
  }

  .input-wrap {
    display: flex;
    align-items: center;
    background: var(--surface-soft);
    border: 1px solid var(--border-input);
    border-radius: var(--radius-sm);
    padding: 0 0.6rem;
  }

  .input-wrap .prefix {
    font-size: 0.95rem;
    margin-right: 0.4rem;
  }

  .input-wrap input {
    border: none;
    background: transparent;
    padding: 0.6rem 0;
    width: 100%;
    outline: none;
    font-size: 0.95rem;
    color: var(--text);
  }

  input[type='text'] {
    background: var(--surface-soft);
    border: 1px solid var(--border-input);
    border-radius: var(--radius-sm);
    padding: 0.6rem 0.75rem;
    font-size: 0.95rem;
    color: var(--text);
    outline: none;
  }

  input[type='text']:focus, .input-wrap:focus-within {
    border-color: var(--accent);
  }

  .theme-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .chip {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.35rem 0.65rem;
    border-radius: var(--radius-pill);
    background: var(--surface-soft);
    border: 1px solid var(--border);
    font-size: 0.83rem;
    color: var(--text-secondary);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .chip:hover {
    border-color: var(--border-hover);
    background: var(--chip-bg);
  }

  .chip.selected {
    background: var(--accent);
    color: #ffffff;
    border-color: var(--accent);
    font-weight: 500;
  }

  .chip-icon {
    font-size: 0.9rem;
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
    margin-top: 0.5rem;
  }

  .btn-primary {
    background: var(--accent);
    color: #ffffff;
    border: none;
    padding: 0.65rem 1.2rem;
    border-radius: var(--radius-sm);
    font-weight: 600;
    font-size: 0.95rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .btn-primary:hover:not(:disabled) {
    filter: brightness(1.1);
  }
  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-secondary {
    background: var(--surface-soft);
    color: var(--text);
    border: 1px solid var(--border);
    padding: 0.65rem 1rem;
    border-radius: var(--radius-sm);
    font-size: 0.92rem;
    cursor: pointer;
  }
  .btn-secondary:hover:not(:disabled) {
    background: var(--border);
  }

  .progress-box {
    margin-top: 0.5rem;
    padding: 0.75rem 1rem;
    background: var(--surface-raised, rgba(20, 107, 58, 0.08));
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 8px);
  }

  .progress-labels {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.85rem;
    font-weight: 500;
    margin-bottom: 0.5rem;
    color: var(--gator-forest, #146b3a);
  }

  .progress-track {
    position: relative;
    height: 6px;
    background: var(--border, rgba(0, 0, 0, 0.1));
    border-radius: 999px;
    overflow: hidden;
  }

  .progress-track.indeterminate .progress-fill {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 38%;
    background: var(--gator-forest, #146b3a);
    border-radius: 999px;
    animation: indeterminate-sweep 1.8s cubic-bezier(0.4, 0, 0.2, 1) infinite;
  }

  @keyframes indeterminate-sweep {
    0% {
      left: -38%;
    }
    100% {
      left: 100%;
    }
  }

  .error-banner {
    background: var(--error-bg);
    color: var(--error);
    padding: 0.6rem 0.9rem;
    border-radius: var(--radius-sm);
    font-size: 0.88rem;
  }

  .ok-banner {
    background: var(--ok-bg);
    color: var(--ok);
    padding: 0.6rem 0.9rem;
    border-radius: var(--radius-sm);
    font-size: 0.88rem;
    font-weight: 500;
  }

  .spinner {
    display: inline-block;
    width: 1rem;
    height: 1rem;
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-radius: 50%;
    border-top-color: #ffffff;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* Results view */
  .results-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.6rem;
  }

  .results-sub {
    font-size: 0.82rem;
    color: var(--muted);
    margin-left: 0.5rem;
  }

  .quick-select {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .link-btn {
    background: none;
    border: none;
    padding: 0;
    color: var(--accent);
    font-size: 0.82rem;
    cursor: pointer;
  }
  .link-btn:hover {
    text-decoration: underline;
  }

  .sep {
    color: var(--muted);
    font-size: 0.8rem;
  }

  .feed-candidates {
    display: flex;
    flex-direction: column;
    gap: 0.65rem;
    max-height: 48vh;
    overflow-y: auto;
    padding-right: 0.25rem;
  }

  .candidate-card {
    display: flex;
    gap: 0.8rem;
    padding: 0.8rem 0.9rem;
    background: var(--surface-soft);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all 0.15s ease;
  }

  .candidate-card:hover {
    border-color: var(--border-hover);
  }

  .candidate-card.checked {
    border-color: var(--accent);
    background: var(--chip-bg);
  }

  .card-check {
    padding-top: 0.15rem;
  }

  .card-check input {
    cursor: pointer;
    accent-color: var(--accent);
    width: 1.1rem;
    height: 1.1rem;
  }

  .card-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .card-top {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .card-meta-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    align-items: center;
  }

  .badge {
    font-size: 0.72rem;
    font-weight: 600;
    padding: 0.12rem 0.45rem;
    border-radius: var(--radius-pill);
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
  }

  .badge-free {
    background: rgba(34, 197, 94, 0.12);
    color: #16a34a;
    border: 1px solid rgba(34, 197, 94, 0.3);
  }

  .badge-excerpt {
    background: rgba(234, 179, 8, 0.12);
    color: #ca8a04;
    border: 1px solid rgba(234, 179, 8, 0.3);
  }

  .badge-paywall {
    background: rgba(239, 68, 68, 0.12);
    color: #dc2626;
    border: 1px solid rgba(239, 68, 68, 0.3);
  }

  .badge-scope {
    background: var(--surface);
    color: var(--text-secondary);
    border: 1px solid var(--border);
  }

  .favicon {
    width: 1.1rem;
    height: 1.1rem;
    border-radius: 3px;
    flex-shrink: 0;
  }

  .card-title {
    margin: 0;
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text);
  }

  .card-desc {
    margin: 0;
    font-size: 0.84rem;
    color: var(--text-secondary);
    line-height: 1.35;
  }

  .card-reason {
    margin-top: 0.15rem;
  }

  .reason-pill {
    font-size: 0.76rem;
    background: var(--ok-bg);
    color: var(--ok);
    padding: 0.15rem 0.55rem;
    border-radius: var(--radius-pill);
    font-weight: 500;
  }

  .card-url {
    font-size: 0.74rem;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    margin-top: 0.15rem;
  }

  .card-url code {
    font-family: monospace;
    opacity: 0.85;
  }

  .card-samples-toggle {
    margin-top: 0.2rem;
  }

  .preview-btn {
    background: none;
    border: none;
    padding: 0.2rem 0;
    color: var(--gator-forest, #146b3a);
    font-size: 0.78rem;
    font-weight: 500;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .preview-btn:hover {
    text-decoration: underline;
  }

  .preview-btn .chevron {
    font-size: 0.65rem;
  }

  .card-samples-list {
    margin-top: 0.35rem;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .sample-item {
    display: flex;
    align-items: flex-start;
    gap: 0.4rem;
    padding: 0.35rem 0.5rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm, 6px);
  }

  .sample-icon {
    font-size: 0.75rem;
    margin-top: 0.1rem;
    opacity: 0.7;
  }

  .sample-text {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 0;
  }

  .sample-title {
    font-size: 0.78rem;
    font-weight: 500;
    color: var(--text);
    line-height: 1.3;
  }

  .sample-date {
    font-size: 0.7rem;
    color: var(--muted);
  }

  .results-actions {
    margin-top: 0.4rem;
  }

  @media (max-width: 600px) {
    .modal {
      width: 95vw;
      max-height: 90vh;
    }
  }
</style>
