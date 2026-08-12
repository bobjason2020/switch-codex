#!/usr/bin/env python3
"""Switch-codex 日志与统计层。

负责请求/错误日志的落库、耗时与 token 提取、聚合统计、筛选选项，
以及历史可用性时间线（首页/历史页的色块数据）。
"""
from __future__ import annotations

import json
import logging
import math
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sy import core, db, timeutil

from sy.const import (
    AVAIL_HISTORY_RETENTION_DAYS,
    COLOR_MID,
    COLOR_BAD,
    ERROR_LOG_BODY_MAX_BYTES,
    ERROR_LOG_RETENTION_HOURS,
    REQUEST_LOG_RETENTION_DAYS,
)

log = logging.getLogger("switchyard.logbook")

_now_beijing = timeutil.now_beijing
_parse_ts = timeutil.parse_ts
_timestamp_in_beijing = timeutil.timestamp_in_beijing
_entry_in_beijing = timeutil.entry_in_beijing
_iso_now = timeutil.iso_now
_last_req_prune = 0.0


def _maybe_prune_request_logs() -> None:
    global _last_req_prune
    now = time.time()
    if now - _last_req_prune < 3600:
        return
    _last_req_prune = now
    try:
        db.prune_request_logs(REQUEST_LOG_RETENTION_DAYS)
    except Exception:
        log.exception("prune request logs failed")


# ---------------------------------------------------------------------------
# usage 提取
# ---------------------------------------------------------------------------


def _token_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_usage_in_obj(obj: Any) -> Optional[dict]:
    if isinstance(obj, dict):
        if isinstance(obj.get("usage"), dict):
            return obj["usage"]
        for v in obj.values():
            found = _find_usage_in_obj(v)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_usage_in_obj(v)
            if found is not None:
                return found
    return None


def _extract_usage(raw: bytes) -> Optional[dict]:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
        found = _find_usage_in_obj(obj)
        if found is not None:
            return found
    except Exception:
        pass

    # 流式 usage 在不同协议里被拆开：
    # - Anthropic：message_start 带 input/cache，message_delta 只带 output；
    # - Responses：response.completed 一次性给全量。
    # 因此分别保留“最早见到的 input/cache”和“最后见到的 output”再合并，
    # 不能像旧实现那样只取最后一个 usage（Anthropic 流会丢 input/cache）。
    first_input: Optional[int] = None
    first_cache_read: Optional[int] = None
    first_cache_creation: Optional[int] = None
    last_output: Optional[int] = None
    last_full: Optional[dict] = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        found = _find_usage_in_obj(obj)
        if found is not None:
            last_full = found
            input_tokens = _token_int(found.get("input_tokens"))
            if input_tokens is not None and first_input is None:
                first_input = input_tokens
            cache_read = _token_int(found.get("cache_read_input_tokens"))
            if cache_read is not None and first_cache_read is None:
                first_cache_read = cache_read
            cache_creation = _token_int(found.get("cache_creation_input_tokens"))
            if cache_creation is not None and first_cache_creation is None:
                first_cache_creation = cache_creation
            output_tokens = _token_int(found.get("output_tokens"))
            if output_tokens is not None:
                last_output = output_tokens

    if last_full is None:
        return None
    merged = dict(last_full)
    if first_input is not None:
        merged["input_tokens"] = first_input
    if first_cache_read is not None:
        merged["cache_read_input_tokens"] = first_cache_read
    if first_cache_creation is not None:
        merged["cache_creation_input_tokens"] = first_cache_creation
    if last_output is not None:
        merged["output_tokens"] = last_output
    return merged


def _extract_session_id(headers: dict[str, str], body: dict) -> Optional[str]:
    """从客户端请求里提取会话 id（借鉴 cc-switch proxy/session.rs）。

    优先级：
    1. 头部 x-claude-code-session-id / claude-code-session-id（Claude Code）；
    2. 头部 x-grok-session-id（Grok CLI）；
    3. 头部 session_id / x-session-id（Codex/Responses 类客户端）；
    4. body.metadata.user_id（兼容 user_xxx_session_yyy 与 JSON 字符串）；
    5. body.metadata.session_id；
    6. body.prompt_cache_key（Grok CLI 用会话 UUID 做缓存键）。
    """
    for key in (
        "x-claude-code-session-id",
        "claude-code-session-id",
        "x-grok-session-id",
        "session-id",
        "x-session-id",
        "session_id",
    ):
        value = (headers.get(key) or headers.get(key.lower()) or "").strip()
        if value:
            return value

    if not isinstance(body, dict):
        return None
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        user_id = metadata.get("user_id")
        if isinstance(user_id, str) and user_id:
            # cc-switch 老格式：user_xxx_session_yyy
            if "_session_" in user_id:
                return user_id.split("_session_", 1)[1]
            # Claude Code 2.1.x 实测：user_id 是含 session_id 的 JSON 字符串
            try:
                parsed = json.loads(user_id)
                if isinstance(parsed, dict):
                    sid = parsed.get("session_id")
                    if isinstance(sid, str) and sid:
                        return sid
            except Exception:
                pass
        sid = metadata.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    for key in ("prompt_cache_key", "session_id"):
        sid = body.get(key)
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
    return None


def _extract_reasoning_effort(body: dict) -> Optional[str]:
    """Responses / Chat 请求里的思考强度。"""
    if not isinstance(body, dict):
        return None
    re_obj = body.get("reasoning")
    if isinstance(re_obj, dict):
        for key in ("effort", "reasoning_effort"):
            value = re_obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    elif isinstance(re_obj, str) and re_obj.strip():
        return re_obj.strip()
    for key in ("reasoning_effort", "effort"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _usage_numbers(usage: Optional[dict]) -> dict[str, Optional[int]]:
    usage = usage or {}
    detail_in = usage.get("input_tokens_details") or {}
    detail_out = usage.get("output_tokens_details") or {}
    input_tokens = _token_int(usage.get("input_tokens"))
    # 缓存：Anthropic 风格（cache_read_input_tokens / cache_creation_input_tokens），
    # 或 OpenAI 风格 input_tokens_details.cached_tokens。
    # 注意语义差异：Anthropic 的 input_tokens 同时排除 cache_read 与
    # cache_creation（两者都是额外量）；而前端用 uncached = input - cached
    # 展示，因此这里把两者都加回总输入，缓存量 = cache_read + cache_creation。
    cached_tokens = None
    cache_read = _token_int(usage.get("cache_read_input_tokens"))
    cache_creation = _token_int(usage.get("cache_creation_input_tokens"))
    if cache_read is not None or cache_creation is not None:
        cached_tokens = (cache_read or 0) + (cache_creation or 0)
        input_tokens = (input_tokens or 0) + (cache_read or 0) + (cache_creation or 0)
    elif isinstance(detail_in, dict):
        cached_tokens = _token_int(detail_in.get("cached_tokens"))
        cache_read = cached_tokens
    output_tokens = _token_int(usage.get("output_tokens"))
    # 思考：Anthropic/DeepSeek 可能顶层直接给 reasoning_tokens，OpenAI 风格在 details 里。
    reasoning_tokens = _token_int(usage.get("reasoning_tokens"))
    if reasoning_tokens is None and isinstance(detail_out, dict):
        reasoning_tokens = _token_int(detail_out.get("reasoning_tokens"))
    total = _token_int(usage.get("total_tokens"))
    if total is None and (input_tokens is not None or output_tokens is not None):
        total = (input_tokens or 0) + (output_tokens or 0)
    return {
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": total,
    }


def _merge_usage(existing: Optional[dict], found: dict) -> dict:
    """合并流式 usage：保留最早的 input/cache，覆盖最后的 output。"""
    incoming = dict(found)
    if not existing:
        return incoming
    merged = dict(existing)
    in_t = _token_int(incoming.get("input_tokens"))
    if in_t is not None and _token_int(merged.get("input_tokens")) is None:
        merged["input_tokens"] = in_t
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        val = _token_int(incoming.get(key))
        if val is not None and _token_int(merged.get(key)) is None:
            merged[key] = val
    out_t = _token_int(incoming.get("output_tokens"))
    if out_t is not None:
        merged["output_tokens"] = out_t
    for key, val in incoming.items():
        if key not in merged or merged.get(key) is None:
            merged[key] = val
    return merged


# ---------------------------------------------------------------------------
# 请求日志
# ---------------------------------------------------------------------------


def _is_probe_log(entry: dict) -> bool:
    if entry.get("is_probe") is True:
        return True
    path = str(entry.get("path") or "")
    if path.startswith("/api/upstreams/") and path.endswith("/test"):
        return True
    return False


def _record_log(**fields: Any) -> None:
    ttft = fields.pop("ttft_ms", None)
    upstream = fields.pop("upstream", None)
    multiplier = core._float_or_none(fields.pop("multiplier", None))
    if multiplier is None:
        multiplier = core.upstream_multiplier_for(upstream)
    is_probe = bool(fields.pop("is_probe", False))
    path = fields.pop("path", "/v1/responses")
    # 端口/协议标识：anthropic（/v1/messages）/ response（/v1/responses）/ chat（chat 转换上游）。
    endpoint = fields.pop("endpoint", None) or (
        "anthropic" if path == "/v1/messages" else "response"
    )
    entry = {
        "ts": _iso_now(),
        "client_ip": fields.pop("client_ip", ""),
        "method": fields.pop("method", "POST"),
        "path": path,
        "endpoint": endpoint,
        "session_id": fields.pop("session_id", None),
        "pool": fields.pop("pool", ""),
        "client_model": fields.pop("client_model", None),
        "reasoning_effort": fields.pop("reasoning_effort", None),
        "upstream": upstream,
        "upstream_url": fields.pop("upstream_url", None),
        "multiplier": multiplier,
        "status": fields.pop("status", None),
        "duration_ms": round(float(fields.pop("duration_ms", 0.0) or 0.0), 1),
        "ttft_ms": round(float(ttft), 1) if ttft is not None else None,
        "error_log_id": fields.pop("error_log_id", None),
        "attempts": fields.pop("attempts", None),
        "input_tokens": _token_int(fields.pop("input_tokens", None)),
        "cached_tokens": _token_int(fields.pop("cached_tokens", None)),
        "cache_read_tokens": _token_int(fields.pop("cache_read_tokens", None)),
        "cache_creation_tokens": _token_int(fields.pop("cache_creation_tokens", None)),
        "output_tokens": _token_int(fields.pop("output_tokens", None)),
        "reasoning_tokens": _token_int(fields.pop("reasoning_tokens", None)),
        "total_tokens": _token_int(fields.pop("total_tokens", None)),
        "stream": bool(fields.pop("stream", False)),
        "is_probe": is_probe,
        "is_classifier": bool(fields.pop("is_classifier", False)),
        "upstream_id": fields.pop("upstream_id", None),
    }
    if not entry["upstream_id"] and upstream:
        try:
            for u in core.load_upstreams():
                if str(u.get("name") or "") == str(upstream):
                    entry["upstream_id"] = u.get("id")
                    break
        except Exception:
            log.exception("backfill upstream_id failed")
    try:
        db.insert_request_log(entry)
        _maybe_prune_request_logs()
    except Exception:
        log.exception("persist request log failed")


def _logs_snapshot() -> list[dict]:
    return db.load_request_logs()


def _traffic_logs_snapshot() -> list[dict]:
    return [e for e in _logs_snapshot() if not _is_probe_log(e)]


def _backfill_log_multipliers_from_current_upstreams() -> int:
    lookup = core._current_upstream_multiplier_lookup()
    items = db.load_request_logs()
    changed = 0
    for obj in items:
        timestamp = _timestamp_in_beijing(obj.get("ts"))
        if timestamp is not None and timestamp != obj.get("ts"):
            obj["ts"] = timestamp
            changed += 1
        if core._float_or_none(obj.get("multiplier")) is None:
            obj["multiplier"] = core._multiplier_from_lookup(obj, lookup)
            changed += 1
    if changed:
        db.replace_request_logs(items)
        log.info("normalized request log fields count=%s", changed)
    return changed


_backfill_log_multipliers_from_current_upstreams()


def _clear_logs() -> None:
    db.clear_request_logs()


# ---------------------------------------------------------------------------
# 错误日志
# ---------------------------------------------------------------------------


def _request_body_for_log(raw: bytes) -> tuple[Any, int, bool]:
    if not raw:
        return None, 0, False
    if len(raw) <= ERROR_LOG_BODY_MAX_BYTES:
        try:
            return json.loads(raw), len(raw), False
        except Exception:
            return raw.decode("utf-8", errors="replace"), len(raw), False
    txt = raw.decode("utf-8", errors="replace")
    return txt[:ERROR_LOG_BODY_MAX_BYTES] + "\n...[truncated]", len(raw), True


def _record_error_log(**fields: Any) -> str:
    entry = {
        "id": uuid.uuid4().hex,
        "ts": _iso_now(),
        "client_ip": fields.pop("client_ip", ""),
        "method": fields.pop("method", "POST"),
        "path": fields.pop("path", "/v1/responses"),
        "pool": fields.pop("pool", ""),
        "client_model": fields.pop("client_model", None),
        "stream": bool(fields.pop("stream", False)),
        "is_probe": bool(fields.pop("is_probe", False)),
        "status": fields.pop("status", None),
        "error": fields.pop("error", None),
        "duration_ms": round(float(fields.pop("duration_ms", 0.0) or 0.0), 1),
        "request_body": fields.pop("request_body", None),
        "request_body_len": fields.pop("request_body_len", None),
        "request_body_truncated": bool(fields.pop("request_body_truncated", False)),
        "attempts": fields.pop("attempts", []),
    }
    try:
        db.insert_error_log(entry)
        db.prune_error_logs(ERROR_LOG_RETENTION_HOURS)
    except Exception:
        log.exception("persist error log failed")
    return entry["id"]


def _error_logs_snapshot() -> list[dict]:
    return db.load_error_logs()


def _find_error_log(eid: str) -> Optional[dict]:
    return db.find_error_log(eid)


def _clear_error_logs() -> None:
    db.clear_error_logs()


# ---------------------------------------------------------------------------
# 可用性历史
# ---------------------------------------------------------------------------


def _append_avail_history(sample: dict[str, Any]) -> None:
    entry = {
        "ts": sample.get("ts") or _iso_now(),
        "model": str(sample.get("model") or "").strip(),
        "ok": bool(sample.get("ok")),
        "light": sample.get("light"),
        "multiplier": sample.get("multiplier"),
        "upstream": sample.get("upstream"),
        "source": sample.get("source"),
        "status_code": sample.get("status_code"),
    }
    if not entry["model"]:
        return
    try:
        db.insert_availability_history(entry)
        db.prune_availability_history(AVAIL_HISTORY_RETENTION_DAYS)
    except Exception:
        log.exception("persist availability history failed")


def _avail_history_snapshot() -> list[dict]:
    return db.load_availability_history()


# ---------------------------------------------------------------------------
# 聚合统计
# ---------------------------------------------------------------------------


def _aggregate_stats(items: list[dict], now: datetime) -> dict:
    total = len(items)
    ok = 0
    sum_in = sum_out = sum_reason = sum_cached = sum_total = 0
    total_cost = 0.0
    total_real_cost_cny = 0.0
    tps_tokens = 0
    tps_seconds = 0.0
    lat: list[float] = []
    ttft_lat: list[float] = []
    last_24h = 0
    per_pool: dict[str, int] = {}
    per_model: dict[str, int] = {}
    cost_by_upstream_cny: dict[str, float] = {}
    upstream_breakdown: dict[str, dict[str, int]] = {}
    model_breakdown: dict[str, dict[str, Any]] = {}
    for e in items:
        pool = e.get("pool") or "?"
        per_pool[pool] = per_pool.get(pool, 0) + 1
        model = core._request_model_label(e)
        per_model[model] = per_model.get(model, 0) + 1
        upstream = str(e.get("upstream") or "未知上游")
        upstream_item = upstream_breakdown.setdefault(
            upstream, {"calls": 0, "total_tokens": 0}
        )
        upstream_item["calls"] += 1
        model_item = model_breakdown.setdefault(
            model, {"calls": 0, "total_tokens": 0, "cost_cny": 0.0}
        )
        model_item["calls"] += 1
        raw_total_tokens = e.get("total_tokens")
        try:
            total_tokens = (
                int(raw_total_tokens)
                if raw_total_tokens is not None
                else int(e.get("input_tokens") or 0) + int(e.get("output_tokens") or 0)
            )
        except (TypeError, ValueError):
            total_tokens = 0
        upstream_item["total_tokens"] += max(total_tokens, 0)
        model_item["total_tokens"] += max(total_tokens, 0)
        cost = core.compute_cost_usd(e)
        if cost is not None:
            total_cost += cost
            real_cost = core.compute_real_cost_cny(e, cost)
            if real_cost is not None:
                total_real_cost_cny += real_cost
                model_item["cost_cny"] += real_cost
                cost_by_upstream_cny[upstream] = (
                    cost_by_upstream_cny.get(upstream, 0.0) + real_cost
                )
        d = e.get("duration_ms")
        if d is not None:
            try:
                # Anthropic/OpenAI 的 output_tokens 已包含 reasoning/thinking，
                # 不再额外相加（借鉴 cc-switch TokenUsage 语义）。
                tps_tokens += int(e.get("output_tokens") or 0)
                tps_seconds += float(d) / 1000.0
            except (TypeError, ValueError):
                pass
        st = e.get("status")
        if st is not None:
            try:
                sti = int(st)
            except (TypeError, ValueError):
                sti = None
            if sti is not None and 200 <= sti < 400:
                ok += 1
        for key, acc in (
            ("input_tokens", "sum_in"),
            ("cached_tokens", "sum_cached"),
            ("output_tokens", "sum_out"),
            ("reasoning_tokens", "sum_reason"),
            ("total_tokens", "sum_total"),
        ):
            v = e.get(key)
            if v is not None:
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    n = 0
                if acc == "sum_in":
                    sum_in += n
                elif acc == "sum_cached":
                    sum_cached += n
                elif acc == "sum_out":
                    sum_out += n
                elif acc == "sum_reason":
                    sum_reason += n
                else:
                    sum_total += n
        d = e.get("duration_ms")
        if d is not None:
            try:
                lat.append(float(d))
            except (TypeError, ValueError):
                pass
        t = e.get("ttft_ms")
        if t is not None:
            try:
                ttft_lat.append(float(t))
            except (TypeError, ValueError):
                pass
        ts = e.get("ts") or ""
        dt = _parse_ts(ts)
        if dt is not None and (now - dt).total_seconds() <= 86400:
            last_24h += 1
    return {
        "total": total,
        "success": ok,
        "error": total - ok,
        "success_rate": round(ok / total, 4) if total else None,
        "total_input_tokens": sum_in,
        "total_output_tokens": sum_out,
        "total_reasoning_tokens": sum_reason,
        "total_cached_tokens": sum_cached,
        "cache_hit_rate": round(sum_cached / sum_in, 4) if sum_in > 0 else None,
        "total_tokens": sum_total,
        "avg_tps": (
            round(tps_tokens / tps_seconds, 1) if tps_tokens > 0 and tps_seconds > 0 else None
        ),
        "total_cost": round(total_cost, 4),
        "total_real_cost_cny": round(total_real_cost_cny, 8),
        "avg_duration_ms": round(sum(lat) / len(lat), 1) if lat else None,
        "avg_ttft_ms": round(sum(ttft_lat) / len(ttft_lat), 1) if ttft_lat else None,
        "last_24h": last_24h,
        "per_pool": per_pool,
        "per_model": per_model,
        "cost_by_upstream_cny": {
            name: round(value, 8)
            for name, value in sorted(
                cost_by_upstream_cny.items(), key=lambda item: item[1], reverse=True
            )
        },
        "upstream_breakdown": {
            name: {
                "cost_cny": round(cost_by_upstream_cny.get(name, 0.0), 8),
                "calls": detail["calls"],
                "total_tokens": detail["total_tokens"],
            }
            for name, detail in sorted(
                upstream_breakdown.items(),
                key=lambda item: cost_by_upstream_cny.get(item[0], 0.0),
                reverse=True,
            )
        },
        "model_breakdown": {
            name: {
                "cost_cny": round(detail["cost_cny"], 8),
                "calls": detail["calls"],
                "total_tokens": detail["total_tokens"],
            }
            for name, detail in sorted(
                model_breakdown.items(),
                key=lambda item: (-item[1]["total_tokens"], item[0]),
            )
        },
    }


def _resolve_log_range(
    range_: str = "today",
    start_raw: Optional[str] = None,
    end_raw: Optional[str] = None,
) -> tuple[Optional[datetime], Optional[datetime]]:
    now = _now_beijing()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    r = (range_ or "").strip().lower()
    if r == "today":
        start = today_start
    elif r == "yesterday":
        start = today_start - timedelta(days=1)
        end = today_start
    elif r in ("3d", "7d", "30d"):
        start = now - timedelta(days=int(r[:-1]))
    if start_raw:
        dt = _parse_ts(start_raw)
        if dt is not None:
            start = dt
    if end_raw:
        dt = _parse_ts(end_raw)
        if dt is not None:
            end = dt
    return start, end


def _log_stats(
    range_: str = "today",
    pool: Optional[str] = None,
    model: Optional[str] = None,
    upstream: Optional[str] = None,
) -> dict:
    items = _traffic_logs_snapshot()
    if pool:
        items = [e for e in items if (e.get("pool") or "") == pool]
    if model:
        items = [e for e in items if core._request_model_label(e) == model]
    if upstream:
        items = [e for e in items if str(e.get("upstream") or "") == upstream]
    start, end = _resolve_log_range(range_)
    now = _now_beijing()
    items = _entries_in_log_range(items, start, end)
    return _aggregate_stats(items, now)


def _ip_usage_stats(
    range_: str = "7d",
    q: Optional[str] = None,
) -> list[dict]:
    """Aggregate non-probe request log by caller IP, with usage/cost."""
    items = _traffic_logs_snapshot()
    start, end = _resolve_log_range(range_)
    items = _entries_in_log_range(items, start, end)
    needle = (q or "").strip().lower()
    agg: dict[str, dict] = {}
    for e in items:
        ip = str(e.get("client_ip") or "").strip() or "local"
        if needle and needle not in ip.lower():
            continue
        row = agg.setdefault(
            ip,
            {
                "ip": ip,
                "requests": 0,
                "success": 0,
                "errors": 0,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "real_cost_cny": 0.0,
                "first_seen": e.get("ts"),
                "last_seen": e.get("ts"),
                "upstreams": set(),
            },
        )
        row["requests"] += 1
        status = e.get("status")
        ok = isinstance(status, int) and 200 <= status < 400
        if ok:
            row["success"] += 1
        else:
            row["errors"] += 1
        row["input_tokens"] += int(e.get("input_tokens") or 0)
        row["cached_tokens"] += int(e.get("cached_tokens") or 0)
        row["output_tokens"] += int(e.get("output_tokens") or 0)
        row["reasoning_tokens"] += int(e.get("reasoning_tokens") or 0)
        row["total_tokens"] += int(e.get("total_tokens") or 0)
        mult = core.entry_multiplier(e)
        cost = core.compute_cost_usd(e)
        if cost is not None:
            row["cost_usd"] = round(float(row["cost_usd"]) + cost, 8)
        real = core.compute_real_cost_cny(e, cost, mult)
        if real is not None:
            row["real_cost_cny"] = round(float(row["real_cost_cny"]) + real, 8)
        ts = e.get("ts")
        if ts and (not row["first_seen"] or ts < row["first_seen"]):
            row["first_seen"] = ts
        if ts and (not row["last_seen"] or ts > row["last_seen"]):
            row["last_seen"] = ts
        upstream = str(e.get("upstream") or "").strip()
        if upstream:
            row["upstreams"].add(upstream)

    out = []
    for ip, row in agg.items():
        row = dict(row)
        row["upstreams"] = sorted(row["upstreams"])
        row["blocked"] = not core.ip_allowed(ip)
        row["mode"] = core.load_public_config().get("mode")
        out.append(row)
    out.sort(key=lambda r: (-r["requests"], -r["total_tokens"], str(r["ip"])))
    return out


def _entries_in_log_range(
    items: list[dict],
    start: Optional[datetime],
    end: Optional[datetime],
) -> list[dict]:
    if start is None and end is None:
        return items
    out: list[dict] = []
    for e in items:
        dt = _parse_ts(e.get("ts") or "")
        if dt is None:
            continue
        if start is not None and dt < start:
            continue
        if end is not None and dt >= end:
            continue
        out.append(e)
    return out


def _count_log_filter_options(items: list[dict]) -> dict[str, list[dict]]:
    pools: dict[str, int] = {}
    models: dict[str, int] = {}
    upstreams: dict[str, int] = {}
    for e in items:
        pool = str(e.get("pool") or "").strip()
        if pool:
            pools[pool] = pools.get(pool, 0) + 1
        model = core._request_model_label(e)
        if model:
            models[model] = models.get(model, 0) + 1
        upstream = str(e.get("upstream") or "").strip()
        if upstream:
            upstreams[upstream] = upstreams.get(upstream, 0) + 1

    def ordered(counts: dict[str, int], key: str) -> list[dict]:
        return [
            {key: name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    return {
        "pools": ordered(pools, "pool"),
        "models": ordered(models, "model"),
        "upstreams": ordered(upstreams, "upstream"),
    }


def _count_error_filter_options(items: list[dict]) -> dict[str, list[dict]]:
    pools: dict[str, int] = {}
    models: dict[str, int] = {}
    upstreams: dict[str, int] = {}
    for e in items:
        pool = str(e.get("pool") or "").strip()
        if pool:
            pools[pool] = pools.get(pool, 0) + 1
        model = core._request_model_label(e)
        if model:
            models[model] = models.get(model, 0) + 1
        for a in e.get("attempts") or []:
            if not isinstance(a, dict):
                continue
            upstream = str(a.get("upstream") or "").strip()
            if upstream:
                upstreams[upstream] = upstreams.get(upstream, 0) + 1

    def ordered(counts: dict[str, int], key: str) -> list[dict]:
        return [
            {key: name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    return {
        "pools": ordered(pools, "pool"),
        "models": ordered(models, "model"),
        "upstreams": ordered(upstreams, "upstream"),
    }


# ---------------------------------------------------------------------------
# 历史可用性时间线
# ---------------------------------------------------------------------------


def _build_availability_history(range_: str = "24h") -> dict[str, Any]:
    range_ = (range_ or "24h").strip().lower()
    bucket_count = 24
    presets = {
        "1h": 3600,
        "24h": 86400,
        "7d": 7 * 86400,
    }
    if range_ not in presets:
        range_ = "24h"
    window_sec = presets[range_]
    bucket_sec = max(1, int(window_sec // bucket_count))
    carry_sec = 3600
    now = _now_beijing()
    start = now - timedelta(seconds=window_sec)
    hist_start = start - timedelta(seconds=carry_sec)

    models = list(core.collect_client_models_for_availability())
    hist = _avail_history_snapshot()
    for e in hist:
        m = str(e.get("model") or "").strip()
        if m and m not in models:
            models.append(m)

    by_model: dict[str, list[tuple[datetime, bool, Optional[float]]]] = {
        m: [] for m in models
    }
    for e in hist:
        m = str(e.get("model") or "").strip()
        if not m:
            continue
        dt = _parse_ts(e.get("ts"))
        if dt is None or dt < hist_start or dt > now:
            continue
        mult = core._float_or_none(e.get("multiplier"))
        by_model.setdefault(m, []).append((dt, bool(e.get("ok")), mult))
    for m in by_model:
        by_model[m].sort(key=lambda item: item[0])

    req_by_model: dict[str, list[tuple[datetime, float]]] = {m: [] for m in models}
    for e in _logs_snapshot():
        if _is_probe_log(e):
            continue
        m = str(e.get("client_model") or "").strip()
        if not m:
            continue
        dt = _parse_ts(e.get("ts"))
        if dt is None or dt < start or dt > now:
            continue
        mult = core.entry_multiplier(e)
        if mult is None:
            continue
        try:
            mf = float(mult)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(mf) or mf < 0:
            continue
        req_by_model.setdefault(m, []).append((dt, mf))
        if m not in models:
            models.append(m)
    for m in req_by_model:
        req_by_model[m].sort(key=lambda item: item[0])

    RATE_GOOD = 0.95
    RATE_MID = 0.80
    data = []
    for m in models:
        samples = by_model.get(m) or []
        reqs = req_by_model.get(m) or []
        ok_counts = [0] * bucket_count
        fail_counts = [0] * bucket_count
        price_sum = [0.0] * bucket_count
        price_n = [0] * bucket_count
        sample_mult_sum = [0.0] * bucket_count
        sample_mult_n = [0] * bucket_count
        in_window_count = 0

        for dt, ok, mult in samples:
            if dt < start or dt > now:
                continue
            in_window_count += 1
            offset = (dt - start).total_seconds()
            idx = int(offset // bucket_sec)
            if idx < 0:
                idx = 0
            if idx >= bucket_count:
                idx = bucket_count - 1
            if ok:
                ok_counts[idx] += 1
                if mult is not None:
                    sample_mult_sum[idx] += float(mult)
                    sample_mult_n[idx] += 1
            else:
                fail_counts[idx] += 1

        for dt, mult in reqs:
            offset = (dt - start).total_seconds()
            idx = int(offset // bucket_sec)
            if idx < 0:
                idx = 0
            if idx >= bucket_count:
                idx = bucket_count - 1
            price_sum[idx] += float(mult)
            price_n[idx] += 1

        raw_rate: list[Optional[float]] = [None] * bucket_count
        for i in range(bucket_count):
            total = ok_counts[i] + fail_counts[i]
            if total > 0:
                raw_rate[i] = ok_counts[i] / total

        resolved_rate: list[Optional[float]] = [None] * bucket_count
        carried_mult: list[Optional[float]] = [None] * bucket_count
        carried = 0
        samples_for_carry = list(samples)
        si = 0
        last_sample_ok: Optional[bool] = None
        last_ts: Optional[datetime] = None
        last_mult: Optional[float] = None
        for i in range(bucket_count):
            b_end = start + timedelta(seconds=(i + 1) * bucket_sec)
            while si < len(samples_for_carry) and samples_for_carry[si][0] <= b_end:
                last_ts, last_sample_ok, sm = samples_for_carry[si]
                if last_sample_ok and sm is not None:
                    last_mult = float(sm)
                si += 1
            if raw_rate[i] is not None:
                resolved_rate[i] = raw_rate[i]
            elif last_sample_ok is not None and last_ts is not None:
                gap = (b_end - last_ts).total_seconds()
                if 0 <= gap <= carry_sec:
                    resolved_rate[i] = 1.0 if last_sample_ok else 0.0
                    carried_mult[i] = last_mult if last_sample_ok else None
                    carried += 1
                else:
                    resolved_rate[i] = None
            else:
                resolved_rate[i] = None

        row_ok = 0
        row_total = 0
        for i in range(bucket_count):
            total = ok_counts[i] + fail_counts[i]
            if total > 0:
                row_ok += ok_counts[i]
                row_total += total
            elif resolved_rate[i] is not None:
                row_total += 1
                if resolved_rate[i] >= RATE_GOOD:
                    row_ok += 1
        rate = round(row_ok / row_total, 4) if row_total else None

        all_price_vals = [mult for _, mult in reqs]
        row_avg_price = (
            round(sum(all_price_vals) / len(all_price_vals), 6) if all_price_vals else None
        )

        cells = []
        up_n = mid_n = down_n = 0
        for i, br in enumerate(resolved_rate):
            b_start = start + timedelta(seconds=i * bucket_sec)
            b_end = b_start + timedelta(seconds=bucket_sec)
            avg_price: Optional[float] = None
            if price_n[i] > 0:
                avg_price = round(price_sum[i] / price_n[i], 6)
            elif sample_mult_n[i] > 0:
                avg_price = round(sample_mult_sum[i] / sample_mult_n[i], 6)
            elif carried_mult[i] is not None:
                avg_price = round(float(carried_mult[i]), 6)

            sample_total = ok_counts[i] + fail_counts[i]
            if br is None:
                state = "empty"
                color = None
            elif br >= RATE_GOOD:
                state = "up"
                color = core._price_color_for_multiplier(
                    avg_price if avg_price is not None else 0.03
                )
                up_n += 1
            elif br >= RATE_MID:
                state = "mid"
                color = COLOR_MID
                mid_n += 1
            else:
                state = "down"
                color = COLOR_BAD
                down_n += 1

            cells.append(
                {
                    "state": state,
                    "start": b_start.isoformat(timespec="seconds"),
                    "end": b_end.isoformat(timespec="seconds"),
                    "avg_multiplier": avg_price,
                    "request_count": price_n[i],
                    "success_rate": round(br, 4) if br is not None else None,
                    "ok_samples": ok_counts[i],
                    "fail_samples": fail_counts[i],
                    "sample_total": sample_total,
                    "color": color,
                }
            )
        data.append(
            {
                "model": m,
                "pool": core.pool_for_client_model(m),
                "availability_rate": rate,
                "avg_multiplier": row_avg_price,
                "samples": in_window_count,
                "request_count": len(reqs),
                "known_buckets": up_n + mid_n + down_n,
                "up_buckets": up_n,
                "mid_buckets": mid_n,
                "down_buckets": down_n,
                "carried_buckets": carried,
                "cells": cells,
            }
        )

    return {
        "range": range_,
        "window_sec": window_sec,
        "bucket_sec": bucket_sec,
        "bucket_count": bucket_count,
        "carry_sec": carry_sec,
        "price_low": 0.03,
        "price_high": 0.20,
        "start": start.isoformat(timespec="seconds"),
        "end": now.isoformat(timespec="seconds"),
        "data": data,
    }
