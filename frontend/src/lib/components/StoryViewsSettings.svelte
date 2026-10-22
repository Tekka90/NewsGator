<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import PresetBar from './PresetBar.svelte';
  import PresetIcon from './PresetIcon.svelte';
  import { ICON_KEYS, isEmojiIcon } from '$lib/presetIcons';
  import {
    SORT_LABELS,
    MAX_LABEL,
    MAX_QUERY,
    canDisable,
    duplicatePreset,
    movePreset,
    newCustomPreset,
    normalizeForSave,
    presetLabel,
    removePreset,
    setEnabled,
    sortedPresets,
    toggleInList,
    validateDraft
  } from '$lib/presets';
  import type { Feed, PresetSourceKind, PresetsDoc, StoryPreset } from '$lib/types';

  const KIND_LABELS: Record<PresetSourceKind, string> = {
    rss: 'RSS',
    mail: 'Newsletter',
    youtube: 'YouTube',
    saved: 'Saved page',
    reader_api: 'Reader account'
  };
  const YOUTUBE_HINT = 'YouTube: videos from YouTube channels or links shared from YouTube';

  let draft = $state<StoryPreset[]>([]);
  let compact = $state(false);
  let lastId = $state<string | null>(null);
  let baseline = $state('');
  let counts = $state<Record<string, number>>({});
  let feeds = $state<Feed[]>([]);
  let categoryNames = $state<string[]>([]);
  let editingId = $state<string | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let error = $state('');
  let status = $state('');
  let dragFrom = $state<number | null>(null);
  let dragOver = $state<number | null>(null);
  let emojiText = $state('');
  let emojiError = $state(false);

  let ordered = $derived(sortedPresets(draft));
  let editing = $derived(ordered.find((p) => p.id === editingId) ?? null);
  let dirty = $derived(snapshot() !== baseline);
  let known = $derived(new Set(feeds.map((f) => f.id)));

  function snapshot(): string {
    return JSON.stringify({ p: normalizeForSave($state.snapshot(draft) as StoryPreset[]), compact });
  }

  function apply(doc: PresetsDoc) {
    draft = sortedPresets(doc.presets);
    compact = doc.compact;
    lastId = doc.last_preset_id;
    baseline = snapshot();
    if (editingId && !draft.some((p) => p.id === editingId)) editingId = null;
  }

  async function refreshCounts() {
    counts = (await api.presets.counts().catch(() => ({ counts: {} }))).counts;
  }

  onMount(async () => {
    try {
      apply(await api.presets.get());
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
    loading = false;
    void refreshCounts();
    api.feeds.list().then((f) => (feeds = f)).catch(() => {});
    api.stories
      .facets()
      .then((f) => (categoryNames = f.categories.map((c) => c.key).sort()))
      .catch(() => {});
  });

  function edit(p: StoryPreset) {
    editingId = editingId === p.id ? null : p.id;
    emojiText = '';
    emojiError = false;
  }

  function toggleEnabled(p: StoryPreset, enabled: boolean) {
    draft = setEnabled(draft, p.id, enabled);
  }

  function pickEmoji() {
    const v = emojiText.trim();
    if (!editing || !v) return;
    if (isEmojiIcon(v)) {
      editing.icon = v;
      emojiError = false;
    } else emojiError = true;
  }

  function add() {
    const p = newCustomPreset(draft.length);
    draft = [...ordered, p];
    editingId = p.id;
  }

  function onDrop(to: number) {
    if (dragFrom !== null && dragFrom !== to) draft = movePreset(draft, dragFrom, to);
    dragFrom = dragOver = null;
  }

  async function save() {
    error = status = '';
    const problem = validateDraft(draft);
    if (problem) {
      error = problem;
      return;
    }
    busy = true;
    try {
      apply(await api.presets.save({ presets: normalizeForSave($state.snapshot(draft) as StoryPreset[]), last_preset_id: lastId, compact }));
      status = 'Saved ✓';
      void refreshCounts();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function reset(scope: 'builtin' | 'all') {
    const msg =
      scope === 'all'
        ? 'Delete all your custom views and restore the four default views? This cannot be undone.'
        : 'Restore the four default views (names, icons, order, sorting)? Your custom views are kept.';
    if (!confirm(msg)) return;
    error = status = '';
    busy = true;
    try {
      apply(await api.presets.reset(scope));
      status = 'Reset ✓';
      void refreshCounts();
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }
</script>

<div class="card">
  <h2>Story views</h2>
  <p class="hint">
    Views are the buttons above the story list. Each has its own filter, sort order and icon. The sort
    menu and the feed and category filters on the Stories page stay as temporary overrides on top of the
    selected view.
  </p>

  {#if loading}
    <p class="hint">Loading…</p>
  {:else}
    <div class="preview">
      <span class="hint">Preview</span>
      <PresetBar presets={draft} activeId={editingId ?? ordered.find((p) => p.enabled)?.id ?? null} {counts} {compact} label="Preview of the story views bar" />
      {#if editing}
        <span class="hint">
          Matching stories: {editing.show_count && counts[editing.id] !== undefined ? counts[editing.id] : '—'}{dirty ? ' (saved version — save to refresh)' : ''}
        </span>
      {/if}
    </div>

    <ul class="views">
      {#each ordered as p, i (p.id)}
        <li
          class:dragover={dragOver === i && dragFrom !== i}
          class:off={!p.enabled}
          ondragover={(e) => { if (dragFrom !== null) { e.preventDefault(); dragOver = i; } }}
          ondrop={(e) => { e.preventDefault(); onDrop(i); }}
        >
          <div class="row">
            <span
              class="handle"
              draggable="true"
              role="img"
              aria-label="Drag to reorder"
              title="Drag to reorder"
              ondragstart={(e) => { dragFrom = i; e.dataTransfer?.setData('text/plain', p.id); }}
              ondragend={() => (dragFrom = dragOver = null)}
            >⠿</span>
            <span class="glyph"><PresetIcon icon={p.icon} size={16} /></span>
            <span class="vname">
              {presetLabel(p)}
              {#if p.kind === 'builtin'}<span class="tag">built-in</span>{/if}
            </span>
            <span class="actions">
              <button type="button" class="ghost" disabled={i === 0} aria-label={`Move ${presetLabel(p)} up`} title="Move up" onclick={() => (draft = movePreset(draft, i, i - 1))}>↑</button>
              <button type="button" class="ghost" disabled={i === ordered.length - 1} aria-label={`Move ${presetLabel(p)} down`} title="Move down" onclick={() => (draft = movePreset(draft, i, i + 1))}>↓</button>
              <label class="switch" title={!p.enabled || canDisable(draft, p.id) ? 'Show in the bar' : 'At least one view must stay enabled'}>
                <input type="checkbox" checked={p.enabled} disabled={p.enabled && !canDisable(draft, p.id)} onchange={(e) => toggleEnabled(p, e.currentTarget.checked)} aria-label={`Show ${presetLabel(p)} in the bar`} />
                Shown
              </label>
              <button type="button" aria-expanded={editingId === p.id} onclick={() => edit(p)}>{editingId === p.id ? 'Close' : 'Edit'}</button>
              <button type="button" onclick={() => { draft = duplicatePreset(draft, p.id); }}>Duplicate</button>
              {#if p.kind === 'custom'}
                <button type="button" class="danger" disabled={!canDisable(draft, p.id)} onclick={() => { if (editingId === p.id) editingId = null; draft = removePreset(draft, p.id); }}>Delete</button>
              {/if}
            </span>
          </div>

          {#if editingId === p.id}
            {@const e = p}
            <div class="editor">
              <label>
                Name {#if p.kind === 'builtin'}<span class="hint">(empty = default name)</span>{/if}
                <input bind:value={e.label} maxlength={MAX_LABEL} placeholder={p.kind === 'builtin' ? presetLabel({ ...p, label: null }) : 'Name'} />
              </label>

              <fieldset>
                <legend>Icon</legend>
                <div class="icons" role="group" aria-label="Icon">
                  {#each ICON_KEYS as key (key)}
                    <button type="button" class="ico" class:sel={e.icon === key} aria-pressed={e.icon === key} aria-label={key} title={key} onclick={() => (e.icon = key)}>
                      <PresetIcon icon={key} size={18} />
                    </button>
                  {/each}
                </div>
                <div class="emojirow">
                  <input bind:value={emojiText} placeholder="…or one emoji" size="10" aria-label="Emoji icon" aria-invalid={emojiError} onchange={pickEmoji} onkeydown={(ev) => { if (ev.key === 'Enter') { ev.preventDefault(); pickEmoji(); } }} />
                  <button type="button" onclick={pickEmoji}>Use emoji</button>
                  {#if emojiError}<span class="err">Enter a single emoji.</span>{/if}
                </div>
              </fieldset>

              <div class="grid">
                <label>
                  Display
                  <select bind:value={e.display}>
                    <option value="icon+label">Icon + name</option>
                    <option value="icon">Icon only</option>
                    <option value="label">Name only</option>
                  </select>
                </label>
                <label>
                  Sort by
                  <select bind:value={e.sort}>
                    {#each Object.entries(SORT_LABELS) as [value, text] (value)}<option {value}>{text}</option>{/each}
                  </select>
                </label>
                <label>
                  Order
                  <select bind:value={e.order}>
                    <option value="desc">Newest / most first</option>
                    <option value="asc">Oldest / least first</option>
                  </select>
                </label>
              </div>
              <label class="check"><input type="checkbox" bind:checked={e.show_count} /> Show the story count</label>

              {#if p.kind === 'custom'}
                <div class="grid">
                  <label>
                    Status
                    <select bind:value={e.criteria.status}>
                      <option value="any">Any</option>
                      <option value="unread">Unread</option>
                      <option value="updated">Updated</option>
                      <option value="saved">Saved</option>
                    </select>
                  </label>
                  <label>
                    Max age (days since last update)
                    <input type="number" min="1" max="3650" bind:value={e.criteria.max_age_days} placeholder="No limit" />
                  </label>
                </div>
                <label>
                  Keywords
                  <input bind:value={e.criteria.query} maxlength={MAX_QUERY} placeholder="e.g. electric bike" />
                  <span class="hint">All words must appear in the title or summary. Case and accents are ignored.</span>
                </label>
                <fieldset>
                  <legend>Source types <span class="hint">(any of)</span></legend>
                  <div class="checks">
                    {#each Object.entries(KIND_LABELS) as [kind, text] (kind)}
                      <label class="check"><input type="checkbox" checked={e.criteria.source_kinds.includes(kind as PresetSourceKind)} onchange={() => (e.criteria.source_kinds = toggleInList(e.criteria.source_kinds, kind as PresetSourceKind))} /> {text}</label>
                    {/each}
                  </div>
                  <span class="hint">{YOUTUBE_HINT}.</span>
                </fieldset>
                <fieldset>
                  <legend>Feeds <span class="hint">(any of; none = all)</span></legend>
                  <div class="checks scroll">
                    {#each feeds as f (f.id)}
                      <label class="check"><input type="checkbox" checked={e.criteria.feed_ids.includes(f.id)} onchange={() => (e.criteria.feed_ids = toggleInList(e.criteria.feed_ids, f.id))} /> {f.title || f.url}</label>
                    {:else}
                      <span class="hint">No feeds.</span>
                    {/each}
                    {#each e.criteria.feed_ids.filter((id) => feeds.length && !known.has(id)) as id (id)}
                      <label class="check"><input type="checkbox" checked onchange={() => (e.criteria.feed_ids = toggleInList(e.criteria.feed_ids, id))} /> Feed #{id} (unavailable)</label>
                    {/each}
                  </div>
                </fieldset>
                <fieldset>
                  <legend>Categories <span class="hint">(any of; none = all)</span></legend>
                  <div class="checks">
                    {#each [...new Set([...categoryNames, ...e.criteria.category_ids])].sort() as c (c)}
                      <label class="check"><input type="checkbox" checked={e.criteria.category_ids.includes(c)} onchange={() => (e.criteria.category_ids = toggleInList(e.criteria.category_ids, c))} /> {c}</label>
                    {:else}
                      <span class="hint">No categories yet.</span>
                    {/each}
                  </div>
                </fieldset>
              {:else}
                <p class="hint">Built-in views have a fixed filter; you can change their name, icon, display and sorting.</p>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ul>

    <div class="footer">
      <button type="button" onclick={add}>Add custom view</button>
      <label class="check inline"><input type="checkbox" bind:checked={compact} /> Compact bar (icons only)</label>
    </div>

    <div class="footer">
      <button type="button" class="primary" disabled={busy || !dirty} onclick={save}>Save views</button>
      {#if dirty}<button type="button" disabled={busy} onclick={() => { error = status = ''; void api.presets.get().then(apply); }}>Discard changes</button>{/if}
      <span class="spacer"></span>
      <button type="button" disabled={busy} onclick={() => reset('builtin')}>Reset built-ins to defaults</button>
      <button type="button" class="danger" disabled={busy} onclick={() => reset('all')}>Reset everything</button>
    </div>
    <div role="status" aria-live="polite">
      {#if error}<p class="err">{error}</p>{/if}
      {#if status}<span class="ok">{status}</span>{/if}
    </div>
  {/if}
</div>

<style>
  .preview { display: flex; flex-wrap: wrap; align-items: center; gap: 0.6rem; margin: 0.6rem 0; min-width: 0; }
  ul.views { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.4rem; }
  li { border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface); padding: 0.4rem 0.6rem; }
  li.dragover { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent) inset; }
  li.off .vname { color: var(--muted); }
  .row { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .handle { cursor: grab; color: var(--faint); user-select: none; padding: 0 0.2rem; }
  .glyph { color: var(--accent); display: inline-flex; width: 1.4rem; justify-content: center; }
  .vname { flex: 1 1 8rem; min-width: 0; font-weight: 600; overflow-wrap: anywhere; }
  .tag { font-size: 0.7rem; font-weight: 600; color: var(--muted); background: var(--chip-bg); border-radius: var(--radius-pill); padding: 0.05rem 0.45rem; margin-left: 0.3rem; }
  .actions { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 0.3rem; }
  .actions button, .footer button, .emojirow button {
    border: 1px solid var(--border-strong); background: var(--surface); color: inherit;
    border-radius: var(--radius-pill); padding: 0.2rem 0.7rem; font-size: 0.85rem;
  }
  button:disabled { background: var(--disabled-bg); color: var(--disabled-text); cursor: default; }
  .actions .ghost { padding: 0.2rem 0.5rem; }
  button.primary { background: var(--accent); color: var(--gator-surface); border-color: var(--accent); }
  button.danger { color: var(--error); border-color: var(--error); }
  .switch { display: inline-flex; align-items: center; gap: 0.25rem; margin: 0; font-size: 0.85rem; }
  .editor { margin-top: 0.6rem; padding-top: 0.6rem; border-top: 1px solid var(--row-border); }
  fieldset { border: 1px solid var(--border); border-radius: var(--radius-md); margin: 0.6rem 0 0; padding: 0.4rem 0.6rem 0.6rem; min-width: 0; }
  legend { font-size: 0.85rem; font-weight: 600; padding: 0 0.3rem; }
  .icons { display: grid; grid-template-columns: repeat(auto-fill, minmax(2.4rem, 1fr)); gap: 0.25rem; }
  .ico { border: 1px solid var(--border); background: var(--surface); color: var(--text); border-radius: var(--radius-sm); padding: 0.4rem 0; display: inline-flex; justify-content: center; }
  .ico:hover { border-color: var(--border-hover); }
  .ico.sel { background: var(--accent); color: var(--gator-surface); border-color: var(--accent); }
  .emojirow { display: flex; flex-wrap: wrap; align-items: center; gap: 0.4rem; margin-top: 0.5rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr)); gap: 0.6rem; }
  .grid select, .grid input, .editor > label input:not([type='checkbox']) { width: 100%; }
  .check { display: flex; align-items: center; gap: 0.4rem; margin: 0.25rem 0; font-weight: 400; }
  .check.inline { margin: 0; }
  .checks { display: flex; flex-wrap: wrap; gap: 0.2rem 1rem; }
  .checks.scroll { max-height: 11rem; overflow-y: auto; }
  .footer { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; margin-top: 0.8rem; }
  .spacer { flex: 1 1 auto; }
  .err { color: var(--error); margin: 0.4rem 0 0; }
  .ok { color: var(--ok); }
  .hint { color: var(--muted); font-size: 0.85rem; font-weight: 400; }
</style>
