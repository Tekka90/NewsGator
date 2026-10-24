/**
 * Web UI localization (interface chrome only — story content has its own
 * `summary_language` pipeline).
 *
 * Messages live in `messages/<locale>/<area>.json` (flat `{ "key": "text" }`, one file per
 * page/component area). A message id is `<area>.<key>`, e.g. `stories.markRead`.
 *   - `{name}` placeholders are interpolated from the params object.
 *   - A plural message is an object `{ "one": "…", "other": "…" }` selected with
 *     `Intl.PluralRules` from `params.count` (CJK locales only need `other`).
 * English is always loaded and is the fallback for any missing key.
 *
 * Usage in Svelte: `{$t('stories.markRead')}`, `{$t('stories.count', { count: n })}`;
 * dates/numbers: `{$fmt.dateTime(iso)}` so they follow the interface language.
 */
import { derived, get, writable } from 'svelte/store';

export const LOCALES = [
  { code: 'en', name: 'English' },
  { code: 'fr', name: 'Français' },
  { code: 'de', name: 'Deutsch' },
  { code: 'es', name: 'Español' },
  { code: 'it', name: 'Italiano' },
  { code: 'pt', name: 'Português' },
  { code: 'ja', name: '日本語' },
  { code: 'ko', name: '한국어' },
  { code: 'zh-Hans', name: '简体中文' },
  { code: 'zh-Hant', name: '繁體中文' }
] as const;

export type Locale = (typeof LOCALES)[number]['code'];
export const DEFAULT_LOCALE: Locale = 'en';
const STORAGE_KEY = 'ng_ui_locale';

type Message = string | Record<string, string>;
type Catalog = Record<string, Message>;
type Params = Record<string, string | number>;

const files = import.meta.glob<Record<string, Message>>('./messages/*/*.json', {
  import: 'default'
});
const eagerEn = import.meta.glob<Record<string, Message>>('./messages/en/*.json', {
  eager: true,
  import: 'default'
});

function area(path: string): string {
  return path.split('/').pop()!.replace('.json', '');
}

function flatten(entries: [string, Record<string, Message>][]): Catalog {
  const out: Catalog = {};
  for (const [path, msgs] of entries) {
    const a = area(path);
    for (const [k, v] of Object.entries(msgs)) out[`${a}.${k}`] = v;
  }
  return out;
}

const catalogs = new Map<Locale, Catalog>([
  [DEFAULT_LOCALE, flatten(Object.entries(eagerEn))]
]);
const catalogVersion = writable(0);
export const locale = writable<Locale>(DEFAULT_LOCALE);

async function ensureLoaded(l: Locale): Promise<void> {
  if (catalogs.has(l)) return;
  const entries = await Promise.all(
    Object.entries(files)
      .filter(([p]) => p.startsWith(`./messages/${l}/`))
      .map(async ([p, load]) => [p, await load()] as [string, Record<string, Message>])
  );
  catalogs.set(l, flatten(entries));
}

export function isLocale(v: string | null | undefined): v is Locale {
  return LOCALES.some((l) => l.code === v);
}

/** Map a BCP-47 tag (`fr-CA`, `zh-TW`, `pt-BR`) to a supported locale, or null. */
export function matchLocale(tag: string): Locale | null {
  const t = tag.toLowerCase();
  if (t.startsWith('zh')) {
    return /hant|tw|hk|mo/.test(t) ? 'zh-Hant' : 'zh-Hans';
  }
  const base = t.split('-')[0];
  return LOCALES.find((l) => l.code.toLowerCase() === base)?.code ?? null;
}

function browserLocale(): Locale | null {
  if (typeof navigator === 'undefined') return null;
  for (const tag of navigator.languages ?? [navigator.language]) {
    const m = matchLocale(tag);
    if (m) return m;
  }
  return null;
}

function storedLocale(): Locale | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return isLocale(v) ? v : null;
  } catch {
    return null;
  }
}

/** Switch the interface language. `''` / null = follow the browser. */
export async function setLocale(pref: string | null | undefined): Promise<void> {
  const target = isLocale(pref) ? pref : (browserLocale() ?? DEFAULT_LOCALE);
  await ensureLoaded(target);
  try {
    if (isLocale(pref)) localStorage.setItem(STORAGE_KEY, pref);
    else localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* private mode: preference just won't persist locally */
  }
  locale.set(target);
  catalogVersion.update((n) => n + 1);
  if (typeof document !== 'undefined') document.documentElement.lang = target;
}

/** Call once on the client before first render (login page included). */
export async function initLocale(): Promise<void> {
  await setLocale(storedLocale());
}

function interpolate(text: string, params?: Params): string {
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m));
}

function lookup(l: Locale, key: string): Message | undefined {
  return catalogs.get(l)?.[key] ?? catalogs.get(DEFAULT_LOCALE)?.[key];
}

export function translate(l: Locale, key: string, params?: Params): string {
  const msg = lookup(l, key);
  if (msg === undefined) return key;
  if (typeof msg === 'string') return interpolate(msg, params);
  const count = Number(params?.count ?? 0);
  const form = new Intl.PluralRules(l).select(count);
  return interpolate(msg[form] ?? msg.other ?? key, params);
}

export const t = derived([locale, catalogVersion], ([$l]) => {
  return (key: string, params?: Params) => translate($l, key, params);
});

export const fmt = derived(locale, ($l) => ({
  dateTime: (d: string | number | Date, o?: Intl.DateTimeFormatOptions) =>
    new Date(d).toLocaleString($l, o),
  date: (d: string | number | Date, o?: Intl.DateTimeFormatOptions) =>
    new Date(d).toLocaleDateString($l, o),
  time: (d: string | number | Date, o?: Intl.DateTimeFormatOptions) =>
    new Date(d).toLocaleTimeString($l, o),
  number: (n: number, o?: Intl.NumberFormatOptions) => n.toLocaleString($l, o),
  regionName: (code: string) =>
    new Intl.DisplayNames([$l], { type: 'region' }).of(code) ?? code,
  languageName: (code: string) =>
    new Intl.DisplayNames([$l], { type: 'language' }).of(code) ?? code
}));

/** Non-reactive helper for non-component code (error strings, etc.). */
export function tNow(key: string, params?: Params): string {
  return translate(get(locale), key, params);
}
