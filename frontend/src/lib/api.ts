/** Typed API client — all backend calls go through here. */

import type { AuthUser, Category, Feed, StoryDetail, StoryListItem, User } from './types';

const BASE = '/api';
const TOKEN_KEY = 'newsgator_token';

// Portable session token: iOS home-screen PWAs do not reliably persist cookies
// across app restarts, so login/setup also return a token we keep in
// localStorage (which does persist) and send as a Bearer header.
export function getToken(): string {
  return typeof localStorage === 'undefined' ? '' : (localStorage.getItem(TOKEN_KEY) ?? '');
}

function setToken(token: string) {
  if (typeof localStorage === 'undefined') return;
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

async function req<T>(
  path: string,
  options: { method?: string; body?: unknown } = {}
): Promise<T> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(BASE + path, {
    method: options.method ?? 'GET',
    credentials: 'include',
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined
  });
  if (res.status === 401 && typeof window !== 'undefined' && !location.pathname.startsWith('/login')) {
    setToken('');
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
  setup: async (username: string, password: string) => {
    const u = await req<AuthUser>('/auth/setup', { method: 'POST', body: { username, password } });
    setToken(u.token);
    return u;
  },
  login: async (username: string, password: string) => {
    const u = await req<AuthUser>('/auth/login', { method: 'POST', body: { username, password } });
    setToken(u.token);
    return u;
  },
  logout: async () => {
    try {
      await req<void>('/auth/logout', { method: 'POST' });
    } finally {
      setToken('');
    }
  },
  me: () => req<User>('/auth/me'),
  patchMe: (patch: {
    summary_language?: string;
    story_sort?: 'updated' | 'published' | 'sources';
    story_order?: 'asc' | 'desc';
  }) => req<User>('/auth/me', { method: 'PATCH', body: patch }),

  feeds: {
    list: () => req<Feed[]>('/feeds'),
    create: (f: { url: string; title?: string; poll_interval_min?: number }) =>
      req<Feed>('/feeds', { method: 'POST', body: f }),
    update: (id: number, patch: Partial<Feed>) =>
      req<Feed>(`/feeds/${id}`, { method: 'PATCH', body: patch }),
    remove: (id: number) => req<void>(`/feeds/${id}`, { method: 'DELETE' }),
    refresh: (id: number) =>
      req<{ new_articles: number }>(`/feeds/${id}/refresh`, { method: 'POST' }),
    refreshAll: () =>
      req<{ feeds_polled: number; new_articles: number }>('/feeds/refresh', {
        method: 'POST'
      }),
    importOpml: async (file: File) => {
      const form = new FormData();
      form.append('file', file);
      const headers: Record<string, string> = {};
      const token = getToken();
      if (token) headers['Authorization'] = `Bearer ${token}`;
      const res = await fetch('/api/feeds/import-opml', {
        method: 'POST',
        credentials: 'include',
        headers,
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
    list: (filter: string = 'all', category?: string, sort: string = 'published', order: string = 'asc') => {
      const params = new URLSearchParams({ filter, sort, order });
      if (category) params.set('category', category);
      return req<StoryListItem[]>(`/stories?${params}`);
    },
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
      }),
    reprocessArticle: (articleId: number) =>
      req<{
        chars: number;
        path: string;
        content_status: string;
        content_warning: string | null;
        requeued: boolean;
      }>(`/stories/articles/${articleId}/reprocess`, { method: 'POST' })
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
