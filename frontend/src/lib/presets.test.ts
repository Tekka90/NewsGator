// @ts-nocheck — runs under node:test (no @types/node in this project); excluded from svelte-check this way
// Run with `npm test` (node:test + native TypeScript stripping — no extra dependency).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { FALLBACK_ICON, ICON_GLYPHS, ICON_KEYS, resolveIcon } from './presetIcons.ts';
import {
  adjustSavedCounts,
  canDisable,
  defaultPresets,
  duplicatePreset,
  movePreset,
  newCustomPreset,
  normalizeForSave,
  pickActivePreset,
  removePreset,
  setEnabled,
  storiesQuery,
  validateDraft
} from './presets.ts';

const vectors = JSON.parse(
  readFileSync(new URL('../../../backend/tests/fixtures/story_presets_vectors.json', import.meta.url), 'utf8')
);

test('icon keys equal the shared catalog, in order', () => {
  assert.deepEqual(ICON_KEYS, vectors.icon_catalog);
  assert.equal(ICON_KEYS.length, 47);
});

test('every catalog key resolves to its own glyph', () => {
  for (const key of vectors.icon_catalog) {
    const r = resolveIcon(key);
    assert.equal(r.type, 'svg');
    assert.equal(r.type === 'svg' && r.key, key);
    assert.ok(ICON_GLYPHS[key].startsWith('<'));
  }
});

test('icon validity vectors: emoji kept, unknown falls back to filter', () => {
  for (const { icon, valid } of vectors.icon_validity as { icon: string; valid: boolean }[]) {
    const r = resolveIcon(icon);
    if (valid && !vectors.icon_catalog.includes(icon)) assert.equal(r.type, 'emoji', icon);
    if (!valid) assert.deepEqual([r.type, r.type === 'svg' && r.key], ['svg', FALLBACK_ICON], icon);
  }
  assert.equal(resolveIcon('toString').type, 'svg');
});

test('client defaults match the shared default vectors', () => {
  assert.deepEqual(defaultPresets(), vectors.defaults);
});

test('active preset: last used, legacy filter, then first enabled', () => {
  const list = defaultPresets();
  assert.equal(pickActivePreset(list, 'saved')?.id, 'saved');
  assert.equal(pickActivePreset(list, null)?.id, 'unread');
  assert.equal(pickActivePreset(list, null, 'updated')?.id, 'updated');
  const off = setEnabled(setEnabled(list, 'unread', false), 'saved', false);
  assert.equal(pickActivePreset(off, 'saved')?.id, 'all');
});

test('last enabled preset cannot be disabled or deleted', () => {
  let list = defaultPresets().map((p) => ({ ...p, enabled: p.id === 'all' }));
  assert.equal(canDisable(list, 'all'), false);
  assert.equal(setEnabled(list, 'all', false), list);
  const custom = { ...newCustomPreset(4, 'c1'), enabled: true };
  list = [...list, custom];
  assert.equal(removePreset(list, 'all'), list);
  assert.equal(removePreset(list, 'c1').length, 4);
});

test('move and duplicate keep positions dense', () => {
  const moved = movePreset(defaultPresets(), 3, 0);
  assert.deepEqual(moved.map((p) => p.id), ['saved', 'all', 'unread', 'updated']);
  assert.deepEqual(moved.map((p) => p.position), [0, 1, 2, 3]);
  const dup = duplicatePreset(defaultPresets(), 'unread', 'copy1');
  assert.deepEqual(dup.map((p) => p.id), ['all', 'unread', 'copy1', 'updated', 'saved']);
  assert.equal(dup[2].kind, 'custom');
  assert.equal(dup[2].label, 'Unread copy');
});

test('optimistic saved count only touches status-only saved presets', () => {
  const custom = newCustomPreset(4, 'c1');
  custom.criteria.status = 'saved';
  custom.criteria.category_ids = ['Tech'];
  const counts = { all: 5, saved: 2, c1: 1 };
  assert.deepEqual(adjustSavedCounts([...defaultPresets(), custom], counts, 1), { all: 5, saved: 3, c1: 1 });
  assert.equal(adjustSavedCounts(defaultPresets(), { saved: 0 }, -1).saved, 0);
});

test('stories query sends overrides only when set', () => {
  assert.equal(storiesQuery({ preset: 'unread' }).toString(), 'preset=unread');
  assert.equal(
    storiesQuery({ preset: 'x', sort: 'saved_at', order: 'asc', category: 'Tech', feedId: 3 }).toString(),
    'preset=x&sort=saved_at&order=asc&category=Tech&feed=3'
  );
});

test('draft validation and normalisation', () => {
  const bad = newCustomPreset(4, 'c1');
  bad.label = '   ';
  assert.match(validateDraft([...defaultPresets(), bad]) ?? '', /name/);
  const ok = newCustomPreset(4, 'c2');
  ok.criteria.query = '  garage ';
  ok.criteria.max_age_days = 0;
  assert.match(validateDraft([...defaultPresets(), ok]) ?? '', /age/);
  ok.criteria.max_age_days = 30;
  assert.equal(validateDraft([...defaultPresets(), ok]), null);
  assert.equal(normalizeForSave([...defaultPresets(), ok])[4].criteria.query, 'garage');
  ok.criteria.query = '  ';
  assert.equal(normalizeForSave([ok])[0].criteria.query, null);
});
