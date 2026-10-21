/** Pure story-preset logic (no DOM, no Svelte) — unit-tested with `npm test`. */
import type {
  PresetCriteria,
  PresetOrder,
  PresetSort,
  StoryPreset
} from './types';

export const BUILTIN_IDS = ['all', 'unread', 'updated', 'saved'] as const;
export const MAX_LABEL = 40;
export const MAX_QUERY = 200;
export const MAX_PRESETS = 200;

const BUILTIN_NAMES: Record<string, string> = {
  all: 'All',
  unread: 'Unread',
  updated: 'Updated',
  saved: 'Saved'
};

export const SORT_LABELS: Record<PresetSort, string> = {
  published: 'Article date',
  updated: 'Processing date',
  sources: 'Source count',
  saved_at: 'Saved date'
};

export function emptyCriteria(): PresetCriteria {
  return { status: 'any', feed_ids: [], category_ids: [], source_kinds: [], query: null, max_age_days: null };
}

function builtin(
  id: string,
  icon: string,
  status: PresetCriteria['status'],
  sort: PresetSort,
  order: PresetOrder,
  position: number
): StoryPreset {
  return {
    id,
    kind: 'builtin',
    label: null,
    icon,
    display: 'icon+label',
    enabled: true,
    position,
    sort,
    order,
    show_count: true,
    criteria: { ...emptyCriteria(), status }
  };
}

/** Defaults used when the presets call fails; identical to the server defaults. */
export function defaultPresets(): StoryPreset[] {
  return [
    builtin('all', 'list', 'any', 'published', 'desc', 0),
    builtin('unread', 'circle-dot', 'unread', 'published', 'asc', 1),
    builtin('updated', 'refresh', 'updated', 'sources', 'desc', 2),
    builtin('saved', 'bookmark', 'saved', 'saved_at', 'desc', 3)
  ];
}

/** Display name: custom label, else the built-in's default name. */
export function presetLabel(p: StoryPreset): string {
  const l = (p.label ?? '').trim();
  if (l) return l;
  return BUILTIN_NAMES[p.id] ?? 'View';
}

/** What the bar shows for a preset, given the global compact toggle. */
export function effectiveDisplay(p: StoryPreset, compact: boolean): StoryPreset['display'] {
  return compact ? 'icon' : p.display;
}

export function sortedPresets(list: StoryPreset[]): StoryPreset[] {
  return [...list].sort((a, b) => a.position - b.position);
}

export function enabledPresets(list: StoryPreset[]): StoryPreset[] {
  return sortedPresets(list).filter((p) => p.enabled);
}

/** Active preset on load: last used, else the legacy story_filter, else the first enabled. */
export function pickActivePreset(
  list: StoryPreset[],
  lastId: string | null | undefined,
  legacyFilter?: string | null
): StoryPreset | null {
  const on = enabledPresets(list);
  for (const id of [lastId, legacyFilter || 'unread']) {
    const hit = id ? on.find((p) => p.id === id) : undefined;
    if (hit) return hit;
  }
  return on[0] ?? null;
}

/** A preset made only of a status criterion (its count is predictable client-side). */
export function isStatusOnly(p: StoryPreset): boolean {
  const c = p.criteria;
  return (
    !c.feed_ids.length &&
    !c.category_ids.length &&
    !c.source_kinds.length &&
    !(c.query ?? '').trim() &&
    c.max_age_days == null
  );
}

/** Optimistic Saved/unsaved update of every status-only "saved" preset's count. */
export function adjustSavedCounts(
  presets: StoryPreset[],
  counts: Record<string, number>,
  delta: 1 | -1
): Record<string, number> {
  const next = { ...counts };
  for (const p of presets) {
    if (p.criteria.status === 'saved' && isStatusOnly(p) && next[p.id] !== undefined) {
      next[p.id] = Math.max(0, next[p.id] + delta);
    }
  }
  return next;
}

export interface ListQuery {
  preset: string;
  sort?: PresetSort;
  order?: PresetOrder;
  category?: string;
  feedId?: number;
}

/** Query string of GET /stories — overrides are sent only when the user set them. */
export function storiesQuery(q: ListQuery): URLSearchParams {
  const params = new URLSearchParams({ preset: q.preset });
  if (q.sort) params.set('sort', q.sort);
  if (q.order) params.set('order', q.order);
  if (q.category) params.set('category', q.category);
  if (q.feedId) params.set('feed', String(q.feedId));
  return params;
}

// --- editor helpers (all return new arrays; positions kept dense) ---

/** Dense positions following the array order. */
export function renumber(list: StoryPreset[]): StoryPreset[] {
  return list.map((p, i) => ({ ...p, position: i }));
}

export function movePreset(list: StoryPreset[], from: number, to: number): StoryPreset[] {
  const arr = sortedPresets(list);
  if (from < 0 || from >= arr.length) return renumber(arr);
  const target = Math.max(0, Math.min(arr.length - 1, to));
  const [item] = arr.splice(from, 1);
  arr.splice(target, 0, item);
  return renumber(arr);
}

export function canDisable(list: StoryPreset[], id: string): boolean {
  return list.some((p) => p.enabled && p.id !== id);
}

export function setEnabled(list: StoryPreset[], id: string, enabled: boolean): StoryPreset[] {
  if (!enabled && !canDisable(list, id)) return list;
  return list.map((p) => (p.id === id ? { ...p, enabled } : p));
}

export function newId(): string {
  return globalThis.crypto.randomUUID();
}

export function newCustomPreset(position: number, id = newId()): StoryPreset {
  return {
    id,
    kind: 'custom',
    label: 'New view',
    icon: 'filter',
    display: 'icon+label',
    enabled: true,
    position,
    sort: 'published',
    order: 'desc',
    show_count: true,
    criteria: emptyCriteria()
  };
}

/** Copy of any preset as a custom one placed right after the source. */
export function duplicatePreset(list: StoryPreset[], id: string, newPresetId = newId()): StoryPreset[] {
  const arr = sortedPresets(list);
  const at = arr.findIndex((p) => p.id === id);
  if (at < 0) return arr;
  const src = arr[at];
  const label = `${presetLabel(src)} copy`.slice(0, MAX_LABEL);
  const copy: StoryPreset = structuredClone({ ...src, id: newPresetId, kind: 'custom', label });
  arr.splice(at + 1, 0, copy);
  return renumber(arr);
}

export function removePreset(list: StoryPreset[], id: string): StoryPreset[] {
  const target = list.find((p) => p.id === id);
  if (!target || target.kind === 'builtin') return list;
  const rest = list.filter((p) => p.id !== id);
  if (!rest.some((p) => p.enabled)) return list;
  return renumber(sortedPresets(rest));
}

export function toggleInList<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

/** First blocking problem of a draft list (same rules as the server), or null. */
export function validateDraft(list: StoryPreset[]): string | null {
  if (!list.some((p) => p.enabled)) return 'At least one view must stay enabled.';
  if (list.length > MAX_PRESETS) return `At most ${MAX_PRESETS} views.`;
  for (const p of list) {
    const label = (p.label ?? '').trim();
    if (p.kind === 'custom' && (label.length < 1 || label.length > MAX_LABEL)) {
      return `Every custom view needs a name (1–${MAX_LABEL} characters).`;
    }
    if (p.kind === 'builtin' && label.length > MAX_LABEL) return `Names are at most ${MAX_LABEL} characters.`;
    if ((p.criteria.query ?? '').length > MAX_QUERY) return `Keywords are at most ${MAX_QUERY} characters.`;
    const age = p.criteria.max_age_days;
    if (age != null && (!Number.isInteger(age) || age < 1 || age > 3650)) {
      return 'Maximum age must be between 1 and 3650 days.';
    }
  }
  return null;
}

/** Payload for PUT: trims text, empty query/max age → null, dense positions. */
export function normalizeForSave(list: StoryPreset[]): StoryPreset[] {
  return renumber(sortedPresets(list)).map((p) => {
    const query = (p.criteria.query ?? '').trim();
    return {
      ...p,
      label: p.kind === 'builtin' && !(p.label ?? '').trim() ? null : (p.label ?? '').trim(),
      criteria: { ...p.criteria, query: query || null, max_age_days: p.criteria.max_age_days ?? null }
    };
  });
}
