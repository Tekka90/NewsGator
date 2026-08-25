/** Shared API types — mirror backend schemas (backend/src/app/api/schemas.py). */

export interface User {
  id: number;
  username: string;
  is_admin: boolean;
  summary_language: string;
}

export interface Feed {
  id: number;
  url: string;
  title: string;
  is_enabled: boolean;
  poll_interval_min: number;
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
  published_at: string | null;
  last_updated_at: string;
  is_read: boolean;
  updated_since_read: boolean;
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
