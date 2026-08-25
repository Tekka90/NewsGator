<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { currentUser } from '$lib/stores';
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

  const PIPELINE_EVENTS = new Set([
    'feed_poll_done', 'fulltext_fetch', 'manual_reprocess',
    'summarize_start', 'summarize_done', 'summarize_error',
    'embed_done', 'process_error', 'queue'
  ]);

  async function loadPipeline() {
    const res = await fetch('/api/activity/pipeline', { credentials: 'include' });
    if (!res.ok) return;
    const body = await res.json();
    pipelineStates = body.states;
    pipelineRows = body.rows;
    queueDepth = body.llm_queue_depth;
  }

  onMount(async () => {
    const res = await fetch('/api/activity/recent', { credentials: 'include' });
    if (res.ok) {
      const body = await res.json();
      events = body.events.slice(-200);
      queueDepth = body.llm_queue_depth;
    }
    await loadPipeline();
    connect();
  });

  function connect() {
    source = new EventSource('/api/activity/stream');
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
    return pipelineStates.indexOf(row.processing_state) > pipelineStates.indexOf(stage);
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
    <table>
      <thead>
        <tr>
          <th>Article</th>
          {#each pipelineStates as s}<th>{s}</th>{/each}
        </tr>
      </thead>
      <tbody>
        {#each pipelineRows as row (row.id)}
          <tr>
            <td class="titlecell">
              <div class="t">{row.title}</div>
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
          </tr>
        {/each}
      </tbody>
    </table>
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
  .status { display: flex; align-items: center; gap: 1rem; }
  .spacer { flex: 1; }
  h2 { font-size: 1.05rem; margin-top: 0; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  th { text-align: left; color: #888; font-weight: 600; font-size: 0.8em; padding: 0.2rem 0.4rem; }
  td { padding: 0.3rem 0.4rem; border-top: 1px solid #f0f0f0; vertical-align: middle; }
  .titlecell .t { font-weight: 500; }
  .titlecell .sub { color: #999; font-size: 0.82em; }
  .badge.partial { background: #fde7e7; color: #a12727; padding: 0 0.4rem; border-radius: 999px; font-size: 0.9em; }
  .stage { text-align: center; width: 5.5rem; }
  .done { color: #2a2; }
  .running { color: #f90; animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: 0.25; } }
  .log { font-family: ui-monospace, monospace; font-size: 0.82rem; max-height: 70vh; overflow-y: auto; }
  .line { display: flex; gap: 0.7rem; padding: 0.12rem 0; border-bottom: 1px solid #f2f2f2; }
  .line.warn { color: #8a5a00; }
  .line.error { color: #a12727; }
  .ts { color: #999; min-width: 5.5rem; }
  .comp { color: #294a7a; min-width: 5rem; }
  .action { font-weight: 600; min-width: 11rem; }
  .detail { color: #666; overflow-wrap: anywhere; }
</style>
