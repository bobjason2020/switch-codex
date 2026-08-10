"""Anthropic Messages ↔ OpenAI Responses 转换，供 /v1/messages 端点使用。

参考 convert.py 的 ChatSseToResponses 接口模式：请求体与非流式响应做纯函数式
转换，流式场景用带状态机的转换器类（ResponsesSseToAnthropic）逐事件吞入
responses SSE 事件 dict，产出 anthropic SSE 字符串。只依赖标准库
json / time / typing，不引入第三方库。
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

# ---------------------------------------------------------------------------
# SSE 帧
# ---------------------------------------------------------------------------


def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------


def stop_reason_to_anthropic(reason: Optional[str]) -> str:
    """responses stop_reason → anthropic stop_reason。

    end_turn→end_turn，tool_use→tool_use，max_output_tokens→max_tokens，
    refusal 及其它/None → end_turn。
    """
    if reason == "end_turn":
        return "end_turn"
    if reason == "tool_use":
        return "tool_use"
    if reason == "max_output_tokens":
        return "max_tokens"
    return "end_turn"


def anthropic_error_response(status: int, message: str) -> dict:
    """构造 Anthropic 错误体（HTTP 4xx/5xx 用）。

    status 429 → rate_limit_error；400 → invalid_request_error；其余 → api_error。
    """
    if status == 429:
        etype = "rate_limit_error"
    elif status == 400:
        etype = "invalid_request_error"
    else:
        etype = "api_error"
    return {"type": "error", "error": {"type": etype, "message": message}}


def _random_id() -> str:
    """基于时间戳生成 12 位十六进制随机 id（不引入 random 依赖）。"""
    return "%012x" % (int(time.time() * 1_000_000) & 0xFFFFFFFFFFFF)


def _parse_usage(usage: Any) -> tuple:
    """防御性解析 usage，兼容多种结构，全部缺失给 0。返回 (input_tokens, output_tokens)。

    优先级：顶层 input_tokens/output_tokens → *_tokens_details 的
    text_tokens/audio_tokens → token_details → 顶层任一 int 字段兜底。
    """
    def _num(v: Any) -> Optional[int]:
        return v if isinstance(v, int) else None

    if not isinstance(usage, dict):
        return 0, 0
    i = _num(usage.get("input_tokens"))
    o = _num(usage.get("output_tokens"))
    if i is not None or o is not None:
        return i or 0, o or 0

    ind = usage.get("input_tokens_details")
    if isinstance(ind, dict):
        for key in ("text_tokens", "audio_tokens", "input_tokens"):
            v = _num(ind.get(key))
            if v is not None:
                i = v
                break
    oud = usage.get("output_tokens_details")
    if isinstance(oud, dict):
        for key in ("text_tokens", "audio_tokens", "output_tokens"):
            v = _num(oud.get(key))
            if v is not None:
                o = v
                break
    if i is not None or o is not None:
        return i or 0, o or 0

    td = usage.get("token_details")
    if isinstance(td, dict):
        for key in ("input_tokens", "prompt_tokens", "text_tokens"):
            v = _num(td.get(key))
            if v is not None:
                i = v
                break
        for key in ("output_tokens", "completion_tokens"):
            v = _num(td.get(key))
            if v is not None:
                o = v
                break
    if i is not None or o is not None:
        return i or 0, o or 0

    # 不再用“任意前两个 int”兜底：可能把 cache 字段误当 input/output。
    return 0, 0


# ---------------------------------------------------------------------------
# 请求转换：Anthropic Messages body → Responses body
# ---------------------------------------------------------------------------


def _tool_result_output(content: Any) -> str:
    """tool_result.content → function_call_output.output（须为字符串）。

    string 直接用；dict/list 用 json.dumps；content 为 blocks 列表时取
    text 块拼接（非 text 块忽略）；均失败时转为字符串。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts = [
            str(p.get("text"))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        ]
        if parts:
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)
    return "" if content is None else str(content)


def _content_blocks_to_input(role: str, blocks: list) -> list:
    """Anthropic content blocks 列表 → responses input items 列表（顺序保持）。"""
    items: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text")
            if not isinstance(text, str) or not text:
                continue
            items.append(
                {"type": "output_text" if role == "assistant" else "input_text", "text": text}
            )
        elif btype == "tool_use":
            name = block.get("name")
            if not isinstance(name, str):
                name = json.dumps(name, ensure_ascii=False) if name is not None else ""
            x = block.get("input")
            if not isinstance(x, dict):
                x = {"value": x}
            items.append(
                {
                    "type": "function_call",
                    "call_id": block.get("id") or "",
                    "name": name,
                    "arguments": json.dumps(x, ensure_ascii=False),
                }
            )
        elif btype == "tool_result":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id") or "",
                    "output": _tool_result_output(block.get("content")),
                }
            )
        # thinking / redacted_thinking / image 等 block 类型：忽略
    return items


def _anthropic_tool_to_responses(tool: dict) -> Optional[dict]:
    """Anthropic tool → responses function tool。"""
    name = tool.get("name")
    if not name:
        return None
    params = tool.get("input_schema")
    if not isinstance(params, dict):
        params = {}
    params = dict(params)
    if not params.get("type"):
        params["type"] = "object"
    return {
        "type": "function",
        "name": name,
        "description": tool.get("description"),
        "parameters": params,
    }


def _anthropic_tool_choice_to_responses(tool_choice: Any) -> Optional[dict]:
    """Anthropic tool_choice → responses tool_choice。"""
    if not isinstance(tool_choice, dict):
        return None
    tc_type = tool_choice.get("type")
    if tc_type == "tool":
        name = tool_choice.get("name")
        if name:
            return {"type": "function", "name": name}
        return None
    if tc_type == "auto":
        return {"type": "auto"}
    if tc_type == "any":
        return {"type": "required"}
    if tc_type == "none":
        return {"type": "none"}
    return None


def _supports_reasoning_effort(model: str) -> bool:
    """模型是否支持 Responses reasoning.effort（借鉴 cc-switch transform.rs）。"""
    m = (model or "").strip().lower()
    if not m:
        return False
    # o 系列：o1 / o3 / o4-mini 等
    if len(m) > 1 and m[0] == "o" and m[1].isdigit():
        return True
    # GPT-5+：gpt-5 / gpt-5.6-luna 等
    if m.startswith("gpt-") and len(m) > 4 and m[4].isdigit() and int(m[4]) >= 5:
        return True
    # xAI Grok Build
    if m == "grok-4.5" or m.startswith("grok-4.5-") or m.startswith("grok-build-"):
        return True
    # Switchyard 的 DeepSeek 池也走 Responses 风格 reasoning_effort
    if m.startswith("deepseek-"):
        return True
    return False


def _resolve_reasoning_effort(body: dict) -> Optional[str]:
    """Anthropic thinking / output_config / reasoning_effort → 归一化 effort 值。

    返回值即 DeepSeek 官方 effort 语义：none/low/medium/high/xhigh/max
    （none 表示关闭思考模式）。

    优先级（借鉴 cc-switch transform.rs）：
    1. output_config.effort：low/medium/high/xhigh/max 原样归一化；
    2. thinking.adaptive → xhigh；thinking.enabled + budget →
       <4000 low / <16000 medium / 其余 high；无 budget 默认 high；
       thinking 关闭 → none；
    3. 顶层 reasoning_effort（DeepSeek 风格）归一化，max 保持 max。
    """
    oc = body.get("output_config")
    if isinstance(oc, dict):
        effort = oc.get("effort")
        if isinstance(effort, str):
            norm = effort.strip().lower()
            if norm in ("low", "medium", "high", "max", "xhigh"):
                return norm

    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        ttype = thinking.get("type")
        if ttype == "adaptive":
            return "xhigh"
        if ttype == "enabled":
            budget = thinking.get("budget_tokens")
            if isinstance(budget, int):
                if budget < 4000:
                    return "low"
                if budget < 16000:
                    return "medium"
                return "high"
            return "high"
        if ttype == "disabled":
            return "none"

    re = body.get("reasoning_effort")
    if isinstance(re, str):
        norm = re.strip().lower()
        if norm in ("low", "medium", "high", "max", "xhigh"):
            return norm
        if norm == "minimal":
            return "low"
    return None


def anthropic_body_to_responses(body: dict) -> dict:
    """Anthropic Messages 请求体 → OpenAI Responses 请求体。

    返回新 dict，不修改入参。model 原样透传（路由关键），max_tokens →
    max_output_tokens，system → instructions，messages → input。
    """
    out: dict[str, Any] = {}
    if body.get("model"):
        out["model"] = body["model"]
    if body.get("max_tokens") is not None:
        out["max_output_tokens"] = body["max_tokens"]
    for key in ("temperature", "top_p", "stream"):
        if body.get(key) is not None:
            out[key] = body[key]

    # Anthropic thinking → Responses reasoning.effort（仅对支持 reasoning 的模型）
    model_name = body.get("model")
    if model_name and _supports_reasoning_effort(str(model_name)):
        effort = _resolve_reasoning_effort(body)
        if effort:
            out["reasoning"] = {"effort": effort}

    system = body.get("system")
    if isinstance(system, str):
        instructions = system
    elif isinstance(system, list):
        instructions = "\n".join(
            str(p.get("text") or "")
            for p in system
            if isinstance(p, dict) and p.get("type") == "text"
        )
    else:
        instructions = ""
    if instructions and instructions.strip():
        out["instructions"] = instructions

    input_items: list[dict] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        if isinstance(content, str):
            input_items.append({"role": role, "content": content})
        elif isinstance(content, list):
            input_items.extend(_content_blocks_to_input(role, content))
    out["input"] = input_items

    tools = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict):
            converted = _anthropic_tool_to_responses(tool)
            if converted:
                tools.append(converted)
    if tools:
        out["tools"] = tools

    tool_choice = _anthropic_tool_choice_to_responses(body.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice

    return out


# ---------------------------------------------------------------------------
# 非流式响应转换：Responses → Anthropic Messages
# ---------------------------------------------------------------------------


def _reasoning_text(item: dict) -> str:
    """reasoning item → 纯文本（summary[].text 或 content[].text 拼接）。"""
    parts: list[str] = []
    for s in item.get("summary") or []:
        if isinstance(s, dict) and isinstance(s.get("text"), str) and s["text"]:
            parts.append(s["text"])
    if not parts:
        for c in item.get("content") or []:
            if isinstance(c, dict) and isinstance(c.get("text"), str) and c["text"]:
                parts.append(c["text"])
    return "\n".join(parts)


def responses_response_to_anthropic(resp: dict, request_body: dict) -> dict:
    """非流式 OpenAI Responses 响应对象 → Anthropic Messages 响应对象。"""
    resp_id = resp.get("id") or ""
    tail = resp_id[-12:] if resp_id else _random_id()
    model = (request_body or {}).get("model") or resp.get("model") or ""

    content: list[dict] = []
    output = resp.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "message":
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            content.append({"type": "text", "text": text})
            elif itype == "function_call":
                arguments = item.get("arguments") or ""
                try:
                    parsed = json.loads(arguments)
                except (ValueError, TypeError):
                    parsed = None
                tool_input = parsed if isinstance(parsed, dict) else {"raw": arguments}
                content.append(
                    {
                        "type": "tool_use",
                        "id": item.get("call_id") or "",
                        "name": item.get("name") or "",
                        "input": tool_input,
                    }
                )
            elif itype == "reasoning":
                text = _reasoning_text(item)
                if text:
                    content.append({"type": "thinking", "thinking": text})
            # 其它 item 类型：跳过

    in_t, out_t = _parse_usage(resp.get("usage"))
    return {
        "id": "msg_" + tail,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason_to_anthropic(resp.get("stop_reason")),
        "stop_sequence": None,
        "usage": {"input_tokens": in_t, "output_tokens": out_t},
    }


def responses_json_to_anthropic_sse(resp: dict, request_body: dict) -> list[str]:
    """把非流式 Responses JSON 合成完整 Anthropic SSE（借鉴 cc-switch
    streaming_responses.rs 的 responses_json_to_anthropic_sse）。

    用于客户端请求 stream=true、上游却忽略 stream 返回 JSON 的兜底。
    """
    message = responses_response_to_anthropic(resp, request_body)
    usage = message.get("usage") or {}
    start_usage = dict(usage)
    start_usage["output_tokens"] = 0
    events = [
        sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message.get("id") or "",
                    "type": "message",
                    "role": "assistant",
                    "model": message.get("model") or "",
                    "usage": start_usage,
                },
            },
        )
    ]

    for index, block in enumerate(message.get("content") or []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
            text = block.get("text")
            if isinstance(text, str) and text:
                events.append(
                    sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "text_delta", "text": text},
                        },
                    )
                )
            events.append(
                sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
            )
        elif btype == "tool_use":
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": block.get("id") or "",
                            "name": block.get("name") or "",
                            "input": {},
                        },
                    },
                )
            )
            tool_input = block.get("input") or {}
            events.append(
                sse_event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(tool_input, ensure_ascii=False),
                        },
                    },
                )
            )
            events.append(
                sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
            )
        elif btype == "thinking":
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "thinking", "thinking": ""},
                    },
                )
            )
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking:
                events.append(
                    sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "thinking_delta", "thinking": thinking},
                        },
                    )
                )
            signature = block.get("signature")
            if isinstance(signature, str) and signature:
                events.append(
                    sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "signature_delta", "signature": signature},
                        },
                    )
                )
            events.append(
                sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
            )
        elif btype == "redacted_thinking":
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": block,
                    },
                )
            )
            events.append(
                sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
            )

    events.append(
        sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": message.get("stop_reason") or "end_turn"},
                "usage": {"output_tokens": usage.get("output_tokens", 0)},
            },
        )
    )
    events.append(sse_event("message_stop", {"type": "message_stop"}))
    return events


# ---------------------------------------------------------------------------
# 流式响应转换：Responses SSE → Anthropic SSE
# ---------------------------------------------------------------------------


class ResponsesSseToAnthropic:
    """流式转换器：逐事件吞入已解析的 responses SSE 事件 dict，产出 anthropic SSE 字符串。

    接口对齐 convert.py 的 ChatSseToResponses：handle_chunk / finalize /
    is_completed / failed_event / latest_usage。注意 handle_chunk 收到的是
    SSE 事件的 data 对象（{"type": "...", ...}），与 ChatSseToResponses
    吞 chat chunk 不同。
    """

    def __init__(self) -> None:
        self.model = ""
        self.completed = False
        self.latest_usage: Optional[dict] = None
        self.last_tool_arguments: dict[str, str] = {}
        self._message_started = False
        self._message_id = ""
        self.message_id = ""
        self._next_block_index = 0
        self._text_index = -1
        self._thinking_index = -1
        self._last_tool_index = -1
        self._tool_index_by_call: dict[str, int] = {}
        self._item_index: dict[str, int] = {}
        self._tool_args: dict[str, str] = {}
        self._input_tokens = 0
        self._output_chars = 0

    # -- 内部状态机 ------------------------------------------------

    def _ensure_message_start(self) -> list[str]:
        """首个响应相关事件触发 message_start，保证只发一次。"""
        if self._message_started:
            return []
        self._message_started = True
        if not self._message_id:
            self._message_id = "msg_" + _random_id()
        self.message_id = self._message_id
        return [
            sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self._message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            )
        ]

    def _on_start(self, chunk: dict) -> list[str]:
        """response.created / response.in_progress：回填 model 与输入 token 计数。"""
        resp = chunk.get("response")
        if isinstance(resp, dict):
            resp_id = resp.get("id")
            if isinstance(resp_id, str) and resp_id:
                # 用上游 response id 派生稳定的 Anthropic message id，便于日志归组。
                self._message_id = "msg_" + resp_id[-12:]
                self.message_id = self._message_id
            if not self.model and resp.get("model"):
                self.model = resp["model"]
            usage = resp.get("usage")
            if isinstance(usage, dict):
                in_t, _ = _parse_usage(usage)
                if in_t:
                    self._input_tokens = in_t
        events = self._ensure_message_start()
        return events

    def _on_item_added(self, chunk: dict) -> list[str]:
        """output_item.added → content_block_start（text / tool_use / thinking）。"""
        events = self._ensure_message_start()
        item = chunk.get("item")
        if not isinstance(item, dict):
            return events
        itype = item.get("type")
        index = self._next_block_index
        self._next_block_index += 1
        item_id = item.get("id")
        if item_id:
            self._item_index[item_id] = index
        if itype == "message":
            self._text_index = index
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        elif itype == "function_call":
            call_id = item.get("call_id") or item_id or ""
            self._tool_index_by_call[call_id] = index
            self._last_tool_index = index
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": item.get("call_id") or "",
                            "name": item.get("name") or "",
                            "input": {},
                        },
                    },
                )
            )
        elif itype == "reasoning":
            self._thinking_index = index
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "thinking", "thinking": ""},
                    },
                )
            )
        return events

    def _on_text_delta(self, chunk: dict) -> list[str]:
        """content_part.delta → text_delta；无打开文本块时先补 content_block_start。"""
        events = self._ensure_message_start()
        delta = chunk.get("delta")
        text = ""
        if isinstance(delta, str):
            # 兼容上游直接下推纯字符串 delta 的简化格式
            text = delta
        elif isinstance(delta, dict):
            text = delta.get("text")
            if text is None:
                inner = delta.get("delta")
                if isinstance(inner, dict):
                    text = inner.get("text")
        if not isinstance(text, str) or not text:
            return events
        if self._text_index < 0:
            index = self._next_block_index
            self._next_block_index += 1
            self._text_index = index
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        self._output_chars += len(text)
        events.append(
            sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._text_index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return events

    def _on_tool_args_delta(self, chunk: dict) -> list[str]:
        """function_call_arguments.delta → input_json_delta，累积 arguments 缓冲。"""
        events = self._ensure_message_start()
        delta = chunk.get("delta")
        if isinstance(delta, dict):
            delta = delta.get("delta") or delta.get("partial_json")
        if not isinstance(delta, str) or not delta:
            return events
        call_id = chunk.get("call_id") or ""
        index = None
        if call_id and call_id in self._tool_index_by_call:
            index = self._tool_index_by_call[call_id]
        item_id = chunk.get("item_id")
        if index is None and item_id and item_id in self._item_index:
            index = self._item_index[item_id]
        if index is None:
            index = self._last_tool_index
        if index is None or index < 0:
            index = self._next_block_index
            self._next_block_index += 1
            self._last_tool_index = index
            if call_id:
                self._tool_index_by_call[call_id] = index
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": call_id or "call_%d" % index,
                            "name": chunk.get("name") or "unknown",
                            "input": {},
                        },
                    },
                )
            )
        self._tool_args[call_id] = self._tool_args.get(call_id, "") + delta
        self.last_tool_arguments[call_id] = self._tool_args[call_id]
        events.append(
            sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": delta},
                },
            )
        )
        return events

    def _on_reasoning_delta(self, chunk: dict) -> list[str]:
        """reasoning_summary_text.delta → thinking_delta。"""
        events = self._ensure_message_start()
        delta = chunk.get("delta")
        if isinstance(delta, dict):
            delta = delta.get("text")
        if not isinstance(delta, str) or not delta:
            return events
        if self._thinking_index < 0:
            index = self._next_block_index
            self._next_block_index += 1
            self._thinking_index = index
            events.append(
                sse_event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "thinking", "thinking": ""},
                    },
                )
            )
        events.append(
            sse_event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._thinking_index,
                    "delta": {"type": "thinking_delta", "thinking": delta},
                },
            )
        )
        return events

    def _on_thinking_done(self, chunk: dict) -> list[str]:
        """reasoning_text.done / content_part.done(reasoning) → 结束 thinking 块（幂等）。"""
        events = self._ensure_message_start()
        if self._thinking_index >= 0:
            events.append(
                sse_event(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": self._thinking_index},
                )
            )
            self._thinking_index = -1
        return events

    def _on_item_done(self, chunk: dict) -> list[str]:
        """output_item.done → content_block_stop（对应已记录的 index）。"""
        events = self._ensure_message_start()
        item = chunk.get("item")
        if not isinstance(item, dict):
            return events
        itype = item.get("type")
        item_id = item.get("id")
        index = self._item_index.get(item_id, -1) if item_id else -1
        if itype == "message":
            if index < 0:
                index = self._text_index
            if index >= 0:
                events.append(
                    sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
                )
        elif itype == "function_call":
            call_id = item.get("call_id") or ""
            if index < 0:
                index = self._tool_index_by_call.get(call_id, self._last_tool_index)
            self.last_tool_arguments[call_id] = self._tool_args.get(
                call_id, item.get("arguments") or ""
            )
            if index >= 0:
                events.append(
                    sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
                )
        elif itype == "reasoning":
            if index < 0:
                index = self._thinking_index
            if index >= 0:
                events.append(
                    sse_event("content_block_stop", {"type": "content_block_stop", "index": index})
                )
        return events

    def _on_completed(self, chunk: dict) -> list[str]:
        """response.completed → message_delta + message_stop，记录 latest_usage。"""
        events = self._ensure_message_start()
        resp = chunk.get("response")
        if not isinstance(resp, dict):
            resp = chunk
        stop = stop_reason_to_anthropic(resp.get("stop_reason") or chunk.get("stop_reason"))
        usage = resp.get("usage")
        if not isinstance(usage, dict):
            usage = chunk.get("usage")
        in_t, out_t = _parse_usage(usage)
        if not isinstance(usage, dict):
            usage = {}
            in_t = self._input_tokens
            if not out_t:
                out_t = self._output_chars
        # 保留完整 usage（含 cache_creation/cache_read 等字段），供日志解析缓存/思考 token。
        self.latest_usage = {
            "input_tokens": in_t,
            "output_tokens": out_t,
            **{
                k: v
                for k, v in usage.items()
                if k
                in ("cache_creation_input_tokens", "cache_read_input_tokens", "reasoning_tokens")
                and v is not None
            },
        }
        events.append(
            sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop},
                    "usage": {"output_tokens": out_t},
                },
            )
        )
        events.append(sse_event("message_stop", {"type": "message_stop"}))
        self.completed = True
        return events

    def _on_error(self, chunk: dict) -> list[str]:
        """上游 error 事件 → anthropic error SSE，后续事件忽略。"""
        err = chunk.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("type") or "上游响应错误"
        else:
            message = "上游响应错误"
        return [self.failed_event(str(message))]

    def _on_failed(self, chunk: dict) -> list[str]:
        """response.failed → anthropic error SSE（message 取 error.message 或 code）。"""
        err = chunk.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("code") or err.get("type")
        else:
            message = chunk.get("message") or chunk.get("code")
        if not message:
            message = "上游请求失败"
        return [self.failed_event(str(message))]

    # -- 对外接口 ------------------------------------------------

    def handle_chunk(self, chunk: dict) -> list[str]:
        """吞一个 responses SSE 事件的 data 对象，返回应下发的 anthropic SSE 字符串列表。"""
        if not isinstance(chunk, dict) or not chunk.get("type"):
            return []
        if self.completed:
            return []
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            in_t, _ = _parse_usage(usage)
            if in_t:
                self._input_tokens = in_t
        etype = chunk["type"]
        if etype in ("response.created", "response.in_progress"):
            return self._on_start(chunk)
        if etype == "response.output_item.added":
            return self._on_item_added(chunk)
        if etype == "response.content_part.delta":
            return self._on_text_delta(chunk)
        if etype == "response.output_text.delta":
            # DeepSeek / opencode.ai 自定义正文增量事件（delta 为字符串）
            return self._on_text_delta(chunk)
        if etype == "response.function_call_arguments.delta":
            return self._on_tool_args_delta(chunk)
        if etype == "response.reasoning_summary_text.delta":
            return self._on_reasoning_delta(chunk)
        if etype == "response.reasoning_text.delta":
            # DeepSeek / opencode.ai 思考增量事件（delta 为字符串）
            return self._on_reasoning_delta(chunk)
        if etype == "response.reasoning_text.done":
            return self._on_thinking_done(chunk)
        if etype == "response.content_part.done":
            part = chunk.get("part")
            if isinstance(part, dict) and part.get("type") == "reasoning_text":
                return self._on_thinking_done(chunk)
            return []
        if etype == "response.output_item.done":
            return self._on_item_done(chunk)
        if etype == "response.completed":
            return self._on_completed(chunk)
        if etype == "error":
            return self._on_error(chunk)
        if etype == "response.failed":
            return self._on_failed(chunk)
        return []

    def finalize(self) -> list[str]:
        """流结束补齐剩余事件：message_start（若没发过）+ message_delta + message_stop。"""
        if self.completed:
            return []
        events = self._ensure_message_start()
        events.append(
            sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": self._output_chars},
                },
            )
        )
        events.append(sse_event("message_stop", {"type": "message_stop"}))
        self.completed = True
        return events

    def is_completed(self) -> bool:
        """是否已见过 response.completed / error / failed（或已 finalize）。"""
        return self.completed

    def failed_event(self, message: str, error_type: Optional[str] = None) -> str:
        """产出一个 anthropic error SSE 字符串（event: error），并标记完成。"""
        self.completed = True
        etype = error_type or "api_error"
        return sse_event("error", {"type": "error", "error": {"type": etype, "message": message}})
