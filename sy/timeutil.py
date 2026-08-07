"""北京时间工具：日志时间统一用 Asia/Shanghai。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def parse_ts(value: Any) -> Optional[datetime]:
    """解析时间戳，返回北京时间 aware datetime。"""
    if not isinstance(value, str):
        return None
    try:
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BEIJING_TZ)
        return dt.astimezone(BEIJING_TZ)
    except Exception:
        return None


def timestamp_in_beijing(value: Any) -> Optional[str]:
    dt = parse_ts(value)
    return dt.isoformat(timespec="milliseconds") if dt is not None else None


def entry_in_beijing(entry: dict) -> dict:
    out = dict(entry)
    ts = timestamp_in_beijing(out.get("ts"))
    if ts is not None:
        out["ts"] = ts
    return out


def iso_now() -> str:
    return now_beijing().isoformat(timespec="milliseconds")
