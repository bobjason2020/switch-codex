#!/usr/bin/env python3
"""Switch-codex 核心业务层。

职责：配置读写、模型池路由、上游归一化、计费单价，以及少量跨模块共享的
可用性辅助函数。不直接依赖 HTTP 路由层。
"""
from __future__ import annotations

import copy
import ipaddress
import logging
import math
import re
from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException

from sy import claude_sync, codex_sync, db, grok_sync, state, timeutil
from sy.const import (
    CACHE_PRIORITY_TTL_SEC,
    DEFAULT_CLIENT_MODELS,
    DEFAULT_MASTER_KEY,
    DEFAULT_MODEL,
    DEFAULT_OPENAI_ALL_MODEL_MAP,
    DEFAULT_PRICING,
    DEFAULT_PROBE_INTERVAL_SEC,
    DEEPSEEK_CLIENT_MODELS,
    DEEPSEEK_DEFAULT_MODEL_MAP,
    DEEPSEEK_POOL,
    GROK_CLIENT_MODELS,
    GROK_DEFAULT_MODEL_MAP,
    GROK_POOL,
    MIN_REAL_COST,
    PROBE_MULTIPLIER_THRESHOLD,
    env_host,
    env_port,
)

log = logging.getLogger("switchyard.core")

BEIJING_TZ = timeutil.BEIJING_TZ
_now_beijing = timeutil.now_beijing
_parse_ts = timeutil.parse_ts
_timestamp_in_beijing = timeutil.timestamp_in_beijing
_entry_in_beijing = timeutil.entry_in_beijing
_iso_now = timeutil.iso_now


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def load_config() -> dict:
    cfg = db.load_config_raw()
    if not isinstance(cfg, dict):
        cfg = {}
    probe = _normalize_probe_config(cfg.get("probe"))
    base = {
        "host": env_host(),
        "port": env_port(),
        "timeout_sec": 120,
        "active_model": DEFAULT_MODEL,
        "probe": probe,
    }
    for k, v in base.items():
        if k == "probe":
            continue
        cfg.setdefault(k, v)
    cfg["probe"] = probe
    if not str(cfg.get("active_model") or "").strip():
        cfg["active_model"] = DEFAULT_MODEL
    if not str(cfg.get("master_key") or "").strip():
        cfg["master_key"] = DEFAULT_MASTER_KEY
        try:
            save_config(cfg)
        except Exception:
            log.exception("persist default master_key failed")
    return cfg


def save_config(cfg: dict) -> None:
    cfg = dict(cfg)
    cfg["probe"] = _normalize_probe_config(cfg.get("probe"))
    db.save_config_raw(cfg)


# ---------------------------------------------------------------------------
# 公网调用 / IP 访问控制
# ---------------------------------------------------------------------------


def public_config_default() -> dict:
    return {
        "enabled": False,
        "public_url": "",
        "mode": "blacklist",
        "allow_loopback": True,
        "trust_proxy_headers": False,
        "blocked": [],
        "allowed": [],
    }


def _valid_ip_rule(value: str) -> bool:
    s = str(value or "").strip()
    if not s:
        return False
    try:
        ipaddress.ip_network(s, strict=False)
        return True
    except ValueError:
        return False


def _clean_ip_list(raw: Any) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        s = str(item or "").strip()
        if s and _valid_ip_rule(s) and s not in out:
            out.append(s)
    return out


def normalize_public_config(raw: Any) -> dict:
    default = public_config_default()
    data = raw if isinstance(raw, dict) else {}
    mode = str(data.get("mode") or "blacklist").strip().lower()
    return {
        "enabled": bool(data.get("enabled", default["enabled"])),
        "public_url": str(data.get("public_url") or default["public_url"]).strip(),
        "mode": mode if mode in ("blacklist", "whitelist") else "blacklist",
        "allow_loopback": bool(data.get("allow_loopback", default["allow_loopback"])),
        "trust_proxy_headers": bool(
            data.get("trust_proxy_headers", default["trust_proxy_headers"])
        ),
        "blocked": _clean_ip_list(data.get("blocked")),
        "allowed": _clean_ip_list(data.get("allowed")),
    }


def load_public_config(cfg: Optional[dict] = None) -> dict:
    cfg = cfg if cfg is not None else load_config()
    return normalize_public_config(cfg.get("public"))


def save_public_config(public: Any) -> dict:
    cfg = load_config()
    cfg["public"] = normalize_public_config(public)
    save_config(cfg)
    return cfg["public"]


def public_access_enabled(cfg: Optional[dict] = None) -> bool:
    return bool(load_public_config(cfg).get("enabled"))


def is_loopback_ip(ip: str) -> bool:
    try:
        return bool(ipaddress.ip_address(str(ip or "").strip()).is_loopback)
    except ValueError:
        return str(ip or "").strip() in ("127.0.0.1", "::1")


def _ip_matches_rule(rule: str, ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(str(ip or "").strip())
    except ValueError:
        return False
    try:
        return addr in ipaddress.ip_network(str(rule or "").strip(), strict=False)
    except ValueError:
        return False


def ip_allowed(ip: str, cfg: Optional[dict] = None) -> bool:
    """Return whether a caller IP may hit the public proxy endpoint."""
    public = load_public_config(cfg)
    if public.get("allow_loopback") and is_loopback_ip(ip):
        return True
    if public.get("mode") == "whitelist":
        return any(_ip_matches_rule(r, ip) for r in public.get("allowed") or [])
    return not any(_ip_matches_rule(r, ip) for r in public.get("blocked") or [])


# ---------------------------------------------------------------------------
# 探测设置
# ---------------------------------------------------------------------------


def default_probe_enabled_for_model(model: str) -> bool:
    """OpenAI / gpt 客户端模型默认开探测；DeepSeek 默认关。"""
    m = str(model or "").strip()
    if not m:
        return True
    if codex_sync.is_deepseek_model(m) or "deepseek" in m.lower():
        return False
    return True


def default_probe_interval_for_model(model: str) -> int:
    return DEFAULT_PROBE_INTERVAL_SEC


def default_probe_enabled_for_upstream(
    multiplier: Any,
    name: Optional[str] = None,
    model: Optional[str] = None,
) -> bool:
    """默认每上游探测：倍率严格低于阈值才开。"""
    mult = normalize_upstream_multiplier(multiplier, name, model)
    return mult < PROBE_MULTIPLIER_THRESHOLD


def upstream_probe_enabled(u: dict) -> bool:
    if "probe_enabled" in u:
        return bool(u.get("probe_enabled"))
    return default_probe_enabled_for_upstream(
        u.get("multiplier"), u.get("name"), u.get("model")
    )


def _clamp_probe_interval(value: Any, default: int = DEFAULT_PROBE_INTERVAL_SEC) -> int:
    try:
        interval_sec = int(value)
    except (TypeError, ValueError):
        interval_sec = default
    return max(60, min(interval_sec, 86400))


def _normalize_model_probe_entry(value: Any, model: str) -> dict[str, Any]:
    default_enabled = default_probe_enabled_for_model(model)
    default_interval = default_probe_interval_for_model(model)
    if isinstance(value, bool):
        return {"enabled": value, "interval_sec": default_interval}
    if isinstance(value, dict):
        enabled = value.get("enabled")
        if enabled is None and "probe_enabled" in value:
            enabled = value.get("probe_enabled")
        if enabled is None:
            enabled = default_enabled
        interval = value.get("interval_sec", value.get("interval", default_interval))
        return {
            "enabled": bool(enabled),
            "interval_sec": _clamp_probe_interval(interval, default_interval),
        }
    return {"enabled": default_enabled, "interval_sec": default_interval}


def _normalize_probe_config(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    interval_sec = _clamp_probe_interval(
        raw.get("interval_sec", DEFAULT_PROBE_INTERVAL_SEC),
        DEFAULT_PROBE_INTERVAL_SEC,
    )
    models_raw = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    models: dict[str, dict[str, Any]] = {}
    for key, value in models_raw.items():
        name = str(key or "").strip()
        if not name:
            continue
        models[name] = _normalize_model_probe_entry(value, name)
    return {"interval_sec": interval_sec, "models": models}


def probe_interval_sec(cfg: Optional[dict] = None) -> int:
    cfg = cfg if cfg is not None else load_config()
    probe = _normalize_probe_config(cfg.get("probe"))
    return int(probe.get("interval_sec") or DEFAULT_PROBE_INTERVAL_SEC)


def probe_settings_for_model(model: str, cfg: Optional[dict] = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else load_config()
    probe = _normalize_probe_config(cfg.get("probe"))
    m = str(model or "").strip()
    models = probe.get("models") or {}
    if m in models:
        return dict(models[m])
    return {
        "enabled": default_probe_enabled_for_model(m),
        "interval_sec": int(
            probe.get("interval_sec") or default_probe_interval_for_model(m)
        ),
    }


def probe_enabled_for_model(model: str, cfg: Optional[dict] = None) -> bool:
    return bool(probe_settings_for_model(model, cfg).get("enabled"))


def probe_interval_for_model(model: str, cfg: Optional[dict] = None) -> int:
    return int(
        probe_settings_for_model(model, cfg).get("interval_sec")
        or DEFAULT_PROBE_INTERVAL_SEC
    )


def set_probe_enabled_for_model(model: str, enabled: bool) -> dict:
    cfg = load_config()
    probe = _normalize_probe_config(cfg.get("probe"))
    m = str(model or "").strip()
    entry = probe_settings_for_model(m, cfg)
    entry["enabled"] = bool(enabled)
    probe["models"][m] = entry
    cfg["probe"] = probe
    save_config(cfg)
    return probe


def save_probe_settings(
    *,
    models: dict[str, Any],
    default_interval_sec: Optional[int] = None,
) -> dict:
    cfg = load_config()
    probe = _normalize_probe_config(cfg.get("probe"))
    if default_interval_sec is not None:
        probe["interval_sec"] = _clamp_probe_interval(
            default_interval_sec, probe["interval_sec"]
        )
    cleaned: dict[str, dict[str, Any]] = {}
    for key, value in (models or {}).items():
        name = str(key or "").strip()
        if not name:
            continue
        cleaned[name] = _normalize_model_probe_entry(value, name)
    existing = dict(probe.get("models") or {})
    existing.update(cleaned)
    probe["models"] = existing
    cfg["probe"] = probe
    save_config(cfg)
    return probe


# ---------------------------------------------------------------------------
# 路由池
# ---------------------------------------------------------------------------


def light_for_multiplier(multiplier: Optional[float], ok: bool) -> str:
    """green (<0.1) / yellow (>=0.1) / red (全部失败)。"""
    if not ok or multiplier is None:
        return "red"
    if float(multiplier) < PROBE_MULTIPLIER_THRESHOLD:
        return "green"
    return "yellow"


def pool_for_client_model(client_model: str) -> str:
    """把客户端模型名映射到路由池。DeepSeek / Grok slug 统一进各自通用池。"""
    m = str(client_model or "").strip()
    if not m:
        return DEFAULT_MODEL
    if m == DEEPSEEK_POOL or codex_sync.is_deepseek_model(m):
        return DEEPSEEK_POOL
    if m == GROK_POOL or grok_sync.is_grok_model(m):
        return GROK_POOL
    pools = collect_models()
    for pool in pools:
        if normalize_model(pool) == m:
            return pool
    return DEFAULT_MODEL


def resolve_route_pool(
    client_model: Optional[str], active_model: Optional[str] = None
) -> str:
    cm = str(client_model or "").strip()
    if cm:
        return pool_for_client_model(cm)
    active = normalize_model(active_model or load_config().get("active_model"))
    if active == codex_sync.LOCAL_DIRECT:
        return DEFAULT_MODEL
    if active == DEEPSEEK_POOL or codex_sync.is_deepseek_model(active):
        return DEEPSEEK_POOL
    if active == GROK_POOL or grok_sync.is_grok_model(active):
        return GROK_POOL
    return active


def enabled_upstreams_for_pool(pool: str) -> list[dict]:
    pool = normalize_model(pool)
    return [
        u
        for u in load_upstreams()
        if bool(u.get("enabled", True)) and normalize_model(u.get("model")) == pool
    ]


def _upstream_effective_priority(u: dict) -> float:
    base = int(u.get("priority", 100))
    try:
        bias = float(u.get("ratio_priority_bias") or 0.0)
    except (TypeError, ValueError):
        bias = 0.0
    if not math.isfinite(bias):
        bias = 0.0
    return base + bias


def sort_upstreams_by_multiplier(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda u: (
            upstream_multiplier_value(u),
            _upstream_effective_priority(u),
            str(u.get("name") or ""),
        ),
    )


def sort_upstreams_by_priority(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda u: (
            _upstream_effective_priority(u),
            upstream_multiplier_value(u),
            str(u.get("name") or ""),
        ),
    )


def _model_availability_age_sec(snap: Optional[dict]) -> Optional[float]:
    if not snap or not snap.get("checked_at"):
        return None
    dt = _parse_ts(snap.get("checked_at"))
    if dt is None:
        return None
    return max(0.0, (datetime.now(BEIJING_TZ) - dt).total_seconds())


def _model_cache_priority_valid(snap: Optional[dict]) -> bool:
    if not snap or not snap.get("ok"):
        return False
    age = _model_availability_age_sec(snap)
    return age is not None and age <= CACHE_PRIORITY_TTL_SEC


def find_upstream_by_id_or_name(
    items: list[dict],
    *,
    upstream_id: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[dict]:
    if upstream_id:
        for u in items:
            if str(u.get("id") or "") == str(upstream_id):
                return u
    if name:
        for u in items:
            if str(u.get("name") or "") == str(name):
                return u
    return None


def order_candidates_for_model(
    client_model: Optional[str],
    pool: str,
    *,
    exclude_ids: Optional[set[str]] = None,
) -> list[dict]:
    items = [
        u
        for u in enabled_upstreams_for_pool(pool)
        if upstream_supports_model(u, client_model)
    ]
    if not items:
        return []
    exclude_ids = exclude_ids or set()
    ordered = sort_upstreams_by_multiplier(items)

    avail = state.get_model_availability_cached(client_model)
    preferred: Optional[dict] = None
    if _model_cache_priority_valid(avail):
        preferred = find_upstream_by_id_or_name(
            items,
            upstream_id=avail.get("upstream_id"),
            name=avail.get("upstream"),
        )

    result: list[dict] = []
    seen: set[str] = set()

    def _add(u: dict) -> None:
        uid = str(u.get("id") or "")
        if not uid or uid in seen or uid in exclude_ids:
            return
        seen.add(uid)
        result.append(u)

    if preferred is not None:
        _add(preferred)
    for u in ordered:
        _add(u)
    # 已失败（exclude）的上游不应在同一请求里被兜底重试。
    if not result:
        for u in ordered:
            uid = str(u.get("id") or "")
            if uid and uid not in seen and uid not in exclude_ids:
                seen.add(uid)
                result.append(u)
    return result


def collect_client_models_for_availability() -> list[str]:
    names: set[str] = set(DEFAULT_CLIENT_MODELS)
    for key in load_pricing().keys():
        k = str(key or "").strip()
        if not k or k == DEFAULT_MODEL:
            continue
        names.add(k)
    if any(
        normalize_model(u.get("model")) == DEEPSEEK_POOL for u in load_upstreams()
    ):
        names.update(DEEPSEEK_CLIENT_MODELS)
    if any(
        normalize_model(u.get("model")) == GROK_POOL for u in load_upstreams()
    ):
        names.update(GROK_CLIENT_MODELS)
    head = [m for m in DEFAULT_CLIENT_MODELS if m in names]
    rest = sorted(n for n in names if n not in head)
    return head + rest


def upstream_health_status(u: dict, health: Optional[dict] = None) -> str:
    if not bool(u.get("enabled", True)):
        return "disabled"
    if health is None:
        return "ok"
    if health.get("ok") is False:
        return "error"
    return "ok"


# ---------------------------------------------------------------------------
# 上游 / 模型归一化
# ---------------------------------------------------------------------------


def normalize_model(value: Optional[str]) -> str:
    m = (value or DEFAULT_MODEL).strip()
    return m or DEFAULT_MODEL


def normalize_request_model(
    value: Optional[str], model: Optional[str] = None
) -> Optional[str]:
    m = str(value or "").strip()
    return m or None


def normalize_model_map_entry(value: Any) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    name = str(value.get("model") or "").strip()
    if not name:
        return None
    actual = str(value.get("actual") or "").strip()
    return {"model": name, "actual": actual or name}


def normalize_model_map(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in value:
        entry = normalize_model_map_entry(raw)
        if entry is None:
            continue
        key = entry["model"]
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def default_model_map_for(model: Optional[str]) -> list[dict]:
    pool = normalize_model(model)
    if pool == DEEPSEEK_POOL or codex_sync.is_deepseek_model(pool):
        return normalize_model_map([dict(e) for e in DEEPSEEK_DEFAULT_MODEL_MAP])
    if pool == GROK_POOL or grok_sync.is_grok_model(pool):
        return normalize_model_map([dict(e) for e in GROK_DEFAULT_MODEL_MAP])
    if pool == DEFAULT_MODEL:
        return normalize_model_map([dict(e) for e in DEFAULT_OPENAI_ALL_MODEL_MAP])
    return [{"model": pool, "actual": pool}]


def upstream_supports_model(upstream: dict, client_model: Optional[str]) -> bool:
    entries = upstream.get("model_map")
    if not isinstance(entries, list) or not entries:
        return True
    cm = str(client_model or "").strip()
    if not cm:
        return True
    return any(str(e.get("model") or "").strip() == cm for e in entries)


def upstream_supports_standalone_web_search(upstream: dict) -> bool:
    """Whether an upstream can receive Codex's standalone search request.

    The endpoint is an OpenAI Responses-side capability. Chat and Anthropic
    adapters cannot preserve its request/response contract, so they are
    intentionally excluded. ``standalone_web_search=False`` is an explicit
    operator opt-out; absent metadata keeps legacy OpenAI upstreams usable.
    """
    if bool(upstream.get("chat_completions")) or bool(upstream.get("anthropic_messages")):
        return False
    if upstream.get("standalone_web_search") is False:
        return False
    return normalize_model(upstream.get("model")) == DEFAULT_MODEL


def upstream_request_model(
    upstream: dict, client_model: Optional[str] = None
) -> Optional[str]:
    entries = upstream.get("model_map")
    cm = str(client_model or "").strip()
    if isinstance(entries, list) and entries:
        for e in entries:
            if cm and str(e.get("model") or "").strip() == cm:
                return str(e.get("actual") or "").strip() or cm
        return None
    raw = normalize_request_model(upstream.get("request_model"))
    if raw is not None and raw != normalize_model(upstream.get("model")):
        return raw
    return None


def normalize_base_url(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if re.match(r"^https?://", raw, flags=re.IGNORECASE):
        return raw.rstrip("/")
    raw = re.sub(r"^(?:https?://)+", "", raw, flags=re.IGNORECASE).rstrip("/")
    if not raw:
        return ""
    return "https://" + raw


def default_upstream_multiplier(name: Optional[str], model: Optional[str]) -> float:
    """默认倍率固定为 1.0，由用户在表单里显式覆盖。"""
    return 1.0


def normalize_upstream_multiplier(
    value: Any,
    name: Optional[str] = None,
    model: Optional[str] = None,
) -> float:
    if value is not None:
        try:
            multiplier = float(value)
            if math.isfinite(multiplier) and multiplier >= 0:
                return multiplier
        except (TypeError, ValueError):
            pass
    return default_upstream_multiplier(name, model)


def upstream_multiplier_value(upstream: dict) -> float:
    return normalize_upstream_multiplier(
        upstream.get("multiplier"), upstream.get("name"), upstream.get("model")
    )


def upstream_multiplier_for(name: Optional[str]) -> float:
    if not name:
        return 1.0
    target = str(name)
    for upstream in load_upstreams():
        if str(upstream.get("name") or "") == target:
            return upstream_multiplier_value(upstream)
    return 1.0


def entry_multiplier(entry: dict) -> float:
    multiplier = _float_or_none(entry.get("multiplier"))
    if multiplier is not None:
        return multiplier
    return upstream_multiplier_for(entry.get("upstream"))


def _current_upstream_multiplier_lookup() -> dict[str, float]:
    out: dict[str, float] = {}
    for upstream in load_upstreams():
        name = str(upstream.get("name") or "")
        if name:
            out[name] = upstream_multiplier_value(upstream)
    return out


def _multiplier_from_lookup(entry: dict, lookup: dict[str, float]) -> float:
    existing = _float_or_none(entry.get("multiplier"))
    if existing is not None:
        return existing
    return lookup.get(str(entry.get("upstream") or ""), 1.0)


def load_upstreams() -> list[dict]:
    return db.load_upstreams()


def save_upstreams(items: list[dict]) -> None:
    db.save_upstreams(items)


def mask_key(key: str) -> str:
    s = str(key or "")
    if len(s) <= 8:
        return "****"
    return s[:4] + "****" + s[-4:]


def collect_models(items: Optional[list[dict]] = None) -> list[str]:
    items = items if items is not None else load_upstreams()
    names: set[str] = {DEFAULT_MODEL}
    for u in items:
        names.add(normalize_model(u.get("model")))
    rest = sorted(n for n in names if n != DEFAULT_MODEL)
    return [DEFAULT_MODEL] + rest


def _request_model_label(entry: dict) -> str:
    value = str(entry.get("client_model") or "").strip()
    return value or "未知模型"


def _request_model_counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in items:
        model = _request_model_label(entry)
        counts[model] = counts.get(model, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


# ---------------------------------------------------------------------------
# 计费
# ---------------------------------------------------------------------------


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) and f >= 0 else None
    except (TypeError, ValueError):
        return None


def default_pricing() -> dict:
    """内置默认单价的深拷贝，调用方可以随意改。"""
    return copy.deepcopy(DEFAULT_PRICING)


def load_pricing() -> dict:
    """内置默认单价 + 数据库覆盖。

    数据库 config.pricing 里存在同名模型时整条替换默认值（而不是按字段合并），
    这样在面板里清掉某个字段就是真的清掉，不会被默认值补回来。数据库没写过的
    模型直接用 DEFAULT_PRICING，保证仓库 clone 下来就有正确单价。
    """
    merged = copy.deepcopy(DEFAULT_PRICING)
    stored = load_config().get("pricing")
    if isinstance(stored, dict):
        for key, vals in stored.items():
            k = str(key or "").strip()
            if not k or not isinstance(vals, dict):
                continue
            merged[k] = copy.deepcopy(vals)
    return merged


def save_pricing(pricing: dict) -> None:
    """只把与内置默认不同的条目写库，与默认一致的继续跟随 DEFAULT_PRICING。"""
    overrides: dict[str, dict] = {}
    for key, vals in (pricing or {}).items():
        k = str(key or "").strip()
        if not k or not isinstance(vals, dict):
            continue
        if DEFAULT_PRICING.get(k) == vals:
            continue
        overrides[k] = vals
    cfg = load_config()
    cfg["pricing"] = overrides
    save_config(cfg)


PRICE_KEYS = ("input_per_m", "output_per_m", "cache_read_per_m", "cache_creation_per_m")
# 长文本档阈值官方默认值：输入上下文超过该值按 long_context 具体单价计费
DEFAULT_LONG_CONTEXT_THRESHOLD = 272000


def _parse_long_context(raw: Any) -> Optional[dict]:
    """解析模型配置里可选的长文本档 long_context 子对象（具体单价，非倍率）。

    未配置返回 None；配置了返回 {threshold, 4 个单价键}，缺失的单价键为 None
    （计算时回退到基础价），threshold 缺省 272000。只有阈值没有价格时视为未配置。
    """
    if not isinstance(raw, dict):
        return None
    if not any(k in raw for k in ("threshold",) + PRICE_KEYS):
        return None
    threshold = _float_or_none(raw.get("threshold"))
    if threshold is None:
        threshold = float(DEFAULT_LONG_CONTEXT_THRESHOLD)
    prices = {k: _float_or_none(raw.get(k)) for k in PRICE_KEYS}
    if not any(v is not None for v in prices.values()):
        return None
    return {"threshold": threshold, **prices}


def _is_long_context(pr: dict, input_tokens: Optional[int]) -> bool:
    lc = pr.get("long_context")
    return (
        isinstance(lc, dict)
        and lc.get("threshold") is not None
        and input_tokens is not None
        and int(input_tokens) > lc["threshold"]
    )


def _effective_prices(pr: dict, input_tokens: Optional[int]) -> dict:
    """按输入上下文长度选出本条日志实际生效的单价。

    未配置 long_context 或输入未超阈值时返回基础单价；超阈值时返回
    long_context 里配置的具体单价，缺失键回退基础价。
    """
    if not _is_long_context(pr, input_tokens):
        return {k: pr.get(k) for k in PRICE_KEYS}
    lc = pr["long_context"]
    return {k: lc.get(k) if lc.get(k) is not None else pr.get(k) for k in PRICE_KEYS}


def pricing_for(pool: str, client_model: Optional[str] = None) -> dict:
    empty = {k: None for k in PRICE_KEYS}
    fallback = dict(empty)
    fallback_lc: Optional[dict] = None
    seen: set[str] = set()
    for key in (client_model, pool):
        key = str(key or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        raw = load_pricing().get(key)
        if not isinstance(raw, dict):
            continue
        current = {k: _float_or_none(raw.get(k)) for k in PRICE_KEYS}
        current_lc = _parse_long_context(raw.get("long_context"))
        if current["input_per_m"] is not None and current["output_per_m"] is not None:
            return {**current, "long_context": current_lc}
        if fallback == empty:
            fallback = current
            fallback_lc = current_lc
    return {**fallback, "long_context": fallback_lc}


def compute_cost_usd(entry: dict) -> Optional[float]:
    input_tokens = entry.get("input_tokens")
    output_tokens = entry.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    pr = _effective_prices(
        pricing_for(entry.get("pool") or "", entry.get("client_model")),
        entry.get("input_tokens"),
    )
    inp = pr["input_per_m"]
    out = pr["output_per_m"]
    if inp is None or out is None:
        return None
    inp_n = int(input_tokens or 0)
    cache_read = int(entry.get("cache_read_tokens") or 0)
    cache_create = int(entry.get("cache_creation_tokens") or 0)
    if cache_read == 0 and cache_create == 0:
        cache_read = int(entry.get("cached_tokens") or 0)
    uncached = max(inp_n - cache_read - cache_create, 0)
    cache_read_price = pr["cache_read_per_m"]
    if cache_read_price is None:
        cache_read_price = inp
    cache_create_price = pr.get("cache_creation_per_m")
    if cache_create_price is None:
        cache_create_price = inp
    cost = (
        uncached * inp
        + cache_read * cache_read_price
        + cache_create * cache_create_price
        + int(output_tokens or 0) * out
    )
    return cost / 1_000_000


def _apply_min_real_cost(raw: Any) -> Optional[float]:
    """Apply NewAPI 1-quota floor so tiny grok bills don't round to $0."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return 0.0 if math.isfinite(value) and value == 0 else None
    if value < MIN_REAL_COST:
        value = MIN_REAL_COST
    return round(value, 8)


def compute_real_cost_cny(
    entry: dict,
    cost_usd: Optional[float] = None,
    multiplier: Optional[float] = None,
) -> Optional[float]:
    if cost_usd is None:
        cost_usd = compute_cost_usd(entry)
    if cost_usd is None:
        return None
    if multiplier is None:
        multiplier = entry_multiplier(entry)
    return _apply_min_real_cost(cost_usd * multiplier)


def cost_breakdown(entry: dict) -> Optional[dict]:
    input_tokens = entry.get("input_tokens")
    output_tokens = entry.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    base_pr = pricing_for(entry.get("pool") or "", entry.get("client_model"))
    pr = _effective_prices(base_pr, entry.get("input_tokens"))
    inp = pr["input_per_m"]
    out = pr["output_per_m"]
    if inp is None or out is None:
        return None
    cache_read = int(entry.get("cache_read_tokens") or 0)
    cache_create = int(entry.get("cache_creation_tokens") or 0)
    if cache_read == 0 and cache_create == 0:
        cache_read = int(entry.get("cached_tokens") or 0)
    uncached = max(int(input_tokens or 0) - cache_read - cache_create, 0)
    cache_read_price = pr["cache_read_per_m"]
    if cache_read_price is None:
        cache_read_price = inp
    cache_create_price = pr.get("cache_creation_per_m")
    if cache_create_price is None:
        cache_create_price = inp
    out_n = int(output_tokens or 0)
    lines = [
        {
            "label": "输入",
            "tokens": uncached,
            "unit_price": inp,
            "cost": round(uncached * inp / 1_000_000, 8),
        },
        {
            "label": "缓存读取",
            "tokens": cache_read,
            "unit_price": cache_read_price,
            "cost": round(cache_read * cache_read_price / 1_000_000, 8),
        },
        {
            "label": "缓存写入",
            "tokens": cache_create,
            "unit_price": cache_create_price,
            "cost": round(cache_create * cache_create_price / 1_000_000, 8),
        },
        {
            "label": "输出",
            "tokens": out_n,
            "unit_price": out,
            "cost": round(out_n * out / 1_000_000, 8),
        },
    ]
    total = round(sum(line["cost"] for line in lines), 8)
    mult = entry_multiplier(entry)
    real = _apply_min_real_cost(total * mult) if mult is not None else None
    return {
        "rows": lines,
        "total": total,
        "multiplier": mult,
        "real_cost_cny": real,
        "tier": "long_context" if _is_long_context(base_pr, entry.get("input_tokens")) else "standard",
        "long_context_threshold": (base_pr.get("long_context") or {}).get("threshold"),
    }


def tps_output_seconds(entry: dict) -> Optional[float]:
    duration_ms = entry.get("duration_ms")
    ttft_ms = entry.get("ttft_ms")
    if duration_ms is None or ttft_ms is None:
        return None
    try:
        secs = (float(duration_ms) - float(ttft_ms)) / 1000.0
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def compute_tps(entry: dict) -> Optional[float]:
    output = entry.get("output_tokens")
    if output is None:
        return None
    try:
        tokens = int(output)
    except (TypeError, ValueError):
        return None
    if tokens <= 0:
        return None
    secs = tps_output_seconds(entry)
    if secs is None:
        return None
    return round(tokens / secs, 1)


# ---------------------------------------------------------------------------
# 上游公开视图 / 切换
# ---------------------------------------------------------------------------


def public_upstream(u: dict, health_map: Optional[dict[str, dict]] = None) -> dict:
    health_map = health_map if health_map is not None else state.probe_health_snapshot()
    health = health_map.get(str(u.get("id") or ""))
    status = upstream_health_status(u, health)
    model = normalize_model(u.get("model"))
    return {
        "id": u["id"],
        "name": u["name"],
        "base_url": u["base_url"],
        "api_key_masked": mask_key(u.get("api_key", "")),
        "priority": int(u.get("priority", 100)),
        "enabled": bool(u.get("enabled", True)),
        "model": model,
        "model_map": normalize_model_map(u.get("model_map")),
        "multiplier": upstream_multiplier_value(u),
        "probe_enabled": upstream_probe_enabled(u),
        "chat_completions": bool(u.get("chat_completions", False)),
        "anthropic_messages": bool(u.get("anthropic_messages", False)),
        "standalone_web_search": (
            None
            if u.get("standalone_web_search") is None
            else bool(u.get("standalone_web_search"))
        ),
        "ratio_source": u.get("ratio_source") or None,
        "ratio_group": u.get("ratio_group") or None,
        "status": status,
        "probe_ok": None if health is None else bool(health.get("ok")),
        "probe_status_code": None if health is None else health.get("status_code"),
        "probe_error": None if health is None else health.get("error"),
        "probe_checked_at": None if health is None else health.get("checked_at"),
        "probe_duration_ms": None if health is None else health.get("duration_ms"),
        "probe_model": None if health is None else health.get("probe_model"),
        "probe_source": None if health is None else health.get("source"),
    }


def _apply_active_model(active: str) -> dict[str, Any]:
    active = normalize_model(active)
    items = load_upstreams()
    known = set(collect_models(items)) | set(DEEPSEEK_CLIENT_MODELS)
    if active != codex_sync.LOCAL_DIRECT and active not in known:
        raise HTTPException(
            status_code=400,
            detail=f"未知模型 {active!r}。请先在上游上创建该 model，或选择已有模型。",
        )

    try:
        codex_result = codex_sync.sync_for_active_model(active)
    except Exception as e:
        log.exception("codex sync failed for %s", active)
        raise HTTPException(status_code=500, detail=f"Codex 配置同步失败: {e}") from e

    cfg = load_config()
    cfg["active_model"] = active
    save_config(cfg)

    if active == DEEPSEEK_POOL:
        scoped = [
            u
            for u in items
            if u.get("enabled", True)
            and normalize_model(u.get("model")) == DEEPSEEK_POOL
        ]
    elif codex_sync.is_deepseek_model(active):
        scoped = [
            u
            for u in items
            if u.get("enabled", True)
            and normalize_model(u.get("model")) == DEEPSEEK_POOL
            and upstream_supports_model(u, active)
        ]
    else:
        scoped = [
            u
            for u in items
            if u.get("enabled", True) and normalize_model(u.get("model")) == active
        ]
    log.info(
        "active_model=%s scoped=%s codex_mode=%s",
        active,
        len(scoped),
        codex_result.get("mode"),
    )
    return {
        "active_model": active,
        "models": collect_models(items),
        "upstreams_in_scope": len(scoped),
        "codex": codex_result,
        "codex_status": codex_sync.status(),
    }


def _apply_claude_config(mode: str, model: Optional[str] = None) -> dict[str, Any]:
    """Claude Code 配置同步：local-direct / openai-all / deepseek 三种模式。

    deepseek 是整体模式：保留当前 DeepSeek 模型作为默认（无则 flash），
    /model 在 Claude 内切换。不改全局 active_model（那是 Codex 卡共用的
    路由默认池）；Claude 请求自带 model，pool_for_client_model 会归池。
    """
    m = (mode or "").strip()
    if m not in ("local-direct", "openai-all", "deepseek"):
        raise HTTPException(
            status_code=400,
            detail=f"未知 Claude 模式 {m!r}（仅支持 local-direct / openai-all / deepseek）",
        )
    slug = (model or "").strip() or None
    if m == "deepseek" and not codex_sync.is_deepseek_model(slug or ""):
        slug = claude_sync.current_deepseek_slug() or "deepseek-v4-flash"

    try:
        result = claude_sync.sync_for_mode(m, slug)
    except Exception as e:
        log.exception("claude sync failed for mode=%s model=%s", m, slug)
        raise HTTPException(status_code=500, detail=f"Claude Code 配置同步失败: {e}") from e

    return {
        "mode": m,
        "active_model": normalize_model(load_config().get("active_model")),
        "claude": claude_sync.status(),
        "changes": result.get("changes") or result.get("actions") or [],
    }


def _apply_claude_bridge(enabled: bool) -> dict[str, Any]:
    """启停 Claude auto-mode-bridge PreToolUse hook。

    仿 _apply_claude_config 的 try/except 风格：失败抛 HTTPException 500
    （detail 为中文信息，如 "auto-mode-bridge hook 配置失败: {e}"），
    成功返回 {"enabled", "changes", "claude"}。
    """
    try:
        if enabled:
            changes = claude_sync.install_hook()
        else:
            changes = claude_sync.uninstall_hook()
    except Exception as e:
        log.exception("claude auto-mode-bridge hook config failed enabled=%s", enabled)
        raise HTTPException(
            status_code=500,
            detail=f"auto-mode-bridge hook 配置失败: {e}",
        ) from e
    return {
        "enabled": bool(enabled),
        "changes": list(changes),
        "claude": claude_sync.status(),
    }


def _apply_grok_config(mode: str, model: Optional[str] = None) -> dict[str, Any]:
    """Grok CLI 配置同步：local-direct / grok 两种模式。

    grok 是整体模式：受管段用当前 grok 客户端模型（无则 grok-4.6），
    /model 在 Grok 内切换；local-direct 恢复介入前快照、直连原端点。

    注意：不修改全局 active_model——那是 Codex 卡共用的路由默认池，Grok 请求
    自带 model=grok-4.6，pool_for_client_model 会直接归入 grok 池，与 active_model
    无关。改成 grok 不应让 Codex 卡的状态/配置跟着变。
    """
    m = (mode or "").strip()
    if m not in ("local-direct", "grok"):
        raise HTTPException(
            status_code=400,
            detail=f"未知 Grok 模式 {m!r}（仅支持 local-direct / grok）",
        )
    pool = (model or "").strip() or grok_sync.GROK_POOL
    if m == "grok" and not grok_sync.is_grok_pool(pool):
        raise HTTPException(
            status_code=400,
            detail=f"不是 Grok 池: {pool!r}（如 grok）",
        )

    try:
        result = grok_sync.sync_for_mode(m, pool)
    except Exception as e:
        log.exception("grok sync failed for mode=%s pool=%s", m, pool)
        raise HTTPException(status_code=500, detail=f"Grok 配置同步失败: {e}") from e

    return {
        "mode": m,
        "active_model": normalize_model(load_config().get("active_model")),
        "grok": grok_sync.status(),
        "changes": result.get("changes") or result.get("actions") or [],
    }


def _price_color_for_multiplier(mult: Optional[float]) -> Optional[str]:
    """倍率 → 颜色：0.03 深绿 → 0.20+ 亮黄。"""
    if mult is None:
        return None
    try:
        m = float(mult)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(m) or m < 0:
        return None
    stops = [
        (0.03, (4, 120, 87)),
        (0.05, (16, 185, 129)),
        (0.08, (34, 197, 94)),
        (0.10, (132, 204, 22)),
        (0.12, (202, 198, 14)),
        (0.15, (234, 179, 8)),
        (0.20, (250, 204, 21)),
        (0.30, (253, 224, 71)),
    ]
    if m <= stops[0][0]:
        r, g, b = stops[0][1]
        return f"#{r:02x}{g:02x}{b:02x}"
    if m >= stops[-1][0]:
        r, g, b = stops[-1][1]
        return f"#{r:02x}{g:02x}{b:02x}"
    for i in range(len(stops) - 1):
        m0, c0 = stops[i]
        m1, c1 = stops[i + 1]
        if m0 <= m <= m1:
            u = 0.0 if m1 <= m0 else (m - m0) / (m1 - m0)
            r = int(round(c0[0] + (c1[0] - c0[0]) * u))
            g = int(round(c0[1] + (c1[1] - c0[1]) * u))
            b = int(round(c0[2] + (c1[2] - c0[2]) * u))
            return f"#{r:02x}{g:02x}{b:02x}"
    r, g, b = stops[-1][1]
    return f"#{r:02x}{g:02x}{b:02x}"
