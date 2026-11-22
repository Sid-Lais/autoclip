"""Unified creative workspace. Legacy projects are adapted, never overwritten."""
import asyncio
import threading
import json
import uuid
from pathlib import Path
from typing import Optional, Literal
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, Query
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from backend.api.v1.gate_errors import gate_refusal
from backend.core.database import get_db
from backend.core.sentry_setup import capture_studio_exception
from backend.models.project import Project
from backend.models.clip import Clip
from backend.schemas.project import ProjectCreate, ProjectType
from backend.services.project_service import ProjectService
from backend.services.platform_strategy import list_platform_strategies
from backend.services.studio import store, jobs, intelligence, range_import, ledger
from backend.services.studio.models import Draft, CreateDraft, DuplicateDraft, ExportDraftRequest, RewriteRequest, Preferences, Language, Scene, ImportOptions, ConfirmPlan, AppendPlatformsRequest, PostCopy

router = APIRouter()


def project_or_404(project_id: str, db: Session):
    call(store.directory, project_id)
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(404, '项目不存在')
    return project

from backend.core.failure_attribution import ModelResponseInvalid  

def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except store.ConflictError as error:
        if getattr(error, 'error_code', None):  # a coded refusal (frontend errorText maps the code)
            raise HTTPException(error.status, {'error_code': error.error_code, 'message': str(error)}) from None
        raise HTTPException(409, str(error)) from None
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        from backend.services.studio.readiness import TranscriptionNotReady
        from backend.services.studio.preflight import SourceRejected
        if isinstance(error, (TranscriptionNotReady, SourceRejected)):
            raise gate_refusal(error) from None
        raise HTTPException(422, str(error)) from None

def validate_draft(project_id, draft):
    duration = intelligence._probe(jobs.source(project_id)).get('duration', 0)
    intelligence.validate_scenes(draft.scenes, duration)

async def _stage_for_gate(video, first_chunk, ext, *, srt_available: bool, route: str = 'studio', goal: str | None = 'auto'):
    """Stage the upload outside any project and run the one preflight on it, in the threadpool.
    Any refusal or failure removes the staged file, so a refused import leaves no project, state or file."""
    from backend.services.studio import preflight
    from backend.api.v1 import transcription_gate
    staged = await stage_upload(video, first_chunk, ext)
    try:
        subtitle = staged.with_suffix('.srt') if srt_available else None
        if subtitle is not None:
            subtitle.write_bytes(b'')  # the SRT is written into the project later; only its presence counts here
        try:
            await transcription_gate.run(preflight.ResolvedInputs(staged, subtitle, entry=route, goal=goal))
        finally:
            if subtitle is not None:
                subtitle.unlink(missing_ok=True)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged

@router.get('/title-presets/{style}/thumbnail')
def title_preset_thumbnail(style: Literal['comic', 'neon', 'arena', 'editorial', 'pixel', 'frosted'], v: int = Query(1, ge=1, le=6)):
    from backend.services.studio.title_art import thumbnail
    return Response(call(thumbnail, style, v), media_type='image/png', headers={'Cache-Control':'public, max-age=86400'})

@router.get('/readiness')
def import_readiness():
    from backend.services.studio import readiness
    return readiness.report()


class InspectUrlBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=8, max_length=2000)
    browser: Optional[Literal['chrome', 'edge', 'firefox', 'safari']] = None
    analysis_mode: Optional[Literal['auto', 'subtitle', 'visual']] = None


# Same-URL inspect shares one threadpool job so rapid paste does not stack workers.
_INSPECT_INFLIGHT: dict[str, asyncio.Task] = {}
_INSPECT_MAP_LOCK = threading.Lock()
INSPECT_WAIT_SEC = 10


def inspect_flight_key(url: str, browser: Optional[str] = None) -> str:
    """Normalize URL + browser for the in-flight map (strip; host lower-cased)."""
    raw = (url or '').strip()
    parsed = urlparse(raw)
    host = (parsed.hostname or '').lower()
    if host:
        netloc = host
        if parsed.port:
            netloc = f'{host}:{parsed.port}'
        # Keep userinfo out (rejected by the handler anyway).
        path = parsed.path or ''
        query = f'?{parsed.query}' if parsed.query else ''
        fragment = f'#{parsed.fragment}' if parsed.fragment else ''
        raw = f'{parsed.scheme.lower()}://{netloc}{path}{query}{fragment}'
    return f'{raw}|{browser or ""}'


def clear_inspect_inflight() -> None:
    """Test helper: drop in-flight map entries."""
    with _INSPECT_MAP_LOCK:
        _INSPECT_INFLIGHT.clear()


async def await_inspect_url(url: str, browser: Optional[str] = None, *, timeout: float = INSPECT_WAIT_SEC):
    """Run jobs.inspect_url once per in-flight key; waiters share the same Task.

    wait_for uses shield so one client's timeout does not cancel the shared
    worker (followers can still receive the result). The worker itself is
    bounded by inspect socket_timeout.
    """
    key = inspect_flight_key(url, browser)
    with _INSPECT_MAP_LOCK:
        task = _INSPECT_INFLIGHT.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                run_in_threadpool(jobs.inspect_url, url.strip(), browser),
            )
            _INSPECT_INFLIGHT[key] = task

            def _clear(done: asyncio.Future, k: str = key) -> None:
                with _INSPECT_MAP_LOCK:
                    cur = _INSPECT_INFLIGHT.get(k)
                    if cur is done:
                        _INSPECT_INFLIGHT.pop(k, None)

            task.add_done_callback(_clear)
    return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


@router.post('/inspect-url')
async def inspect_url(body: InspectUrlBody):
    """F4: info-only probe before download. 10 s timeout; cookie/private failures reuse download recovery."""
    from backend.core.failure_codes import user_visible_error
    from backend.pipeline.failures import PipelineFailure
    parsed = urlparse(body.url.strip())
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or parsed.username or parsed.password or not any(
        host == d or host.endswith('.' + d) for d in ('youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv')
    ):
        raise HTTPException(422, 'Only HTTPS Bilibili or YouTube video links are supported')
    from backend.services.studio import planning
    mode = body.analysis_mode
    if mode is None:
        mode = planning.preferred_analysis_mode() or 'auto'
    try:
        meta = await await_inspect_url(body.url.strip(), body.browser)
    except asyncio.TimeoutError:
        raise HTTPException(504, 'Timed out reading video info; please try again later') from None
    except PipelineFailure as error:
        raise HTTPException(
            422,
            {'error_code': error.code or 'source_blocked', 'message': error.user_message()},
        ) from None
    except Exception as error:
        coded = getattr(error, 'error_code', None) or getattr(error, 'code', None)
        message = user_visible_error(error)
        if coded:
            raise HTTPException(422, {'error_code': coded, 'message': message}) from None
        raise HTTPException(422, message or 'Unable to read video info') from None
    route = planning.preview_route(
        meta.get('duration'),
        bool(meta.get('has_manual_subtitle')),
        meta.get('has_audio'),
        mode,
    )
    return {**meta, **route, 'long_video': range_import.is_long(meta.get('duration'))}


QUEUE_DAYS = 3
QUEUE_LIMIT = 60


def _queue_item(project, state: dict) -> dict:
    """One row of the queue: where this project is, in words and as a 0-100 bar."""
    analysis = state.get('analysis') or {}
    variants = state.get('output_variants') or []
    status, phase = analysis.get('status'), analysis.get('phase')
    error = str(analysis.get('error') or '')
    if status == 'completed':
        kind, stage, percent = 'done', 'done', 100
    elif status == 'failed':
        skipped = 'No gameplay found' in error
        kind, stage, percent = ('skipped' if skipped else 'failed'), 'skipped' if skipped else 'failed', 0
    elif status == 'running':
        kind = 'running'
        raw = analysis.get('percent')
        if phase == 'rendering':
            stage, percent = 'rendering', 80 if any(v.get('status') in ('queued', 'rendering') for v in variants) else 90
        elif phase == 'production':
            stage, percent = 'analyzing', 45
        else:
            stage = 'downloading'
            percent = 5 + round(min(max(float(raw), 0.0), 100.0) * 0.3) if isinstance(raw, (int, float)) else 10
    else:
        kind, stage, percent = 'queued', 'queued', 0
    return {'project_id': str(project.id), 'name': project.name, 'kind': kind, 'stage': stage, 'percent': percent,
            'message': str(analysis.get('message') or '')[:160], 'error': error[:200] if kind in ('failed', 'skipped') else '',
            'created_at': project.created_at.isoformat() + 'Z' if project.created_at else None}


@router.get('/queue')
def clip_queue(db: Session = Depends(get_db)):
    """Recent projects with their live stage, newest first. Powers the queue panel on the home page."""
    from datetime import datetime, timedelta
    rows = (db.query(Project).filter(Project.created_at >= datetime.utcnow() - timedelta(days=QUEUE_DAYS))
            .order_by(Project.created_at.desc()).limit(QUEUE_LIMIT).all())
    items = []
    for project in rows:
        try:
            state = store.read(str(project.id))
        except Exception:  # noqa: BLE001 - a project without Studio state (legacy) is not part of the queue
            continue
        items.append(_queue_item(project, state))
    counts = {kind: sum(1 for item in items if item['kind'] == kind) for kind in ('queued', 'running', 'done', 'failed', 'skipped')}
    return {'items': items, 'counts': counts}

@router.get('/capabilities')
def capabilities():
    return {'visual_analysis': intelligence.ready(), 'visual_model': intelligence.visual_config()[2], 'languages': ['source', 'zh', 'en', 'ja']}


@router.get('/platform-strategies')
def platform_strategies():
    """Public output targets for the quick-generation entry point."""
    return {'strategies': list_platform_strategies()}


from backend.services.studio import vision_settings, analysis_preferences

@router.get('/analysis-preferences')
def get_analysis_preferences():
    return call(analysis_preferences.load)

@router.put('/analysis-preferences')
def save_analysis_preferences(body: analysis_preferences.AnalysisPreferences):
    return call(analysis_preferences.save, body)


@router.get('/vision-settings')
def get_vision_settings():
    return vision_settings.public()

@router.put('/vision-settings')
def save_vision_settings(body: vision_settings.VisionSettingsInput):
    return call(vision_settings.save, body)

@router.post('/vision-settings/test')
def test_vision_settings(body: vision_settings.VisionSettingsInput):
    try:
        return vision_settings.test(body)
    except Exception as error:
        capture_studio_exception(error, 'vision_test', analysis_mode='visual')
        raise HTTPException(502, 'Vision connection test failed; check the endpoint, key, and whether the model supports image input') from None

@router.post('/import')
async def import_visual(
    goal: Literal['auto', 'content', 'highlight', 'promo'] = Form('auto'),
    language: Language = Form('source'),
    aspect: Optional[Literal['original', 'portrait', 'landscape']] = Form(None),
    duration: Optional[int] = Form(None, ge=10, le=120),
    instruction: str = Form('', max_length=1000),
    platforms: list[str] = Form(['douyin']),
    auto_start: bool = Form(False),
    portrait_style: Literal['auto', 'interview', 'podcast'] = Form('auto'),
    html_template: Optional[Literal['editorial', 'street', 'news', 'cinema', 'classic']] = Form(None),
    recommended_template: Optional[Literal['editorial', 'street', 'classic']] = Form(None),
    brand_outro_enabled: bool = Form(True),
    subtitle: Optional[UploadFile] = File(None),
    name: str = Form('智能剪辑', max_length=200),
    url: Optional[str] = Form(None),
    browser: Optional[Literal['chrome', 'edge', 'firefox', 'safari']] = Form(None),
    video: Optional[UploadFile] = File(None),
    source_window_start: Optional[float] = Form(None, ge=0),
    source_window_end: Optional[float] = Form(None, gt=0),
    features: str = Form(''),
    db: Session = Depends(get_db),
):
    if bool(url) == bool(video):
        raise HTTPException(422, 'Provide a video link or file')
    from backend.services.studio import readiness
    call(readiness.ensure_analysis_usable)
    if url:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or parsed.username or parsed.password or not any(host == d or host.endswith('.' + d) for d in ('youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv')):
            raise HTTPException(422, 'Only HTTPS Bilibili or YouTube video links are supported')
    ext = Path(video.filename or '').suffix.lower() if video else '.mp4'
    if ext not in ('.mp4', '.mov', '.mkv', '.webm', '.avi'):
        raise HTTPException(422, 'Unsupported video format')
    first_chunk = await video.read(1024 * 1024) if video else None
    if video and not first_chunk:
        await video.close()
        raise HTTPException(422, 'The video file is empty; please choose another')
    subtitle_bytes = None
    if subtitle:
        try:
            if Path(subtitle.filename or '').suffix.lower() != '.srt':
                raise HTTPException(422, 'Subtitle files must be SRT')
            subtitle_bytes = await subtitle.read(2 * 1024 * 1024 + 1)
            if not subtitle_bytes or len(subtitle_bytes) > 2 * 1024 * 1024:
                raise HTTPException(422, 'Subtitle file is empty or larger than 2 MB')
        finally:
            await subtitle.close()
    staged = None
    if video:
        # 1.5.7: block before creating a project when local Whisper is required but missing.
        try:
            staged = await _stage_for_gate(video, first_chunk, ext, srt_available=subtitle_bytes is not None, goal=goal)
        except HTTPException:
            await video.close()
            raise
    try:
        source_window = None
        if source_window_end is not None:
            source_window = {
                'start': float(source_window_start or 0),
                'end': float(source_window_end),
            }
        prefs = ImportOptions(
            goal=goal, language=language, aspect=aspect, duration=duration, instruction=instruction,
            platforms=platforms, auto_start=auto_start, portrait_style=portrait_style, html_template=html_template,
            recommended_template=recommended_template,
            branding={'outro_enabled': brand_outro_enabled},
            source_window=source_window,
        )
        project = ProjectService(db).create_project(ProjectCreate(name=name.strip() or '智能剪辑', project_type=ProjectType.DEFAULT, source_url=url, settings={'creative': {'goal': goal}, 'smart_import': prefs.model_dump(), 'import_staging': not prefs.auto_start, 'creative_browser': browser, 'platforms': prefs.platforms, 'brand_outro_enabled': prefs.branding.outro_enabled}))
    except BaseException:
        if staged is not None:
            staged.unlink(missing_ok=True)
        raise
    pid = str(project.id)
    try:
        # mkdir inside the try: a failure here still runs the staging cleanup below.
        raw = store.directory(pid) / 'raw'
        raw.mkdir(parents=True, exist_ok=True)
        if subtitle_bytes is not None:
            (raw / 'input.srt').write_bytes(subtitle_bytes)
            if not video:
                # Upload carried an SRT but no local video yet (URL path); still stamp origin.
                from backend.services.studio import pipeline_resume
                pipeline_resume.record_srt_origin(store.directory(pid) / 'metadata', 'attached')
        if video:
            path = raw / ('input' + ext)
            if staged is not None:
                import shutil
                shutil.move(str(staged), str(path))
            else:
                with path.open('wb') as target:
                    target.write(first_chunk)
                    while chunk := await video.read(1024 * 1024):
                        target.write(chunk)
            project.video_path = str(path)
            db.commit()
            if subtitle_bytes is not None:
                # P1 #6: user-attached SRT must survive 「从头重做」.
                from backend.services.studio import pipeline_resume
                pipeline_resume.record_srt_origin(
                    store.directory(pid) / 'metadata', 'attached', video=path,
                )
        snapshot = None
        if features and len(features) <= 4000:
            try:
                parsed = json.loads(features)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                snapshot = parsed
        run_id = call(jobs.inspect_project, pid, prefs, url, browser, features=snapshot)
    except Exception:
        project.status = 'failed'
        db.commit()
        raise
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)  # no-op once moved into the project
        if video:
            await video.close()
    return {'project_id': pid, 'analysis_run_id': run_id}

def _require_supported_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if parsed.scheme != 'https' or parsed.username or parsed.password or not any(
        host == d or host.endswith('.' + d) for d in ('youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv')
    ):
        raise HTTPException(422, 'Only HTTPS Bilibili or YouTube video links are supported')


def _start_clip_project(db: Session, url: str, title: str, options: range_import.ClipOptions, source_range=None):
    """Create one auto-started project for ``url``: a single downloaded range, or the whole (short) video."""
    snapshot = options.features if options.features and len(json.dumps(options.features)) <= 4000 else None
    ranged = source_range is not None
    goal = 'content' if ranged else 'auto'
    prefs = ImportOptions(
        goal=goal, language=options.language, aspect=options.aspect, duration=options.duration, instruction=options.instruction,
        platforms=options.platforms, auto_start=True, portrait_style=options.portrait_style, html_template=options.html_template,
        recommended_template=options.recommended_template, branding={'outro_enabled': options.brand_outro_enabled},
        source_range=source_range.model_dump() if ranged else None,
        visual_focus=options.visual_focus if ranged else None,
    )
    name = range_import.project_name(title, source_range) if ranged else ((title or 'Smart editing').strip()[:200] or 'Smart editing')
    project = ProjectService(db).create_project(ProjectCreate(
        name=name, project_type=ProjectType.DEFAULT, source_url=url,
        settings={'creative': {'goal': goal}, 'smart_import': prefs.model_dump(), 'import_staging': False,
                  'creative_browser': options.browser, 'platforms': prefs.platforms, 'brand_outro_enabled': prefs.branding.outro_enabled}))
    pid = str(project.id)
    try:
        (store.directory(pid) / 'raw').mkdir(parents=True, exist_ok=True)
        run_id = call(jobs.inspect_project, pid, prefs, url, options.browser, features=snapshot)
    except Exception:
        project.status = 'failed'
        db.commit()
        raise
    return {'project_id': pid, 'analysis_run_id': run_id}


@router.post('/import-ranges')
def import_ranges(body: range_import.ImportRangesBody, db: Session = Depends(get_db)):
    """Long-video mode: one project per chosen range. Only those ranges are downloaded, never the whole video."""
    from backend.services.studio import readiness
    call(readiness.ensure_analysis_usable)
    url = body.url.strip()
    _require_supported_url(url)
    created, failure, duplicates = [], None, 0
    used, _ = ledger.used_parts(db, url, store.read)
    for spec in body.ranges:
        if ledger.is_repeat(spec.start, spec.end, used):
            duplicates += 1
            continue
        try:
            created.append({**_start_clip_project(db, url, body.title, body, spec), 'start': spec.start, 'end': spec.end})
            used.append((spec.start, spec.end))
        except Exception as error:  # noqa: BLE001 - earlier ranges keep running; report this one
            failure = error
            break
    if not created and failure is not None:
        raise failure
    return {'projects': created, 'skipped': len(body.ranges) - len(created) - duplicates, 'duplicates': duplicates}


class SearchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(default=12, ge=1, le=25)


@router.post('/search')
async def search_videos(body: SearchBody):
    """Find videos for a query, most viewed first; the top few come back flagged ``suggested``."""
    from backend.services.studio import discovery
    try:
        results = await asyncio.wait_for(run_in_threadpool(discovery.search_videos, body.query.strip(), body.limit), timeout=40)
    except asyncio.TimeoutError:
        raise HTTPException(504, 'The search took too long. Try again in a moment.') from None
    except Exception as error:  # noqa: BLE001
        from backend.core.failure_codes import user_visible_error
        raise HTTPException(422, user_visible_error(error) or 'The search could not be completed.') from None
    return {'results': results}


class SuggestBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    url: str = Field(min_length=8, max_length=2000)
    browser: Optional[Literal['chrome', 'edge', 'firefox', 'safari']] = None


@router.post('/suggest-clips')
async def suggest_clips(body: SuggestBody, db: Session = Depends(get_db)):
    """The parts of a video worth clipping, from YouTube's "Most replayed" graph (chapters as fallback)."""
    from backend.services.studio import discovery
    _require_supported_url(body.url.strip())
    used, whole_used = ledger.used_parts(db, body.url.strip(), store.read)
    try:
        return await asyncio.wait_for(run_in_threadpool(discovery.suggest_clips, body.url.strip(), body.browser, used, whole_used), timeout=40)
    except asyncio.TimeoutError:
        raise HTTPException(504, 'Reading the video took too long. Try again in a moment.') from None
    except Exception as error:  # noqa: BLE001
        from backend.core.failure_codes import user_visible_error
        raise HTTPException(422, user_visible_error(error) or 'The video could not be read.') from None


@router.post('/auto-clip')
def auto_clip(body: range_import.AutoClipBody, db: Session = Depends(get_db)):
    """Make clips for every approved item: its suggested ranges, or the whole video when it is short enough."""
    from backend.services.studio import readiness
    call(readiness.ensure_analysis_usable)
    created, skipped, duplicates = [], [], 0
    for item in body.items:
        url = item.url.strip()
        _require_supported_url(url)
        used, whole_used = ledger.used_parts(db, url, store.read)
        if not item.ranges and whole_used:
            duplicates += 1
            continue
        if not item.ranges:
            try:
                if range_import.is_long(jobs.inspect_url(url, body.browser).get('duration')):
                    skipped.append({'url': url, 'reason': 'This video is longer than 1.5 hours: pick the parts to clip.'})
                    continue
            except Exception:  # noqa: BLE001 - an unreadable link is reported, the rest still run
                skipped.append({'url': url, 'reason': 'The video could not be read.'})
                continue
        for spec in item.ranges or [None]:
            if spec is not None and ledger.is_repeat(spec.start, spec.end, used):
                duplicates += 1
                continue
            try:
                made = _start_clip_project(db, url, item.title, body, spec)
                if spec is not None:
                    used.append((spec.start, spec.end))
                created.append({**made, 'url': url, **({'start': spec.start, 'end': spec.end} if spec else {})})
            except Exception as error:  # noqa: BLE001
                from backend.core.failure_codes import user_visible_error
                skipped.append({'url': url, 'reason': user_visible_error(error) or 'This clip could not be started.'})
    if not created and skipped:
        raise HTTPException(422, skipped[0]['reason'])
    if not created and duplicates:
        raise HTTPException(409, 'These parts were already clipped. Nothing new to make from them.')
    return {'projects': created, 'skipped': skipped, 'duplicates': duplicates}



async def stage_upload(video: UploadFile, first_chunk: bytes, ext: str) -> Path:
    """Write the upload outside any project so a refused import leaves no project behind."""
    staging = store.get_projects_directory().parent / 'import-staging'
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / (uuid.uuid4().hex + ext)
    try:
        with path.open('wb') as target:
            target.write(first_chunk)
            while chunk := await video.read(1024 * 1024):
                target.write(chunk)
    except BaseException:  # includes client disconnect / cancellation mid-upload
        path.unlink(missing_ok=True)
        raise
    return path


@router.post('/template-recommend')
async def template_recommend(
    url: Optional[str] = Form(None),
    header: Optional[UploadFile] = File(None),
):
    """Recommend an editing style from a link or a file header. No download."""
    if url:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or parsed.username or parsed.password or not any(host == domain or host.endswith('.' + domain) for domain in ('youtube.com', 'youtu.be', 'bilibili.com', 'b23.tv')):
            raise HTTPException(422, 'Only HTTPS Bilibili or YouTube video links are supported')
    blob = None
    if header is not None:
        try:
            blob = await header.read(1024 * 1024)
        finally:
            await header.close()
    if not url and not blob:
        raise HTTPException(422, 'Provide a video link or file')
    from backend.services.studio.template_recommend import recommend_template
    return recommend_template(url=url, header=blob)


@router.get('/{project_id}')
def workspace(project_id: str, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    from backend.services.studio import attempts
    from backend.services.studio import pipeline_resume
    data = attempts.public_workspace(call(store.recover, project_id))
    resume_point = pipeline_resume.resume_point_for_project(project_id)
    return {**data, 'resume_point': resume_point, 'material_origin': 'sample' if (project.processing_config or {}).get('example') else 'user', 'example_version': (project.processing_config or {}).get('example_version'), 'jobs': [{k: v for k, v in j.items() if k not in ('instance', 'snapshot')} for j in data['jobs']]}

@router.get('/{project_id}/source-preview')
def source_preview_status(project_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    from backend.services.studio import preview
    return call(preview.status, project_id)

@router.post('/{project_id}/source-preview')
def prepare_source_preview(project_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    from backend.services.studio import preview
    return call(preview.start, project_id)

@router.get('/{project_id}/source-preview/video')
def source_preview_video(project_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    from backend.services.studio import preview
    return FileResponse(call(preview.ready_file, project_id), media_type='video/mp4')

@router.get('/{project_id}/source')
def source_video(project_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    path = call(jobs.source, project_id)
    return FileResponse(path)

@router.get('/{project_id}/drafts/{draft_id}/thumbnail')
def draft_thumbnail(project_id: str, draft_id: str, revision: int = Query(..., ge=1),
                    job_id: Optional[str] = Query(None, pattern=r'^[a-f0-9]{32}$'), db: Session = Depends(get_db)):
    from backend.services.studio.thumbnails import draft_frame
    project_or_404(project_id, db)
    data = call(draft_frame, project_id, draft_id, revision, job_id)
    # Server memory cache is keyed by actual source stats. Revalidate on navigation.
    return Response(data, media_type='image/jpeg', headers={'Cache-Control': 'private, no-cache'})

@router.post('/{project_id}/title-preview')
def title_preview(project_id: str, body: Draft, db: Session = Depends(get_db), layer: Literal['artwork', 'backdrop'] = 'artwork'):
    from backend.services.studio import title_art
    project_or_404(project_id, db)
    info = call(intelligence._probe, call(jobs.source, project_id))
    w,h = {'portrait':(1080,1920),'landscape':(1920,1080)}.get(body.aspect, (info.get('width'),info.get('height')))
    if not w or not h:
        raise HTTPException(422,'Unable to read source video dimensions')
    # Use the same output resolution/font metrics as export; no LLM call or draft mutation.
    w,h = int(w)//2*2, int(h)//2*2
    if layer == 'backdrop':
        from backend.services.studio.title_materials import backdrop_png
        data = call(backdrop_png, body.hook, body.title_style, w, h, **title_art.options_for(body))
    else:
        data = call(title_art.png_bytes, body.hook, body.title_style, w, h, **title_art.options_for(body))
    return Response(data, media_type='image/png', headers={'Cache-Control':'no-store'})

@router.get('/{project_id}/candidates')
def candidates(project_id: str, db: Session = Depends(get_db)):
    """Only offer ranges from this project's actual source video."""
    project_or_404(project_id, db)
    info = call(intelligence._probe, call(jobs.source, project_id))
    duration = info.get('duration', 0)
    rows = []
    skipped = 0
    raw_events = call(store.read, project_id)['events']
    raw_clips = db.query(Clip).filter(Clip.project_id == project_id).order_by(Clip.start_time).all()
    sources = [('visual', e) for e in raw_events]
    sources.extend(('legacy', {'id': str(c.id), 'label': c.title[:120], 'start': c.start_time,
                              'end': c.end_time, 'evidence': (c.recommendation_reason or '')[:1000]}) for c in raw_clips)
    for kind, raw in sources:
        try:
            scene = Scene.model_validate(raw)
            intelligence.validate_scenes([scene], duration)
        except ValueError:
            skipped += 1
            continue
        rows.append({**scene.model_dump(), 'id': f'{kind}-{scene.id}', 'kind': kind})
    return {'duration': duration, 'candidates': rows,
            'warnings': [f'{skipped} clip(s) with invalid timing were excluded from candidates'] if skipped else []}

@router.post('/{project_id}/analyze')
def analyze_again(project_id: str, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    config = project.processing_config or {}
    # Legacy visual projects must enter the same screening/confirmation boundary.
    # A stored creative goal describes output, not permission for a new paid scan.
    prefs = ImportOptions.model_validate(config['smart_import']) if 'smart_import' in config else ImportOptions.model_validate(Preferences.model_validate(config.get('creative', {})).model_dump())
    url = None
    try:
        jobs.source(project_id)
    except FileNotFoundError:
        url = (project.project_metadata or {}).get('source_url')
        if not url:
            raise HTTPException(404, 'Source footage not found; please re-import')
    if not url:
        from backend.services.studio import preflight
        video = jobs.source(project_id)
        call(preflight.check, preflight.ResolvedInputs(video, video.parent / 'input.srt', entry='studio', goal=prefs.goal))
    run_id = call(jobs.inspect_project, project_id, prefs, url, (project.processing_config or {}).get('creative_browser'))
    return {'ok': True, 'analysis_run_id': run_id}

class RetryCommand(BaseModel):
    """The one retry command (#419): the backend validates the parent and creates the attempt."""
    model_config = ConfigDict(extra='forbid')
    scope: Literal['analysis', 'variant']
    variant_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    parent_attempt_id: Optional[str] = Field(default=None, pattern=r'^t-[a-z0-9-]{10,100}$')
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r'^[A-Za-z0-9_.:-]+$')
    fresh: bool = False  # 「从头重做」clears pipeline cache before resume


@router.post('/{project_id}/retry')
def retry_command(project_id: str, body: RetryCommand, db: Session = Depends(get_db)):
    """Returns the attempt receipt. Same idempotency_key -> same receipt, dispatched once."""
    from backend.services.studio import attempts
    project = project_or_404(project_id, db)
    retry = {'parent_attempt_id': body.parent_attempt_id, 'key': body.idempotency_key, 'fresh': bool(body.fresh)}
    if body.scope == 'variant':
        if not body.variant_id:
            raise HTTPException(422, 'Missing export version')
        variant = call(jobs.retry_variant, project_id, body.variant_id, retry)
        receipt = attempts.by_key(call(store.read, project_id), body.idempotency_key) or attempts.current(call(store.read, project_id))
        return {'attempt': attempts.public(receipt) if receipt else None, 'variant': variant}
    config = project.processing_config or {}
    prefs = ImportOptions.model_validate(config['smart_import']) if 'smart_import' in config else ImportOptions.model_validate(Preferences.model_validate(config.get('creative', {})).model_dump())
    url = None
    try:
        jobs.source(project_id)
    except FileNotFoundError:
        url = (project.project_metadata or {}).get('source_url')
        if not url:
            raise HTTPException(404, 'Source footage not found; please re-import')
    run_id = call(jobs.inspect_project, project_id, prefs, url, config.get('creative_browser'), retry=retry)
    receipt = attempts.by_key(call(store.read, project_id), body.idempotency_key)
    return {'ok': True, 'analysis_run_id': run_id, 'attempt': attempts.public(receipt) if receipt else None}


@router.put('/{project_id}/plan')
def correct_plan(project_id: str, body: ImportOptions, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    # Keep the check, requested preferences and job reservation atomic with other submissions.
    with store.lock:
        if (store.recover(project_id).get('analysis') or {}).get('status') == 'running':
            raise HTTPException(409, 'Analysis is still running; you can edit the plan once it finishes. Existing drafts are kept')
        url = None
        try:
            jobs.source(project_id)
        except FileNotFoundError:
            url = (project.project_metadata or {}).get('source_url')
            if not url:
                raise HTTPException(404, 'Source footage not found; please re-import')
        if not url:
            from backend.services.studio import preflight
            video = jobs.source(project_id)
            call(preflight.check, preflight.ResolvedInputs(video, video.parent / 'input.srt', entry='studio', goal=body.goal))
        previous_config = dict(project.processing_config or {})
        project.processing_config = {**previous_config, 'smart_import': body.model_dump()}
        db.commit()
        try:
            run_id = call(jobs.inspect_project, project_id, body, url, previous_config.get('creative_browser'))
        except Exception:
            # A rejected submission must not persist preferences for a plan
            # that was never produced. inspect_project restores the JSON state.
            project.processing_config = previous_config
            db.commit()
            raise
    return {'ok': True, 'analysis_run_id': run_id}

@router.post('/{project_id}/start')
def confirm_and_start(project_id: str, body: ConfirmPlan, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    run_id = call(jobs.confirm_project, project_id, body)
    return {'ok': True, 'analysis_run_id': run_id}

class EtaReport(BaseModel):
    """First ETA from the desktop progress bar (minutes)."""
    model_config = ConfigDict(extra='forbid')
    eta_initial_min: float = Field(gt=0, le=24 * 60)


@router.put('/{project_id}/eta')
def report_eta(project_id: str, body: EtaReport, db: Session = Depends(get_db)):
    """Persist eta_initial_min onto the open generation so the finished event can carry it."""
    project_or_404(project_id, db)
    minutes = round(float(body.eta_initial_min), 2)
    with store.lock:
        data = call(store.recover, project_id)
        generation = data.get('generation') if isinstance(data.get('generation'), dict) else None
        analysis = data.get('analysis') if isinstance(data.get('analysis'), dict) else None
        running = (generation and generation.get('status') in ('screening', 'production', 'rendering', 'awaiting_confirmation')) \
            or (analysis and analysis.get('status') == 'running')
        if not running or generation is None:
            return {'ok': False, 'reason': 'not_running'}
        if isinstance(generation.get('eta_initial_min'), (int, float)) and not isinstance(generation.get('eta_initial_min'), bool) and generation.get('eta_initial_min') > 0:
            return {'ok': True, 'eta_initial_min': generation['eta_initial_min'], 'kept': True}
        generation['eta_initial_min'] = minutes
        call(store.write, project_id, data)
    return {'ok': True, 'eta_initial_min': minutes, 'kept': False}

@router.post('/{project_id}/platforms')
def append_platforms(project_id: str, body: AppendPlatformsRequest, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    return call(jobs.append_platform_variants, project_id, body.platforms, body.branding.model_dump())  # 1.5.7: always refused, no attempt


def _variant_job(project_id: str, variant_id: str):
    state = call(store.read, project_id)
    variant = next((item for item in state.get('output_variants', []) if item['id'] == variant_id), None)
    if not variant:
        raise HTTPException(404, 'Export version not found')
    job = next((j for j in state['jobs'] if j['job_id'] == variant.get('render_job_id')), None)
    if not job or job['status'] != 'completed':
        raise HTTPException(409, 'Export not generated yet')
    return variant, job


@router.get('/{project_id}/output-variants/{variant_id}/cover')
def output_variant_cover(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    from backend.services.studio import publish_kit
    project_or_404(project_id, db)
    variant, job = _variant_job(project_id, variant_id)
    path, _ = publish_kit.cover_file(project_id, job['job_id'], variant['strategy_id'])
    if path is None:
        raise HTTPException(404, 'Cover not generated yet')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


@router.post('/{project_id}/output-variants/{variant_id}/cover/ai')
def redesign_output_variant_cover(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    from backend.services import cover
    project_or_404(project_id, db)
    _variant_job(project_id, variant_id)
    cfg = cover.load_config()
    if not (cfg.enabled and cfg.configured):
        raise HTTPException(409, 'Enable AI covers and choose an image model in Settings first')
    if not cfg.allow_send_frame:
        raise HTTPException(409, 'Allow sending reference frames in Settings first')
    job = call(jobs.request_ai_cover, project_id, variant_id)
    if job is None:
        raise HTTPException(409, 'Check your AI cover settings first')
    return job


@router.get('/{project_id}/output-variants/{variant_id}/cover/ai')
def output_variant_cover_job(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    _variant_job(project_id, variant_id)
    return call(jobs.ai_cover_status, project_id, variant_id)


@router.put('/{project_id}/output-variants/{variant_id}/post')
def update_output_variant_post(project_id: str, variant_id: str, body: PostCopy, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    return call(jobs.update_post, project_id, variant_id, body)


@router.get('/{project_id}/output-variants/{variant_id}/kit')
def output_variant_kit(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    from urllib.parse import quote
    from backend.services.platform_strategy import platform_strategy
    from backend.services.studio import publish_kit
    project_or_404(project_id, db)
    variant, job = _variant_job(project_id, variant_id)
    video = store.directory(project_id) / 'output' / 'studio' / f"{job['job_id']}.mp4"
    if not video.is_file():
        raise HTTPException(404, 'Export file has been removed')
    cover, _ = publish_kit.cover_file(project_id, job['job_id'], variant['strategy_id'])
    post = variant.get('post') or {'title': (job.get('result') or {}).get('title', '')}
    strategy = platform_strategy(variant['strategy_id'])
    path, name = publish_kit.kit_file(video, cover, post, strategy.label, english=strategy.audience_language == 'en')
    return FileResponse(path, media_type='application/zip', headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name)}"},
                        background=BackgroundTask(path.unlink, missing_ok=True))


@router.post('/{project_id}/output-variants/{variant_id}/produce')
def produce_output_variant(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    return call(jobs.produce_variant, project_id, variant_id)


@router.post('/{project_id}/output-variants/{variant_id}/retry')
def retry_output_variant(project_id: str, variant_id: str, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    return call(jobs.retry_variant, project_id, variant_id)


@router.post('/{project_id}/drafts')
def create_draft(project_id: str, body: CreateDraft, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    clips = db.query(Clip).filter(Clip.project_id == project_id, Clip.id.in_(body.clip_ids)).all()
    by_id = {str(c.id): c for c in clips}
    if any(cid not in by_id for cid in body.clip_ids):
        raise HTTPException(404, 'The selected clips do not belong to this project')
    scenes = [Scene(id=uuid.uuid4().hex, label=by_id[cid].title, start=by_id[cid].start_time, end=by_id[cid].end_time) for cid in body.clip_ids]
    project = project_or_404(project_id, db)
    prefs = (project.processing_config or {}).get('creative', {})
    draft = Draft(id=uuid.uuid4().hex, title=body.title, scenes=scenes, origin='legacy', language=prefs.get('language', 'source'), aspect=prefs.get('aspect') or 'original')
    call(validate_draft, project_id, draft)
    store.directory(project_id).mkdir(parents=True, exist_ok=True)
    import hashlib
    import json
    source_key = hashlib.sha256(json.dumps(body.clip_ids).encode()).hexdigest()
    return call(store.open_legacy_editor, project_id, draft, source_key, reuse_existing=body.reuse_existing)

@router.post('/{project_id}/events/{event_id}/draft')
def event_draft(project_id: str, event_id: str, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    prefs = Preferences.model_validate((project.processing_config or {}).get('creative', {}))
    event = next((e for e in store.read(project_id)['events'] if e['id'] == event_id), None)
    if not event:
        raise HTTPException(404, 'Clip not found')
    draft = Draft(id=uuid.uuid4().hex, title=event['label'], scenes=[Scene.model_validate(event)], aspect=prefs.aspect, layout='crop' if prefs.aspect == 'portrait' else 'fit', language=prefs.language, subtitles=False, origin='visual-highlight')
    call(validate_draft, project_id, draft)
    return call(store.save_draft, project_id, draft, create=True)

@router.put('/{project_id}/drafts/{draft_id}')
def save(project_id: str, draft_id: str, body: Draft, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    if draft_id != body.id:
        raise HTTPException(422, 'Draft ID mismatch')
    call(validate_draft, project_id, body)
    return call(store.save_draft, project_id, body)

@router.post('/{project_id}/drafts/{draft_id}/duplicate')
def duplicate(project_id: str, draft_id: str, body: DuplicateDraft, db: Session = Depends(get_db)):
    project = project_or_404(project_id, db)
    if draft_id != body.draft.id:
        raise HTTPException(422, 'Source draft ID mismatch')
    call(validate_draft, project_id, body.draft)
    result = call(store.duplicate_draft, project_id, body)
    project.processing_config = {**(project.processing_config or {}), 'studio_draft_count': len(store.read(project_id)['drafts'])}
    db.commit()
    return result

@router.post('/{project_id}/rewrite')
def rewrite(project_id: str, body: RewriteRequest, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    if not any(d['id'] == body.draft.id for d in store.read(project_id)['drafts']):
        raise HTTPException(404, '草稿不存在')
    try:
        result = intelligence.text_json('你是剪辑文案编辑。按用户要求优化 title 和 hook，保持事实；依据仅限原文与镜头证据。不能声称已修改镜头、声音或视频，也不能承诺投放效果。返回 {"title":"...","hook":"..."}。', {'instruction': body.instruction, 'language': body.draft.language, 'title': body.draft.title, 'hook': body.draft.hook, 'scenes': [s.model_dump() for s in body.draft.scenes]})
        # (11:09): the model's own reply failed our checks -> MODEL / invalid_response, no raw text.
        # only the reply's own shape is judged (not a dict, a missing key, a field the schema
        # rejects); a TypeError from our own code is not caught here and stays local/unexpected.
        if not isinstance(result, dict):
            raise ModelResponseInvalid('The model returned an invalid rewritten copy format; please try again')
        try:
            candidate = Draft.model_validate({**body.draft.model_dump(), 'title': result['title'], 'hook': result['hook']})
        except (KeyError, ValueError):  # pydantic's ValidationError is a ValueError
            raise ModelResponseInvalid('The model returned an invalid rewritten copy format; please try again') from None
    except Exception as error:
        capture_studio_exception(error, 'rewrite')
        raise HTTPException(502, 'Failed to generate copy; check model settings and try again. The original is unchanged') from None
    return candidate

@router.get('/{project_id}/subtitles')
def subtitles(project_id: str, db: Session = Depends(get_db)):
    """Subtitle cues in seconds, for the editor's styled preview overlay."""
    from backend.pipeline.quality import to_seconds
    from backend.services.publish_export import _load_srt_entries
    project_or_404(project_id, db)
    entries = _load_srt_entries(project_id)
    return {'cues': [{'start': to_seconds(e['start_time']), 'end': to_seconds(e['end_time']), 'text': e.get('text', '')} for e in entries]}

@router.get('/framing/status')
def framing_status():
    from backend.services.studio import framing
    return framing.get_status()

@router.post('/framing/install')
def framing_install():
    from backend.services.studio import framing
    return {**framing.start_install(manual=True), **framing.get_status()}

@router.post('/{project_id}/auto-frame')
def auto_frame(project_id: str, body: Draft, db: Session = Depends(get_db)):
    """Centre each scene's crop window on the speaker. Pure analysis: nothing is saved."""
    from backend.services.publish_export import _probe
    from backend.services.studio import framing
    project_or_404(project_id, db)
    if not framing.is_installed():
        raise HTTPException(409, framing.unavailable_reason())
    video = call(jobs.source, project_id)
    info = _probe(video)
    if not info.get('width') or not info.get('height'):
        raise HTTPException(422, 'Unable to read source video dimensions')
    try:
        return framing.auto_frame(video, body, int(info['width']), int(info['height']))
    except Exception as error:
        capture_studio_exception(error, 'auto_frame')
        raise HTTPException(502, 'Auto-framing failed; you can adjust the framing manually') from None

@router.post('/{project_id}/drafts/{draft_id}/export')
def export(project_id: str, draft_id: str, body: ExportDraftRequest | None = None, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    state = store.read(project_id)
    raw = next((d for d in state['drafts'] if d['id'] == draft_id), None)
    if not raw:
        raise HTTPException(404, '草稿不存在')
    if body and body.revision != raw['revision']:
        raise HTTPException(409, 'The draft was updated in another window; reload before exporting')
    draft = Draft.model_validate(raw)
    call(validate_draft, project_id, draft)
    variant = jobs.export_variant(
        state.get('output_variants', []), draft_id,
        variant_id=body.variant_id if body else None,
        revision=draft.revision, aspect=draft.aspect,
    )
    if variant:
        return call(jobs.export, project_id, draft, brand_outro=bool(variant.get('branding', {}).get('outro_enabled', True)), **jobs.template_export_kwargs(variant))
    return call(jobs.export, project_id, draft)

@router.get('/{project_id}/exports/{job_id}/video')
def rendered_video(project_id: str, job_id: str, download: bool = False, db: Session = Depends(get_db)):
    project_or_404(project_id, db)
    job = next((j for j in store.read(project_id)['jobs'] if j['job_id'] == job_id), None)
    if not job:
        raise HTTPException(404, 'Export not found')
    if job['status'] != 'completed':
        raise HTTPException(409, 'Export not finished yet')
    path = store.directory(project_id) / 'output' / 'studio' / f'{job_id}.mp4'
    if not path.is_file():
        raise HTTPException(404, 'Export file has been removed')
    return FileResponse(path, media_type='video/mp4', filename=path.name if download else None)
