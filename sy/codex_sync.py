#!/usr/bin/env python3
"""Apply / restore Codex config the same way as DeepSeek's official setup script.

Official TARGET / DEL keys (from codex-deepseek-setup.sh):
  TARGET: model, model_provider, preferred_auth_method, forced_login_method,
          model_reasoning_effort, model_catalog_json
  DEL_A:  profile, oss_provider, openai_base_url  (+ entire [profiles*] sections)
  DEL_B:  model_context_window, model_auto_compact_*, base_instructions, ...

Adaptation for Switch-codex:
  - Keep model_provider = "simple" so traffic still goes through :4100
  - Do NOT inject experimental_bearer_token into config (key stays on upstream)
  - Still write official models.json (tool/shell/instructions metadata)
  - Update [model_providers.simple].model to the DeepSeek slug
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from sy import db as sr_db
from sy.const import DEEPSEEK_CLIENT_MODELS, DEEPSEEK_POOL, provider_base_url

log = logging.getLogger("switchyard.codex_sync")

ROUTER_ROOT = Path(__file__).resolve().parent.parent
DATA = ROUTER_ROOT / "data"
OFFICIAL_MODELS = DATA / "deepseek-models.json"
BACKUP_DIR = DATA / "codex-backup"
BACKUP_CONFIG = BACKUP_DIR / "config.toml"
BACKUP_MODELS = BACKUP_DIR / "models.json"
MANIFEST = BACKUP_DIR / "manifest.json"

DEFAULT_MODEL = "openai"
LOCAL_DIRECT = "local-direct"
DEEPSEEK_KNOWN = set(DEEPSEEK_CLIENT_MODELS)  # 与 sy.const 保持单一事实源
MODELS_TEMPLATE_URL = "https://cdn.deepseek.com/api-docs/codex-deepseek-setup.sh"
ROUTER_CONFIG = DATA / "config.json"

# 永久保留的“介入前”快照（切回本机原配置用）
ORIGINAL_DIR = BACKUP_DIR / "original"
ORIGINAL_MANIFEST = ORIGINAL_DIR / "manifest.json"
# 离开 openai-all 模式时保存的最近状态（切回时还原 openai 期间的修改）
OPENAI_SNAP_DIR = BACKUP_DIR / "openai"
OPENAI_SNAP_CONFIG = OPENAI_SNAP_DIR / "config.toml"
OPENAI_SNAP_MODELS = OPENAI_SNAP_DIR / "models.json"
OPENAI_SNAP_MANIFEST = OPENAI_SNAP_DIR / "manifest.json"

SIMPLE_PROVIDER_BASE_URL = provider_base_url()
SIMPLE_PROVIDER_KEYS = (
    ("name", '"simple"'),
    ("base_url", f'"{SIMPLE_PROVIDER_BASE_URL}"'),
    ("wire_api", '"responses"'),
    ("requires_openai_auth", "true"),
)

TARGET_KEYS = (
    "model",
    "model_provider",
    "preferred_auth_method",
    "forced_login_method",
    "model_reasoning_effort",
    "model_catalog_json",
)
DEL_A = {"profile", "oss_provider", "openai_base_url"}
DEL_B = {
    "model_context_window",
    "model_auto_compact_token_limit",
    "model_auto_compact_token_limit_scope",
    "base_instructions",
    "model_instructions_file",
    "compact_prompt",
    "experimental_compact_prompt_file",
    "service_tier",
    "model_verbosity",
    "model_reasoning_summary",
    "plan_mode_reasoning_effort",
    "experimental_use_unified_exec_tool",
}
SKIP_SECTIONS = {
    "profiles",
    # official rewrites deepseek provider; we do not use it (simple provider instead)
    "model_providers.deepseek",
}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def config_path() -> Path:
    return codex_home() / "config.toml"


def models_path() -> Path:
    return codex_home() / "models.json"


def catalog_value() -> str:
    # Match official: use ~/.codex/models.json when CODEX_HOME unset
    if os.environ.get("CODEX_HOME"):
        return str(models_path())
    return "~/.codex/models.json"


def is_deepseek_model(name: str) -> bool:
    m = (name or "").strip()
    if not m or m == DEFAULT_MODEL:
        return False
    if m in DEEPSEEK_KNOWN:
        return True
    return m.startswith("deepseek-")


def is_deepseek_mode_active() -> bool:
    return MANIFEST.exists() and _load_manifest().get("mode") == "deepseek"


def _load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_manifest(data: dict) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    MANIFEST.chmod(0o600)


def auth_path() -> Path:
    return codex_home() / "auth.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _copy_file_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    dst.chmod(0o600)
    return True


def _router_master_key() -> str:
    try:
        cfg = sr_db.load_config_raw()
        if isinstance(cfg, dict) and cfg.get("master_key"):
            return str(cfg["master_key"])
    except Exception:
        log.warning("failed to read master key from sqlite; falling back to config.json")
    cfg = _read_json(ROUTER_CONFIG, {})
    return str(cfg.get("master_key") or "sk-switch-codex")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def ensure_original_snapshot() -> dict:
    """一次性快照项目介入前的 Codex 配置；老部署用首次 apply_deepseek 的旧备份迁移。"""
    if ORIGINAL_MANIFEST.exists():
        return _read_json(ORIGINAL_MANIFEST, {})

    info: dict[str, Any] = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "codex_home": str(codex_home()),
    }
    # 老部署：旧备份（首次 apply_deepseek 前的快照）就是最接近“介入前”的状态
    if BACKUP_CONFIG.exists():
        src_cfg = BACKUP_CONFIG
        src_models = BACKUP_MODELS if BACKUP_MODELS.exists() else None
        info["original_config_existed"] = True
        info["original_models_existed"] = src_models is not None
    else:
        src_cfg = config_path() if config_path().exists() else None
        src_models = models_path() if models_path().exists() else None
        info["original_config_existed"] = src_cfg is not None
        info["original_models_existed"] = src_models is not None
    auth = auth_path()
    info["original_auth_existed"] = auth.exists()

    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    if src_cfg:
        _copy_file_if_exists(src_cfg, ORIGINAL_DIR / "config.toml")
    if src_models:
        _copy_file_if_exists(src_models, ORIGINAL_DIR / "models.json")
    if auth.exists():
        _copy_file_if_exists(auth, ORIGINAL_DIR / "auth.json")
    ORIGINAL_MANIFEST.write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ORIGINAL_MANIFEST.chmod(0o600)
    log.info("original codex snapshot created: %s", info)
    return info


def _save_openai_snapshot_if_needed() -> None:
    """离开 openai-all 前保存当前状态，切回时能还原 openai 期间的修改。"""
    if _load_manifest().get("mode") != "openai-all":
        return
    OPENAI_SNAP_DIR.mkdir(parents=True, exist_ok=True)
    info = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "config_existed": config_path().exists(),
        "models_existed": models_path().exists(),
    }
    _copy_file_if_exists(config_path(), OPENAI_SNAP_CONFIG)
    _copy_file_if_exists(models_path(), OPENAI_SNAP_MODELS)
    OPENAI_SNAP_MANIFEST.write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    OPENAI_SNAP_MANIFEST.chmod(0o600)


def _ensure_simple_provider(text: str) -> tuple[str, list[str]]:
    """确保 config.toml 有 [model_providers.simple] 且指向 4100。"""
    lines = text.splitlines()
    sec_idx: Optional[int] = None
    body_start = 0
    for i, line in enumerate(lines):
        t = line.strip()
        if sec_idx is None and t.startswith("[") and _section_name(line) == "model_providers.simple":
            sec_idx = i
            body_start = i + 1
            continue
        if sec_idx is not None and t.startswith("["):
            break

    if sec_idx is None:
        block = ["", "[model_providers.simple]"] + [
            f"{k} = {v}" for k, v in SIMPLE_PROVIDER_KEYS
        ]
        if not text.strip():
            block = block[1:]
        lines = lines + block
        return "\n".join(lines), ["新增 [model_providers.simple] → 4100"]

    end = len(lines)
    keys_present: set[str] = set()
    for j in range(body_start, len(lines)):
        t = lines[j].strip()
        if t.startswith("["):
            end = j
            break
        k = _key_of(lines[j])
        if k:
            keys_present.add(k)
    missing = [kv for kv in SIMPLE_PROVIDER_KEYS if kv[0] not in keys_present]
    if missing:
        insert = [f"{k} = {v}" for k, v in missing]
        lines[end:end] = insert
        return "\n".join(lines), ["[model_providers.simple] 补全: " + ", ".join(k for k, _ in missing)]
    return "\n".join(lines), []


def _write_client_auth() -> bool:
    """写 ~/.codex/auth.json：客户端用 master key 访问 4100。"""
    auth = auth_path()
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text(
        json.dumps({"OPENAI_API_KEY": _router_master_key()}, indent=2) + "\n",
        encoding="utf-8",
    )
    auth.chmod(0o600)
    log.info("wrote client auth.json")
    return True


def ensure_models_template() -> None:
    """data/deepseek-models.json 缺失时，从官方 setup 脚本下载内嵌模板。"""
    if OFFICIAL_MODELS.exists():
        return
    log.info("downloading DeepSeek official models template from %s", MODELS_TEMPLATE_URL)
    try:
        with urllib.request.urlopen(MODELS_TEMPLATE_URL, timeout=30) as resp:
            script = resp.read().decode("utf-8", errors="replace")
        match = re.search(r"<<'CODEX_MODELS_JSON'\n(.*?)\nCODEX_MODELS_JSON", script, re.S)
        if not match:
            raise RuntimeError("setup 脚本中未找到 CODEX_MODELS_JSON 模板")
        data = json.loads(match.group(1))
        slugs = {m.get("slug") for m in data.get("models", [])}
        if "deepseek-v4-flash" not in slugs or "deepseek-v4-pro" not in slugs:
            raise RuntimeError("模板缺少 deepseek-v4-flash / deepseek-v4-pro")
    except Exception as exc:
        raise RuntimeError(
            f"下载 DeepSeek 官方 models 模板失败（{exc}）。"
            "请手动放置 data/deepseek-models.json（提取自官方 codex-deepseek-setup.sh）后重试。"
        ) from exc
    OFFICIAL_MODELS.parent.mkdir(parents=True, exist_ok=True)
    OFFICIAL_MODELS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    OFFICIAL_MODELS.chmod(0o600)
    log.info("saved official models template → %s", OFFICIAL_MODELS)


def _trim(s: str) -> str:
    return s.strip()


def _key_of(line: str) -> str:
    l = _trim(line)
    if not l or l.startswith("#") or "=" not in l:
        return ""
    k = _trim(l.split("=", 1)[0])
    if (k.startswith('"') and k.endswith('"')) or (k.startswith("'") and k.endswith("'")):
        k = k[1:-1]
    return k


def _is_section_header(line: str, depth: int, mlstate: str) -> bool:
    if mlstate or depth != 0:
        return False
    t = _trim(line)
    return t.startswith("[")


def _section_name(header_line: str) -> str:
    t = _trim(header_line)
    # [[array]] or [section]
    while t.startswith("["):
        t = t[1:]
    t = t.split("]", 1)[0]
    t = t.replace('"', "").replace("'", "")
    return _trim(t)


def _scan_line(line: str, depth: int, mlstate: str) -> tuple[int, str]:
    """Track bracket depth + multiline strings (same idea as official script)."""
    n = len(line)
    i = 0
    instr = ""
    while i < n:
        c = line[i]
        if mlstate:
            c3 = line[i : i + 3]
            if mlstate == "basic" and c3 == '"""':
                mlstate = ""
                i += 3
                continue
            if mlstate == "literal" and c3 == "'''":
                mlstate = ""
                i += 3
                continue
            if mlstate == "basic" and c == "\\":
                i += 2
                continue
            i += 1
            continue
        if instr:
            if instr == "basic":
                if c == "\\":
                    i += 2
                    continue
                if c == '"':
                    instr = ""
            else:
                if c == "'":
                    instr = ""
            i += 1
            continue
        c3 = line[i : i + 3]
        if c3 == '"""':
            mlstate = "basic"
            i += 3
            continue
        if c3 == "'''":
            mlstate = "literal"
            i += 3
            continue
        if c == "#":
            break
        if c == '"':
            instr = "basic"
        elif c == "'":
            instr = "literal"
        elif c == "[":
            depth += 1
        elif c == "]":
            if depth > 0:
                depth -= 1
        i += 1
    return depth, mlstate


def _target_value(key: str, model_slug: str) -> str:
    if key == "model":
        return f'"{model_slug}"'
    if key == "model_provider":
        return '"simple"'
    if key == "preferred_auth_method":
        return '"apikey"'
    if key == "forced_login_method":
        return '"api"'
    if key == "model_reasoning_effort":
        return '"high"'
    if key == "model_catalog_json":
        return f'"{catalog_value()}"'
    raise KeyError(key)


def transform_config_toml(
    text: str, model_slug: str, mode: str = "deepseek"
) -> tuple[str, list[str]]:
    """Official-style surgery on config.toml. Returns (new_text, change_report)."""
    is_ds = mode == "deepseek"
    targets = TARGET_KEYS if is_ds else ("model_provider",)
    lines = text.splitlines()
    out: list[str] = []
    report: list[str] = []
    seen: set[str] = set()
    depth = 0
    mlstate = ""
    cur_section = ""
    skip_section = False
    i = 0
    n = len(lines)
    insert_at = 0  # index in `out` after last top-level key

    def consume_assignment_from(idx: int) -> int:
        """Advance past a full assignment (possibly multi-line). Start at assignment line."""
        nonlocal depth, mlstate
        depth, mlstate = _scan_line(lines[idx], depth, mlstate)
        idx += 1
        while idx < n and (mlstate or depth != 0):
            depth, mlstate = _scan_line(lines[idx], depth, mlstate)
            idx += 1
        return idx

    while i < n:
        line = lines[i]
        # section header?
        if _is_section_header(line, depth, mlstate):
            hdr = _section_name(line)
            cur_section = hdr
            skip_section = False
            base = hdr.split(".", 1)[0] if hdr else ""
            if hdr in SKIP_SECTIONS or base == "profiles" or hdr.startswith("profiles."):
                skip_section = True
                report.append(f"删除 [{hdr}]")
                depth, mlstate = _scan_line(line, depth, mlstate)
                i += 1
                continue
            if hdr.startswith("model_providers.deepseek"):
                skip_section = True
                report.append(f"删除 [{hdr}]（改用 simple 走 4100）")
                depth, mlstate = _scan_line(line, depth, mlstate)
                i += 1
                continue
            out.append(line)
            depth, mlstate = _scan_line(line, depth, mlstate)
            i += 1
            continue

        if cur_section and skip_section:
            depth, mlstate = _scan_line(line, depth, mlstate)
            i += 1
            continue

        # inside a normal section
        if cur_section:
            k = _key_of(line) if not mlstate and depth == 0 else ""
            # Update model inside [model_providers.simple]
            if (
                is_ds
                and cur_section == "model_providers.simple"
                and k == "model"
                and not mlstate
                and depth == 0
            ):
                out.append(f'model = "{model_slug}"')
                report.append(f'[model_providers.simple] model → "{model_slug}"')
                i = consume_assignment_from(i)
                continue
            # fix wire_api chat → responses (official)
            if k == "wire_api" and not mlstate and depth == 0:
                val = _trim(line.split("=", 1)[1]) if "=" in line else ""
                if val.startswith('"chat"') or val.startswith("'chat'"):
                    indent = line[: len(line) - len(line.lstrip())]
                    out.append(f'{indent}wire_api = "responses"')
                    report.append(f"[{cur_section}] wire_api chat → responses")
                    i = consume_assignment_from(i)
                    continue
            out.append(line)
            depth, mlstate = _scan_line(line, depth, mlstate)
            i += 1
            continue

        # ---- top-level leading area ----
        k = _key_of(line) if not mlstate and depth == 0 else ""
        if k and k in targets and not mlstate and depth == 0:
            newv = _target_value(k, model_slug)
            old = _trim(line.split("=", 1)[1]) if "=" in line else ""
            out.append(f"{k} = {newv}")
            seen.add(k)
            insert_at = len(out)
            if old != newv:
                report.append(f"改写 {k}: {old} → {newv}")
            i = consume_assignment_from(i)
            continue
        if is_ds and k and k in DEL_A and not mlstate and depth == 0:
            old = _trim(line.split("=", 1)[1]) if "=" in line else ""
            report.append(f"删除 {k} = {old[:60]}")
            i = consume_assignment_from(i)
            continue
        if is_ds and k and k in DEL_B and not mlstate and depth == 0:
            old = _trim(line.split("=", 1)[1]) if "=" in line else ""
            report.append(f"删除 {k} = {old[:60]}")
            i = consume_assignment_from(i)
            continue

        out.append(line)
        if k and not mlstate and depth == 0:
            insert_at = len(out)
        depth, mlstate = _scan_line(line, depth, mlstate)
        i += 1

    missing = [k for k in targets if k not in seen]
    if missing:
        block = [f"{k} = {_target_value(k, model_slug)}" for k in missing]
        report.append("补全缺失: " + ", ".join(missing))
        # insert before first section if possible
        first_sec = next((idx for idx, l in enumerate(out) if _trim(l).startswith("[")), len(out))
        at = min(insert_at, first_sec) if insert_at else first_sec
        # blank line before section
        if at < len(out) and _trim(out[at]).startswith("["):
            block.append("")
        out = out[:at] + block + out[at:]

    # ensure trailing newline
    text_out = "\n".join(out)
    if text_out and not text_out.endswith("\n"):
        text_out += "\n"
    if not text_out:
        # brand new file
        text_out = "\n".join(f"{k} = {_target_value(k, model_slug)}" for k in targets) + "\n"
        report.append("新建 config.toml")
    text_out, provider_report = _ensure_simple_provider(text_out)
    report += provider_report
    return text_out, report


def _write_models_json() -> None:
    if not OFFICIAL_MODELS.exists():
        raise FileNotFoundError(
            f"缺少官方 models 模板: {OFFICIAL_MODELS}（应从 codex-deepseek-setup.sh 提取）"
        )
    dest = models_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(OFFICIAL_MODELS, dest)
    dest.chmod(0o600)
    log.info("wrote official models.json → %s", dest)


def apply_deepseek(model_slug: str) -> dict[str, Any]:
    """Mirror official install: models.json + config.toml surgery + 4100 provider/auth."""
    model_slug = (model_slug or "").strip()
    if not is_deepseek_model(model_slug):
        raise ValueError(f"不是 DeepSeek 模型: {model_slug!r}")

    ensure_original_snapshot()
    _save_openai_snapshot_if_needed()
    ensure_models_template()

    cfg_p = config_path()
    original = cfg_p.read_text(encoding="utf-8") if cfg_p.exists() else ""
    new_text, report = transform_config_toml(original, model_slug, mode="deepseek")
    _atomic_write(cfg_p, new_text)

    _write_models_json()
    _write_client_auth()

    man = _load_manifest()
    man.update(
        {
            "mode": "deepseek",
            "model_slug": model_slug,
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "catalog_value": catalog_value(),
            "changes": report,
        }
    )
    _save_manifest(man)
    log.info("applied deepseek codex config model=%s changes=%s", model_slug, len(report))
    return {
        "mode": "deepseek",
        "model_slug": model_slug,
        "config_path": str(cfg_p),
        "models_path": str(models_path()),
        "changes": report,
    }


def apply_openai_all() -> dict[str, Any]:
    """配置 Codex 走 4100（openai-all 池）：provider 段 + auth.json，保留用户模型与自定义。"""
    ensure_original_snapshot()
    orig = _read_json(ORIGINAL_MANIFEST, {})
    actions: list[str] = []

    # 基准：最近一次 openai 快照 > 原始快照 > 当前配置
    if OPENAI_SNAP_CONFIG.exists():
        base_text = OPENAI_SNAP_CONFIG.read_text(encoding="utf-8")
    elif (ORIGINAL_DIR / "config.toml").exists():
        base_text = (ORIGINAL_DIR / "config.toml").read_text(encoding="utf-8")
    else:
        base_text = config_path().read_text(encoding="utf-8") if config_path().exists() else ""

    new_text, report = transform_config_toml(base_text, DEFAULT_MODEL, mode="openai-all")
    _atomic_write(config_path(), new_text)
    actions += report

    mods_p = models_path()
    if OPENAI_SNAP_MODELS.exists():
        _copy_file_if_exists(OPENAI_SNAP_MODELS, mods_p)
        actions.append("restored openai models.json")
    elif orig.get("original_models_existed") and (ORIGINAL_DIR / "models.json").exists():
        _copy_file_if_exists(ORIGINAL_DIR / "models.json", mods_p)
        actions.append("restored original models.json")
    else:
        if mods_p.exists():
            mods_p.unlink()
            actions.append("deleted models.json (originally missing)")
        else:
            actions.append("models.json untouched (none)")

    _write_client_auth()
    _save_manifest(
        {
            "mode": "openai-all",
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "changes": actions,
        }
    )
    log.info("applied openai-all codex config: %s", actions)
    return {"mode": "openai-all", "config_path": str(config_path()), "changes": actions}


def restore_local_original() -> dict[str, Any]:
    """恢复项目介入前的本机原配置（config.toml / models.json / auth.json），不经 4100。"""
    ensure_original_snapshot()
    _save_openai_snapshot_if_needed()
    orig = _read_json(ORIGINAL_MANIFEST, {})
    actions: list[str] = []
    cfg_p, mods_p, auth = config_path(), models_path(), auth_path()

    if orig.get("original_config_existed") and (ORIGINAL_DIR / "config.toml").exists():
        _copy_file_if_exists(ORIGINAL_DIR / "config.toml", cfg_p)
        actions.append("restored config.toml")
    else:
        if cfg_p.exists():
            cfg_p.unlink()
            actions.append("deleted config.toml (originally missing)")

    if orig.get("original_models_existed") and (ORIGINAL_DIR / "models.json").exists():
        _copy_file_if_exists(ORIGINAL_DIR / "models.json", mods_p)
        actions.append("restored models.json")
    else:
        if mods_p.exists():
            mods_p.unlink()
            actions.append("deleted models.json (originally missing)")

    if orig.get("original_auth_existed") and (ORIGINAL_DIR / "auth.json").exists():
        _copy_file_if_exists(ORIGINAL_DIR / "auth.json", auth)
        actions.append("restored auth.json")
    else:
        if auth.exists():
            auth.unlink()
            actions.append("deleted auth.json (originally missing)")

    _save_manifest(
        {
            "mode": LOCAL_DIRECT,
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actions": actions,
        }
    )
    log.info("restored local original codex config: %s", actions)
    return {"mode": LOCAL_DIRECT, "actions": actions}


def sync_for_active_model(active_model: str) -> dict[str, Any]:
    """Entry point for router UI / API."""
    active = (active_model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if active == LOCAL_DIRECT:
        return restore_local_original()
    if active == DEFAULT_MODEL:
        return apply_openai_all()
    if active == DEEPSEEK_POOL:
        return apply_deepseek_pool()
    if is_deepseek_model(active):
        return apply_deepseek(active)
    # other custom pools: do not touch codex (routing only)
    return {
        "mode": "routing-only",
        "model_slug": active,
        "note": "非 DeepSeek / 非 openai / 非本机原配置：只切换路由池，不改 Codex 配置",
    }


def status() -> dict[str, Any]:
    man = _load_manifest()
    cfg = config_path()
    model = None
    provider = None
    if cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if _trim(line).startswith("[") and not line.strip().startswith("#"):
                # only top-level until first section — rough
                pass
        # simple top-level scan
        depth = 0
        ml = ""
        in_top = True
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if in_top and _is_section_header(line, depth, ml):
                in_top = False
                continue
            if not in_top:
                continue
            k = _key_of(line)
            if k == "model" and "=" in line:
                model = _trim(line.split("=", 1)[1]).strip('"').strip("'")
            if k == "model_provider" and "=" in line:
                provider = _trim(line.split("=", 1)[1]).strip('"').strip("'")
            depth, ml = _scan_line(line, depth, ml)
    return {
        "codex_home": str(codex_home()),
        "config_exists": cfg.exists(),
        "models_json_exists": models_path().exists(),
        "mode": man.get("mode") or "",
        "backup_mode": man.get("mode"),
        "backup_model_slug": man.get("model_slug"),
        "config_model": model,
        "config_provider": provider,
    }


def _current_top_model() -> Optional[str]:
    """读取 config.toml 顶层 model（不进入任何 section）。"""
    cfg = config_path()
    if not cfg.exists():
        return None
    depth = 0
    ml = ""
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if _is_section_header(line, depth, ml):
            break
        k = _key_of(line)
        if k == "model" and "=" in line:
            val = _trim(line.split("=", 1)[1]).strip('"').strip("'")
            if val:
                return val
        depth, ml = _scan_line(line, depth, ml)
    return None


def apply_deepseek_pool() -> dict[str, Any]:
    """整体 deepseek 模式：写 provider + 含 flash/pro 的 models.json，保留当前 DeepSeek 模型作为默认。"""
    ensure_original_snapshot()
    _save_openai_snapshot_if_needed()
    ensure_models_template()
    slug = _current_top_model()
    if not is_deepseek_model(slug or ""):
        slug = "deepseek-v4-flash"
    return apply_deepseek(slug)
