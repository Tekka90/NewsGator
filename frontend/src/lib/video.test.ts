// @ts-nocheck — runs under node:test (no @types/node in this project)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { formatDuration, planPlayer, providerLabel, safeHttpUrl } from './video.ts';

const base = { provider: null, videoId: null, url: null, embedUrl: null } as const;

test('youtube id wins and is validated', () => {
  assert.deepEqual(planPlayer({ ...base, provider: 'youtube', videoId: 'dQw4w9WgXcQ' }), {
    mode: 'youtube',
    id: 'dQw4w9WgXcQ'
  });
  assert.equal(planPlayer({ ...base, provider: 'youtube', videoId: '<bad>' }).mode, 'none');
});

test('legacy payload with only video_id still plays as youtube', () => {
  assert.equal(planPlayer({ ...base, videoId: 'dQw4w9WgXcQ' }).mode, 'youtube');
});

test('direct files use the video element, hls is flagged', () => {
  assert.deepEqual(planPlayer({ ...base, provider: 'file', url: 'https://a.example/x.mp4' }), {
    mode: 'file',
    src: 'https://a.example/x.mp4',
    hls: false
  });
  const hls = planPlayer({ ...base, provider: 'file', url: 'https://a.example/m.m3u8?x=1' });
  assert.equal(hls.mode === 'file' && hls.hls, true);
});

test('embed players use a sandboxed iframe, otherwise fall back to a link', () => {
  assert.equal(
    planPlayer({ ...base, provider: 'embed', url: 'https://v.example/w/1', embedUrl: 'https://v.example/e/1' }).mode,
    'iframe'
  );
  assert.deepEqual(planPlayer({ ...base, provider: 'embed', url: 'https://v.example/w/1' }), {
    mode: 'link',
    href: 'https://v.example/w/1'
  });
});

test('unsafe schemes are rejected everywhere', () => {
  assert.equal(safeHttpUrl('javascript:alert(1)'), null);
  assert.equal(safeHttpUrl('data:text/html,x'), null);
  assert.equal(safeHttpUrl('/relative'), null);
  assert.equal(planPlayer({ ...base, provider: 'file', url: 'javascript:alert(1)' }).mode, 'none');
  assert.equal(planPlayer({ ...base, provider: 'embed', embedUrl: 'data:text/html,x' }).mode, 'none');
});

test('duration and provider label', () => {
  assert.equal(formatDuration(52), '0:52');
  assert.equal(formatDuration(3723), '1:02:03');
  assert.equal(formatDuration(null), '');
  assert.equal(providerLabel('youtube'), 'YouTube');
  assert.equal(providerLabel('file'), 'Video');
  assert.equal(providerLabel(null), 'Video');
});
