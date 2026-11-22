<script lang="ts">
  import { api } from '$lib/api';
  import { t } from '$lib/i18n';
  import { removeRuleAsking } from '$lib/ruleRemoval';
  import { groupCss } from '$lib/constellation';
  import type { TopicDuration, TopicGroup, TopicMode, TopicPreview } from '$lib/types';

  let {
    group,
    onclose,
    onchange,
    onzoom
  }: {
    group: TopicGroup;
    onclose: () => void;
    /** a rule was created or removed */
    onchange?: () => void | Promise<void>;
    onzoom?: () => void;
  } = $props();

  let preview = $state<TopicPreview | null>(null);
  let duration = $state<TopicDuration>('fade');
  let busy = $state(false);
  let error = $state('');

  const DURATIONS: TopicDuration[] = ['fade', '7d', '30d', 'forever'];

  $effect(() => {
    const id = group.id;
    preview = null;
    error = '';
    duration = 'fade';
    api.topicGroups
      .preview(id)
      .then((p) => {
        if (id === group.id) preview = p;
      })
      .catch(() => {
        preview = null;
      });
  });

  async function run(action: () => Promise<unknown>) {
    if (busy) return;
    busy = true;
    error = '';
    try {
      await action();
      onclose();
      await onchange?.();
    } catch (e) {
      error = e instanceof Error ? e.message : $t('topics.error');
    } finally {
      busy = false;
    }
  }

  const apply = (mode: TopicMode) => run(() => api.topicGroups.createRule(group.id, mode, duration));
  const removeRule = () => (group.rule ? run(() => removeRuleAsking(group.rule!.id)) : undefined);
</script>

<svelte:window onkeydown={(e) => e.key === 'Escape' && onclose()} />

<button class="bg" aria-label={$t('topics.close')} onclick={onclose} tabindex="-1"></button>
<div class="sheet" role="dialog" aria-label={group.label}>
  <h3><span class="swatch" style:background={groupCss(group.hue)}></span>{group.label}</h3>
  {#if group.description}<p class="desc">{group.description}</p>{/if}
  <p class="meta">
    {$t('topics.topicCount', { count: group.topic_count })} ·
    {$t('topics.storyCount', { count: group.story_count })} ·
    {$t('topics.unreadCount', { count: group.unread_count })}
  </p>
  {#if group.rule}
    <button class="primary" disabled={busy} onclick={removeRule}>
      {group.rule.mode === 'follow' ? $t('topics.unfollow') : $t('topics.unmute')}
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
      {#each DURATIONS as d (d)}
        <option value={d}>{$t(d === 'fade' ? 'topics.durationFadeGroup' : 'topics.duration_' + d)}</option>
      {/each}
    </select>
  </label>
  <div class="actions">
    <button class="primary" disabled={busy} onclick={() => apply('mute')}>{$t('topics.muteGroup')}</button>
    <button disabled={busy} onclick={() => apply('follow')}>{$t('topics.followGroup')}</button>
    {#if onzoom}<button disabled={busy} onclick={onzoom}>{$t('topics.zoomIn')}</button>{/if}
    <button class="ghost" onclick={onclose}>{$t('topics.close')}</button>
  </div>
  {#if error}<p class="err">{error}</p>{/if}
</div>

<style>
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
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .swatch {
    width: 0.8rem;
    height: 0.8rem;
    border-radius: 50%;
    flex: none;
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
