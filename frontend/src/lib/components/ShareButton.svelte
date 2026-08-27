<script lang="ts">
  /** Share a story via the native Web Share API (iMessage, Mail, …) or the
   *  clipboard. Clicking opens a language picker: "As is" is instant (no LLM
   *  call), any other language translates the headline + summary on the
   *  server first. Because that server round-trip (translation can take
   *  seconds) consumes the click's transient user activation — which both
   *  navigator.share and navigator.clipboard require — the prepared card is
   *  shown in a preview sheet with explicit Share…/Copy buttons, so the
   *  share/copy always happens on a fresh user gesture.
   *
   *  Note: the primary link is folded into the shared text instead of the
   *  separate `url` field — some share targets (Messages…) silently drop
   *  `text` when `url` is present.
   *
   *  Always available — unlike Readeck there is nothing to configure. */
  import { api } from '$lib/api';

  let {
    storyId,
    iconOnly = false,
    buttonClass = ''
  }: { storyId: number; iconOnly?: boolean; buttonClass?: string } = $props();

  type Langs = { summary_language: string; languages: { code: string; name: string }[] };
  type ShareCard = {
    title: string;
    text: string;
    url: string;
    language: string;
    translated: boolean;
    latency_ms: number;
  };

  let menuOpen = $state(false);
  let busy = $state(false);
  let copied = $state(false);
  let error = $state('');
  let langs = $state<Langs | null>(null);
  let card = $state<ShareCard | null>(null);

  const canNativeShare = typeof navigator !== 'undefined' && Boolean(navigator.share);

  async function toggle(e: Event) {
    e.preventDefault();
    e.stopPropagation();
    error = '';
    card = null;
    menuOpen = !menuOpen;
    if (menuOpen && !langs) {
      langs = await api.stories.shareLanguages().catch(() => null);
      if (!langs) error = 'Could not load languages';
    }
  }

  function closeAll() {
    menuOpen = false;
    card = null;
    error = '';
  }

  function onBackdrop(e: Event) {
    e.preventDefault();
    e.stopPropagation();
    closeAll();
  }

  async function pick(language: string | null, e: Event) {
    e.preventDefault();
    e.stopPropagation();
    menuOpen = false;
    busy = true;
    error = '';
    try {
      card = await api.stories.share(storyId, language);
    } catch (err) {
      error = err instanceof Error ? err.message : 'Share failed';
    } finally {
      busy = false;
    }
  }

  /** Title + text with the primary link guaranteed inside the text. */
  function sharePayload(c: ShareCard): { title: string; text: string } {
    const text = c.text.includes(c.url) ? c.text : `${c.text}\n${c.url}`;
    return { title: c.title, text };
  }

  async function shareNative() {
    if (!card) return;
    error = '';
    try {
      await navigator.share(sharePayload(card));
      closeAll();
    } catch (err) {
      // user dismissed the native share sheet — not an error
      if (err instanceof DOMException && err.name === 'AbortError') return;
      error = err instanceof Error ? err.message : 'Share failed';
    }
  }

  /** Legacy clipboard for insecure contexts (plain HTTP on the LAN), where
   *  navigator.clipboard is undefined. */
  function legacyCopy(text: string): boolean {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    ta.remove();
    return ok;
  }

  async function copyCard() {
    if (!card) return;
    error = '';
    const p = sharePayload(card);
    const full = `${p.title}\n\n${p.text}`;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(full);
      } else if (!legacyCopy(full)) {
        throw new Error('Clipboard is unavailable on this browser');
      }
      copied = true;
      setTimeout(() => (copied = false), 2000);
    } catch (err) {
      error = err instanceof Error ? err.message : 'Copy failed';
    }
  }
</script>

<span class="sharewrap">
  <button
    class="iconbtn {buttonClass}"
    onclick={toggle}
    disabled={busy}
    aria-label="Share story"
    title="Share story"
  >
    {#if busy}
      …
    {:else}
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M12 3v13" /><path d="m7 8 5-5 5 5" />
        <path d="M5 13v6a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-6" />
      </svg>
      {#if !iconOnly}Share{/if}
    {/if}
  </button>
  {#if menuOpen}
    <button class="backdrop" aria-label="Close share menu" onclick={onBackdrop} tabindex="-1"></button>
    <div class="menu" role="menu">
      <button role="menuitem" onclick={(e) => pick(null, e)}>As is</button>
      {#each (langs?.languages ?? []).filter((l) => l.code !== langs?.summary_language) as l (l.code)}
        <button role="menuitem" onclick={(e) => pick(l.code, e)}>{l.name}</button>
      {/each}
    </div>
  {/if}
  {#if error && !card}<span class="shareerr" title={error}>⚠</span>{/if}
</span>

{#if card}
  <button class="sheetbg" aria-label="Close share preview" onclick={onBackdrop} tabindex="-1"></button>
  <div class="sheetwrap">
    <div class="sheet" role="dialog" aria-label="Share preview">
      <div class="sheethead">
        <strong>{card.title}</strong>
        {#if card.translated}<span class="langbadge">{card.language}</span>{/if}
      </div>
      <pre class="preview">{card.text}</pre>
      {#if error}<p class="shareerr">{error}</p>{/if}
      <div class="sheetactions">
        {#if canNativeShare}
          <button class="primary" onclick={shareNative}>Share…</button>
        {/if}
        <button onclick={copyCard}>{copied ? '✓ Copied' : 'Copy'}</button>
        <button class="linkbtn" onclick={onBackdrop}>Close</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .sharewrap {
    position: relative;
    display: inline-flex;
    align-items: center;
  }
  .iconbtn {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  .backdrop {
    position: fixed;
    inset: 0;
    z-index: 30;
    background: transparent;
    border: none;
    padding: 0;
    cursor: default;
  }
  .menu {
    position: absolute;
    top: calc(100% + 4px);
    right: 0;
    z-index: 31;
    display: flex;
    flex-direction: column;
    min-width: 9rem;
    padding: 0.25rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    box-shadow: 0 4px 16px rgb(0 0 0 / 0.25);
  }
  .menu button {
    background: none;
    border: none;
    color: var(--text);
    text-align: left;
    padding: 0.4rem 0.6rem;
    border-radius: 6px;
    cursor: pointer;
  }
  .menu button:hover {
    background: var(--chip-bg);
    color: var(--accent);
  }
  .shareerr {
    color: var(--error);
  }
  .sheetbg {
    position: fixed;
    inset: 0;
    z-index: 40;
    background: rgb(0 0 0 / 0.45);
    border: none;
    padding: 0;
    cursor: default;
  }
  .sheetwrap {
    position: fixed;
    inset: 0;
    z-index: 41;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem;
    pointer-events: none;
  }
  .sheet {
    pointer-events: auto;
    width: min(34rem, 100%);
    max-height: 80vh;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    padding: 1rem;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    box-shadow: 0 8px 32px rgb(0 0 0 / 0.35);
  }
  .sheethead {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
  }
  .sheethead strong {
    overflow-wrap: anywhere;
  }
  .langbadge {
    font-size: 0.72em;
    text-transform: uppercase;
    background: var(--chip-bg);
    color: var(--accent);
    padding: 0.1rem 0.5rem;
    border-radius: 999px;
    flex-shrink: 0;
  }
  .preview {
    margin: 0;
    padding: 0.6rem;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font: inherit;
    font-size: 0.85em;
    color: var(--muted);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
  }
  .sheetactions {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
    flex-wrap: wrap;
  }
  .primary {
    font-weight: 600;
  }
</style>
