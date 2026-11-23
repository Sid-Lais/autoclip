"""Streamer webcam overlays: find the face-cam in a game clip and lay out the clip with the streamer on top.

A webcam overlay is a face that stays put in one corner for the whole clip. In-game characters move, so a face is only
an overlay when it shows up in most sampled frames at nearly the same place. The finished 9:16 picture has the streamer
in the top half and the whole game picture, on a soft blurred backdrop, in the bottom half.
"""
from __future__ import annotations

import logging
import os
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from backend.utils.ffmpeg_utils import get_ffmpeg_path

logger = logging.getLogger(__name__)

SAMPLES = 9
DETECT_WIDTH = 960
MIN_FACE = 0.03       # face width / frame width; smaller is noise
MAX_FACE = 0.30       # larger is a full-screen talking head, not an overlay
MIN_SEEN = 0.55       # share of sampled frames the face must appear in
MAX_DRIFT = 0.05      # how far (of the frame) the face may wander and still be one overlay
CAM_WIDTH = 3.4       # webcam crop width in face widths
OUT_W, OUT_H = 1080, 1920
HALF = OUT_H // 2
MARKER = 'stacked.txt'


def _frames(video: Path, start: float, end: float, folder: Path) -> list[Path]:
    paths = []
    span = max(0.1, end - start)
    for i in range(SAMPLES):
        at = start + span * (i + 0.5) / SAMPLES
        out = folder / f'{i}.jpg'
        subprocess.run([get_ffmpeg_path(), '-v', 'error', '-ss', f'{at:.2f}', '-i', str(video), '-frames:v', '1',
                        '-vf', f'scale={DETECT_WIDTH}:-2', '-y', str(out)], capture_output=True, timeout=30)
        if out.is_file() and out.stat().st_size:
            paths.append(out)
    return paths


def overlay_region(faces_per_frame: list[list[tuple[float, float, float, float]]], frame_w: int, frame_h: int) -> Optional[dict]:
    """The webcam crop (pixels of the source) from the faces seen in each sampled frame, or None without a steady overlay.

    Each face is (x, y, w, h) in pixels. The overlay is the face position that recurs in most frames.
    """
    total = len(faces_per_frame)
    if total < 3:
        return None
    best: list[tuple[float, float, float, float]] = []
    for frame in faces_per_frame:
        for seed in frame:
            sx, sy = (seed[0] + seed[2] / 2) / frame_w, (seed[1] + seed[3] / 2) / frame_h
            matches = []
            for other_frame in faces_per_frame:
                near = [f for f in other_frame
                        if abs((f[0] + f[2] / 2) / frame_w - sx) <= MAX_DRIFT and abs((f[1] + f[3] / 2) / frame_h - sy) <= MAX_DRIFT]
                if near:
                    matches.append(max(near, key=lambda f: f[2]))
            if len(matches) > len(best):
                best = matches
    if len(best) / total < MIN_SEEN:
        return None
    x = statistics.median(f[0] for f in best)
    y = statistics.median(f[1] for f in best)
    w = statistics.median(f[2] for f in best)
    h = statistics.median(f[3] for f in best)
    if not MIN_FACE <= w / frame_w <= MAX_FACE:
        return None
    cx, cy = (x + w / 2) / frame_w, (y + h / 2) / frame_h
    if 0.35 < cx < 0.65:
        return None  # a face in the middle of the picture is part of the game, not a webcam at the side or in a corner
    crop_w = min(frame_w, w * CAM_WIDTH)
    crop_h = min(frame_h, crop_w * HALF / OUT_W)
    crop_w = min(crop_w, crop_h * OUT_W / HALF)  # keep the slot's shape when the height was clamped
    left = min(max(0.0, x + w / 2 - crop_w / 2), frame_w - crop_w)
    top = min(max(0.0, y + h / 2 - crop_h / 2), frame_h - crop_h)
    return {'x': left / frame_w, 'y': top / frame_h, 'w': crop_w / frame_w, 'h': crop_h / frame_h}


def find_overlay(video: Path, start: float, end: float) -> Optional[dict]:
    """The webcam region as fractions of the source frame, or None when the clip has no steady face-cam."""
    from backend.services.studio import framing
    cv2 = framing.load_cv2()
    if cv2 is None or not framing.MODEL.is_file():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        seen, size = [], None
        for path in _frames(video, start, end, Path(tmp)):
            image = cv2.imread(str(path))
            if image is None:
                continue
            height, width = image.shape[:2]
            size = (width, height)
            with framing._detector_lock:
                _, rows = framing._detector(cv2, width, height, framing.TRACK_SCORE).detect(image)
                rows = rows.copy() if rows is not None else []
            seen.append([(float(r[0]), float(r[1]), float(r[2]), float(r[3])) for r in rows
                         if MIN_FACE * width <= float(r[2]) <= MAX_FACE * width])
    if size is None:
        return None
    region = overlay_region(seen, size[0], size[1])
    return region


def stack_graph(region: dict, src_w: int, src_h: int) -> str:
    """Filter graph: streamer crop on top, the whole game picture on a blurred backdrop below."""
    from backend.services.publish_export import blurred_backdrop
    cw, ch = max(2, round(region['w'] * src_w) // 2 * 2), max(2, round(region['h'] * src_h) // 2 * 2)
    cx, cy = round(region['x'] * src_w), round(region['y'] * src_h)
    return (f"[0:v]split=3[a][b][c];"
            f"[a]crop={cw}:{ch}:{cx}:{cy},scale={OUT_W}:{HALF}:force_original_aspect_ratio=increase,crop={OUT_W}:{HALF},setsar=1[top];"
            f"[b]{blurred_backdrop(OUT_W, HALF)}[bg];"
            f"[c]scale={OUT_W}:-2,setsar=1[game];"
            f"[bg][game]overlay=(W-w)/2:(H-h)/2[bottom];"
            f"[top][bottom]vstack=inputs=2[out]")


def stack_if_overlay(video: Path, start: float, end: float) -> bool:
    """Replace ``video`` with the stacked 9:16 version when it has a webcam overlay. True when it did.

    The original stays next to it as ``input.original.mp4``. A clip is only stacked once.
    """
    marker = video.parent / MARKER
    if marker.exists():
        return False
    try:
        region = find_overlay(video, start, end)
        if region is None:
            return False
        from backend.services.studio import intelligence
        probe = intelligence._probe(video)
        src_w, src_h = int(probe.get('width') or 0), int(probe.get('height') or 0)
        if src_w <= 0 or src_h <= 0 or src_h > src_w:
            return False  # already vertical
        out = video.with_name(video.stem + '.stack.mp4')
        cmd = [get_ffmpeg_path(), '-v', 'error', '-y', '-i', str(video), '-filter_complex', stack_graph(region, src_w, src_h),
               '-map', '[out]', '-map', '0:a?', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19', '-pix_fmt', 'yuv420p',
               '-c:a', 'copy', '-movflags', '+faststart', str(out)]
        done = subprocess.run(cmd, capture_output=True, timeout=1800)
        if done.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            logger.warning('Stacked layout failed: %s', done.stderr[-300:].decode('utf-8', 'replace'))
            out.unlink(missing_ok=True)
            return False
        original = video.with_name(video.stem + '.original' + video.suffix)
        os.replace(video, original)
        os.replace(out, video)
        marker.write_text(f'{region}\n', encoding='utf-8')
        return True
    except Exception as error:  # noqa: BLE001 - the plain layout is always an acceptable result
        logger.warning('Webcam detection failed: %s', type(error).__name__)
        return False
