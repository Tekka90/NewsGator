<script lang="ts">
  import { api } from '$lib/api';
  import { t } from '$lib/i18n';
  import { removeRuleAsking } from '$lib/ruleRemoval';
  import type { Topic, TopicDuration, TopicMode, TopicPreview } from '$lib/types';

  let {
    topic,
    onclose,
    onchange
  }: {
    topic: Topic;
    onclose: () => void;
    /** a rule was created or removed */
    onchange?: () => void | Promise<void>;
  } = $props();

  let preview = $state<TopicPreview | null>(null);
  let duration = $state<TopicDuration>('fade');
  let busy = $state(false);
  let error = $state('');

  const DURATIONS: TopicDuration[] = ['fade', '7d', '30d', 'forever'];

  $effect(() => {
    const id = topic.id;
    preview = null;
    error = '';
    duration = 'fade';
    if (topic.manual) return;
    api.topics
      .preview(id)
      .then((p) => {
        if (id === topic.id) preview = p;
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

  const apply = (mode: TopicMode) => run(() => api.topics.createRule(topic.id, mode, duration));
  const setMode = (mode: TopicMode) =>
    topic.rule && topic.rule.mode !== mode ? run(() => api.topics.updateRule(topic.rule!.id, mode)) : undefined;
  const removeManual = () => (topic.rule ? run(() => removeRuleAsking(topic.rule!.id)) : undefined);
  const removeRule = (tp: Topic) => (tp.rule ? run(() => removeRuleAsking(tp.rule!.id)) : undefined);
</script>

<svelte:window onkeydown={(e) => e.key === 'Escape' && onclose()} />

<button class="bg" aria-label={$t('topics.close')} onclick={onclose} tabindex="-1"></button>
<div class="sheet" role="dialog" aria-label={topic.label}>
  <h3>{topic.label}</h3>
  {#if topic.manual}
    <p class="meta">
      {$t('topics.storyCount', { count: topic.story_count })} ·
      {$t('topics.unreadCount', { count: topic.unread_count })}
    </p>
    <div class="actions" role="radiogroup" aria-label={$t('topics.addMode')}>
      <button
        role="radio"
        aria-checked={topic.rule?.mode === 'follow'}
        class:primary={topic.rule?.mode === 'follow'}
        disabled={busy}
        onclick={() => setMode('follow')}>{$t('topics.follow')}</button>
      <button
        role="radio"
        aria-checked={topic.rule?.mode === 'mute'}
        class:primary={topic.rule?.mode === 'mute'}
        disabled={busy}
        onclick={() => setMode('mute')}>{$t('topics.mute')}</button>
    </div>
    <div class="actions manualactions">
      <button disabled={busy} onclick={removeManual}>{$t('topics.removeTopic')}</button>
      <button class="ghost" onclick={onclose}>{$t('topics.close')}</button>
    </div>
    {#if error}<p class="err">{error}</p>{/if}
  {:else}
  {#if topic.description}<p class="desc">{topic.description}</p>{/if}
  <p class="meta">
    {$t('topics.primaryCount', { count: topic.story_count, unread: topic.unread_count })} ·
    {$t('topics.coveredCount', {
      count: topic.covered_story_count ?? topic.story_count,
      unread: topic.covered_unread_count ?? topic.unread_count
    })} ·
    {$t('topics.sourceCount', { count: topic.source_count })}
  </p>
  {#if topic.rule}
    <button class="primary" disabled={busy} onclick={() => removeRule(topic)}>
      {topic.rule.mode === 'follow' ? $t('topics.unfollow') : $t('topics.unmute')}
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
    <button class="ghost" onclick={onclose}>{$t('topics.close')}</button>
  </div>
  {#if error}<p class="err">{error}</p>{/if}
  {/if}
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
  .manualactions {
    margin-top: 0.6rem;
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
