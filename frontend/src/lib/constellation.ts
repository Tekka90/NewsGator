// Pure helpers for the Topic Radar constellation (no DOM, unit-tested).

export const HUE_MIN = 140; // green
export const HUE_MAX = 270; // violet

/** Hue (degrees) from the azimuth around the vertical axis: neighbours share colours. */
export function hueFromPosition(pos: [number, number, number]): number {
  const az = Math.atan2(pos[0], pos[2]); // -π..π
  return HUE_MIN + (HUE_MAX - HUE_MIN) * ((az + Math.PI) / (2 * Math.PI));
}

export const GROUP_SAT = 0.65;
export const GROUP_LIGHT = 0.55;
export const TINT_SPREAD = 36; // degrees of hue variation among the topics of one group

/** CSS colour of a topic group: hsl(hue, 65%, 55%). */
export const groupCss = (hue: number) => `hsl(${((hue % 360) + 360) % 360}, ${Math.round(GROUP_SAT * 100)}%, ${Math.round(GROUP_LIGHT * 100)}%)`;

/** Topic hue inside a group: the group hue, nudged by position so neighbours stay distinguishable. */
export function tintHue(groupHue: number, pos: [number, number, number], spread = TINT_SPREAD): number {
  const k = (hueFromPosition(pos) - HUE_MIN) / (HUE_MAX - HUE_MIN) - 0.5; // -0.5..0.5
  return (((groupHue + k * spread) % 360) + 360) % 360;
}

/** h in degrees, s and l in 0..1 → [r, g, b] in 0..255. */
export function hslToRgb(h: number, s: number, l: number): [number, number, number] {
  const hh = (((h % 360) + 360) % 360) / 60;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((hh % 2) - 1));
  const [r, g, b] =
    hh < 1 ? [c, x, 0] : hh < 2 ? [x, c, 0] : hh < 3 ? [0, c, x] : hh < 4 ? [0, x, c] : hh < 5 ? [x, 0, c] : [c, 0, x];
  const m = l - c / 2;
  return [(r + m) * 255, (g + m) * 255, (b + m) * 255];
}

/** Flags the `n` highest scores (ties keep list order). */
export function topNFlags(scores: number[], n: number): boolean[] {
  const order = scores.map((s, i) => i).sort((a, b) => scores[b] - scores[a] || a - b);
  const flags = scores.map(() => false);
  for (const i of order.slice(0, Math.max(0, n))) flags[i] = true;
  return flags;
}

export interface LabelBox {
  id: number;
  score: number;
  x: number;
  y: number;
  w: number;
  h: number;
  force?: boolean;
}

const overlaps = (a: LabelBox, b: LabelBox) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/** Greedy declutter: forced boxes first, then biggest score wins; overlapping labels are hidden. */
export function declutterLabels(boxes: LabelBox[]): Set<number> {
  const sorted = [...boxes].sort((a, b) => Number(!!b.force) - Number(!!a.force) || b.score - a.score);
  const placed: LabelBox[] = [];
  for (const b of sorted) {
    if (b.force || !placed.some((p) => overlaps(p, b))) placed.push(b);
  }
  return new Set(placed.map((b) => b.id));
}

/** Yaw/pitch that bring a point to face the viewer (yaw is the nearest equivalent to `currentYaw`). */
export function facingAngles(
  pos: [number, number, number],
  currentYaw: number,
  maxPitch = 1.3
): { yaw: number; pitch: number } {
  const [x, y, z] = pos;
  const turns = Math.PI * 2;
  let yaw = -Math.atan2(x, z);
  yaw += Math.round((currentYaw - yaw) / turns) * turns;
  const pitch = Math.max(-maxPitch, Math.min(maxPitch, Math.atan2(y, Math.hypot(x, z))));
  return { yaw, pitch };
}

export const distance3 = (a: [number, number, number], b: [number, number, number]) =>
  Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);
