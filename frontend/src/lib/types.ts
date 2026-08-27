/** Shared API types — mirror backend schemas (backend/src/app/api/schemas.py). */

export interface User {
  id: number;
  username: string;
  is_admin: boolean;
  summary_language: string;
  // '' = follow the server default (published, oldest first)
  story_sort: '' | 'updated' | 'published' | 'sources';
  story_order: '' | 'asc' | 'desc';
  // '' = follow the server default (unread)
  story_filter: '' | 'all' | 'unread' | 'updated';
}

export interface AuthUser extends User {
  token: string;
}

// --- LLM usage metrics (admin Usage page) ---

export interface UsageTotals {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  estimated_calls: number;
}

export interface UsageGroup extends UsageTotals {
  endpoint: 'chat' | 'embed';
  latency_ms: number;
  tokens_per_s: number | null;
}

export interface UsageSummary {
  period: 'day' | 'month' | 'all';
  totals: UsageTotals;
  by_kind: (UsageGroup & { kind: string })[];
  by_model: (UsageGroup & { model: string })[];
}

export interface UsageDailyRow {
  day: string;
  kind: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  latency_ms: number;
}

export interface UsageByFeed {
  feeds: {
    feed_id: number | null;
    title: string;
    url: string | null;
    calls: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    estimated_calls: number;
  }[];
}

export interface PipelineRow {
  id: number;
  title: string;
  feed_title: string;
  processing_state: string;
  fetched_at: string;
  content_status: string;
  story_id: number | null;
}

export interface Feed {
  id: number;
  url: string;
  title: string;
  is_enabled: boolean;
  poll_interval_min: number;
  backfill_days: number | null;
  last_fetched_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
  fetch_fulltext: boolean;
}

export interface Category {
  id: number;
  name: string;
}

export interface StoryListItem {
  id: number;
  title: string;
  summary: string;
  category: string;
  image_url: string | null;
  version: number;
  is_frozen: boolean;
  source_count: number;
  source_hosts: string[];
  published_at: string | null;
  last_updated_at: string;
  is_read: boolean;
  updated_since_read: boolean;
  readeck_bookmark_id: string | null;
}

export interface StoryArticle {
  id: number;
  title: string;
  url: string;
  image_url: string | null;
  language: string;
  summary: string | null;
  content_status: string;
  content_warning: string | null;
  published_at: string | null;
  feed_id: number;
  feed_title: string;
  feed_url: string;
}

export interface StoryRevision {
  version: number;
  summary: string;
  created_at: string;
}

export interface StoryDetail extends StoryListItem {
  first_seen_at: string;
  articles: StoryArticle[];
  revisions: StoryRevision[];
}
