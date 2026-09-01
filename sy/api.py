"""Switch-codex 管理 API 路由。"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from sy import auth, claude_sync, codex_sync, core, db, grok_sync, logbook, probes, state, timeutil
from sy.const import (
    COLOR_BAD,
    DEFAULT_MODEL,
    DEFAULT_NEWAPI_PROBE,
    DEFAULT_PROBE_INTERVAL_SEC,
    DEEPSEEK_CLIENT_MODELS,
    DEEPSEEK_POOL,
    ERROR_LOG_RETENTION_HOURS,
    PROBE_MULTIPLIER_THRESHOLD,
)

log = logging.getLogger("switchyard.api")
_entry_in_beijing = timeutil.entry_in_beijing

router = APIRouter()

class LoginIn(BaseModel):
    password: str = Field(..., min_length=1)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)


@router.get("/api/auth/status")
async def auth_status(authorization: Optional[str] = Header(None)):
    token = auth._bearer_token(authorization)
    logged_in = bool(token) and auth._session_valid(token)
    return {
        "logged_in": logged_in,
        "must_change": bool(auth.ensure_auth().get("must_change")),
        "default_password": auth.default_password_active(),
    }


@router.post("/api/login")
async def login(body: LoginIn, request: Request):
    client_ip = auth.login_client_ip(request)
    if auth._login_blocked(client_ip):
        raise HTTPException(status_code=429, detail="尝试次数过多，请 5 分钟后再试")
    cfg = auth.ensure_auth()
    if not auth._verify_password(body.password, cfg.get("password_hash", "")):
        auth._record_login_failure(client_ip)
        raise HTTPException(status_code=401, detail="密码错误")
    auth._record_login_success(client_ip)
    return {
        "token": auth._new_session(),
        "must_change": bool(cfg.get("must_change")),
    }


@router.post("/api/logout")
async def logout(token: str = Depends(auth.require_session)):
    auth.revoke_session(token)
    return {"ok": True}


@router.post("/api/change-password")
async def change_password(
    body: ChangePasswordIn,
    _: str = Depends(auth.require_session),
):
    cfg = auth.ensure_auth()
    if not auth._verify_password(body.old_password, cfg.get("password_hash", "")):
        raise HTTPException(status_code=400, detail="旧密码错误")
    cfg["password_hash"] = auth._hash_password(body.new_password)
    cfg["must_change"] = False
    cfg["changed_at"] = datetime.now().isoformat(timespec="seconds")
    auth.save_auth(cfg)
    auth.revoke_all_sessions()
    log.info("admin password changed; all sessions revoked")
    return {"ok": True, "token": auth._new_session()}


# ---------- models ----------

class UpstreamIn(BaseModel):
    name: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    priority: int = 100
    enabled: bool = True
    model: str = DEFAULT_MODEL
    model_map: Optional[list[dict]] = None
    multiplier: Optional[float] = Field(None, ge=0)
    probe_enabled: Optional[bool] = None
    chat_completions: Optional[bool] = None
    anthropic_messages: Optional[bool] = None
    standalone_web_search: Optional[bool] = None


class UpstreamUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    model: Optional[str] = None
    model_map: Optional[list[dict]] = None
    multiplier: Optional[float] = Field(None, ge=0)
    probe_enabled: Optional[bool] = None
    chat_completions: Optional[bool] = None
    anthropic_messages: Optional[bool] = None
    standalone_web_search: Optional[bool] = None


class ActiveModelIn(BaseModel):
    active_model: str = Field(..., min_length=1)


class ClaudeConfigIn(BaseModel):
    mode: str = Field(..., min_length=1)  # local-direct | openai-all | deepseek
    model: Optional[str] = Field(None, min_length=1)


class GrokConfigIn(BaseModel):
    mode: str = Field(..., min_length=1)  # local-direct | grok
    model: Optional[str] = Field(None, min_length=1)


class ClaudeBridgeIn(BaseModel):
    enabled: bool


class ModelProbeIn(BaseModel):
    model: str = Field(..., min_length=1)
    probe_enabled: bool
    interval_sec: Optional[int] = Field(None, ge=60, le=86400)


class ModelProbeSettingsIn(BaseModel):
    """Bulk save for the model-availability settings modal."""
    default_interval_sec: Optional[int] = Field(None, ge=60, le=86400)
    models: dict[str, dict[str, Any]] = Field(default_factory=dict)


class ModelProbeRunIn(BaseModel):
    """Optional single-model trigger for the availability board."""
    model: Optional[str] = Field(None, min_length=1)


class PricingIn(BaseModel):
    pricing: dict[str, dict[str, Any]]


class PublicAccessIn(BaseModel):
    enabled: bool = False
    public_url: str = ""
    mode: str = "blacklist"
    allow_loopback: bool = True
    trust_proxy_headers: bool = False
    blocked: list[str] = Field(default_factory=list)
    allowed: list[str] = Field(default_factory=list)


class NewApiProbeIn(BaseModel):
    name: str = Field(..., min_length=1)
    enabled: bool = True
    interval_sec: int = Field(DEFAULT_NEWAPI_PROBE["interval_sec"], ge=15, le=86400)
    base_url: str = Field(..., min_length=1)
    group: str = Field(..., min_length=1)
    upstream_name: str = Field(..., min_length=1)
    access_token: str = ""
    priority_bias: float = Field(0.0, ge=-1, le=1)


class NewApiProbeUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1)
    enabled: Optional[bool] = None
    interval_sec: Optional[int] = Field(None, ge=15, le=86400)
    base_url: Optional[str] = Field(None, min_length=1)
    group: Optional[str] = Field(None, min_length=1)
    upstream_name: Optional[str] = Field(None, min_length=1)
    access_token: Optional[str] = None
    clear_access_token: bool = False
    priority_bias: Optional[float] = Field(None, ge=-1, le=1)


# ---------- health + UI ----------

@router.get("/api/config")
async def get_config(_: str = Depends(auth.require_master)):
    cfg = core.load_config()
    items = core.load_upstreams()
    active = core.normalize_model(cfg.get("active_model"))
    return {
        "active_model": active,
        "models": core.collect_models(items),
        "timeout_sec": cfg.get("timeout_sec", 120),
        "host": cfg.get("host", "127.0.0.1"),
        "port": cfg.get("port", 4100),
        "codex": codex_sync.status(),
        "claude": claude_sync.status(),
        "grok": grok_sync.status(),
    }


@router.put("/api/active-model")
async def set_active_model(body: ActiveModelIn, _: str = Depends(auth.require_master)):
    return core._apply_active_model(body.active_model)


@router.put("/api/claude/config")
async def set_claude_config(body: ClaudeConfigIn, _: str = Depends(auth.require_master)):
    return core._apply_claude_config(body.mode, body.model)


@router.put("/api/grok/config")
async def set_grok_config(body: GrokConfigIn, _: str = Depends(auth.require_master)):
    return core._apply_grok_config(body.mode, body.model)


@router.put("/api/claude/bridge")
async def set_claude_bridge(body: ClaudeBridgeIn, _: str = Depends(auth.require_master)):
    return core._apply_claude_bridge(body.enabled)


@router.get("/api/models")
async def list_models(_: str = Depends(auth.require_master)):
    cfg = core.load_config()
    items = core.load_upstreams()

    def enabled_in_pool(pool: str, client_model: Optional[str] = None) -> int:
        n = 0
        for u in items:
            if not u.get("enabled", True):
                continue
            if core.normalize_model(u.get("model")) != core.normalize_model(pool):
                continue
            if client_model and not core.upstream_supports_model(u, client_model):
                continue
            n += 1
        return n

    def entry(model: str, pool: str, passthrough: bool, sync_label: str) -> dict:
        return {
            "model": model,
            "pool": pool,
            "enabled_upstreams": enabled_in_pool(pool),
            "passthrough": passthrough,
            "probe_enabled": core.probe_enabled_for_model(model, cfg),
            "codex_sync": sync_label,
        }

    data = [
        entry(DEFAULT_MODEL, DEFAULT_MODEL, True, "restore"),
        entry(DEEPSEEK_POOL, DEEPSEEK_POOL, False, "official-deepseek"),
    ]

    pools = core.collect_models(items)
    for pool in pools:
        if pool in (DEFAULT_MODEL, DEEPSEEK_POOL) or codex_sync.is_deepseek_model(pool):
            continue
        data.append(entry(pool, pool, False, "routing-only"))

    pool_rows = [{"model": p, "enabled_upstreams": enabled_in_pool(p)} for p in pools]
    return {
        "data": data,
        "pools": pool_rows,
        "active_model": core.normalize_model(cfg.get("active_model")),
        "probe_interval_sec": core.probe_interval_sec(cfg),
        "codex": codex_sync.status(),
        "claude": claude_sync.status(),
        "grok": grok_sync.status(),
    }


@router.put("/api/models/probe")
async def set_model_probe(body: ModelProbeIn, _: str = Depends(auth.require_master)):
    model = str(body.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model required")
    entry: dict[str, Any] = {"enabled": bool(body.probe_enabled)}
    if body.interval_sec is not None:
        entry["interval_sec"] = int(body.interval_sec)
    else:
        entry["interval_sec"] = core.probe_interval_for_model(model)
    probe = core.save_probe_settings(models={model: entry})
    settings = core.probe_settings_for_model(model)
    log.info(
        "probe settings model=%s enabled=%s interval=%s",
        model,
        settings.get("enabled"),
        settings.get("interval_sec"),
    )
    return {
        "model": model,
        "probe_enabled": bool(settings.get("enabled")),
        "interval_sec": int(settings.get("interval_sec") or DEFAULT_PROBE_INTERVAL_SEC),
        "probe": probe,
        "probe_interval_sec": core.probe_interval_sec(),
    }


@router.get("/api/model-availability/settings")
async def get_model_availability_settings(_: str = Depends(auth.require_master)):
    cfg = core.load_config()
    models = core.collect_client_models_for_availability()
    data = []
    for m in models:
        s = core.probe_settings_for_model(m, cfg)
        data.append(
            {
                "model": m,
                "pool": core.pool_for_client_model(m),
                "probe_enabled": bool(s.get("enabled")),
                "interval_sec": int(s.get("interval_sec") or DEFAULT_PROBE_INTERVAL_SEC),
            }
        )
    return {
        "data": data,
        "default_interval_sec": core.probe_interval_sec(cfg),
        "threshold": PROBE_MULTIPLIER_THRESHOLD,
    }


@router.put("/api/model-availability/settings")
async def put_model_availability_settings(
    body: ModelProbeSettingsIn,
    _: str = Depends(auth.require_master),
):
    probe = core.save_probe_settings(
        models=body.models or {},
        default_interval_sec=body.default_interval_sec,
    )
    log.info(
        "probe settings saved models=%s default_interval=%s",
        list((body.models or {}).keys()),
        probe.get("interval_sec"),
    )
    return await get_model_availability_settings(_)


@router.get("/api/upstreams")
async def list_upstreams(_: str = Depends(auth.require_master)):
    items = sorted(
        core.load_upstreams(),
        key=lambda x: (
            core.normalize_model(x.get("model")),
            core.upstream_multiplier_value(x),
            core._upstream_effective_priority(x),
            x.get("name", ""),
        ),
    )
    health_map = state.probe_health_snapshot()
    return {"data": [core.public_upstream(u, health_map) for u in items]}


@router.get("/api/newapi-probes")
async def list_newapi_probes(_: str = Depends(auth.require_master)):
    """List NewAPI ratio probes (token redacted) plus upstream names for the form."""
    items = probes.load_newapi_probes()
    upstream_names = sorted(
        {
            str(u.get("name") or "")
            for u in core.load_upstreams()
            if str(u.get("name") or "").strip()
        },
        key=str.lower,
    )
    return {
        "data": [probes.public_newapi_probe(p) for p in items],
        "upstreams": upstream_names,
    }


@router.post("/api/newapi-probes")
async def create_newapi_probe(body: NewApiProbeIn, _: str = Depends(auth.require_master)):
    """Add a new NewAPI ratio probe to the database."""
    items = probes.load_newapi_probes()
    probe = probes._normalize_newapi_probe(
        {
            "id": str(uuid.uuid4()),
            "name": body.name,
            "enabled": body.enabled,
            "interval_sec": body.interval_sec,
            "base_url": body.base_url,
            "group": body.group,
            "upstream_name": body.upstream_name,
            "access_token": body.access_token or "",
            "priority_bias": body.priority_bias,
        },
        index=len(items),
    )
    items.append(probe)
    probes.save_newapi_probes(items)
    probes._sync_newapi_probe_bias_to_upstream(probe)
    # Wake the background loop immediately so the new probe runs right away.
    state.set_newapi_probe_state(probe["id"], {"next_run_at": 0})
    log.info("newapi probe created id=%s name=%s", probe["id"], probe["name"])
    return probes.public_newapi_probe(probe)


@router.post("/api/newapi-probes/run")
async def run_all_newapi_probes(_: str = Depends(auth.require_master)):
    """Manually trigger every enabled NewAPI probe once."""
    results = []
    for p in probes.load_newapi_probes():
        if not p.get("enabled"):
            continue
        results.append(probes.probe_newapi_ratio_once(p))
    return {"data": results, "count": len(results)}


@router.put("/api/newapi-probes/{probe_id}")
async def update_newapi_probe(
    probe_id: str,
    body: NewApiProbeUpdate,
    _: str = Depends(auth.require_master),
):
    """Update one NewAPI probe (settings take effect immediately)."""
    items = probes.load_newapi_probes()
    target = None
    for p in items:
        if str(p.get("id") or "") == probe_id:
            target = p
            break
    if target is None:
        raise HTTPException(status_code=404, detail=f"探测不存在: {probe_id}")

    upd = body.model_dump(exclude_unset=True)
    if upd.get("clear_access_token"):
        target["access_token"] = ""
    elif upd.get("access_token"):
        target["access_token"] = str(upd["access_token"]).strip()
    upd.pop("access_token", None)
    upd.pop("clear_access_token", None)
    for key, value in upd.items():
        if value is not None:
            target[key] = value

    normalized = probes._normalize_newapi_probe(target)
    for i, p in enumerate(items):
        if str(p.get("id") or "") == probe_id:
            items[i] = normalized
            break
    probes.save_newapi_probes(items)
    probes._sync_newapi_probe_bias_to_upstream(normalized)
    # Force a run soon so the new settings take effect immediately.
    state.set_newapi_probe_state(probe_id, {"next_run_at": 0})
    log.info("newapi probe updated id=%s name=%s", normalized["id"], normalized["name"])
    return probes.public_newapi_probe(normalized)


@router.delete("/api/newapi-probes/{probe_id}")
async def delete_newapi_probe(probe_id: str, _: str = Depends(auth.require_master)):
    """Remove one NewAPI probe from the database."""
    items = probes.load_newapi_probes()
    remaining = [p for p in items if str(p.get("id") or "") != probe_id]
    if len(remaining) == len(items):
        raise HTTPException(status_code=404, detail=f"探测不存在: {probe_id}")
    probes.save_newapi_probes(remaining)
    state.pop_newapi_probe_state(probe_id)
    log.info("newapi probe deleted id=%s", probe_id)
    return {"ok": True}


@router.post("/api/newapi-probes/{probe_id}/run")
async def run_newapi_probe(probe_id: str, _: str = Depends(auth.require_master)):
    """Manually trigger one NewAPI probe."""
    for p in probes.load_newapi_probes():
        if str(p.get("id") or "") == probe_id:
            return probes.probe_newapi_ratio_once(p)
    raise HTTPException(status_code=404, detail=f"探测不存在: {probe_id}")


# ---------- public access ----------


@router.get("/api/public/settings")
async def get_public_settings(_: str = Depends(auth.require_master)):
    """Return public-access switch, URL, client key and IP rules."""
    cfg = core.load_config()
    public = core.load_public_config(cfg)
    base = public["public_url"].rstrip("/")
    return {
        "enabled": public["enabled"],
        "public_url": base,
        "base_url": f"{base}/v1" if base else "",
        "api_endpoint": f"{base}/v1/responses" if base else "",
        "key": str(cfg.get("master_key") or ""),
        "mode": public["mode"],
        "allow_loopback": public["allow_loopback"],
        "trust_proxy_headers": public["trust_proxy_headers"],
        "blocked": public["blocked"],
        "allowed": public["allowed"],
    }


@router.put("/api/public/settings")
async def put_public_settings(
    body: PublicAccessIn,
    _: str = Depends(auth.require_master),
):
    """Save public-access switch and IP rules (immediately enforced)."""
    core.save_public_config(body.model_dump())
    log.info(
        "public settings updated enabled=%s mode=%s blocked=%s allowed=%s",
        body.enabled,
        body.mode,
        len(body.blocked),
        len(body.allowed),
    )
    return await get_public_settings(_)


@router.get("/api/public/ip-stats")
async def get_public_ip_stats(
    range: str = "7d",
    q: Optional[str] = None,
    _: str = Depends(auth.require_master),
):
    """Per-IP usage stats from the request log (non-probe traffic)."""
    return {
        "data": logbook._ip_usage_stats(range, q),
        "range": range,
    }


@router.post("/api/upstreams")
async def create_upstream(body: UpstreamIn, _: str = Depends(auth.require_master)):
    items = core.load_upstreams()
    multiplier = core.normalize_upstream_multiplier(body.multiplier, body.name, body.model)
    model = core.normalize_model(body.model)
    if body.probe_enabled is None:
        probe_enabled = core.default_probe_enabled_for_upstream(
            multiplier, body.name, body.model
        )
    else:
        probe_enabled = bool(body.probe_enabled)
    item = {
        "id": str(uuid.uuid4()),
        "name": body.name.strip(),
        "base_url": core.normalize_base_url(body.base_url),
        "api_key": body.api_key.strip(),
        "priority": int(body.priority),
        "enabled": bool(body.enabled),
        "model": model,
        "model_map": (
            core.normalize_model_map(body.model_map)
            if body.model_map is not None
            else core.default_model_map_for(model)
        ),
        "multiplier": multiplier,
        "probe_enabled": probe_enabled,
        "chat_completions": bool(body.chat_completions),
        "anthropic_messages": bool(body.anthropic_messages),
        "standalone_web_search": body.standalone_web_search,
    }
    items.append(item)
    core.save_upstreams(items)
    log.info(
        "created upstream name=%s model=%s priority=%s probe=%s",
        item["name"],
        item["model"],
        item["priority"],
        item["probe_enabled"],
    )
    return core.public_upstream(item)


@router.put("/api/upstreams/{uid}")
async def update_upstream(uid: str, body: UpstreamUpdate, _: str = Depends(auth.require_master)):
    items = core.load_upstreams()
    for u in items:
        if u.get("id") == uid:
            data = body.model_dump(exclude_unset=True)
            old_model = core.normalize_model(u.get("model"))
            if "name" in data and data["name"] is not None:
                u["name"] = data["name"].strip()
            if "base_url" in data and data["base_url"] is not None:
                u["base_url"] = core.normalize_base_url(data["base_url"])
            if "api_key" in data and data["api_key"] is not None and data["api_key"] != "":
                u["api_key"] = data["api_key"].strip()
            if "priority" in data and data["priority"] is not None:
                u["priority"] = int(data["priority"])
            if "enabled" in data and data["enabled"] is not None:
                u["enabled"] = bool(data["enabled"])
            if "model" in data and data["model"] is not None:
                u["model"] = core.normalize_model(data["model"])
            if "model_map" in data and data["model_map"] is not None:
                u["model_map"] = core.normalize_model_map(data["model_map"])
            elif u.get("model") != old_model:
                # Pool renamed → reset to the new pool's default model list.
                u["model_map"] = core.default_model_map_for(u.get("model"))
            if u.get("request_model") is not None:
                u["request_model"] = None
            if "multiplier" in data and data["multiplier"] is not None:
                u["multiplier"] = core.normalize_upstream_multiplier(
                    data["multiplier"], u.get("name"), u.get("model")
                )
            if "probe_enabled" in data and data["probe_enabled"] is not None:
                u["probe_enabled"] = bool(data["probe_enabled"])
            if "chat_completions" in data and data["chat_completions"] is not None:
                u["chat_completions"] = bool(data["chat_completions"])
            if "anthropic_messages" in data and data["anthropic_messages"] is not None:
                u["anthropic_messages"] = bool(data["anthropic_messages"])
            if "standalone_web_search" in data:
                u["standalone_web_search"] = (
                    None
                    if data["standalone_web_search"] is None
                    else bool(data["standalone_web_search"])
                )
            core.save_upstreams(items)
            return core.public_upstream(u)
    raise HTTPException(status_code=404, detail="upstream not found")


@router.delete("/api/upstreams/{uid}")
async def delete_upstream(uid: str, _: str = Depends(auth.require_master)):
    items = core.load_upstreams()
    new_items = [u for u in items if u.get("id") != uid]
    if len(new_items) == len(items):
        raise HTTPException(status_code=404, detail="upstream not found")
    core.save_upstreams(new_items)
    state.clear_probe_health(uid)
    log.info("deleted upstream id=%s", uid)

    cfg = core.load_config()
    active = core.normalize_model(cfg.get("active_model"))
    known = set(core.collect_models(new_items)) | set(DEEPSEEK_CLIENT_MODELS)
    if active != codex_sync.LOCAL_DIRECT and active not in known:
        # 只改路由默认池，不触发 Codex 同步（删光上游时不应覆盖 ~/.codex）。
        cfg["active_model"] = DEFAULT_MODEL
        core.save_config(cfg)
        return {"ok": True, "active_model_reset": {"active_model": DEFAULT_MODEL}}
    return {"ok": True}


@router.get("/api/availability-history")
async def get_availability_history(
    range: str = "24h",
    _: str = Depends(auth.require_master),
):
    """Historical availability bars for each client model (1h / 24h / 7d)."""
    return logbook._build_availability_history(range)


@router.get("/api/model-availability")
async def get_model_availability(_: str = Depends(auth.require_master)):
    cfg = core.load_config()
    snap = state.model_availability_snapshot()
    models = core.collect_client_models_for_availability()
    data = []
    next_secs: list[float] = []
    now = time.time()
    for m in models:
        settings = core.probe_settings_for_model(m, cfg)
        interval = int(settings.get("interval_sec") or DEFAULT_PROBE_INTERVAL_SEC)
        enabled = bool(settings.get("enabled"))
        base = snap.get(m) or {
            "model": m,
            "pool": core.pool_for_client_model(m),
            "ok": None,
            "light": "unknown",
            "multiplier": None,
            "upstream": None,
            "checked_at": None,
            "attempts": 0,
            "error": None,
        }
        base = dict(base)
        age = core._model_availability_age_sec(base)
        if age is not None and age > interval:
            # 超出该模型的探测间隔，视为过期，不再展示旧状态。
            base = {
                "model": m,
                "pool": core.pool_for_client_model(m),
                "ok": None,
                "light": "unknown",
                "multiplier": None,
                "upstream": None,
                "upstream_id": None,
                "checked_at": base.get("checked_at"),
                "attempts": 0,
                "error": None,
                "stale": True,
            }
        else:
            base["stale"] = False
        item = dict(base)
        item["probe_enabled"] = enabled
        item["interval_sec"] = interval
        # Same multiplier→color gradient as the historical availability page.
        if item.get("ok") and item.get("multiplier") is not None:
            item["color"] = core._price_color_for_multiplier(item.get("multiplier"))
        elif item.get("ok") is False:
            item["color"] = COLOR_BAD
        else:
            item["color"] = None
        if enabled:
            # Seconds until this model's next clock boundary.
            next_ts = (int(now) // interval + 1) * interval
            item["next_boundary_sec"] = round(max(0.0, next_ts - now), 1)
            next_secs.append(item["next_boundary_sec"])
        else:
            item["next_boundary_sec"] = None
            if item.get("light") == "unknown" and item.get("ok") is None:
                item["light"] = "disabled"
        data.append(item)
    return {
        "data": data,
        "interval_sec": core.probe_interval_sec(cfg),
        "threshold": PROBE_MULTIPLIER_THRESHOLD,
        "next_boundary_sec": round(min(next_secs), 1) if next_secs else None,
    }


@router.post("/api/model-availability/run")
async def run_model_availability(
    body: Optional[ModelProbeRunIn] = None,
    _: str = Depends(auth.require_master),
):
    """Manual trigger for one cascade round (admin).

    Body ``{"model": "..."}`` probes just that model; otherwise all enabled
    models are probed.
    """
    if body and body.model:
        model = str(body.model).strip()
        if model not in core.collect_client_models_for_availability():
            raise HTTPException(status_code=404, detail=f"模型不存在: {model}")
        cfg = core.load_config()
        timeout = min(float(cfg.get("timeout_sec", 120)), 30.0)
        await probes._cascade_probe_model(model, timeout=timeout, record_log=True)
        if core.probe_settings_for_model(model, cfg).get("enabled"):
            probes._reschedule_model_probe(model, cfg)
        log.info("manual single model probe model=%s", model)
        return await get_model_availability(_)
    await probes._run_model_cascade_probes(only_due=False)
    return await get_model_availability(_)


@router.post("/api/upstreams/{uid}/test")
async def test_upstream(uid: str, _: str = Depends(auth.require_master)):
    items = core.load_upstreams()
    target = next((u for u in items if u.get("id") == uid), None)
    if not target:
        raise HTTPException(status_code=404, detail="upstream not found")
    result = await probes._probe_upstream(target, source="manual", record_log=True)
    # Keep the previous response shape for the UI.
    out = {
        "ok": bool(result.get("ok")),
        "status_code": result.get("status_code"),
        "upstream": result.get("upstream"),
        "model": result.get("model"),
        "probe_model": result.get("probe_model"),
        "url": result.get("url"),
        "body_preview": result.get("body_preview"),
        "status": "ok" if result.get("ok") else "error",
        "duration_ms": result.get("duration_ms"),
    }
    if result.get("error"):
        out["error"] = result["error"]
    return out


@router.post("/api/upstreams/{uid}/standalone-search-test")
async def test_upstream_standalone_search(uid: str, _: str = Depends(auth.require_master)):
    target = next((u for u in core.load_upstreams() if u.get("id") == uid), None)
    if not target:
        raise HTTPException(status_code=404, detail="upstream not found")
    return await probes.probe_standalone_web_search(target)


# ---------- request logs ----------

@router.get("/api/pricing")
async def get_pricing(_: str = Depends(auth.require_master)):
    pricing = core.load_pricing()
    models = core.collect_client_models_for_availability()
    extra = sorted(k for k in pricing if k not in models)
    return {"pricing": pricing, "models": models + extra}


@router.put("/api/pricing")
async def set_pricing(body: PricingIn, _: str = Depends(auth.require_master)):
    cleaned: dict[str, dict[str, float]] = {}
    for pool, vals in body.pricing.items():
        d: dict[str, float] = {}
        for k in core.PRICE_KEYS:
            v = vals.get(k)
            if v is not None:
                f = core._float_or_none(v)
                if f is not None:
                    d[k] = f
        lc_raw = vals.get("long_context")
        if isinstance(lc_raw, dict):
            lc: dict[str, float] = {}
            for k in ("threshold",) + core.PRICE_KEYS:
                v = lc_raw.get(k)
                if v is not None:
                    f = core._float_or_none(v)
                    if f is not None:
                        lc[k] = f
            if lc:
                d["long_context"] = lc
        if d:
            cleaned[str(pool).strip()] = d
    core.save_pricing(cleaned)
    log.info("pricing updated pools=%s", list(cleaned))
    # 回默认价的条目不写库，返回合并后的生效单价而不是提交值
    return {"pricing": core.load_pricing()}


@router.get("/api/logs/models")
async def list_request_models(_: str = Depends(auth.require_master)):
    # Usage-oriented model list: exclude probe traffic.
    counts = core._request_model_counts(logbook._traffic_logs_snapshot())
    return {
        "data": [
            {"model": model, "requests": count}
            for model, count in counts.items()
        ]
    }


@router.get("/api/logs/upstreams")
async def list_log_upstreams(_: str = Depends(auth.require_master)):
    """Upstream names appearing in request/error logs, with counts."""
    req_counts: dict[str, int] = {}
    all_counts: dict[str, int] = {}
    for e in logbook._logs_snapshot():
        name = str(e.get("upstream") or "").strip()
        if name:
            all_counts[name] = all_counts.get(name, 0) + 1
    for e in logbook._traffic_logs_snapshot():
        name = str(e.get("upstream") or "").strip()
        if name:
            req_counts[name] = req_counts.get(name, 0) + 1
    err_counts: dict[str, int] = {}
    for e in logbook._error_logs_snapshot():
        for a in e.get("attempts") or []:
            if not isinstance(a, dict):
                continue
            name = str(a.get("upstream") or "").strip()
            if name:
                err_counts[name] = err_counts.get(name, 0) + 1
    return {
        "data": [
            {"upstream": name, "requests": count}
            for name, count in sorted(
                req_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "all": [
            {"upstream": name, "requests": count}
            for name, count in sorted(
                all_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "errors": [
            {"upstream": name, "errors": count}
            for name, count in sorted(
                err_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }


@router.get("/api/logs/filter-options")
async def list_log_filter_options(
    range: str = "today",
    start: Optional[str] = None,
    end: Optional[str] = None,
    _: str = Depends(auth.require_master),
):
    """Range-scoped filter options (pool / model / upstream) for all log pages."""
    start_dt, end_dt = logbook._resolve_log_range(range, start, end)
    traffic = logbook._entries_in_log_range(logbook._traffic_logs_snapshot(), start_dt, end_dt)
    all_logs = logbook._entries_in_log_range(logbook._logs_snapshot(), start_dt, end_dt)
    errors = logbook._entries_in_log_range(logbook._error_logs_snapshot(), start_dt, end_dt)
    return {
        "stats": logbook._count_log_filter_options(traffic),
        "logs": logbook._count_log_filter_options(all_logs),
        "errors": logbook._count_error_filter_options(errors),
    }


@router.get("/api/logs/stats")
async def logs_stats(
    range: str = "today",
    pool: Optional[str] = None,
    model: Optional[str] = None,
    upstream: Optional[str] = None,
    _: str = Depends(auth.require_master),
):
    return logbook._log_stats(range, pool, model, upstream)


@router.get("/api/logs")
async def list_logs(
    limit: int = 100,
    offset: int = 0,
    range: str = "today",
    start: Optional[str] = None,
    end: Optional[str] = None,
    pool: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    upstream: Optional[str] = None,
    q: Optional[str] = None,
    _: str = Depends(auth.require_master),
):
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    start_dt, end_dt = logbook._resolve_log_range(range, start, end)
    total, items = db.query_request_logs(
        limit=limit,
        offset=offset,
        start=start_dt.isoformat(timespec="milliseconds") if start_dt else None,
        end=end_dt.isoformat(timespec="milliseconds") if end_dt else None,
        pool=pool,
        model=model,
        status=status,
        upstream=upstream,
        q=q,
    )
    page = [_entry_in_beijing(e) for e in items]
    for e in page:
        # 旧日志无 endpoint 字段：按路径兜底补全（anthropic / response）。
        if not e.get("endpoint"):
            e["endpoint"] = "anthropic" if (e.get("path") or "").startswith("/v1/messages") else "response"
        e["multiplier"] = core.entry_multiplier(e)
        e["cost_usd"] = core.compute_cost_usd(e)
        e["real_cost_cny"] = core.compute_real_cost_cny(e, e["cost_usd"], e["multiplier"])
        e["tps"] = core.compute_tps(e)
        e["cost_breakdown"] = core.cost_breakdown(e)
    return {"data": page, "total": total, "limit": limit, "offset": offset}


@router.delete("/api/logs")
async def clear_logs(_: str = Depends(auth.require_master)):
    logbook._clear_logs()
    log.info("request logs cleared")
    return {"ok": True}


# ---------- error logs ----------

@router.get("/api/errors")
async def list_error_logs(
    limit: int = 50,
    offset: int = 0,
    range: str = "today",
    start: Optional[str] = None,
    end: Optional[str] = None,
    pool: Optional[str] = None,
    model: Optional[str] = None,
    upstream: Optional[str] = None,
    q: Optional[str] = None,
    _: str = Depends(auth.require_master),
):
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    start_dt, end_dt = logbook._resolve_log_range(range, start, end)
    total, items = db.query_error_logs(
        limit=limit,
        offset=offset,
        start=start_dt.isoformat(timespec="milliseconds") if start_dt else None,
        end=end_dt.isoformat(timespec="milliseconds") if end_dt else None,
        pool=pool,
        model=model,
        upstream=upstream,
        q=q,
    )
    page: list[dict] = []
    for e in items:
        out = _entry_in_beijing(e)
        out["has_body"] = e.get("request_body") is not None
        out.pop("request_body", None)
        page.append(out)
    return {
        "data": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "retention_hours": ERROR_LOG_RETENTION_HOURS,
    }


@router.get("/api/errors/{eid}")
async def get_error_log(eid: str, _: str = Depends(auth.require_master)):
    entry = logbook._find_error_log(eid)
    if not entry:
        raise HTTPException(status_code=404, detail="error log not found")
    return {"data": _entry_in_beijing(entry)}


@router.delete("/api/errors")
async def clear_error_logs(_: str = Depends(auth.require_master)):
    logbook._clear_error_logs()
    log.info("error logs cleared")
    return {"ok": True}
