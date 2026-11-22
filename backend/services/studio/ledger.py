"""What has already been clipped from a video, so the same part is never cut twice.

The record is the projects themselves: each ranged clip stores its source link and range. A part counts as used while
its project exists and has not failed for a reason that a retry could fix. A part skipped for having no gameplay also
counts, since another try would be skipped again. Deleting a project frees its part.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from backend.models.project import Project

OVERLAP_SHARE = 0.5  # a candidate is a repeat when this much of it lies inside a part already made
SKIPPED_TEXT = 'No gameplay found'


def video_key(url: str) -> Optional[str]:
    """The video id of a YouTube or Bilibili link (same video, any link shape), else the bare link."""
    text = (url or '').strip()
    match = (re.search(r'(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})', text)
             or re.search(r'(BV[A-Za-z0-9]{10})', text))
    return match.group(1) if match else (text or None)


def _counts(project: Project, read_state) -> bool:
    if str(getattr(project.status, 'value', project.status)).lower() != 'failed':
        return True
    try:
        error = str(((read_state(str(project.id)) or {}).get('analysis') or {}).get('error') or '')
    except Exception:  # noqa: BLE001 - no readable state: treat as a failure that may be retried
        return False
    return SKIPPED_TEXT in error


def used_parts(db: Session, url: str, read_state) -> tuple[list[tuple[float, float]], bool]:
    """(ranges already clipped from this video, whether the whole video was already clipped)."""
    key = video_key(url)
    ranges: list[tuple[float, float]] = []
    whole = False
    for project in db.query(Project).all():
        meta = project.project_metadata or {}
        if not key or video_key(str(meta.get('source_url') or '')) != key:
            continue
        smart = (project.processing_config or {}).get('smart_import') or {}
        if not _counts(project, read_state):
            continue
        part = smart.get('source_range')
        if part:
            ranges.append((float(part['start']), float(part['end'])))
        else:
            whole = True
    return ranges, whole


def is_repeat(start: float, end: float, used: Iterable[tuple[float, float]]) -> bool:
    length = max(end - start, 1e-6)
    return any(max(0.0, min(end, b) - max(start, a)) / length >= OVERLAP_SHARE for a, b in used)
