"""Upload finished clips straight to your own YouTube channel (YouTube Data API v3, OAuth 2.0).

No third-party service: you create a free Google Cloud project, enable "YouTube Data API v3", make a *Web application*
OAuth client and give its client id / secret to AutoClip. "Connect YouTube" then runs Google's consent screen once and
stores the refresh token in the data directory (``youtube.json``, mode 0600).

Uploads are private by default. A video is only uploaded once per output variant (the record is kept in the project).
A project whose API client has not passed Google's audit may be forced to private by YouTube regardless of this setting.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
UPLOAD_URL = 'https://www.googleapis.com/upload/youtube/v3/videos'
API = 'https://www.googleapis.com/youtube/v3'
SCOPES = ('https://www.googleapis.com/auth/youtube.upload', 'https://www.googleapis.com/auth/youtube.readonly')
CONFIG_FILENAME = 'youtube.json'
PRIVACY = ('private', 'unlisted', 'public')
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 5000
TAGS_CHARS_LIMIT = 450  # YouTube allows 500 characters in total; keep headroom for its quoting rules
SHORTS_MAX_SEC = 60.0
DEFAULT_AUTO_STRATEGIES = ('youtube_shorts', 'youtube_long')
HTTP_TIMEOUT = 30
_STATES: dict[str, float] = {}
_STATE_TTL = 1800
_lock = threading.Lock()


class YouTubeError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class YouTubeConfig:
    client_id: str = ''
    client_secret: str = ''
    refresh_token: str = ''
    channel_title: str = ''
    channel_id: str = ''
    privacy: str = 'private'
    auto_post: bool = False
    strategies: list[str] = field(default_factory=lambda: list(DEFAULT_AUTO_STRATEGIES))

    @property
    def has_client(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        return self.has_client and bool(self.refresh_token)


def config_path() -> Path:
    from backend.core.path_utils import get_data_directory
    return get_data_directory() / CONFIG_FILENAME


def load_config() -> YouTubeConfig:
    path = config_path()
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            logger.warning('Could not read %s: %s', path, type(error).__name__)
    privacy = data.get('privacy') if data.get('privacy') in PRIVACY else 'private'
    strategies = [s for s in data.get('strategies') or DEFAULT_AUTO_STRATEGIES if isinstance(s, str)]
    return YouTubeConfig(
        client_id=str(os.getenv('YOUTUBE_CLIENT_ID') or data.get('client_id') or '').strip(),
        client_secret=str(os.getenv('YOUTUBE_CLIENT_SECRET') or data.get('client_secret') or '').strip(),
        refresh_token=str(data.get('refresh_token') or '').strip(),
        channel_title=str(data.get('channel_title') or ''), channel_id=str(data.get('channel_id') or ''),
        privacy=privacy, auto_post=bool(data.get('auto_post')), strategies=strategies or list(DEFAULT_AUTO_STRATEGIES),
    )


def _write(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _raw() -> dict[str, Any]:
    try:
        return json.loads(config_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_settings(*, client_id: str | None = None, client_secret: str | None = None, privacy: str | None = None,
                  auto_post: bool | None = None, strategies: list[str] | None = None) -> YouTubeConfig:
    """Only the fields passed are changed. A new client id / secret drops the old connection (its token belongs to the old client)."""
    data = _raw()
    if privacy is not None:
        if privacy not in PRIVACY:
            raise ValueError(f'privacy must be one of: {", ".join(PRIVACY)}')
        data['privacy'] = privacy
    if auto_post is not None:
        data['auto_post'] = bool(auto_post)
    if strategies is not None:
        data['strategies'] = [s for s in strategies if s]
    for key, value in (('client_id', client_id), ('client_secret', client_secret)):
        if value is not None and value.strip() != str(data.get(key) or ''):
            data[key] = value.strip()
            for stale in ('refresh_token', 'channel_title', 'channel_id'):
                data.pop(stale, None)
    _write(data)
    return load_config()


def disconnect() -> None:
    data = _raw()
    for key in ('refresh_token', 'channel_title', 'channel_id'):
        data.pop(key, None)
    _write(data)


# --------------------------------------------------------------------------- OAuth
def auth_url(redirect_uri: str) -> str:
    cfg = load_config()
    if not cfg.has_client:
        raise YouTubeError('Save the OAuth client id and secret first.', 400)
    state = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        for key in [k for k, at in _STATES.items() if now - at > _STATE_TTL]:
            _STATES.pop(key, None)
        _STATES[state] = now
    query = {'client_id': cfg.client_id, 'redirect_uri': redirect_uri, 'response_type': 'code', 'scope': ' '.join(SCOPES),
             'access_type': 'offline', 'prompt': 'select_account consent', 'include_granted_scopes': 'true', 'state': state}
    return f'{AUTH_URL}?{urlencode(query)}'


def complete_auth(code: str, state: str, redirect_uri: str, session: requests.Session | None = None) -> YouTubeConfig:
    with _lock:
        issued = _STATES.pop(state, None)
    if issued is None or time.time() - issued > _STATE_TTL:
        raise YouTubeError('This sign-in link expired or was not started here. Start Connect YouTube again.', 400)
    cfg = load_config()
    http = session or requests
    resp = http.post(TOKEN_URL, data={'code': code, 'client_id': cfg.client_id, 'client_secret': cfg.client_secret,
                                      'redirect_uri': redirect_uri, 'grant_type': 'authorization_code'}, timeout=HTTP_TIMEOUT)
    body = _json(resp)
    if resp.status_code != 200 or not body.get('refresh_token'):
        detail = body.get('error_description') or body.get('error') or 'Google did not return a refresh token'
        raise YouTubeError(f'Google sign-in failed: {detail}', 400)
    data = _raw()
    data['refresh_token'] = body['refresh_token']
    _write(data)
    try:
        channel = _channel(body.get('access_token') or '', http)
        data['channel_title'], data['channel_id'] = channel
        _write(data)
    except YouTubeError:
        logger.info('Connected, but the channel name could not be read')
    return load_config()


def _json(resp: requests.Response) -> dict[str, Any]:
    try:
        value = resp.json()
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _channel(access_token: str, http=requests) -> tuple[str, str]:
    resp = http.get(f'{API}/channels', params={'part': 'snippet', 'mine': 'true'},
                    headers={'Authorization': f'Bearer {access_token}'}, timeout=HTTP_TIMEOUT)
    items = _json(resp).get('items') or []
    if resp.status_code != 200 or not items:
        raise YouTubeError('No YouTube channel found for this Google account. Create a channel first.', resp.status_code)
    return str(items[0]['snippet'].get('title') or ''), str(items[0].get('id') or '')


def access_token(cfg: YouTubeConfig | None = None, http=requests) -> str:
    cfg = cfg or load_config()
    if not cfg.connected:
        raise YouTubeError('YouTube is not connected.', 400)
    resp = http.post(TOKEN_URL, data={'client_id': cfg.client_id, 'client_secret': cfg.client_secret,
                                      'refresh_token': cfg.refresh_token, 'grant_type': 'refresh_token'}, timeout=HTTP_TIMEOUT)
    body = _json(resp)
    if resp.status_code != 200 or not body.get('access_token'):
        if body.get('error') == 'invalid_grant':
            raise YouTubeError('YouTube access expired or was revoked. Connect YouTube again (tokens of an app in "Testing" mode last 7 days).', 401)
        raise YouTubeError(f'Could not get YouTube access: {body.get("error_description") or body.get("error") or resp.status_code}', resp.status_code)
    return str(body['access_token'])


# --------------------------------------------------------------------------- metadata
def _clean_tags(tags: list[str]) -> list[str]:
    out, total = [], 0
    for raw in tags or []:
        tag = str(raw).strip().lstrip('#').replace('<', '').replace('>', '')
        if not tag or tag.lower() in {t.lower() for t in out}:
            continue
        if total + len(tag) + 1 > TAGS_CHARS_LIMIT:
            break
        out.append(tag)
        total += len(tag) + 1
    return out


def build_metadata(post: dict[str, Any], fallback_title: str, duration: float, vertical: bool, privacy: str) -> dict[str, Any]:
    """YouTube snippet/status from a variant's generated post copy."""
    title = (str(post.get('title') or '').strip() or fallback_title.strip() or 'Clip')[:TITLE_LIMIT].rstrip()
    tags = _clean_tags(list(post.get('tags') or []))
    description = str(post.get('description') or '').strip()
    hashtags = ' '.join(f'#{t.replace(" ", "")}' for t in tags[:3])
    if hashtags and hashtags not in description:
        description = f'{description}\n\n{hashtags}'.strip()
    if vertical and duration <= SHORTS_MAX_SEC and '#shorts' not in description.lower():
        description = f'{description}\n#Shorts'.strip()
    return {
        'snippet': {'title': title.replace('<', '').replace('>', ''), 'description': description[:DESCRIPTION_LIMIT].replace('<', '').replace('>', ''),
                    'tags': tags, 'categoryId': '22'},
        'status': {'privacyStatus': privacy, 'selfDeclaredMadeForKids': False, 'embeddable': True},
    }


# --------------------------------------------------------------------------- upload
def upload_video(video: Path, metadata: dict[str, Any], token: str, thumbnail: Path | None = None, http=requests) -> dict[str, Any]:
    """Resumable upload. Returns {'video_id', 'url', 'privacy'}."""
    size = video.stat().st_size
    if not size:
        raise YouTubeError('The finished video file is empty.')
    start = http.post(UPLOAD_URL, params={'uploadType': 'resumable', 'part': 'snippet,status'}, data=json.dumps(metadata).encode('utf-8'),
                      headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=UTF-8',
                               'X-Upload-Content-Length': str(size), 'X-Upload-Content-Type': 'video/mp4'}, timeout=HTTP_TIMEOUT)
    if start.status_code != 200 or not start.headers.get('Location'):
        raise _api_error(start, 'YouTube refused the upload')
    with video.open('rb') as handle:
        done = http.put(start.headers['Location'], data=handle, headers={'Content-Length': str(size), 'Content-Type': 'video/mp4'},
                        timeout=(HTTP_TIMEOUT, 3600))
    if done.status_code not in (200, 201):
        raise _api_error(done, 'The YouTube upload did not finish')
    body = _json(done)
    video_id = str(body.get('id') or '')
    if not video_id:
        raise YouTubeError('YouTube did not return a video id.')
    if thumbnail and thumbnail.is_file():
        try:
            thumb = http.post('https://www.googleapis.com/upload/youtube/v3/thumbnails/set', params={'videoId': video_id},
                              data=thumbnail.read_bytes(), headers={'Authorization': f'Bearer {token}', 'Content-Type': 'image/jpeg'},
                              timeout=HTTP_TIMEOUT)
            if thumb.status_code != 200:
                logger.info('Custom thumbnail not set (HTTP %s); channels need phone verification for this', thumb.status_code)
        except requests.RequestException:
            logger.info('Custom thumbnail not set')
    return {'video_id': video_id, 'url': f'https://youtu.be/{video_id}',
            'privacy': str((body.get('status') or {}).get('privacyStatus') or metadata['status']['privacyStatus'])}


def _api_error(resp: requests.Response, prefix: str) -> YouTubeError:
    error = (_json(resp).get('error') or {}) if resp is not None else {}
    reasons = ','.join(str(item.get('reason')) for item in (error.get('errors') or []) if isinstance(item, dict))
    message = error.get('message') or f'HTTP {resp.status_code}'
    if 'quotaExceeded' in reasons:
        message = 'The daily YouTube API quota is used up (about 6 uploads per day). It resets at midnight Pacific time.'
    elif 'uploadLimitExceeded' in reasons:
        message = 'YouTube upload limit reached for this channel today.'
    elif 'youtubeSignupRequired' in reasons:
        message = 'This Google account has no YouTube channel.'
    return YouTubeError(f'{prefix}: {message}', resp.status_code)


# --------------------------------------------------------------------------- per-variant publishing
def _records_path(project_id: str) -> Path:
    from backend.services.studio import store
    return store.directory(project_id) / 'youtube_uploads.json'


def read_records(project_id: str) -> dict[str, Any]:
    try:
        return json.loads(_records_path(project_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _remember(project_id: str, variant_id: str, record: dict[str, Any]) -> None:
    with _lock:
        records = read_records(project_id)
        records[variant_id] = record
        path = _records_path(project_id)
        temporary = path.with_name(f'.{path.name}.{secrets.token_hex(4)}.tmp')
        temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)


def _fallback_copy(project_id: str, post: dict[str, Any], title: str) -> dict[str, Any]:
    """Copy for clips whose post text is empty (no AI model was available): credit the source, keywords from the title."""
    import re
    from backend.services.studio import store
    meta = store.read(project_id).get('source_meta') or {}
    source = str(meta.get('title') or '').strip()
    channel = str(meta.get('channel') or '').strip()
    url = ''
    try:
        from backend.core.database import SessionLocal
        from backend.models.project import Project
        with SessionLocal() as db:
            project = db.get(Project, project_id)
            url = str(((project.project_metadata or {}).get('source_url') if project else '') or getattr(project, 'source_url', '') or '')
    except Exception:  # noqa: BLE001 - the credit line is a nicety
        url = ''
    lines = [str(post.get('title') or title).strip()]
    if source:
        lines.append(f'Clip from: {source}' + (f' ({channel})' if channel else ''))
    if url:
        lines.append(f'Original video: {url}')
    words = [w for w in re.findall(r"[A-Za-z0-9]{3,}", f'{title} {source}') if w.lower() not in {'the', 'and', 'for', 'with', 'from'}]
    tags = list(post.get('tags') or [])
    for word in words:
        if len(tags) >= 8:
            break
        if word.lower() not in {t.lower() for t in tags}:
            tags.append(word)
    return {**post, 'description': str(post.get('description') or '').strip() or '\n'.join(lines), 'tags': tags}


def publish_variant(project_id: str, variant_id: str, *, privacy: str | None = None, http=requests) -> dict[str, Any]:
    """Upload one finished output variant with its generated title / description / tags. Already uploaded -> the old record."""
    from backend.services.studio import publish_kit, publishing, store
    existing = read_records(project_id).get(variant_id)
    if existing and existing.get('status') == 'uploaded':
        return existing
    cfg = load_config()
    if not cfg.connected:
        raise YouTubeError('YouTube is not connected. Settings -> Publishing -> Connect YouTube.', 400)
    meta = publishing.output_variant_meta(project_id, variant_id)
    video = Path(meta['video_path'])
    width, height = int(meta.get('width') or 0), int(meta.get('height') or 0)
    chosen = privacy if privacy in PRIVACY else cfg.privacy
    post = dict(meta.get('post') or {})
    if not str(post.get('description') or '').strip() or not post.get('tags'):
        post = _fallback_copy(project_id, post, str(meta.get('generated_title') or ''))
    metadata = build_metadata(post, str(meta.get('generated_title') or ''), float(meta.get('duration_sec') or 0),
                              height > width > 0, chosen)
    cover, _ = publish_kit.cover_file(project_id, str(meta['studio_job_id']), str(meta['strategy_id']))
    _remember(project_id, variant_id, {'status': 'uploading', 'started_at': store.now()})
    try:
        result = upload_video(video, metadata, access_token(cfg, http), cover, http)
    except Exception as error:
        _remember(project_id, variant_id, {'status': 'failed', 'error': str(error)[:500], 'finished_at': store.now()})
        raise
    record = {'status': 'uploaded', 'finished_at': store.now(), 'title': metadata['snippet']['title'], **result}
    _remember(project_id, variant_id, record)
    return record


def maybe_auto_publish(project_id: str, job_id: str) -> None:
    """Called when a render finishes: upload the matching variants in the background when auto-post is on."""
    cfg = load_config()
    if not (cfg.auto_post and cfg.connected):
        return
    from backend.services.studio import store
    variants = [v for v in store.read(project_id).get('output_variants', [])
                if v.get('render_job_id') == job_id and v.get('status') == 'completed' and v.get('strategy_id') in cfg.strategies]
    for variant in variants:
        threading.Thread(target=_auto_job, args=(project_id, variant['id']), name='youtube-upload', daemon=True).start()


def _auto_job(project_id: str, variant_id: str) -> None:
    try:
        publish_variant(project_id, variant_id)
    except Exception as error:  # noqa: BLE001 - the failure is in the record; the clip itself is untouched
        logger.warning('Automatic YouTube upload failed: %s', type(error).__name__)
