<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import type { UsageByFeed, UsageDailyRow, UsageSummary } from '$lib/types';
  import { t, fmt, tNow } from '$lib/i18n';

  let summaries = $state<Partial<Record<'day' | 'month' | 'all', UsageSummary>>>({});
  let dailyRows = $state<UsageDailyRow[]>([]);
  let byFeed = $state<UsageByFeed['feeds']>([]);
  let error = $state('');

  // Price playground: NOT a server setting — kept in localStorage so you can
  // play live with "what would this cost on provider X / model Y".
  const PRICE_KEYS = {
    input: 'newsgator_price_input',
    output: 'newsgator_price_output',
    embed: 'newsgator_price_embed'
  };
  let priceInput = $state(''); // $ per 1M prompt tokens (chat)
  let priceOutput = $state(''); // $ per 1M completion tokens (chat)
  let priceEmbed = $state(''); // $ per 1M tokens (embeddings)

  onMount(async () => {
    if (!$currentUser?.is_admin) return;
    priceInput = localStorage.getItem(PRICE_KEYS.input) ?? '';
    priceOutput = localStorage.getItem(PRICE_KEYS.output) ?? '';
    priceEmbed = localStorage.getItem(PRICE_KEYS.embed) ?? '';
    try {
      const [day, month, all, daily, feeds] = await Promise.all([
        api.usage.summary('day'),
        api.usage.summary('month'),
        api.usage.summary('all'),
        api.usage.daily(90),
        api.usage.byFeed()
      ]);
      summaries = { day, month, all };
      dailyRows = daily.rows;
      byFeed = feeds.feeds;
    } catch (e) {
      error = e instanceof Error ? e.message : tNow('usage.loadFailed');
    }
  });

  function savePrices() {
    localStorage.setItem(PRICE_KEYS.input, priceInput);
    localStorage.setItem(PRICE_KEYS.output, priceOutput);
    localStorage.setItem(PRICE_KEYS.embed, priceEmbed);
  }

  function num(s: string): number {
    const n = parseFloat(s);
    return Number.isFinite(n) && n >= 0 ? n : 0;
  }

  /** Estimated cost ($) for one summary's totals, using the price inputs. */
  function cost(s: UsageSummary | undefined): number | null {
    if (!s) return null;
    const chatIn = s.by_kind
      .filter((k) => k.endpoint === 'chat')
      .reduce((a, k) => a + k.prompt_tokens, 0);
    const chatOut = s.by_kind
      .filter((k) => k.endpoint === 'chat')
      .reduce((a, k) => a + k.completion_tokens, 0);
    const embedTok = s.by_kind
      .filter((k) => k.endpoint === 'embed')
      .reduce((a, k) => a + k.prompt_tokens, 0);
    return (
      (chatIn * num(priceInput) + chatOut * num(priceOutput) + embedTok * num(priceEmbed)) / 1e6
    );
  }

  let short = $derived.by(() => {
    const f = $fmt;
    return (n: number | null | undefined): string => {
      if (n === null || n === undefined) return '—';
      if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
      if (n >= 10_000) return (n / 1_000).toFixed(1) + 'k';
      return f.number(n);
    };
  });

  function fmtCost(c: number | null): string {
    if (c === null) return '—';
    return c < 0.01 && c > 0 ? '<$0.01' : '$' + c.toFixed(2);
  }

  const KIND_KEYS = new Set([
    'summarize', 'embed', 'cluster_embed', 'pairwise', 'novelty',
    'headline', 'merge', 'share_translate', 'backfill_embed'
  ]);
  const kindLabel = (k: string) => (KIND_KEYS.has(k) ? $t(`usage.kind_${k}`) : k);

  // Daily chart: total tokens per UTC day (all kinds summed), CSS bars.
  let chart = $derived.by(() => {
    const perDay = new Map<string, number>();
    for (const r of dailyRows) {
      perDay.set(r.day, (perDay.get(r.day) ?? 0) + r.total_tokens);
    }
    const days = [...perDay.entries()].sort(([a], [b]) => a.localeCompare(b));
    const max = Math.max(1, ...days.map(([, v]) => v));
    return days.map(([day, tokens]) => ({ day, tokens, pct: (100 * tokens) / max }));
  });

  const periods = ['day', 'month', 'all'] as const;

  let estimatedShare = $derived.by(() => {
    const t = summaries.all?.totals;
    return t && t.calls > 0 ? t.estimated_calls / t.calls : 0;
  });
</script>

<svelte:head><title>{$t('usage.pageTitle')}</title></svelte:head>

{#if !$currentUser?.is_admin}
  <p class="muted">{$t('usage.adminOnly')}</p>
{:else}
  <h1>{$t('usage.heading')}</h1>

  {#if error}<p class="error">{error}</p>{/if}

  {#if estimatedShare > 0}
    <p class="warn">
      {$t('usage.warnPre', { pct: (estimatedShare * 100).toFixed(0) })} <strong>{$t('usage.warnEstimated')}</strong>
      {$t('usage.warnMid')} <code>usage</code>
      {$t('usage.warnPost')}
    </p>
  {/if}

  <section class="prices">
    <h2>{$t('usage.playground')}</h2>
    <p class="hint">
      {$t('usage.playgroundHint')}
    </p>
    <div class="price-inputs">
      <label>{$t('usage.chatInput')} <input bind:value={priceInput} oninput={savePrices} placeholder="0.00" inputmode="decimal" /></label>
      <label>{$t('usage.chatOutput')} <input bind:value={priceOutput} oninput={savePrices} placeholder="0.00" inputmode="decimal" /></label>
      <label>{$t('usage.embeddings')} <input bind:value={priceEmbed} oninput={savePrices} placeholder="0.00" inputmode="decimal" /></label>
    </div>
  </section>

  <div class="cards">
    {#each periods as p (p)}
      {@const s = summaries[p]}
      <div class="card">
        <h3>{p === 'day' ? $t('usage.today') : p === 'month' ? $t('usage.thisMonth') : $t('usage.allTime')}</h3>
        {#if s}
          <div class="big">{short(s.totals.total_tokens)}</div>
          <div class="muted">{$t('usage.tokensCalls', { calls: short(s.totals.calls) })}</div>
          <div class="muted">{$t('usage.inOut', { in: short(s.totals.prompt_tokens), out: short(s.totals.completion_tokens) })}</div>
          {#if num(priceInput) + num(priceOutput) + num(priceEmbed) > 0}
            <div class="cost">≈ {fmtCost(cost(s))}</div>
          {/if}
        {:else}
          <div class="muted">…</div>
        {/if}
      </div>
    {/each}
  </div>

  <h2>{$t('usage.tokensPerDay')} <span class="muted">{$t('usage.chartHint', { count: Math.min(90, chart.length) })}</span></h2>
  {#if chart.length}
    <div class="chart">
      {#each chart as c (c.day)}
        <div class="bar" style:height="{Math.max(c.pct, 1)}%" title={$t('usage.barTitle', { day: c.day, tokens: short(c.tokens) })}></div>
      {/each}
    </div>
  {:else}
    <p class="muted">{$t('usage.noUsage')}</p>
  {/if}

  <div class="tables">
    <section>
      <h2>{$t('usage.byStage')}</h2>
      <table>
        <thead>
          <tr><th>{$t('usage.colStage')}</th><th>{$t('usage.colCalls')}</th><th>{$t('usage.colIn')}</th><th>{$t('usage.colOut')}</th><th>{$t('usage.colTotal')}</th><th>tok/s</th></tr>
        </thead>
        <tbody>
          {#each summaries.all?.by_kind ?? [] as k (k.kind)}
            <tr>
              <td>{kindLabel(k.kind)}{#if k.estimated_calls > 0}<span class="est" title={$t('usage.estimatedCalls', { count: k.estimated_calls })}>~</span>{/if}</td>
              <td>{short(k.calls)}</td>
              <td>{short(k.prompt_tokens)}</td>
              <td>{k.endpoint === 'chat' ? short(k.completion_tokens) : '—'}</td>
              <td>{short(k.total_tokens)}</td>
              <td>{k.tokens_per_s ?? '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>

    <section>
      <h2>{$t('usage.byModel')}</h2>
      <table>
        <thead>
          <tr><th>{$t('usage.colModel')}</th><th>{$t('usage.colCalls')}</th><th>{$t('usage.colTotalTokens')}</th><th>tok/s</th></tr>
        </thead>
        <tbody>
          {#each summaries.all?.by_model ?? [] as m (m.model + m.endpoint)}
            <tr>
              <td>{m.model || $t('usage.unknownModel')} <span class="muted">{m.endpoint}</span></td>
              <td>{short(m.calls)}</td>
              <td>{short(m.total_tokens)}</td>
              <td>{m.tokens_per_s ?? '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>

    <section>
      <h2>{$t('usage.byFeed')} <span class="muted">{$t('usage.allTimeParen')}</span></h2>
      <table>
        <thead>
          <tr><th>{$t('usage.colFeed')}</th><th>{$t('usage.colCalls')}</th><th>{$t('usage.colTotalTokens')}</th></tr>
        </thead>
        <tbody>
          {#each byFeed as f (f.feed_id ?? 'none')}
            <tr>
              <td>{f.title}{#if f.estimated_calls > 0}<span class="est" title={$t('usage.estimatedCalls', { count: f.estimated_calls })}>~</span>{/if}</td>
              <td>{short(f.calls)}</td>
              <td>{short(f.total_tokens)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>
  </div>
{/if}

<style>
  h1 { margin-bottom: 0.5rem; }
  h2 { font-size: 1.05rem; margin: 1.5rem 0 0.5rem; }
  .muted { color: var(--muted); font-weight: normal; font-size: 0.85rem; }
  .hint { color: var(--muted); font-size: 0.85rem; margin: 0.25rem 0 0.5rem; }
  .error { color: var(--error); }
  .warn {
    background: var(--warn-bg);
    color: var(--warn);
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    padding: 0.6rem 0.8rem;
  }
  .prices {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.8rem 1rem;
    margin: 1rem 0;
  }
  .prices h2 { margin-top: 0; }
  .price-inputs { display: flex; gap: 1rem; flex-wrap: wrap; }
  .price-inputs label { display: flex; flex-direction: column; font-size: 0.85rem; color: var(--text-secondary); gap: 0.2rem; }
  .price-inputs input {
    width: 7rem;
    padding: 0.3rem 0.5rem;
    border: 1px solid var(--border-input);
    border-radius: 6px;
    background: var(--bg);
    color: var(--text);
  }
  .cards { display: flex; gap: 1rem; flex-wrap: wrap; }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.8rem 1rem;
    min-width: 11rem;
    flex: 1 1 11rem;
  }
  .card h3 { margin: 0 0 0.3rem; font-size: 0.9rem; color: var(--text-secondary); }
  .card .big { font-size: 1.6rem; font-weight: 700; }
  .card .cost { color: var(--accent); font-weight: 600; margin-top: 0.3rem; }
  .chart {
    display: flex;
    align-items: flex-end;
    gap: 2px;
    height: 120px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 0.5rem;
  }
  .bar {
    flex: 1;
    min-width: 2px;
    background: var(--accent);
    border-radius: 2px 2px 0 0;
  }
  .tables section {
    margin-bottom: 1.5rem;
    /* tables are wider than a phone viewport — scroll inside the section
       (same pattern as the Activity page) instead of overflowing the page */
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  table { border-collapse: collapse; width: 100%; min-width: 30rem; background: var(--surface); border: 1px solid var(--table-border); border-radius: 8px; }
  th, td { text-align: left; padding: 0.4rem 0.7rem; border-bottom: 1px solid var(--row-border); font-size: 0.9rem; }
  th { color: var(--text-secondary); font-weight: 600; }
  .est { color: var(--warn); font-weight: 700; margin-left: 0.25rem; cursor: help; }
</style>
