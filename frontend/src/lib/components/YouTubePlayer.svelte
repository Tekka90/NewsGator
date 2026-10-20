<script lang="ts">
  // Click-to-load: no request to YouTube until the user taps play, and the
  // iframe uses the privacy-enhanced nocookie domain.
  let { videoId, title = '' }: { videoId: string; title?: string } = $props();
  let loaded = $state(false);
  const safeId = $derived(/^[\w-]{6,20}$/.test(videoId) ? videoId : '');
</script>

{#if safeId}
  <!-- data-no-swipe: the swipe deck ignores gestures that start here -->
  <div class="player" data-no-swipe>
    {#if loaded}
      <iframe
        src="https://www.youtube-nocookie.com/embed/{safeId}?autoplay=1&rel=0&playsinline=1"
        title={title || 'YouTube video'}
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
        allowfullscreen
        referrerpolicy="strict-origin-when-cross-origin"
      ></iframe>
    {:else}
      <button type="button" class="poster" onclick={() => (loaded = true)} aria-label="Play video{title ? `: ${title}` : ''}">
        <img src="https://i.ytimg.com/vi/{safeId}/hqdefault.jpg" alt="" loading="lazy" referrerpolicy="no-referrer" />
        <span class="play" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="28" height="28"><path d="M8 5v14l11-7z" fill="currentColor" /></svg>
        </span>
      </button>
    {/if}
  </div>
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
  iframe, .poster { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
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
</style>
