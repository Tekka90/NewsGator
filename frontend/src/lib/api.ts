/** Typed API client — all backend calls go through here. */

import type { Category, Feed, StoryDetail, StoryListItem, User } from './types';

const BASE = '/api';

async function req<T>(
  path: string,
  options: { method?: string; body?: unknown } = {}
): Promise<T> {
  const res = await fetch(BASE + path, {
    method: options.method ?? 'GET',
    credentials: 'include',
    headers: options.body !== undefined ? { 'Content-Type': 'application/json' } : {},
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined
  });
  if (res.status === 401 && typeof window !== 'undefined' && !location.pathname.startsWith('/login')) {
    location.href = '/login';
    throw new Error('unauthorized');
  }
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail ?? `HTTP ${res.status}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  setupNeeded: () => req<{ setup_needed: boolean }>('/auth/setup-needed'),
  setup: (username: string, password: string) =>
    req<User>('/auth/setup', { method: 'POST', body: { username, password } }),
  login: (username: string, password: string) =>
    req<User>('/auth/login', { method: 'POST', body: { username, password } }),
  logout: () => req<void>('/auth/logout', { method: 'POST' }),
  me: () => req<User>('/auth/me'),

  feeds: {
    list: () => req<Feed[]>('/feeds'),
    create: (f: { url: string; title?: string; poll_interval_min?: number }) =>
      req<Feed>('/feeds', { method: 'POST', body: f }),
    update: (id: number, patch: Partial<Feed>) =>
      req<Feed>(`/feeds/${id}`, { method: 'PATCH', body: patch }),
    remove: (id: number) => req<void>(`/feeds/${id}`, { method: 'DELETE' }),
    importOpml: async (file: File) => {
      const form = new FormData();
      form.append('file', file);
      const res = await fetch('/api/feeds/import-opml', {
        method: 'POST',
        credentials: 'include',
        body: form
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.detail ?? `HTTP ${res.status}`);
      }
      return res.json() as Promise<{
        added: number;
        skipped_existing: number;
        invalid: number;
        feeds: Feed[];
      }>;
    }
  },

  categories: {
    list: () => req<Category[]>('/categories'),
    create: (name: string) => req<Category>('/categories', { method: 'POST', body: { name } }),
    rename: (id: number, name: string) =>
      req<Category>(`/categories/${id}`, { method: 'PATCH', body: { name } }),
    remove: (id: number) => req<void>(`/categories/${id}`, { method: 'DELETE' })
  },

  stories: {
    list: (filter: string = 'all', category?: string) =>
      req<StoryListItem[]>(
        `/stories?filter=${filter}${category ? `&category=${encodeURIComponent(category)}` : ''}`
      ),
    detail: (id: number) => req<StoryDetail>(`/stories/${id}`),
    read: (id: number) => req<void>(`/stories/${id}/read`, { method: 'POST' }),
    unread: (id: number) => req<void>(`/stories/${id}/unread`, { method: 'POST' }),
    diff: (id: number, fromVersion: number) =>
      req<{ from_version: number; changes: { version: number; summary: string; at: string }[] }>(
        `/stories/${id}/diff?from=${fromVersion}`
      ),
    merge: (id: number, sourceStoryId: number) =>
      req<void>(`/stories/${id}/merge`, {
        method: 'POST',
        body: { source_story_id: sourceStoryId }
      }),
    moveArticle: (articleId: number, storyId: number) =>
      req<void>(`/stories/articles/${articleId}/move`, {
        method: 'POST',
        body: { story_id: storyId }
      })
  },

  settings: {
    get: () =>
      req<{ values: Record<string, string | number>; overridden: string[]; llm_queue_depth: number }>(
        '/settings'
      ),
    patch: (values: Record<string, string | number>) =>
      req<{ values: Record<string, string | number> }>('/settings', {
        method: 'PATCH',
        body: { values }
      }),
    testLlm: () =>
      req<{ chat: boolean; embeddings: boolean; errors: string[]; api_key_hint?: string }>(
        '/settings/test-llm',
        { method: 'POST' }
      ),
    thresholdReport: () =>
      req<{
        current: { tau_attach: number; tau_gray: number };
        labeled_pairs: number;
        decisions_logged: number;
        candidates: { tau: number; precision: number; recall: number; f1: number }[];
        suggested_tau_attach: number | null;
      }>('/settings/threshold-report')
  }
};
