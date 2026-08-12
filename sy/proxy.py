"""Switch-codex 代理层：/v1/responses 多上游透传、failover 与重级联。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from sy import anthropic, auth, convert, core, logbook, probes, timeutil
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

    Cloudflare 经 loopback 进来时始终采用 cf-connecting-ip；
    其它代理头仅在 ``trust_proxy_headers`` 打开时采信。
    """
    peer = request.client.host if request.client else ""
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf and (trust_proxy_headers or core.is_loopback_ip(peer)):
        return cf.split(",")[0].strip()
    if trust_proxy_headers:
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[0]
    return peer


def _is_public_request(request: Request, trust_proxy_headers: bool = False) -> bool:
    """非回环对端、或 loopback+cf-connecting-ip（cloudflared）视为公网。"""
    peer = request.client.host if request.client else ""
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf and core.is_loopback_ip(peer):
        return True
    if peer and not core.is_loopback_ip(peer):
        return True
    if trust_proxy_headers:
        return bool(
            cf or (request.headers.get("x-forwarded-for") or "").strip()
        )
    return False


_SAFE_PATH_SEG = re.compile(r"^[A-Za-z0-9_.-]+$")


def _safe_responses_path(path: str, method: str) -> str:
    """Sanitize /v1/responses/{path}；拒绝穿越。POST 只允许集合本身。"""
    raw = (path or "").strip().strip("/")
    if not raw:
        return "responses"
    if method == "POST":
        raise HTTPException(status_code=405, detail="POST not allowed on nested responses path")
    if ".." in raw or "\\" in raw or raw.startswith("/"):
        raise HTTPException(status_code=400, detail="invalid path")
    parts = [p for p in raw.split("/") if p]
    if not parts or any(not _SAFE_PATH_SEG.fullmatch(p) for p in parts):
        raise HTTPException(status_code=400, detail="invalid path")
    return "responses/" + "/".join(parts)


class _SseLineBuffer:
    """按完整 UTF-8 行切 SSE，避免 chunk 截断 JSON。"""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> list[str]:
        if not chunk:
            return []
        self._buf += chunk
        lines: list[str] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            raw, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            lines.append(raw.decode("utf-8", errors="replace"))
        return lines

    def flush(self) -> Optional[str]:
        if not self._buf:
            return None
        raw, self._buf = self._buf, b""
        return raw.decode("utf-8", errors="replace")


class _UsageSink:
    def __init__(self) -> None:
        self.usage: Optional[dict] = None

    def feed_obj(self, obj: Any) -> None:
        found = logbook._find_usage_in_obj(obj)
        if found:
            self.usage = logbook._merge_usage(self.usage, found)

    def feed_line(self, line: str) -> None:
        text = line.strip()
        if text.startswith("data:"):
            text = text[5:].strip()
        if not text.startswith("{"):
            return
        try:
            obj = json.loads(text)
        except Exception:
            return
        self.feed_obj(obj)


def _sse_error_bytes(message: str) -> bytes:
    payload = json.dumps(
        {"type": "error", "error": {"type": "api_error", "message": message}},
        ensure_ascii=False,
    )
    return f"event: error\ndata: {payload}\n\n".encode("utf-8")


def _stream_headers(base: dict[str, str]) -> dict[str, str]:
    out = dict(base)
    out.setdefault("Cache-Control", "no-cache")
    out.setdefault("X-Accel-Buffering", "no")
    return out


async def _aclose_quietly(*objs: Any) -> None:
    for obj in objs:
        if obj is None:
            continue
        try:
            await obj.aclose()
        except Exception:
            pass


def _live_ok_payload(
    client_model: str, route_pool: str, upstream: dict, status_code: int
) -> dict:
    mult = core.upstream_multiplier_value(upstream)
    return {
        "model": str(client_model),
        "pool": route_pool,
        "ok": True,
        "light": core.light_for_multiplier(mult, True),
        "multiplier": mult,
        "upstream": upstream.get("name"),
        "upstream_id": upstream.get("id"),
        "status_code": status_code,
        "duration_ms": None,
        "checked_at": _iso_now(),
        "attempts": 1,
        "attempt_detail": [],
        "error": None,
        "source": "live-request",
    }


async def _kick_recascade(
    client_model: Optional[str],
    failed_ids: set[str],
    timeout: float,
) -> None:
    if not client_model:
        return

    async def _run() -> None:
        try:
            await probes._cascade_probe_model(
                str(client_model),
                timeout=min(timeout, 30.0),
                record_log=True,
                exclude_ids=set(failed_ids),
            )
        except Exception:
            log.exception("background re-cascade failed model=%s", client_model)

    try:
        asyncio.create_task(_run())
    except Exception:
        log.exception("schedule re-cascade failed model=%s", client_model)


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
    passthrough: bool = False,
) -> httpx.Response:
    if passthrough:
        # Anthropic 原生透传：body 已是 Anthropic Messages 格式，直接发上游 {base}/messages。
        url = _build_upstream_url(upstream["base_url"], "messages")
        request_model = None
    else:
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
        "Content-Type": content_type or "application/json",
    }
    if passthrough:
        # Anthropic 原生端点用 x-api-key 认证（与 Anthropic 官方一致）
        headers["x-api-key"] = upstream["api_key"]
        # anthropic-version：客户端有则透传，缺失补默认值（借鉴 cc-switch forwarder）
        headers["anthropic-version"] = (
            extra_headers.get("anthropic-version") or "2023-06-01"
        )
        if extra_headers.get("anthropic-beta"):
            headers["anthropic-beta"] = extra_headers["anthropic-beta"]
    else:
        headers["Authorization"] = f"Bearer {upstream['api_key']}"
        for k in ("accept", "openai-beta"):
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
    extra = {
        k.lower(): v
        for k, v in request.headers.items()
        if k.lower() in ("accept", "openai-beta", "anthropic-beta")
    }
    req_body, req_body_len, req_body_trunc = logbook._request_body_for_log(body)

    method = request.method.upper()
    path_suffix = _safe_responses_path(path, method)
    log_path = f"/v1/responses/{path}" if path else "/v1/responses"

    # client model + stream + reasoning effort if present (passthrough — no rewrite)
    client_model = None
    stream = False
    reasoning_effort = None
    j: dict = {}
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
    session_id = logbook._extract_session_id(
        {k.lower(): v for k, v in request.headers.items()}, j
    )

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
        endpoint: Optional[str] = None,
        upstream_id: Optional[str] = None,
    ) -> None:
        logbook._record_log(
            client_ip=client_ip,
            method=method,
            path=log_path,
            endpoint=endpoint,
            session_id=session_id,
            pool=core.resolve_route_pool(client_model, active),
            client_model=client_model,
            reasoning_effort=reasoning_effort,
            upstream=upstream,
            upstream_id=upstream_id,
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
            pool=core.resolve_route_pool(client_model, active),
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
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    idx = 0
    auth_failovers = 0

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
                if (
                    not cascaded_once
                    and client_model
                    and prefer_name
                    and upstream.get("name") == prefer_name
                ):
                    cascaded_once = True
                    log.info(
                        "re-cascade after connection fail model=%s exclude=%s",
                        client_model,
                        list(failed_ids),
                    )
                    await _kick_recascade(client_model, failed_ids, timeout)
                    candidates = core.order_candidates_for_model(
                        client_model, route_pool, exclude_ids=set(failed_ids)
                    )
                    prefer_name = candidates[0].get("name") if candidates else None
                    idx = 0
                continue

            upstream_url = str(resp.request.url) if resp.request is not None else ""

            if resp.status_code < 400:
                log.info(
                    "success upstream=%s pool=%s status=%s",
                    upstream.get("name"),
                    umodel,
                    resp.status_code,
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
                        sink = _UsageSink()
                        sse_buf = _SseLineBuffer()
                        stream_error = None
                        client_disconnect = False
                        first_ms = None
                        try:
                            async for chunk in r.aiter_bytes():
                                if first_ms is None:
                                    first_ms = (time.perf_counter() - t0) * 1000.0
                                if not chunk:
                                    continue
                                for line in sse_buf.feed(chunk):
                                    stripped = line.strip()
                                    if not stripped.startswith("data:"):
                                        continue
                                    data = stripped[5:].strip()
                                    if data == "[DONE]":
                                        events = conv.finalize()
                                    elif data.startswith("{"):
                                        try:
                                            obj = json.loads(data)
                                        except Exception:
                                            continue
                                        sink.feed_obj(obj)
                                        events = conv.handle_chunk(obj)
                                    else:
                                        continue
                                    for ev in events:
                                        yield ev.encode("utf-8")
                            leftover = sse_buf.flush()
                            if leftover and leftover.strip().startswith("data:"):
                                data = leftover.strip()[5:].strip()
                                if data.startswith("{"):
                                    try:
                                        sink.feed_obj(json.loads(data))
                                    except Exception:
                                        pass
                            if not conv.is_completed():
                                for ev in conv.finalize():
                                    yield ev.encode("utf-8")
                        except asyncio.CancelledError:
                            client_disconnect = True
                            raise
                        except Exception as e:
                            stream_error = f"stream aborted: {e}"
                            try:
                                yield conv.failed_event(str(e), "stream_error").encode("utf-8")
                            except Exception:
                                pass
                        finally:
                            await _aclose_quietly(r, c)
                            stream_err_log_id = None
                            ok = not stream_error and conv.is_completed()
                            if not client_disconnect and not ok:
                                reason = stream_error or "stream ended without response.completed"
                                stream_err_log_id = record_error(
                                    status=r.status_code,
                                    error=reason,
                                    attempts=list(errors),
                                )
                            if ok and client_model:
                                probes._set_model_availability(
                                    str(client_model),
                                    _live_ok_payload(
                                        str(client_model), route_pool, up, r.status_code
                                    ),
                                )
                            record(
                                status=r.status_code,
                                upstream=up.get("name"),
                                url=uurl,
                                multiplier=core.upstream_multiplier_value(up),
                                error_log_id=stream_err_log_id,
                                attempts=list(errors) if errors else None,
                                usage=conv.latest_usage or sink.usage,
                                duration_ms=(time.perf_counter() - t0) * 1000.0,
                                ttft_ms=first_ms if first_ms is not None else arrived,
                                endpoint="chat",
                                upstream_id=up.get("id"),
                            )

                    out_headers = _stream_headers({
                        "content-type": "text/event-stream",
                        "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                        "x-switch-codex-pool": _header_safe(umodel),
                        "x-switch-codex-route-pool": _header_safe(route_pool),
                        "x-switch-codex-active-model": _header_safe(active),
                    })
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
                    if client_model:
                        probes._set_model_availability(
                            str(client_model),
                            _live_ok_payload(
                                str(client_model), route_pool, upstream, 200
                            ),
                        )
                    record(
                        status=200,
                        upstream=upstream.get("name"),
                        url=upstream_url,
                        multiplier=core.upstream_multiplier_value(upstream),
                        attempts=list(errors) if errors else None,
                        usage=usage,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                        ttft_ms=arrived_ms,
                        endpoint="chat",
                        upstream_id=upstream.get("id"),
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
                    sink = _UsageSink()
                    sse_buf = _SseLineBuffer()
                    stream_error = None
                    client_disconnect = False
                    first_ms = None
                    try:
                        async for chunk in r.aiter_bytes():
                            if first_ms is None:
                                first_ms = (time.perf_counter() - t0) * 1000.0
                            if chunk:
                                for line in sse_buf.feed(chunk):
                                    sink.feed_line(line)
                                yield chunk
                    except asyncio.CancelledError:
                        client_disconnect = True
                        raise
                    except Exception as e:
                        stream_error = f"stream aborted: {e}"
                        try:
                            yield _sse_error_bytes(str(e))
                        except Exception:
                            pass
                    finally:
                        leftover = sse_buf.flush()
                        if leftover:
                            sink.feed_line(leftover)
                        await _aclose_quietly(r, c)
                        stream_err_log_id = None
                        ok = stream_error is None
                        if stream_error and not client_disconnect:
                            stream_err_log_id = record_error(
                                status=r.status_code,
                                error=stream_error,
                                attempts=list(errors),
                            )
                        if ok and not client_disconnect and client_model:
                            probes._set_model_availability(
                                str(client_model),
                                _live_ok_payload(
                                    str(client_model), route_pool, up, r.status_code
                                ),
                            )
                        record(
                            status=r.status_code,
                            upstream=up.get("name"),
                            url=uurl,
                            multiplier=core.upstream_multiplier_value(up),
                            error_log_id=stream_err_log_id,
                            attempts=list(errors) if errors else None,
                            usage=sink.usage,
                            duration_ms=(time.perf_counter() - t0) * 1000.0,
                            ttft_ms=first_ms if first_ms is not None else arrived,
                            upstream_id=up.get("id"),
                        )

                out_headers = _stream_headers({})
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
            if resp.status_code in (401, 403):
                if auth_failovers >= 1:
                    failover = False
                else:
                    auth_failovers += 1
                    failover = True
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
                    endpoint="chat" if upstream.get("chat_completions") else None,
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


@router.api_route("/v1/messages", methods=["POST"])
async def proxy_anthropic_messages(
    request: Request,
    _: str = Depends(auth.require_client_key),
):
    """Claude Code 兼容端点：Anthropic Messages → OpenAI Responses 转换后
    走与 /v1/responses 完全平行的多上游路由、failover 与重级联；响应再转回
    Anthropic 格式（非流式 JSON 或 event-stream SSE）。"""
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
    extra = {
        k.lower(): v
        for k, v in request.headers.items()
        if k.lower() in ("accept", "openai-beta", "anthropic-version", "anthropic-beta")
    }

    log_path = "/v1/messages"
    method = "POST"

    try:
        _j = json.loads(body) if body else {}
    except Exception:
        _j = {}
    if not isinstance(_j, dict):
        _j = {}
    session_id = logbook._extract_session_id(
        {k.lower(): v for k, v in request.headers.items()}, _j
    )
    # 先置默认值：错误路径的 record()/record_error() 调用早于完整解析（如非法
    # JSON 的 400 分支），闭包引用不能未定义（此前只预置了 is_classifier）。
    is_classifier = False
    client_model = None
    stream = False
    reasoning_effort = None
    req_body = None
    req_body_len = 0
    req_body_trunc = False

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
            session_id=session_id,
            pool=core.resolve_route_pool(client_model, active),
            client_model=client_model,
            reasoning_effort=reasoning_effort,
            is_classifier=is_classifier,
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
            pool=core.resolve_route_pool(client_model, active),
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

    # 解析 Anthropic Messages 请求 → Responses 请求体（model 原样透传，路由关键）。
    try:
        j = json.loads(body) if body else {}
    except Exception:
        eid = record_error(status=400, error="invalid anthropic JSON body")
        record(status=400, error_log_id=eid)
        raise HTTPException(status_code=400, detail="请求体必须是合法的 JSON")
    if not isinstance(j, dict):
        j = {}
    client_model = j.get("model")
    stream = bool(j.get("stream", False))
    # 思考强度：统一走 anthropic._resolve_reasoning_effort 归一化（thinking /
    # output_config / reasoning_effort 三种客户端写法 → 真实 effort 值），
    # 日志记录与转换共用同一逻辑，避免标记字符串漂移。
    reasoning_effort = anthropic._resolve_reasoning_effort(j)
    # Claude Code auto-mode 分类器：非流式 + 无 effort 信号。强制 low（分类
    # 判定用不到高强度思考，省延迟省 token）+ 打标，便于日志区分主对话流。
    is_classifier = anthropic.looks_like_classifier(j)
    if is_classifier:
        reasoning_effort = "low"
    req_payload = anthropic.anthropic_body_to_responses(j)
    # openai-all 是池标识而非真实模型名：Claude Code 发 model=openai-all 时，
    # 映射到该池的默认入口模型（gpt-5.6-luna），使路由候选与上游 model_map 匹配。
    if client_model == core.DEFAULT_MODEL:
        client_model = core.DEFAULT_CLIENT_MODELS[0]
        req_payload["model"] = client_model
    # reasoning 注入（分类器强制 low / 客户端显式 effort）：仅对支持 reasoning
    # 参数的模型注入——不支持的池收到陌生参数会被上游 400。映射后才判定，
    # 保证 openai-all 池按入口模型（gpt-5.6-luna）判断。
    if is_classifier:
        target = str(req_payload.get("model") or client_model)
        if anthropic._supports_reasoning_effort(target):
            req_payload["reasoning"] = {"effort": "low"}
    else:
        effort = anthropic._resolve_reasoning_effort(j)
        if effort and anthropic._supports_reasoning_effort(str(client_model)):
            req_payload["reasoning"] = {"effort": effort}
    upstream_body = json.dumps(req_payload, ensure_ascii=False).encode("utf-8")
    req_body, req_body_len, req_body_trunc = logbook._request_body_for_log(upstream_body)

    # 公网调用开关 + IP 黑白名单（与 /v1/responses 一致）。
    if is_public_request:
        if not core.public_access_enabled():
            eid = record_error(status=403, error="public access disabled")
            record(status=403, error_log_id=eid)
            raise HTTPException(status_code=403, detail="Public API access is disabled")
        if not core.ip_allowed(client_ip):
            eid = record_error(status=403, error=f"IP not allowed: {client_ip}")
            record(status=403, error_log_id=eid)
            raise HTTPException(status_code=403, detail="IP not allowed")

    # 按客户端模型路由：优先可用性缓存胜者，其次倍率低→高，失败则重级联。
    route_pool = core.resolve_route_pool(client_model, active)
    failed_ids: set[str] = set()
    cascaded_once = False
    candidates = core.order_candidates_for_model(client_model, route_pool)
    if not candidates:
        route_pool = active
        candidates = core.order_candidates_for_model(client_model, route_pool)
    if not candidates:
        eid = record_error(status=503, error="no enabled upstreams for route pool")
        record(status=503, error_log_id=eid)
        raise HTTPException(
            status_code=503,
            detail=(
                f"无可用上游: route_pool={route_pool!r} "
                f"(client_model={client_model!r}, active_model={active!r})。"
                f"请为该池添加或启用上游。"
            ),
        )

    prefer_name = candidates[0].get("name") if candidates else None
    log.info(
        "proxy(claude) active_pool=%s route_pool=%s client_model=%s prefer=%s candidates=%s",
        active,
        route_pool,
        client_model,
        prefer_name,
        [u.get("name") for u in candidates],
    )

    errors: list[dict] = []
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    idx = 0
    auth_failovers = 0

    try:
        while idx < len(candidates):
            upstream = candidates[idx]
            idx += 1
            uid = str(upstream.get("id") or "")
            umodel = core.normalize_model(upstream.get("model"))
            # 双模式：上游声明 anthropic_messages 时原生透传（body 用原始 Anthropic 请求），
            # 否则走 Responses 转换层（body 用转换后的 upstream_body）。
            passthrough_mode = bool(upstream.get("anthropic_messages"))
            # 透传模式也改写 model：客户端恒发 openai-all 池标识（或 DeepSeek
            # slug），真实 Anthropic 网关不认。按上游 model_map 改写后再转发；
            # 无映射时 _rewrite_request_model 原样返回，行为同改造前。
            forward_body = upstream_body
            if passthrough_mode:
                request_model = core.upstream_request_model(upstream, client_model)
                if request_model is None:
                    # 上游 model_map 可能以原始客户端标识为键（如 openai-all），
                    # 归一化后的 client_model 查不到时用 body 里的原值再查一次。
                    request_model = core.upstream_request_model(upstream, j.get("model"))
                forward_body = _rewrite_request_model(body, request_model)
            try:
                resp = await _forward_once(
                    client,
                    upstream,
                    method,
                    "responses",
                    forward_body,
                    content_type,
                    extra,
                    client_model,
                    passthrough=passthrough_mode,
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
                if (
                    not cascaded_once
                    and client_model
                    and prefer_name
                    and upstream.get("name") == prefer_name
                ):
                    cascaded_once = True
                    log.info(
                        "re-cascade after connection fail model=%s exclude=%s",
                        client_model,
                        list(failed_ids),
                    )
                    await _kick_recascade(client_model, failed_ids, timeout)
                    candidates = core.order_candidates_for_model(
                        client_model, route_pool, exclude_ids=set(failed_ids)
                    )
                    prefer_name = candidates[0].get("name") if candidates else None
                    idx = 0
                continue

            upstream_url = str(resp.request.url) if resp.request is not None else ""

            if resp.status_code < 400:
                log.info(
                    "success upstream=%s pool=%s status=%s",
                    upstream.get("name"),
                    umodel,
                    resp.status_code,
                )
                arrived_ms = (time.perf_counter() - t0) * 1000.0
                out_ct = resp.headers.get("content-type", "")

                if passthrough_mode:
                    # 原生透传：上游响应本身就是 Anthropic Messages 格式，原样返回（零转换）。
                    if "event-stream" in out_ct:

                        async def stream_passthrough(
                            r=resp,
                            c=client,
                            up=upstream,
                            uurl=upstream_url,
                            arrived=arrived_ms,
                        ):
                            buf = bytearray()
                            stream_error = None
                            client_disconnect = False
                            first_ms = None
                            try:
                                async for chunk in r.aiter_bytes():
                                    if first_ms is None:
                                        first_ms = (time.perf_counter() - t0) * 1000.0
                                    if chunk:
                                        if len(buf) < LOG_STREAM_BUF_MAX:
                                            buf.extend(chunk)
                                        yield chunk
                            except asyncio.CancelledError:
                                client_disconnect = True
                                raise
                            except Exception as e:
                                stream_error = f"stream aborted: {e}"
                                raise
                            finally:
                                await r.aclose()
                                await c.aclose()
                                stream_err_log_id = None
                                if stream_error and not client_disconnect:
                                    stream_err_log_id = record_error(
                                        status=r.status_code,
                                        error=stream_error,
                                        attempts=list(errors),
                                    )
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

                        out_headers = {
                            "content-type": out_ct or "text/event-stream",
                            "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                            "x-switch-codex-pool": _header_safe(umodel),
                            "x-switch-codex-route-pool": _header_safe(route_pool),
                            "x-switch-codex-active-model": _header_safe(active),
                        }
                        if client_model is not None:
                            out_headers["x-switch-codex-client-model"] = _header_safe(client_model)
                        return StreamingResponse(
                            stream_passthrough(),
                            status_code=resp.status_code,
                            headers=out_headers,
                            media_type=out_ct or "text/event-stream",
                        )
                    raw = await resp.aread()
                    await resp.aclose()
                    usage = logbook._extract_usage(raw)
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
                        "content-type": resp.headers.get("content-type", "application/json"),
                        "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                        "x-switch-codex-pool": _header_safe(umodel),
                        "x-switch-codex-route-pool": _header_safe(route_pool),
                        "x-switch-codex-active-model": _header_safe(active),
                    }
                    if client_model is not None:
                        out_headers["x-switch-codex-client-model"] = _header_safe(client_model)
                    await client.aclose()
                    return Response(
                        content=raw,
                        status_code=200,
                        media_type="application/json",
                        headers=out_headers,
                    )

                chat_mode = bool(upstream.get("chat_completions"))
                if "event-stream" in out_ct:
                    # 流式：上游 SSE → Anthropic Messages SSE。
                    # - chat_completions 上游：先 ChatSseToResponses → Responses SSE，
                    #   再 ResponsesSseToAnthropic → Anthropic SSE；
                    # - 其它上游：直接 Responses SSE → Anthropic SSE。
                    converter = anthropic.ResponsesSseToAnthropic()
                    converter.model = client_model or upstream.get("model") or ""
                    chat_conv = convert.ChatSseToResponses() if chat_mode else None

                    def feed_responses_sse(sse_list: list[str]) -> list[str]:
                        """把 ChatSseToResponses 产出的 Responses SSE 字符串喂给
                        ResponsesSseToAnthropic，返回应下发的 Anthropic SSE 列表。"""
                        out: list[str] = []
                        for ev in sse_list:
                            for line in ev.splitlines():
                                line = line.strip()
                                if not line.startswith("data:"):
                                    continue
                                data = line[5:].strip()
                                if data == "[DONE]" or not data.startswith("{"):
                                    continue
                                try:
                                    obj = json.loads(data)
                                except Exception:
                                    continue
                                out.extend(converter.handle_chunk(obj))
                        return out

                    async def stream_body_convert(
                        r=resp,
                        c=client,
                        up=upstream,
                        uurl=upstream_url,
                        conv=converter,
                        chatc=chat_conv,
                        arrived=arrived_ms,
                    ):
                        buf = bytearray()
                        stream_error = None
                        client_disconnect = False
                        first_ms = None
                        pending_line = ""
                        chat_finalized = False
                        try:
                            async for chunk in r.aiter_bytes():
                                if first_ms is None:
                                    first_ms = (time.perf_counter() - t0) * 1000.0
                                if not chunk:
                                    continue
                                if len(buf) < LOG_STREAM_BUF_MAX:
                                    buf.extend(chunk)
                                # 跨 chunk 缓冲：SSE data 行可能被任意截断，暂存最后不完整行。
                                pending_line += chunk.decode("utf-8", errors="replace")
                                lines = pending_line.split("\n")
                                pending_line = lines.pop()
                                for line in lines:
                                    line = line.strip()
                                    if line.startswith("data:"):
                                        data = line[5:].strip()
                                        if data == "[DONE]":
                                            if chatc is not None and not chat_finalized:
                                                chat_finalized = True
                                                events = feed_responses_sse(chatc.finalize())
                                            else:
                                                events = conv.finalize()
                                        elif data.startswith("{"):
                                            try:
                                                obj = json.loads(data)
                                            except Exception:
                                                continue
                                            if chatc is not None:
                                                events = feed_responses_sse(
                                                    chatc.handle_chunk(obj)
                                                )
                                            else:
                                                events = conv.handle_chunk(obj)
                                        else:
                                            continue
                                        for ev in events:
                                            yield ev.encode("utf-8")
                            # 流结束：处理缓冲中遗留的最后一行
                            if pending_line.strip():
                                line = pending_line.strip()
                                if not line.startswith("data:"):
                                    # 非数据行（id:/event:/注释行）：与主循环一致直接跳过，
                                    # 不引用 data/events（此前会 NameError 误判整个流 abort）。
                                    pass
                                else:
                                    data = line[5:].strip()
                                    if data == "[DONE]":
                                        if chatc is not None and not chat_finalized:
                                            chat_finalized = True
                                            events = feed_responses_sse(chatc.finalize())
                                        else:
                                            events = conv.finalize()
                                        for ev in events:
                                            yield ev.encode("utf-8")
                                    elif data.startswith("{"):
                                        try:
                                            obj = json.loads(data)
                                        except Exception:
                                            pass
                                        else:
                                            if chatc is not None:
                                                events = feed_responses_sse(
                                                    chatc.handle_chunk(obj)
                                                )
                                            else:
                                                events = conv.handle_chunk(obj)
                                            for ev in events:
                                                yield ev.encode("utf-8")
                            if chatc is not None and not chat_finalized:
                                chat_finalized = True
                                for ev in feed_responses_sse(chatc.finalize()):
                                    yield ev.encode("utf-8")
                            if not conv.is_completed():
                                for ev in conv.finalize():
                                    yield ev.encode("utf-8")
                        except asyncio.CancelledError:
                            client_disconnect = True
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
                            if not client_disconnect:
                                if stream_error or not conv.is_completed():
                                    reason = stream_error or "stream ended without message_stop"
                                    stream_err_log_id = record_error(
                                        status=r.status_code,
                                        error=reason,
                                        attempts=list(errors),
                                    )
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

                # 非流式：上游返回 JSON（含客户端 stream=true 但上游非流式的兜底）。
                raw = await resp.aread()
                await resp.aclose()
                try:
                    parsed = json.loads(raw)
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
                if chat_mode:
                    try:
                        resp_obj = convert.chat_response_to_responses(parsed)
                    except Exception as e:
                        eid = record_error(
                            status=502,
                            error=f"chat→responses conversion failed: {e}",
                            attempts=list(errors),
                        )
                        record(status=502, error_log_id=eid, attempts=list(errors) if errors else None)
                        await client.aclose()
                        return JSONResponse(
                            status_code=502,
                            content=anthropic.anthropic_error_response(
                                502, f"chat→responses conversion failed: {e}"
                            ),
                        )
                else:
                    resp_obj = parsed

                # 从转换后的 Responses 对象提取 usage：chat 上游的原始 usage 是
                # prompt_tokens/completion_tokens，需要先归一化成 responses 结构。
                usage = logbook._extract_usage(
                    json.dumps(resp_obj, ensure_ascii=False).encode("utf-8")
                )
                if stream:
                    # 客户端要流、上游却回 JSON：合成完整 Anthropic SSE 再下发
                    # （借鉴 cc-switch streaming_responses.responses_json_to_anthropic_sse）。
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
                    sse_events = anthropic.responses_json_to_anthropic_sse(resp_obj, req_payload)
                    out_headers = {
                        "content-type": "text/event-stream",
                        "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                        "x-switch-codex-pool": _header_safe(umodel),
                        "x-switch-codex-route-pool": _header_safe(route_pool),
                        "x-switch-codex-active-model": _header_safe(active),
                    }
                    if client_model is not None:
                        out_headers["x-switch-codex-client-model"] = _header_safe(client_model)
                    await client.aclose()
                    return StreamingResponse(
                        iter([ev.encode("utf-8") for ev in sse_events]),
                        status_code=200,
                        headers=out_headers,
                        media_type="text/event-stream",
                    )

                out = anthropic.responses_response_to_anthropic(resp_obj, req_payload)
                out_bytes = json.dumps(out, ensure_ascii=False).encode("utf-8")
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

            err_text = (await resp.aread()).decode("utf-8", errors="replace")
            await resp.aclose()
            capacity_error = _is_capacity_error(err_text)
            failover = (
                capacity_error
                or resp.status_code in FAILOVER_STATUS
                or resp.status_code >= 500
            )
            if resp.status_code in (401, 403):
                if auth_failovers >= 1:
                    failover = False
                else:
                    auth_failovers += 1
                    failover = True
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
                if not passthrough_mode:
                    # 非透传上游的错误是 OpenAI/Responses 风格，需要包成 Anthropic
                    # 错误信封，否则 Claude Code 解析不了（借鉴 cc-switch error_mapper）。
                    err_text = json.dumps(
                        anthropic.anthropic_error_response(
                            resp.status_code, err_text[:500]
                        ),
                        ensure_ascii=False,
                    )
                return Response(
                    content=err_text.encode("utf-8"),
                    status_code=resp.status_code,
                    media_type="application/json",
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

    # 全部上游失败：优先透传第一个可用 4xx，否则 502，错误体用 Anthropic 格式。
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
        content=anthropic.anthropic_error_response(
            final_status,
            f"All upstreams failed for client_model={client_model!r} "
            f"route_pool={route_pool} active_model={active}",
        ),
    )
