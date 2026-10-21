<script lang="ts">
  import { resolveIcon } from '$lib/presetIcons';

  let { icon, size = 15 }: { icon: string | null | undefined; size?: number } = $props();
  let resolved = $derived(resolveIcon(icon));
</script>

{#if resolved.type === 'svg'}
  <!-- markup comes from the constant catalog in presetIcons.ts, never from user input -->
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    {@html resolved.markup}
  </svg>
{:else}
  <span class="emoji" style:font-size="{size}px" aria-hidden="true">{resolved.emoji}</span>
{/if}

<style>
  svg,
  .emoji {
    flex-shrink: 0;
    line-height: 1;
  }
</style>
