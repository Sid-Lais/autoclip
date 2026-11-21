"""把 AutoClip 接入 opencode CLI（opencode.json 的 mcp 配置）。

opencode（https://opencode.ai）的本地 MCP server 写在配置文件的 mcp 段：

    {
      "mcp": {
        "autoclip": { "type": "local", "command": ["/abs/path/autoclip", "mcp"], "enabled": true }
      }
    }

- 全局配置：`~/.config/opencode/opencode.json`（`OPENCODE_CONFIG` / `XDG_CONFIG_HOME` 可改路径）
- 项目配置：`<项目>/opencode.json`；opencode 会合并多处配置，项目覆盖全局
- 同时存在 `.json` 与 `.jsonc` 时 opencode 先读 json 再读 jsonc（后者覆盖同名键）：
  只有 jsonc 时直接写它；两份都有且 jsonc 里已有 mcp 段时拒绝写入（避免“写入成功但实际加载旧配置”）

对应 CLI：`autoclip mcp install opencode`（`--scope project` 写项目配置、`--print` 只打印片段）。
本模块只做配置读写与命令探测，不 import 后端重依赖，方便 CLI 与测试单独使用。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

OPENCODE_SCHEMA = "https://opencode.ai/config.json"
DEFAULT_SERVER_NAME = "autoclip"


def detect_server() -> Dict[str, Any]:
    """探测用什么命令启动 MCP server：PATH 里的 autoclip → autoclip-mcp → python -m backend.mcp_server。"""
    exe = shutil.which("autoclip")
    if exe:
        return {"command": [exe, "mcp"], "cwd": None, "environment": None, "kind": "autoclip"}
    exe = shutil.which("autoclip-mcp")
    if exe:
        return {"command": [exe], "cwd": None, "environment": None, "kind": "autoclip-mcp"}
    repo_root = Path(__file__).resolve().parents[2]
    env = {"PYTHONPATH": str(repo_root)}
    cwd = str(repo_root) if (repo_root / "backend" / "mcp_server.py").is_file() else None
    return {
        "command": [sys.executable or "python", "-m", "backend.mcp_server"],
        "cwd": cwd,
        "environment": env,
        "kind": "module",
    }


def opencode_config_path(scope: str = "global", project_dir: Optional[Path] = None) -> Path:
    """目标配置文件：global → ~/.config/opencode/opencode.json；project → <目录>/opencode.json。"""
    if scope == "project":
        base = Path(project_dir).expanduser() if project_dir else Path.cwd()
        return base / "opencode.json"
    override = os.environ.get("OPENCODE_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return config_home / "opencode" / "opencode.json"


def build_entry(server: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """opencode `mcp` 段里的 autoclip 条目（type=local）。"""
    server = server or detect_server()
    entry: Dict[str, Any] = {"type": "local", "command": list(server["command"]), "enabled": True}
    if server.get("cwd"):
        entry["cwd"] = server["cwd"]
    if server.get("environment"):
        entry["environment"] = dict(server["environment"])
    return entry


def render_snippet(entry: Dict[str, Any], name: str = DEFAULT_SERVER_NAME) -> str:
    """手动合并进 opencode 配置时用的 JSON 片段。"""
    return json.dumps({"mcp": {name: entry}}, ensure_ascii=False, indent=2)


def _strip_comments(text: str) -> str:
    """去掉 // 与 /* */ 注释；字符串（含转义）里的内容不动。"""
    out: List[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """去掉对象 / 数组结尾多余的逗号；字符串里的逗号不动。"""
    out: List[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_config_text(text: str) -> Tuple[Dict[str, Any], bool]:
    """解析 opencode 配置文本，返回 (data, 是否走了 JSONC 宽松模式)。

    先按严格 JSON 解析；失败再容忍注释与尾随逗号（opencode 官方支持 JSONC）。
    两种都失败时抛 json.JSONDecodeError。
    """
    text = text.lstrip("\ufeff")  # Windows 编辑器可能带 UTF-8 BOM
    try:
        data = json.loads(text)
        lenient = False
    except json.JSONDecodeError:
        data = json.loads(_strip_trailing_commas(_strip_comments(text)))
        lenient = True
    if not isinstance(data, dict):
        raise json.JSONDecodeError("The top level of the opencode config must be a JSON object", text, 0)
    return data, lenient


def install_opencode(
    target: Path,
    *,
    server: Optional[Dict[str, Any]] = None,
    name: str = DEFAULT_SERVER_NAME,
    force: bool = False,
    discover_jsonc: bool = True,
) -> Dict[str, Any]:
    """把 autoclip 写进 opencode 配置（合并写入：不动其它键，重装也保留同名条目里的自定义字段）。

    返回报告：ok / action / path / entry / snippet / backup / warnings / error / hint。
    action 取值 created / updated / unchanged / manual；manual = 没有写文件，
    需要用户手动合并 snippet（配置解析失败，或带注释又没给 force）。
    discover_jsonc=False 时严格写 target，用于 OPENCODE_CONFIG 显式指定的文件。
    """
    server = server or detect_server()
    entry = build_entry(server)
    report: Dict[str, Any] = {
        "ok": False,
        "action": "manual",
        "path": str(target),
        "entry": entry,
        "snippet": render_snippet(entry, name=name),
        "backup": None,
        "warnings": [],
        "error": None,
        "hint": None,
    }

    # 同时存在 opencode.json / opencode.jsonc 时，opencode 先读 json 再读 jsonc（后者覆盖）：
    # 只有 jsonc 就写 jsonc；两份都有且 jsonc 里已有 mcp 段时拒绝写入，避免“成功但不生效”。
    write_target = target
    if discover_jsonc and target.name == "opencode.json":
        jsonc_path = target.with_name("opencode.jsonc")
        if jsonc_path.is_file():
            if not target.is_file():
                write_target = jsonc_path
            else:
                try:
                    jsonc_data, _ = parse_config_text(jsonc_path.read_text(encoding="utf-8-sig"))
                except Exception as e:  # noqa: BLE001
                    report.update(
                        error=f"The opencode.jsonc in the same directory cannot be parsed: {e}",
                        hint="opencode.jsonc overrides opencode.json; fix it first, or merge the snippet below by hand",
                    )
                    return report
                if isinstance(jsonc_data.get("mcp"), dict):
                    report.update(
                        error="Both opencode.json and opencode.jsonc exist, and the jsonc already has an mcp section (it overrides the json); "
                              "writing would appear to succeed but not take effect",
                        hint=f"Keep only one config file, or merge the snippet below into {jsonc_path.name} by hand",
                    )
                    return report
    report["path"] = str(write_target)

    exists = write_target.is_file()
    data: Dict[str, Any] = {}
    lenient = False
    if exists:
        try:
            data, lenient = parse_config_text(write_target.read_text(encoding="utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            report.update(error=f"The existing config cannot be parsed: {e}", hint="To avoid deleting anything, merge the snippet below into the config by hand")
            return report
        if lenient and not force:
            report.update(
                error="The existing config has comments / trailing commas (JSONC); rewriting would lose the comments, so it was left untouched",
                hint=f"If that is acceptable, rerun with --force (it first backs up to {write_target.name}.bak)",
            )
            return report
    if write_target is not target:
        report["warnings"].append("opencode.json not found; wrote opencode.jsonc instead (it has higher priority)")

    mcp = data.get("mcp")
    if mcp is not None and not isinstance(mcp, dict):
        report.update(action="error", error="The mcp field in the config is not an object; nothing was changed", hint="Clean up the mcp field by hand and retry")
        return report
    mcp = dict(mcp or {})

    # 重装只更新安装器负责的字段（type / command / 模块回退的 cwd 与 PYTHONPATH），
    # 保留同名条目里用户自己的 environment、timeout、enabled 等设置。
    existing_entry = mcp.get(name)
    merged = entry
    if isinstance(existing_entry, dict):
        merged = dict(existing_entry)
        merged["type"] = entry["type"]
        merged["command"] = entry["command"]
        if server.get("kind") == "module":
            env = dict(merged.get("environment") or {})
            for k, v in (entry.get("environment") or {}).items():
                if k == "PYTHONPATH":
                    # 当前安装路径优先，同时保留用户的额外模块路径；重复安装不再追加。
                    paths = env[k].split(os.pathsep) if env.get(k) else []
                    env[k] = os.pathsep.join([v, *(p for p in paths if p != v)])
                else:
                    env.setdefault(k, v)
            if env:
                merged["environment"] = env
            if entry.get("cwd"):
                merged["cwd"] = entry["cwd"]
    report["entry"] = merged
    report["snippet"] = render_snippet(merged, name=name)

    if exists and existing_entry == merged:
        report.update(ok=True, action="unchanged")
        return report

    data.setdefault("$schema", OPENCODE_SCHEMA)
    mcp[name] = merged
    data["mcp"] = mcp

    backup: Optional[Path] = None
    if exists:
        backup = write_target.with_name(write_target.name + ".bak")
        shutil.copy2(write_target, backup)
    write_target.parent.mkdir(parents=True, exist_ok=True)
    write_target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if lenient:
        report["warnings"].append(f"Comments / trailing commas in the original config were not preserved; rewritten as plain JSON (backup: {backup})")
    if isinstance(existing_entry, dict) and existing_entry.get("enabled") is False:
        report["warnings"].append("The existing entry has enabled=false, so opencode will not load it; that field was left unchanged")
    if server.get("kind") == "module":
        report["warnings"].append("No autoclip command on PATH; fell back to python -m backend.mcp_server (depends on cwd / PYTHONPATH)")

    report.update(ok=True, action="updated" if exists else "created", backup=str(backup) if backup else None)
    return report
