"""Long-video mode: pick time ranges online, download only those ranges, one clip per range.

A source longer than ``LONG_VIDEO_SEC`` is never downloaded whole. The picker streams the video
from the platform's own embedded player, the user marks one or more ranges, and only those ranges
are fetched (``yt-dlp --download-sections``). Each range becomes its own project whose source is
the downloaded section, so the normal pipeline (captions, framing, packaging, post copy) runs on
a few minutes of video instead of hours.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

LONG_VIDEO_SEC = 90 * 60  # longer than this -> choose ranges instead of downloading the whole video
MAX_RANGES = 20
MIN_RANGE_SEC = 10
MAX_RANGE_SEC = 60 * 60

_YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}
_BILIBILI_HOSTS = {'bilibili.com', 'www.bilibili.com', 'm.bilibili.com'}
_YOUTUBE_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
_BILIBILI_ID = re.compile(r'^(BV[0-9A-Za-z]{10}|av\d+)$')


def is_long(duration: float | None) -> bool:
    return isinstance(duration, (int, float)) and duration > LONG_VIDEO_SEC


def embed_info(url: str) -> Optional[dict]:
    """The embeddable player for ``url``: {'site': 'youtube'|'bilibili', 'id': ..., 'page': ...}.

    YouTube gets full control (the IFrame API reports the playhead, seeks and pauses). Bilibili's
    embed only accepts a start offset, so the picker falls back to typed timestamps there.
    """
    parsed = urlparse((url or '').strip())
    host = (parsed.hostname or '').lower()
    query = parse_qs(parsed.query)
    parts = [part for part in parsed.path.split('/') if part]
    if host in _YOUTUBE_HOSTS:
        candidate = parts[0] if host == 'youtu.be' and parts else None
        if candidate is None and parts and parts[0] in ('live', 'shorts', 'embed', 'v') and len(parts) > 1:
            candidate = parts[1]
        if candidate is None:
            candidate = (query.get('v') or [None])[0]
        if candidate and _YOUTUBE_ID.match(candidate):
            return {'site': 'youtube', 'id': candidate}
    if host in _BILIBILI_HOSTS and len(parts) >= 2 and parts[0] == 'video' and _BILIBILI_ID.match(parts[1]):
        try:
            page = max(1, int((query.get('p') or ['1'])[0]))
        except ValueError:
            page = 1
        return {'site': 'bilibili', 'id': parts[1], 'page': page}
    return None


class SourceRange(BaseModel):
    """An absolute time range (seconds) on the original video."""
    model_config = ConfigDict(extra='forbid')
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    label: str = Field(default='', max_length=120)

    @model_validator(mode='after')
    def ordered(self):
        length = self.end - self.start
        if length < MIN_RANGE_SEC:
            raise ValueError(f'Each range must be at least {MIN_RANGE_SEC} seconds long.')
        if length > MAX_RANGE_SEC:
            raise ValueError(f'Each range can be at most {MAX_RANGE_SEC // 60} minutes long.')
        return self


def _no_overlap(ranges: list['SourceRange']) -> None:
    spans = sorted((r.start, r.end) for r in ranges)
    for (_, previous_end), (start, _) in zip(spans, spans[1:]):
        if start < previous_end:
            raise ValueError('Ranges must not overlap.')


class ClipOptions(BaseModel):
    """What every clip of one request is made like (same fields as a normal import)."""
    model_config = ConfigDict(extra='forbid')
    browser: Optional[Literal['chrome', 'edge', 'firefox', 'safari']] = None
    language: Literal['source', 'zh', 'en', 'ja'] = 'source'
    aspect: Optional[Literal['original', 'portrait', 'landscape']] = None
    duration: Optional[int] = Field(default=None, ge=10, le=120)
    instruction: str = Field(default='', max_length=1000)
    platforms: list[str] = Field(default_factory=lambda: ['douyin'], min_length=1, max_length=8)
    portrait_style: Literal['auto', 'interview', 'podcast'] = 'auto'
    html_template: Optional[Literal['editorial', 'street', 'news', 'cinema', 'classic']] = None
    recommended_template: Optional[Literal['editorial', 'street', 'classic']] = None
    brand_outro_enabled: bool = True
    features: Optional[dict] = None


class ImportRangesBody(ClipOptions):
    url: str = Field(min_length=8, max_length=2000)
    title: str = Field(default='', max_length=200)
    ranges: list[SourceRange] = Field(min_length=1, max_length=MAX_RANGES)

    @model_validator(mode='after')
    def distinct(self):
        _no_overlap(self.ranges)
        return self


MAX_AUTO_ITEMS = 10


class AutoClipItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=8, max_length=2000)
    title: str = Field(default='', max_length=200)
    # Empty: the whole video goes through the normal pipeline (only for videos short enough to download whole).
    ranges: list[SourceRange] = Field(default_factory=list, max_length=MAX_RANGES)

    @model_validator(mode='after')
    def distinct(self):
        _no_overlap(self.ranges)
        return self


class AutoClipBody(ClipOptions):
    items: list[AutoClipItem] = Field(min_length=1, max_length=MAX_AUTO_ITEMS)


def clock(seconds: float) -> str:
    total = int(round(seconds))
    return f'{total // 3600:d}:{total % 3600 // 60:02d}:{total % 60:02d}'


def project_name(title: str, source_range: SourceRange) -> str:
    base = (title or 'Clip').strip()[:120]
    return f'{base} [{clock(source_range.start)}-{clock(source_range.end)}]'


_TIMING = re.compile(
    r'(?P<h1>\d+):(?P<m1>\d{2}):(?P<s1>\d{2})[,.](?P<f1>\d{1,3})\s*-->\s*'
    r'(?P<h2>\d+):(?P<m2>\d{2}):(?P<s2>\d{2})[,.](?P<f2>\d{1,3})')


def _seconds(h: str, m: str, s: str, f: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(f.ljust(3, '0')[:3]) / 1000


def _stamp(value: float) -> str:
    millis = int(round(max(value, 0) * 1000))
    return f'{millis // 3600000:02d}:{millis % 3600000 // 60000:02d}:{millis % 60000 // 1000:02d},{millis % 1000:03d}'


def trim_srt(text: str, start: float, end: float) -> tuple[str, int]:
    """Keep the cues that overlap [start, end] and shift them so the range begins at 0.

    Platform subtitles are timed against the whole video; the downloaded section starts at 0.
    Returns (srt text, cue count).
    """
    length = end - start
    cues: list[tuple[float, float, str]] = []
    for block in re.split(r'\r?\n\s*\r?\n', text.replace('\ufeff', '').strip()):
        lines = block.strip().splitlines()
        for index, line in enumerate(lines):
            match = _TIMING.search(line)
            if not match:
                continue
            cue_start = _seconds(match['h1'], match['m1'], match['s1'], match['f1']) - start
            cue_end = _seconds(match['h2'], match['m2'], match['s2'], match['f2']) - start
            body = '\n'.join(lines[index + 1:]).strip()
            if body and cue_end > 0 and cue_start < length:
                cues.append((max(cue_start, 0.0), min(cue_end, length), body))
            break
    out = [f'{number}\n{_stamp(a)} --> {_stamp(b)}\n{body}\n' for number, (a, b, body) in enumerate(cues, 1)]
    return '\n'.join(out), len(cues)


def trim_srt_file(path: Path, start: float, end: float) -> int:
    """Rewrite ``path`` for the range; returns the cue count (0 means nothing usable)."""
    trimmed, count = trim_srt(path.read_text(encoding='utf-8', errors='ignore'), start, end)
    if count:
        path.write_text(trimmed, encoding='utf-8')
    return count


# A range is cut with `ffmpeg -ss -t` on the stream URL. Over HTTPS (DASH mp4) ffmpeg seeks with range requests and
# reads only the range. A HLS "DVR" playlist (what past live streams are served as) cannot be seeked: ffmpeg reads
# every segment before the start, so a clip an hour in would download the hour. HLS stays as the last fallback.
RANGE_FORMAT = (
    'bestvideo[height<=1080][protocol=https][vcodec^=avc1]+bestaudio[protocol=https][ext=m4a]'
    '/bestvideo[height<=1080][protocol=https]+bestaudio[protocol=https]'
    '/best[height<=1080][protocol=https]'
    '/bestvideo[height<=1080]+bestaudio/best[height<=1080]'
)


def download_options(source_range) -> dict:
    """yt-dlp options that fetch only the range (cut on the exact frame, not the nearest keyframe)."""
    from yt_dlp.utils import download_range_func
    return {
        'format': RANGE_FORMAT,
        'download_ranges': download_range_func(None, [(float(source_range.start), float(source_range.end))]),
        'force_keyframes_at_cuts': True,
    }
