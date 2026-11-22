"""Keep only the gameplay part of a downloaded range.

A replay peak often lands on a cinematic trailer cut, a title card, a face-cam or a menu. This samples frames from
the downloaded range, asks the configured model which frames show real in-game play (player-controlled camera, game
HUD), and returns the longest uninterrupted gameplay stretch. A range with no such stretch is refused instead of
rendered, so reactions and trailers never become clips when "gameplay only" is on.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import tempfile
from pathlib import Path

import requests

from backend.core.failure_codes import UserFacingError
from backend.utils.ffmpeg_utils import get_ffmpeg_path

logger = logging.getLogger(__name__)

SAMPLE_EVERY_SEC = 2.5
BATCH = 12
MIN_GAMEPLAY_SEC = 10.0
MIN_GAMEPLAY_SHARE = 0.4  # of the kept stretch, so a reaction clip still has to be mostly about the game
FRAME_WIDTH = 512
REQUEST_TIMEOUT = 90
LABELS = ('gameplay', 'trailer', 'facecam', 'other')

PROMPT = (
    'You label video frames from a game video. For EACH image, in order, answer with exactly one label:\n'
    '- "gameplay": live play with a player-controlled camera or character, usually with game HUD (minimap, health, '
    'wanted stars, ammo, speedometer), driving, shooting, free roaming. Raw in-game footage, also when a small '
    'webcam overlay sits in a corner.\n'
    '- "trailer": a pre-rendered or cinematic cut: letterboxed film look, rapid scripted editing, logos, release dates, '
    'title cards, cutscenes, "official trailer" footage.\n'
    '- "facecam": a person on camera filling most of the frame (streamer, reactor, presenter, studio) with no game '
    'footage as the main picture.\n'
    '- "other": menus, maps, slides, text, news graphics, black frames, thumbnails, anything else.\n'
    'Answer only JSON: {"labels": ["gameplay", ...]} with exactly one label per image.'
)


def _endpoint() -> dict:
    from backend.services import ai_model_settings as ai
    settings = ai.load()
    if settings is None or settings.analysis is None:
        raise UserFacingError('Choose an AI model in Settings → Providers and models first.')
    return ai.chat_endpoint(ai.connection_for(settings, settings.analysis), settings.analysis.model)


def sample_frames(video: Path, duration: float, folder: Path) -> list[tuple[float, bytes]]:
    frames = []
    t = SAMPLE_EVERY_SEC / 2
    index = 0
    while t < duration:
        out = folder / f'{index}.jpg'
        subprocess.run([get_ffmpeg_path(), '-v', 'error', '-ss', f'{t:.2f}', '-i', str(video), '-frames:v', '1',
                        '-vf', f'scale={FRAME_WIDTH}:-2', '-y', str(out)], capture_output=True, timeout=30)
        if out.is_file() and out.stat().st_size:
            frames.append((t, out.read_bytes()))
        t += SAMPLE_EVERY_SEC
        index += 1
    return frames


def classify(frames: list[bytes], http=requests) -> list[str]:
    """One label per frame. Anything the model leaves out or garbles counts as "other"."""
    endpoint = _endpoint()
    labels: list[str] = []
    for start in range(0, len(frames), BATCH):
        chunk = frames[start:start + BATCH]
        content = [{'type': 'text', 'text': f'{len(chunk)} images follow.'}]
        content += [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(f).decode()}} for f in chunk]
        resp = http.post(endpoint['base_url'].rstrip('/') + '/chat/completions',
                         headers={'Authorization': f"Bearer {endpoint['api_key']}", 'Content-Type': 'application/json'},
                         json={'model': endpoint['model'], 'temperature': 0, 'max_tokens': 400,
                               'messages': [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': content}]},
                         timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise UserFacingError('The AI model could not look at the frames. Check the model in Settings supports images.')
        text = resp.json()['choices'][0]['message']['content'] or ''
        got: list = []
        try:
            raw = text.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
            got = list(json.loads(raw).get('labels') or [])
        except (ValueError, AttributeError):
            logger.warning('Gameplay labels were not valid JSON')
        got = [str(x).lower() if str(x).lower() in LABELS else 'other' for x in got][:len(chunk)]
        labels += got + ['other'] * (len(chunk) - len(got))
    return labels


def longest_gameplay(times: list[float], labels: list[str], duration: float) -> tuple[float, float] | None:
    """The best stretch of gameplay with reaction face-cam cut-ins allowed.

    A stretch is a run of gameplay / face-cam frames (one stray other frame does not split it), padded by half a sample.
    It needs real gameplay in it: at least two gameplay frames and ``MIN_GAMEPLAY_SHARE`` of the stretch. The stretch
    with the most gameplay wins. Trailers, menus and slides end a stretch.
    """
    keep = [label in ('gameplay', 'facecam') for label in labels]
    for i in range(1, len(keep) - 1):
        if not keep[i] and keep[i - 1] and keep[i + 1]:
            keep[i] = True
    best, best_key, run_start = None, None, None
    for i, on in enumerate(keep + [False]):
        if on and run_start is None:
            run_start = i
        if not on and run_start is not None:
            frames = labels[run_start:i]
            play = sum(1 for label in frames if label == 'gameplay')
            span = (max(0.0, times[run_start] - SAMPLE_EVERY_SEC / 2), min(duration, times[i - 1] + SAMPLE_EVERY_SEC / 2))
            if (play >= 2 and play / len(frames) >= MIN_GAMEPLAY_SHARE and span[1] - span[0] >= MIN_GAMEPLAY_SEC
                    and (best_key is None or (play, span[1] - span[0]) > best_key)):
                best, best_key = span, (play, span[1] - span[0])
            run_start = None
    return (round(best[0], 2), round(best[1], 2)) if best else None


def find_span(video: Path, duration: float, http=requests) -> tuple[float, float]:
    """(start, end) of the gameplay inside ``video``, or a refusal when there is none."""
    with tempfile.TemporaryDirectory() as folder:
        sampled = sample_frames(video, duration, Path(folder))
    if not sampled:
        raise UserFacingError('Could not read frames from this part of the video.')
    labels = classify([frame for _, frame in sampled], http)
    span = longest_gameplay([t for t, _ in sampled], labels, duration)
    if span is None:
        raise UserFacingError('No gameplay found in this part (it shows a trailer, menus, or only a face-cam). Skipped.')
    return span
