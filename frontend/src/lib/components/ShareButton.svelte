<script lang="ts">
  /** Share a story via the native Web Share API (iMessage, Mail, …) with a
   *  clipboard fallback on browsers without it. Clicking opens a language
   *  picker: "As is" is instant (no LLM call), any other language translates
   *  the headline + summary on the server first. Always available — unlike
   *  Readeck there is nothing to configure. */
  import { api } from '$lib/api';

  let {
    storyId,
    iconOnly = false,
    buttonClass = ''
  }: { storyId: number; iconOnly?: boolean; buttonClass?: string } = $props();

  type Langs = { summary_language: string; languages: { code: string; name: string }[] };

  let open = $state(false);
  let busy = $state(false);
  let copied = $state(false);
  let error = $state('');
  let langs = $state<Langs | null>(null);

  async function toggle(e: Event) {
    e.preventDefault();
    e.stopPropagation();
    error = '';
    open = !open;
    if (open && !langs) {
      langs = await api.stories.shareLanguages().catch(() => null);
      if (!langs) error = 'Could not load languages';
    }
  }

  function close(e: Event) {
    e.preventDefault();
    e.stopPropagation();
    open = false;
  }

  async function pick(language: string | null, e: Event) {
    e.preventDefault();
    e.stopPropagation();
    open = false;
    busy = true;
    error = '';
    try {
      const card = await api.stories.share(storyId, language);
      if (navigator.share) {
        await navigator.share({ title: card.title, text: card.text, url: card.url });
      } else {
        await navigator.clipboard.writeText(`${card.title}\n\n${card.text}\n${card.url}`);
        copied = true;
        setTimeout(() => (copied = false), 2000);
      }
    } catch (err) {
      // user dismissed the native share sheet — not an error
      if (err instanceof DOMException && err.name === 'AbortError') return;
      error = err instanceof Error ? err.message : 'Share failed';
    } finally {
      busy = false;
    }
  }
</script>

<span class="sharewrap">
  <button
    class="iconbtn {buttonClass}"
    class:copied
    onclick={toggle}
    disabled={busy}
    aria-label="Share story"
    title={copied ? 'Copied to clipboard' : 'Share story'}
  >
    {#if busy}
      …
    {:else if copied}
      ✓
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
  {#if open}
    <button class="backdrop" aria-label="Close share menu" onclick={close} tabindex="-1"></button>
    <div class="menu" role="menu">
      <button role="menuitem" onclick={(e) => pick(null, e)}>As is</button>
      {#each (langs?.languages ?? []).filter((l) => l.code !== langs?.summary_language) as l (l.code)}
        <button role="menuitem" onclick={(e) => pick(l.code, e)}>{l.name}</button>
      {/each}
    </div>
  {/if}
  {#if error}<span class="shareerr" title={error}>⚠</span>{/if}
</span>

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
  .iconbtn.copied {
    color: var(--ok);
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
    cursor: help;
  }
</style>
