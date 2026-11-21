<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { t } from '$lib/i18n';
  import type { Topic, TopicDuration, TopicMode, TopicPreview } from '$lib/types';

  let {
    hiddenCount = 0,
    showingHidden = false,
    onchange,
    ontogglehidden
  }: {
    hiddenCount?: number;
    showingHidden?: boolean;
    /** a rule was created: reload stories + counts */
    onchange?: () => void;
    ontogglehidden?: () => void;
  } = $props();

  let topics = $state<Topic[]>([]);
  let selected = $state<Topic | null>(null);
  let preview = $state<TopicPreview | null>(null);
  let duration = $state<TopicDuration>('fade');
  let busy = $state(false);
  let error = $state('');

  const DURATIONS: TopicDuration[] = ['fade', '7d', '30d', 'forever'];

  export async function refresh() {
    try {
      topics = await api.topics.list();
    } catch {
      topics = []; // disabled server-side, or an older server: the strip simply stays hidden
    }
  }

  onMount(refresh);

  async function open(topic: Topic) {
    selected = topic;
    preview = null;
    error = '';
    duration = 'fade';
    try {
      preview = await api.topics.preview(topic.id);
    } catch {
      preview = null;
    }
  }

  function close() {
    selected = null;
  }

  async function apply(mode: TopicMode) {
    if (!selected || busy) return;
    busy = true;
    error = '';
    try {
      await api.topics.createRule(selected.id, mode, duration);
      selected = null;
      await refresh();
      onchange?.();
    } catch (e) {
      error = e instanceof Error ? e.message : $t('topics.error');
    } finally {
      busy = false;
    }
  }

  async function removeRule(topic: Topic) {
    if (!topic.rule || busy) return;
    busy = true;
    try {
      await api.topics.removeRule(topic.rule.id);
      selected = null;
      await refresh();
      onchange?.();
    } catch (e) {
      error = e instanceof Error ? e.message : $t('topics.error');
    } finally {
      busy = false;
    }
  }

  let trending = $derived(topics.filter((x) => !x.rule));
  let ruled = $derived(topics.filter((x) => x.rule));
</script>

{#if topics.length > 0 || hiddenCount > 0}
  <div class="radar" role="group" aria-label={$t('topics.label')}>
    {#each trending as topic (topic.id)}
      <button
        type="button"
        class="chip"
        title={topic.description || topic.label}
        aria-label={$t('topics.chipLabel', { label: topic.label, count: topic.unread_count })}
        onclick={() => open(topic)}
      >
        <span class="name">{topic.label}</span>
        <span class="n">{topic.unread_count}</span>
      </button>
    {/each}
    {#each ruled as topic (topic.id)}
      <button
        type="button"
        class="chip ruled"
        class:follow={topic.rule?.mode === 'follow'}
        title={topic.description || topic.label}
        onclick={() => open(topic)}
      >
        <span aria-hidden="true">{topic.rule?.mode === 'follow' ? '★' : '⊘'}</span>
        <span class="name">{topic.label}</span>
      </button>
    {/each}
    {#if hiddenCount > 0 || showingHidden}
      <button
        type="button"
        class="chip hidden-chip"
        class:on={showingHidden}
        aria-pressed={showingHidden}
        title={$t('topics.hiddenTitle')}
        onclick={() => ontogglehidden?.()}
      >
        {$t('topics.hiddenChip', { count: hiddenCount })}
      </button>
    {/if}
  </div>
{/if}

{#if selected}
  <button class="bg" aria-label={$t('topics.close')} onclick={close} tabindex="-1"></button>
  <div class="sheet" role="dialog" aria-label={selected.label}>
    <h3>{selected.label}</h3>
    {#if selected.description}<p class="desc">{selected.description}</p>{/if}
    <p class="meta">
      {$t('topics.storyCount', { count: selected.story_count })} ·
      {$t('topics.sourceCount', { count: selected.source_count })}
    </p>
    {#if selected.rule}
      <button class="primary" disabled={busy} onclick={() => selected && removeRule(selected)}>
        {selected.rule.mode === 'follow' ? $t('topics.unfollow') : $t('topics.unmute')}
      </button>
    {/if}
    {#if preview && preview.samples.length}
      <p class="hide">{$t('topics.wouldHide', { count: preview.would_hide_unread })}</p>
      <ul>
        {#each preview.samples as s (s.id)}<li>{s.title}</li>{/each}
      </ul>
    {/if}
    <label class="dur">
      {$t('topics.duration')}
      <select bind:value={duration}>
        {#each DURATIONS as d (d)}<option value={d}>{$t('topics.duration_' + d)}</option>{/each}
      </select>
    </label>
    <div class="actions">
      <button class="primary" disabled={busy} onclick={() => apply('mute')}>{$t('topics.mute')}</button>
      <button disabled={busy} onclick={() => apply('follow')}>{$t('topics.follow')}</button>
      <button class="ghost" onclick={close}>{$t('topics.close')}</button>
    </div>
    {#if error}<p class="err">{error}</p>{/if}
  </div>
{/if}

<style>
  .radar {
    display: flex;
    gap: 0.4rem;
    overflow-x: auto;
    scrollbar-width: none;
    padding: 0.1rem 0 0.5rem;
    max-width: 100%;
    min-width: 0;
  }
  .radar::-webkit-scrollbar {
    display: none;
  }
  .chip {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--border);
    background: var(--surface-soft);
    color: var(--text);
    border-radius: var(--radius-pill);
    padding: 0.2rem 0.7rem;
    font-size: 0.82rem;
    font-weight: 600;
    white-space: nowrap;
  }
  .chip:hover {
    border-color: var(--accent);
  }
  .chip:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 1px;
  }
  .n {
    font-size: 0.72em;
    background: var(--border);
    border-radius: var(--radius-pill);
    padding: 0.02rem 0.35rem;
  }
  .ruled {
    color: var(--muted);
  }
  .ruled.follow {
    color: var(--text);
    border-color: var(--gator-gold);
  }
  .hidden-chip {
    color: var(--muted);
    border-style: dashed;
  }
  .hidden-chip.on {
    background: var(--accent);
    color: var(--gator-surface);
    border-style: solid;
  }
  .bg {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.4);
    border: none;
    z-index: 40;
  }
  .sheet {
    position: fixed;
    z-index: 41;
    left: 50%;
    bottom: 0;
    transform: translateX(-50%);
    width: min(32rem, 100%);
    box-sizing: border-box;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-card) var(--radius-card) 0 0;
    padding: 1.1rem 1.2rem calc(1.1rem + env(safe-area-inset-bottom));
    max-height: 80dvh;
    overflow-y: auto;
  }
  h3 {
    margin: 0 0 0.3rem;
  }
  .desc {
    margin: 0 0 0.4rem;
  }
  .meta {
    color: var(--muted);
    font-size: 0.85rem;
    margin: 0 0 0.6rem;
  }
  .hide {
    font-weight: 600;
    margin: 0.6rem 0 0.2rem;
  }
  ul {
    margin: 0 0 0.6rem;
    padding-left: 1.1rem;
    font-size: 0.88rem;
    color: var(--muted);
  }
  .dur {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: 0.85rem;
    margin-bottom: 0.7rem;
  }
  .dur select {
    font-size: 1rem;
    width: 100%;
  }
  .actions {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
  }
  .primary {
    background: var(--accent);
    color: var(--gator-surface);
  }
  .ghost {
    background: transparent;
  }
  .err {
    color: var(--danger, #b3261e);
    font-size: 0.85rem;
  }
</style>
