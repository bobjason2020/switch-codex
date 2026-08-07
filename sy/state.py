"""进程内共享状态：探测健康、模型可用性、NewAPI 探测、后台线程。"""
from __future__ import annotations

import threading
from typing import Any, Optional


# 上游手动探测健康（仅手动「测试」更新）
_probe_health: dict[str, dict[str, Any]] = {}
_probe_lock = threading.Lock()

# 模型级联可用性（每整五分钟探测）
_model_availability: dict[str, dict[str, Any]] = {}
_model_avail_lock = threading.Lock()
_probe_loop_started = False
_probe_stop = threading.Event()

# NewAPI 分组倍率探测
_newapi_probe_state: dict[str, dict[str, Any]] = {}
_newapi_probe_lock = threading.Lock()
_newapi_probe_loop_started = False
_newapi_probe_stop = threading.Event()
_newapi_probe_exec_lock = threading.Lock()


def probe_health_snapshot() -> dict[str, dict[str, Any]]:
    with _probe_lock:
        return {uid: dict(info) for uid, info in _probe_health.items()}


def set_probe_health(uid: str, info: dict[str, Any]) -> None:
    with _probe_lock:
        _probe_health[uid] = dict(info)


def clear_probe_health(uid: str) -> None:
    with _probe_lock:
        _probe_health.pop(uid, None)


def model_availability_snapshot() -> dict[str, dict[str, Any]]:
    with _model_avail_lock:
        return {k: dict(v) for k, v in _model_availability.items()}


def get_model_availability_cached(client_model: Optional[str]) -> Optional[dict[str, Any]]:
    cm = str(client_model or "").strip()
    if not cm:
        return None
    return model_availability_snapshot().get(cm)


def update_model_availability_snapshot(model: str, info: dict[str, Any]) -> dict[str, Any]:
    """更新内存快照并返回旧值。"""
    with _model_avail_lock:
        prev = dict(_model_availability.get(str(model)) or {})
        _model_availability[str(model)] = dict(info)
    return prev


def model_next_run(model: str) -> Optional[float]:
    with _model_avail_lock:
        st = _model_availability.get(str(model))
        return float(st["next_run_at"]) if st and st.get("next_run_at") is not None else None


def set_model_next_run(model: str, value: float) -> None:
    with _model_avail_lock:
        st = _model_availability.setdefault(str(model), {})
        st["next_run_at"] = value


def get_probe_stop_event() -> threading.Event:
    return _probe_stop


def get_newapi_stop_event() -> threading.Event:
    return _newapi_probe_stop


def newapi_probe_snapshot(probe_id: str) -> dict[str, Any]:
    with _newapi_probe_lock:
        st = _newapi_probe_state.get(str(probe_id))
        return dict(st) if st else {}


def set_newapi_probe_state(probe_id: str, info: dict[str, Any]) -> None:
    with _newapi_probe_lock:
        _newapi_probe_state[str(probe_id)] = dict(info)


def pop_newapi_probe_state(probe_id: str) -> None:
    with _newapi_probe_lock:
        _newapi_probe_state.pop(str(probe_id), None)
