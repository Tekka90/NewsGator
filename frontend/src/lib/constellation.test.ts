// @ts-nocheck — runs under node:test (no @types/node in this project)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { declutterLabels, facingAngles, hslToRgb, hueFromPosition, topNFlags, HUE_MAX, HUE_MIN } from './constellation.ts';

test('hue follows azimuth and stays in the green→violet range', () => {
  const back = hueFromPosition([0, 0, -1]);
  const front = hueFromPosition([0, 0, 1]);
  assert.ok(Math.abs(front - (HUE_MIN + HUE_MAX) / 2) < 1e-9);
  assert.ok(back >= HUE_MIN && back <= HUE_MAX);
  assert.ok(Math.abs(hueFromPosition([0.5, 0.9, 1]) - hueFromPosition([0.51, -0.9, 1])) < 2);
  assert.ok(hueFromPosition([1, 0, 0]) > front);
});

test('hsl conversion', () => {
  assert.deepEqual(hslToRgb(0, 1, 0.5).map(Math.round), [255, 0, 0]);
  assert.deepEqual(hslToRgb(120, 1, 0.5).map(Math.round), [0, 255, 0]);
});

test('topNFlags keeps the biggest scores, ties by order', () => {
  assert.deepEqual(topNFlags([0.1, 0.9, 0.5, 0.5], 2), [false, true, true, false]);
  assert.deepEqual(topNFlags([1, 2], 5), [true, true]);
  assert.deepEqual(topNFlags([1, 2], 0), [false, false]);
});

test('declutter drops overlapping smaller labels, forced always shown', () => {
  const box = (id, score, x, force = false) => ({ id, score, x, y: 0, w: 50, h: 10, force });
  const shown = declutterLabels([box(1, 0.2, 0), box(2, 0.9, 30), box(3, 0.5, 200)]);
  assert.deepEqual([...shown].sort(), [2, 3]);
  const forced = declutterLabels([box(1, 0.2, 0, true), box(2, 0.9, 30)]);
  assert.deepEqual([...forced].sort(), [1]);
  assert.deepEqual([...declutterLabels([box(1, 0.9, 0, true), box(2, 0.1, 10, true)])].sort(), [1, 2]);
});

test('facingAngles centres the point on the view axis', () => {
  const pos = [0.6, 0.4, -0.5];
  const { yaw, pitch } = facingAngles(pos, 10);
  const [x, y, z] = pos;
  const x1 = x * Math.cos(yaw) + z * Math.sin(yaw);
  const z1 = -x * Math.sin(yaw) + z * Math.cos(yaw);
  const y2 = y * Math.cos(pitch) - z1 * Math.sin(pitch);
  assert.ok(Math.abs(x1) < 1e-9 && Math.abs(y2) < 1e-9 && z1 > 0);
  assert.ok(Math.abs(yaw - 10) <= Math.PI);
});
