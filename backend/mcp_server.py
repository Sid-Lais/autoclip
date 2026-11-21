"""
AutoClip MCP server（stdio）——让 Cursor / Claude Code / 任何 MCP 客户端直接调 AutoClip 出片。

启动：
    autoclip mcp                       # 装了包
    python -m backend.mcp_server       # 仓库内

客户端配置示例（Cursor `~/.cursor/mcp.json` / Claude `claude mcp add` / opencode）：
    { "mcpServers": { "autoclip": { "command": "autoclip", "args": ["mcp"] } } }
    或 { "command": "/path/to/autoclip/venv/bin/python", "args": ["-m", "backend.mcp_server"],
         "env": { "PYTHONPATH": "/path/to/autoclip" } }
    opencode（opencode.json；一条命令：`autoclip mcp install opencode`，见 docs/OPENCODE.md）：
    { "mcp": { "autoclip": { "type": "local", "command": ["autoclip", "mcp"], "enabled": true } } }

工具：
    clip_video          同步出片（几分钟到几十分钟，带进度通知）
    start_clip_job      后台出片，立刻返回 project_id
    get_job_status      查进度 / 拿结果
    get_project         已完成项目的切片、合集与文件路径
    list_projects       最近项目
    list_providers      可用模型提供商与本地预设（ollama / lmstudio）
    check_environment   ffmpeg / Whisper / 模型连接体检
    export_clip         渲可发布成片（9:16 / 字幕 / 标题卡）
    publish_clip        经 Upload-Post 发到 TikTok / Instagram / YouTube Shorts 等海外平台
    get_publish_status  查各平台发布结果
    list_publish_profiles  Upload-Post 里可用的 profile 与已连接平台

依赖 `mcp` Python SDK（requirements.txt 已含；兼容 1.x FastMCP 与 2.x MCPServer）。
"""
from __future__ import annotations

import asyncio
import logging
import inspect
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from backend import __version__

from backend.services.local_runner import (
    LLMOverride,
    RunRequest,
    configure_environment,
    setup_logging,
)

logger = logging.getLogger(__name__)

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server, Context  # type: ignore
except ImportError:  # mcp 1.x
    try:
        from mcp.server.fastmcp import FastMCP as _Server, Context  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise SystemExit("Missing mcp SDK: pip install mcp") from e

INSTRUCTIONS = """AutoClip 1.5 automatically turns long videos into videos, covers, post copy and ZIP publish kits tailored to the target platforms.
Prefer start_quick_output + get_quick_output_status; platforms supports douyin, xiaohongshu, bilibili, tiktok, reels, shorts, youtube_long.
It shares the desktop app's model and outro settings; subtitles are transcribed automatically when missing. When done, use export_kits=true to get the publish kit.
When the old raw clips / collections are needed, the user gives a local video path → call clip_video (or start_clip_job + poll get_job_status) →
then present the returned clip list (title / time range / score / file path) to the user.
Without subtitles, local Whisper transcribes the video; the first run is slower. To save money or work offline: provider="ollama" (requires Ollama installed locally).
Zero clips usually means the score threshold is too high; retry with min_score=0.5."""

server = _Server(
    name="autoclip",
    instructions=INSTRUCTIONS,
    **({'version': __version__} if 'version' in inspect.signature(_Server).parameters else {}),
)


@server.tool(name='get_version', description='Read the AutoClip CLI / MCP version, to check a colleague\'s test environment.')
def get_version() -> Dict[str, Any]:
    return {'version': __version__}


@server.tool(name='start_quick_output', description='One-click output: takes a local video or an HTTPS YouTube / Bilibili link and automatically produces videos, covers and post copy for the given platforms. template can be editorial / street / news / cinema / classic, and is only accepted when mcp_v2_tools is on; when it is off, passing a style returns disabled. The returned template is the style actually used, and requested_template is the requested value. Shares the desktop AI and outro settings and returns project_id immediately; query it afterwards with get_quick_output_status.')
def start_quick_output(source: str, platforms: Optional[List[str]] = None, name: Optional[str] = None,
                       srt_path: Optional[str] = None, instruction: str = '', browser: Optional[str] = None,
                       portrait_style: str = 'auto', template: Optional[str] = None) -> Dict[str, Any]:
    from backend.services import mcp_jobs
    from backend.services import quick_output_runner as quick
    from backend.services.studio.template_choice import style_for_request
    try:
        project_id = quick.start(source, platforms or ['douyin'], name=name, srt_path=srt_path, instruction=instruction,
                                 browser=browser, portrait_style=portrait_style, template=template)
    except quick.StyleDisabled as error:
        return {'ok': False, 'error_code': 'disabled', 'error': str(error),
                **mcp_jobs.progress_fields(status='failed')}
    except (ValueError, FileNotFoundError) as error:
        return {'ok': False, 'error_code': 'invalid_input', 'error': str(error),
                **mcp_jobs.progress_fields(status='failed')}
    mcp_jobs.write_job(project_id, status='running', progress=0, stage='screening', kind='quick', error_code='none')
    record = mcp_jobs.read_job(project_id) or {}
    return {'ok': True, 'version': __version__, 'project_id': project_id,
            **style_for_request(template),
            **mcp_jobs.progress_fields(status='running', progress=0, stage='screening', started_at=record.get('started_at'))}


@server.tool(name='get_quick_output_status', description='Query the status of a one-click output. Returns progress, stage, eta, poll_after_sec, and each platform\'s video, cover and post copy. When complete, use export_kits=true to get the publish kit ZIP path. A job that cannot be found is unknown; a job whose process is gone is interrupted. This tool does not call a model or start production.')
def get_quick_output_status(project_id: str, export_kits: bool = False) -> Dict[str, Any]:
    from backend.services import mcp_jobs
    from backend.services import quick_output_runner as quick
    if not mcp_jobs.valid_id(project_id):
        return mcp_jobs.lookup(project_id)
    try:
        result = quick.status(project_id, export_kits=export_kits)
    except FileNotFoundError:
        return mcp_jobs.lookup(project_id)
    except ValueError as error:
        return {'ok': False, 'project_id': project_id, 'error_code': 'invalid_input', 'error': str(error),
                **mcp_jobs.progress_fields(status='unknown')}
    view = mcp_jobs.progress_fields(status=result.get('status') or 'unknown', progress=result.get('progress'),
                                    stage=result.get('stage') or result.get('phase'))
    view.update(result)
    return {'ok': result.get('status') not in ('failed', 'unknown', 'interrupted', 'cancelled'), **view}

# ---------------------------------------------------------------- job registry ---
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_pipeline_lock = threading.Lock()  # 全局 LLM 配置是进程级的，任务串行跑


def _job_update(project_id: str, **fields: Any) -> None:
    with _jobs_lock:
        _jobs.setdefault(project_id, {})
        _jobs[project_id].update(fields)
        snapshot = dict(_jobs[project_id])
    from backend.services import mcp_jobs
    mcp_jobs.write_job(
        project_id,
        status=snapshot.get('status'),
        progress=snapshot.get('percent', snapshot.get('progress')),
        stage=snapshot.get('stage'),
        kind=snapshot.get('kind') or 'clip',
        result=snapshot.get('result'),
        error_code=snapshot.get('error_code'),
    )


def _run_job(req: RunRequest, override: LLMOverride, link: bool) -> Dict[str, Any]:
    """阻塞执行；在线程里调用。"""
    from backend.services.local_runner import configure_llm, prepare_project, run_pipeline, summarize_project
    from backend.services.studio.readiness import TranscriptionNotReady
    from backend.services.studio.preflight import SourceRejected

    with _pipeline_lock:
        try:
            info = configure_llm(override)
            video_in_raw = prepare_project(req, link=link)
            _job_update(req.project_id, status="running", llm=info, percent=0, stage="INGEST", message="Starting")

            def on_progress(p: Dict[str, Any]) -> None:
                _job_update(req.project_id, percent=p.get("percent", 0), stage=p.get("stage"), message=p.get("message"))

            result = run_pipeline(req, video_in_raw, on_progress=on_progress)
            if result.get("status") != "succeeded":
                from backend.services.mcp_telemetry import normalize_error
                # keep a source failure's own code (source_unreadable...), never fold it into "failed".
                _job_update(req.project_id, status="failed", error_code=normalize_error(result.get("error_code"), ok=False),
                            error=result.get("error") or "处理失败")
                return _jobs[req.project_id]
            summary = summarize_project(req.project_id)
            _job_update(req.project_id, status="completed", percent=100, stage="DONE", message="完成",
                        error_code="none", result=summary)
            return _jobs[req.project_id]
        except SourceRejected as e:  # #284: the source's own code, never folded into invalid_input / failed
            _job_update(req.project_id, status="failed", error_code=e.code, error=str(e)[:500])
            return _jobs[req.project_id]
        except TranscriptionNotReady as e:  # only the 1.5.7 gate; other ValueErrors are product failures
            _job_update(req.project_id, status="failed", error_code="invalid_input", error=str(e)[:500])
            return _jobs[req.project_id]
        except Exception as e:  # noqa: BLE001
            logger.exception("MCP 任务失败")
            _job_update(req.project_id, status="failed", error_code="failed", error=str(e)[:500])
            return _jobs[req.project_id]


def _make_request(video_path: str, srt_path: Optional[str], name: Optional[str], category: str,
                  min_score: Optional[float]) -> RunRequest:
    return RunRequest(
        video=Path(video_path),
        srt=Path(srt_path) if srt_path else None,
        name=name,
        category=category or "default",
        min_score=min_score,
    )


def _make_override(provider: Optional[str], model: Optional[str], base_url: Optional[str]) -> LLMOverride:
    # The key stays in the environment. It is not a tool argument, so it is not copied into the agent transcript.
    return LLMOverride(provider=provider, model=model, base_url=base_url, api_key=os.getenv("AUTOCLIP_API_KEY"))


# ---------------------------------------------------------------- tools ---
@server.tool(
    name="clip_video",
    description=(
        "Cut a local video into highlight clips (synchronous; takes minutes to tens of minutes, sending progress notifications meanwhile). "
        "Returns the clip list (title / start-end time / score / mp4 path) and collections. "
        "provider can be dashscope / openai / gemini / deepseek / seed / kimi / glm / grok / infistar / api88 / apimart / ollama / lmstudio; if omitted, the model configured in the desktop app is used."
    ),
)
async def clip_video(
    video_path: str,
    ctx: Context,
    srt_path: Optional[str] = None,
    name: Optional[str] = None,
    category: str = "default",
    min_score: Optional[float] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    req = _make_request(video_path, srt_path, name, category, min_score)
    override = _make_override(provider, model, base_url)
    _job_update(req.project_id, status="queued", percent=0, video=str(req.video))

    task = asyncio.create_task(asyncio.to_thread(_run_job, req, override, True))
    last_percent = -1
    while not task.done():
        await asyncio.sleep(1.0)
        job = _jobs.get(req.project_id, {})
        pct = int(job.get("percent") or 0)
        if pct != last_percent:
            last_percent = pct
            try:
                await ctx.report_progress(pct, 100, f"{job.get('stage', '')} {job.get('message', '')}".strip())
            except Exception:  # noqa: BLE001
                pass
    job = task.result()
    if job.get("status") != "completed":
        return {"ok": False, "project_id": req.project_id, "error_code": job.get("error_code") or "failed",
                "error": job.get("error", "处理失败")}
    return {"ok": True, **job["result"], "llm": job.get("llm")}


@server.tool(
    name="start_clip_job",
    description="Start a clip job in the background and return project_id immediately; then poll progress and results with get_job_status. Suited to clients that put a timeout on a single tool call.",
)
def start_clip_job(
    video_path: str,
    srt_path: Optional[str] = None,
    name: Optional[str] = None,
    category: str = "default",
    min_score: Optional[float] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    from backend.services import mcp_jobs
    req = _make_request(video_path, srt_path, name, category, min_score)
    if not req.video.expanduser().exists():
        return {"ok": False, "error_code": "invalid_input", "error": f"Video not found: {req.video}",
                **mcp_jobs.progress_fields(status="failed")}
    override = _make_override(provider, model, base_url)
    _job_update(req.project_id, status="queued", percent=0, stage="queued", video=str(req.video))
    record = mcp_jobs.read_job(req.project_id) or {}
    t = threading.Thread(target=_run_job, args=(req, override, True), daemon=True, name=f"autoclip-{req.project_id[:8]}")
    t.start()
    return {"ok": True, "project_id": req.project_id, "hint": "Poll with get_job_status at the interval given by poll_after_sec.",
            **mcp_jobs.progress_fields(status="queued", progress=0, stage="queued", started_at=record.get("started_at"))}


@server.tool(name="get_job_status", description="Query a job started by start_clip_job. Returns progress, stage, eta, poll_after_sec. No record means unknown; a missing worker process means interrupted. A project that cannot be found is never reported as completed.")
def get_job_status(project_id: str) -> Dict[str, Any]:
    from backend.services import mcp_jobs
    return mcp_jobs.lookup(project_id)


@server.tool(name="get_project", description="Read a processed project's clips (title / time / score / file), collections and output directory.")
def get_project(project_id: str) -> Dict[str, Any]:
    from backend.services.local_runner import summarize_project

    try:
        return {"ok": True, **summarize_project(project_id)}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


@server.tool(name="list_projects", description="List recent AutoClip projects (shares the data directory with the desktop app).")
def list_projects(limit: int = 20) -> List[Dict[str, Any]]:
    from backend.services.local_runner import list_projects as _list

    return _list(limit=limit)


@server.tool(name="list_providers", description="Available model providers and local presets (default address and model for ollama / lmstudio), plus the configuration currently in use.")
def list_providers() -> Dict[str, Any]:
    from backend.core.llm_manager import get_llm_manager
    from backend.core.local_presets import presets_as_dicts

    return {
        "current": get_llm_manager().get_current_provider_info(),
        "cloud": ["dashscope", "openai", "gemini", "deepseek", "seed", "kimi", "glm", "grok", "infistar", "api88", "apimart"],
        "local_presets": presets_as_dicts(),
    }


@server.tool(
    name="export_clip",
    description=(
        "Render an already-cut clip into a ready-to-publish video: 9:16 (Douyin/Xiaohongshu/Shorts), burned-in subtitles, title card. "
        "preset: douyin / xiaohongshu / shorts / bilibili / original. "
        "Returns the video path; exporting again with the same parameters hits the cache."
    ),
)
def export_clip(
    project_id: str,
    clip_id: str,
    preset: str = "douyin",
    subtitles: bool = True,
    title_card: bool = True,
) -> Dict[str, Any]:
    from backend.services.publish_export import ExportRequest, export_clip as _export
    try:
        return _export(ExportRequest(project_id, clip_id, preset, subtitles, title_card))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:500]}


@server.tool(
    name="publish_clip",
    description=(
        "Publish a clip to overseas platforms via Upload-Post (tiktok / instagram / youtube / facebook / linkedin / x / threads / "
        "pinterest / bluesky …); several platforms can be targeted at once. It first renders the video from the preset (vertical-video platforms default to shorts 9:16), then submits asynchronously; "
        "returns request_id; use get_publish_status to check each platform's result. An Upload-Post API Key must be configured first "
        "(env var UPLOAD_POST_API_KEY, or autoclip publish --api-key … --save) along with a profile (user). "
        "extra can pass through platform fields, such as {\"privacy_level\": \"SELF_ONLY\", \"privacyStatus\": \"unlisted\"}; "
        "scheduled_date (ISO-8601) schedules the post."
    ),
)
def publish_clip(
    project_id: str,
    clip_id: str,
    platforms: List[str],
    user: Optional[str] = None,
    preset: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    subtitles: bool = True,
    title_card: bool = True,
    scheduled_date: Optional[str] = None,
    timezone: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    from backend.services import upload_post_publisher as up

    try:
        return up.publish_clip(up.PublishRequest(
            project_id=project_id, clip_id=clip_id, platforms=platforms, user=user, preset=preset,
            title=title, description=description, subtitles=subtitles, title_card=title_card,
            scheduled_date=scheduled_date, timezone=timezone, extra=extra or {},
        ))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:500]}


@server.tool(
    name="get_publish_status",
    description="Query the result of publish_clip: status (pending / processing / completed / failed) and each platform's success / url / error. While processing, checking every 10 seconds is enough.",
)
def get_publish_status(request_id: str, project_id: Optional[str] = None) -> Dict[str, Any]:
    from backend.services import upload_post_publisher as up

    try:
        return up.get_status(request_id, project_id=project_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "request_id": request_id, "error": str(e)[:500]}


@server.tool(
    name="list_publish_profiles",
    description="List the profiles (user) under the Upload-Post API Key and the platforms each has connected; call this first when the user doesn't know which user to fill in. Also returns whether the current configuration is ready.",
)
def list_publish_profiles() -> Dict[str, Any]:
    from backend.services import upload_post_publisher as up

    cfg = up.load_config()
    out: Dict[str, Any] = {"configured": cfg.configured, "source": cfg.source, "default_user": cfg.user, "platforms": up.PLATFORMS}
    if not cfg.configured:
        out["hint"] = "First set UPLOAD_POST_API_KEY or `autoclip publish --api-key … --user … --save` (https://app.upload-post.com/api-keys)"
        return out
    try:
        out["profiles"] = up.list_profiles(cfg)
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(e)[:500]
    return out


@server.tool(name="check_environment", description="Health check: whether ffmpeg, the Whisper runtime and the model connection are ready. Calling it once before producing clips saves trouble.")
def check_environment(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    from backend.services.local_runner import configure_llm, environment_report

    override = _make_override(provider, model, base_url)
    try:
        configure_llm(override)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    rep = environment_report()
    rep["ok"] = bool(rep["ffmpeg"]["ok"] and rep["llm"]["ok"])
    return rep


LEGACY_TOOLS = frozenset({
    'clip_video', 'start_clip_job', 'get_job_status', 'export_clip',
    'publish_clip', 'get_publish_status', 'list_publish_profiles',
})
_V2_SPECS: tuple = ()


def _v2_enabled() -> bool:
    from backend.services.studio.features import flag_enabled, resolve_features
    return flag_enabled(resolve_features(), 'mcp_v2_tools')


def _disabled(project_id: str = '') -> Dict[str, Any]:
    from backend.services import mcp_jobs
    body = mcp_jobs.progress_fields(status='unknown')
    return {'ok': False, 'project_id': project_id, 'error_code': 'disabled', **body}


def cancel_job(project_id: str) -> Dict[str, Any]:
    """Stop a running render. Finished videos stay; the job is marked cancelled."""
    if not _v2_enabled():
        return _disabled(project_id)
    from backend.services import mcp_jobs
    return mcp_jobs.cancel(project_id)


def list_styles() -> Dict[str, Any]:
    """Editing styles the produce/template parameter accepts. No extra knobs."""
    if not _v2_enabled():
        return _disabled()
    catalog = (
        {'id': 'editorial', 'name': '杂志风'},
        {'id': 'street', 'name': '街头快剪'},
        {'id': 'news', 'name': 'News'},
        {'id': 'cinema', 'name': 'Cinema'},
        {'id': 'classic', 'name': '经典'},
    )
    return {'ok': True, 'styles': list(catalog)}


def _sync_v2_tools() -> None:
    manager = getattr(server, '_tool_manager', None)
    if manager is None:
        return
    current = {tool.name for tool in manager.list_tools()}
    enabled = _v2_enabled()
    for fn, name, description in _V2_SPECS:
        if enabled and name not in current:
            server.add_tool(fn, name=name, description=description)
        elif not enabled and name in current:
            server.remove_tool(name)


def _present_tool(tool):
    if tool.name not in LEGACY_TOOLS:
        return tool
    description = tool.description or ''
    if not description.startswith('deprecated:'):
        description = 'deprecated: ' + description
    meta = dict(tool.meta or {})
    meta['deprecated'] = True
    return tool.model_copy(update={'description': description, 'meta': meta})


_original_list_tools = server.list_tools


async def _list_tools():
    _sync_v2_tools()
    return [_present_tool(tool) for tool in await _original_list_tools()]


server.list_tools = _list_tools  # type: ignore[method-assign]
_V2_SPECS = (
    (cancel_job, 'cancel_job', 'Cancel a running clip or quick-output job and stop its ffmpeg. Videos already written are not deleted. Returns unknown if the job cannot be found.'),
    (list_styles, 'list_styles', 'List the editing style ids: editorial, street (street cut), news, cinema, classic. Pass one as the template of start_quick_output or the CLI --template.'),
)

from backend.services import mcp_telemetry

_original_call_tool = server.call_tool


async def _call_tool(name, arguments, context=None):
    started = time.perf_counter()
    client = mcp_telemetry.client_from_context(context)
    try:
        result = await _original_call_tool(name, arguments, context)
    except Exception:
        mcp_telemetry.record(name, client, started, ok=False, error_code='failed')
        raise
    ok, error_code, job_id, terminal = mcp_telemetry.outcome_from_result(result)
    mcp_telemetry.record(name, client, started, ok=ok, error_code=error_code, job_id=job_id, terminal=terminal)
    return result


server.call_tool = _call_tool  # type: ignore[method-assign]


# ---------------------------------------------------------------- entry ---
async def _serve_stdio() -> None:
    """
    stdout 是 MCP 协议通道。流水线里散落着 print()，一旦落到 stdout 就会把协议打坏，
    所以把真正的 stdout 交给 MCP 传输层，再把 sys.stdout 指到 stderr。
    """
    import io
    import anyio
    from mcp.server.stdio import stdio_server

    real_stdout = anyio.wrap_file(io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True))
    real_stdin = anyio.wrap_file(io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"))
    sys.stdout = sys.stderr

    lowlevel = getattr(server, "_lowlevel_server", None) or getattr(server, "_mcp_server")
    async with stdio_server(stdin=real_stdin, stdout=real_stdout) as (read_stream, write_stream):
        await lowlevel.run(read_stream, write_stream, lowlevel.create_initialization_options())


def main() -> int:
    configure_environment()
    setup_logging(verbose=False)  # 日志只进文件，终端（stderr）保持安静
    import anyio

    anyio.run(_serve_stdio)
    return 0


if __name__ == "__main__":
    sys.exit(main())
