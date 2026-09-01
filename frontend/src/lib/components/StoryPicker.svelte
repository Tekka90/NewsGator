<script lang="ts">
  /** Searchable story picker for merge/move targets on the story detail page.
   *
   *  Candidates come from the proximity-ranked endpoints (centroid cosine for
   *  merge, article-embedding cosine for move) and arrive best-first; the
   *  exact score is shown as a % badge (null = unscored recency fallback).
   *  Typing filters by title substring. Candidates are fetched lazily on first
   *  open via the `load` prop, so a story with many sources costs no requests
   *  until a picker is actually used.
   *
   *  Two-step flow like the <select> it replaces: picking only sets
   *  `selected`; the parent executes with its own Move/Merge button. Typing
   *  after a pick clears the selection so the button can't act on a stale id. */
  import type { SimilarStory } from '$lib/types';

  let {
    load,
    placeholder = 'Select story…',
    selected = $bindable(null)
  }: {
    load: () => Promise<SimilarStory[]>;
    placeholder?: string;
    selected?: SimilarStory | null;
  } = $props();

  let open = $state(false);
  let query = $state('');
  let candidates = $state<SimilarStory[] | null>(null);
  let loading = $state(false);
  let error = $state('');
  let highlight = $state(0);
  let rootEl: HTMLDivElement | undefined = $state();

  // unique per instance — several pickers coexist on the story detail page
  const menuId = `story-picker-${Math.random().toString(36).slice(2)}`;

  const filtered = $derived(
    (candidates ?? []).filter((c) =>
      c.title.toLowerCase().includes(query.trim().toLowerCase())
    )
  );

  async function openPicker() {
    open = true;
    error = '';
    if (candidates || loading) return;
    loading = true;
    try {
      candidates = await load();
    } catch (e) {
      error = e instanceof Error ? e.message : 'Load failed';
    } finally {
      loading = false;
    }
  }

  function onInput() {
    selected = null; // text no longer reflects a pick — disarm the action button
    void openPicker();
  }

  function pick(c: SimilarStory) {
    selected = c;
    query = c.title;
    open = false;
  }

  function onkeydown(e: KeyboardEvent) {
    if (e.key === 'Escape') {
      open = false;
      return;
    }
    if (!open && (e.key === 'ArrowDown' || e.key === 'Enter')) {
      e.preventDefault();
      void openPicker();
      return;
    }
    if (!open) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      highlight = Math.min(highlight + 1, filtered.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      highlight = Math.max(highlight - 1, 0);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const c = filtered[highlight];
      if (c) pick(c);
    }
  }

  function onWindowClick(e: MouseEvent) {
    if (open && rootEl && !rootEl.contains(e.target as Node)) open = false;
  }

  // Reset the keyboard highlight whenever the filter text changes.
  $effect(() => {
    void query;
    highlight = 0;
  });
</script>

<svelte:window onclick={onWindowClick} />

<div class="picker" bind:this={rootEl}>
  <input
    type="text"
    {placeholder}
    bind:value={query}
    onfocus={openPicker}
    oninput={onInput}
    {onkeydown}
    role="combobox"
    aria-expanded={open}
    aria-controls={menuId}
    aria-label={placeholder}
    autocomplete="off"
  />
  {#if open}
    <div class="menu" role="listbox" id={menuId}>
      {#if loading}
        <div class="item muted">Loading…</div>
      {:else if error}
        <div class="item warn">{error}</div>
      {:else if filtered.length === 0}
        <div class="item muted">No matching story</div>
      {:else}
        {#each filtered as c, i (c.id)}
          <button
            type="button"
            class="item"
            class:active={i === highlight}
            role="option"
            aria-selected={i === highlight}
            onmouseenter={() => (highlight = i)}
            onclick={() => pick(c)}
          >
            <span class="name">{c.title}</span>
            {#if c.similarity !== null}
              <span class="score">{Math.round(c.similarity * 100)}%</span>
            {/if}
          </button>
        {/each}
      {/if}
    </div>
  {/if}
</div>

<style>
  .picker { position: relative; flex: 1 1 14rem; min-width: 0; max-width: 100%; }
  .picker input { width: 100%; box-sizing: border-box; }
  .menu {
    position: absolute; top: 100%; left: 0; right: 0; z-index: 30;
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    max-height: 16rem; overflow-y: auto; margin-top: 2px;
    box-shadow: 0 4px 16px rgb(0 0 0 / 0.25);
  }
  .item {
    display: flex; align-items: baseline; gap: 0.5rem; width: 100%;
    padding: 0.45rem 0.6rem; border: none; border-radius: 0; background: none;
    text-align: left; color: var(--text); font: inherit; cursor: pointer;
  }
  .item.active { background: var(--chip-bg); }
  .name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .score {
    flex-shrink: 0; font-size: 0.75em;
    background: var(--chip-bg); color: var(--accent);
    padding: 0.05rem 0.4rem; border-radius: 999px;
  }
  .muted { color: var(--muted); cursor: default; }
  .warn { color: var(--error); cursor: default; }
</style>
