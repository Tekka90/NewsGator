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
