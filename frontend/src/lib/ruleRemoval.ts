import { writable } from 'svelte/store';
import { api } from './api';

export interface RemovalAsk {
  count: number;
  resolve: (keepHidden: boolean | null) => void;
}

/** Set while the "bring back the hidden stories?" prompt is open (rendered by the layout). */
export const removalAsk = writable<RemovalAsk | null>(null);

/**
 * Remove a topic/group rule. A mute rule that currently hides stories asks whether to bring
 * them back; cancelling the prompt keeps the rule.
 */
export async function removeRuleAsking(ruleId: number): Promise<boolean> {
  let count = 0;
  try {
    const rule = (await api.topics.rules()).find((r) => r.id === ruleId);
    if (rule?.mode === 'mute') count = rule.hidden_count ?? 0;
  } catch {
    // unknown count: plain removal
  }
  if (count <= 0) {
    await api.topics.removeRule(ruleId);
    return true;
  }
  const keep = await new Promise<boolean | null>((resolve) => removalAsk.set({ count, resolve }));
  removalAsk.set(null);
  if (keep === null) return false;
  await api.topics.removeRule(ruleId, keep);
  return true;
}
