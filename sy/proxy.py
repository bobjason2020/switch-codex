"""Switch-codex 代理层：/v1/responses、/v1/alpha/search、/v1/messages 纯透传。

纯透传语义：
- 不做任何协议转换（去掉 Responses↔Chat、Anthropic↔Responses）。
- 不做多上游 failover：请求只按模型池选定的一个上游直连，失败如实返回。
- 不做 model 重写：客户端模型名原样透传，不改写 body。
- 不深度解析 SSE 改写字节流；仅旁路读取 usage 用于日志/统计。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Mapping, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from sy import auth, core, logbook, probes, timeutil
from sy.const import ERROR_LOG_ATTEMPT_BODY_MAX, LOG_STREAM_BUF_MAX

log = logging.getLogger("switchyard.proxy")
router = APIRouter()
_iso_now = timeutil.iso_now

# These headers describe the client-to-proxy connection or carry credentials
# that must be replaced for the selected upstream.  Everything else is kept so
# protocol-specific clients (including Codex compaction) can add headers
# without requiring a proxy release.
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_GENERATED_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "content-length",
        "host",
        "x-api-key",
    }
)
_HOP_BY_HOP_RESPONSE_HEADERS = _HOP_BY_HOP_HEADERS | frozenset(
    {"content-length", "content-encoding", "host"}
)

MAX_CLIENT_REQUEST_BODY_BYTES = 16 * 1024 * 1024
MAX_UPSTREAM_RESPONSE_BODY_BYTES = 32 * 1024 * 1024
_READ_CHUNK_SIZE = 64 * 1024


class _BodyTooLarge(ValueError):
    """Raised when a buffered request or upstream response exceeds its limit."""


def _content_length_exceeds(headers: Mapping[str, str], limit: int) -> bool:
    raw = headers.get("content-length")
    if not raw:
        return False
    try:
        return int(raw) > limit
    except (TypeError, ValueError):
        return False


async def _read_request_body_limited(request: Request) -> bytes:
    """Read the request body without allowing a chunked upload to grow unbounded."""
    if _content_length_exceeds(request.headers, MAX_CLIENT_REQUEST_BODY_BYTES):
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds {MAX_CLIENT_REQUEST_BODY_BYTES} byte limit",
        )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_CLIENT_REQUEST_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Request body exceeds {MAX_CLIENT_REQUEST_BODY_BYTES} byte limit",
            )
    return bytes(body)


async def _read_upstream_body_limited(response: httpx.Response) -> bytes:
    """Read a non-streaming upstream response with a fixed memory ceiling."""
    if _content_length_exceeds(response.headers, MAX_UPSTREAM_RESPONSE_BODY_BYTES):
        raise _BodyTooLarge(
            f"Upstream response exceeds {MAX_UPSTREAM_RESPONSE_BODY_BYTES} byte limit"
        )
    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=_READ_CHUNK_SIZE):
        body.extend(chunk)
        if len(body) > MAX_UPSTREAM_RESPONSE_BODY_BYTES:
            raise _BodyTooLarge(
                f"Upstream response exceeds {MAX_UPSTREAM_RESPONSE_BODY_BYTES} byte limit"
            )
    return bytes(body)


def _forward_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return end-to-end client headers safe to send to an upstream.

    Starlette's ``Headers`` is case-insensitive, but normalize keys here so
    auth replacement and downstream lookup remain deterministic.  The proxy
    owns authentication and framing headers; all other client headers are
    intentionally preserved, including ``x-codex-*`` and custom metadata.
    """
    connection_value = next(
        (value for key, value in headers.items() if str(key).lower() == "connection"),
        "",
    )
    connection_tokens = {
        token.strip().lower()
        for token in str(connection_value).split(",")
        if token.strip()
    }
    out: dict[str, str] = {}
    for key, value in headers.items():
        name = str(key).lower()
        if (
            name in _HOP_BY_HOP_HEADERS
            or name in connection_tokens
            or name in _GENERATED_REQUEST_HEADERS
        ):
            continue
        out[name] = str(value)
    return out


def _upstream_response_headers(
    response: httpx.Response,
    overrides: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Copy end-to-end upstream headers while leaving framing to Starlette."""
    out = {
        str(key).lower(): str(value)
        for key, value in response.headers.items()
        if str(key).lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
    }
    if overrides:
        out.update({str(key).lower(): str(value) for key, value in overrides.items()})
    return out


def _header_safe(value: Any) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ")[:512]


def _caller_ip(request: Request, trust_proxy_headers: bool = False) -> str:
    if trust_proxy_headers:
        for h in ("x-forwarded-for", "cf-connecting-ip", "x-real-ip"):
            raw = request.headers.get(h)
            if raw:
                return raw.split(",")[0].strip()
    return request.client.host if request.client else ""


def _is_public_request(request: Request, trust_proxy_headers: bool = False) -> bool:
    ip = _caller_ip(request, trust_proxy_headers)
    if ip in ("127.0.0.1", "::1", "localhost"):
        return False
    return True


_SAFE_PATH_SEG = __import__("re").compile(r"^[A-Za-z0-9._~-]+$")


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

    def __init__(self, max_bytes: int = LOG_STREAM_BUF_MAX) -> None:
        self._buf = b""
        self._max_bytes = max_bytes

    def feed(self, chunk: bytes) -> list[str]:
        if not chunk:
            return []
        self._buf += chunk
        lines: list[str] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            if nl > self._max_bytes:
                self._buf = b""
                raise _BodyTooLarge(
                    f"Upstream SSE line exceeds {self._max_bytes} byte limit"
                )
            raw, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            lines.append(raw.decode("utf-8", errors="replace"))
        if len(self._buf) > self._max_bytes:
            self._buf = b""
            raise _BodyTooLarge(
                f"Upstream SSE line exceeds {self._max_bytes} byte limit"
            )
        return lines

    def flush(self) -> Optional[str]:
        if not self._buf:
            return None
        raw, self._buf = self._buf, b""
        return raw.decode("utf-8", errors="replace")


class _UsageSink:
    """只读旁路：从透传流中提取 usage，不改写任何字节。"""

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


def _build_upstream_url(base_url: str, path_suffix: str) -> str:
    base = base_url.rstrip("/")
    suf = path_suffix.lstrip("/")
    return f"{base}/{suf}"


async def _forward_once(
    client: httpx.AsyncClient,
    upstream: dict,
    path_suffix: str,
    body: bytes,
    content_type: str,
    extra_headers: Mapping[str, str],
    *,
    method: str = "POST",
    anthropic: bool = False,
) -> httpx.Response:
    """单上游纯透传：body 原样、模型名不改写、无协议转换。

    ``anthropic=True`` 用于 /v1/messages（Anthropic Messages 原生），用
    x-api-key 认证；否则 /v1/responses 与 /v1/alpha/search 用 Bearer。
    """
    url = _build_upstream_url(upstream["base_url"], path_suffix)
    headers = _forward_request_headers(extra_headers)
    headers["content-type"] = content_type or headers.get(
        "content-type", "application/json"
    )
    if anthropic:
        # Anthropic 原生端点用 x-api-key 认证（与 Anthropic 官方一致）。
        headers["x-api-key"] = upstream["api_key"]
        headers["anthropic-version"] = (
            headers.get("anthropic-version") or "2023-06-01"
        )
    else:
        headers["Authorization"] = f"Bearer {upstream['api_key']}"
    log.info(
        "try upstream=%s pool=%s %s %s",
        upstream.get("name"),
        core.normalize_model(upstream.get("model")),
        method,
        url,
    )
    req = client.build_request(method, url, content=body, headers=headers)
    return await client.send(req, stream=True)


@router.api_route("/v1/alpha/search", methods=["POST"])
@router.api_route("/v1/responses", methods=["POST"])
@router.api_route("/v1/responses/{path:path}", methods=["GET", "POST", "DELETE"])
async def proxy_responses(
    request: Request,
    path: str = "",
    _: str = Depends(auth.require_client_key),
):
    """纯透传 /v1/responses 与 /v1/alpha/search。

    按模型池选定一个上游直连（保留按倍率/可用性缓存排序，但不 failover），
    失败如实返回。流式响应逐块透传并旁路提取 usage。
    """
    cfg = core.load_config()
    timeout = float(cfg.get("timeout_sec", 120))
    active = core.normalize_model(cfg.get("active_model"))
    t0 = time.perf_counter()
    public = core.load_public_config(cfg)
    trust_proxy_headers = bool(public.get("trust_proxy_headers"))
    client_ip = _caller_ip(request, trust_proxy_headers)
    is_public_request = _is_public_request(request, trust_proxy_headers)
    method = request.method.upper()
    body = await _read_request_body_limited(request)
    content_type = request.headers.get("content-type", "application/json")
    extra = _forward_request_headers(request.headers)
    req_body, req_body_len, req_body_trunc = logbook._request_body_for_log(body)

    standalone_search = request.url.path == "/v1/alpha/search"
    path_suffix = "alpha/search" if standalone_search else _safe_responses_path(path, method)
    log_path = (
        "/v1/alpha/search"
        if standalone_search
        else (f"/v1/responses/{path}" if path else "/v1/responses")
    )

    # client model + stream（仅用于日志/路由；body 原样透传，不改写）
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
                reasoning_effort = logbook._extract_reasoning_effort(j)
        except Exception:
            pass
    session_context = logbook._extract_session_context(
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
            **session_context,
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

    # 按模型池选定上游：取排序后第一个（倍率低→高，可用性缓存优先）。
    # 纯透传不做 failover——失败如实返回，不再切到其它上游重发。
    # /v1/responses 只接受 Responses 原生上游；chat_completions 上游因无转换层
    # 无法承接 Responses 协议，予以排除。
    route_pool = core.resolve_route_pool(client_model, active)
    candidates = core.order_candidates_for_model(client_model, route_pool)
    if standalone_search:
        candidates = [
            u for u in candidates if core.upstream_supports_standalone_web_search(u)
        ]
    else:
        candidates = [u for u in candidates if not u.get("chat_completions")]
    if not candidates:
        # Fall back to active pool if client model maps to an empty pool.
        route_pool = active
        candidates = core.order_candidates_for_model(client_model, route_pool)
        if standalone_search:
            candidates = [
                u for u in candidates if core.upstream_supports_standalone_web_search(u)
            ]
        else:
            candidates = [u for u in candidates if not u.get("chat_completions")]
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

    upstream = candidates[0]
    umodel = core.normalize_model(upstream.get("model"))
    log.info(
        "proxy active_pool=%s route_pool=%s client_model=%s upstream=%s",
        active,
        route_pool,
        client_model,
        upstream.get("name"),
    )

    errors: list[dict] = []
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        resp = await _forward_once(
            client, upstream, path_suffix, body, content_type, extra, method=method
        )
    except Exception as e:
        await client.aclose()
        log.warning("upstream=%s connection error: %s", upstream.get("name"), e)
        probes.mark_model_upstream_failed(client_model, upstream, status=None, error=str(e))
        errors.append(
            {
                "upstream": upstream.get("name"),
                "pool": umodel,
                "priority": int(upstream.get("priority", 100)),
                "multiplier": core.upstream_multiplier_value(upstream),
                "status": None,
                "error": str(e),
                "failover": False,
            }
        )
        eid = record_error(status=None, error=f"proxy exception: {e}", attempts=list(errors))
        record(status=None, error_log_id=eid, attempts=list(errors) if errors else None)
        raise HTTPException(status_code=502, detail=f"upstream connection error: {e}")

    upstream_url = str(resp.request.url) if resp.request is not None else ""
    arrived_ms = (time.perf_counter() - t0) * 1000.0
    out_ct = resp.headers.get("content-type", "")

    if resp.status_code < 400:
        log.info(
            "success upstream=%s pool=%s status=%s",
            upstream.get("name"),
            umodel,
            resp.status_code,
        )

        if "event-stream" in out_ct:
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
                    leftover = sse_buf.flush()
                    if leftover:
                        sink.feed_line(leftover)
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
                    await _aclose_quietly(r, c)
                    stream_err_log_id = None
                    if stream_error and not client_disconnect:
                        stream_err_log_id = record_error(
                            status=r.status_code,
                            error=stream_error,
                            attempts=list(errors),
                        )
                    if stream_error is None and not client_disconnect and client_model:
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
                        endpoint=("search" if standalone_search else None),
                        upstream_id=up.get("id"),
                    )

            out_headers = _stream_headers(
                _upstream_response_headers(
                    resp,
                    {
                        "content-type": out_ct or "text/event-stream",
                        "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                        "x-switch-codex-pool": _header_safe(umodel),
                        "x-switch-codex-route-pool": _header_safe(route_pool),
                        "x-switch-codex-active-model": _header_safe(active),
                    },
                )
            )
            if client_model is not None:
                out_headers["x-switch-codex-client-model"] = _header_safe(client_model)
            return StreamingResponse(
                stream_body(),
                status_code=resp.status_code,
                headers=out_headers,
                media_type=out_ct,
            )

        # 非流式（含 standalone search 的 JSON 信封）：读 body 透传 + 提取 usage。
        upstream_headers = _upstream_response_headers(resp)
        try:
            raw = await _read_upstream_body_limited(resp)
        except _BodyTooLarge as e:
            await _aclose_quietly(resp, client)
            eid = record_error(status=502, error=str(e), attempts=list(errors))
            record(status=502, error_log_id=eid, attempts=list(errors))
            return JSONResponse(status_code=502, content={"detail": str(e)})
        await resp.aclose()
        usage = logbook._extract_usage(raw)
        if client_model:
            probes._set_model_availability(
                str(client_model),
                _live_ok_payload(str(client_model), route_pool, upstream, resp.status_code),
            )
        record(
            status=resp.status_code,
            upstream=upstream.get("name"),
            url=upstream_url,
            multiplier=core.upstream_multiplier_value(upstream),
            attempts=list(errors) if errors else None,
            usage=usage,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            ttft_ms=arrived_ms,
            endpoint=("search" if standalone_search else None),
            upstream_id=upstream.get("id"),
        )
        await client.aclose()
        return Response(
            content=raw,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers={
                **upstream_headers,
                "content-type": resp.headers.get("content-type", "application/json"),
                "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                "x-switch-codex-pool": _header_safe(umodel),
                "x-switch-codex-route-pool": _header_safe(route_pool),
                "x-switch-codex-active-model": _header_safe(active),
            },
        )

    # 上游错误（>=400）：如实返回，不做 failover。
    upstream_headers = _upstream_response_headers(resp)
    try:
        err_text = (await _read_upstream_body_limited(resp)).decode(
            "utf-8", errors="replace"
        )
    except _BodyTooLarge as e:
        await _aclose_quietly(resp, client)
        eid = record_error(status=502, error=str(e), attempts=list(errors))
        record(status=502, error_log_id=eid, attempts=list(errors))
        return JSONResponse(status_code=502, content={"detail": str(e)})
    await resp.aclose()
    log.warning(
        "upstream=%s status=%s body=%s",
        upstream.get("name"),
        resp.status_code,
        err_text[:300],
    )
    probes.mark_model_upstream_failed(
        client_model, upstream, status=resp.status_code, error=err_text[:500]
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
            "failover": False,
        }
    )
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
        endpoint=("search" if standalone_search else None),
    )
    await client.aclose()
    return Response(
        content=err_text,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers={
            **upstream_headers,
            "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
            "x-switch-codex-pool": _header_safe(umodel),
            "x-switch-codex-route-pool": _header_safe(route_pool),
            "x-switch-codex-active-model": _header_safe(active),
        },
    )


@router.api_route("/v1/messages", methods=["POST"])
async def proxy_anthropic_messages(
    request: Request,
    _: str = Depends(auth.require_client_key),
):
    """纯透传 /v1/messages：Anthropic Messages 原生透传到 anthropic_messages 上游。

    不做 Anthropic↔Responses 转换；上游必须声明 anthropic_messages=True。
    失败如实返回，不做 failover。
    """
    cfg = core.load_config()
    timeout = float(cfg.get("timeout_sec", 120))
    active = core.normalize_model(cfg.get("active_model"))
    t0 = time.perf_counter()
    public = core.load_public_config(cfg)
    trust_proxy_headers = bool(public.get("trust_proxy_headers"))
    client_ip = _caller_ip(request, trust_proxy_headers)
    is_public_request = _is_public_request(request, trust_proxy_headers)
    body = await _read_request_body_limited(request)
    content_type = request.headers.get("content-type", "application/json")
    extra = _forward_request_headers(request.headers)

    log_path = "/v1/messages"
    method = "POST"

    try:
        _j = json.loads(body) if body else {}
    except Exception:
        _j = {}
    if not isinstance(_j, dict):
        _j = {}
    session_context = logbook._extract_session_context(
        {k.lower(): v for k, v in request.headers.items()}, _j
    )
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
        upstream_id: Optional[str] = None,
    ) -> None:
        logbook._record_log(
            client_ip=client_ip,
            method=method,
            path=log_path,
            **session_context,
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

    # 解析（仅用于日志/路由；body 原样透传）
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
    reasoning_effort = logbook._extract_reasoning_effort(j)
    req_body, req_body_len, req_body_trunc = logbook._request_body_for_log(body)

    # 公网调用开关 + IP 黑白名单。
    if is_public_request:
        if not core.public_access_enabled():
            eid = record_error(status=403, error="public access disabled")
            record(status=403, error_log_id=eid)
            raise HTTPException(status_code=403, detail="Public API access is disabled")
        if not core.ip_allowed(client_ip):
            eid = record_error(status=403, error=f"IP not allowed: {client_ip}")
            record(status=403, error_log_id=eid)
            raise HTTPException(status_code=403, detail="IP not allowed")

    # 按模型池选定上游：仅支持 anthropic_messages 原生上游（纯透传无转换层）。
    route_pool = core.resolve_route_pool(client_model, active)
    candidates = [
        u
        for u in core.order_candidates_for_model(client_model, route_pool)
        if u.get("anthropic_messages")
    ]
    if not candidates:
        route_pool = active
        candidates = [
            u
            for u in core.order_candidates_for_model(client_model, route_pool)
            if u.get("anthropic_messages")
        ]
    if not candidates:
        eid = record_error(
            status=503,
            error="no anthropic-messages upstream for route pool",
        )
        record(status=503, error_log_id=eid)
        raise HTTPException(
            status_code=503,
            detail=(
                f"No anthropic-messages upstream for route_pool={route_pool!r} "
                f"(client_model={client_model!r}, active_model={active!r}). "
                f"纯透传模式下 /v1/messages 只支持 anthropic_messages 上游。"
            ),
        )

    upstream = candidates[0]
    umodel = core.normalize_model(upstream.get("model"))
    log.info(
        "proxy(claude) active_pool=%s route_pool=%s client_model=%s upstream=%s",
        active,
        route_pool,
        client_model,
        upstream.get("name"),
    )

    errors: list[dict] = []
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        resp = await _forward_once(
            client,
            upstream,
            "messages",
            body,
            content_type,
            extra,
            method=method,
            anthropic=True,
        )
    except Exception as e:
        await client.aclose()
        log.warning("upstream=%s connection error: %s", upstream.get("name"), e)
        probes.mark_model_upstream_failed(client_model, upstream, status=None, error=str(e))
        errors.append(
            {
                "upstream": upstream.get("name"),
                "pool": umodel,
                "priority": int(upstream.get("priority", 100)),
                "multiplier": core.upstream_multiplier_value(upstream),
                "status": None,
                "error": str(e),
                "failover": False,
            }
        )
        eid = record_error(status=None, error=f"proxy exception: {e}", attempts=list(errors))
        record(status=None, error_log_id=eid, attempts=list(errors) if errors else None)
        raise HTTPException(status_code=502, detail=f"upstream connection error: {e}")

    upstream_url = str(resp.request.url) if resp.request is not None else ""
    arrived_ms = (time.perf_counter() - t0) * 1000.0
    out_ct = resp.headers.get("content-type", "")

    if resp.status_code < 400:
        log.info(
            "success upstream=%s pool=%s status=%s",
            upstream.get("name"),
            umodel,
            resp.status_code,
        )

        if "event-stream" in out_ct:
            async def stream_passthrough(
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
                    leftover = sse_buf.flush()
                    if leftover:
                        sink.feed_line(leftover)
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
                    await _aclose_quietly(r, c)
                    stream_err_log_id = None
                    if stream_error and not client_disconnect:
                        stream_err_log_id = record_error(
                            status=r.status_code,
                            error=stream_error,
                            attempts=list(errors),
                        )
                    if stream_error is None and not client_disconnect and client_model:
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

            out_headers = _stream_headers(
                _upstream_response_headers(
                    resp,
                    {
                        "content-type": out_ct or "text/event-stream",
                        "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                        "x-switch-codex-pool": _header_safe(umodel),
                        "x-switch-codex-route-pool": _header_safe(route_pool),
                        "x-switch-codex-active-model": _header_safe(active),
                    },
                )
            )
            if client_model is not None:
                out_headers["x-switch-codex-client-model"] = _header_safe(client_model)
            return StreamingResponse(
                stream_passthrough(),
                status_code=resp.status_code,
                headers=out_headers,
                media_type=out_ct or "text/event-stream",
            )

        # 非流式：读 body 透传 + 提取 usage。
        upstream_headers = _upstream_response_headers(resp)
        try:
            raw = await _read_upstream_body_limited(resp)
        except _BodyTooLarge as e:
            await _aclose_quietly(resp, client)
            eid = record_error(status=502, error=str(e), attempts=list(errors))
            record(status=502, error_log_id=eid, attempts=list(errors))
            return JSONResponse(status_code=502, content={"detail": str(e)})
        await resp.aclose()
        usage = logbook._extract_usage(raw)
        if client_model:
            probes._set_model_availability(
                str(client_model),
                _live_ok_payload(str(client_model), route_pool, upstream, resp.status_code),
            )
        record(
            status=resp.status_code,
            upstream=upstream.get("name"),
            url=upstream_url,
            multiplier=core.upstream_multiplier_value(upstream),
            attempts=list(errors) if errors else None,
            usage=usage,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            ttft_ms=arrived_ms,
            upstream_id=upstream.get("id"),
        )
        await client.aclose()
        return Response(
            content=raw,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers={
                **upstream_headers,
                "content-type": resp.headers.get("content-type", "application/json"),
                "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
                "x-switch-codex-pool": _header_safe(umodel),
                "x-switch-codex-route-pool": _header_safe(route_pool),
                "x-switch-codex-active-model": _header_safe(active),
            },
        )

    # 上游错误（>=400）：如实返回（Anthropic 原生上游错误体本身就是 Anthropic 格式）。
    upstream_headers = _upstream_response_headers(resp)
    try:
        err_text = (await _read_upstream_body_limited(resp)).decode(
            "utf-8", errors="replace"
        )
    except _BodyTooLarge as e:
        await _aclose_quietly(resp, client)
        eid = record_error(status=502, error=str(e), attempts=list(errors))
        record(status=502, error_log_id=eid, attempts=list(errors))
        return JSONResponse(status_code=502, content={"detail": str(e)})
    await resp.aclose()
    log.warning(
        "upstream=%s status=%s body=%s",
        upstream.get("name"),
        resp.status_code,
        err_text[:300],
    )
    probes.mark_model_upstream_failed(
        client_model, upstream, status=resp.status_code, error=err_text[:500]
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
            "failover": False,
        }
    )
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
            **upstream_headers,
            "x-switch-codex-upstream": _header_safe(upstream.get("name", "")),
            "x-switch-codex-pool": _header_safe(umodel),
            "x-switch-codex-route-pool": _header_safe(route_pool),
            "x-switch-codex-active-model": _header_safe(active),
        },
    )
