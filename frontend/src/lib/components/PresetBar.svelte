<script lang="ts">
  import { tick } from 'svelte';
  import PresetIcon from './PresetIcon.svelte';
  import { effectiveDisplay, enabledPresets, presetLabel } from '$lib/presets';
  import type { StoryPreset } from '$lib/types';

  let {
    presets,
    activeId,
    counts = {},
    compact = false,
    onselect,
    label = 'Story views'
  }: {
    presets: StoryPreset[];
    activeId: string | null;
    counts?: Record<string, number | undefined>;
    compact?: boolean;
    onselect?: (p: StoryPreset) => void;
    label?: string;
  } = $props();

  let shown = $derived(enabledPresets(presets));
  let bar = $state<HTMLElement>();

  // keep the active pill visible when the bar scrolls horizontally
  $effect(() => {
    void activeId;
    void tick().then(() => {
      bar?.querySelector<HTMLElement>('[aria-pressed="true"]')?.scrollIntoView({
        block: 'nearest',
        inline: 'nearest'
      });
    });
  });

  // ←/→/Home/End move focus between views (buttons stay in the tab order)
  function onkeydown(e: KeyboardEvent) {
    const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
    if (!keys.includes(e.key) || !bar) return;
    const items = [...bar.querySelectorAll<HTMLElement>('button')];
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next =
      e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : at + (e.key === 'ArrowRight' ? 1 : -1);
    const target = items[(next + items.length) % items.length];
    if (target) {
      e.preventDefault();
      target.focus();
    }
  }
</script>

<div class="presetbar" role="group" aria-label={label} bind:this={bar}>
  {#each shown as p (p.id)}
    {@const display = effectiveDisplay(p, compact)}
    {@const name = presetLabel(p)}
    {@const count = p.show_count ? counts[p.id] : undefined}
    <button
      type="button"
      class:active={p.id === activeId}
      class:icononly={display === 'icon'}
      aria-pressed={p.id === activeId}
      aria-label={count !== undefined ? `${name}, ${count}` : name}
      title={name}
      onclick={() => onselect?.(p)}
      {onkeydown}
    >
      {#if display !== 'label'}<PresetIcon icon={p.icon} />{/if}
      {#if display !== 'icon'}<span class="name">{name}</span>{/if}
      {#if count !== undefined}<span class="pill-count">{count}</span>{/if}
    </button>
  {/each}
</div>

<style>
  .presetbar {
    display: inline-flex;
    gap: 0.2rem;
    background: var(--surface-soft);
    padding: 0.22rem;
    border-radius: var(--radius-pill);
    border: 1px solid var(--border);
    max-width: 100%;
    min-width: 0;
    overflow-x: auto;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
  }
  .presetbar::-webkit-scrollbar {
    display: none;
  }
  button {
    border: none;
    background: transparent;
    color: var(--muted);
    border-radius: var(--radius-pill);
    padding: 0.25rem 0.85rem;
    font-size: 0.85rem;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    flex: 0 0 auto;
    white-space: nowrap;
    transition: background 0.15s ease, color 0.15s ease, box-shadow 0.15s ease;
  }
  button.icononly {
    padding: 0.25rem 0.6rem;
  }
  button:hover {
    color: var(--text);
  }
  button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
  button.active {
    background: var(--accent);
    color: var(--gator-surface);
    box-shadow: 0 2px 6px rgba(20, 107, 58, 0.22);
  }
  .pill-count {
    font-size: 0.75em;
    font-weight: 700;
    opacity: 0.9;
    background: var(--border);
    color: var(--text);
    padding: 0.05rem 0.35rem;
    border-radius: var(--radius-pill);
  }
  button.active .pill-count {
    background: rgba(255, 255, 255, 0.25);
    color: inherit;
  }
</style>
