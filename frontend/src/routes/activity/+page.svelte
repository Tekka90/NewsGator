<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { currentUser } from '$lib/stores';
  import { api, authHeaders, streamUrl } from '$lib/api';
  import type { PipelineRow } from '$lib/types';

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
  let lastEventAt = $state(0);
  let reprocessing = $state<number | null>(null);

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
    'embed_done', 'process_error', 'queue'
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

  function fmt(e: ActivityEvent): string {
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
  Activity
  <span class="dot" class:on={live} title={live ? 'live' : 'reconnecting…'}></span>
</h1>

<div class="card status">
  <span>LLM queue: <strong>{queueDepth}</strong> article{queueDepth === 1 ? '' : 's'} waiting</span>
  <span class="spacer"></span>
  <select bind:value={componentFilter}>
    <option value="">all components</option>
    {#each ['ingest', 'fulltext', 'llm', 'cluster', 'retention'] as c}
      <option value={c}>{c}</option>
    {/each}
  </select>
</div>

{#if pipelineRows.length}
  <div class="card">
    <h2>Pipeline</h2>
    <div class="tablewrap">
      <table>
      <thead>
        <tr>
          <th>Article</th>
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
                {#if row.content_status === 'partial'}<span class="badge partial">partial</span>{/if}
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
                {reprocessing === row.id ? '…' : 'reprocess'}
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
      <span class="ts">{e.ts ? new Date(e.ts).toLocaleTimeString() : ''}</span>
      <span class="comp">{e.component}</span>
      <span class="action">{e.action}</span>
      <span class="detail">{fmt(e)}</span>
    </div>
  {:else}
    <p>No activity yet — events appear here in real time as the pipeline runs.</p>
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
