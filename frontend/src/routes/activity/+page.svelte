<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { currentUser } from '$lib/stores';
  import { api, authHeaders, streamUrl } from '$lib/api';
  import type { LLMInteraction, PipelineRow } from '$lib/types';
  import { t, fmt } from '$lib/i18n';

  interface ActivityEvent {
    ts?: string;
    level?: string;
    component?: string;
    action?: string;
    detail?: Record<string, unknown>;
  }

  let events = $state<ActivityEvent[]>([]);
  let queueDepth = $state(0);
  let componentFilter = $state('');
  let source: EventSource | null = null;
  let live = $state(false);
  let pipelineStates = $state<string[]>([]);
  let pipelineRows = $state<PipelineRow[]>([]);
  let pipelineInFlight = $state(0);
  let pipelineTruncated = $state(false);
  let lastEventAt = $state(0);
  let reprocessing = $state<number | null>(null);
  let llmInteractions = $state<LLMInteraction[]>([]);
  let llmTraceEnabled = $state(false);

  async function reprocess(articleId: number) {
    reprocessing = articleId;
    try {
      await api.stories.reprocessArticle(articleId);
    } catch {
      /* surfaced in the event log via process_error */
    } finally {
      reprocessing = null;
      await loadPipeline();
    }
  }

  const PIPELINE_EVENTS = new Set([
    'feed_poll_done', 'fulltext_fetch', 'manual_reprocess',
    'summarize_start', 'summarize_done', 'summarize_error',
    'embed_done', 'process_error', 'queue',
    // newsletter ingestion also feeds the LLM pipeline — refresh on mail events
    'mail_poll_start', 'mail_poll_done', 'newsletter_processing', 'newsletter_feed_created'
  ]);

  async function loadPipeline() {
    const res = await fetch('/api/activity/pipeline', {
      credentials: 'include',
      headers: authHeaders()
    });
    if (!res.ok) return;
    const body = await res.json();
    pipelineStates = body.states;
    pipelineRows = body.rows;
    pipelineInFlight = body.in_flight ?? 0;
    pipelineTruncated = body.truncated ?? false;
    queueDepth = body.llm_queue_depth;
  }

  onMount(async () => {
    const res = await fetch('/api/activity/recent', {
      credentials: 'include',
      headers: authHeaders()
    });
    if (res.ok) {
      const body = await res.json();
      events = body.events.slice(-200);
      queueDepth = body.llm_queue_depth;
    }
    await loadPipeline();
    const llmRes = await fetch('/api/activity/llm', {
      credentials: 'include',
      headers: authHeaders()
    });
    if (llmRes.ok) {
      const body = await llmRes.json();
      llmTraceEnabled = body.enabled;
      llmInteractions = body.interactions; // newest first
    }
    connect();
  });

  function connect() {
    source = new EventSource(streamUrl('/api/activity/stream'));
    source.onopen = () => (live = true);
    source.onerror = () => (live = false);
    source.onmessage = (msg) => {
      const payload = JSON.parse(msg.data);
      if (payload.action === 'ping') return;
      if (payload.action === 'queue' || payload.llm_queue_depth !== undefined) {
        queueDepth = payload.llm_queue_depth;
        if (payload.action === 'queue') loadPipeline();  // skip duplicate load on 'hello'
        return;
      }
      if (payload.action === 'llm_interaction') {
        llmTraceEnabled = true;
        const it = payload.interaction as LLMInteraction;
        const idx = llmInteractions.findIndex((x) => x.id === it.id);
        if (idx >= 0) {
          llmInteractions[idx] = it; // status flip running → done/error
        } else {
          llmInteractions = [it, ...llmInteractions].slice(0, 50);
        }
        return;
      }
      events = [...events.slice(-499), payload];
      // Refresh the pipeline table on pipeline events, throttled to 2s
      if (PIPELINE_EVENTS.has(payload.action)) {
        const now = Date.now();
        if (now - lastEventAt > 2000) {
          lastEventAt = now;
          loadPipeline();
        }
      }
    };
  }

  onDestroy(() => source?.close());

  let filtered = $derived(
    componentFilter ? events.filter((e) => e.component === componentFilter) : events
  );

  function fmtDetail(e: ActivityEvent): string {
    const d = e.detail ?? {};
    const parts = Object.entries(d)
      .filter(([, v]) => v !== null && v !== '')
      .map(([k, v]) => `${k}=${v}`);
    return parts.join(' ');
  }

  function stageDone(row: PipelineRow, stage: string): boolean {
    const at = pipelineStates.indexOf(row.processing_state);
    const target = pipelineStates.indexOf(stage);
    // the terminal state counts as done, not running
    return at > target || (at === target && at === pipelineStates.length - 1);
  }
</script>

<h1>
  {$t('activity.title')}
  <span class="dot" class:on={live} title={live ? $t('activity.live') : $t('activity.reconnecting')}></span>
</h1>

<div class="card status">
  <span>{$t('activity.queueLabel')} <strong>{queueDepth}</strong> {$t('activity.queueWaiting', { count: queueDepth })}</span>
  <span class="spacer"></span>
  <select bind:value={componentFilter}>
    <option value="">{$t('activity.allComponents')}</option>
    {#each ['ingest', 'fulltext', 'llm', 'cluster', 'retention'] as c}
      <option value={c}>{c}</option>
    {/each}
  </select>
</div>

{#if llmTraceEnabled}
  <div class="card">
    <h2>{$t('activity.llmInteractions')}</h2>
    <div class="llmlist">
      {#each llmInteractions as it (it.id)}
        <details class="llmitem" open={it.status === 'running'}>
          <summary>
            <span class="ldot {it.status}"></span>
            <span class="lkind">{it.kind}</span>
            <span class="llabel">{it.label ?? ''}</span>
            <span class="lmeta">
              {it.model}
              {#if it.status === 'done' && it.latency_ms !== null}· {(it.latency_ms / 1000).toFixed(1)}s{/if}
              {#if it.usage?.total_tokens}· {it.usage.total_tokens} tok{/if}
              {#if it.attempts > 1}· {$t('activity.attempts', { count: it.attempts })}{/if}
            </span>
            <span class="lts">{$fmt.time(it.ts)}</span>
          </summary>
          <div class="lbody">
            {#if it.endpoint === 'chat'}
              <h3>
                {$t('activity.systemPrompt')}
                {#if it.request.system_chars}
                  <span class="chars">{$t('activity.chars', { count: it.request.system_chars })}</span>
                {/if}
              </h3>
              <pre>{it.request.system}</pre>
              <h3>
                {$t('activity.userPrompt')}
                {#if it.request.user_chars}
                  <span class="chars">
                    {$t(it.request.truncated ? 'activity.charsTruncated' : 'activity.chars', { count: it.request.user_chars })}
                  </span>
                {/if}
              </h3>
              <pre>{it.request.user}</pre>
            {:else}
              <h3>{$t('activity.embeddingInput')}</h3>
              <p class="embedinfo">{$t('activity.embedInfo', { texts: it.request.texts ?? 0, chars: it.request.chars ?? 0 })}</p>
              <pre>{it.request.sample}</pre>
            {/if}
            <h3>{$t('activity.response')}</h3>
            {#if it.status === 'running'}
              <p class="waiting">{$t('activity.waiting')}</p>
            {:else if it.status === 'error'}
              <pre class="errtext">{it.error}</pre>
            {:else}
              <pre>{it.response}</pre>
            {/if}
          </div>
        </details>
      {:else}
        <p class="empty">{$t('activity.noLlm')}</p>
      {/each}
    </div>
  </div>
{/if}

{#if pipelineRows.length}
  <div class="card">
    <h2>{$t('activity.pipeline')}</h2>
    <p class="sub">
      {$t('activity.inFlight', { count: pipelineInFlight })}{pipelineTruncated ? ' ' + $t('activity.showingFirst') : ''}
      · {$t('activity.lastFinished', { count: Math.max(0, pipelineRows.length - pipelineInFlight) })}
    </p>
    <div class="tablewrap">
      <table>
      <thead>
        <tr>
          <th>{$t('activity.colArticle')}</th>
          {#each pipelineStates as s}<th>{s}</th>{/each}
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each pipelineRows as row (row.id)}
          <tr>
            <td class="titlecell">
              <div class="t">
                {#if row.story_id}
                  <a href="/stories/{row.story_id}">{row.title}</a>
                {:else}
                  {row.title}
                {/if}
              </div>
              <div class="sub">
                {row.feed_title}
                {#if row.content_status === 'partial'}<span class="badge partial">{$t('activity.partial')}</span>{/if}
              </div>
            </td>
            {#each pipelineStates as stage}
              <td class="stage">
                {#if stageDone(row, stage)}
                  <span class="done">✓</span>
                {:else if row.processing_state === stage}
                  <span class="running">●</span>
                {/if}
              </td>
            {/each}
            <td class="stage">
              <button class="link" onclick={() => reprocess(row.id)} disabled={reprocessing === row.id}>
                {reprocessing === row.id ? '…' : $t('activity.reprocess')}
              </button>
            </td>
          </tr>
        {/each}
      </tbody>
      </table>
    </div>
  </div>
{/if}

<div class="card log">
  {#each [...filtered].reverse() as e, i (i)}
    <div class="line {e.level}">
      <span class="ts">{e.ts ? $fmt.time(e.ts) : ''}</span>
      <span class="comp">{e.component}</span>
      <span class="action">{e.action}</span>
      <span class="detail">{fmtDetail(e)}</span>
    </div>
  {:else}
    <p>{$t('activity.noActivity')}</p>
  {/each}
</div>

<style>
  h1 { display: flex; align-items: center; gap: 0.6rem; }
  .dot {
    width: 10px; height: 10px; border-radius: 50%;
    background: #c33; display: inline-block;
  }
  .dot.on { background: #2a2; }
  .status { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
  .spacer { flex: 1; }
  h2 { font-size: 1.05rem; margin-top: 0; }
  /* pipeline table is wider than a phone screen — scroll it horizontally */
  .tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { width: 100%; min-width: 38rem; border-collapse: collapse; font-size: 0.88rem; }
  th { text-align: left; color: var(--muted); font-weight: 600; font-size: 0.8em; padding: 0.2rem 0.4rem; }
  td { padding: 0.3rem 0.4rem; border-top: 1px solid var(--row-border); vertical-align: middle; }
  .titlecell .t { font-weight: 500; }
  .titlecell .sub { color: var(--faint); font-size: 0.82em; }
  .badge.partial { background: var(--error-bg); color: var(--error); padding: 0 0.4rem; border-radius: 999px; font-size: 0.9em; }
  .stage { text-align: center; width: 5.5rem; }
  .done { color: var(--ok); }
  .running { color: #f90; animation: pulse 1.2s ease-in-out infinite; }
  button.link {
    border: none; background: none; color: var(--accent); cursor: pointer;
    font-size: 0.85em; padding: 0; text-decoration: underline;
  }
  button.link:disabled { color: var(--faint); cursor: default; }
  @keyframes pulse { 50% { opacity: 0.25; } }
  /* live LLM interaction trace */
  .llmlist { max-height: 60vh; overflow-y: auto; }
  .llmitem { border-top: 1px solid var(--row-border); padding: 0.3rem 0; }
  .llmitem summary {
    display: flex; align-items: baseline; gap: 0.5rem; flex-wrap: wrap;
    cursor: pointer; font-size: 0.88rem; list-style: none; min-width: 0;
  }
  .llmitem summary::-webkit-details-marker { display: none; }
  .ldot { width: 9px; height: 9px; border-radius: 50%; flex: none; align-self: center; }
  .ldot.running { background: #f90; animation: pulse 1.2s ease-in-out infinite; }
  .ldot.done { background: var(--ok); }
  .ldot.error { background: var(--error); }
  .lkind {
    background: var(--accent); color: var(--bg); border-radius: 999px;
    padding: 0 0.5rem; font-size: 0.78em; font-weight: 600; flex: none;
  }
  .llabel { font-weight: 500; overflow-wrap: anywhere; min-width: 0; }
  .lmeta { margin-left: auto; color: var(--faint); font-size: 0.8em; }
  .lts { color: var(--faint); font-size: 0.8em; }
  .lbody { padding: 0.4rem 0 0.4rem 1.2rem; }
  .lbody h3 {
    font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--muted); margin: 0.6rem 0 0.2rem;
  }
  .lbody .chars { font-weight: 400; text-transform: none; color: var(--faint); }
  .lbody pre {
    margin: 0; padding: 0.5rem 0.6rem; background: var(--bg); border-radius: 6px;
    font-size: 0.8rem; white-space: pre-wrap; overflow-wrap: anywhere;
    max-height: 16rem; overflow-y: auto;
  }
  .lbody pre.errtext { color: var(--error); }
  .lbody .waiting { color: #f90; font-size: 0.85rem; margin: 0.2rem 0; }
  .lbody .embedinfo { color: var(--text-secondary); font-size: 0.85rem; margin: 0.2rem 0; }
  .empty { color: var(--faint); }
  .log { font-family: ui-monospace, monospace; font-size: 0.82rem; max-height: 70vh; overflow-y: auto; }
  .line { display: flex; gap: 0.7rem; padding: 0.12rem 0; border-bottom: 1px solid var(--row-border); }
  .line.warn { color: var(--warn); }
  .line.error { color: var(--error); }
  .ts { color: var(--faint); min-width: 5.5rem; }
  .comp { color: var(--accent); min-width: 5rem; }
  .action { font-weight: 600; min-width: 11rem; }
  .detail { color: var(--text-secondary); overflow-wrap: anywhere; }

  @media (max-width: 700px) {
    .log { font-size: 0.78rem; max-height: 60vh; }
    .line { flex-wrap: wrap; gap: 0.1rem 0.5rem; }
    .ts { min-width: 4.3rem; }
    .comp { min-width: 0; }
    .action { min-width: 0; overflow-wrap: anywhere; }
    .detail { flex: 1 1 100%; } /* details on their own line */
  }
</style>
