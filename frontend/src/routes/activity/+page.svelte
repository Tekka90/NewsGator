<script lang="ts">
  import { onDestroy, onMount } from 'svelte';
  import { currentUser } from '$lib/stores';

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

  onMount(async () => {
    const res = await fetch('/api/activity/recent', { credentials: 'include' });
    if (res.ok) {
      const body = await res.json();
      events = body.events.slice(-200);
      queueDepth = body.llm_queue_depth;
    }
    connect();
  });

  function connect() {
    source = new EventSource('/api/activity/stream');
    source.onopen = () => (live = true);
    source.onerror = () => (live = false);
    source.onmessage = (msg) => {
      const payload = JSON.parse(msg.data);
      if (payload.action === 'ping') return;
      if (payload.llm_queue_depth !== undefined) {
        queueDepth = payload.llm_queue_depth;
        return;
      }
      events = [...events.slice(-499), payload];
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
  .log { font-family: ui-monospace, monospace; font-size: 0.82rem; max-height: 70vh; overflow-y: auto; }
  .line { display: flex; gap: 0.7rem; padding: 0.12rem 0; border-bottom: 1px solid #f2f2f2; }
  .line.warn { color: #8a5a00; }
  .line.error { color: #a12727; }
  .ts { color: #999; min-width: 5.5rem; }
  .comp { color: #294a7a; min-width: 5rem; }
  .action { font-weight: 600; min-width: 11rem; }
  .detail { color: #666; overflow-wrap: anywhere; }
</style>
