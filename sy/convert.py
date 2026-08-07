"""Responses ↔ Chat Completions 格式转换（chat_completions 模式上游专用）。

参考 cc-switch 的 transform_codex_chat / streaming_codex_chat / codex_responses_sse
实现思路，按本项目的实际需求裁剪：只支持标准 function tool 与 message/reasoning
item，不涉及 custom_tool_call / tool_search / namespace 等 Codex 扩展格式。
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
# 请求转换：Responses body → Chat Completions body
# ---------------------------------------------------------------------------


def _normalize_parameters(params: Any) -> dict:
    if isinstance(params, dict):
        if params.get("type") == "object":
            return params
        out = dict(params)
        out["type"] = "object"
        return out
    return {"type": "object", "properties": {}}


def _responses_tool_to_chat(tool: dict) -> Optional[dict]:
    if tool.get("type") != "function":
        return None
    fn = tool.get("function")
    if isinstance(fn, dict):
        out = {"type": "function", "function": dict(fn)}
        out["function"]["name"] = fn.get("name") or tool.get("name")
        if not out["function"].get("name"):
            return None
        out["function"]["parameters"] = _normalize_parameters(fn.get("parameters"))
        if tool.get("strict") is not None and "strict" not in out["function"]:
            out["function"]["strict"] = tool["strict"]
        return out
    name = tool.get("name")
    if not name:
        return None
    out = {
        "type": "function",
        "function": {
            "name": name,
            "description": tool.get("description"),
            "parameters": _normalize_parameters(tool.get("parameters")),
        },
    }
    if tool.get("strict") is not None:
        out["function"]["strict"] = tool["strict"]
    return out


def _content_to_chat(role: str, content: Any) -> Any:
    if content is None or isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            text = part.get("text")
            if text:
                parts.append({"type": "text", "text": text})
        elif ptype == "refusal":
            text = part.get("refusal")
            if text:
                parts.append({"type": "text", "text": text})
        elif ptype == "input_image":
            img = part.get("image_url")
            if isinstance(img, dict):
                parts.append({"type": "image_url", "image_url": img})
            elif isinstance(img, str):
                parts.append({"type": "image_url", "image_url": {"url": img}})
        elif ptype == "input_audio" and isinstance(part.get("input_audio"), dict):
            parts.append({"type": "input_audio", "input_audio": part["input_audio"]})
    texts = [p["text"] for p in parts if p.get("type") == "text"]
    non_text = [p for p in parts if p.get("type") != "text"]
    if not non_text:
        return "\n".join(texts)
    return parts


def _responses_role_to_chat(role: str) -> str:
    if role in ("system", "developer"):
        return "system"
    if role == "assistant":
        return "assistant"
    if role == "tool":
        return "tool"
    return "user"


def _tool_call_to_chat(item: dict) -> dict:
    return {
        "id": item.get("call_id", ""),
        "type": "function",
        "function": {
            "name": item.get("name", ""),
            "arguments": item.get("arguments") or "",
        },
    }


def _tool_output_to_chat(item: dict) -> dict:
    output = item.get("output")
    if isinstance(output, (dict, list)):
        output = json.dumps(output, ensure_ascii=False)
    elif output is None:
        output = ""
    return {
        "role": "tool",
        "tool_call_id": item.get("call_id", ""),
        "content": str(output),
    }


def _reasoning_text(item: dict) -> str:
    parts: list[str] = []
    for s in item.get("summary") or []:
        if isinstance(s, dict):
            parts.append(str(s.get("text") or ""))
    if not parts:
        for c in item.get("content") or []:
            if isinstance(c, dict) and c.get("text"):
                parts.append(str(c["text"]))
    return "\n".join(p for p in parts if p)


def _message_has_content(msg: dict) -> bool:
    content = msg.get("content")
    if content is None:
        return False
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return bool(content)
    return True


def _append_reasoning_to_message(msg: dict, reasoning: str) -> None:
    if not reasoning:
        return
    existing = msg.get("reasoning_content") or ""
    msg["reasoning_content"] = f"{existing}\n{reasoning}" if existing else reasoning
    if not _message_has_content(msg):
        msg["content"] = ""


def _flush_tool_calls(messages: list, pending_tool_calls: list, pending_reasoning: list) -> None:
    if not pending_tool_calls:
        return
    tool_calls = list(pending_tool_calls)
    pending_tool_calls.clear()
    if messages and messages[-1].get("role") == "assistant":
        existing = messages[-1].get("tool_calls")
        if isinstance(existing, list):
            known = {tc.get("id") for tc in existing}
            for tc in tool_calls:
                if not tc.get("id") or tc["id"] not in known:
                    existing.append(tc)
        else:
            messages[-1]["tool_calls"] = tool_calls
        if pending_reasoning:
            _append_reasoning_to_message(
                messages[-1], "\n".join(pending_reasoning)
            )
            pending_reasoning.clear()
        return
    msg: dict[str, Any] = {"role": "assistant", "content": "", "tool_calls": tool_calls}
    if pending_reasoning:
        msg["reasoning_content"] = "\n".join(pending_reasoning)
        pending_reasoning.clear()
    messages.append(msg)


def _flush_reasoning(messages: list, pending_reasoning: list) -> None:
    if not pending_reasoning:
        return
    reasoning = "\n".join(pending_reasoning)
    pending_reasoning.clear()
    if messages and messages[-1].get("role") == "assistant":
        _append_reasoning_to_message(messages[-1], reasoning)
        return
    messages.append({"role": "assistant", "content": "", "reasoning_content": reasoning})


def _append_input_items(input_value: Any, messages: list) -> None:
    pending_tool_calls: list[dict] = []
    pending_reasoning: list[str] = []
    seen_tool_call_ids: set[str] = set()

    def flush_tool_calls() -> None:
        _flush_tool_calls(messages, pending_tool_calls, pending_reasoning)

    def handle_item(item: Any) -> None:
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id") or ""
            if call_id:
                seen_tool_call_ids.add(call_id)
            pending_tool_calls.append(_tool_call_to_chat(item))
            return
        if item_type in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
            flush_tool_calls()
            call_id = item.get("call_id") or ""
            if call_id and call_id not in seen_tool_call_ids:
                output = item.get("output")
                if isinstance(output, (dict, list)):
                    output = json.dumps(output, ensure_ascii=False)
                messages.append(
                    {
                        "role": "user",
                        "content": f"Function call output ({call_id}): {output or ''}",
                    }
                )
                return
            messages.append(_tool_output_to_chat(item))
            return
        if item_type == "reasoning":
            text = _reasoning_text(item)
            if text:
                pending_reasoning.append(text)
            return
        if item_type in ("input_text", "input_image", "input_file", "input_audio", "output_text"):
            flush_tool_calls()
            role = _responses_role_to_chat(
                item.get("role") or ("assistant" if item_type == "output_text" else "user")
            )
            msg: dict[str, Any] = {
                "role": role,
                "content": _content_to_chat(role, [item]),
            }
            if role == "assistant":
                if pending_reasoning:
                    _append_reasoning_to_message(msg, "\n".join(pending_reasoning))
                    pending_reasoning.clear()
                if not _message_has_content(msg) and not msg.get("tool_calls"):
                    msg["content"] = ""
            else:
                if pending_reasoning:
                    _flush_reasoning(messages, pending_reasoning)
            messages.append(msg)
            return
        if item.get("role") is not None or item.get("content") is not None:
            flush_tool_calls()
            role = _responses_role_to_chat(item.get("role") or "user")
            msg = {
                "role": role,
                "content": _content_to_chat(role, item.get("content")),
            }
            if role == "assistant":
                if pending_reasoning:
                    _append_reasoning_to_message(msg, "\n".join(pending_reasoning))
                    pending_reasoning.clear()
                if not _message_has_content(msg) and not msg.get("tool_calls"):
                    msg["content"] = ""
            else:
                if pending_reasoning:
                    _flush_reasoning(messages, pending_reasoning)
            messages.append(msg)
            return

    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        for item in input_value:
            handle_item(item)
    else:
        handle_item(input_value)
    _flush_tool_calls(messages, pending_tool_calls, pending_reasoning)


def _normalize_chat_messages(messages: list) -> None:
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        has_content = _message_has_content(msg)
        has_tool_calls = bool(msg.get("tool_calls"))
        if not has_content and not has_tool_calls:
            msg["content"] = ""


def _collapse_system_messages_to_head(messages: list) -> list:
    system_chunks: list[str] = []
    rest: list[dict] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                system_chunks.append(content.strip())
            continue
        rest.append(msg)
    if not system_chunks:
        return rest
    return [{"role": "system", "content": "\n\n".join(system_chunks)}] + rest


def _tool_choice_to_chat(tool_choice: Any) -> Any:
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        fn = tool_choice.get("function")
        if isinstance(fn, dict):
            return {"type": "function", "function": fn}
        name = tool_choice.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return tool_choice


def responses_body_to_chat(body: dict) -> dict:
    out: dict[str, Any] = {}
    if body.get("model"):
        out["model"] = body["model"]

    messages: list[dict] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    elif isinstance(instructions, list):
        text = "".join(
            p.get("text", "")
            for p in instructions
            if isinstance(p, dict) and p.get("type") == "input_text"
        )
        if text.strip():
            messages.append({"role": "system", "content": text})

    if "input" in body:
        _append_input_items(body["input"], messages)
    _normalize_chat_messages(messages)
    out["messages"] = _collapse_system_messages_to_head(messages)

    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    elif body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    elif body.get("max_completion_tokens") is not None:
        out["max_completion_tokens"] = body["max_completion_tokens"]
    for key in ("temperature", "top_p", "stream"):
        if body.get(key) is not None:
            out[key] = body[key]

    reasoning = body.get("reasoning")
    effort = None
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
    elif isinstance(reasoning, str):
        effort = reasoning
    if effort is None:
        effort = body.get("reasoning_effort")
    if effort:
        out["reasoning_effort"] = effort

    tools = []
    for tool in body.get("tools") or []:
        if isinstance(tool, dict):
            converted = _responses_tool_to_chat(tool)
            if converted:
                tools.append(converted)
    if tools:
        out["tools"] = tools

    if body.get("tool_choice") is not None:
        out["tool_choice"] = _tool_choice_to_chat(body["tool_choice"])
    if body.get("parallel_tool_calls") is not None:
        out["parallel_tool_calls"] = body["parallel_tool_calls"]

    if not tools:
        out.pop("tool_choice", None)
        out.pop("parallel_tool_calls", None)

    if out.get("stream"):
        stream_options = body.get("stream_options") or {}
        if not stream_options.get("include_usage"):
            stream_options = dict(stream_options)
            stream_options["include_usage"] = True
        out["stream_options"] = stream_options

    return out


# ---------------------------------------------------------------------------
# usage 转换
# ---------------------------------------------------------------------------


def chat_usage_to_responses(usage: Any) -> dict:
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
        }
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
    total = usage.get("total_tokens", (input_tokens or 0) + (output_tokens or 0))
    result = {
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "total_tokens": total or 0,
    }
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    cached = 0
    if isinstance(details, dict):
        cached = details.get("cached_tokens", 0)
    cached = cached or usage.get("cache_read_input_tokens", 0) or 0
    cached = cached or usage.get("cachedContentTokenCount", 0) or 0
    if cached:
        result["input_tokens_details"] = {"cached_tokens": cached}
    out_details = usage.get("completion_tokens_details")
    if isinstance(out_details, dict):
        out_details = dict(out_details)
        out_details.setdefault("reasoning_tokens", 0)
        result["output_tokens_details"] = out_details
    else:
        result["output_tokens_details"] = {"reasoning_tokens": 0}
    return result


# ---------------------------------------------------------------------------
# 非流式响应转换：Chat Completions → Responses
# ---------------------------------------------------------------------------


def _status_from_finish_reason(finish_reason: Optional[str]) -> str:
    if finish_reason == "length":
        return "incomplete"
    return "completed"


def _text_message_item(item_id: str, text: str) -> dict:
    return {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _reasoning_item(item_id: str, text: str) -> dict:
    return {
        "id": item_id,
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text}],
    }


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _leading_think_decision(buffer: str) -> str:
    trimmed = buffer.lstrip()
    if not trimmed:
        return "need_more"
    if trimmed.startswith(_THINK_OPEN):
        return "reasoning"
    if _THINK_OPEN.startswith(trimmed):
        return "need_more"
    return "text"


def _split_leading_think_block(text: str):
    """Split a leading <think>...</think> block from assistant content.

    Returns (reasoning, answer) or None when there is no leading think block.
    """
    stripped = text.lstrip()
    if not stripped.startswith(_THINK_OPEN):
        return None
    body_start = stripped.find(">") + 1
    close_start = stripped.find(_THINK_CLOSE, body_start)
    if close_start < 0:
        return None
    reasoning = stripped[body_start:close_start].strip()
    answer = stripped[close_start + len(_THINK_CLOSE):].strip()
    return (reasoning, answer)


def _function_call_item(item_id: str, call_id: str, name: str, arguments: str) -> dict:
    return {
        "id": item_id,
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def chat_response_to_responses(chat: dict) -> dict:
    chat_id = chat.get("id") or "switch-codex"
    response_id = chat_id if chat_id.startswith("resp_") else f"resp_{chat_id}"
    created_at = chat.get("created") or int(time.time())
    model = chat.get("model") or ""
    usage = chat_usage_to_responses(chat.get("usage"))

    output: list[dict] = []
    for choice in chat.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        reasoning_text = message.get("reasoning_content")
        if isinstance(reasoning_text, str) and reasoning_text:
            output.append(_reasoning_item(f"rs_{response_id}", reasoning_text))
        text = message.get("content")
        if isinstance(text, str) and text:
            think = _split_leading_think_block(text)
            if think is not None:
                reasoning, answer = think
                if reasoning and not reasoning_text:
                    output.append(_reasoning_item(f"rs_{response_id}", reasoning))
                if answer:
                    output.append(_text_message_item(f"{response_id}_msg", answer))
            else:
                output.append(_text_message_item(f"{response_id}_msg", text))
        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            output.append(
                _function_call_item(
                    f"fc_{tc.get('id') or 'call'}",
                    tc.get("id") or "call",
                    fn.get("name") or "",
                    fn.get("arguments") or "",
                )
            )

    finish_reason = None
    for choice in chat.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason"):
            finish_reason = choice.get("finish_reason")
            break
    status = _status_from_finish_reason(finish_reason)
    response = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "model": model,
        "output": output,
        "usage": usage,
    }
    if status == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    return response


# ---------------------------------------------------------------------------
# 流式响应转换：Chat SSE → Responses SSE
# ---------------------------------------------------------------------------


class _ItemState:
    __slots__ = ("added", "done", "output_index", "item_id", "text")

    def __init__(self) -> None:
        self.added = False
        self.done = False
        self.output_index = 0
        self.item_id = ""
        self.text = ""


class _ToolState:
    __slots__ = ("added", "done", "output_index", "item_id", "call_id", "name", "arguments")

    def __init__(self) -> None:
        self.added = False
        self.done = False
        self.output_index = 0
        self.item_id = ""
        self.call_id = ""
        self.name = ""
        self.arguments = ""


class ChatSseToResponses:
    """把 Chat Completions SSE chunk 转成 Responses SSE 事件字符串序列。"""

    def __init__(self) -> None:
        self.response_id = "resp_switch-codex"
        self.created_at = int(time.time())
        self.model = ""
        self.started = False
        self.completed = False
        self.finish_reason: Optional[str] = None
        self.latest_usage: Optional[dict] = None
        self.text = _ItemState()
        self.reasoning = _ItemState()
        self.tools: dict[int, _ToolState] = {}
        self.next_tool_key = 0
        self.next_output_index = 0
        self.output_items: list[dict] = []
        self.dropped_tool_calls = 0
        self.content_buffer = ""
        self.in_think = False
        self.think_buffer = ""

    # -- helpers ---------------------------------------------------------

    def _next_output_index(self) -> int:
        idx = self.next_output_index
        self.next_output_index += 1
        return idx

    def _base_response(self, status: str, output: list) -> dict:
        return {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": output,
            "usage": self.latest_usage
            or {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }

    def _ensure_started(self) -> list[str]:
        if self.started:
            return []
        self.started = True
        response = self._base_response("in_progress", [])
        return [
            sse_event("response.created", {"type": "response.created", "response": response}),
            sse_event("response.in_progress", {"type": "response.in_progress", "response": response}),
        ]

    def _push_reasoning_delta(self, delta: str) -> list[str]:
        events = []
        if not self.reasoning.added:
            idx = self._next_output_index()
            item_id = f"rs_{self.response_id}"
            self.reasoning.output_index = idx
            self.reasoning.item_id = item_id
            self.reasoning.added = True
            events.append(
                sse_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": idx,
                        "item": {
                            "id": item_id,
                            "type": "reasoning",
                            "status": "in_progress",
                            "summary": [],
                        },
                    },
                )
            )
            events.append(
                sse_event(
                    "response.reasoning_summary_part.added",
                    {
                        "type": "response.reasoning_summary_part.added",
                        "item_id": item_id,
                        "output_index": idx,
                        "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""},
                    },
                )
            )
        self.reasoning.text += delta
        events.append(
            sse_event(
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": self.reasoning.item_id,
                    "output_index": self.reasoning.output_index,
                    "summary_index": 0,
                    "delta": delta,
                },
            )
        )
        return events

    def _push_text_delta(self, delta: str) -> list[str]:
        events = []
        if not self.text.added:
            idx = self._next_output_index()
            item_id = f"{self.response_id}_msg"
            self.text.output_index = idx
            self.text.item_id = item_id
            self.text.added = True
            events.append(
                sse_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": idx,
                        "item": {
                            "id": item_id,
                            "type": "message",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                )
            )
            events.append(
                sse_event(
                    "response.content_part.added",
                    {
                        "type": "response.content_part.added",
                        "item_id": item_id,
                        "output_index": idx,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "", "annotations": []},
                    },
                )
            )
        self.text.text += delta
        events.append(
            sse_event(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": self.text.item_id,
                    "output_index": self.text.output_index,
                    "content_index": 0,
                    "delta": delta,
                },
            )
        )
        return events

    def _flush_ready_tool_calls(self) -> list[str]:
        events = []
        while True:
            state = self.tools.get(self.next_tool_key)
            if state is None:
                break
            if state.added or state.done:
                self.next_tool_key += 1
                continue
            if not state.call_id or not state.name:
                break
            idx = self._next_output_index()
            state.added = True
            state.output_index = idx
            state.item_id = f"fc_{state.call_id}"
            events.append(
                sse_event(
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "output_index": idx,
                        "item": {
                            "id": state.item_id,
                            "type": "function_call",
                            "status": "in_progress",
                            "call_id": state.call_id,
                            "name": state.name,
                            "arguments": "",
                        },
                    },
                )
            )
            if state.arguments:
                events.append(
                    sse_event(
                        "response.function_call_arguments.delta",
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": state.item_id,
                            "output_index": idx,
                            "delta": state.arguments,
                        },
                    )
                )
            self.next_tool_key += 1
        return events

    def _push_tool_call_delta(self, tool_call: dict) -> list[str]:
        index = tool_call.get("index")
        if isinstance(index, int):
            key = index
        else:
            key = len(self.tools)
        state = self.tools.setdefault(key, _ToolState())
        args_delta = ""
        call_id = tool_call.get("id")
        if isinstance(call_id, str) and call_id:
            state.call_id = call_id
        fn = tool_call.get("function") or {}
        name = fn.get("name")
        if isinstance(name, str) and name:
            state.name = name
        args = fn.get("arguments")
        if isinstance(args, str) and args:
            state.arguments += args
            args_delta = args
        events = self._flush_ready_tool_calls()
        if args_delta and state.added:
            events.append(
                sse_event(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": state.item_id,
                        "output_index": state.output_index,
                        "delta": args_delta,
                    },
                )
            )
        return events

    def _finalize_reasoning(self) -> list[str]:
        if not self.reasoning.added or self.reasoning.done:
            return []
        idx = self.reasoning.output_index
        item_id = self.reasoning.item_id
        text = self.reasoning.text
        item = _reasoning_item(item_id, text)
        events = [
            sse_event(
                "response.reasoning_summary_text.done",
                {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": item_id,
                    "output_index": idx,
                    "summary_index": 0,
                    "text": text,
                },
            ),
            sse_event(
                "response.reasoning_summary_part.done",
                {
                    "type": "response.reasoning_summary_part.done",
                    "item_id": item_id,
                    "output_index": idx,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": text},
                },
            ),
            sse_event(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": idx, "item": item},
            ),
        ]
        self.output_items.append(item)
        self.reasoning.done = True
        return events

    def _finalize_text(self) -> list[str]:
        if not self.text.added or self.text.done:
            return []
        idx = self.text.output_index
        item_id = self.text.item_id
        text = self.text.text
        item = _text_message_item(item_id, text)
        events = [
            sse_event(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": idx,
                    "content_index": 0,
                    "text": text,
                },
            ),
            sse_event(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": idx,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": text, "annotations": []},
                },
            ),
            sse_event(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": idx, "item": item},
            ),
        ]
        self.output_items.append(item)
        self.text.done = True
        return events

    def _finalize_tools(self) -> list[str]:
        events = []
        for key in sorted(self.tools):
            state = self.tools[key]
            if state.done:
                continue
            if not state.name:
                self.dropped_tool_calls += 1
                state.done = True
                continue
            if not state.added:
                idx = self._next_output_index()
                state.added = True
                state.output_index = idx
                state.item_id = f"fc_{state.call_id or f'call_{key}'}"
                events.append(
                    sse_event(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": idx,
                            "item": {
                                "id": state.item_id,
                                "type": "function_call",
                                "status": "in_progress",
                                "call_id": state.call_id or f"call_{key}",
                                "name": state.name,
                                "arguments": "",
                            },
                        },
                    )
                )
            idx = state.output_index
            item = _function_call_item(
                state.item_id,
                state.call_id or f"call_{key}",
                state.name,
                state.arguments,
            )
            events.append(
                sse_event(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": state.item_id,
                        "output_index": idx,
                        "arguments": state.arguments,
                    },
                )
            )
            events.append(
                sse_event(
                    "response.output_item.done",
                    {"type": "response.output_item.done", "output_index": idx, "item": item},
                )
            )
            self.output_items.append(item)
            state.done = True
        return events

    def _handle_content_stream(self, delta: str) -> list[str]:
        """Feed a content delta, routing <think> blocks to reasoning items."""
        events: list[str] = []
        if self.in_think:
            close_idx = delta.find(_THINK_CLOSE)
            if close_idx < 0:
                self.think_buffer += delta
                return events
            self.think_buffer += delta[:close_idx]
            if self.think_buffer.strip():
                events.extend(self._push_reasoning_delta(self.think_buffer))
            self.think_buffer = ""
            self.in_think = False
            rest = delta[close_idx + len(_THINK_CLOSE):]
            if rest:
                events.extend(self._handle_content_stream(rest))
            return events

        combined = self.content_buffer + delta
        decision = _leading_think_decision(combined)
        if decision == "need_more":
            self.content_buffer = combined
            return events
        self.content_buffer = ""
        if decision == "text":
            events.extend(self._push_text_delta(combined))
            return events
        body = combined[combined.find(">") + 1:]
        close_idx = body.find(_THINK_CLOSE)
        if close_idx >= 0:
            think_text = body[:close_idx]
            if think_text.strip():
                events.extend(self._push_reasoning_delta(think_text))
            rest = body[close_idx + len(_THINK_CLOSE):]
            if rest:
                events.extend(self._push_text_delta(rest))
            return events
        self.in_think = True
        self.think_buffer = body
        return events

    def _flush_pending_think(self) -> list[str]:
        if self.in_think and self.think_buffer.strip():
            events = self._push_reasoning_delta(self.think_buffer)
            self.think_buffer = ""
            self.in_think = False
            return events
        return []

    def _has_substantive_output(self) -> bool:
        return bool(
            self.text.text.strip()
            or self.reasoning.text.strip()
            or self.tools
        )

    def is_completed(self) -> bool:
        return self.completed

    # -- main API --------------------------------------------------------

    def failed_event(self, message: str, error_type: Optional[str] = None) -> str:
        self.completed = True
        error: dict = {"message": message}
        if error_type:
            error["type"] = error_type
        response = self._base_response("failed", list(self.output_items))
        response["error"] = error
        return sse_event("response.failed", {"type": "response.failed", "response": response})

    def handle_chunk(self, chunk: dict) -> list[str]:
        if chunk.get("id") and isinstance(chunk["id"], str):
            cid = chunk["id"]
            self.response_id = cid if cid.startswith("resp_") else f"resp_{cid}"
        if chunk.get("model"):
            self.model = chunk["model"]
        if isinstance(chunk.get("created"), int):
            self.created_at = chunk["created"]
        events = self._ensure_started()
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage:
            self.latest_usage = chat_usage_to_responses(usage)
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return events
        choice = choices[0]
        if not isinstance(choice, dict):
            return events
        delta = choice.get("delta")
        if isinstance(delta, dict):
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                events.extend(self._push_reasoning_delta(reasoning))
            content = delta.get("content")
            if isinstance(content, str) and content:
                events.extend(self._handle_content_stream(content))
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        events.extend(self._push_tool_call_delta(tc))
        finish = choice.get("finish_reason")
        if isinstance(finish, str) and finish:
            self.finish_reason = finish
        return events

    def finalize(self) -> list[str]:
        if self.completed:
            return []
        events = self._ensure_started()
        if self.content_buffer:
            events.extend(self._push_text_delta(self.content_buffer))
            self.content_buffer = ""
        events.extend(self._flush_pending_think())
        events.extend(self._finalize_reasoning())
        events.extend(self._finalize_text())
        events.extend(self._finalize_tools())

        if self.finish_reason == "length":
            status = "incomplete"
        else:
            status = "completed"
        response = self._base_response(status, list(self.output_items))
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        events.append(
            sse_event("response.completed", {"type": "response.completed", "response": response})
        )
        self.completed = True
        return events
