<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { t } from '$lib/i18n';
  import type { TopicRule } from '$lib/types';

  let rules = $state<TopicRule[]>([]);
  let loaded = $state(false);

  async function load() {
    try {
      rules = await api.topics.rules();
    } catch {
      rules = [];
    }
    loaded = true;
  }

  async function remove(rule: TopicRule) {
    await api.topics.removeRule(rule.id);
    rules = rules.filter((r) => r.id !== rule.id);
  }

  onMount(load);
</script>

{#if loaded && rules.length > 0}
  <div class="card">
    <h2>{$t('topics.rulesTitle')}</h2>
    <p class="hint">{$t('topics.rulesHelp')}</p>
    <ul>
      {#each rules as rule (rule.id)}
        <li>
          <span class="label">{rule.mode === 'follow' ? '★' : '⊘'} {rule.label}</span>
          <span class="hint">
            {rule.until_fade
              ? $t('topics.duration_fade')
              : rule.expires_at
                ? $t('topics.until', { date: rule.expires_at.slice(0, 10) })
                : $t('topics.duration_forever')}
            · {$t('topics.matched', { count: rule.matched_stories })}
          </span>
          <button onclick={() => remove(rule)}>
            {rule.mode === 'follow' ? $t('topics.unfollow') : $t('topics.unmute')}
          </button>
        </li>
      {/each}
    </ul>
  </div>
{/if}

<style>
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  li {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
    padding: 0.35rem 0;
    border-top: 1px solid var(--border);
  }
  .label {
    font-weight: 600;
    flex: 1 1 10rem;
  }
</style>
