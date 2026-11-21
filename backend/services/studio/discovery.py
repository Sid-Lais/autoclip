"""Find videos by search query and suggest which parts of them are worth clipping.

Two real signals are used, no guessing about future views:
- YouTube search results carry view counts, so candidates are ranked by how much they were watched.
- YouTube's "Most replayed" graph (`heatmap` in yt-dlp's info, 100 segments of relative replay value) marks the
  moments viewers actually rewatch. Its peaks become the suggested clip ranges. Chapters are the fallback for videos
  without the graph.
Videos with neither get no range suggestion: a short one is left to the normal transcript-scoring pipeline.
"""
from __future__ import annotations

import re
from typing import Optional

from backend.services.studio import range_import

MAX_SEARCH_RESULTS = 25
SUGGESTED_VIDEOS = 5
MAX_MOMENTS = 4
MIN_PEAK_VALUE = 0.55  # replay value (0..1, relative to the video's own most replayed moment)
CLIP_MIN_SEC = 30.0
CLIP_MAX_SEC = 90.0
MIN_VIDEO_SEC = 4 * 60  # search ignores trailers and Shorts-length uploads


def _video_url(entry: dict) -> Optional[str]:
    identifier = str(entry.get('id') or '')
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier):
        return f'https://www.youtube.com/watch?v={identifier}'
    return None


def _thumbnail(entry: dict) -> Optional[str]:
    for item in reversed(entry.get('thumbnails') or []):
        if str(item.get('url') or '').startswith('http'):
            return str(item['url'])[:500]
    return None


def search_videos(query: str, limit: int = 12, min_duration: float = MIN_VIDEO_SEC) -> list[dict]:
    """Most viewed matches first. Each result is flagged ``suggested`` for the top few."""
    from backend.utils.download_recovery import extract_info_with_recovery
    from backend.utils.ffmpeg_utils import get_ffmpeg_path
    limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    info = extract_info_with_recovery(
        f'ytsearch{min(MAX_SEARCH_RESULTS, limit * 2)}:{query}',
        {'extract_flat': True, 'skip_download': True, 'quiet': True, 'ffmpeg_location': get_ffmpeg_path(),
         'socket_timeout': 10, 'retries': 1},
    )
    seen, results = set(), []
    for entry in info.get('entries') or []:
        url = _video_url(entry or {})
        duration = entry.get('duration') if entry else None
        views = entry.get('view_count') if entry else None
        if not url or url in seen or not isinstance(duration, (int, float)) or duration < min_duration:
            continue
        if entry.get('live_status') in ('is_live', 'is_upcoming'):
            continue  # a stream still running has no fixed length to cut from
        seen.add(url)
        results.append({
            'url': url,
            'title': str(entry.get('title') or '')[:200],
            'channel': str(entry.get('channel') or entry.get('uploader') or '')[:120],
            'duration': float(duration),
            'view_count': int(views) if isinstance(views, (int, float)) else None,
            'thumbnail': _thumbnail(entry),
            'long_video': range_import.is_long(duration),
        })
    results.sort(key=lambda item: item['view_count'] or 0, reverse=True)
    results = results[:limit]
    for index, item in enumerate(results):
        item['suggested'] = index < SUGGESTED_VIDEOS and bool(item['view_count'])
    return results


def moments_from_heatmap(heatmap: list[dict], duration: float) -> list[dict]:
    """Top non-overlapping replay peaks, each as a 30-90 s window around the peak segment."""
    peaks = sorted(
        (seg for seg in heatmap or [] if isinstance(seg.get('value'), (int, float)) and seg['value'] >= MIN_PEAK_VALUE),
        key=lambda seg: seg['value'], reverse=True)
    moments: list[dict] = []
    for seg in peaks:
        start, end = float(seg['start_time']), float(seg['end_time'])
        centre = (start + end) / 2
        length = min(CLIP_MAX_SEC, max(CLIP_MIN_SEC, end - start))
        window_start = max(0.0, centre - length / 2)
        window_end = min(duration, window_start + length)
        window_start = max(0.0, window_end - length)
        if window_end - window_start < range_import.MIN_RANGE_SEC:
            continue
        if any(window_start < m['end'] and window_end > m['start'] for m in moments):
            continue
        moments.append({'start': round(window_start, 1), 'end': round(window_end, 1),
                        'score': round(float(seg['value']), 2), 'source': 'most_replayed'})
        if len(moments) >= MAX_MOMENTS:
            break
    return sorted(moments, key=lambda m: m['start'])


def moments_from_chapters(chapters: list[dict], duration: float) -> list[dict]:
    """Fallback without a replay graph: the opening stretch of the first few chapters (the part titles promise)."""
    moments = []
    for chapter in chapters or []:
        start = float(chapter.get('start_time') or 0)
        end = float(chapter.get('end_time') or duration)
        if end - start < CLIP_MIN_SEC:
            continue
        moments.append({'start': round(start, 1), 'end': round(min(end, start + CLIP_MAX_SEC), 1), 'score': None,
                        'source': 'chapter', 'label': str(chapter.get('title') or '')[:120]})
    return moments[:MAX_MOMENTS]


def suggest_clips(url: str, browser: Optional[str] = None) -> dict:
    """What can be clipped from ``url`` and how: ``ranges`` (cut these parts), ``whole`` (let the pipeline choose) or ``manual``."""
    from backend.utils.download_recovery import extract_info_with_recovery
    from backend.utils.ffmpeg_utils import get_ffmpeg_path, ytdlp_js_runtimes
    options = {'skip_download': True, 'quiet': True, 'noplaylist': True, 'ffmpeg_location': get_ffmpeg_path(),
               'socket_timeout': 10, 'retries': 1, **ytdlp_js_runtimes()}
    if browser:
        options['cookiesfrombrowser'] = (browser,)
    info = extract_info_with_recovery(url, options)
    duration = float(info.get('duration') or 0)
    moments = moments_from_heatmap(info.get('heatmap') or [], duration)
    if not moments:
        moments = moments_from_chapters(info.get('chapters') or [], duration)
    views = info.get('view_count')
    if moments:
        mode = 'ranges'
    elif range_import.is_long(duration):
        mode = 'manual'  # too long to download whole and nothing to base a cut on
    else:
        mode = 'whole'
    return {
        'url': url,
        'title': str(info.get('title') or '')[:200],
        'duration': duration,
        'view_count': int(views) if isinstance(views, (int, float)) else None,
        'mode': mode,
        'moments': moments,
        'signal': moments[0]['source'] if moments else None,
    }
