<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { t } from '$lib/i18n';
  import TopicSheet from '$lib/components/TopicSheet.svelte';
  import { removeRuleAsking } from '$lib/ruleRemoval';
  import GroupSheet from '$lib/components/GroupSheet.svelte';
  import type { Topic, TopicGroup, TopicMode } from '$lib/types';
  import {
    GROUP_LIGHT,
    GROUP_SAT,
    declutterLabels,
    distance3,
    facingAngles,
    groupCss,
    hslToRgb,
    hueFromPosition,
    tintHue,
    topNFlags,
    type LabelBox
  } from '$lib/constellation';

  type View = 'mix' | 'volume' | 'unread' | 'recent';
  const VIEWS: View[] = ['mix', 'volume', 'unread', 'recent'];
  const LONG_PRESS_MS = 500;
  const AUTO_ROTATE = 0.07; // rad/s
  const CAMERA = 3; // perspective distance (unit sphere radius = 1)
  const TOP_STARS = 20;
  const NEIGHBOURHOOD = 1.2; // scene units kept bright around the focused star
  const DIM_ALPHA = 0.15;
  const FOCUS_ZOOM = 1.5;
  const DECLUTTER_MS = 200;

  const DOUBLE_TAP_MS = 350;

  // A star is a topic, or a topic group (galaxy) dressed as one so both levels share the canvas.
  type Star = Topic & { group?: TopicGroup };
  type Level = 'groups' | 'flat' | 'manual' | number; // flat = server without topic groups

  type RGB = [number, number, number];
  interface Node {
    topic: Star;
    pos: [number, number, number];
    r: number;
    rTarget: number;
    mute: number;
    follow: number;
    phase: number;
    score: number;
    full: boolean;
    hue: number;
    dim: number;
    labelA: number;
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

  let topics = $state<Star[]>([]);
  let level = $state<Level>('groups');
  let groups = $state<TopicGroup[]>([]);
  let selectedGroup = $state<TopicGroup | null>(null);
  let loading = $state(true);
  let view = $state<View>('mix');
  let ruleFilter = $state<'all' | 'follow' | 'mute'>('all');
  let selected = $state<Topic | null>(null);
  let focused = $state<Star | null>(null);
  let showAll = $state(false);
  let ranked = $state<Star[]>([]);
  let listOpen = $state(false);
  let hiddenCount = $state(0);
  let manualCount = $state(0);
  let toast = $state('');
  let addQuery = $state('');
  let addMode = $state<TopicMode>('follow');
  let addDuration = $state<'7d' | '30d' | 'forever'>('forever');
  let adding = $state(false);
  let addError = $state('');
  const ADD_DURATIONS = ['7d', '30d', 'forever'] as const;
  const ADD_ERRORS = ['invalid_length', 'too_many', 'embedding_failed'];
  const currentGroup = $derived(typeof level === 'number' ? groups.find((g) => g.id === level) : undefined);
  const atGroups = () => level === 'groups';
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
  function scores(list: Star[], v: View): number[] {
    if (atGroups()) {
      const top = Math.max(1, ...list.map((x) => x.story_count));
      return list.map((x) => x.story_count / top);
    }
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
    const flags = topNFlags(sc, TOP_STARS);
    nodes.forEach((n, i) => {
      n.score = sc[i];
      n.full = showAll || atGroups() || flags[i];
      // normalised so the biggest topic of the current view is always the largest
      n.rTarget = n.full ? 5 + 21 * Math.sqrt(sc[i] / top) : 2.5;
    });
    ranked = [...nodes]
      .sort((a, b) => b.score - a.score)
      .map((n) => n.topic);
    needsDraw = true;
  }

  const groupStar = (g: TopicGroup): Star => ({
    id: g.id,
    label: g.label,
    description: g.description,
    story_count: g.story_count,
    source_count: g.topic_count,
    unread_count: g.unread_count,
    recent_count: 0,
    last_story_at: '',
    rule: g.rule ? { id: g.rule.id, mode: g.rule.mode } : null,
    position: g.position,
    group: g,
    followed_topic_count: g.followed_topic_count,
    muted_topic_count: g.muted_topic_count
  });

  function hueOf(tp: Star, p: [number, number, number]): number {
    if (tp.group) return tp.group.hue;
    return currentGroup ? tintHue(currentGroup.hue, p) : hueFromPosition(p);
  }

  function syncNodes() {
    const old = new Map(nodes.map((n) => [n.topic.id, n]));
    nodes = topics.map((tp, i) => {
      const prev = old.get(tp.id);
      const p = tp.position ?? [0, 0, 0];
      if (prev) {
        prev.topic = tp;
        prev.pos = p;
        prev.hue = hueOf(tp, p);
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
        score: 0,
        full: true,
        hue: hueOf(tp, p),
        dim: 1,
        labelA: 0,
        sx: 0,
        sy: 0,
        sr: 0,
        depth: 0
      };
    });
    applyView();
  }

  const stateOf = (tp: Star) => (tp.rule?.mode === 'mute' ? 'muted' : tp.rule?.mode === 'follow' ? 'followed' : 'active');
  const stateText = (tp: Star) => $t('topics.state_' + stateOf(tp));

  async function fetchLevel(lv: Level): Promise<{ stars: Star[]; level: Level }> {
    if (lv === 'groups') {
      groups = await api.topicGroups.list().catch(() => []);
      manualCount = (await api.topics.list(true, { manualOnly: true }).catch(() => [])).length;
      if (groups.length) {
        const selected = groups.filter((group) => {
          if (ruleFilter === 'follow') {
            return group.rule?.mode === 'follow' || (group.followed_topic_count ?? 0) > 0;
          }
          if (ruleFilter === 'mute') {
            return group.rule?.mode === 'mute' || (group.muted_topic_count ?? 0) > 0;
          }
          return true;
        });
        return { stars: selected.map(groupStar), level: lv };
      }
      return { stars: await api.topics.list(true), level: 'flat' }; // server without groups
    }
    if (lv === 'manual') return { stars: await api.topics.list(true, { manualOnly: true }), level: lv };
    if (lv === 'flat') return { stars: await api.topics.list(true), level: lv };
    const children = await api.topics.list(true, { group: lv });
    return {
      stars: ruleFilter === 'all'
        ? children
        : children.filter((topic) => topic.rule?.mode === ruleFilter),
      level: lv
    };
  }

  async function load() {
    const asked = level;
    let found: Star[] = [];
    let landed: Level = asked;
    try {
      const r = await fetchLevel(asked);
      found = r.stars.filter((x) => x.position);
      landed = r.level;
    } catch {
      found = [];
    }
    if (level !== asked) return; // the user moved on while this was loading
    level = landed;
    topics = found;
    syncNodes();
    if (focused) focused = topics.find((x) => x.id === focused!.id) ?? null;
    loading = false;
  }

  async function loadHidden() {
    try {
      hiddenCount = (await api.stories.counts()).ignored ?? 0;
    } catch {
      hiddenCount = 0;
    }
  }

  function enterLevel(next: Level) {
    level = next;
    focused = null;
    selected = null;
    selectedGroup = null;
    focusAnim = null;
    nodes = [];
    topics = [];
    ranked = [];
    loading = true;
    showAll = false;
    zoom = 1;
    shownLabels = new Set();
    lastDeclutter = -1e9;
    needsDraw = true;
    void load();
  }

  const zoomIntoGroup = (g: TopicGroup) => enterLevel(g.id);
  const openDetails = (s: Star) => {
    if (s.group) selectedGroup = s.group;
    else selected = s;
  };

  async function changed() {
    await Promise.all([load(), loadHidden()]);
  }

  function say(msg: string) {
    toast = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast = ''), 2600);
  }

  async function addTopic(e: Event) {
    e.preventDefault();
    const q = addQuery.trim();
    if (!q || adding) return;
    adding = true;
    addError = '';
    try {
      await api.topics.createManual(q, addMode, addDuration);
      addQuery = '';
      await changed();
    } catch (err) {
      const m = err instanceof Error ? err.message : '';
      addError = ADD_ERRORS.includes(m) ? $t('topics.err_' + m) : m || $t('topics.error');
    } finally {
      adding = false;
    }
  }

  async function quickToggle(tp: Star) {
    try {
      if (tp.group) {
        if (tp.rule) {
          if (await removeRuleAsking(tp.rule.id)) say($t('topics.toastReset', { label: tp.label }));
        } else {
          await api.topicGroups.createRule(tp.group.id, 'mute', 'fade');
          say($t('topics.toastMuted', { label: tp.label }));
        }
      } else if (tp.manual && tp.rule) {
        const next: TopicMode = tp.rule.mode === 'mute' ? 'follow' : 'mute';
        await api.topics.updateRule(tp.rule.id, next);
        say($t(next === 'mute' ? 'topics.toastMuted' : 'topics.toastFollowed', { label: tp.label }));
      } else if (tp.rule) {
        if (await removeRuleAsking(tp.rule.id)) say($t('topics.toastReset', { label: tp.label }));
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
  let focusAnim: { yaw: number; pitch: number; zoom: number } | null = null;
  let shownLabels = new Set<number>();
  let lastDeclutter = -1e9;
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
    const focusNode = focused ? nodes.find((x) => x.topic.id === focused!.id) : undefined;

    for (const n of nodes) {
      const mt = n.topic.rule?.mode === 'mute' ? 1 : 0;
      const ft = n.topic.rule?.mode === 'follow' ? 1 : 0;
      n.r += (n.rTarget - n.r) * k;
      n.mute += (mt - n.mute) * k;
      n.follow += (ft - n.follow) * k;
      const dimT = focusNode && distance3(n.pos, focusNode.pos) > NEIGHBOURHOOD ? DIM_ALPHA : 1;
      n.dim = reduced ? dimT : n.dim + (dimT - n.dim) * k;
      const pr = project(n, cy, sy_, cp, sp, unit);
      n.sx = pr.sx;
      n.sy = pr.sy;
      n.depth = (pr.z + 1) / 2; // 0 far .. 1 near
      n.sr = n.r * scale * pr.f * Math.sqrt(zoom);
    }
    const order = [...nodes].sort((a, b) => a.depth - b.depth);

    const fontOf = (n: Node) =>
      Math.round(Math.max(10, Math.min(18, 10 + (n.r - 5) * 0.28)) * Math.max(0.85, Math.min(1.25, scale)));
    if (now - lastDeclutter > DECLUTTER_MS) {
      lastDeclutter = now;
      const boxes: LabelBox[] = [];
      for (const n of nodes) {
        if (!n.full) continue;
        const isFocus = n === focusNode;
        if (!isFocus && n.dim < 0.5) continue;
        const fs = fontOf(n);
        ctx.font = `600 ${fs}px ui-rounded, ui-sans-serif, system-ui, sans-serif`;
        boxes.push({
          id: n.topic.id,
          score: n.score,
          x: n.sx + n.sr * 1.5 + 4,
          y: n.sy - fs / 2,
          w: ctx.measureText(n.topic.label).width,
          h: fs,
          force: isFocus || !!n.topic.group
        });
      }
      shownLabels = declutterLabels(boxes);
    }

    for (const n of order) {
      const pulse = reduced ? 1 : 1 + 0.05 * Math.sin(now / 700 + n.phase) * (1 - n.mute);
      const lit = 1 - n.mute;
      const R = n.sr * pulse;
      const smallF = n.full ? 1 : 0.55; // unlabelled dots stay dim
      const depthA = (0.45 + 0.55 * n.depth) * n.dim * smallF;
      const glowR = R * (n.full ? 1.3 + 0.35 * lit : 1.2);

      const hue = n.topic.group ? hslToRgb(n.hue, GROUP_SAT, GROUP_LIGHT) : hslToRgb(n.hue, 0.62, 0.52);
      const core = mix(mix([255, 255, 255], hue, 0.35), mix(p.forestDark, [90, 100, 94], 0.6), n.mute);
      const halo = mix(hue, p.gold, n.follow);
      const coreC = mix(core, mix(p.gold, [255, 255, 255], 0.35), n.follow);
      const glowA = (0.15 + 0.7 * lit) * depthA;

      // short, faint halo first, then a solid disc with a crisp edge
      const g = ctx.createRadialGradient(n.sx, n.sy, R * 0.9, n.sx, n.sy, glowR);
      g.addColorStop(0, rgba(halo, glowA * 0.45));
      g.addColorStop(1, rgba(halo, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(n.sx, n.sy, glowR, 0, Math.PI * 2);
      ctx.fill();

      const body = mix(halo, coreC, 0.2);
      const d = ctx.createRadialGradient(n.sx - R * 0.3, n.sy - R * 0.3, 0, n.sx, n.sy, R);
      d.addColorStop(0, rgba(coreC, depthA));
      d.addColorStop(1, rgba(body, depthA * (0.55 + 0.45 * lit)));
      ctx.fillStyle = d;
      ctx.beginPath();
      ctx.arc(n.sx, n.sy, R, 0, Math.PI * 2);
      ctx.fill();

      if (n.topic.manual) {
        ctx.save();
        ctx.setLineDash([Math.max(2, R * 0.28), Math.max(2, R * 0.2)]);
        ctx.strokeStyle = rgba(p.paper, 0.85 * depthA * (1 - 0.5 * n.mute));
        ctx.lineWidth = Math.max(1.2, R * 0.1);
        ctx.beginPath();
        ctx.arc(n.sx, n.sy, R * (n.follow > 0.02 ? 1.9 : 1.55), 0, Math.PI * 2);
        ctx.stroke();
        ctx.restore();
      }

      if (n.follow > 0.02) {
        ctx.strokeStyle = rgba(p.gold, 0.9 * n.follow * depthA);
        ctx.lineWidth = Math.max(1.5, R * 0.14);
        ctx.beginPath();
        ctx.arc(n.sx, n.sy, R * 1.45, 0, Math.PI * 2);
        ctx.stroke();
      }

      const isFocus = n === focusNode;
      const wantLabel = n.full && shownLabels.has(n.topic.id);
      n.labelA += ((wantLabel ? 1 : 0) - n.labelA) * (reduced ? 1 : k);
      let la = n.labelA * (isFocus ? 1 : Math.max(0.35, Math.min(1, 0.45 + n.depth)) * 0.95);
      la *= (1 - 0.55 * n.mute) * (isFocus ? 1 : n.dim);
      if (la > 0.03) {
        const fs = fontOf(n);
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
      const rad = Math.max(n.sr * 1.3, n.full ? 14 : 10);
      if (Math.hypot(n.sx - x, n.sy - y) <= rad && (!best || n.depth > best.depth)) best = n;
    }
    return best;
  }

  function focusTopic(tp: Star) {
    const n = nodes.find((x) => x.topic.id === tp.id);
    focused = tp;
    if (!n) return;
    const a = facingAngles(n.pos, yaw);
    vYaw = 0;
    if (reduced) {
      yaw = a.yaw;
      pitch = a.pitch;
      zoom = FOCUS_ZOOM;
      focusAnim = null;
    } else {
      focusAnim = { ...a, zoom: FOCUS_ZOOM };
    }
    lastInteract = performance.now();
    needsDraw = true;
  }
  function clearFocus() {
    focused = null;
    focusAnim = null;
    lastInteract = performance.now();
    needsDraw = true;
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
    let lastTap = { id: Number.NaN, at: 0 };

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
      focusAnim = null;
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
        if (!moved && !longFired) {
          const now = performance.now();
          if (!downNode) clearFocus();
          else if (downNode.topic.group && lastTap.id === downNode.topic.id && now - lastTap.at < DOUBLE_TAP_MS) {
            lastTap = { id: Number.NaN, at: 0 };
            zoomIntoGroup(downNode.topic.group);
          } else {
            const tapped = downNode.topic;
            lastTap = { id: tapped.id, at: now };
            if (focused?.id === tapped.id) openDetails(tapped);
            else focusTopic(tapped);
          }
        }
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
      focusAnim = null;
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
      const interacting = pointers.size > 0 || selected !== null || focused !== null;
      let rotating = false;
      if (focusAnim && pointers.size === 0) {
        const f = 1 - Math.exp(-dt * 5);
        yaw += (focusAnim.yaw - yaw) * f;
        pitch += (focusAnim.pitch - pitch) * f;
        zoom += (focusAnim.zoom - zoom) * f;
        rotating = true;
        if (Math.abs(focusAnim.yaw - yaw) + Math.abs(focusAnim.pitch - pitch) + Math.abs(focusAnim.zoom - zoom) < 0.002) {
          focusAnim = null;
        }
      }
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
          Math.abs(n.follow - (n.topic.rule?.mode === 'follow' ? 1 : 0)) > 0.01 ||
          Math.abs(n.labelA - (n.full && shownLabels.has(n.topic.id) ? 1 : 0)) > 0.02 ||
          Math.abs(n.dim - (focused ? n.dim : 1)) > 0.01
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

<svelte:window onkeydown={(e) => e.key === 'Escape' && !selected && focused && clearFocus()} />

<div class="ai">
<div class="stage" bind:this={wrap}>
  <!-- svelte-ignore a11y_no_interactive_element_to_noninteractive_role -->
  <canvas
    bind:this={canvas}
    role="img"
    aria-label={$t(atGroups() ? 'topics.canvasLabelGroups' : 'topics.canvasLabel', { count: topics.length })}
  ></canvas>

  {#if !loading && topics.length === 0}
    <p class="empty">
      {$t(level === 'manual' ? 'topics.manualEmpty' : typeof level === 'number' ? 'topics.groupEmpty' : 'topics.empty')}
    </p>
  {/if}

  <div class="top">
    {#if level === 'manual' || typeof level === 'number'}
      <nav class="crumbs" aria-label={$t('topics.breadcrumbLabel')}>
        <button type="button" class="pill" onclick={() => enterLevel('groups')}>
          ‹ {$t('topics.breadcrumbRoot')}
        </button>
        <span class="crumb" aria-current="page">
          {#if currentGroup}<span class="swatch" style:background={groupCss(currentGroup.hue)}></span>{/if}
          {level === 'manual' ? $t('topics.myTopics') : (currentGroup?.label ?? '')}
        </span>
      </nav>
    {/if}
    {#if !atGroups()}
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
    {#if level !== 'manual'}
      <button type="button" class="pill" aria-pressed={showAll} class:on={showAll} onclick={() => { showAll = !showAll; applyView(); }}>
        {$t('topics.showAll')}
      </button>
      <div class="rulefilter" role="group" aria-label={$t('topics.ruleFilterLabel')}>
        <button type="button" class:active={ruleFilter === 'all'} onclick={() => { ruleFilter = 'all'; void load(); }}>
          {$t('topics.ruleFilterAll')}
        </button>
        <button type="button" class:active={ruleFilter === 'follow'} onclick={() => { ruleFilter = 'follow'; void load(); }}>
          {$t('topics.ruleFilterFollowed')}
        </button>
        <button type="button" class:active={ruleFilter === 'mute'} onclick={() => { ruleFilter = 'mute'; void load(); }}>
          {$t('topics.ruleFilterMuted')}
        </button>
      </div>
    {/if}
    {:else}
      <div class="rulefilter" role="group" aria-label={$t('topics.ruleFilterLabel')}>
        <button type="button" class:active={ruleFilter === 'all'} onclick={() => { ruleFilter = 'all'; void load(); }}>
          {$t('topics.ruleFilterAll')}
        </button>
        <button type="button" class:active={ruleFilter === 'follow'} onclick={() => { ruleFilter = 'follow'; void load(); }}>
          {$t('topics.ruleFilterFollowed')}
        </button>
        <button type="button" class:active={ruleFilter === 'mute'} onclick={() => { ruleFilter = 'mute'; void load(); }}>
          {$t('topics.ruleFilterMuted')}
        </button>
      </div>
      {#if manualCount > 0}
        <button type="button" class="pill" onclick={() => enterLevel('manual')}>{$t('topics.myTopics')}</button>
      {/if}
    {/if}
    <button type="button" class="pill listtoggle" aria-expanded={listOpen} onclick={() => (listOpen = !listOpen)}>
      {$t(atGroups() ? 'topics.groupsTitle' : 'topics.listTitle')}
    </button>
    {#if hiddenCount > 0}
      <a class="hiddenbtn" href="/?hidden=1" title={$t('topics.hiddenTitle')}>
        {$t('topics.hiddenButton', { count: hiddenCount })}
      </a>
    {/if}
  </div>

  {#if level === 'manual' || level === 'flat'}
  <form class="add" onsubmit={addTopic}>
    <input
      type="text"
      bind:value={addQuery}
      maxlength="120"
      placeholder={$t('topics.addPlaceholder')}
      aria-label={$t('topics.addLabel')}
      disabled={adding}
    />
    <select bind:value={addMode} aria-label={$t('topics.addMode')}>
      <option value="follow">{$t('topics.follow')}</option>
      <option value="mute">{$t('topics.mute')}</option>
    </select>
    <select bind:value={addDuration} aria-label={$t('topics.addDuration')}>
      {#each ADD_DURATIONS as d (d)}<option value={d}>{$t('topics.duration_' + d)}</option>{/each}
    </select>
    <button type="submit" disabled={adding || !addQuery.trim()}>{$t('topics.addButton')}</button>
    {#if addError}<p class="adderr" role="alert">{addError}</p>{/if}
  </form>
  {/if}

  <ul class="legend" aria-hidden="true">
    <li><span class="dot active"></span>{$t('topics.legendActive')}</li>
    <li><span class="dot muted"></span>{$t('topics.legendMuted')}</li>
    <li><span class="dot followed"></span>{$t('topics.legendFollowed')}</li>
    <li><span class="dot manual"></span>{$t('topics.legendManual')}</li>
  </ul>

  <ul class="sr-only">
    {#each topics as tp (tp.id)}
      <li>
        <button type="button" onclick={() => openDetails(tp)}>
          {$t(tp.group ? 'topics.nodeLabelGroup' : tp.manual ? 'topics.nodeLabelManual' : 'topics.nodeLabel', {
            label: tp.label,
            count: tp.unread_count,
            state: stateText(tp)
          })}
        </button>
      </li>
    {/each}
  </ul>

  {#if focused}
    <div class="focuscard" role="group" aria-label={focused.label}>
      <div class="ftext">
        <strong>{focused.label}</strong>
        {#if focused.group}
          {#if focused.description}<span class="fdesc">{focused.description}</span>{/if}
          <span>
            {$t('topics.topicCount', { count: focused.group.topic_count })} ·
            {$t('topics.primaryCount', { count: focused.story_count, unread: focused.unread_count })} ·
            {$t('topics.coveredCount', {
              count: focused.covered_story_count ?? focused.story_count,
              unread: focused.covered_unread_count ?? focused.unread_count
            })}
          </span>
        {:else}
          <span>
            {$t('topics.primaryCount', { count: focused.story_count, unread: focused.unread_count })} ·
            {$t('topics.coveredCount', {
              count: focused.covered_story_count ?? focused.story_count,
              unread: focused.covered_unread_count ?? focused.unread_count
            })}
          </span>
        {/if}
      </div>
      {#if focused.group}
        {@const fg = focused.group}
        <button type="button" class="fdetails" onclick={() => zoomIntoGroup(fg)}>{$t('topics.zoomIn')}</button>
      {/if}
      <button
        type="button"
        class={focused.group ? 'fsecondary' : 'fdetails'}
        onclick={() => openDetails(focused!)}>{$t('topics.details')}</button>
      <button type="button" class="fclose" aria-label={$t('topics.clearFocus')} title={$t('topics.clearFocus')} onclick={clearFocus}>×</button>
    </div>
  {/if}

  {#if toast}<p class="toast" role="status">{toast}</p>{/if}

  <a class="fab" href="/chat" aria-label={$t('topics.chatButton')} title={$t('topics.chatButton')}>
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.6 8.6 0 0 1-3.6-.8L3 21l1.9-5.4A8.4 8.4 0 1 1 21 11.5z" />
    </svg>
  </a>
</div>

<aside class="panel" class:open={listOpen} aria-label={$t(atGroups() ? 'topics.groupsTitle' : 'topics.listTitle')}>
  <h2>{$t(atGroups() ? 'topics.groupsTitle' : 'topics.listTitle')}</h2>
  <ul>
    {#each ranked as tp (tp.id)}
      {@const st = stateOf(tp)}
      <li class:isfocus={focused?.id === tp.id}>
        <button type="button" class="rowmain" onclick={() => focusTopic(tp)}>
          <span class="sd {st}" aria-hidden="true"></span>
          <span class="rl">{tp.label}</span>
          <span class="rc">
            {#if tp.group && ((tp.group.followed_topic_count ?? 0) + (tp.group.muted_topic_count ?? 0) > 0)}
              <span class="groupmarks">
                {#if tp.group.followed_topic_count}<b class="followmark">★ {tp.group.followed_topic_count}</b>{/if}
                {#if tp.group.muted_topic_count}<b class="mutemark">× {tp.group.muted_topic_count}</b>{/if}
              </span>
            {:else}
              {tp.unread_count}
              {#if (tp.covered_unread_count ?? tp.unread_count) !== tp.unread_count}
                <span class="covered">/ {tp.covered_unread_count}</span>
              {/if}
            {/if}
          </span>
          <span class="sr-only">{stateText(tp)}</span>
        </button>
        <button type="button" class="rowbtn" onclick={() => quickToggle(tp)}>
          {$t(!tp.rule ? 'topics.mute' : tp.manual ? (tp.rule.mode === 'mute' ? 'topics.follow' : 'topics.mute') : tp.rule.mode === 'mute' ? 'topics.unmute' : 'topics.unfollow')}
        </button>
        {#if tp.group}
          {@const g = tp.group}
          <button type="button" class="rowbtn" onclick={() => zoomIntoGroup(g)}>{$t('topics.zoomIn')}</button>
        {/if}
        <button type="button" class="rowbtn" onclick={() => openDetails(tp)}>{$t('topics.details')}</button>
      </li>
    {/each}
  </ul>
</aside>
</div>

{#if selected}
  <TopicSheet topic={selected} onclose={() => (selected = null)} onchange={changed} />
{/if}
{#if selectedGroup}
  {@const sg = selectedGroup}
  <GroupSheet
    group={sg}
    onclose={() => (selectedGroup = null)}
    onchange={changed}
    onzoom={() => zoomIntoGroup(sg)} />
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
    display: flex;
  }
  .stage {
    position: relative;
    flex: 1;
    min-width: 0;
    overflow: hidden;
  }
  .panel {
    display: none;
    flex-direction: column;
    width: 21rem;
    flex: none;
    background: rgba(11, 61, 42, 0.96);
    border-left: 1px solid rgba(248, 247, 241, 0.15);
    color: var(--gator-paper);
    overflow: hidden;
  }
  .panel h2 {
    margin: 0;
    padding: 0.7rem 0.9rem 0.4rem;
    font-size: 0.9rem;
    opacity: 0.85;
  }
  .panel ul {
    list-style: none;
    margin: 0;
    padding: 0 0.4rem 0.6rem;
    overflow-y: auto;
    flex: 1;
  }
  .panel li {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.1rem 0.2rem;
    border-radius: 0.5rem;
  }
  .panel li.isfocus {
    background: rgba(248, 247, 241, 0.12);
  }
  .rowmain {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    text-align: left;
    border: none;
    background: transparent;
    color: inherit;
    padding: 0.35rem 0.3rem;
    font-size: 0.85rem;
  }
  .rl {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .rc {
    font-variant-numeric: tabular-nums;
    opacity: 0.75;
    font-size: 0.78rem;
  }
  .sd {
    width: 0.6rem;
    height: 0.6rem;
    border-radius: 50%;
    flex: none;
  }
  .sd.active {
    background: var(--gator-leaf);
  }
  .sd.followed {
    background: var(--gator-gold);
  }
  .sd.muted {
    background: var(--gator-muted);
    opacity: 0.6;
  }
  .rowbtn {
    border: 1px solid rgba(248, 247, 241, 0.3);
    background: transparent;
    color: inherit;
    border-radius: var(--radius-pill);
    font-size: 0.72rem;
    padding: 0.18rem 0.55rem;
    white-space: nowrap;
  }
  .rowbtn:hover,
  .rowmain:hover {
    background: rgba(248, 247, 241, 0.1);
  }
  .rowbtn:focus-visible,
  .rowmain:focus-visible,
  .pill:focus-visible,
  .focuscard button:focus-visible {
    outline: 2px solid var(--gator-gold);
    outline-offset: 2px;
  }
  .pill {
    color: var(--gator-paper);
    font-size: 0.85rem;
    font-weight: 600;
    padding: 0.32rem 0.85rem;
    border-radius: var(--radius-pill);
    border: 1px solid rgba(248, 247, 241, 0.3);
    background: rgba(11, 61, 42, 0.6);
  }
  .pill.on {
    background: var(--gator-forest);
    border-color: var(--gator-gold);
  }
  .rulefilter {
    display: inline-flex;
    padding: 0.2rem;
    gap: 0.15rem;
    border-radius: var(--radius-pill);
    background: rgba(248, 247, 241, 0.12);
  }
  .rulefilter button {
    border: 0;
    border-radius: var(--radius-pill);
    padding: 0.28rem 0.7rem;
    color: var(--gator-paper);
    background: transparent;
    font-weight: 600;
  }
  .rulefilter button.active {
    color: var(--gator-forest-dark);
    background: var(--gator-gold);
  }
  .groupmarks {
    display: inline-flex;
    gap: 0.3rem;
  }
  .followmark { color: var(--gator-gold); }
  .mutemark { color: var(--gator-muted); }
  .listtoggle {
    display: inline-block;
  }
  .focuscard {
    position: absolute;
    left: 50%;
    bottom: 3.2rem;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 0.6rem;
    max-width: min(92%, 26rem);
    padding: 0.5rem 0.6rem 0.5rem 0.9rem;
    border-radius: 1rem;
    background: var(--gator-paper);
    color: var(--gator-ink);
    box-shadow: var(--gator-shadow);
  }
  .ftext {
    display: flex;
    flex-direction: column;
    min-width: 0;
    font-size: 0.85rem;
  }
  .ftext strong {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .ftext span {
    opacity: 0.7;
    font-size: 0.78rem;
  }
  .fdetails {
    border: none;
    border-radius: var(--radius-pill);
    background: var(--gator-forest);
    color: var(--gator-paper);
    font-weight: 600;
    font-size: 0.82rem;
    padding: 0.35rem 0.85rem;
  }
  .fsecondary {
    border: 1px solid var(--gator-forest);
    border-radius: var(--radius-pill);
    background: transparent;
    color: var(--gator-forest);
    font-weight: 600;
    font-size: 0.82rem;
    padding: 0.35rem 0.85rem;
  }
  .ftext .fdesc {
    white-space: normal;
    opacity: 0.85;
  }
  .crumbs {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 0;
  }
  .crumb {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--gator-paper);
    font-weight: 700;
    font-size: 0.9rem;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .swatch {
    width: 0.7rem;
    height: 0.7rem;
    border-radius: 50%;
    flex: none;
  }
  .fclose {
    border: none;
    background: transparent;
    color: inherit;
    font-size: 1.3rem;
    line-height: 1;
    padding: 0.1rem 0.5rem;
  }
  @media (min-width: 900px) {
    .panel {
      display: flex;
    }
    .listtoggle {
      display: none;
    }
  }
  @media (max-width: 899px) {
    .panel.open {
      display: flex;
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      width: auto;
      height: 50%;
      border-left: none;
      border-top: 1px solid rgba(248, 247, 241, 0.2);
      z-index: 30;
    }
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
  .add {
    position: absolute;
    top: 3.4rem;
    left: 0.7rem;
    right: 0.7rem;
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    gap: 0.35rem;
    pointer-events: none;
  }
  .add > * {
    pointer-events: auto;
  }
  .add input,
  .add select {
    font-size: 0.85rem;
    padding: 0.3rem 0.7rem;
    border-radius: var(--radius-pill);
    border: 1px solid rgba(248, 247, 241, 0.25);
    background: rgba(11, 61, 42, 0.75);
    color: var(--gator-paper);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
  }
  .add input {
    width: min(15rem, 100%);
  }
  .add input::placeholder {
    color: rgba(248, 247, 241, 0.6);
  }
  .add button {
    font-size: 0.85rem;
    font-weight: 600;
    border: none;
    border-radius: var(--radius-pill);
    padding: 0.3rem 0.9rem;
    background: var(--gator-gold);
    color: var(--gator-forest-dark);
  }
  .add button:disabled {
    opacity: 0.5;
  }
  .add input:focus-visible,
  .add select:focus-visible,
  .add button:focus-visible {
    outline: 2px solid var(--gator-gold);
    outline-offset: 2px;
  }
  .adderr {
    flex-basis: 100%;
    margin: 0;
    text-align: center;
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--gator-paper);
    text-shadow: 0 1px 4px rgba(0, 0, 0, 0.8);
  }
  .dot.manual {
    border: 1.5px dashed var(--gator-paper);
    box-sizing: border-box;
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
    background: linear-gradient(135deg, hsl(140 62% 52%), hsl(195 62% 52%), hsl(270 62% 58%));
    box-shadow: 0 0 8px hsl(195 62% 52% / 0.6);
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
