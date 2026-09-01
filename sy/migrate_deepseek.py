"""一次性迁移：DeepSeek 池名 deepseek-v4-flash -> deepseek，并补齐 flash/pro 的 model_map、pro 价格与探测默认值。"""
from __future__ import annotations

import logging
from typing import Any

from sy import db
from sy.const import (
    DEFAULT_MODEL,
    DEFAULT_PRICING,
    DEEPSEEK_CLIENT_MODELS,
    DEEPSEEK_POOL,
)

log = logging.getLogger("switchyard.migrate_deepseek")

MIGRATION_KEY = "deepseek_pool_v5"


def _normalize_entries(raw: Any) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("model") or "").strip()
        if not name or name in seen:
            continue
        actual = str(item.get("actual") or "").strip()
        out.append({"model": name, "actual": actual or name})
        seen.add(name)
    return out


def migrate() -> dict:
    if db.get_setting(MIGRATION_KEY):
        return {"migrated": False, "reason": "already-migrated"}

    renamed = 0
    openai_renamed = 0
    items = db.load_upstreams()
    for u in items:
        m = str(u.get("model") or "").strip()
        if m == "openai-all":
            u["model"] = DEFAULT_MODEL
            openai_renamed += 1
            continue
        if m not in DEEPSEEK_CLIENT_MODELS:
            continue
        u["model"] = DEEPSEEK_POOL
        renamed += 1
        entries = _normalize_entries(u.get("model_map"))
        existing = {e["model"] for e in entries}
        for slug in DEEPSEEK_CLIENT_MODELS:
            if slug not in existing:
                entries.append({"model": slug, "actual": slug})
        u["model_map"] = entries
    if renamed or openai_renamed:
        db.save_upstreams(items)

    # 历史日志的 pool 列统一到新池名，避免筛选下拉出现新旧两套名字。
    conn = db._get_conn()
    for table in ("request_logs", "error_logs"):
        conn.execute("UPDATE %s SET pool = 'openai' WHERE pool = 'openai-all'" % table)
        conn.execute(
            "UPDATE %s SET pool = 'deepseek' WHERE pool IN ('deepseek-v4-flash', 'deepseek-v4-pro')"
            % table
        )
    conn.commit()

    cfg = db.load_config_raw() or {}
    if not isinstance(cfg, dict):
        cfg = {}
    changed_cfg = False

    pricing = cfg.get("pricing") if isinstance(cfg.get("pricing"), dict) else {}
    # deepseek-v4-pro 已进 DEFAULT_PRICING，由内置默认价兜底，无需再 seed 覆盖项
    if "deepseek-v4-pro" not in pricing and "deepseek-v4-pro" not in DEFAULT_PRICING:
        pricing["deepseek-v4-pro"] = {
            "input_per_m": 3.0,
            "output_per_m": 6.0,
            "cache_read_per_m": 0.025,
        }
        cfg["pricing"] = pricing
        changed_cfg = True

    probe = cfg.get("probe") if isinstance(cfg.get("probe"), dict) else {}
    models = probe.get("models") if isinstance(probe.get("models"), dict) else {}
    if "deepseek-v4-pro" not in models:
        models["deepseek-v4-pro"] = {"enabled": True, "interval_sec": 36000}
        probe["models"] = models
        cfg["probe"] = probe
        changed_cfg = True

    active = str(cfg.get("active_model") or "").strip()
    if active in DEEPSEEK_CLIENT_MODELS:
        cfg["active_model"] = DEEPSEEK_POOL
        changed_cfg = True
    elif active == "openai-all":
        cfg["active_model"] = DEFAULT_MODEL
        changed_cfg = True

    if changed_cfg:
        db.save_config_raw(cfg)

    db.set_setting(
        MIGRATION_KEY,
        {"applied": True, "deepseek_renamed": renamed, "openai_renamed": openai_renamed},
    )
    log.info(
        "pool rename migration applied deepseek=%s openai=%s", renamed, openai_renamed
    )
    return {
        "migrated": True,
        "deepseek_renamed": renamed,
        "openai_renamed": openai_renamed,
        "pricing_added": "deepseek-v4-pro",
        "probe_added": "deepseek-v4-pro",
    }
