"""
发布到海外平台（Upload-Post）的 API。

    GET  /publish/upload-post/config                       当前配置（key 打码）
    PUT  /publish/upload-post/config                       保存 key / 默认 profile（保存前先校验 key）
    DELETE /publish/upload-post/config                     删除本地保存的配置
    GET  /publish/upload-post/profiles                     API Key 下的 profile 与已连接平台
    POST /publish/upload-post/{project_id}/clips/{clip_id} 导出 + 提交发布（后台任务，返回 job_id）
    GET  /publish/upload-post/jobs/{job_id}                任务状态；已提交的附带各平台结果（remote）
    GET  /publish/upload-post/requests/{request_id}        直接按 request_id 查 Upload-Post
    GET  /publish/upload-post/{project_id}/records         这个项目提交过的发布记录

桌面 / Docker / 脚本模式都可用（不依赖 check_desktop_mode）。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend.services import bilibili_publisher as bili
from backend.services import cover as cover_svc
from backend.services import upload_post_publisher as up

router = APIRouter(prefix="/publish", tags=["publish"])


class UploadPostConfigBody(BaseModel):
    api_key: str | None = Field(None, description="Upload-Post API Key; if omitted, the existing one is kept")
    user: str | None = Field(None, description="Default profile (the user in Upload-Post)")


class BilibiliConfigBody(BaseModel):
    cookie: str = Field(..., description="Cookie from the browser request headers; must include SESSDATA, bili_jct and DedeUserID")


class BilibiliPublishBody(BaseModel):
    output_variant_id: str | None = Field(None, pattern=r'^[a-zA-Z0-9_-]{1,100}$', description="A completed Bilibili auto-produced version; when provided, no re-export is done")
    title: str | None = None
    description: str | None = None
    subtitles: bool = True
    title_card: bool = True
    scheduled_date: str | None = None
    timezone: str | None = None
    visibility: str = Field("private", description="private = only me / public = everyone")


class PublishBody(BaseModel):
    platforms: list[str] = Field(..., description="tiktok / instagram / youtube / facebook / linkedin / x / threads / pinterest / bluesky …")
    user: str | None = Field(None, description="Upload-Post profile; if omitted, the default from the config is used")
    preset: str | None = Field(None, description="Export preset for publishing; chosen automatically by platform if omitted (shorts for portrait platforms, otherwise original)")
    output_variant_id: str | None = Field(None, pattern=r'^[a-zA-Z0-9_-]{1,100}$', description="A completed auto-produced platform version; when provided, no re-export is done")
    title: str | None = Field(None, description="Title; if omitted, the clip title is used")
    description: str | None = Field(None, description="Description for YouTube / LinkedIn / Facebook / Pinterest")
    subtitles: bool = True
    title_card: bool = True
    scheduled_date: str | None = Field(None, description="ISO-8601 scheduled publish time")
    timezone: str | None = Field(None, description="IANA time zone, used with scheduled_date")
    extra: dict[str, Any] = Field(default_factory=dict, description="Pass-through platform-specific fields, e.g. privacy_level, privacyStatus, facebook_page_id, pinterest_board_id")


class CoverConfigBody(BaseModel):
    enabled: bool | None = None
    provider: str | None = Field(None, description="openai / seedream / dashscope")
    model: str | None = None
    api_key: str | None = Field(None, description="If omitted, the existing key is kept")
    base_url: str | None = None
    ocr_model: str | None = None
    allow_send_frame: bool | None = Field(None, description="Whether video frames may be sent to a third-party image generation service; off by default")
    mode: str | None = Field(None, description="text_model = follow the provider of the AI model / custom = configure separately")


class CoverGenerateBody(BaseModel):
    platform: str = Field("bilibili", description="bilibili = landscape / douyin = portrait")
    title: str | None = None
    subtitle: str | None = None
    badge: str | None = None
    content_type: str | None = Field(None, description="knowledge / interview / game / vlog / general")
    force_local: bool = False
    sync: bool = Field(False, description="When True, generate synchronously (for testing); defaults to a background task")


def _bili_error(exc: Exception) -> HTTPException:
    if isinstance(exc, bili.BilibiliError):
        code = exc.status_code if exc.status_code in (400, 401, 404) else 400
        return HTTPException(status_code=code, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _http_error(e: Exception) -> HTTPException:
    if isinstance(e, up.UploadPostError):
        code = e.status_code if e.status_code in (401, 402, 403, 404, 409, 422, 429) else 400
        return HTTPException(status_code=code, detail=str(e))
    if isinstance(e, (ValueError, FileNotFoundError)):
        return HTTPException(status_code=400, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@router.get("/upload-post/config")
async def get_upload_post_config():
    cfg = up.load_config()
    return {
        "configured": cfg.configured,
        "source": cfg.source,
        "api_key_masked": cfg.masked_key(),
        "user": cfg.user,
        "base_url": cfg.base_url,
        "platforms": up.PLATFORMS,
    }


@router.put("/upload-post/config")
async def put_upload_post_config(body: UploadPostConfigBody):
    if body.api_key is None and body.user is None:
        raise HTTPException(status_code=400, detail="At least one of api_key or user is required")
    if body.api_key is not None:
        key = body.api_key.strip()
        if not key:
            raise HTTPException(status_code=400, detail="api_key must not be empty")
        try:
            verified = up.verify_api_key(up.UploadPostConfig(api_key=key, base_url=up.load_config().base_url))
        except up.UploadPostError as e:
            raise _http_error(e)
    else:
        verified = None
    cfg = up.save_config(api_key=body.api_key, user=body.user)
    if cfg.source == "env":
        # 环境变量优先于文件：文件写了但不会生效，如实告诉调用方
        note = "Saved to file, but the current process is using the UPLOAD_POST_API_KEY environment variable"
    else:
        note = None
    return {"ok": True, "configured": cfg.configured, "source": cfg.source, "api_key_masked": cfg.masked_key(),
            "user": cfg.user, "account": verified, "note": note}


@router.delete("/upload-post/config")
async def delete_upload_post_config():
    up.clear_config()
    return {"ok": True, "configured": up.load_config().configured}


@router.get("/upload-post/profiles")
async def get_upload_post_profiles():
    try:
        return {"profiles": up.list_profiles()}
    except Exception as e:  # noqa: BLE001
        raise _http_error(e)


@router.post("/upload-post/{project_id}/clips/{clip_id}")
async def start_upload_post_publish(project_id: str, clip_id: str, body: PublishBody):
    from backend.services.publish_export import PRESETS, load_clip_meta
    try:
        load_clip_meta(project_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        platforms = up.normalize_platforms(body.platforms)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if body.preset and body.preset not in PRESETS:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {body.preset} (options: {', '.join(PRESETS)})")
    cfg = up.load_config()
    if not cfg.configured:
        raise HTTPException(status_code=400, detail="Upload-Post API Key is not configured (PUT /publish/upload-post/config or set the UPLOAD_POST_API_KEY environment variable)")
    if not (body.user or cfg.user):
        raise HTTPException(status_code=400, detail="No Upload-Post profile (user) specified")
    return up.start_publish(up.PublishRequest(
        project_id=project_id, clip_id=clip_id, platforms=platforms, user=body.user, preset=body.preset, output_variant_id=body.output_variant_id,
        title=body.title, description=body.description, subtitles=body.subtitles, title_card=body.title_card,
        scheduled_date=body.scheduled_date, timezone=body.timezone, extra=body.extra,
    ))


@router.get("/upload-post/jobs/{job_id}")
async def get_upload_post_job(job_id: str):
    job = up.get_publish_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Publish job not found")
    return job


@router.get("/upload-post/requests/{request_id}")
async def get_upload_post_request(request_id: str, project_id: str | None = None):
    try:
        return up.get_status(request_id, project_id=project_id)
    except Exception as e:  # noqa: BLE001
        raise _http_error(e)


@router.get("/upload-post/{project_id}/records")
async def list_upload_post_records(project_id: str):
    return {"records": up.list_records(project_id)}


@router.delete("/upload-post/{project_id}/records/{request_id}")
async def cancel_upload_post_record(project_id: str, request_id: str):
    try:
        return up.cancel_record(project_id, request_id)
    except bili.BilibiliError as e:
        raise _bili_error(e)
    except Exception as e:  # noqa: BLE001
        raise _http_error(e)


@router.get("/bilibili/config")
async def get_bilibili_config():
    return bili.load_config().public()


@router.put("/bilibili/config")
async def put_bilibili_config(body: BilibiliConfigBody):
    cookie = body.cookie.strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="Cookie must not be empty")
    try:
        account = bili.verify_cookie(cookie)
    except bili.BilibiliError as e:
        raise _bili_error(e)
    cfg = bili.save_config(account["cookie"], account["nickname"], account["uid"])
    payload = {"ok": True, **cfg.public()}
    if cfg.source == "env":
        payload["note"] = "Saved to file, but the current process is using the BILIBILI_COOKIE environment variable"
    return payload


@router.delete("/bilibili/config")
async def delete_bilibili_config():
    bili.clear_config()
    return {"ok": True, **bili.load_config().public()}


@router.post("/bilibili/{project_id}/clips/{clip_id}")
async def start_bilibili_publish(project_id: str, clip_id: str, body: BilibiliPublishBody):
    from backend.services.publish_export import load_clip_meta
    try:
        load_clip_meta(project_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if body.visibility not in ("private", "public"):
        raise HTTPException(status_code=400, detail="Visibility must be either private or public")
    if not bili.load_config().configured:
        raise HTTPException(status_code=400, detail="Bilibili account is not configured yet")
    return bili.start_publish(bili.BilibiliPublishRequest(
        project_id=project_id,
        clip_id=clip_id,
        output_variant_id=body.output_variant_id,
        title=body.title,
        description=body.description,
        subtitles=body.subtitles,
        title_card=body.title_card,
        scheduled_date=body.scheduled_date,
        timezone=body.timezone,
        visibility=body.visibility,
    ))


@router.get("/bilibili/jobs/{job_id}")
async def get_bilibili_job(job_id: str):
    job = bili.get_publish_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Publish job not found")
    return job


def _cover_error(exc: Exception) -> HTTPException:
    if isinstance(exc, cover_svc.CoverError):
        code = exc.status_code if exc.status_code in (400, 401, 404) else 400
        return HTTPException(status_code=code, detail=str(exc))
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/covers/config")
async def get_cover_config():
    return cover_svc.load_config().public()


@router.put("/covers/config")
async def put_cover_config(body: CoverConfigBody):
    if body.mode is not None and body.mode not in ("text_model", "custom"):
        raise HTTPException(status_code=400, detail="mode must be text_model or custom")
    if body.mode != "text_model" and body.provider is not None and body.provider.strip().lower() not in ("openai", "seedream", "dashscope"):
        raise HTTPException(status_code=400, detail="Image generation provider must be openai, seedream or dashscope")
    cfg = cover_svc.save_config(
        enabled=body.enabled,
        provider=body.provider,
        model=body.model,
        api_key=body.api_key,
        base_url=body.base_url,
        ocr_model=body.ocr_model,
        allow_send_frame=body.allow_send_frame,
        mode=body.mode,
    )
    payload = {"ok": True, **cfg.public()}
    if cfg.source == "env":
        payload["note"] = "The key comes from the IMAGE_API_KEY environment variable; changes here will not override it."
    return payload


@router.delete("/covers/config")
async def delete_cover_config():
    return {"ok": True, **cover_svc.clear_config().public()}


@router.get("/covers/{project_id}/clips/{clip_id}")
async def get_clip_cover(project_id: str, clip_id: str, platform: str = "bilibili"):
    from backend.services.publish_export import load_clip_meta
    try:
        load_clip_meta(project_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    found = cover_svc.get_cover(project_id, clip_id, platform)
    if not found:
        return {"ok": False, "platform": cover_svc.normalize_platform(platform), "url": None}
    return found


@router.get("/covers/{project_id}/clips/{clip_id}/file")
async def get_clip_cover_file(project_id: str, clip_id: str, platform: str = "bilibili"):
    platform = cover_svc.normalize_platform(platform)
    path = cover_svc.cover_path(project_id, clip_id, platform)
    if not path.exists() or path.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="This cover does not exist yet")
    return FileResponse(path, media_type="image/jpeg", filename=f"{clip_id}_{platform}.jpg")


@router.post("/covers/{project_id}/clips/{clip_id}")
async def start_cover_generate(project_id: str, clip_id: str, body: CoverGenerateBody):
    from backend.services.publish_export import load_clip_meta
    try:
        load_clip_meta(project_id, clip_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    platform = cover_svc.normalize_platform(body.platform)
    if body.sync:
        try:
            return cover_svc.generate_cover(
                project_id=project_id,
                clip_id=clip_id,
                platform=platform,
                title=body.title,
                subtitle=body.subtitle,
                badge=body.badge,
                content_type=body.content_type,
                force_local=body.force_local,
            )
        except Exception as e:  # noqa: BLE001
            raise _cover_error(e)
    return cover_svc.start_generate(
        project_id=project_id,
        clip_id=clip_id,
        platform=platform,
        title=body.title,
        subtitle=body.subtitle,
        badge=body.badge,
        content_type=body.content_type,
        force_local=body.force_local,
    )


@router.get("/covers/jobs/{job_id}")
async def get_cover_job(job_id: str):
    job = cover_svc.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Cover job not found")
    return job


@router.post("/covers/jobs/{job_id}/cancel")
async def cancel_cover_job(job_id: str):
    return cover_svc.cancel_job(job_id)


# --------------------------------------------------------------------------- YouTube (direct, own Google Cloud project)
from fastapi import Request  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from backend.services import youtube_publisher as yt  # noqa: E402


class YouTubeConfigBody(BaseModel):
    client_id: str | None = Field(default=None, max_length=300)
    client_secret: str | None = Field(default=None, max_length=300)
    privacy: str | None = None
    auto_post: bool | None = None
    strategies: list[str] | None = Field(default=None, max_length=8)


class YouTubePublishBody(BaseModel):
    privacy: str | None = None


REDIRECT_URI = "http://localhost:3000/api/v1/publish/youtube/callback"


def _yt_redirect_uri(request: Request) -> str:
    # Always the one address registered in the Google client (Google only accepts http for localhost). Someone signing in
    # on another device lands on a page that does not load there; they send the address from the bar back (see /youtube/complete).
    return REDIRECT_URI


def _yt_http_error(error: Exception) -> HTTPException:
    if isinstance(error, yt.YouTubeError):
        return HTTPException(status_code=error.status_code if error.status_code in (400, 401, 403, 404, 409, 429) else 400, detail=str(error))
    if isinstance(error, (ValueError, FileNotFoundError)):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail="The YouTube request failed")


def _yt_view(request: Request) -> dict[str, Any]:
    cfg = yt.load_config()
    return {"has_client": cfg.has_client, "client_id": cfg.client_id, "connected": cfg.connected, "channel_title": cfg.channel_title,
            "privacy": cfg.privacy, "auto_post": cfg.auto_post, "strategies": cfg.strategies, "redirect_uri": _yt_redirect_uri(request)}


@router.get("/youtube/config")
async def get_youtube_config(request: Request):
    return _yt_view(request)


@router.put("/youtube/config")
async def put_youtube_config(body: YouTubeConfigBody, request: Request):
    try:
        yt.save_settings(client_id=body.client_id, client_secret=body.client_secret, privacy=body.privacy,
                         auto_post=body.auto_post, strategies=body.strategies)
    except Exception as error:  # noqa: BLE001
        raise _yt_http_error(error)
    return _yt_view(request)


@router.delete("/youtube/config")
async def delete_youtube_connection(request: Request):
    yt.disconnect()
    return _yt_view(request)


@router.get("/youtube/auth-url")
async def get_youtube_auth_url(request: Request):
    try:
        return {"url": yt.auth_url(_yt_redirect_uri(request))}
    except Exception as error:  # noqa: BLE001
        raise _yt_http_error(error)

class YouTubeCompleteBody(BaseModel):
    url: str = Field(min_length=10, max_length=4000)


@router.post("/youtube/complete")
async def complete_youtube_from_address(body: YouTubeCompleteBody, request: Request):
    """Finish the sign-in from the address Google redirected to (copied from the browser bar of whoever signed in)."""
    from urllib.parse import parse_qs, urlparse
    query = parse_qs(urlparse(body.url.strip()).query)
    code, state = (query.get("code") or [""])[0], (query.get("state") or [""])[0]
    if not code or not state:
        raise HTTPException(status_code=400, detail="That address has no sign-in code. Copy the whole address from the browser bar after signing in.")
    try:
        await run_in_threadpool(yt.complete_auth, code, state, _yt_redirect_uri(request))
    except Exception as error:  # noqa: BLE001
        raise _yt_http_error(error)
    return _yt_view(request)
@router.get("/youtube/callback")
async def youtube_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Google sends the browser back here. Show the result in the app's settings page."""
    if error or not code:
        return RedirectResponse(f"/#/settings?section=publish&youtube=denied", status_code=302)
    try:
        await run_in_threadpool(yt.complete_auth, code, state, _yt_redirect_uri(request))
    except Exception:  # noqa: BLE001
        return RedirectResponse("/#/settings?section=publish&youtube=failed", status_code=302)
    return RedirectResponse("/#/settings?section=publish&youtube=connected", status_code=302)


@router.post("/youtube/{project_id}/variants/{variant_id}")
async def publish_youtube_variant(project_id: str, variant_id: str, body: YouTubePublishBody):
    try:
        return await run_in_threadpool(yt.publish_variant, project_id, variant_id, privacy=body.privacy)
    except Exception as error:  # noqa: BLE001
        raise _yt_http_error(error)


@router.get("/youtube/{project_id}/records")
async def list_youtube_records(project_id: str):
    return {"records": yt.read_records(project_id)}
