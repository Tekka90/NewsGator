<script lang="ts">
  /** Chatbot: ask questions across the whole story archive (RAG).
   *  Each turn retrieves grounding stories fresh from the server and grounds
   *  the answer on their summaries; cited stories render as clickable cards.
   *  History is stored server-side (per user) so it follows the user across
   *  devices; this page loads it on mount and appends each new turn. */
  import { onMount, tick } from 'svelte';
  import { api, faviconUrl } from '$lib/api';
  import type { ChatStory } from '$lib/types';

  type Turn =
    | { role: 'user'; text: string }
    | { role: 'assistant'; text: string; stories: ChatStory[]; latency_ms: number }
    | { role: 'error'; text: string };

  let question = $state('');
  let turns = $state<Turn[]>([]);
  let busy = $state(false);
  let listEl = $state<HTMLElement>();
  let taEl = $state<HTMLTextAreaElement>();

  onMount(async () => {
    try {
      const hist = await api.chat.history();
      turns = hist.map((m) =>
        m.role === 'assistant'
          ? { role: 'assistant', text: m.content, stories: m.stories, latency_ms: m.latency_ms }
          : m.role === 'error'
            ? { role: 'error', text: m.content }
            : { role: 'user', text: m.content }
      );
      scrollToBottom();
    } catch {
      /* no history / chat disabled — start fresh */
    }
  });

  function scrollToBottom() {
    requestAnimationFrame(() => {
      listEl?.scrollTo({ top: listEl.scrollHeight, behavior: 'smooth' });
    });
  }

  // Grow the composer with its content (1 row → up to ~9rem), reset after send.
  function autogrow() {
    if (!taEl) return;
    taEl.style.height = 'auto';
    taEl.style.height = `${Math.min(taEl.scrollHeight, 144)}px`;
  }

  async function send() {
    const q = question.trim();
    if (!q || busy) return;
    question = '';
    await tick(); // let the emptied value flush before measuring height
    autogrow();
    busy = true;
    turns = [...turns, { role: 'user', text: q }];
    scrollToBottom();
    try {
      const res = await api.chat.ask(q);
      turns = [
        ...turns,
        { role: 'assistant', text: res.answer, stories: res.stories, latency_ms: res.latency_ms }
      ];
    } catch (e) {
      turns = [...turns, { role: 'error', text: e instanceof Error ? e.message : 'Chat failed' }];
    } finally {
      busy = false;
      scrollToBottom();
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  async function clear() {
    turns = [];
    await api.chat.clearHistory().catch(() => {});
  }
</script>

<svelte:head><title>Chat · NewsGator</title></svelte:head>

<div class="chatwrap">
  <header class="chathead">
    <h1>Ask your archive</h1>
    {#if turns.length}
      <button class="clearbtn" onclick={clear} disabled={busy}>Clear</button>
    {/if}
  </header>

  <div class="turns" bind:this={listEl}>
    {#if turns.length === 0}
      <div class="empty">
        <p>Ask anything across the stories you've loaded.</p>
        <p class="hint">e.g. “What's the latest on the EU AI Act?” · “Any news about SpaceX this week?”</p>
      </div>
    {/if}
    {#each turns as turn}
      {#if turn.role === 'user'}
        <div class="bubble user">{turn.text}</div>
      {:else if turn.role === 'error'}
        <div class="bubble error">{turn.text}</div>
      {:else}
        <div class="bubble assistant">
          <p class="ans">{turn.text}</p>
          {#if turn.stories.length}
            <div class="cites">
              {#each turn.stories as s}
                <a class="cite" href="/stories/{s.id}">
                  {#if s.source_hosts.length}
                    <img class="favicon" src={faviconUrl(s.source_hosts[0])} alt="" loading="lazy" />
                  {/if}
                  <span class="cite-title">{s.title}</span>
                  {#if s.cited}<span class="citedchip">cited</span>{/if}
                </a>
              {/each}
            </div>
          {/if}
        </div>
      {/if}
    {/each}
    {#if busy}
      <div class="bubble assistant thinking">Thinking…</div>
    {/if}
  </div>

  <form class="composer" onsubmit={(e) => { e.preventDefault(); send(); }}>
    <textarea
      bind:this={taEl}
      bind:value={question}
      onkeydown={onKeydown}
      oninput={autogrow}
      placeholder="Ask a question…"
      rows="1"
      enterkeyhint="send"
      disabled={busy}
    ></textarea>
    <button type="submit" disabled={busy || !question.trim()}>Send</button>
  </form>
</div>

<style>
  .chatwrap {
    display: flex;
    flex-direction: column;
    /* Fill the visible area below the sticky nav. Use the *dynamic* viewport
       height so iOS toolbar show/hide is tracked; subtract the nav height
       (sticky, never gives space back) and the layout's vertical margins. */
    height: calc(100vh - var(--nav-h, 0px) - 3rem);
    height: calc(100dvh - var(--nav-h, 0px) - 3rem);
    max-width: 60rem;
    margin: 0 auto;
    width: 100%;
    box-sizing: border-box;
  }
  @media (max-width: 700px) {
    .chatwrap {
      height: calc(100vh - var(--nav-h, 0px) - 1.6rem);
      height: calc(100dvh - var(--nav-h, 0px) - 1.6rem);
    }
  }
  .chathead {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .chathead h1 {
    font-size: 1.25rem;
    margin: 0.25rem 0;
  }
  .clearbtn {
    background: none;
    border: 1px solid var(--border, #ccc);
    border-radius: 6px;
    padding: 0.2rem 0.6rem;
    cursor: pointer;
    color: var(--text);
  }
  .turns {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 0.75rem 0.25rem;
  }
  .empty {
    margin: auto;
    text-align: center;
    color: var(--muted, #888);
  }
  .empty .hint {
    font-size: 0.85rem;
    opacity: 0.8;
  }
  .bubble {
    max-width: 85%;
    padding: 0.55rem 0.8rem;
    border-radius: 12px;
    white-space: pre-wrap;
    word-wrap: break-word;
    line-height: 1.4;
  }
  .bubble.user {
    align-self: flex-end;
    background: var(--accent, #2f6fed);
    color: #fff;
    border-bottom-right-radius: 4px;
  }
  .bubble.assistant {
    align-self: flex-start;
    background: var(--surface, #fff);
    border: 1px solid var(--border, #e2e2e2);
    border-bottom-left-radius: 4px;
  }
  .bubble.error {
    align-self: flex-start;
    background: #7a1f1f22;
    border: 1px solid #a33;
    color: var(--text);
  }
  .thinking {
    opacity: 0.7;
    font-style: italic;
  }
  .ans {
    margin: 0;
  }
  .cites {
    margin-top: 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .cite {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.3rem 0.5rem;
    border-radius: 8px;
    background: var(--bg, #f3f3f3);
    text-decoration: none;
    color: var(--text);
    font-size: 0.88rem;
    min-width: 0;
  }
  .cite:hover {
    background: var(--border, #e5e5e5);
  }
  .favicon {
    width: 16px;
    height: 16px;
    border-radius: 3px;
    flex-shrink: 0;
  }
  .cite-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }
  .citedchip {
    flex-shrink: 0;
    font-size: 0.7rem;
    padding: 0.05rem 0.4rem;
    border-radius: 999px;
    background: var(--accent, #2f6fed);
    color: #fff;
  }
  .composer {
    display: flex;
    align-items: flex-end;
    gap: 0.5rem;
    padding-top: 0.5rem;
    /* iOS: keep the composer clear of the home-indicator band */
    padding-bottom: env(safe-area-inset-bottom, 0px);
    border-top: 1px solid var(--border, #e2e2e2);
  }
  .composer textarea {
    flex: 1;
    min-width: 0;
    resize: none;
    padding: 0.6rem 0.7rem;
    border-radius: 10px;
    border: 1px solid var(--border, #ccc);
    background: var(--surface, #fff);
    color: var(--text);
    font-family: inherit;
    /* iOS auto-zooms focused inputs under 16px and then mis-scrolls the page */
    font-size: 1rem;
    line-height: 1.35;
    max-width: 100%;
    box-sizing: border-box;
  }
  .composer button {
    padding: 0.6rem 1.1rem;
    border-radius: 10px;
    border: none;
    background: var(--accent, #2f6fed);
    color: #fff;
    cursor: pointer;
    flex-shrink: 0;
    white-space: nowrap;
    font-size: 1rem;
    align-self: stretch;
  }
  .composer button:disabled {
    opacity: 0.5;
    cursor: default;
  }
</style>
