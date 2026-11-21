#!/usr/bin/env python3
"""
autoclip — command-line clip generation.

    autoclip run video.mp4                         # use the model configured in the desktop app
    autoclip run video.mp4 --provider ollama       # local Ollama (default qwen2.5:7b, no key needed)
    autoclip run video.mp4 --provider lmstudio --model qwen2.5-7b-instruct
    AUTOCLIP_API_KEY=sk-... autoclip run video.mp4 --provider openai --base-url https://api.deepseek.com/v1 --model deepseek-chat
    autoclip run video.mp4 --srt video.srt --min-score 0.6 --json
    autoclip list / show <project_id> / providers / doctor
    autoclip publish <project_id> --clip 2 --platform tiktok --platform youtube   # publish to overseas platforms via Upload-Post
    autoclip mcp install opencode                  # write AutoClip into opencode's MCP config (global by default)

Output shares the same data directory as the desktop app (mac: ~/Library/Application Support/AutoClip),
so finished projects show up on the desktop app's home page. Use --data-dir or AUTOCLIP_DATA_DIR to change the directory.

No install needed: `python -m backend.cli ...`; after `pip install -e .` the `autoclip` command is available.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from backend import __version__

# 只 import 不触发数据库 / 模型初始化的东西；重活放在子命令里、configure_environment 之后
from backend.services.local_runner import (
    PROVIDER_CHOICES,
    LLMOverride,
    RunRequest,
    configure_environment,
    setup_logging,
)

STAGE_NAMES = {
    "INGEST": "准备", "SUBTITLE": "字幕", "ANALYZE": "分析",
    "HIGHLIGHT": "选段", "EXPORT": "导出", "DONE": "完成",
}
_TTY = sys.stderr.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def _dim(s: str) -> str:
    return _c("2", s)


def _bold(s: str) -> str:
    return _c("1", s)


def _err(msg: str) -> None:
    print(_c("31", "error: ") + msg, file=sys.stderr)


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("Model (if omitted, uses the config from the desktop app's settings page)")
    g.add_argument("--provider", choices=PROVIDER_CHOICES, help="dashscope / openai / gemini / deepseek / seed / kimi / glm / grok / infistar / api88 / apimart, or a local preset: ollama / lmstudio")
    g.add_argument("--model", help="Model name, e.g. qwen-plus, gpt-4o-mini, qwen2.5:7b")
    g.add_argument("--base-url", help="OpenAI-compatible API endpoint (used when provider=openai; ollama/lmstudio have defaults)")
    g.add_argument("--api-key", help="Deprecated. Use the AUTOCLIP_API_KEY environment variable instead; passing it still works but prints a warning to stderr")


def _llm_override(args: argparse.Namespace) -> LLMOverride:
    supplied = getattr(args, "api_key", None)
    if supplied:
        print("Warning: --api-key is deprecated; use the AUTOCLIP_API_KEY environment variable instead.", file=sys.stderr)
    return LLMOverride(
        provider=getattr(args, "provider", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        api_key=supplied or os.getenv("AUTOCLIP_API_KEY"),
    )


# ---------------------------------------------------------------- run ---
def cmd_produce(args: argparse.Namespace) -> int:
    from backend.services import mcp_telemetry
    from backend.services import quick_output_runner as quick
    started = time.perf_counter()
    outcome = {'ok': False, 'error_code': 'failed', 'job_id': None, 'terminal': False}
    try:
        # Providers and ASR may print progress from worker threads. Keep stdout parseable
        # for scripts, just as the MCP stdio entry point keeps its protocol stream clean.
        with contextlib.redirect_stdout(sys.stderr):
            project_id = quick.start(args.source, args.platform or ['douyin'], name=args.name, srt_path=args.srt,
                                     instruction=args.instruction, browser=args.browser, portrait_style=args.portrait_style,
                                     template=args.template)
            if not args.json:
                print(f'1.5 One-click production · {project_id} · progress is written to the project directory', file=sys.stderr)
            result = quick.wait(project_id, timeout=args.timeout)
        ok = result['status'] in ('completed', 'partial') and not result.get('timed_out')
        if result.get('timed_out'):
            error_code = 'timeout'
        elif ok:
            error_code = 'none'
        else:
            error_code = mcp_telemetry.normalize_error(result.get('error_code') or result.get('status'), ok=False)  
        outcome = {'ok': ok, 'error_code': error_code, 'job_id': project_id, 'terminal': True}
        result['ok'] = ok
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    except quick.StyleDisabled as error:
        outcome = {'ok': False, 'error_code': 'disabled', 'job_id': None, 'terminal': False}
        print(json.dumps({'ok': False, 'error_code': 'disabled', 'error': str(error)}, ensure_ascii=False))
        return 2
    except (ValueError, FileNotFoundError) as error:
        outcome = {'ok': False, 'error_code': 'invalid_input', 'job_id': None, 'terminal': False}
        print(json.dumps({'ok': False, 'error_code': 'invalid_input', 'error': str(error)}, ensure_ascii=False))
        return 2
    finally:
        mcp_telemetry.record('produce', 'cli', started, **outcome)


def cmd_outputs(args: argparse.Namespace) -> int:
    from backend.services import mcp_telemetry
    from backend.services import quick_output_runner as quick
    started = time.perf_counter()
    outcome = {'ok': False, 'error_code': 'invalid_input', 'job_id': None, 'terminal': False}
    try:
        result = quick.status(args.project_id, export_kits=args.export_kits)
        status = result.get('status')
        ok = status in ('completed', 'partial', 'running', 'queued')
        terminal = status in ('completed', 'partial', 'failed', 'interrupted', 'cancelled')
        error_code = 'none' if ok else mcp_telemetry.normalize_error(status, ok=False)
        outcome = {'ok': ok, 'error_code': error_code, 'job_id': args.project_id, 'terminal': terminal}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, FileNotFoundError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        return 2
    finally:
        mcp_telemetry.record('outputs', 'cli', started, **outcome)


def cmd_run(args: argparse.Namespace) -> int:
    from backend.services import mcp_telemetry
    from backend.services.local_runner import configure_llm, prepare_project, run_pipeline, summarize_project

    started = time.perf_counter()
    outcome = {'ok': False, 'error_code': 'failed', 'job_id': None, 'terminal': False}
    t0 = time.time()
    try:
        return _cmd_run_body(args, outcome, configure_llm, prepare_project, run_pipeline, summarize_project, t0)
    finally:
        mcp_telemetry.record('run', 'cli', started, **outcome)


def _cmd_run_body(args, outcome, configure_llm, prepare_project, run_pipeline, summarize_project, t0) -> int:
    try:
        info = configure_llm(_llm_override(args))
    except Exception as e:  # noqa: BLE001
        outcome.update(ok=False, error_code='invalid_input')
        _err(str(e))
        return 2

    from backend.services.studio.readiness import TranscriptionNotReady
    from backend.services.studio.preflight import SourceRejected
    req = RunRequest(
        video=Path(args.video),
        srt=Path(args.srt) if args.srt else None,
        name=args.name,
        category=args.category,
        min_score=args.min_score,
        register_db=not args.no_db,
    )
    try:
        video_in_raw = prepare_project(req, link=not args.copy)
    except (FileNotFoundError, TranscriptionNotReady, SourceRejected) as e:  # only preflight refusals
        # #284: a source refusal keeps its own code (source_unreadable / source_too_short / source_no_audio).
        code = e.code if isinstance(e, SourceRejected) else 'invalid_input'
        outcome.update(ok=False, error_code=code, job_id=req.project_id)
        if args.json:  # scripts read the contract from stdout
            print(json.dumps({'ok': False, 'error_code': code, 'project_id': req.project_id,
                              'error': str(e)}, ensure_ascii=False))
        else:
            _err(str(e))
        return 2

    if not args.json:
        model_desc = f"{info.get('provider')} · {info.get('model')}"
        if info.get("base_url"):
            model_desc += f" · {info['base_url']}"
        print(_bold(req.name or req.video.stem) + _dim(f"  {req.project_id}"), file=sys.stderr)
        print(_dim(f"Model {model_desc}"), file=sys.stderr)
        if not req.srt:
            print(_dim("No subtitle file; transcribing locally with Whisper (the model is downloaded on first use)"), file=sys.stderr)

    last = {"line": ""}

    def on_progress(p: Dict[str, Any]) -> None:
        if args.json:
            return
        stage = STAGE_NAMES.get(p.get("stage", ""), p.get("stage", ""))
        line = f"[{p.get('percent', 0):3d}%] {stage:<3} {p.get('message', '')}"
        if line != last["line"]:
            print(line, file=sys.stderr)
            last["line"] = line

    result = run_pipeline(req, video_in_raw, on_progress=on_progress)
    elapsed = time.time() - t0

    if result.get("status") != "succeeded":
        from backend.services.mcp_telemetry import normalize_error
        outcome.update(ok=False, error_code=normalize_error(result.get('error_code') or result.get('status'), ok=False),
                       job_id=req.project_id, terminal=True)
        if args.json:
            print(json.dumps({"ok": False, "project_id": req.project_id, "error": result.get("error"),
                              "error_code": outcome.get("error_code")}, ensure_ascii=False))  
        else:
            _err(result.get("error") or "处理失败")
            print(_dim(f"Log: {os.getenv('LOG_FILE')}"), file=sys.stderr)
        return 1

    outcome.update(ok=True, error_code='none', job_id=req.project_id, terminal=True)
    summary = summarize_project(req.project_id)
    summary["elapsed_sec"] = round(elapsed, 1)
    summary["llm"] = {k: info.get(k) for k in ("provider", "model", "base_url") if info.get(k)}
    if args.json:
        print(json.dumps({"ok": True, **summary}, ensure_ascii=False, indent=2))
        return 0

    print(file=sys.stderr)
    n_clips, n_cols = summary["counts"]["clips"], summary["counts"]["collections"]
    print(_bold(f"Done  {n_clips} clips · {n_cols} collections · {elapsed:.0f}s"), file=sys.stderr)
    for c in summary["clips"]:
        score = str(c["score_100"]) if c.get("score_100") is not None else "-"
        print(f"  {score:>4}  {c['start_time']} → {c['end_time']}  {c['title']}", file=sys.stderr)
    if n_clips == 0:
        print(_dim("  No segment reached the score threshold; try --min-score 0.5"), file=sys.stderr)
    print(_dim(f"Output directory {summary['clips_dir']}"), file=sys.stderr)
    # stdout 只放项目 id，方便 shell 里接管道
    print(req.project_id)
    return 0


# ---------------------------------------------------------------- list / show ---
def cmd_list(args: argparse.Namespace) -> int:
    from backend.services.local_runner import list_projects

    items = list_projects(limit=args.limit)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    if not items:
        print(_dim("No projects yet. Start with: autoclip run <video>"))
        return 0
    for it in items:
        print(f"{it['project_id']}  {it['status']:<9} {it['clips']:>3} clips  {it['updated_at'][:16]}  {it['name']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from backend.services.local_runner import summarize_project

    try:
        s = summarize_project(args.project_id)
    except FileNotFoundError as e:
        _err(str(e))
        return 2
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    print(_bold(s["name"]) + _dim(f"  {s['project_id']}"))
    print(_dim(f"{s['counts']['clips']} clips · {s['counts']['collections']} collections · {s['project_dir']}"))
    for c in s["clips"]:
        score = str(c["score_100"]) if c.get("score_100") is not None else "-"
        print(f"  {score:>4}  {c['start_time']} → {c['end_time']}  {c['title']}")
        if c.get("file"):
            print(_dim(f"        {c['file']}"))
    for col in s["collections"]:
        print(f"  Collection  {col['title']}  {_dim('clips ' + ', '.join(col['clip_ids']))}")
        if col.get("file"):
            print(_dim(f"        {col['file']}"))
    return 0


# ---------------------------------------------------------------- providers / doctor ---
def cmd_providers(args: argparse.Namespace) -> int:
    from backend.core.local_presets import LOCAL_PRESETS
    from backend.core.llm_manager import get_llm_manager

    rows = [
        ("dashscope", "阿里通义千问", "需要 key；国内直连，qwen-plus 性价比高"),
        ("openai", "OpenAI / 兼容接口", "需要 key；--base-url 可指向 DeepSeek / 智谱 / OpenRouter / vLLM"),
        ("gemini", "Google Gemini", "Requires key"),
        ("deepseek", "DeepSeek", "Requires key; official API, deepseek-flash"),
        ("seed", "Seed（火山方舟）", "需要 key；国内直连，豆包 Seed 2.1"),
        ("kimi", "Kimi（月之暗面）", "需要 key；国内直连"),
        ("glm", "智谱 GLM", "需要 key；国内直连"),
        ("grok", "Grok (xAI)", "Requires key"),
        ("api88", "88API Token aggregation platform (sponsor)", "Requires key; new users get trial credit on sign-up"),
        ("infistar", "Infistar (sponsor)", "Requires key; sign up via the dedicated link to get trial credit"),
        ("apimart", "APIMart (sponsor)", "Requires key; one key for language, image and video models"),
    ] + [
        (p.key, p.display_name, f"No key needed; default {p.base_url}" + (f", default model {p.default_model}" if p.default_model else "")
         + (f". {p.context_hint}" if p.context_hint else ""))
        for p in LOCAL_PRESETS.values()
    ]
    info = get_llm_manager().get_current_provider_info()
    if args.json:
        print(json.dumps({"current": info, "providers": [dict(zip(("key", "name", "note"), r)) for r in rows]}, ensure_ascii=False, indent=2))
        return 0
    for key, name, note in rows:
        mark = "●" if key == info.get("provider") else " "
        print(f"{mark} {key:<12} {name:<18} {_dim(note)}")
    cur = f"{info.get('provider')} · {info.get('model')}" + (f" · {info['base_url']}" if info.get("base_url") else "")
    print(_dim(f"\nCurrent: {cur}  ({'available' if info.get('available') else 'key not configured'})"))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from backend.services.local_runner import configure_llm, environment_report

    try:
        configure_llm(_llm_override(args))
    except Exception as e:  # noqa: BLE001
        _err(str(e))
    rep = environment_report()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ffmpeg"]["ok"] and rep["llm"]["ok"] else 1

    def line(ok: Optional[bool], label: str, detail: str) -> None:
        mark = _c("32", "✓") if ok else _c("31", "✗")
        print(f"{mark} {label:<8} {detail}")

    print(_dim(f"Data directory {rep['data_dir']} · Python {rep['python']}"))
    line(rep["ffmpeg"]["ok"], "ffmpeg", rep["ffmpeg"].get("path") or rep["ffmpeg"].get("error", ""))
    w = rep["whisper"]
    line(w["ok"], "whisper", ("faster-whisper installed" if w["ok"] else "Not installed (needed for videos without subtitles; install with one click in the desktop app: Settings → Default Model → Speech to Subtitles)"))
    llm = rep["llm"]
    llm_desc = f"{llm.get('provider')} · {llm.get('model')}" + (f" · {llm['base_url']}" if llm.get("base_url") else "")
    line(llm["ok"], "模型", llm_desc + ("" if llm["ok"] else f"  {_dim(llm.get('error') or '')}"))
    if llm.get("context_hint"):
        print(_dim(f"    {llm['context_hint']}"))
    return 0 if rep["ffmpeg"]["ok"] and llm["ok"] else 1


# ---------------------------------------------------------------- export ---
def cmd_export(args: argparse.Namespace) -> int:
    from backend.services.publish_export import ExportRequest, export_clip, list_presets, load_clip_meta
    from backend.services.local_runner import summarize_project

    if args.list_presets:
        rows = list_presets()
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for p in rows:
                print(f"{p['key']:<14} {p['label']:<16} {p.get('w') or '-'}x{p.get('h') or '-'}  {p.get('layout')}")
        return 0

    clip_ids = list(args.clip or [])
    if not clip_ids:
        try:
            summary = summarize_project(args.project_id)
        except FileNotFoundError as e:
            _err(str(e))
            return 2
        clip_ids = [c["id"] for c in summary["clips"]]
        if not clip_ids:
            _err("This project has no clips")
            return 2

    results = []
    for cid in clip_ids:
        try:
            load_clip_meta(args.project_id, cid)
            r = export_clip(ExportRequest(
                project_id=args.project_id, clip_id=cid, preset=args.preset,
                subtitles=not args.no_subtitles, title_card=not args.no_title,
            ))
            results.append(r)
            if not args.json:
                print(f"{'cached' if r.get('cached') else 'ok':<8} {cid}  {r.get('path')}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            results.append({"ok": False, "clip_id": cid, "error": str(e)[:400]})
            _err(f"{cid}: {e}")

    if args.json:
        print(json.dumps({"ok": all(r.get("ok") for r in results), "exports": results}, ensure_ascii=False, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


# ---------------------------------------------------------------- publish ---
def _print_publish_status(st: Dict[str, Any]) -> None:
    print(f"{st.get('status')}  {st.get('completed') or 0}/{st.get('total') or '?'}  {_dim(st.get('request_id') or st.get('job_id') or '')}", file=sys.stderr)
    for r in st.get("results") or []:
        if r.get("skipped"):
            mark, detail = _dim("-"), "Profile has not connected this platform; skipped"
        elif r.get("success"):
            mark, detail = _c("32", "✓"), r.get("url") or r.get("message") or ""
            if r.get("fallback_to_inbox"):
                detail += "  (saved to the TikTok inbox as a draft; publish it in the app)"
        else:
            mark, detail = _c("31", "✗"), r.get("error") or r.get("message") or r.get("status") or ""
        print(f"  {mark} {r.get('platform', ''):<12} {detail}", file=sys.stderr)


def cmd_publish(args: argparse.Namespace) -> int:
    from backend.services import upload_post_publisher as up

    # 保存配置
    if args.save:
        if not (args.api_key or args.user):
            _err("--save requires --api-key and/or --user")
            return 2
        if args.api_key:
            try:
                acct = up.verify_api_key(up.UploadPostConfig(api_key=args.api_key, base_url=up.load_config().base_url))
            except up.UploadPostError as e:
                _err(str(e))
                return 2
            print(_dim(f"API Key is valid: {acct.get('email')} · {acct.get('plan')}"), file=sys.stderr)
        cfg = up.save_config(api_key=args.api_key, user=args.user)
        print(f"Saved to {up.config_path()}  key={cfg.masked_key()} user={cfg.user or '-'}", file=sys.stderr)
        if cfg.source == "env":
            print(_dim("Note: the UPLOAD_POST_API_KEY environment variable currently takes precedence over the file"), file=sys.stderr)
        return 0

    cfg = up.load_config()
    if args.api_key:
        cfg = up.UploadPostConfig(api_key=args.api_key, user=args.user or cfg.user, base_url=cfg.base_url, source="cli")

    if args.list_profiles:
        try:
            profiles = up.list_profiles(cfg)
        except up.UploadPostError as e:
            _err(str(e))
            return 2
        if args.json:
            print(json.dumps(profiles, ensure_ascii=False, indent=2))
            return 0
        if not profiles:
            print(_dim("No profiles under this API Key yet. Create and connect an account at https://app.upload-post.com/manage-users."))
        for pr in profiles:
            mark = "●" if pr["username"] == cfg.user else " "
            connected = ", ".join(pr["connected_platforms"]) or "No platforms connected"
            reconnect = ", ".join(pr.get("reconnect_platforms") or [])
            extra = f"  (needs reconnect: {reconnect})" if reconnect else ""
            print(f"{mark} {pr['username']:<24} {_dim(connected + extra)}")
        return 0

    if args.status:
        try:
            st = up.wait_for_status(args.status, config=cfg, project_id=args.project_id,
                                    on_update=None if args.json else _print_publish_status) if args.wait \
                else up.get_status(args.status, config=cfg, project_id=args.project_id)
        except up.UploadPostError as e:
            _err(str(e))
            return 2
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        elif not args.wait:
            _print_publish_status(st)
        return 0 if st.get("status") not in ("failed", "not_found") else 1

    if not args.project_id:
        _err("project_id is required (or use --list-profiles / --status)")
        return 2
    if not args.platform:
        _err("At least one --platform is required, e.g. --platform tiktok --platform youtube")
        return 2
    try:
        platforms = up.normalize_platforms(args.platform)
    except ValueError as e:
        _err(str(e))
        return 2

    clip_ids = list(args.clip or [])
    if not clip_ids:
        from backend.services.local_runner import summarize_project
        try:
            summary = summarize_project(args.project_id)
        except FileNotFoundError as e:
            _err(str(e))
            return 2
        clip_ids = [c["id"] for c in summary["clips"]]
        if not clip_ids:
            _err("This project has no clips")
            return 2
        if len(clip_ids) > 1 and not args.yes:
            _err(f"No --clip specified; this would publish all {len(clip_ids)} clips to {', '.join(platforms)}; add --yes to confirm")
            return 2

    extra: Dict[str, Any] = {}
    for kv in args.extra or []:
        if "=" not in kv:
            _err(f"--extra must be in key=value format: {kv}")
            return 2
        k, v = kv.split("=", 1)
        extra[k.strip()] = v.strip()

    results = []
    for cid in clip_ids:
        req = up.PublishRequest(
            project_id=args.project_id, clip_id=cid, platforms=platforms, user=args.user, preset=args.preset,
            title=args.title, description=args.description, subtitles=not args.no_subtitles,
            title_card=not args.no_title, scheduled_date=args.schedule, timezone=args.timezone, extra=extra,
        )
        try:
            r = up.publish_clip(req, config=cfg)
            results.append(r)
            if not args.json:
                print(f"{_c('32', 'submitted')} {cid}  {r['title']}  {_dim(r['request_id'])}", file=sys.stderr)
                for w in r.get("export_warnings") or []:
                    print(_dim(f"         {w}"), file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            results.append({"ok": False, "clip_id": cid, "error": str(e)[:400]})
            _err(f"{cid}: {e}")

    if args.wait:
        for r in results:
            if not r.get("ok"):
                continue
            st = up.wait_for_status(r["request_id"], timeout_sec=args.timeout, config=cfg, project_id=args.project_id)
            r["status"] = st.get("status")
            r["results"] = st.get("results")
            r["ok"] = st.get("status") != "failed"
            if not args.json:
                print(_bold(f"{r['clip_id']}  {r['title']}"), file=sys.stderr)
                _print_publish_status(st)

    if args.json:
        print(json.dumps({"ok": all(r.get("ok") for r in results), "published": results}, ensure_ascii=False, indent=2))
    elif not args.wait:
        print(_dim("To view results: autoclip publish --status <request_id> (add --wait to wait for a final state)"), file=sys.stderr)
    return 0 if all(r.get("ok") for r in results) else 1


# ---------------------------------------------------------------- mcp ---
def cmd_mcp(args: argparse.Namespace) -> int:
    if getattr(args, "mcp_cmd", None) == "install":
        return cmd_mcp_install(args)
    from backend.mcp_server import main as mcp_main

    return mcp_main()


def _print_opencode_next_steps() -> None:
    print(_dim("  In opencode just say: cut C:\\Videos\\talk.mp4 into clips, and it will call AutoClip's MCP tools"))
    print(_dim("  If the model isn't set up, run autoclip doctor first; opencode needs a new session to load the new MCP"))


def cmd_mcp_install(args: argparse.Namespace) -> int:
    """把 AutoClip 写进 MCP 客户端配置；目前支持 opencode。"""
    from backend.services import opencode_setup

    target = opencode_setup.opencode_config_path(args.scope, Path(args.dir) if args.dir else None)
    if args.print_only:
        snippet = opencode_setup.render_snippet(opencode_setup.build_entry(), name=args.name)
        if args.json:
            print(json.dumps({"client": args.client, "path": str(target), "snippet": snippet}, ensure_ascii=False, indent=2))
        else:
            print(snippet)
            print(_dim(f"\nMerge the snippet above into {target}"), file=sys.stderr)
        return 0

    explicit_config = args.scope == "global" and bool(os.environ.get("OPENCODE_CONFIG"))
    report = opencode_setup.install_opencode(
        target, name=args.name, force=args.force, discover_jsonc=not explicit_config,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif report["action"] == "created":
        print(_c("32", "✓ ") + f"Wrote {report['path']}")
        _print_opencode_next_steps()
    elif report["action"] == "updated":
        print(_c("32", "✓ ") + f"Updated mcp.{args.name} in {report['path']}")
        if report.get("backup"):
            print(_dim(f"  Old config backed up: {report['backup']}"))
        _print_opencode_next_steps()
    elif report["action"] == "unchanged":
        print(_dim(f"• {report['path']} already has an identical mcp.{args.name}; unchanged"))
    else:
        _err(report.get("error") or "Write failed")
        if report.get("hint"):
            print(_dim(f"  {report['hint']}"), file=sys.stderr)
        print(report["snippet"])
    for w in report.get("warnings") or []:
        print(_dim(f"  ! {w}"), file=sys.stderr)
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------- parser ---
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autoclip",
        description="AutoClip command line: cut a long video into highlight clips with a single command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("\n\n", 1)[1] if __doc__ else None,
    )
    p.add_argument("--data-dir", help="Data directory (shared with the desktop app by default; AUTOCLIP_DATA_DIR also works)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="Print backend logs to the terminal")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser('produce', help='1.5 One-click production: platform videos, covers, publishing copy and publish kits (shares desktop settings)')
    q.add_argument('source', help='Local video path or an HTTPS YouTube / Bilibili link')
    q.add_argument('--platform', action='append', help='Platform, repeatable: douyin / xiaohongshu / tiktok / instagram_reels / youtube_shorts / youtube_long / bilibili')
    q.add_argument('--name')
    q.add_argument('--srt', help='Local SRT subtitle file')
    q.add_argument('--instruction', default='')
    q.add_argument('--portrait-style', choices=['auto', 'interview', 'podcast'], default='auto', help='Portrait layout; aspect ratio and text language still follow the platform')
    q.add_argument('--template', choices=['editorial', 'street', 'news', 'cinema', 'classic'], help='Editing style (controlled by the mcp_v2_tools flag, on by default; set AUTOCLIP_FLAGS=mcp_v2_tools=off to disable): editorial magazine style / street quick-cut / news / cinema / classic. The template in the result is the style actually used')
    q.add_argument('--browser', choices=['chrome', 'edge', 'firefox', 'safari'])
    q.add_argument('--timeout', type=float, default=7200, help='Seconds to wait; the project is not cancelled on timeout')
    q.add_argument('--json', action='store_true', help='Print only the JSON result to stdout')
    q.set_defaults(func=cmd_produce)

    outputs = sub.add_parser('outputs', help='Check one-click production progress and file paths (does not trigger generation)')
    outputs.add_argument('project_id')
    outputs.add_argument('--export-kits', action='store_true', help='Write publish-kit ZIPs for completed versions')
    outputs.set_defaults(func=cmd_outputs)

    r = sub.add_parser("run", help="Process a video and output clips and collections")
    r.add_argument("video", help="Video file path")
    r.add_argument("--srt", help="Existing subtitle file (otherwise transcribed locally with Whisper)")
    r.add_argument("--name", help="Project name (defaults to the file name)")
    r.add_argument("--category", default="default",
                   choices=["default", "knowledge", "business", "opinion", "experience", "speech", "content_review", "entertainment"],
                   help="Content type; affects the prompts")
    r.add_argument("--min-score", type=float, help="Minimum score threshold 0–1 (default 0.7; lower it if you get 0 clips)")
    r.add_argument("--copy", action="store_true", help="Copy the video into the project directory (default is a hard link, which uses no extra space)")
    r.add_argument("--no-db", action="store_true", help="Do not write SQLite (the project will not appear in the desktop app)")
    r.add_argument("--json", action="store_true", help="Output the result as JSON (for scripts / agents)")
    _add_llm_args(r)
    r.set_defaults(func=cmd_run)

    l = sub.add_parser("list", help="List projects")
    l.add_argument("--limit", type=int, default=30)
    l.add_argument("--json", action="store_true")
    l.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="Show a project's clips and file paths")
    s.add_argument("project_id")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    pr = sub.add_parser("providers", help="List available model providers and local presets")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_providers)

    d = sub.add_parser("doctor", help="Check ffmpeg / Whisper / model connectivity")
    d.add_argument("--json", action="store_true")
    _add_llm_args(d)
    d.set_defaults(func=cmd_doctor)

    m = sub.add_parser("mcp", help="Run as an MCP server (stdio) for Cursor / Claude / opencode; the install subcommand writes the client config automatically")
    m.set_defaults(func=cmd_mcp)  # 不带子命令 = 直接起 server（子命令的分发在 cmd_mcp 里判断）
    mi_sub = m.add_subparsers(dest="mcp_cmd")
    mi = mi_sub.add_parser("install", help="Write AutoClip into the MCP client config (currently supports opencode)")
    mi.add_argument("client", nargs="?", default="opencode", choices=["opencode"], help="MCP client (default opencode)")
    mi.add_argument("--scope", choices=["global", "project"], default="global",
                    help="global=~/.config/opencode/opencode.json; project=<--dir directory>/opencode.json")
    mi.add_argument("--dir", help="Project directory for project scope (default: current directory)")
    mi.add_argument("--name", default="autoclip", help="MCP server name in the config (default autoclip)")
    mi.add_argument("--print", dest="print_only", action="store_true", help="Only print the config snippet; do not modify files")
    mi.add_argument("--force", action="store_true", help="If the existing config has comments / trailing commas, back it up and rewrite as plain JSON")
    mi.add_argument("--json", action="store_true")

    e = sub.add_parser("export", help="Render clips into publish-ready videos (9:16 / burned-in subtitles / title card)")
    e.add_argument("project_id")
    e.add_argument("--preset", default="douyin", choices=["douyin", "xiaohongshu", "shorts", "bilibili", "original"])
    e.add_argument("--clip", action="append", help="Clip id, repeatable; exports all if omitted")
    e.add_argument("--no-subtitles", action="store_true")
    e.add_argument("--no-title", action="store_true")
    e.add_argument("--list-presets", action="store_true")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_export)

    pu = sub.add_parser("publish", help="Publish clips to TikTok / Instagram / YouTube Shorts / X and other overseas platforms via Upload-Post")
    pu.add_argument("project_id", nargs="?")
    pu.add_argument("--clip", action="append", help="Clip id, repeatable; all clips if omitted (requires --yes)")
    pu.add_argument("--platform", action="append", help="Platform, repeatable or comma-separated: tiktok, instagram, youtube, facebook, linkedin, x, threads, pinterest, bluesky …")
    pu.add_argument("--user", help="Upload-Post profile (defaults to the saved one)")
    pu.add_argument("--preset", choices=["douyin", "xiaohongshu", "shorts", "bilibili", "original"],
                    help="Export preset for publishing; chosen automatically by platform if omitted (shorts for portrait platforms, otherwise original)")
    pu.add_argument("--title", help="Title (defaults to the clip title)")
    pu.add_argument("--description", help="Description (YouTube / LinkedIn / Facebook / Pinterest)")
    pu.add_argument("--no-subtitles", action="store_true")
    pu.add_argument("--no-title", action="store_true", help="Do not add a title card")
    pu.add_argument("--schedule", help="Scheduled publish time, ISO-8601, e.g. 2026-10-01T09:00:00")
    pu.add_argument("--timezone", help="IANA time zone, used with --schedule, e.g. Asia/Shanghai")
    pu.add_argument("--extra", action="append", metavar="KEY=VALUE",
                    help="Pass-through platform-specific fields, e.g. privacy_level=SELF_ONLY, privacyStatus=unlisted, facebook_page_id=…")
    pu.add_argument("--wait", action="store_true", help="After submitting, wait for each platform's result")
    pu.add_argument("--timeout", type=float, default=600, help="Maximum seconds to wait for --wait")
    pu.add_argument("--yes", action="store_true", help="Confirm publishing all clips when --clip is not specified")
    pu.add_argument("--api-key", help="Temporarily specify the API Key (use with --save to persist it)")
    pu.add_argument("--save", action="store_true", help="Save --api-key / --user to upload_post.json in the data directory")
    pu.add_argument("--list-profiles", action="store_true", help="List the profiles under the API Key and their connected platforms")
    pu.add_argument("--status", metavar="REQUEST_ID", help="Check per-platform results of a publish request")
    pu.add_argument("--json", action="store_true")
    pu.set_defaults(func=cmd_publish)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_environment(Path(args.data_dir) if args.data_dir else None)
    setup_logging(verbose=args.verbose)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        _err("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
