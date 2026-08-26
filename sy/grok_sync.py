"""Grok CLI 客户端配置同步（~/.grok/config.toml 的 model 段）。

与 codex_sync / claude_sync 对称，为 Grok CLI（grok-build）提供两种模式：
  - local-direct：恢复项目介入前的本机原配置，Grok CLI 直连原端点；
  - grok：在 config.toml 写入受管模型段 [model."switchyard"]，指向本路由
    4100 的 OpenAI Responses 兼容端点，并把 [models].default / web_search
    都设为该段。

Grok CLI 会把请求体 model 字段设为受管段的 model（grok-4.6 等客户端模型），
路由端按 model_map 把 grok 客户端模型归入通用 grok 池。本模块管理受管段、
[models].default 与 [models].web_search（都指向受管段），并清掉浮生搜索相关
段；config.toml 的其它内容（用户自定义模型、其余 mcp_servers、ui、
marketplace 等）逐行原样保留。

快照/恢复与 claude_sync 同风格：介入前一次性快照到 data/grok-backup/original，
写入一律同目录 tmp + replace 且权限 0o600，恢复前留 restore-point 备份。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sy.codex_sync import (
    _atomic_write,
    _is_section_header,
    _key_of,
    _read_text_file,
    _router_master_key,
    _scan_line,
    _secure_mkdir,
    _section_name,
    _trim,
)
from sy.const import GROK_CLIENT_MODELS, GROK_POOL, provider_base_url

log = logging.getLogger("switchyard.grok_sync")

ROUTER_ROOT = Path(__file__).resolve().parent.parent
DATA = ROUTER_ROOT / "data"
BACKUP_DIR = DATA / "grok-backup"
ORIGINAL_DIR = BACKUP_DIR / "original"
ORIGINAL_MANIFEST = ORIGINAL_DIR / "manifest.json"
MANIFEST = BACKUP_DIR / "manifest.json"

LOCAL_DIRECT = "local-direct"
GROK_KNOWN = set(GROK_CLIENT_MODELS)

# 旧受管段名；思考强度菜单按模型 id 查找，所以现改写成 [model."<slug>"]。
LEGACY_MANAGED_KEY = "switchyard"
MANAGED_CONTEXT_WINDOW = 500000
MANAGED_BASE_URL = provider_base_url()

# 切到 grok 池时一并清掉，避免从本机快照再把浮生搜索带回来
DROPPED_SECTIONS = (
    "model.grok-4.5-fusheng",
    "mcp_servers.fusheng-search",
    f"model.{LEGACY_MANAGED_KEY}",
)


def grok_home() -> Path:
    """Grok 配置目录（GROK_HOME 优先，默认 ~/.grok）。"""
    return Path(os.environ.get("GROK_HOME") or (Path.home() / ".grok")).expanduser()


def config_path() -> Path:
    return grok_home() / "config.toml"


def is_grok_model(name: str) -> bool:
    """是否 Grok 客户端模型（如 grok-4.6），用于把请求模型归入 grok 池。"""
    m = (name or "").strip()
    if not m:
        return False
    if m in GROK_KNOWN:
        return True
    return m.startswith("grok-")


def is_grok_pool(name: str) -> bool:
    """是否 Grok 路由池名（grok 或 grok- 前缀）。"""
    m = (name or "").strip()
    return bool(m) and (m == GROK_POOL or m.startswith("grok-"))


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_manifest() -> dict:
    return _read_json(MANIFEST, {})


def _save_manifest(data: dict) -> None:
    _secure_mkdir(BACKUP_DIR)
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    MANIFEST.chmod(0o600)


def _copy_file_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    _secure_mkdir(dst.parent)
    shutil.copy2(src, dst)
    try:
        dst.chmod(0o600)
    except OSError:
        pass
    return True


def _normalize_toml_text(text: str) -> str:
    """去掉 Windows 记事本/部分客户端写入的 BOM，空文件当空表。"""
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    return text


def _read_config_text(path: Path) -> str:
    """读 Grok config.toml：兼容 UTF-8 BOM 与 UTF-16（Windows 常见）。"""
    return _normalize_toml_text(_read_text_file(path))


def _parse_config(text: str) -> dict:
    """解析 TOML；损坏直接抛中文 RuntimeError（调用方不得半改文件）。"""
    text = _normalize_toml_text(text)
    if not text.strip():
        return {}
    try:
        obj = tomllib.loads(text)
    except Exception as exc:
        raise RuntimeError(f"解析 Grok config.toml 失败: {exc}") from exc
    if not isinstance(obj, dict):
        raise RuntimeError("Grok config.toml 顶层必须是 TOML 表")
    return obj


def ensure_original_snapshot() -> dict:
    """一次性快照项目介入前的 Grok config.toml（"介入前"基准，永久保留）。"""
    if ORIGINAL_MANIFEST.exists():
        return _read_json(ORIGINAL_MANIFEST, {})

    # 防污染（同 claude_sync 守卫）：恢复是全文件覆盖且不可逆，只允许在
    # 默认配置目录拍快照；GROK_HOME 指向别处时拒绝，避免快照污染真实配置。
    if str(grok_home().resolve()) != str((Path.home() / ".grok").resolve()):
        raise RuntimeError(
            f"拒绝在非默认配置目录拍原始快照: {grok_home()}（GROK_HOME 指向了别处）"
        )

    sp = config_path()
    info: dict[str, Any] = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "grok_home": str(grok_home()),
        "original_config_existed": sp.exists(),
    }
    _secure_mkdir(ORIGINAL_DIR)
    if sp.exists():
        _copy_file_if_exists(sp, ORIGINAL_DIR / "config.toml")
    ORIGINAL_MANIFEST.write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ORIGINAL_MANIFEST.chmod(0o600)
    log.info("original grok config snapshot created: %s", info)
    return info


def current_grok_slug() -> Optional[str]:
    """manifest 记住的上次 grok 客户端模型；不是已知 grok 模型则返回 None。"""
    slug = _load_manifest().get("model_slug")
    if is_grok_model(slug or ""):
        return str(slug)
    return None


def _managed_key(model_slug: str) -> str:
    return str(model_slug or "").strip() or next(iter(GROK_KNOWN))


def _is_dropped_section(hdr: str, model_slug: str = "") -> bool:
    keys = {LEGACY_MANAGED_KEY}
    slug = str(model_slug or "").strip()
    if slug:
        keys.add(slug)
    for key in keys:
        prefix = f"model.{key}"
        if hdr == prefix or hdr.startswith(prefix + "."):
            return True
    for prefix in DROPPED_SECTIONS:
        if hdr == prefix or hdr.startswith(prefix + "."):
            return True
    return False


def _toml_str(value: str) -> str:
    """转义 TOML 基本字符串，避免 key/url 中的引号或反斜杠截断配置。"""
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


_MANAGED_REASONING_EFFORTS = (
    ("low", "Low", False),
    ("medium", "Medium", False),
    ("high", "High", False),
    ("xhigh", "Xhigh", True),
)


def _managed_block(model_slug: str) -> list[str]:
    key = _managed_key(model_slug)
    lines = [
        f"[model.{_toml_str(key)}]",
        f"model = {_toml_str(key)}",
        f"name = {_toml_str(key)}",
        f"base_url = {_toml_str(MANAGED_BASE_URL)}",
        f"api_key = {_toml_str(_router_master_key())}",
        'api_backend = "responses"',
        "supports_backend_search = true",
        "supports_reasoning_effort = true",
        'reasoning_effort = "xhigh"',
        f"context_window = {MANAGED_CONTEXT_WINDOW}",
    ]
    for value, label, default in _MANAGED_REASONING_EFFORTS:
        lines.extend(
            [
                "",
                f"[[model.{_toml_str(key)}.reasoning_efforts]]",
                f"value = {_toml_str(value)}",
                f"label = {_toml_str(label)}",
            ]
        )
        if default:
            lines.append("default = true")
    return lines


def transform_config_toml(text: str, model_slug: str) -> tuple[str, list[str]]:
    """写入受管段，并把 [models].default / web_search 都指向它。

    同时清掉浮生搜索模型段与 MCP。返回 (new_text, change_report)。
    除上述受管字段外逐行保留原文件；编辑前先整体解析校验，损坏直接抛
    RuntimeError，不做任何部分修改。
    """
    if text.strip():
        _parse_config(text)

    managed_key = _managed_key(model_slug)
    lines = text.splitlines()
    out: list[str] = []
    report: list[str] = []
    depth = 0
    mlstate = ""
    cur_section = ""
    skip_section = False
    default_seen = False
    web_search_seen = False
    models_seen = False
    i = 0
    n = len(lines)

    def advance(line: str) -> None:
        nonlocal depth, mlstate
        depth, mlstate = _scan_line(line, depth, mlstate)

    while i < n:
        line = lines[i]
        if _is_section_header(line, depth, mlstate):
            hdr = _section_name(line)
            cur_section = hdr
            skip_section = _is_dropped_section(hdr, managed_key)
            if skip_section:
                if hdr.startswith("model.grok-4.5-fusheng") or hdr.startswith(
                    "mcp_servers.fusheng-search"
                ):
                    report.append(f"移除浮生搜索段 [{hdr}]")
                else:
                    report.append(f"移除受管段 [{hdr}]")
                advance(line)
                i += 1
                continue
            if hdr == "models":
                models_seen = True
            out.append(line)
            advance(line)
            i += 1
            continue

        if skip_section:
            advance(line)
            i += 1
            continue

        # 只改写 [models] 段的 default / web_search（单行赋值）
        if cur_section == "models" and not mlstate and depth == 0:
            k = _key_of(line)
            if k in ("default", "web_search") and "=" in line:
                indent = line[: len(line) - len(line.lstrip())]
                new_line = f"{indent}{k} = {_toml_str(managed_key)}"
                if new_line != line:
                    report.append(f"改写 [models] {k} → {managed_key}")
                out.append(new_line)
                if k == "default":
                    default_seen = True
                else:
                    web_search_seen = True
                advance(line)
                i += 1
                continue

        out.append(line)
        advance(line)
        i += 1

    missing_models_keys: list[tuple[str, str]] = []
    if not default_seen:
        missing_models_keys.append(("default", f"补全 [models] default = {managed_key}"))
    if not web_search_seen:
        missing_models_keys.append(("web_search", f"补全 [models] web_search = {managed_key}"))
    if missing_models_keys:
        if models_seen:
            insert_at = None
            for idx, l in enumerate(out):
                if _is_section_header(l, 0, "") and _section_name(l) == "models":
                    insert_at = idx + 1
                    break
            if insert_at is not None:
                for offset, (key, note) in enumerate(missing_models_keys):
                    out.insert(insert_at + offset, f"{key} = {_toml_str(managed_key)}")
                    report.append(note)
            else:
                out.extend(["", "[models]"])
                for key, note in missing_models_keys:
                    out.append(f"{key} = {_toml_str(managed_key)}")
                    report.append(note.replace("补全", "新增", 1))
        else:
            out.extend(["", "[models]"])
            for key, note in missing_models_keys:
                out.append(f"{key} = {_toml_str(managed_key)}")
                report.append(note.replace("补全", "新增", 1))

    # 移除受管段可能遗留尾部空行：先清空再追加，保证重复应用幂等
    while out and not out[-1].strip():
        out.pop()
    block = _managed_block(model_slug)
    if out:
        block = [""] + block
    out.extend(block)
    report.append(f"写入受管段 model={model_slug}")

    text_out = "\n".join(out).rstrip("\n") + "\n"
    # 终稿必须可解析，防止拼出坏 TOML
    _parse_config(text_out)
    return text_out, report


def apply_grok_pool(pool_name: str, model_slug: Optional[str] = None) -> dict:
    """配置 Grok CLI 走 4100 的 grok 池（受管段用指定客户端模型）。"""
    pool = (pool_name or "").strip()
    if not is_grok_pool(pool):
        raise ValueError(f"不是 Grok 池: {pool!r}")
    slug = (model_slug or "").strip() or current_grok_slug() or next(iter(GROK_KNOWN))
    if not is_grok_model(slug):
        raise ValueError(f"不是 Grok 客户端模型: {slug!r}")

    ensure_original_snapshot()
    sp = config_path()
    original = _read_config_text(sp) if sp.exists() else ""
    new_text, changes = transform_config_toml(original, slug)
    sp.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(sp, new_text)

    man = _load_manifest()
    man.update(
        {
            "mode": "grok",
            "pool": pool,
            "model_slug": slug,
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "changes": changes,
        }
    )
    _save_manifest(man)
    log.info("applied grok config pool=%s model=%s changes=%s", pool, slug, changes)
    return {
        "mode": "grok",
        "pool": pool,
        "model_slug": slug,
        "config_path": str(sp),
        "changes": changes,
    }


def restore_local_original() -> dict:
    """恢复项目介入前的本机原配置：Grok CLI 直连原端点，不经 4100。"""
    ensure_original_snapshot()
    orig = _read_json(ORIGINAL_MANIFEST, {})
    # 恢复是全文件覆盖且不可逆：快照损坏或目录漂移时必须拒绝。
    if not isinstance(orig, dict) or not orig.get("grok_home"):
        raise RuntimeError("原始配置快照缺失或损坏，拒绝恢复（请手动核对 config.toml）")
    if os.path.realpath(str(orig["grok_home"])) != os.path.realpath(str(grok_home())):
        raise RuntimeError(
            f"快照目录 {orig['grok_home']!r} 与当前 grok_home {grok_home()!r} "
            "不一致，拒绝恢复"
        )

    actions: list[str] = []
    sp = config_path()
    if sp.exists():
        _copy_file_if_exists(
            sp,
            BACKUP_DIR / f"restore-point-{datetime.now().strftime('%Y%m%d-%H%M%S')}.toml",
        )
    if orig.get("original_config_existed") and (ORIGINAL_DIR / "config.toml").exists():
        _copy_file_if_exists(ORIGINAL_DIR / "config.toml", sp)
        actions.append("restored config.toml")
    elif orig.get("original_config_existed") is False:
        if sp.exists():
            sp.unlink()
            actions.append("deleted config.toml (originally missing)")
    else:
        actions.append("config.toml untouched (snapshot incomplete)")

    man = _load_manifest()
    man.update(
        {
            "mode": LOCAL_DIRECT,
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actions": actions,
        }
    )
    _save_manifest(man)
    log.info("restored local original grok config: %s", actions)
    return {"mode": LOCAL_DIRECT, "config_path": str(sp), "actions": actions}


def sync_for_mode(mode: str, pool_name: Optional[str] = None) -> dict:
    """按模式分发：local-direct / grok（与 claude 的 sync_for_mode 对称）。"""
    m = (mode or "").strip()
    if m == LOCAL_DIRECT:
        return restore_local_original()
    if m == "grok":
        pool = (pool_name or "").strip() or GROK_POOL
        if not is_grok_pool(pool):
            raise ValueError(f"grok 模式必须提供 Grok 池名: {pool_name!r}")
        return apply_grok_pool(pool)
    raise ValueError(f"未知模式: {mode!r}（仅支持 {LOCAL_DIRECT} / grok）")


def status() -> dict:
    """当前 Grok 客户端配置状态（所有读取异常防御，失败返回空值）。"""
    man = _load_manifest()
    sp = config_path()
    config_exists = sp.exists()
    config_model: Optional[str] = None
    config_base_url: Optional[str] = None
    default_model: Optional[str] = None
    managed_present = False
    try:
        if config_exists:
            obj = _parse_config(_read_config_text(sp))
            models_section = obj.get("models")
            if isinstance(models_section, dict):
                default_model = models_section.get("default")
            model_map = obj.get("model")
            managed = None
            if isinstance(model_map, dict):
                slug = man.get("model_slug") or default_model
                for key in (slug, LEGACY_MANAGED_KEY):
                    if key and isinstance(model_map.get(key), dict):
                        managed = model_map[key]
                        break
                if managed is None:
                    for cand in model_map.values():
                        if not isinstance(cand, dict):
                            continue
                        if str(cand.get("base_url") or "").rstrip("/") == MANAGED_BASE_URL.rstrip(
                            "/"
                        ):
                            managed = cand
                            break
            if isinstance(managed, dict):
                managed_present = True
                config_model = managed.get("model")
                config_base_url = managed.get("base_url")
    except Exception:
        log.warning("failed to read grok config for status", exc_info=True)
    return {
        "grok_home": str(grok_home()),
        "config_exists": config_exists,
        "mode": man.get("mode") or "",
        "pool": man.get("pool") or "",
        "config_model": config_model,
        "config_base_url": config_base_url,
        "default_model": default_model,
        "managed_present": managed_present,
        "backup_mode": man.get("mode"),
        "applied_at": man.get("applied_at"),
    }
