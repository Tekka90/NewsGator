#!/usr/bin/env node
// Fails when the web UI translations are inconsistent (run: npm run i18n:check).
//   1. every locale has exactly the same area files and keys as English
//   2. {placeholders} match English; plural messages carry valid forms incl. `other`
//   3. every literal $t('area.key') / t('area.key') in the sources exists in English
//   4. no empty translations
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const msgRoot = join(root, 'src/lib/i18n/messages');
const LOCALES = ['en', 'fr', 'de', 'es', 'it', 'pt', 'ja', 'ko', 'zh-Hans', 'zh-Hant'];
const bad = [];

const load = (loc) => {
  const dir = join(msgRoot, loc);
  const out = {};
  for (const f of readdirSync(dir).filter((f) => f.endsWith('.json'))) {
    const area = f.replace('.json', '');
    const data = JSON.parse(readFileSync(join(dir, f), 'utf8'));
    for (const [k, v] of Object.entries(data)) out[`${area}.${k}`] = v;
  }
  return out;
};

const placeholders = (m) => {
  const texts = typeof m === 'string' ? [m] : Object.values(m);
  return [...new Set(texts.flatMap((t) => [...t.matchAll(/\{(\w+)\}/g)].map((x) => x[1])))].sort();
};

const en = load('en');
for (const loc of LOCALES.filter((l) => l !== 'en')) {
  const cat = load(loc);
  for (const k of Object.keys(en)) if (!(k in cat)) bad.push(`${loc}: missing ${k}`);
  for (const k of Object.keys(cat)) if (!(k in en)) bad.push(`${loc}: extra ${k}`);
  const forms = new Set(['zero', 'one', 'two', 'few', 'many', 'other']);
  for (const [k, v] of Object.entries(cat)) {
    if (!(k in en)) continue;
    const texts = typeof v === 'string' ? [v] : Object.values(v);
    if (texts.some((t) => typeof t !== 'string' || !t.trim())) bad.push(`${loc}: empty ${k}`);
    if (typeof v !== typeof en[k]) bad.push(`${loc}: plural/string shape differs for ${k}`);
    else if (typeof v === 'object') {
      if (!('other' in v)) bad.push(`${loc}: ${k} has no "other" form`);
      for (const f of Object.keys(v)) if (!forms.has(f)) bad.push(`${loc}: ${k} bad form ${f}`);
    }
    if (placeholders(v).join() !== placeholders(en[k]).join())
      bad.push(`${loc}: placeholders differ for ${k}`);
  }
}
for (const [k, v] of Object.entries(en)) {
  if (typeof v === 'object' && !('other' in v)) bad.push(`en: ${k} has no "other" form`);
}

const walk = (dir) =>
  readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
const used = /(?:\$t|\btNow|\bt)\(\s*'([a-zA-Z0-9_.-]+)'\s*[,)]/g;
for (const f of walk(join(root, 'src')).filter((p) => /\.(svelte|ts)$/.test(p))) {
  if (f.includes('/i18n/')) continue;
  for (const m of readFileSync(f, 'utf8').matchAll(used)) {
    if (m[1].includes('.') && !(m[1] in en)) bad.push(`${f.replace(root + '/', '')}: unknown key ${m[1]}`);
  }
}

if (bad.length) {
  console.error(bad.join('\n'));
  console.error(`\n${bad.length} translation problem(s)`);
  process.exit(1);
}
console.log(`translations OK (${Object.keys(en).length} keys × ${LOCALES.length} locales)`);
