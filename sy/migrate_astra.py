"""一次性迁移：为既有 OpenAI 池上游补充 gpt-6-astra 映射。"""
from __future__ import annotations

import logging
from typing import Any

from sy import db
from sy.const import DEFAULT_CLIENT_MODEL, DEFAULT_MODEL

log = logging.getLogger("switchyard.migrate_astra")

MIGRATION_KEY = "openai_gpt_6_astra_v1"
DEFAULT_KEY = "openai_gpt_6_astra_default_v1"
ASTRA_MODEL = DEFAULT_CLIENT_MODEL


def _normalize_entries(raw: Any) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        if not model or model in seen:
            continue
        actual = str(item.get("actual") or "").strip()
        out.append({"model": model, "actual": actual or model})
        seen.add(model)
    return out


def migrate() -> dict[str, Any]:
    if db.get_setting(MIGRATION_KEY):
        ensure_project_default()
        return {"migrated": False, "reason": "already-migrated"}

    updated_names: list[str] = []
    items = db.load_upstreams()
    for upstream in items:
        if str(upstream.get("model") or "").strip() != DEFAULT_MODEL:
            continue
        entries = _normalize_entries(upstream.get("model_map"))
        if not entries or any(entry["model"] == ASTRA_MODEL for entry in entries):
            continue
        entries.append({"model": ASTRA_MODEL, "actual": ASTRA_MODEL})
        upstream["model_map"] = entries
        updated_names.append(str(upstream.get("name") or upstream.get("id") or ""))

    if updated_names:
        db.save_upstreams(items)
    db.set_setting(MIGRATION_KEY, {"applied": True, "upstreams_updated": len(updated_names)})
    ensure_project_default()
    log.info("gpt-6-astra migration applied upstreams_updated=%s", len(updated_names))
    return {
        "migrated": True,
        "upstreams_updated": len(updated_names),
        "names": updated_names,
    }


def ensure_project_default() -> bool:
    cfg = db.load_config_raw() or {}
    if not isinstance(cfg, dict):
        cfg = {}
    changed = str(cfg.get("active_model") or "").strip() != DEFAULT_CLIENT_MODEL
    if changed:
        cfg["active_model"] = DEFAULT_CLIENT_MODEL
        db.save_config_raw(cfg)
    if not db.get_setting(DEFAULT_KEY):
        db.set_setting(DEFAULT_KEY, {"applied": True, "active_model": DEFAULT_CLIENT_MODEL})
    return changed
