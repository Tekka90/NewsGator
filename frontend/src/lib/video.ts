import type { VideoProvider } from './types';

export interface VideoSource {
  provider: VideoProvider | null;
  videoId: string | null;
  url: string | null;
  embedUrl: string | null;
}

export type PlayerPlan =
  | { mode: 'youtube'; id: string }
  | { mode: 'file'; src: string; hls: boolean }
  | { mode: 'iframe'; src: string }
  | { mode: 'link'; href: string }
  | { mode: 'none' };

const YOUTUBE_ID = /^[\w-]{6,20}$/;
const HLS_PATH = /\.m3u8(?:$|[?#])/i;

/** Only absolute http(s) URLs may reach an iframe/video/link; everything else is dropped. */
export function safeHttpUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw);
    return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : null;
  } catch {
    return null;
  }
}

export function planPlayer(v: VideoSource): PlayerPlan {
  const url = safeHttpUrl(v.url);
  const embed = safeHttpUrl(v.embedUrl);
  if ((v.provider === 'youtube' || (!v.provider && v.videoId)) && v.videoId && YOUTUBE_ID.test(v.videoId)) {
    return { mode: 'youtube', id: v.videoId };
  }
  if (v.provider === 'file' && url) return { mode: 'file', src: url, hls: HLS_PATH.test(url) };
  if (embed) return { mode: 'iframe', src: embed };
  if (url) return { mode: 'link', href: url };
  return { mode: 'none' };
}

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return '';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

export function providerLabel(provider: VideoProvider | null | undefined): string {
  return provider === 'youtube' ? 'YouTube' : 'Video';
}
