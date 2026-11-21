<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { t } from '$lib/i18n';
  import TopicSheet from '$lib/components/TopicSheet.svelte';
  import type { Topic } from '$lib/types';

  type View = 'mix' | 'volume' | 'unread' | 'recent';
  const VIEWS: View[] = ['mix', 'volume', 'unread', 'recent'];
  const LONG_PRESS_MS = 500;
  const AUTO_ROTATE = 0.07; // rad/s
  const CAMERA = 3; // perspective distance (unit sphere radius = 1)

  type RGB = [number, number, number];
  interface Node {
    topic: Topic;
    pos: [number, number, number];
    r: number;
    rTarget: number;
    mute: number;
    follow: number;
    phase: number;
    // projection cache (last frame)
    sx: number;
    sy: number;
    sr: number;
    depth: number;
  }
  interface Palette {
    forest: RGB;
    forestDark: RGB;
    leaf: RGB;
    gold: RGB;
    paper: RGB;
  }

  let topics = $state<Topic[]>([]);
  let loading = $state(true);
  let view = $state<View>('mix');
  let selected = $state<Topic | null>(null);
  let hiddenCount = $state(0);
  let toast = $state('');
  let wrap: HTMLDivElement;
  let canvas: HTMLCanvasElement;

  let nodes: Node[] = [];
  let palette: Palette | null = null;
  let reduced = false;
  let toastTimer: ReturnType<typeof setTimeout> | undefined;

  const hex = (v: string, fallback: RGB): RGB => {
    const m = /^#?([0-9a-f]{6})$/i.exec(v.trim());
    if (!m) return fallback;
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };
  const rgba = (c: RGB, a: number) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${Math.max(0, Math.min(1, a))})`;
  const mix = (a: RGB, b: RGB, k: number): RGB => [
    a[0] + (b[0] - a[0]) * k,
    a[1] + (b[1] - a[1]) * k,
    a[2] + (b[2] - a[2]) * k
  ];

  function readPalette() {
    const cs = getComputedStyle(document.documentElement);
    const g = (name: string, fb: RGB) => hex(cs.getPropertyValue(name), fb);
    palette = {
      forest: g('--gator-forest', [20, 107, 58]),
      forestDark: g('--gator-forest-dark', [11, 61, 42]),
      leaf: g('--gator-leaf', [73, 168, 90]),
      gold: g('--gator-gold', [242, 184, 39]),
      paper: g('--gator-paper', [248, 247, 241])
    };
  }

  // --- scoring ---
  function scores(list: Topic[], v: View): number[] {
    const max = (f: (x: Topic) => number) => Math.max(1, ...list.map(f));
    const mS = max((x) => x.story_count);
    const mU = max((x) => x.unread_count);
    const mR = max((x) => x.recent_count);
    return list.map((x) => {
      if (v === 'volume') return x.story_count / mS;
      if (v === 'unread') return x.unread_count / mU;
      if (v === 'recent') return x.recent_count / mR;
      return 0.4 * (x.story_count / mS) + 0.35 * (x.unread_count / mU) + 0.25 * (x.recent_count / mR);
    });
  }

  function applyView() {
    const sc = scores(topics, view);
    const top = Math.max(1e-6, ...sc);
    nodes.forEach((n, i) => {
      // normalised so the biggest topic of the current view is always the largest
      n.rTarget = 5 + 21 * Math.sqrt(sc[i] / top);
    });
    needsDraw = true;
  }

  function syncNodes() {
    const old = new Map(nodes.map((n) => [n.topic.id, n]));
    nodes = topics.map((tp, i) => {
      const prev = old.get(tp.id);
      const p = tp.position ?? [0, 0, 0];
      if (prev) {
        prev.topic = tp;
        prev.pos = p;
        return prev;
      }
      return {
        topic: tp,
        pos: p,
        r: 5,
        rTarget: 5,
        mute: tp.rule?.mode === 'mute' ? 1 : 0,
        follow: tp.rule?.mode === 'follow' ? 1 : 0,
        phase: i * 1.7,
        sx: 0,
        sy: 0,
        sr: 0,
        depth: 0
      };
    });
    applyView();
  }

  const stateOf = (tp: Topic) => (tp.rule?.mode === 'mute' ? 'muted' : tp.rule?.mode === 'follow' ? 'followed' : 'active');
  const stateText = (tp: Topic) => $t('topics.state_' + stateOf(tp));

  async function load() {
    try {
      topics = (await api.topics.list(true)).filter((x) => x.position);
    } catch {
      topics = [];
    }
    syncNodes();
    loading = false;
  }

  async function loadHidden() {
    try {
      hiddenCount = (await api.stories.counts()).ignored ?? 0;
    } catch {
      hiddenCount = 0;
    }
  }

  async function changed() {
    await Promise.all([load(), loadHidden()]);
  }

  function say(msg: string) {
    toast = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast = ''), 2600);
  }

  async function quickToggle(tp: Topic) {
    try {
      if (tp.rule) {
        await api.topics.removeRule(tp.rule.id);
        say($t('topics.toastReset', { label: tp.label }));
      } else {
        await api.topics.createRule(tp.id, 'mute', 'fade');
        say($t('topics.toastMuted', { label: tp.label }));
      }
      await changed();
    } catch (e) {
      say(e instanceof Error ? e.message : $t('topics.error'));
    }
  }

  // --- view switcher (radiogroup, roving tabindex) ---
  function pickView(v: View) {
    view = v;
    applyView();
  }
  function onSegKey(e: KeyboardEvent) {
    const dir = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
    if (!dir) return;
    e.preventDefault();
    const next = VIEWS[(VIEWS.indexOf(view) + dir + VIEWS.length) % VIEWS.length];
    pickView(next);
    (e.currentTarget as HTMLElement).querySelector<HTMLElement>(`[data-view="${next}"]`)?.focus();
  }

  // --- 3D state ---
  let yaw = 0.6;
  let pitch = -0.25;
  let vYaw = 0;
  let zoom = 1;
  let width = 0;
  let height = 0;
  let dpr = 1;
  let lastInteract = 0;
  let needsDraw = true;
  let stars: { x: number; y: number; s: number; p: number }[] = [];

  function project(n: Node, cy: number, sy_: number, cp: number, sp: number, unit: number) {
    const [x, y, z] = n.pos;
    const x1 = x * cy + z * sy_;
    const z1 = -x * sy_ + z * cy;
    const y2 = y * cp - z1 * sp;
    const z2 = y * sp + z1 * cp;
    const f = CAMERA / (CAMERA - z2);
    return { sx: width / 2 + x1 * unit * f, sy: height / 2 + y2 * unit * f, f, z: z2 };
  }

  function draw(now: number, dt: number) {
    if (!palette || !width) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const p = palette;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const bg = ctx.createRadialGradient(width / 2, height / 2, 0, width / 2, height / 2, Math.max(width, height) * 0.75);
    bg.addColorStop(0, rgba(mix(p.forestDark, p.forest, 0.18), 1));
    bg.addColorStop(0.55, rgba(mix(p.forestDark, [0, 0, 0], 0.55), 1));
    bg.addColorStop(1, rgba(mix(p.forestDark, [0, 0, 0], 0.88), 1));
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, width, height);

    for (const s of stars) {
      const tw = reduced ? 0.6 : 0.5 + 0.5 * Math.sin(now / 1100 + s.p);
      ctx.fillStyle = rgba(p.paper, 0.12 + 0.35 * tw * s.s);
      const x = (((s.x * width - yaw * 14 * s.s) % width) + width) % width;
      ctx.fillRect(x, s.y * height - pitch * 10 * s.s, s.s * 1.3, s.s * 1.3);
    }

    const unit = (Math.min(width, height) / 2) * 0.62 * zoom;
    const scale = Math.min(width, height) / 700;
    const cy = Math.cos(yaw);
    const sy_ = Math.sin(yaw);
    const cp = Math.cos(pitch);
    const sp = Math.sin(pitch);
    const k = 1 - Math.exp(-dt * 6);
    const maxR = Math.max(1, ...nodes.map((n) => n.rTarget));

    for (const n of nodes) {
      const mt = n.topic.rule?.mode === 'mute' ? 1 : 0;
      const ft = n.topic.rule?.mode === 'follow' ? 1 : 0;
      n.r += (n.rTarget - n.r) * k;
      n.mute += (mt - n.mute) * k;
      n.follow += (ft - n.follow) * k;
      const pr = project(n, cy, sy_, cp, sp, unit);
      n.sx = pr.sx;
      n.sy = pr.sy;
      n.depth = (pr.z + 1) / 2; // 0 far .. 1 near
      n.sr = n.r * scale * pr.f * Math.sqrt(zoom);
    }
    const order = [...nodes].sort((a, b) => a.depth - b.depth);

    for (const n of order) {
      const pulse = reduced ? 1 : 1 + 0.05 * Math.sin(now / 700 + n.phase) * (1 - n.mute);
      const lit = 1 - n.mute;
      const R = n.sr * pulse;
      const depthA = 0.45 + 0.55 * n.depth;
      const glowR = R * (1.4 + 2.6 * lit);

      const core = mix(mix([255, 255, 255], p.leaf, 0.35), mix(p.forestDark, [90, 100, 94], 0.6), n.mute);
      const halo = mix(p.leaf, p.gold, n.follow);
      const coreC = mix(core, mix(p.gold, [255, 255, 255], 0.35), n.follow);
      const glowA = (0.15 + 0.7 * lit) * depthA;

      const g = ctx.createRadialGradient(n.sx, n.sy, 0, n.sx, n.sy, glowR);
      g.addColorStop(0, rgba(coreC, depthA));
      g.addColorStop(Math.min(0.9, R / glowR), rgba(mix(halo, coreC, 0.25), glowA));
      g.addColorStop(1, rgba(halo, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(n.sx, n.sy, glowR, 0, Math.PI * 2);
      ctx.fill();

      if (n.follow > 0.02) {
        ctx.strokeStyle = rgba(p.gold, 0.9 * n.follow * depthA);
        ctx.lineWidth = Math.max(1.5, R * 0.14);
        ctx.beginPath();
        ctx.arc(n.sx, n.sy, R * 1.45, 0, Math.PI * 2);
        ctx.stroke();
      }

      const big = n.rTarget / maxR > 0.55;
      let la = big ? 0.95 : Math.max(0, (n.depth - 0.25) / 0.6) * 0.9;
      la *= 1 - 0.55 * n.mute;
      if (la > 0.03) {
        const fs = Math.round(Math.max(10, Math.min(18, 10 + (n.r - 5) * 0.28)) * Math.max(0.85, Math.min(1.25, scale)));
        ctx.font = `600 ${fs}px ui-rounded, ui-sans-serif, system-ui, sans-serif`;
        ctx.textBaseline = 'middle';
        ctx.shadowColor = rgba([0, 0, 0], 0.7 * la);
        ctx.shadowBlur = 4;
        ctx.fillStyle = rgba(mix(p.paper, p.gold, n.follow * 0.6), la);
        ctx.fillText(n.topic.label, n.sx + R * 1.5 + 4, n.sy);
        ctx.shadowBlur = 0;
      }
    }
  }

  function hit(x: number, y: number): Node | null {
    let best: Node | null = null;
    for (const n of nodes) {
      const rad = Math.max(n.sr * 1.3, 14);
      if (Math.hypot(n.sx - x, n.sy - y) <= rad && (!best || n.depth > best.depth)) best = n;
    }
    return best;
  }

  onMount(() => {
    readPalette();
    const mq = matchMedia('(prefers-reduced-motion: reduce)');
    const onMq = () => {
      reduced = mq.matches;
      needsDraw = true;
    };
    onMq();
    mq.addEventListener('change', onMq);

    let seed = 7;
    const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    stars = Array.from({ length: 140 }, () => ({ x: rnd(), y: rnd(), s: 0.4 + rnd() * 1.1, p: rnd() * 6.3 }));

    const resize = () => {
      const r = wrap.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = r.width;
      height = r.height;
      canvas.width = Math.max(1, Math.round(width * dpr));
      canvas.height = Math.max(1, Math.round(height * dpr));
      needsDraw = true;
    };
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);
    resize();

    // --- pointer handling ---
    const pointers = new Map<number, { x: number; y: number }>();
    let pinchDist = 0;
    let downNode: Node | null = null;
    let downAt = { x: 0, y: 0 };
    let moved = false;
    let longFired = false;
    let longTimer: ReturnType<typeof setTimeout> | undefined;
    let lastCtx = 0;

    const local = (e: PointerEvent | MouseEvent) => {
      const r = canvas.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    };
    const touch = () => {
      lastInteract = performance.now();
      needsDraw = true;
    };

    const onDown = (e: PointerEvent) => {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      canvas.setPointerCapture(e.pointerId);
      const pt = local(e);
      pointers.set(e.pointerId, pt);
      touch();
      if (pointers.size === 1) {
        downAt = pt;
        moved = false;
        longFired = false;
        vYaw = 0;
        downNode = hit(pt.x, pt.y);
        clearTimeout(longTimer);
        if (downNode) {
          const target = downNode;
          longTimer = setTimeout(() => {
            if (!moved && pointers.size === 1) {
              longFired = true;
              void quickToggle(target.topic);
            }
          }, LONG_PRESS_MS);
        }
      } else if (pointers.size === 2) {
        clearTimeout(longTimer);
        moved = true;
        const [a, b] = [...pointers.values()];
        pinchDist = Math.hypot(a.x - b.x, a.y - b.y);
      }
    };
    const onMove = (e: PointerEvent) => {
      const prev = pointers.get(e.pointerId);
      if (!prev) return;
      const pt = local(e);
      pointers.set(e.pointerId, pt);
      touch();
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (pinchDist > 0) zoom = Math.max(0.5, Math.min(3, zoom * (d / pinchDist)));
        pinchDist = d;
        return;
      }
      if (!moved && Math.hypot(pt.x - downAt.x, pt.y - downAt.y) > 6) {
        moved = true;
        clearTimeout(longTimer);
      }
      if (moved) {
        const dx = pt.x - prev.x;
        const dy = pt.y - prev.y;
        yaw += dx * 0.006;
        pitch = Math.max(-1.3, Math.min(1.3, pitch + dy * 0.006));
        vYaw = dx * 0.006 * 60;
      }
    };
    const onUp = (e: PointerEvent) => {
      if (!pointers.has(e.pointerId)) return;
      pointers.delete(e.pointerId);
      clearTimeout(longTimer);
      touch();
      if (pointers.size === 0) {
        if (!moved && !longFired && downNode) selected = downNode.topic;
        downNode = null;
      }
      pinchDist = 0;
    };
    const onContext = (e: MouseEvent) => {
      e.preventDefault();
      const now = performance.now();
      if (longFired || now - lastCtx < 400) return; // touch long-press already handled
      lastCtx = now;
      const pt = local(e);
      const n = hit(pt.x, pt.y);
      if (n) void quickToggle(n.topic);
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      zoom = Math.max(0.5, Math.min(3, zoom * Math.exp(-e.deltaY * 0.0015)));
      touch();
    };

    canvas.addEventListener('pointerdown', onDown);
    canvas.addEventListener('pointermove', onMove);
    canvas.addEventListener('pointerup', onUp);
    canvas.addEventListener('pointercancel', onUp);
    canvas.addEventListener('contextmenu', onContext);
    canvas.addEventListener('wheel', onWheel, { passive: false });

    // --- animation loop ---
    let raf = 0;
    let prevT = performance.now();
    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const dt = Math.min(0.1, (now - prevT) / 1000);
      prevT = now;
      const interacting = pointers.size > 0 || selected !== null;
      let rotating = false;
      if (!interacting) {
        if (Math.abs(vYaw) > 0.01) {
          yaw += vYaw * dt;
          vYaw *= Math.exp(-dt * 3);
          rotating = true;
        } else if (!reduced && now - lastInteract > 1500) {
          yaw += AUTO_ROTATE * dt;
          rotating = true;
        }
      }
      const settling = nodes.some(
        (n) =>
          Math.abs(n.r - n.rTarget) > 0.02 ||
          Math.abs(n.mute - (n.topic.rule?.mode === 'mute' ? 1 : 0)) > 0.01 ||
          Math.abs(n.follow - (n.topic.rule?.mode === 'follow' ? 1 : 0)) > 0.01
      );
      if (!reduced || needsDraw || rotating || settling || interacting) {
        draw(now, dt);
        needsDraw = false;
      }
    };
    raf = requestAnimationFrame(frame);

    void load();
    void loadHidden();

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      mq.removeEventListener('change', onMq);
      canvas.removeEventListener('pointerdown', onDown);
      canvas.removeEventListener('pointermove', onMove);
      canvas.removeEventListener('pointerup', onUp);
      canvas.removeEventListener('pointercancel', onUp);
      canvas.removeEventListener('contextmenu', onContext);
      canvas.removeEventListener('wheel', onWheel);
      clearTimeout(longTimer);
      clearTimeout(toastTimer);
    };
  });
</script>

<svelte:head><title>{$t('nav.ai')}</title></svelte:head>

<div class="ai" bind:this={wrap}>
  <!-- svelte-ignore a11y_no_interactive_element_to_noninteractive_role -->
  <canvas
    bind:this={canvas}
    role="img"
    aria-label={$t('topics.canvasLabel', { count: topics.length })}
  ></canvas>

  {#if !loading && topics.length === 0}
    <p class="empty">{$t('topics.empty')}</p>
  {/if}

  <div class="top">
    <div class="seg" role="radiogroup" aria-label={$t('topics.viewLabel')} tabindex="-1" onkeydown={onSegKey}>
      {#each VIEWS as v (v)}
        <button
          type="button"
          role="radio"
          data-view={v}
          aria-checked={view === v}
          tabindex={view === v ? 0 : -1}
          class:on={view === v}
          onclick={() => pickView(v)}
        >
          {$t('topics.view_' + v)}
        </button>
      {/each}
    </div>
    {#if hiddenCount > 0}
      <a class="hiddenbtn" href="/?hidden=1" title={$t('topics.hiddenTitle')}>
        {$t('topics.hiddenButton', { count: hiddenCount })}
      </a>
    {/if}
  </div>

  <ul class="legend" aria-hidden="true">
    <li><span class="dot active"></span>{$t('topics.legendActive')}</li>
    <li><span class="dot muted"></span>{$t('topics.legendMuted')}</li>
    <li><span class="dot followed"></span>{$t('topics.legendFollowed')}</li>
  </ul>

  <ul class="sr-only">
    {#each topics as tp (tp.id)}
      <li>
        <button type="button" onclick={() => (selected = tp)}>
          {$t('topics.nodeLabel', { label: tp.label, count: tp.unread_count, state: stateText(tp) })}
        </button>
      </li>
    {/each}
  </ul>

  {#if toast}<p class="toast" role="status">{toast}</p>{/if}

  <a class="fab" href="/chat" aria-label={$t('topics.chatButton')} title={$t('topics.chatButton')}>
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.6 8.6 0 0 1-3.6-.8L3 21l1.9-5.4A8.4 8.4 0 1 1 21 11.5z" />
    </svg>
  </a>
</div>

{#if selected}
  <TopicSheet topic={selected} onclose={() => (selected = null)} onchange={changed} />
{/if}

<style>
  .ai {
    position: fixed;
    top: var(--nav-h, 3rem);
    left: 0;
    right: 0;
    bottom: 0;
    overflow: hidden;
    background: var(--gator-forest-dark);
  }
  canvas {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    touch-action: none;
    cursor: grab;
    user-select: none;
    -webkit-user-select: none;
    -webkit-touch-callout: none;
  }
  canvas:active {
    cursor: grabbing;
  }
  .top {
    position: absolute;
    top: 0.7rem;
    left: 0.7rem;
    right: 0.7rem;
    display: flex;
    justify-content: center;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
    pointer-events: none;
  }
  .top > * {
    pointer-events: auto;
  }
  .seg {
    display: inline-flex;
    padding: 3px;
    gap: 2px;
    border-radius: var(--radius-pill);
    background: rgba(11, 61, 42, 0.6);
    border: 1px solid rgba(248, 247, 241, 0.18);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
  }
  .seg button {
    border: none;
    background: transparent;
    color: var(--gator-paper);
    opacity: 0.75;
    border-radius: var(--radius-pill);
    padding: 0.3rem 0.85rem;
    font-size: 0.85rem;
    font-weight: 600;
  }
  .seg button.on {
    background: var(--gator-forest);
    opacity: 1;
  }
  .seg button:focus-visible,
  .hiddenbtn:focus-visible,
  .fab:focus-visible {
    outline: 2px solid var(--gator-gold);
    outline-offset: 2px;
  }
  .hiddenbtn {
    color: var(--gator-paper);
    text-decoration: none;
    font-size: 0.85rem;
    font-weight: 600;
    padding: 0.32rem 0.85rem;
    border-radius: var(--radius-pill);
    border: 1px dashed rgba(248, 247, 241, 0.4);
    background: rgba(11, 61, 42, 0.6);
  }
  .legend {
    position: absolute;
    left: 0.9rem;
    bottom: 0.9rem;
    margin: 0;
    padding: 0;
    list-style: none;
    display: flex;
    gap: 0.8rem;
    font-size: 0.75rem;
    color: var(--gator-paper);
    opacity: 0.8;
    pointer-events: none;
  }
  .legend li {
    display: flex;
    align-items: center;
    gap: 0.3rem;
  }
  .dot {
    width: 0.65rem;
    height: 0.65rem;
    border-radius: 50%;
  }
  .dot.active {
    background: radial-gradient(circle, var(--gator-paper), var(--gator-leaf));
    box-shadow: 0 0 8px var(--gator-leaf);
  }
  .dot.muted {
    background: var(--gator-muted);
    opacity: 0.55;
  }
  .dot.followed {
    background: var(--gator-gold);
    box-shadow: 0 0 0 2px var(--gator-forest-dark), 0 0 0 3.5px var(--gator-gold);
  }
  .empty {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    text-align: center;
    margin: 0;
    padding: 2rem;
    color: var(--gator-paper);
    opacity: 0.85;
    pointer-events: none;
  }
  .toast {
    position: absolute;
    left: 50%;
    bottom: 4.5rem;
    transform: translateX(-50%);
    margin: 0;
    padding: 0.45rem 1rem;
    border-radius: var(--radius-pill);
    background: var(--gator-paper);
    color: var(--gator-ink);
    font-size: 0.85rem;
    font-weight: 600;
    max-width: 90%;
    box-shadow: var(--gator-shadow);
  }
  .fab {
    position: absolute;
    right: 1rem;
    bottom: calc(1rem + env(safe-area-inset-bottom));
    width: 3.2rem;
    height: 3.2rem;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--gator-gold);
    color: var(--gator-forest-dark);
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
  }
  .fab:hover {
    background: var(--gator-gold-hover);
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
    list-style: none;
  }
</style>
