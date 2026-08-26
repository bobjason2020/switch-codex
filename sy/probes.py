#!/usr/bin/env python3
"""Switch-codex 探测与可用性层。

包含模型级联可用性探测（按优先级、倍率）、内存可用性快照、后台探测线程，
以及 NewAPI 分组倍率自动同步。
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
import uuid
from typing import Any, Optional

import httpx

from sy import core, db, logbook, state, timeutil
from sy.const import (
    DEEPSEEK_CLIENT_MODELS,
    DEEPSEEK_POOL,
    DEFAULT_MODEL,
    DEFAULT_NEWAPI_PROBE,
    DEFAULT_PROBE_INTERVAL_SEC,
    GROK_CLIENT_MODELS,
    GROK_POOL,
)

log = logging.getLogger("switchyard.probes")

_iso_now = timeutil.iso_now
_now_beijing = timeutil.now_beijing
_parse_ts = timeutil.parse_ts


# ---------------------------------------------------------------------------
# 可用性快照
# ---------------------------------------------------------------------------


def _restore_model_availability() -> None:
    """启动时从 DB 恢复可用性快照；首次运行从历史回填。"""
    try:
        stored = db.load_model_availability()
        with state._model_avail_lock:
            state._model_availability.clear()
            for model, info in stored.items():
                if isinstance(info, dict):
                    info = dict(info)
                    info.pop("next_run_at", None)
                    state._model_availability[str(model)] = info
        if stored:
            log.info("restored model availability from db models=%s", len(stored))
            return
        last: dict[str, dict[str, Any]] = {}
        for e in db.load_availability_history():
            m = str(e.get("model") or "").strip()
            if not m:
                continue
            ok = bool(e.get("ok"))
            last[m] = {
                "model": m,
                "pool": core.pool_for_client_model(m),
                "ok": ok,
                "light": e.get("light") or ("ok" if ok else "red"),
                "multiplier": e.get("multiplier"),
                "upstream": e.get("upstream"),
                "upstream_id": None,
                "status_code": e.get("status_code"),
                "checked_at": e.get("ts"),
                "attempts": 0,
                "attempt_detail": [],
                "error": None,
                "source": e.get("source") or "history-backfill",
            }
        if last:
            with state._model_avail_lock:
                state._model_availability.update(last)
            db.save_model_availability(last)
            log.info("backfilled model availability from history models=%s", len(last))
    except Exception:
        log.exception("restore model availability failed")


def _set_model_availability(model: str, info: dict[str, Any]) -> None:
    """更新内存可用性快照并持久化最新状态 + 追加历史样本。"""
    payload = dict(info)
    payload.setdefault("model", str(model))
    prev = state.update_model_availability_snapshot(str(model), payload)

    try:
        persist = dict(payload)
        persist.pop("next_run_at", None)
        db.save_model_availability({str(model): persist})
    except Exception:
        log.exception("persist model availability failed")

    source = str(payload.get("source") or "")
    if payload.get("invalidated") and source == "":
        source = "invalidated"

    ok = payload.get("ok")
    if ok is None:
        return
    try:
        prev_ts = _parse_ts(prev.get("checked_at") or prev.get("ts"))
        now_ts = _parse_ts(payload.get("checked_at")) or _now_beijing()
        same = (
            bool(prev.get("ok")) == bool(ok)
            and str(prev.get("upstream") or "") == str(payload.get("upstream") or "")
            and str(prev.get("light") or "") == str(payload.get("light") or "")
        )
        if same and prev_ts is not None and (now_ts - prev_ts).total_seconds() < 30:
            return
    except Exception:
        pass

    logbook._append_avail_history(
        {
            "ts": payload.get("checked_at") or _iso_now(),
            "model": str(model),
            "ok": bool(ok),
            "light": payload.get("light"),
            "multiplier": payload.get("multiplier"),
            "upstream": payload.get("upstream"),
            "source": source or payload.get("source") or "probe",
            "status_code": payload.get("status_code"),
        }
    )


def mark_model_upstream_failed(
    client_model: Optional[str],
    upstream: dict,
    *,
    status: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """首选可用性上游失败时，使缓存失效以便重新级联。"""
    cm = str(client_model or "").strip()
    if not cm:
        return
    avail = state.get_model_availability_cached(cm)
    if not avail or not avail.get("ok"):
        return
    uid = str(upstream.get("id") or "")
    uname = str(upstream.get("name") or "")
    if uid and str(avail.get("upstream_id") or "") == uid:
        pass
    elif uname and str(avail.get("upstream") or "") == uname:
        pass
    else:
        return
    info = dict(avail)
    info.update(
        {
            "ok": False,
            "light": "red",
            "multiplier": None,
            "upstream": None,
            "upstream_id": None,
            "status_code": status,
            "error": error or "preferred upstream failed during request",
            "checked_at": _iso_now(),
            "invalidated": True,
        }
    )
    _set_model_availability(cm, info)
    log.info(
        "availability invalidated model=%s failed_upstream=%s status=%s",
        cm,
        uname or uid,
        status,
    )


# ---------------------------------------------------------------------------
# 模型级联探测
# ---------------------------------------------------------------------------


def _probe_model_for_pool(umodel: str) -> str:
    """池名 -> 一个具体的代表客户端模型（避免把池名直接当模型名探测）。"""
    umodel = str(umodel or "").strip()
    if umodel == DEFAULT_MODEL:
        return "gpt-5.6-sol"
    if umodel == DEEPSEEK_POOL:
        return DEEPSEEK_CLIENT_MODELS[0]
    if umodel == GROK_POOL:
        return GROK_CLIENT_MODELS[0]
    return umodel


def _pick_probe_client_model(target: dict, umodel: str) -> str:
    """未指定客户端模型时，选一个该上游真正支持的具体模型名。

    优先从 model_map 里挑池代表模型（如 deepseek-v4-flash），没有代表模型时
    退回第一条映射；这样 core.upstream_request_model 才能解析出 actual 模型，
    而不是把 pool 名（openai / deepseek / grok）直接发给上游。
    """
    preferred = _probe_model_for_pool(umodel)
    entries = target.get("model_map")
    if isinstance(entries, list) and entries:
        for e in entries:
            if str(e.get("model") or "").strip() == preferred:
                return preferred
        for e in entries:
            m = str(e.get("model") or "").strip()
            if m:
                return m
    return preferred


async def _probe_upstream(
    target: dict,
    *,
    source: str = "manual",
    record_log: bool = True,
    timeout: Optional[float] = None,
    client_model: Optional[str] = None,
) -> dict[str, Any]:
    uid = str(target.get("id") or "")
    umodel = core.normalize_model(target.get("model"))
    probe_model = (
        str(client_model or "").strip()
        or _pick_probe_client_model(target, umodel)
    )
    request_model = core.upstream_request_model(target, probe_model)
    payload_model = request_model or probe_model
    chat_mode = bool(target.get("chat_completions"))
    anthropic_mode = bool(target.get("anthropic_messages"))
    url = str(target.get("base_url") or "").rstrip("/") + (
        "/messages"
        if anthropic_mode
        else ("/chat/completions" if chat_mode else "/responses")
    )
    payload: dict[str, Any]
    if anthropic_mode:
        # Anthropic 原生上游：用 Messages 格式探测（x-api-key 认证）
        payload = {
            "model": payload_model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "Reply exactly: OK"}],
        }
    elif chat_mode:
        payload = {
            "model": payload_model,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply exactly: OK",
                }
            ],
            "max_tokens": 8,
        }
    else:
        payload = {
            "model": payload_model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Reply exactly: OK"}
                    ],
                }
            ],
            "max_output_tokens": 8,
        }
    if timeout is None:
        timeout = float(core.load_config().get("timeout_sec", 120))
    upstream_name = str(target.get("name") or "")
    log_path = f"/api/upstreams/{uid}/test"
    t0 = time.perf_counter()
    try:
        probe_headers = {
            "Content-Type": "application/json",
        }
        if anthropic_mode:
            probe_headers["x-api-key"] = target["api_key"]
            probe_headers["anthropic-version"] = "2023-06-01"
        else:
            probe_headers["Authorization"] = f"Bearer {target['api_key']}"
        # A redirect target is not an authenticated upstream. Do not forward
        # upstream credentials to it.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            r = await client.post(
                url,
                headers=probe_headers,
                json=payload,
            )
        duration_ms = (time.perf_counter() - t0) * 1000.0
        # Redirects are deliberately not followed, so only a real successful
        # response may mark this upstream healthy.
        ok = 200 <= r.status_code < 300
        usage = logbook._extract_usage(r.content) if ok else None
        err_text = None if ok else (r.text[:500] or f"HTTP {r.status_code}")
        if record_log:
            err_log_id = None
            if not ok:
                err_log_id = logbook._record_error_log(
                    client_ip="",
                    method="POST",
                    path=log_path,
                    pool=umodel,
                    client_model=probe_model,
                    stream=False,
                    is_probe=True,
                    status=r.status_code,
                    error=err_text,
                    duration_ms=duration_ms,
                    request_body=payload,
                    request_body_len=0,
                    request_body_truncated=False,
                    attempts=[],
                )
            logbook._record_log(
                method="POST",
                path=log_path,
                pool=umodel,
                client_model=probe_model,
                upstream=upstream_name,
                upstream_url=url,
                multiplier=core.upstream_multiplier_value(target),
                status=r.status_code,
                duration_ms=duration_ms,
                error_log_id=err_log_id,
                stream=False,
                is_probe=True,
                **logbook._usage_numbers(usage),
            )
        result = {
            "ok": ok,
            "status_code": r.status_code,
            "upstream": upstream_name,
            "upstream_id": uid,
            "model": umodel,
            "probe_model": probe_model,
            "request_model": payload_model,
            "url": url,
            "body_preview": r.text[:500],
            "duration_ms": round(duration_ms, 1),
            "source": source,
            "error": err_text,
            "multiplier": core.upstream_multiplier_value(target),
        }
    except Exception as e:
        duration_ms = (time.perf_counter() - t0) * 1000.0
        err = str(e)
        if record_log:
            err_log_id = logbook._record_error_log(
                client_ip="",
                method="POST",
                path=log_path,
                pool=umodel,
                client_model=probe_model,
                stream=False,
                is_probe=True,
                status=None,
                error=err,
                duration_ms=duration_ms,
                request_body=payload,
                request_body_len=0,
                request_body_truncated=False,
                attempts=[],
            )
            logbook._record_log(
                method="POST",
                path=log_path,
                pool=umodel,
                client_model=probe_model,
                upstream=upstream_name,
                upstream_url=url,
                multiplier=core.upstream_multiplier_value(target),
                status=None,
                duration_ms=duration_ms,
                error_log_id=err_log_id,
                stream=False,
                is_probe=True,
            )
        result = {
            "ok": False,
            "status_code": None,
            "upstream": upstream_name,
            "upstream_id": uid,
            "model": umodel,
            "probe_model": probe_model,
            "request_model": payload_model,
            "url": url,
            "error": err,
            "duration_ms": round(duration_ms, 1),
            "source": source,
            "multiplier": core.upstream_multiplier_value(target),
        }

    if uid and source == "manual":
        state.set_probe_health(
            uid,
            {
                "ok": bool(result.get("ok")),
                "status_code": result.get("status_code"),
                "error": result.get("error"),
                "checked_at": _iso_now(),
                "duration_ms": result.get("duration_ms"),
                "probe_model": result.get("probe_model"),
                "source": source,
            },
        )
    return result


async def probe_standalone_web_search(
    target: dict, *, timeout: Optional[float] = None
) -> dict[str, Any]:
    """Probe the OpenAI standalone search endpoint without invoking a search.

    ``/alpha/search`` validates the standalone protocol before doing work. A
    minimal request therefore lets the dashboard distinguish route support
    from an ordinary Responses probe without charging a web search.
    """
    if not core.upstream_supports_standalone_web_search(target):
        return {
            "ok": False,
            "supported": False,
            "status_code": None,
            "error": "upstream is not an OpenAI Responses provider",
            "upstream": target.get("name"),
        }
    timeout = float(timeout or min(float(core.load_config().get("timeout_sec", 120)), 20.0))
    model = core.upstream_request_model(target, "gpt-5.6-sol") or "gpt-5.6-sol"
    # The endpoint rejects an otherwise valid request with an "empty calls"
    # error. Include the smallest real search command so capability probing
    # exercises the same protocol as production requests.
    payload = {
        "id": "switchyard-capability-probe",
        "model": model,
        "input": "OK",
        "commands": {
            "search_query": [{"q": "site:example.com switchyard capability probe"}],
            "response_length": "short",
        },
        "settings": {"allowed_callers": ["direct"], "external_web_access": False},
        "max_output_tokens": 16,
    }
    url = str(target.get("base_url") or "").rstrip("/") + "/alpha/search"
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {target.get('api_key') or ''}",
                    "Content-Type": "application/json",
                },
            )
            raw = await resp.aread()
        text = raw.decode("utf-8", errors="replace")
        # 2xx means the endpoint understands the protocol. Some gateways
        # return a structured validation result rather than a search result.
        supported = 200 <= resp.status_code < 300
        return {
            "ok": supported,
            "supported": supported,
            "status_code": resp.status_code,
            "upstream": target.get("name"),
            "url": url,
            "model": model,
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "body_preview": text[:1000],
            "error": None if supported else text[:500],
        }
    except Exception as exc:
        return {
            "ok": False,
            "supported": False,
            "status_code": None,
            "upstream": target.get("name"),
            "url": url,
            "model": model,
            "duration_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "error": str(exc),
        }


def _seconds_until_next_clock_boundary(interval_sec: int = 300) -> float:
    interval_sec = max(30, int(interval_sec))
    now = time.time()
    next_ts = (int(now) // interval_sec + 1) * interval_sec
    return max(0.5, float(next_ts - now))


async def _cascade_probe_model(
    client_model: str,
    *,
    timeout: float = 30.0,
    record_log: bool = True,
    exclude_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    client_model = str(client_model or "").strip()
    pool = core.pool_for_client_model(client_model)
    exclude_ids = exclude_ids or set()
    items = [
        u
        for u in core.sort_upstreams_by_priority(core.enabled_upstreams_for_pool(pool))
        if str(u.get("id") or "") not in exclude_ids
        and core.upstream_supports_model(u, client_model)
    ]
    attempts: list[dict[str, Any]] = []
    winner: Optional[dict[str, Any]] = None
    for u in items:
        try:
            result = await _probe_upstream(
                u,
                source="model-cascade",
                record_log=record_log,
                timeout=timeout,
                client_model=client_model,
            )
        except Exception as e:
            result = {
                "ok": False,
                "status_code": None,
                "upstream": u.get("name"),
                "upstream_id": u.get("id"),
                "error": str(e),
                "duration_ms": None,
                "multiplier": core.upstream_multiplier_value(u),
                "probe_model": client_model,
            }
        attempt = {
            "upstream": result.get("upstream"),
            "upstream_id": result.get("upstream_id"),
            "multiplier": result.get("multiplier"),
            "ok": bool(result.get("ok")),
            "status_code": result.get("status_code"),
            "duration_ms": result.get("duration_ms"),
            "error": (result.get("error") or None),
        }
        attempts.append(attempt)
        if result.get("ok"):
            winner = result
            break

    if winner is not None:
        mult = float(winner.get("multiplier") or 0.0)
        light = core.light_for_multiplier(mult, True)
        info = {
            "model": client_model,
            "pool": pool,
            "ok": True,
            "light": light,
            "multiplier": mult,
            "upstream": winner.get("upstream"),
            "upstream_id": winner.get("upstream_id"),
            "status_code": winner.get("status_code"),
            "duration_ms": winner.get("duration_ms"),
            "checked_at": _iso_now(),
            "attempts": len(attempts),
            "attempt_detail": attempts,
            "error": None,
        }
    else:
        info = {
            "model": client_model,
            "pool": pool,
            "ok": False,
            "light": "red",
            "multiplier": None,
            "upstream": None,
            "upstream_id": None,
            "status_code": None,
            "duration_ms": None,
            "checked_at": _iso_now(),
            "attempts": len(attempts),
            "attempt_detail": attempts,
            "error": "all upstreams failed" if attempts else "no enabled upstreams",
        }
    _set_model_availability(client_model, info)
    log.info(
        "model cascade model=%s light=%s mult=%s upstream=%s attempts=%s",
        client_model,
        info["light"],
        info.get("multiplier"),
        info.get("upstream"),
        info.get("attempts"),
    )
    return info


def _seed_model_next_run(
    model: str,
    cfg: Optional[dict] = None,
    now: Optional[float] = None,
) -> float:
    settings = core.probe_settings_for_model(model, cfg)
    interval = int(settings.get("interval_sec") or DEFAULT_PROBE_INTERVAL_SEC)
    now = time.time() if now is None else now
    existing = state.model_next_run(str(model))
    if existing is not None:
        return existing
    next_run = now + _seconds_until_next_clock_boundary(max(30, interval))
    state.set_model_next_run(str(model), next_run)
    return next_run


def _reschedule_model_probe(model: str, cfg: Optional[dict] = None) -> None:
    settings = core.probe_settings_for_model(model, cfg)
    interval = int(settings.get("interval_sec") or DEFAULT_PROBE_INTERVAL_SEC)
    next_run = time.time() + _seconds_until_next_clock_boundary(max(30, interval))
    state.set_model_next_run(str(model), next_run)


def _model_due_for_probe(
    model: str, cfg: Optional[dict] = None, now: Optional[float] = None
) -> bool:
    settings = core.probe_settings_for_model(model, cfg)
    if not settings.get("enabled"):
        return False
    now = time.time() if now is None else now
    next_run = state.model_next_run(model)
    if next_run is None:
        return False
    return now >= next_run


async def _run_model_cascade_probes(only_due: bool = False) -> None:
    cfg = core.load_config()
    models = core.collect_client_models_for_availability()
    timeout = min(float(cfg.get("timeout_sec", 120)), 30.0)
    now = time.time()
    selected: list[str] = []
    for model in models:
        settings = core.probe_settings_for_model(model, cfg)
        if not settings.get("enabled"):
            continue
        if only_due:
            _seed_model_next_run(model, cfg, now=now)
            if not _model_due_for_probe(model, cfg, now=now):
                continue
        selected.append(model)
    if not selected:
        log.info("model cascade round: nothing due")
        return
    log.info("model cascade round start models=%s (parallel)", selected)

    async def _one(model: str) -> None:
        if state.get_probe_stop_event().is_set():
            return
        try:
            await _cascade_probe_model(model, timeout=timeout, record_log=True)
        except Exception:
            log.exception("model cascade failed model=%s", model)
        finally:
            _reschedule_model_probe(model, cfg)

    await asyncio.gather(*[_one(m) for m in selected])
    log.info("model cascade round done")


def _probe_loop() -> None:
    if state.get_probe_stop_event().wait(3):
        return
    try:
        cfg = core.load_config()
        for model in core.collect_client_models_for_availability():
            settings = core.probe_settings_for_model(model, cfg)
            if settings.get("enabled"):
                _seed_model_next_run(model, cfg)
    except Exception:
        log.exception("model cascade schedule seed failed")
    while not state.get_probe_stop_event().is_set():
        wait = min(30.0, _seconds_until_next_clock_boundary(60))
        if state.get_probe_stop_event().wait(wait):
            break
        try:
            asyncio.run(_run_model_cascade_probes(only_due=True))
        except Exception:
            log.exception("model cascade round failed")


def _start_probe_loop() -> None:
    if state._probe_loop_started:
        return
    state._probe_loop_started = True
    t = threading.Thread(target=_probe_loop, name="model-cascade-probe", daemon=True)
    t.start()
    log.info("model cascade probe loop started (per-model interval, clock-aligned)")


# ---------------------------------------------------------------------------
# NewAPI 分组倍率探测
# ---------------------------------------------------------------------------


def _normalize_newapi_probe(raw: Any, index: int = 0) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULT_NEWAPI_PROBE)
    if isinstance(raw, dict):
        out.update({k: raw[k] for k in DEFAULT_NEWAPI_PROBE if k in raw})
    out["id"] = str(raw.get("id") or "").strip() or str(uuid.uuid4())
    out["name"] = str(raw.get("name") or f"NewAPI 探测 {index + 1}").strip()
    out["enabled"] = bool(out.get("enabled", True))
    try:
        interval = int(out.get("interval_sec") or DEFAULT_NEWAPI_PROBE["interval_sec"])
    except (TypeError, ValueError):
        interval = DEFAULT_NEWAPI_PROBE["interval_sec"]
    out["interval_sec"] = max(15, min(interval, 86400))
    base_url = str(out.get("base_url") or "").strip().rstrip("/")
    if base_url and not re.match(r"^[a-z][a-z0-9+.-]*://", base_url, re.IGNORECASE):
        base_url = "https://" + base_url
    out["base_url"] = base_url
    out["group"] = str(out.get("group") or "").strip()
    out["upstream_name"] = str(out.get("upstream_name") or "").strip()
    out["access_token"] = str(out.get("access_token") or "").strip()
    try:
        bias = float(out.get("priority_bias") or 0.0)
    except (TypeError, ValueError):
        bias = 0.0
    out["priority_bias"] = max(-1.0, min(1.0, bias)) if math.isfinite(bias) else 0.0
    return out


def load_newapi_probes() -> list[dict[str, Any]]:
    return [
        _normalize_newapi_probe(p, i)
        for i, p in enumerate(db.load_newapi_probes())
    ]


def save_newapi_probes(items: list[dict[str, Any]]) -> None:
    db.save_newapi_probes(items)


def public_newapi_probe(probe: dict[str, Any]) -> dict[str, Any]:
    out = dict(_normalize_newapi_probe(probe))
    tok = str(out.get("access_token") or "")
    out["access_token_masked"] = core.mask_key(tok) if tok else ""
    out["access_token_set"] = bool(tok)
    out.pop("access_token", None)
    out["state"] = state.newapi_probe_snapshot(str(out["id"]))
    return out


def _sync_newapi_probe_bias_to_upstream(probe: dict[str, Any]) -> None:
    probe = _normalize_newapi_probe(probe)
    probe_id = str(probe.get("id") or "")
    upstream_name = str(probe.get("upstream_name") or "").strip()
    items = core.load_upstreams()
    target = None
    for u in items:
        if probe_id and str(u.get("ratio_probe_id") or "") == probe_id:
            target = u
            break
    if target is None and upstream_name:
        for u in items:
            if str(u.get("name") or "") == upstream_name:
                target = u
                break
    if target is None:
        return
    bias = float(probe.get("priority_bias") or 0.0)
    old = target.get("ratio_priority_bias")
    if old == bias:
        return
    target["ratio_priority_bias"] = bias
    core.save_upstreams(items)
    log.info("newapi probe bias synced upstream=%s bias=%s", target.get("name"), bias)


def _priority_from_multiplier(mult: float) -> int:
    try:
        m = float(mult)
    except (TypeError, ValueError):
        return 100
    if not math.isfinite(m) or m < 0:
        return 100
    return max(1, int(round(m * 100)))


def _fetch_newapi_group_ratio(
    *,
    base_url: str,
    group: str,
    access_token: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = access_token

    groups: dict[str, Any] = {}
    source = ""
    errors: list[str] = []

    try:
        # The optional access token must never be sent to a redirect target.
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            r = client.get(f"{base_url}/api/user/groups", headers=headers)
        if 200 <= r.status_code < 300:
            payload = r.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                groups = data
                source = "user/groups"
        else:
            errors.append(f"user/groups HTTP {r.status_code}")
    except Exception as e:
        errors.append(f"user/groups: {e}")

    if not groups:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                r = client.get(
                    f"{base_url}/api/pricing", headers={"Accept": "application/json"}
                )
            if 200 <= r.status_code < 300:
                payload = r.json()
                gr = payload.get("group_ratio") if isinstance(payload, dict) else None
                if isinstance(gr, dict):
                    groups = {
                        k: ({"ratio": v} if not isinstance(v, dict) else v)
                        for k, v in gr.items()
                    }
                    source = "pricing.group_ratio"
            else:
                errors.append(f"pricing HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"pricing: {e}")

    if not groups:
        return {
            "ok": False,
            "error": "; ".join(errors) or "no group ratio data",
            "group": group,
            "ratio": None,
            "source": source or None,
        }

    entry = groups.get(group)
    if entry is None:
        gl = group.lower()
        for k, v in groups.items():
            if gl in str(k).lower() or str(k).lower() in gl:
                entry = v
                group = str(k)
                break

    if entry is None:
        return {
            "ok": False,
            "error": f"group not found: {group}",
            "group": group,
            "ratio": None,
            "groups": {
                k: (v.get("ratio") if isinstance(v, dict) else v)
                for k, v in groups.items()
            },
            "source": source,
        }

    if isinstance(entry, dict):
        ratio_raw = entry.get("ratio", entry.get("group_ratio"))
        desc = entry.get("desc") or entry.get("description")
    else:
        ratio_raw = entry
        desc = None
    try:
        ratio = float(ratio_raw)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": f"invalid ratio for {group}: {ratio_raw!r}",
            "group": group,
            "ratio": None,
            "source": source,
        }
    return {
        "ok": True,
        "group": group,
        "ratio": ratio,
        "desc": desc,
        "source": source,
        "groups": {
            k: (v.get("ratio") if isinstance(v, dict) else v) for k, v in groups.items()
        },
        "error": None,
    }


def apply_newapi_ratio_to_upstream(
    ratio: float,
    *,
    upstream_name: str,
    probe: dict[str, Any],
) -> dict[str, Any]:
    items = core.load_upstreams()
    target = None
    probe_id = str(probe.get("id") or "")
    for u in items:
        if str(u.get("name") or "") == str(upstream_name).strip():
            target = u
            break
    if target is None and probe_id:
        for u in items:
            if str(u.get("ratio_probe_id") or "") == probe_id:
                target = u
                break
    if target is None:
        return {"updated": False, "error": f"upstream not found: {upstream_name}"}

    new_mult = float(ratio)
    new_pri = _priority_from_multiplier(new_mult)
    old_mult = core.upstream_multiplier_value(target)
    old_pri = int(target.get("priority", 100))
    changed = abs(old_mult - new_mult) > 1e-9 or old_pri != new_pri
    target["multiplier"] = new_mult
    target["priority"] = new_pri
    target["ratio_source"] = "newapi"
    target["ratio_probe_id"] = probe_id
    target["ratio_probe_name"] = str(probe.get("name") or "")
    target["ratio_group"] = (
        str(probe.get("group") or "").strip() or target.get("ratio_group") or ""
    )
    target["ratio_priority_bias"] = float(probe.get("priority_bias") or 0.0)
    core.save_upstreams(items)
    if changed:
        log.info(
            "newapi ratio applied name=%s probe=%s mult %.4f→%.4f priority %s→%s",
            target.get("name"),
            probe_id,
            old_mult,
            new_mult,
            old_pri,
            new_pri,
        )
    return {
        "updated": True,
        "changed": changed,
        "upstream": target.get("name"),
        "upstream_id": target.get("id"),
        "multiplier": new_mult,
        "priority": new_pri,
        "old_multiplier": old_mult,
        "old_priority": old_pri,
    }


def probe_newapi_ratio_once(probe: dict[str, Any]) -> dict[str, Any]:
    probe = _normalize_newapi_probe(probe)
    probe_id = str(probe.get("id") or "")
    with state._newapi_probe_exec_lock:
        if not probe.get("enabled"):
            info = {
                "ok": False,
                "enabled": False,
                "error": "probe disabled",
                "checked_at": _iso_now(),
                "probe_id": probe_id,
                "probe_name": probe.get("name"),
            }
            state.set_newapi_probe_state(probe_id, info)
            return info

        result = _fetch_newapi_group_ratio(
            base_url=probe["base_url"],
            group=probe["group"],
            access_token=probe.get("access_token") or "",
        )
        info: dict[str, Any] = {
            "enabled": True,
            "checked_at": _iso_now(),
            "probe_id": probe_id,
            "probe_name": probe.get("name"),
            "base_url": probe["base_url"],
            "group": probe["group"],
            "interval_sec": probe["interval_sec"],
            "upstream_name": probe["upstream_name"],
            **result,
        }
        if result.get("ok") and result.get("ratio") is not None:
            applied = apply_newapi_ratio_to_upstream(
                float(result["ratio"]),
                upstream_name=probe["upstream_name"],
                probe=probe,
            )
            info["applied"] = applied
            if applied.get("updated"):
                info["multiplier"] = applied.get("multiplier")
                info["priority"] = applied.get("priority")
        else:
            info["applied"] = {"updated": False}
        info["next_run_at"] = time.time() + _seconds_until_next_clock_boundary(
            max(30, probe["interval_sec"])
        )
        state.set_newapi_probe_state(probe_id, info)
        return info


def _newapi_probe_loop() -> None:
    if state.get_newapi_stop_event().wait(2):
        return
    while not state.get_newapi_stop_event().is_set():
        now = time.time()
        for p in load_newapi_probes():
            if not p.get("enabled"):
                continue
            pid = str(p.get("id") or "")
            st = state.newapi_probe_snapshot(pid)
            if st.get("next_run_at") is not None and now < float(st.get("next_run_at")):
                continue
            try:
                probe_newapi_ratio_once(p)
            except Exception:
                log.exception("newapi ratio probe failed id=%s", pid)
        if state.get_newapi_stop_event().wait(2):
            break


def _start_newapi_probe_loop() -> None:
    if state._newapi_probe_loop_started:
        return
    items = load_newapi_probes()
    state._newapi_probe_loop_started = True
    t = threading.Thread(target=_newapi_probe_loop, name="newapi-ratio-probe", daemon=True)
    t.start()
    log.info(
        "newapi ratio probe loop started probes=%s enabled=%s",
        len(items),
        sum(1 for p in items if p.get("enabled")),
    )


def _start_background_loops() -> None:
    """启动模型可用性 + NewAPI 倍率探测。"""
    from sy import auth

    auth.restore_admin_sessions()
    _restore_model_availability()
    _start_probe_loop()
    _start_newapi_probe_loop()
