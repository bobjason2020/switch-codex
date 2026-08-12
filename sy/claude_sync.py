#!/usr/bin/env python3
"""Claude Code 客户端配置同步（~/.claude/settings.json 的 env 段）。

与 codex_sync.py 对称，为 Claude Code 客户端提供三种模式：
  - local-direct：恢复项目介入前的本机原配置，Claude Code 直连原端点；
  - openai-all：env 指向本路由 4100 的 Anthropic 兼容端点，模型 openai-all；
  - deepseek：env 指向本路由 4100，模型为 DeepSeek slug（如 deepseek-v4-flash）。

Claude Code 会把 settings.json 中 env 键的内容注入为环境变量；本模块只管理
MANAGED_ENV_KEYS 列出的键，settings.json 的其它内容（顶层键、其它 env 键）原样保留。
路由的 Anthropic base URL 不带 /v1（Claude Code 会自动拼接 /v1/messages），
认证 token 为路由 master key（客户端与 4100 用同一把 key）。

本模块还负责 claude-auto-mode-bridge 的 PreToolUse hook 安装/卸载：
hook 文件落在 claude_home()/auto-mode-bridge，随 env 应用一并安装，
随快照恢复仅移除 settings.json 中的注册（bridge 目录保留，rules.json 可能有用户修改）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from sy.codex_sync import _router_master_key, is_deepseek_model
from sy.const import DEEPSEEK_CLIENT_MODELS, provider_base_url

log = logging.getLogger("switchyard.claude_sync")

ROUTER_ROOT = Path(__file__).resolve().parent.parent
DATA = ROUTER_ROOT / "data"
BACKUP_DIR = DATA / "claude-backup"
ORIGINAL_DIR = BACKUP_DIR / "original"
ORIGINAL_MANIFEST = ORIGINAL_DIR / "manifest.json"
MANIFEST = BACKUP_DIR / "manifest.json"

DEFAULT_MODEL = "openai"  # 与 codex_sync 一致
LOCAL_DIRECT = "local-direct"
DEEPSEEK_KNOWN = set(DEEPSEEK_CLIENT_MODELS)  # 与 sy.const 保持单一事实源

# 本模块只管理这些 env 键；settings.json 的其它内容必须原样保留
MANAGED_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)

# claude-auto-mode-bridge：PreToolUse hook 的 vendored 源与目标目录名
BRIDGE_SRC = ROUTER_ROOT / "sy" / "bridge"
BRIDGE_DIR_NAME = "auto-mode-bridge"


def _router_anthropic_base() -> str:
    """路由的 Anthropic 兼容 base URL（不带 /v1，Claude Code 会自动拼 /v1/messages）。

    provider_base_url() 尾部是 /v1 时直接去掉；否则去掉最后一个 path 段；
    保证以 "/" 结尾（如 http://127.0.0.1:4100/）。
    """
    u = urlsplit(provider_base_url().strip())
    path = u.path
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    elif "/" in path.strip("/"):
        segs = path.rstrip("/").split("/")
        path = "/".join(segs[:-1])
    return urlunsplit((u.scheme, u.netloc, path or "/", u.query, u.fragment))


ROUTER_ANTHROPIC_BASE = _router_anthropic_base()


def claude_home() -> Path:
    """Claude Code 配置目录（CLAUDE_CONFIG_DIR 优先，默认 ~/.claude）。"""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()


def settings_path() -> Path:
    return claude_home() / "settings.json"


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
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    MANIFEST.chmod(0o600)


def _copy_file_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    dst.chmod(0o600)
    return True


def _atomic_write(path: Path, text: str) -> None:
    """原子写：同目录 tmp 文件 + replace（仿 codex_sync._atomic_write）。"""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _read_settings() -> dict:
    """读 settings.json；文件缺失返回 {}，JSON 解析失败抛 RuntimeError（中文信息）。"""
    p = settings_path()
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"解析 Claude settings.json 失败: {p}（{exc}）") from exc
    if not isinstance(obj, dict):
        raise RuntimeError(f"Claude settings.json 顶层必须是 JSON 对象: {p}")
    return obj


def _write_settings(obj: dict) -> None:
    """写 settings.json：indent=2（ensure_ascii=False）、权限 0o600、原子替换。"""
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _bridge_dir() -> Path:
    """auto-mode-bridge 目录：claude_home()/auto-mode-bridge（尊重 CLAUDE_CONFIG_DIR）。"""
    return claude_home() / BRIDGE_DIR_NAME


def _hook_command() -> str:
    """PreToolUse hook 的 command：sys.executable + classifier.py 绝对路径（反斜杠换 /）。"""
    return f"{sys.executable} {_bridge_dir() / 'classifier.py'}".replace("\\", "/")


def _bridge_installed() -> bool:
    """settings.json 是否已注册 auto-mode-bridge 的 PreToolUse hook。

    任一 hooks.PreToolUse 条目（含 "hooks" 子列表的 dict）里的 command 含
    "auto-mode-bridge" 子串即视为已安装；settings 缺失或读取异常按未安装处理。
    """
    try:
        obj = _read_settings()
    except Exception:
        return False
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        return False
    pre = hooks.get("PreToolUse")
    if not isinstance(pre, list):
        return False
    for entry in pre:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            continue
        for h in entry["hooks"]:
            if isinstance(h, dict) and "auto-mode-bridge" in str(h.get("command", "")):
                return True
    return False


def install_hook() -> list[str]:
    """安装 auto-mode-bridge 的 PreToolUse hook（幂等，返回中文变更报告）。

    classifier.py 总是跟随 vendored 更新拷贝；rules.json / LICENSE 仅目标缺失时
    拷贝（不覆盖用户修改）。settings.json 的 hooks.PreToolUse 先剔除所有含
    auto-mode-bridge 的旧条目再追加新条目，其余键原样保留；无变化时不重写文件。
    """
    report: list[str] = []
    dst_dir = _bridge_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)

    classifier_src = BRIDGE_SRC / "classifier.py"
    classifier_dst = dst_dir / "classifier.py"
    existed = classifier_dst.exists()
    shutil.copy2(classifier_src, classifier_dst)
    report.append("更新 classifier.py" if existed else "拷贝 classifier.py")

    for name in ("rules.json", "LICENSE"):
        dst = dst_dir / name
        if not dst.exists():
            shutil.copy2(BRIDGE_SRC / name, dst)
            report.append(f"首次拷贝 {name}")

    obj = _read_settings()
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        obj["hooks"] = hooks
    pre = hooks.get("PreToolUse")
    if not isinstance(pre, list):
        pre = []
        hooks["PreToolUse"] = pre

    new_command = _hook_command()
    new_entry = {"matcher": "*", "hooks": [{"type": "command", "command": new_command, "timeout": 15}]}

    def _is_bridge_entry(entry: Any) -> bool:
        return (
            isinstance(entry, dict)
            and isinstance(entry.get("hooks"), list)
            and any(
                isinstance(h, dict) and "auto-mode-bridge" in str(h.get("command", ""))
                for h in entry["hooks"]
            )
        )

    bridge_entries = [e for e in pre if _is_bridge_entry(e)]
    kept = [e for e in pre if not _is_bridge_entry(e)]
    final = kept + [new_entry]
    if final == pre:
        report.append("hook 已就绪")
        return report
    hooks["PreToolUse"] = final
    _write_settings(obj)
    if bridge_entries:
        report.append("移除旧 hook 条目")
    report.append("安装 hook")
    return report


def uninstall_hook() -> list[str]:
    """移除 settings.json 中 auto-mode-bridge 的 PreToolUse hook（不动 bridge 目录）。

    只剔除含 auto-mode-bridge 的条目；PreToolUse 空列表时清理该键、
    hooks 空 dict 一并清理，其余键原样保留。有变更才写文件。
    返回中文变更报告；未安装时返回 []。
    """
    obj = _read_settings()
    hooks = obj.get("hooks")
    if not isinstance(hooks, dict):
        return []
    pre = hooks.get("PreToolUse")
    if not isinstance(pre, list):
        return []
    kept: list[Any] = []
    removed = False
    for entry in pre:
        if isinstance(entry, dict) and isinstance(entry.get("hooks"), list) and any(
            isinstance(h, dict) and "auto-mode-bridge" in str(h.get("command", ""))
            for h in entry["hooks"]
        ):
            removed = True
            continue
        kept.append(entry)
    if not removed:
        return []
    if kept:
        hooks["PreToolUse"] = kept
    else:
        hooks.pop("PreToolUse", None)
    if not hooks:
        obj.pop("hooks", None)
    _write_settings(obj)
    return ["移除 auto-mode-bridge hook"]


def ensure_original_snapshot() -> dict:
    """一次性快照项目介入前的 Claude settings.json（"介入前"基准，永久保留）。"""
    if ORIGINAL_MANIFEST.exists():
        return _read_json(ORIGINAL_MANIFEST, {})

    # 防污染（2026-08-09 故障教训）：只允许在默认配置目录下拍快照。
    # CLAUDE_CONFIG_DIR 指向测试目录时会拍下占位配置，之后 restore_local_original
    # 会把真实配置（statusLine/插件等）整个覆盖掉且不可逆。
    if str(claude_home().resolve()) != str((Path.home() / ".claude").resolve()):
        raise RuntimeError(
            f"拒绝在非默认配置目录拍原始快照: {claude_home()}（CLAUDE_CONFIG_DIR 指向了别处）"
        )

    sp = settings_path()
    info: dict[str, Any] = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "claude_home": str(claude_home()),
        "original_settings_existed": sp.exists(),
    }
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    if sp.exists():
        _copy_file_if_exists(sp, ORIGINAL_DIR / "settings.json")
    ORIGINAL_MANIFEST.write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ORIGINAL_MANIFEST.chmod(0o600)
    log.info("original claude settings snapshot created: %s", info)
    return info


def _managed_env(model_slug: str) -> dict[str, str]:
    """走 4100 的 env 集合：Anthropic 兼容端点 + 路由 master key + 指定模型。"""
    return {
        "ANTHROPIC_BASE_URL": ROUTER_ANTHROPIC_BASE,
        "ANTHROPIC_AUTH_TOKEN": _router_master_key(),
        "ANTHROPIC_MODEL": model_slug,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model_slug,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model_slug,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model_slug,
    }


# 值不允许明文进日志/UI 的 env 键（master key 只进 settings.json 本体）
SECRET_ENV_KEYS = frozenset({"ANTHROPIC_AUTH_TOKEN"})


def _mask_secret(value: Any) -> str:
    """脱敏：超短值整体打码，否则只留首尾 4 字符（防 master key 泄漏进日志与 UI）。"""
    s = str(value)
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}…{s[-4:]}"


def _apply_env(env_target: dict[str, str]) -> list[str]:
    """把 env_target 合并进 settings.json 的 env，生成 changes 报告并写文件。

    只触碰 env_target 中的键（均在 MANAGED_ENV_KEYS 内），其它键原样保留；
    原本 env 不存在时创建 env 键。无变化时不重写文件（保留用户原有格式）。
    changes 报告对 SECRET_ENV_KEYS 脱敏（真实值只写入 settings.json 本体）。
    """
    obj = _read_settings()
    changes: list[str] = []
    env = obj.get("env")
    if not isinstance(env, dict):
        env = {}
        obj["env"] = env
        changes.append("创建 env 段")
    for key, new in env_target.items():
        old = env.get(key)
        if old == new:
            continue
        if old is None:
            shown = _mask_secret(new) if key in SECRET_ENV_KEYS else new
            changes.append(f"补全 env {key}: {shown}")
        else:
            old_shown = _mask_secret(old) if key in SECRET_ENV_KEYS else old
            shown = _mask_secret(new) if key in SECRET_ENV_KEYS else new
            changes.append(f"改写 {key}: {old_shown} → {shown}")
        env[key] = new
    if changes:
        _write_settings(obj)
    return changes


def apply_openai_all() -> dict:
    """配置 Claude Code 走 4100 的 openai-all 池（Anthropic 兼容端点）。"""
    ensure_original_snapshot()
    changes = _apply_env(_managed_env(DEFAULT_MODEL))
    hook_report: list[str] = []
    try:
        hook_report = install_hook()
    except Exception:
        log.warning("install auto-mode-bridge hook failed (env 已应用)", exc_info=True)
    changes = changes + hook_report
    man = _load_manifest()
    man.update(
        {
            "mode": "openai-all",
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "changes": changes,
        }
    )
    _save_manifest(man)
    log.info("applied openai-all claude settings: %s", changes)
    return {"mode": "openai-all", "settings_path": str(settings_path()), "changes": changes}


def apply_deepseek(model_slug: str) -> dict:
    """配置 Claude Code 走 4100 的 DeepSeek 池（模型为 model_slug）。"""
    model_slug = (model_slug or "").strip()
    if not is_deepseek_model(model_slug):
        raise ValueError(f"不是 DeepSeek 模型: {model_slug!r}")
    ensure_original_snapshot()
    env_target = _managed_env(model_slug)
    env_target["CLAUDE_CODE_SUBAGENT_MODEL"] = model_slug
    changes = _apply_env(env_target)
    hook_report: list[str] = []
    try:
        hook_report = install_hook()
    except Exception:
        log.warning("install auto-mode-bridge hook failed (env 已应用)", exc_info=True)
    changes = changes + hook_report
    man = _load_manifest()
    man.update(
        {
            "mode": "deepseek",
            "model_slug": model_slug,
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "changes": changes,
        }
    )
    _save_manifest(man)
    log.info("applied deepseek claude settings model=%s: %s", model_slug, changes)
    return {
        "mode": "deepseek",
        "model_slug": model_slug,
        "settings_path": str(settings_path()),
        "changes": changes,
    }


def restore_local_original() -> dict:
    """恢复项目介入前的本机原配置：Claude Code 直连原端点，不经 4100。"""
    ensure_original_snapshot()
    orig = _read_json(ORIGINAL_MANIFEST, {})
    # 恢复是全文件覆盖且不可逆：快照损坏或目录漂移时必须拒绝，否则会把
    # 快照覆盖到错误的 claude_home（快照侧有目录守卫，恢复侧此前没有；
    # manifest 损坏时 _read_json 返回 {}，会误判"原本无配置"而删掉 settings.json）。
    if not isinstance(orig, dict) or not orig.get("claude_home"):
        raise RuntimeError("原始配置快照缺失或损坏，拒绝恢复（请手动核对 settings.json）")
    if os.path.realpath(str(orig["claude_home"])) != os.path.realpath(str(claude_home())):
        raise RuntimeError(
            f"快照目录 {orig['claude_home']!r} 与当前 claude_home {claude_home()!r} "
            "不一致，拒绝恢复"
        )
    actions: list[str] = []
    # 覆盖 settings.json 之前探测 hook 注册情况（只读，不触碰 bridge 目录）
    had_hook = _bridge_installed()
    sp = settings_path()
    # 恢复前备份当前配置（restore 是全文件覆盖且不可逆），误恢复后仍可找回
    if sp.exists():
        _copy_file_if_exists(
            sp, BACKUP_DIR / f"restore-point-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        )
    if orig.get("original_settings_existed") and (ORIGINAL_DIR / "settings.json").exists():
        _copy_file_if_exists(ORIGINAL_DIR / "settings.json", sp)
        actions.append("restored settings.json")
    else:
        if sp.exists():
            sp.unlink()
            actions.append("deleted settings.json (originally missing)")
    # 快照恢复是整文件覆盖，settings.json 中的 hook 注册随之消失；
    # bridge 目录保留（rules.json 可能有用户修改）
    if had_hook:
        actions.append("移除 hook（随快照恢复）")
    man = _load_manifest()
    man.update(
        {
            "mode": LOCAL_DIRECT,
            "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "actions": actions,
        }
    )
    _save_manifest(man)
    log.info("restored local original claude settings: %s", actions)
    return {"mode": LOCAL_DIRECT, "settings_path": str(sp), "actions": actions}


def sync_for_mode(mode: str, model_slug: Optional[str] = None) -> dict:
    """按模式分发：local-direct / openai-all / deepseek（与 codex 的 sync_for_active_model 对称）。"""
    m = (mode or "").strip()
    if m == LOCAL_DIRECT:
        return restore_local_original()
    if m == DEFAULT_MODEL or m == "openai-all":
        return apply_openai_all()
    if m == "deepseek":
        if not is_deepseek_model(model_slug or ""):
            raise ValueError(f"deepseek 模式必须提供 DeepSeek 模型 slug: {model_slug!r}")
        return apply_deepseek(model_slug or "")
    raise ValueError(f"未知模式: {mode!r}（仅支持 {LOCAL_DIRECT} / {DEFAULT_MODEL} / deepseek）")


def status() -> dict:
    """当前 Claude 客户端配置状态（所有读取异常防御，失败返回空值）。"""
    man = _load_manifest()
    sp = settings_path()
    config_base_url: Optional[str] = None
    config_model: Optional[str] = None
    try:
        if sp.exists():
            obj = json.loads(sp.read_text(encoding="utf-8"))
            env = obj.get("env") if isinstance(obj, dict) else None
            if isinstance(env, dict):
                config_base_url = env.get("ANTHROPIC_BASE_URL")
                config_model = env.get("ANTHROPIC_MODEL")
    except Exception:
        log.warning("failed to read claude settings for status", exc_info=True)
    try:
        bridge = {
            "installed": _bridge_installed(),
            "command": _hook_command(),
            "rules_present": (_bridge_dir() / "rules.json").exists(),
            "bridge_dir": str(_bridge_dir()),
        }
    except Exception:
        log.warning("failed to probe auto-mode-bridge for status", exc_info=True)
        bridge = {}
    return {
        "claude_home": str(claude_home()),
        "settings_exists": sp.exists(),
        "mode": man.get("mode") or "",
        "config_base_url": config_base_url,
        "config_model": config_model,
        "backup_mode": man.get("mode"),
        "applied_at": man.get("applied_at"),
        "bridge": bridge,
    }


def current_deepseek_slug() -> Optional[str]:
    """读当前 Claude settings 里的 ANTHROPIC_MODEL；是 DeepSeek slug 才返回。"""
    try:
        if settings_path().exists():
            obj = json.loads(settings_path().read_text(encoding="utf-8"))
            env = obj.get("env") if isinstance(obj, dict) else None
            model = env.get("ANTHROPIC_MODEL") if isinstance(env, dict) else None
            if is_deepseek_model(model or ""):
                return str(model)
    except Exception:
        log.warning("failed to read current claude deepseek slug", exc_info=True)
    return None
