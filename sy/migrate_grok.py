"""一次性迁移：从 ~/.grok/config.toml 提取自定义 grok 端点，seed 成 grok 池上游。

只导入满足以下条件的 [model."<key>"] 段：
  - model 是 grok 客户端模型（GROK_CLIENT_MODELS 或 grok- 前缀）；
  - base_url / api_key 均非空，且 base_url 不是本机地址（避免把 4100 自身 seed 回来）。

以 settings 的 grok_pool_v1 键做门控，跑过一次不再重复；没有可用端点时不写
门控键，等用户后续装好 Grok 配置再在下一次启动重试。
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sy import db
from sy.grok_sync import _parse_config, _read_config_text
from sy.const import (
    DEFAULT_PRICING,
    GROK_CLIENT_MODELS,
    GROK_DEFAULT_MODEL_MAP,
    GROK_DEFAULT_PRICING,
    GROK_POOL,
)

log = logging.getLogger("switchyard.migrate_grok")

MIGRATION_KEY = "grok_pool_v1"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _grok_config_path() -> Path:
    # 一次性 seed 只读默认 ~/.grok，忽略 GROK_HOME（测试/临时目录不能污染池）。
    return Path.home() / ".grok" / "config.toml"


def _candidate_entries() -> list[dict[str, str]]:
    p = _grok_config_path()
    if not p.exists():
        return []
    try:
        obj = _parse_config(_read_config_text(p))
    except Exception as exc:
        log.warning("skip grok pool seeding: failed to parse %s (%s)", p, exc)
        return []

    models = obj.get("model")
    if not isinstance(models, dict):
        return []
    known = set(GROK_CLIENT_MODELS)
    out: list[dict[str, str]] = []
    for key, section in models.items():
        if not isinstance(section, dict):
            continue
        model = str(section.get("model") or "").strip()
        if not model or (model not in known and not model.startswith("grok-")):
            continue
        base_url = str(section.get("base_url") or "").strip()
        api_key = str(section.get("api_key") or "").strip()
        if not base_url or not api_key:
            continue
        host = (urlsplit(base_url).hostname or "").lower()
        if not host or host in LOCAL_HOSTS:
            continue
        out.append({"name": str(key), "base_url": base_url, "api_key": api_key})
    return out


def _ensure_grok_pricing() -> list[str]:
    cfg = db.load_config_raw() or {}
    if not isinstance(cfg, dict):
        cfg = {}
    pricing = cfg.get("pricing") if isinstance(cfg.get("pricing"), dict) else {}
    added: list[str] = []
    for model, vals in GROK_DEFAULT_PRICING.items():
        # 已进 DEFAULT_PRICING 的模型由内置默认价兜底，不再往库里 seed 覆盖项
        if model not in pricing and model not in DEFAULT_PRICING:
            pricing[model] = dict(vals)
            added.append(model)
    if added:
        cfg["pricing"] = pricing
        db.save_config_raw(cfg)
        log.info("grok pricing seeded models=%s", added)
    return added


def migrate() -> dict[str, Any]:
    pricing_added = _ensure_grok_pricing()
    if db.get_setting(MIGRATION_KEY):
        return {
            "migrated": False,
            "reason": "already-migrated",
            "pricing_added": pricing_added,
        }

    candidates = _candidate_entries()
    if not candidates:
        log.info("grok pool seeding skipped: no usable grok endpoints in %s", _grok_config_path())
        return {
            "migrated": False,
            "reason": "no-candidates",
            "pricing_added": pricing_added,
        }

    items = db.load_upstreams()
    existing_urls = {
        str(u.get("base_url") or "").strip().rstrip("/")
        for u in items
        if str(u.get("model") or "").strip() == GROK_POOL
    }

    added_names: list[str] = []
    for cand in candidates:
        url = cand["base_url"].rstrip("/")
        if url in existing_urls:
            continue
        items.append(
            {
                "id": str(uuid.uuid4()),
                "name": cand["name"],
                "base_url": cand["base_url"],
                "api_key": cand["api_key"],
                "model": GROK_POOL,
                "model_map": [dict(e) for e in GROK_DEFAULT_MODEL_MAP],
                "multiplier": 1.0,
                "priority": 100,
                "enabled": True,
                "probe_enabled": False,
                "request_model": None,
                "chat_completions": False,
                "anthropic_messages": False,
            }
        )
        existing_urls.add(url)
        added_names.append(cand["name"])

    if added_names:
        db.save_upstreams(items)
    db.set_setting(MIGRATION_KEY, {"applied": True, "upstreams_added": len(added_names)})
    log.info("grok pool migration applied upstreams_added=%s names=%s", len(added_names), added_names)
    return {
        "migrated": True,
        "upstreams_added": len(added_names),
        "names": added_names,
        "pricing_added": pricing_added,
    }
