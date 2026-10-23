<script lang="ts">
  // One player for every video article. YouTube stays click-to-load on the
  // privacy-enhanced nocookie domain; direct files use <video>; other embeds go
  // in a sandboxed iframe loaded on tap; anything else falls back to a link.
  import { planPlayer, safeHttpUrl } from '$lib/video';
  import type { VideoProvider } from '$lib/types';

  let {
    videoId = null,
    provider = null,
    url = null,
    embedUrl = null,
    posterUrl = null,
    title = ''
  }: {
    videoId?: string | null;
    provider?: VideoProvider | null;
    url?: string | null;
    embedUrl?: string | null;
    posterUrl?: string | null;
    title?: string;
  } = $props();

  let loaded = $state(false);
  let failed = $state(false);
  const plan = $derived(planPlayer({ provider, videoId, url, embedUrl }));
  const poster = $derived(safeHttpUrl(posterUrl));
  const label = $derived(`Play video${title ? `: ${title}` : ''}`);
  // HLS plays natively on Safari only; elsewhere we link out instead of bundling a player.
  const hlsUnsupported = $derived(
    plan.mode === 'file' &&
      plan.hls &&
      typeof document !== 'undefined' &&
      !document.createElement('video').canPlayType('application/vnd.apple.mpegurl')
  );
  const fallbackHref = $derived(
    plan.mode === 'file' ? plan.src : plan.mode === 'link' ? plan.href : null
  );
</script>

{#if plan.mode === 'youtube'}
  <!-- data-no-swipe: the swipe deck ignores gestures that start here -->
  <div class="player" data-no-swipe>
    {#if loaded}
      <iframe
        src="https://www.youtube-nocookie.com/embed/{plan.id}?autoplay=1&rel=0&playsinline=1"
        title={title || 'YouTube video'}
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
        allowfullscreen
        referrerpolicy="strict-origin-when-cross-origin"
      ></iframe>
    {:else}
      <button type="button" class="poster" onclick={() => (loaded = true)} aria-label={label}>
        <img src="https://i.ytimg.com/vi/{plan.id}/hqdefault.jpg" alt="" loading="lazy" referrerpolicy="no-referrer" />
        <span class="play" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="28" height="28"><path d="M8 5v14l11-7z" fill="currentColor" /></svg>
        </span>
      </button>
    {/if}
  </div>
{:else if plan.mode === 'file' && !failed && !hlsUnsupported}
  <div class="player" data-no-swipe>
    <!-- svelte-ignore a11y_media_has_caption -->
    <video
      src={plan.src}
      poster={poster ?? undefined}
      controls
      playsinline
      preload="metadata"
      aria-label={title || 'Video'}
      onerror={() => (failed = true)}
    ></video>
  </div>
{:else if plan.mode === 'iframe'}
  <div class="player" data-no-swipe>
    {#if loaded}
      <iframe
        src={plan.src}
        title={title || 'Video'}
        sandbox="allow-scripts allow-same-origin allow-presentation allow-popups"
        allow="autoplay; fullscreen; picture-in-picture; encrypted-media"
        allowfullscreen
        referrerpolicy="strict-origin-when-cross-origin"
      ></iframe>
    {:else}
      <button type="button" class="poster" onclick={() => (loaded = true)} aria-label={label}>
        {#if poster}<img src={poster} alt="" loading="lazy" referrerpolicy="no-referrer" />{/if}
        <span class="play" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="28" height="28"><path d="M8 5v14l11-7z" fill="currentColor" /></svg>
        </span>
      </button>
    {/if}
  </div>
{:else if fallbackHref}
  <a class="openlink" href={fallbackHref} target="_blank" rel="noopener noreferrer" data-no-swipe>
    ▶ Open video in browser
  </a>
{/if}

<style>
  .player {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: var(--video-player-bg);
    border-radius: var(--radius-md);
    overflow: hidden;
    touch-action: manipulation;
  }
  iframe, video, .poster { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
  video { background: var(--video-player-bg); }
  .poster { padding: 0; margin: 0; background: none; border: 0; cursor: pointer; }
  .poster img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .play {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    width: 4rem; height: 2.8rem; border-radius: var(--radius-md);
    background: var(--video-red); color: #fff;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.35);
  }
  .poster:hover .play, .poster:focus-visible .play { filter: brightness(1.12); }
  .openlink {
    display: inline-block;
    padding: 0.5rem 0.9rem;
    border-radius: var(--radius-md);
    background: var(--video-badge-bg);
    color: var(--video-red);
    text-decoration: none;
    font-weight: 600;
  }
</style>
