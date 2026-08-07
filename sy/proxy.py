"""Switch-codex 代理层：/v1/responses 多上游透传、failover 与重级联。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from sy import auth, convert, core, logbook, probes, timeutil
from sy.const import (
    CAPACITY_HINTS,
    ERROR_LOG_ATTEMPT_BODY_MAX,
    FAILOVER_STATUS,
    LOG_STREAM_BUF_MAX,
)

log = logging.getLogger("switchyard.proxy")
_iso_now = timeutil.iso_now

router = APIRouter()


def _is_capacity_error(text: Optional[str]) -> bool:
    """Detect upstream "model at capacity" style errors regardless of HTTP status."""
    lower = (text or "").lower()
    return any(hint in lower for hint in CAPACITY_HINTS)


def _header_safe(value: Any) -> str:
    s = str(value or "")
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return s.encode("ascii", errors="replace").decode("ascii")


def _caller_ip(request: Request, trust_proxy_headers: bool = False) -> str:
    """Return the effective client IP.

    By default only the socket peer is trusted. When ``trust_proxy_headers``
    is explicitly enabled (e.g. behind Cloudflare / a trusted reverse proxy),
    ``cf-connecting-ip`` and ``X-Forwarded-For`` are honored.
    """
    if trust_proxy_headers:
        cf = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf:
            return cf.split(",")[0].strip()
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[0]
    return request.client.host if request.client else ""


def _is_public_request(request: Request, trust_proxy_headers: bool = False) -> bool:
    """Direct non-loopback peers are public; with proxy headers enabled,
    loopback requests carrying trusted proxy headers are also public."""
    peer = request.client.host if request.client else ""
    if peer and not core.is_loopback_ip(peer):
        return True
    if trust_proxy_headers:
        return bool(
            (request.headers.get("cf-connecting-ip") or "").strip()
            or (request.headers.get("x-forwarded-for") or "").strip()
        )
    return False


def _build_upstream_url(base_url: str, path_suffix: str) -> str:
    base = base_url.rstrip("/")
    suf = path_suffix.lstrip("/")
    return f"{base}/{suf}"


def _rewrite_request_model(raw: bytes, request_model: Optional[str]) -> bytes:
    """Rewrite body.model to the upstream's configured request model.

    Returns the original bytes when there is nothing to change (or the body is
    not parseable JSON), so tolerant upstreams keep the exact payload.
    """
    if not raw or not request_model:
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(data, dict) or not isinstance(data.get("model"), str):
        return raw
    current = data["model"].strip()
    if not current or current == request_model:
        return raw
    data["model"] = request_model
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except Exception:
        return raw


async def _forward_once(
    client: httpx.AsyncClient,
    upstream: dict,
    method: str,
    path_suffix: str,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str],
    client_model: Optional[str] = None,
) -> httpx.Response:
    chat_mode = bool(upstream.get("chat_completions"))
    if chat_mode:
        path_suffix = "chat/completions"
        try:
            body = json.dumps(
                convert.responses_body_to_chat(json.loads(body)),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except Exception as e:
            raise ValueError(f"responses→chat conversion failed: {e}") from e
    url = _build_upstream_url(upstream["base_url"], path_suffix)
    request_model = core.upstream_request_model(upstream, client_model)
    body = _rewrite_request_model(body, request_model)
    headers = {
        "Authorization": f"Bearer {upstream['api_key']}",
        "Content-Type": content_type or "application/json",
    }
    for k in ("Accept", "OpenAI-Beta"):
        if k in extra_headers:
            headers[k] = extra_headers[k]
    log.info(
        "try upstream=%s pool=%s request_model=%s priority=%s %s %s",
        upstream.get("name"),
        core.normalize_model(upstream.get("model")),
        request_model,
        upstream.get("priority"),
        method,
        url,
    )
    req = client.build_request(method, url, content=body, headers=headers)
    return await client.send(req, stream=True)


@router.api_route("/v1/responses", methods=["POST"])
@router.api_route("/v1/responses/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy_responses(
    request: Request,
    path: str = "",
    _: str = Depends(auth.require_client_key),
):
    cfg = core.load_config()
    timeout = float(cfg.get("timeout_sec", 120))
    active = core.normalize_model(cfg.get("active_model"))
    t0 = time.perf_counter()
    public = core.load_public_config(cfg)
    trust_proxy_headers = bool(public.get("trust_proxy_headers"))
    client_ip = _caller_ip(request, trust_proxy_headers)
    is_public_request = _is_public_request(request, trust_proxy_headers)
    body = await request.body()
    content_type = request.headers.get("content-type", "application/json")
    extra = {k: v for k, v in request.headers.items() if k.lower() in ("accept", "openai-beta")}
    req_body, req_body_len, req_body_trunc = logbook._request_body_for_log(body)

    path_suffix = "responses" if not path else f"responses/{path}"
    log_path = f"/v1/responses/{path}" if path else "/v1/responses"
    method = request.method.upper()

    # client model + stream + reasoning effort if present (passthrough — no rewrite)
    client_model = None
    stream = False
    reasoning_effort = None
    if body:
        try:
            j = json.loads(body)
            if isinstance(j, dict):
                client_model = j.get("model")
                stream = bool(j.get("stream", False))
                re_obj = j.get("reasoning")
                if isinstance(re_obj, dict):
                    reasoning_effort = re_obj.get("effort")
                elif isinstance(re_obj, str):
                    reasoning_effort = re_obj
                if reasoning_effort is None:
                    reasoning_effort = j.get("reasoning_effort")
        except Exception:
            pass

    def record(
        status: Optional[int] = None,
        upstream: Optional[str] = None,
        url: Optional[str] = None,
        multiplier: Optional[float] = None,
        error_log_id: Optional[str] = None,
        attempts: Optional[list[dict]] = None,
        usage: Optional[dict] = None,
        duration_ms: Optional[float] = None,
        ttft_ms: Optional[float] = None,
    ) -> None:
        logbook._record_log(
            client_ip=client_ip,
            method=method,
            path=log_path,
            pool=active,
            client_model=client_model,
            reasoning_effort=reasoning_effort,
            upstream=upstream,
            upstream_url=url,
            multiplier=multiplier,
            status=status,
            duration_ms=(
                duration_ms
                if duration_ms is not None
                else (time.perf_counter() - t0) * 1000.0
            ),
            ttft_ms=ttft_ms,
            error_log_id=error_log_id,
            attempts=attempts,
            stream=stream,
            **logbook._usage_numbers(usage),
        )

    def record_error(
        status: Optional[int] = None,
        error: Optional[str] = None,
        attempts: Optional[list[dict]] = None,
        duration_ms: Optional[float] = None,
    ) -> str:
        return logbook._record_error_log(
            client_ip=client_ip,
            method=method,
            path=log_path,
            pool=active,
            client_model=client_model,
            stream=stream,
            status=status,
            error=error,
            duration_ms=(
                duration_ms
                if duration_ms is not None
                else (time.perf_counter() - t0) * 1000.0
            ),
            request_body=req_body,
            request_body_len=req_body_len,
            request_body_truncated=req_body_trunc,
            attempts=attempts or [],
        )

    # 公网调用开关 + IP 黑白名单（仅作用于 /v1/responses 客户端端点）。
    if is_public_request:
        if not core.public_access_enabled():
            eid = record_error(status=403, error="public access disabled")
            record(status=403, error_log_id=eid)
            raise HTTPException(status_code=403, detail="Public API access is disabled")
        if not core.ip_allowed(client_ip):
            eid = record_error(status=403, error=f"IP not allowed: {client_ip}")
            record(status=403, error_log_id=eid)
            raise HTTPException(status_code=403, detail="IP not allowed")

    # Route by client model availability when possible:
    # 1) prefer last cascade winner for this client_model
    # 2) then low→high multiplier within the model's pool
    # 3) on preferred failure → re-cascade probe (excluding failed ids) and continue
    route_pool = core.resolve_route_pool(client_model, active)
    failed_ids: set[str] = set()
    cascaded_once = False
    candidates = core.order_candidates_for_model(client_model, route_pool)
    if not candidates:
        # Fall back to active pool if client model maps to an empty pool.
        route_pool = active
        candidates = core.order_candidates_for_model(client_model, route_pool)
    if not candidates:
        eid = record_error(status=503, error="no enabled upstreams for route pool")
        record(status=503, error_log_id=eid)
        raise HTTPException(
            status_code=503,
            detail=(
                f"No enabled upstreams for route_pool={route_pool!r} "
                f"(client_model={client_model!r}, active_model={active!r}). "
                f"Add/enable an upstream for that pool."
            ),
        )

    prefer_name = candidates[0].get("name") if candidates else None
    log.info(
        "proxy active_pool=%s route_pool=%s client_model=%s prefer=%s candidates=%s",
        active,
        route_pool,
        client_model,
        prefer_name,
        [u.get("name") for u in candidates],
    )

    errors: list[dict] = []
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    idx = 0

    try:
        while idx < len(candidates):
            upstream = candidates[idx]
            idx += 1
            uid = str(upstream.get("id") or "")
            umodel = core.normalize_model(upstream.get("model"))
            try:
                resp = await _forward_once(
                    client,
                    upstream,
                    method,
                    path_suffix,
                    body,
                    content_type,
                    extra,
                    client_model,
                )
            except Exception as e:
                log.warning("upstream=%s connection error: %s", upstream.get("name"), e)
                if uid:
                    failed_ids.add(uid)
                probes.mark_model_upstream_failed(
                    client_model, upstream, status=None, error=str(e)
                )
                errors.append(
                    {
                        "upstream": upstream.get("name"),
                        "pool": umodel,
                        "priority": int(upstream.get("priority", 100)),
                        "multiplier": core.upstream_multiplier_value(upstream),
                        "status": None,
                        "error": str(e),
                        "failover": True,
                    }
                )
                # Preferred/availability winner failed → re-cascade once.
                if (
                    not cascaded_once
                    and client_model
                    and prefer_name
                    and upstream.get("name") == prefer_name
                ):
                    cascaded_once = True
                    probe_timeout = min(timeout, 30.0)
                    log.info(
                        "re-cascade after connection fail model=%s exclude=%s",
                        client_model,
                        list(failed_ids),
                    )
                    try:
                        avail = await probes._cascade_probe_model(
                            str(client_model),
                            timeout=probe_timeout,
                            record_log=True,
                            exclude_ids=set(failed_ids),
                        )
                    except Exception:
                        log.exception("re-cascade failed model=%s", client_model)
                        avail = None
                    if avail and avail.get("ok"):
                        candidates = core.order_candidates_for_model(
                            client_model, route_pool, exclude_ids=set(failed_ids)
                        )
                        prefer_name = candidates[0].get("name") if candidates else None
                        idx = 0
                        log.info(
                            "re-cascade winner=%s remaining=%s",
                            prefer_name,
                            [u.get("name") for u in candidates],
                        )
                continue

            upstream_url = str(resp.request.url) if resp.request is not None else ""

            if resp.status_code < 400:
                log.info(
                    "success upstream=%s pool=%s status=%s",
                    upstream.get("name"),
                    umodel,
                    resp.status_code,
                )
                # Refresh availability cache with the live winner.
                if client_model:
                    mult = core.upstream_multiplier_value(upstream)
                    probes._set_model_availability(
                        str(client_model),
                        {
                            "model": str(client_model),
                            "pool": route_pool,
                            "ok": True,
                            "light": core.light_for_multiplier(mult, True),
                            "multiplier": mult,
                            "upstream": upstream.get("name"),
                            "upstream_id": upstream.get("id"),
                            "status_code": resp.status_code,
                            "duration_ms": None,
                            "checked_at": _iso_now(),
                            "attempts": 1,
                            "attempt_detail": [],
                            "error": None,
                            "source": "live-request",
                        },
                    )
                arrived_ms = (time.perf_counter() - t0) * 1000.0
                convert_mode = bool(upstream.get("chat_completions"))
                out_ct = resp.headers.get("content-type", "")

                if convert_mode and "event-stream" in out_ct:
                    # Chat SSE → Responses SSE 转换流。
                    converter = convert.ChatSseToResponses()
                    converter.model = client_model or upstream.get("model") or ""

                    async def stream_body_convert(
                        r=resp,
                        c=client,
                        up=upstream,
                        uurl=upstream_url,
                        conv=converter,
                        arrived=arrived_ms,
                    ):
                        buf = bytearray()
                        stream_error = None
                        first_ms = None
                        try:
                            async for chunk in r.aiter_raw():
                                if first_ms is None:
                                    first_ms = (time.perf_counter() - t0) * 1000.0
                                if not chunk:
                                    continue
                                if len(buf) < LOG_STREAM_BUF_MAX:
                                    buf.extend(chunk)
                                text = chunk.decode("utf-8", errors="replace")
                                for line in text.splitlines():
                                    line = line.strip()
                                    if line.startswith("data:"):
                                        data = line[5:].strip()
                                        if data == "[DONE]":
                                            events = conv.finalize()
                                        elif data.startswith("{"):
                                            try:
                                                obj = json.loads(data)
                                            except Exception:
                                                continue
                                            events = conv.handle_chunk(obj)
                                        else:
                                            continue
                                        for ev in events:
                                            yield ev.encode("utf-8")
                            if not conv.is_completed():
                                for ev in conv.finalize():
                                    yield ev.encode("utf-8")
                        except asyncio.CancelledError:
                            stream_error = "client disconnected before stream completed"
                            try:
                                yield conv.failed_event(stream_error, "client_disconnect").encode("utf-8")
                            except Exception:
                                pass
                            raise
                        except Exception as e:
                            stream_error = f"stream aborted: {e}"
                            try:
                                yield conv.failed_event(str(e), "stream_error").encode("utf-8")
                            except Exception:
                                pass
                        finally:
                            await r.aclose()
                            await c.aclose()
                            stream_err_log_id = None
                            if stream_error or not conv.is_completed():
                                reason = stream_error or "stream ended without response.completed"
                                stream_err_log_id = record_error(
                                    status=r.status_code,
                                    error=reason,
                                    attempts=list(errors),
                                )
                            if not conv.is_completed():
                                status = 499
                            else:
                                status = r.status_code
                            usage = conv.latest_usage or logbook._extract_usage(bytes(buf))
                            record(
                                status=status,
                                upstream=up.get("name"),
                                url=uurl,
                                multiplier=core.upstream_multiplier_value(up),
                                error_log_id=stream_err_log_id,
                                attempts=list(errors) if errors else None,
                                usage=usage,
                                duration_ms=(time.perf_counter() - t0) * 1000.0,
                                ttft_ms=first_ms if first_ms is not None else arrived,
                            )

                    out_headers = {
                        "content-type": "text/event-stream",
                        "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                        "x-switch-codex-pool": _header_safe(umodel),
                        "x-switch-codex-route-pool": _header_safe(route_pool),
                        "x-switch-codex-active-model": _header_safe(active),
                    }
                    if client_model is not None:
                        out_headers["x-switch-codex-client-model"] = _header_safe(client_model)
                    return StreamingResponse(
                        stream_body_convert(),
                        status_code=200,
                        headers=out_headers,
                        media_type="text/event-stream",
                    )

                if convert_mode:
                    # 非流式 Chat Completions 响应 → Responses 对象。
                    raw = await resp.aread()
                    await resp.aclose()
                    try:
                        chat_obj = json.loads(raw)
                    except Exception:
                        out_headers = {
                            "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                            "x-switch-codex-pool": _header_safe(umodel),
                            "x-switch-codex-route-pool": _header_safe(route_pool),
                            "x-switch-codex-active-model": _header_safe(active),
                        }
                        await client.aclose()
                        return Response(
                            content=raw,
                            status_code=502,
                            media_type="application/json",
                            headers=out_headers,
                        )
                    out = convert.chat_response_to_responses(chat_obj)
                    out_bytes = json.dumps(out, ensure_ascii=False).encode("utf-8")
                    usage = logbook._extract_usage(out_bytes)
                    record(
                        status=200,
                        upstream=upstream.get("name"),
                        url=upstream_url,
                        multiplier=core.upstream_multiplier_value(upstream),
                        attempts=list(errors) if errors else None,
                        usage=usage,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                        ttft_ms=arrived_ms,
                    )
                    out_headers = {
                        "content-type": "application/json",
                        "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                        "x-switch-codex-pool": _header_safe(umodel),
                        "x-switch-codex-route-pool": _header_safe(route_pool),
                        "x-switch-codex-active-model": _header_safe(active),
                    }
                    if client_model is not None:
                        out_headers["x-switch-codex-client-model"] = _header_safe(client_model)
                    await client.aclose()
                    return Response(
                        content=out_bytes,
                        status_code=200,
                        media_type="application/json",
                        headers=out_headers,
                    )

                async def stream_body(
                    r=resp,
                    c=client,
                    up=upstream,
                    uurl=upstream_url,
                    arrived=arrived_ms,
                ):
                    buf = bytearray()
                    stream_error = None
                    first_ms = None
                    try:
                        async for chunk in r.aiter_raw():
                            if first_ms is None:
                                first_ms = (time.perf_counter() - t0) * 1000.0
                            if chunk:
                                if len(buf) < LOG_STREAM_BUF_MAX:
                                    buf.extend(chunk)
                                yield chunk
                    except asyncio.CancelledError:
                        stream_error = "client disconnected before stream completed"
                        raise
                    except Exception as e:
                        stream_error = f"stream aborted: {e}"
                        raise
                    finally:
                        await r.aclose()
                        await c.aclose()
                        stream_err_log_id = None
                        if stream_error:
                            stream_err_log_id = record_error(
                                status=r.status_code,
                                error=stream_error,
                                attempts=list(errors),
                            )
                        if stream_error:
                            status = 499
                        else:
                            status = r.status_code
                        record(
                            status=status,
                            upstream=up.get("name"),
                            url=uurl,
                            multiplier=core.upstream_multiplier_value(up),
                            error_log_id=stream_err_log_id,
                            attempts=list(errors) if errors else None,
                            usage=logbook._extract_usage(bytes(buf)),
                            duration_ms=(time.perf_counter() - t0) * 1000.0,
                            ttft_ms=first_ms if first_ms is not None else arrived,
                        )

                out_headers = {}
                ct = resp.headers.get("content-type")
                if ct:
                    out_headers["content-type"] = ct
                out_headers["x-switch-codex-upstream"] = _header_safe(upstream.get("name", ""))
                out_headers["x-switch-codex-pool"] = _header_safe(umodel)
                out_headers["x-switch-codex-route-pool"] = _header_safe(route_pool)
                out_headers["x-switch-codex-active-model"] = _header_safe(active)
                if client_model is not None:
                    out_headers["x-switch-codex-client-model"] = _header_safe(client_model)
                return StreamingResponse(
                    stream_body(),
                    status_code=resp.status_code,
                    headers=out_headers,
                    media_type=ct,
                )

            err_text = (await resp.aread()).decode("utf-8", errors="replace")
            await resp.aclose()
            capacity_error = _is_capacity_error(err_text)
            failover = (
                capacity_error
                or resp.status_code in FAILOVER_STATUS
                or resp.status_code >= 500
            )
            log.warning(
                "upstream=%s status=%s failover=%s body=%s",
                upstream.get("name"),
                resp.status_code,
                failover,
                err_text[:300],
            )
            if uid:
                failed_ids.add(uid)
            probes.mark_model_upstream_failed(
                client_model,
                upstream,
                status=resp.status_code,
                error=err_text[:500],
            )
            errors.append(
                {
                    "upstream": upstream.get("name"),
                    "pool": umodel,
                    "priority": int(upstream.get("priority", 100)),
                    "multiplier": core.upstream_multiplier_value(upstream),
                    "url": upstream_url,
                    "status": resp.status_code,
                    "error": err_text[:ERROR_LOG_ATTEMPT_BODY_MAX],
                    "failover": failover,
                    "capacity_error": capacity_error,
                }
            )
            if not failover:
                eid = record_error(
                    status=resp.status_code,
                    error=err_text[:500],
                    attempts=list(errors),
                )
                record(
                    status=resp.status_code,
                    upstream=upstream.get("name"),
                    url=upstream_url,
                    multiplier=core.upstream_multiplier_value(upstream),
                    error_log_id=eid,
                    attempts=list(errors) if errors else None,
                )
                await client.aclose()
                return Response(
                    content=err_text,
                    status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"),
                    headers={
                        "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                        "x-switch-codex-pool": _header_safe(umodel),
                        "x-switch-codex-route-pool": _header_safe(route_pool),
                        "x-switch-codex-active-model": _header_safe(active),
                    },
                )

            # On failover of the preferred availability winner, re-run cascade once.
            if (
                not cascaded_once
                and client_model
                and prefer_name
                and upstream.get("name") == prefer_name
            ):
                cascaded_once = True
                probe_timeout = min(timeout, 30.0)
                log.info(
                    "re-cascade after failover model=%s failed=%s exclude=%s",
                    client_model,
                    upstream.get("name"),
                    list(failed_ids),
                )
                try:
                    avail = await probes._cascade_probe_model(
                        str(client_model),
                        timeout=probe_timeout,
                        record_log=True,
                        exclude_ids=set(failed_ids),
                    )
                except Exception:
                    log.exception("re-cascade failed model=%s", client_model)
                    avail = None
                if avail and avail.get("ok"):
                    candidates = core.order_candidates_for_model(
                        client_model, route_pool, exclude_ids=set(failed_ids)
                    )
                    prefer_name = candidates[0].get("name") if candidates else None
                    idx = 0
                    log.info(
                        "re-cascade winner=%s remaining=%s",
                        prefer_name,
                        [u.get("name") for u in candidates],
                    )
                else:
                    # No new winner; continue residual low→high list excluding failed.
                    candidates = core.order_candidates_for_model(
                        client_model, route_pool, exclude_ids=set(failed_ids)
                    )
                    prefer_name = None
                    idx = 0
        await client.aclose()
    except Exception as e:
        await client.aclose()
        eid = record_error(status=None, error=f"proxy exception: {e}", attempts=list(errors))
        record(status=None, error_log_id=eid, attempts=list(errors) if errors else None)
        raise

    # 全部上游失败时，优先透传第一个可用的 4xx（如 429 限流），
    # 让请求日志与错误日志状态一致；没有 4xx 才回 502。
    final_status = 502
    first_4xx = next(
        (
            a.get("status")
            for a in errors
            if isinstance(a.get("status"), int) and 400 <= a["status"] < 500
        ),
        None,
    )
    if first_4xx is not None:
        final_status = first_4xx
    eid = record_error(
        status=final_status,
        error="all upstreams failed: " + json.dumps(errors, ensure_ascii=False)[:1000],
        attempts=list(errors),
    )
    record(
        status=final_status,
        error_log_id=eid,
        attempts=list(errors) if errors else None,
    )
    return JSONResponse(
        status_code=final_status,
        content={
            "error": {
                "message": (
                    f"All upstreams failed for client_model={client_model!r} "
                    f"route_pool={route_pool} active_model={active}"
                ),
                "type": "router_error",
                "active_model": active,
                "route_pool": route_pool,
                "client_model": client_model,
                "attempts": errors,
            }
        },
    )
