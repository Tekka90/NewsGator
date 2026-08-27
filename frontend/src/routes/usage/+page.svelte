<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import type { UsageByFeed, UsageDailyRow, UsageSummary } from '$lib/types';

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
      error = e instanceof Error ? e.message : 'Failed to load usage';
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

  function fmt(n: number | null | undefined): string {
    if (n === null || n === undefined) return '—';
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
    if (n >= 10_000) return (n / 1_000).toFixed(1) + 'k';
    return n.toLocaleString();
  }

  function fmtCost(c: number | null): string {
    if (c === null) return '—';
    return c < 0.01 && c > 0 ? '<$0.01' : '$' + c.toFixed(2);
  }

  const KIND_LABELS: Record<string, string> = {
    summarize: 'Summaries',
    embed: 'Embeddings',
    cluster_embed: 'Cluster embeddings',
    pairwise: 'Pairwise checks',
    novelty: 'Novelty checks',
    headline: 'Headlines',
    merge: 'Story merges',
    share_translate: 'Share translations',
    backfill_embed: 'Backfill embeddings'
  };
  const kindLabel = (k: string) => KIND_LABELS[k] ?? k;

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

<svelte:head><title>Usage — NewsGator</title></svelte:head>

{#if !$currentUser?.is_admin}
  <p class="muted">Admin only.</p>
{:else}
  <h1>LLM usage</h1>

  {#if error}<p class="error">{error}</p>{/if}

  {#if estimatedShare > 0}
    <p class="warn">
      ⚠ {(estimatedShare * 100).toFixed(0)}% of calls have <strong>estimated</strong> token
      counts — the LLM server did not return a <code>usage</code> object, so a
      chars-per-token heuristic was used. Treat those numbers as rough.
    </p>
  {/if}

  <section class="prices">
    <h2>Cost playground</h2>
    <p class="hint">
      Prices in $ per 1M tokens — not saved on the server, tweak freely to compare
      providers/models against your real usage.
    </p>
    <div class="price-inputs">
      <label>Chat input <input bind:value={priceInput} oninput={savePrices} placeholder="0.00" inputmode="decimal" /></label>
      <label>Chat output <input bind:value={priceOutput} oninput={savePrices} placeholder="0.00" inputmode="decimal" /></label>
      <label>Embeddings <input bind:value={priceEmbed} oninput={savePrices} placeholder="0.00" inputmode="decimal" /></label>
    </div>
  </section>

  <div class="cards">
    {#each periods as p (p)}
      {@const s = summaries[p]}
      <div class="card">
        <h3>{p === 'day' ? 'Today' : p === 'month' ? 'This month' : 'All time'}</h3>
        {#if s}
          <div class="big">{fmt(s.totals.total_tokens)}</div>
          <div class="muted">tokens · {fmt(s.totals.calls)} calls</div>
          <div class="muted">in {fmt(s.totals.prompt_tokens)} / out {fmt(s.totals.completion_tokens)}</div>
          {#if num(priceInput) + num(priceOutput) + num(priceEmbed) > 0}
            <div class="cost">≈ {fmtCost(cost(s))}</div>
          {/if}
        {:else}
          <div class="muted">…</div>
        {/if}
      </div>
    {/each}
  </div>

  <h2>Tokens per day <span class="muted">(last {Math.min(90, chart.length)} days with activity)</span></h2>
  {#if chart.length}
    <div class="chart">
      {#each chart as c (c.day)}
        <div class="bar" style:height="{Math.max(c.pct, 1)}%" title="{c.day}: {fmt(c.tokens)} tokens"></div>
      {/each}
    </div>
  {:else}
    <p class="muted">No usage recorded yet.</p>
  {/if}

  <div class="tables">
    <section>
      <h2>By stage</h2>
      <table>
        <thead>
          <tr><th>Stage</th><th>Calls</th><th>In</th><th>Out</th><th>Total</th><th>tok/s</th></tr>
        </thead>
        <tbody>
          {#each summaries.all?.by_kind ?? [] as k (k.kind)}
            <tr>
              <td>{kindLabel(k.kind)}{#if k.estimated_calls > 0}<span class="est" title="{k.estimated_calls} estimated call(s)">~</span>{/if}</td>
              <td>{fmt(k.calls)}</td>
              <td>{fmt(k.prompt_tokens)}</td>
              <td>{k.endpoint === 'chat' ? fmt(k.completion_tokens) : '—'}</td>
              <td>{fmt(k.total_tokens)}</td>
              <td>{k.tokens_per_s ?? '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>

    <section>
      <h2>By model</h2>
      <table>
        <thead>
          <tr><th>Model</th><th>Calls</th><th>Total tokens</th><th>tok/s</th></tr>
        </thead>
        <tbody>
          {#each summaries.all?.by_model ?? [] as m (m.model + m.endpoint)}
            <tr>
              <td>{m.model || '(unknown)'} <span class="muted">{m.endpoint}</span></td>
              <td>{fmt(m.calls)}</td>
              <td>{fmt(m.total_tokens)}</td>
              <td>{m.tokens_per_s ?? '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>

    <section>
      <h2>By source feed <span class="muted">(all time)</span></h2>
      <table>
        <thead>
          <tr><th>Feed</th><th>Calls</th><th>Total tokens</th></tr>
        </thead>
        <tbody>
          {#each byFeed as f (f.feed_id ?? 'none')}
            <tr>
              <td>{f.title}{#if f.estimated_calls > 0}<span class="est" title="{f.estimated_calls} estimated call(s)">~</span>{/if}</td>
              <td>{fmt(f.calls)}</td>
              <td>{fmt(f.total_tokens)}</td>
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
  .tables section { margin-bottom: 1.5rem; }
  table { border-collapse: collapse; width: 100%; background: var(--surface); border: 1px solid var(--table-border); border-radius: 8px; }
  th, td { text-align: left; padding: 0.4rem 0.7rem; border-bottom: 1px solid var(--row-border); font-size: 0.9rem; }
  th { color: var(--text-secondary); font-weight: 600; }
  .est { color: var(--warn); font-weight: 700; margin-left: 0.25rem; cursor: help; }
</style>
