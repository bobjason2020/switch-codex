#!/usr/bin/env python3
"""Switch-codex SQLite storage layer for the multi-upstream router.

Replaces the JSON / JSONL data files with a single WAL-mode SQLite database.
Legacy files are imported once on first startup, then archived under
``data/legacy-backup/`` so nothing is lost.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "switchyard.db"
LEGACY_DB = DATA / "simple_router.db"
LEGACY_BACKUP_DIR = DATA / "legacy-backup"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")

LEGACY_FILES = (
    "config.json",
    "upstreams.json",
    "auth.json",
    "newapi_probes.json",
    "request_logs.jsonl",
    "error_logs.jsonl",
    "availability_history.jsonl",
)

MIGRATION_KEY = "storage_migrated_v1"

_log = logging.getLogger("switchyard.storage")
_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS upstreams (
    id      TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS newapi_probes (
    id      TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_logs (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    pool         TEXT,
    client_model TEXT,
    status       INTEGER,
    is_probe     INTEGER NOT NULL DEFAULT 0,
    upstream     TEXT,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_logs_ts ON request_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_request_logs_pool ON request_logs(pool);
CREATE INDEX IF NOT EXISTS idx_request_logs_model ON request_logs(client_model);
CREATE INDEX IF NOT EXISTS idx_request_logs_status ON request_logs(status);

CREATE TABLE IF NOT EXISTS error_logs (
    id           TEXT PRIMARY KEY,
    ts           TEXT NOT NULL,
    pool         TEXT,
    client_model TEXT,
    status       INTEGER,
    is_probe     INTEGER NOT NULL DEFAULT 0,
    upstream     TEXT,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_error_logs_ts ON error_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_error_logs_pool ON error_logs(pool);
CREATE INDEX IF NOT EXISTS idx_error_logs_model ON error_logs(client_model);

CREATE TABLE IF NOT EXISTS availability_history (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    model       TEXT NOT NULL,
    ok          INTEGER,
    light       TEXT,
    multiplier  REAL,
    upstream    TEXT,
    source      TEXT,
    status_code INTEGER,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_avail_history_ts ON availability_history(ts DESC);
CREATE INDEX IF NOT EXISTS idx_avail_history_model ON availability_history(model);

CREATE TABLE IF NOT EXISTS model_availability (
    model   TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token      TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
"""


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)


def _ts_key(value: Any) -> str:
    dt = _parse_ts(value)
    if dt is None:
        return str(value or "")
    return dt.isoformat(timespec="milliseconds")


def _as_opt_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value)
    return s or None


def _as_opt_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_probe_entry(entry: dict) -> bool:
    if entry.get("is_probe") is True:
        return True
    path = str(entry.get("path") or "")
    return path.startswith("/api/upstreams/") and path.endswith("/test")


def _decode(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _ensure_data_dir() -> None:
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(DATA, 0o700)
    except OSError:
        pass


def _migrate_old_db_if_needed() -> None:
    """从旧 simple_router.db 平滑迁移到 switchyard.db（先备份，不删除旧库）。"""
    if DB_PATH.exists() or not LEGACY_DB.exists():
        return
    try:
        _ensure_data_dir()
        src = sqlite3.connect(f"file:{LEGACY_DB}?mode=ro", uri=True)
        dst = sqlite3.connect(DB_PATH)
        src.backup(dst)
        dst.close()
        src.close()
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass
        _log.info("migrated legacy database %s -> %s", LEGACY_DB, DB_PATH)
    except Exception:
        _log.exception("legacy database migration failed")


def _connect() -> sqlite3.Connection:
    _ensure_data_dir()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _ensure_log_upstream_columns(conn)
    conn.commit()
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    return conn


def _ensure_log_upstream_columns(conn: sqlite3.Connection) -> None:
    """Add the ``upstream`` filter column to log tables on existing DBs.

    Fresh databases get the column directly from SCHEMA; this migration only
    alters pre-existing tables and backfills values from stored payloads.
    """
    for table in ("request_logs", "error_logs"):
        columns = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "upstream" in columns:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN upstream TEXT")
        if table == "request_logs":
            conn.execute(
                "UPDATE request_logs SET upstream = json_extract(payload, '$.upstream') "
                "WHERE upstream IS NULL"
            )
        else:
            conn.execute(
                "UPDATE error_logs SET upstream = ("
                "SELECT json_group_array(json_extract(j.value, '$.upstream')) "
                "FROM json_each(error_logs.payload, '$.attempts') AS j "
                "WHERE json_extract(j.value, '$.upstream') IS NOT NULL "
                "AND json_extract(j.value, '$.upstream') != ''"
                ") WHERE upstream IS NULL"
            )
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_upstream ON {table}(upstream)")
        _log.info("migrated %s: added upstream column", table)


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
        return _conn


def init_db() -> None:
    with _lock:
        _migrate_old_db_if_needed()
        _get_conn()


# ---------------------------------------------------------------------------
# settings (config / auth / pricing)
# ---------------------------------------------------------------------------


def get_setting(key: str, default: Any = None) -> Any:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        conn.commit()


def delete_setting(key: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        conn.commit()


def load_config_raw() -> Optional[dict]:
    value = get_setting("config")
    return value if isinstance(value, dict) else None


def save_config_raw(cfg: dict) -> None:
    set_setting("config", cfg)


def load_auth_raw() -> dict:
    value = get_setting("auth")
    return value if isinstance(value, dict) else {}


def save_auth_raw(auth: dict) -> None:
    set_setting("auth", auth)


# ---------------------------------------------------------------------------
# admin sessions
# ---------------------------------------------------------------------------


def load_admin_sessions() -> list[tuple[str, str]]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT token, expires_at FROM admin_sessions"
        ).fetchall()
    return [(str(r["token"]), str(r["expires_at"])) for r in rows]


def save_admin_session(token: str, expires_at: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO admin_sessions(token, expires_at) "
            "VALUES(?, ?)",
            (token, expires_at),
        )
        conn.commit()


def delete_admin_session(token: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
        conn.commit()


def delete_all_admin_sessions() -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM admin_sessions")
        conn.commit()


def purge_expired_admin_sessions(before_iso: str) -> int:
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM admin_sessions WHERE expires_at < ?", (before_iso,)
        )
        conn.commit()
    return max(cur.rowcount or 0, 0)


# ---------------------------------------------------------------------------
# upstreams / newapi probes
# ---------------------------------------------------------------------------


def load_upstreams() -> list[dict]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT payload FROM upstreams ORDER BY rowid"
        ).fetchall()
    return [_decode(r["payload"]) for r in rows]


def save_upstreams(items: list[dict]) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM upstreams")
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("id") or uuid.uuid4())
            conn.execute(
                "INSERT OR REPLACE INTO upstreams(id, payload) VALUES(?, ?)",
                (iid, json.dumps(item, ensure_ascii=False)),
            )
        conn.commit()


def load_newapi_probes() -> list[dict]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT payload FROM newapi_probes ORDER BY rowid"
        ).fetchall()
    return [_decode(r["payload"]) for r in rows]


def save_newapi_probes(items: list[dict]) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM newapi_probes")
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = str(item.get("id") or uuid.uuid4())
            conn.execute(
                "INSERT OR REPLACE INTO newapi_probes(id, payload) VALUES(?, ?)",
                (iid, json.dumps(item, ensure_ascii=False)),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# request logs
# ---------------------------------------------------------------------------


def _insert_request_log_row(conn: sqlite3.Connection, entry: dict) -> None:
    conn.execute(
        "INSERT INTO request_logs(ts, pool, client_model, status, is_probe, upstream, payload) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            _ts_key(entry.get("ts")),
            _as_opt_str(entry.get("pool")),
            _as_opt_str(entry.get("client_model")),
            _as_opt_int(entry.get("status")),
            1 if _is_probe_entry(entry) else 0,
            _as_opt_str(entry.get("upstream")),
            json.dumps(entry, ensure_ascii=False),
        ),
    )


def insert_request_log(entry: dict) -> None:
    with _lock:
        conn = _get_conn()
        _insert_request_log_row(conn, entry)
        conn.commit()


def load_request_logs() -> list[dict]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT payload FROM request_logs ORDER BY seq"
        ).fetchall()
    return [_decode(r["payload"]) for r in rows]


def load_request_logs_range(
    start: Optional[str] = None, end: Optional[str] = None
) -> list[dict]:
    """按 ts 范围加载请求日志(升序),start/end 为 ISO 字符串,含 start、不含 end。"""
    where: list[str] = []
    params: list[Any] = []
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts < ?")
        params.append(end)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            f"SELECT payload FROM request_logs{where_sql} ORDER BY seq", params
        ).fetchall()
    return [_decode(r["payload"]) for r in rows]


def load_last_usage_entry(session_id: str, upstream: str) -> Optional[dict]:
    """返回同 (cache_session_id, upstream) 最近一条带 usage 的日志。

    供写路径增量判定掉缓存时补种前驱状态；无则返回 None。
    """
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT payload FROM request_logs "
            "WHERE (json_extract(payload, '$.cache_session_id') = ? "
            "OR (json_extract(payload, '$.cache_session_id') IS NULL "
            "AND json_extract(payload, '$.session_id') = ?)) "
            "AND upstream = ? "
            "AND json_extract(payload, '$.cache_read_tokens') IS NOT NULL "
            "AND json_extract(payload, '$.input_tokens') IS NOT NULL "
            "ORDER BY seq DESC LIMIT 1",
            (str(session_id), str(session_id), str(upstream)),
        ).fetchone()
    return _decode(row["payload"]) if row is not None else None


def has_request_logs_missing_field(field: str, expected_value: Any = None) -> bool:
    """是否存在 payload 缺少字段或字段值不符合预期的请求日志。"""
    paths = {
        "is_cache_miss": "$.is_cache_miss",
        "cache_miss_tokens": "$.cache_miss_tokens",
        "cache_miss_extra_usd": "$.cache_miss_extra_usd",
        "cache_miss_type": "$.cache_miss_type",
        "cache_miss_rule_version": "$.cache_miss_rule_version",
        "multiplier": "$.multiplier",
    }
    path = paths.get(str(field))
    if path is None:
        raise ValueError(f"unsupported request log field: {field!r}")
    with _lock:
        conn = _get_conn()
        if expected_value is None:
            row = conn.execute(
                "SELECT seq FROM request_logs "
                "WHERE json_extract(payload, ?) IS NULL LIMIT 1",
                (path,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT seq FROM request_logs "
                "WHERE json_extract(payload, ?) IS NULL "
                "OR json_extract(payload, ?) != ? LIMIT 1",
                (path, path, expected_value),
            ).fetchone()
    return row is not None


def load_request_log_rows() -> list[tuple[int, dict]]:
    """返回 (seq, payload) 列表(升序),供字段回填按行定位更新。"""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT seq, payload FROM request_logs ORDER BY seq"
        ).fetchall()
    return [(int(r["seq"]), _decode(r["payload"])) for r in rows]


def load_request_log_rows_after(seq: int = 0, limit: int = 500) -> list[tuple[int, dict]]:
    """Load a bounded, ordered batch for resumable maintenance jobs."""
    limit = max(1, min(int(limit), 5000))
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT seq, payload FROM request_logs WHERE seq > ? ORDER BY seq LIMIT ?",
            (int(seq), limit),
        ).fetchall()
    return [(int(r["seq"]), _decode(r["payload"])) for r in rows]


def update_request_log_payloads(pairs: list[tuple[int, dict]]) -> None:
    """按 seq 逐条更新 payload(不增删行,回填期间并发写入的新行不受影响)。"""
    with _lock:
        conn = _get_conn()
        conn.execute("BEGIN")
        try:
            for seq, item in pairs:
                conn.execute(
                    "UPDATE request_logs SET payload = ? WHERE seq = ?",
                    (json.dumps(item, ensure_ascii=False), int(seq)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query_request_logs(
    limit: int,
    offset: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
    pool: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[str] = None,
    upstream: Optional[str] = None,
    q: Optional[str] = None,
) -> tuple[int, list[dict]]:
    """Paginated request-log query (newest first)."""
    where: list[str] = []
    params: list[Any] = []
    # The management request-log view excludes health/probe traffic.
    where.append("is_probe = 0")
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts < ?")
        params.append(end)
    if pool:
        where.append("pool = ?")
        params.append(pool)
    if model:
        if model == "未知模型":
            where.append("(client_model IS NULL OR client_model = '')")
        else:
            where.append("client_model = ?")
            params.append(model)
    if status:
        if status in ("success", "ok"):
            where.append("status >= 200 AND status < 400")
        elif status in ("error", "fail"):
            where.append("status IS NULL OR status < 200 OR status >= 400")
        elif status == "cache_miss":
            where.append(
                "status >= 200 AND status < 400 "
                "AND json_extract(payload, '$.is_cache_miss') IN (1, true)"
            )
        elif str(status).isdigit():
            where.append("status = ?")
            params.append(int(status))
    if upstream:
        where.append("upstream = ?")
        params.append(upstream)
    if q:
        needle = f"%{q.lower()}%"
        where.append(
            "(LOWER(COALESCE(json_extract(payload, '$.upstream'), '')) LIKE ? "
            "OR LOWER(COALESCE(json_extract(payload, '$.client_model'), '')) LIKE ? "
            "OR LOWER(COALESCE(json_extract(payload, '$.client_ip'), '')) LIKE ?)"
        )
        params.extend([needle, needle, needle])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM request_logs{where_sql}", params
        ).fetchone()
        total = int(row["n"] or 0)
        rows = conn.execute(
            f"SELECT payload FROM request_logs{where_sql} "
            "ORDER BY seq DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return total, [_decode(r["payload"]) for r in rows]


def replace_request_logs(items: list[dict]) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM request_logs")
        for item in items:
            if isinstance(item, dict):
                _insert_request_log_row(conn, item)
        conn.commit()


def clear_request_logs() -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM request_logs")
        conn.commit()


# ---------------------------------------------------------------------------
# error logs
# ---------------------------------------------------------------------------


def _insert_error_log_row(conn: sqlite3.Connection, entry: dict) -> None:
    eid = str(entry.get("id") or uuid.uuid4().hex)
    attempted = [
        str(a.get("upstream") or "").strip()
        for a in (entry.get("attempts") or [])
        if isinstance(a, dict) and str(a.get("upstream") or "").strip()
    ]
    conn.execute(
        "INSERT OR REPLACE INTO error_logs"
        "(id, ts, pool, client_model, status, is_probe, upstream, payload) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            eid,
            _ts_key(entry.get("ts")),
            _as_opt_str(entry.get("pool")),
            _as_opt_str(entry.get("client_model")),
            _as_opt_int(entry.get("status")),
            1 if _is_probe_entry(entry) else 0,
            json.dumps(attempted, ensure_ascii=False) if attempted else None,
            json.dumps(entry, ensure_ascii=False),
        ),
    )


def insert_error_log(entry: dict) -> None:
    with _lock:
        conn = _get_conn()
        _insert_error_log_row(conn, entry)
        conn.commit()


def load_error_logs() -> list[dict]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT payload FROM error_logs ORDER BY ts, id"
        ).fetchall()
    return [_decode(r["payload"]) for r in rows]


def query_error_logs(
    limit: int,
    offset: int,
    start: Optional[str] = None,
    end: Optional[str] = None,
    pool: Optional[str] = None,
    model: Optional[str] = None,
    upstream: Optional[str] = None,
    q: Optional[str] = None,
) -> tuple[int, list[dict]]:
    """Paginated error-log query (newest first)."""
    where: list[str] = []
    params: list[Any] = []
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts < ?")
        params.append(end)
    if pool:
        where.append("pool = ?")
        params.append(pool)
    if model:
        if model == "未知模型":
            where.append("(client_model IS NULL OR client_model = '')")
        else:
            where.append("client_model = ?")
            params.append(model)
    if upstream:
        where.append(
            "EXISTS (SELECT 1 FROM json_each(error_logs.upstream) AS ue "
            "WHERE ue.value = ?)"
        )
        params.append(upstream)
    if q:
        where.append("LOWER(payload) LIKE ?")
        params.append(f"%{q.lower()}%")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM error_logs{where_sql}", params
        ).fetchone()
        total = int(row["n"] or 0)
        rows = conn.execute(
            f"SELECT payload FROM error_logs{where_sql} "
            "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return total, [_decode(r["payload"]) for r in rows]


def find_error_log(eid: str) -> Optional[dict]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT payload FROM error_logs WHERE id = ?", (eid,)
        ).fetchone()
    if row is None:
        return None
    return _decode(row["payload"])


def clear_error_logs() -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM error_logs")
        conn.commit()


def prune_request_logs(days: int) -> int:
    cutoff = (datetime.now(BEIJING_TZ) - timedelta(days=days)).isoformat(
        timespec="milliseconds"
    )
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM request_logs WHERE ts < ?", (cutoff,)
        )
        conn.commit()
    return cur.rowcount


def prune_error_logs(hours: int) -> int:
    cutoff = (datetime.now(BEIJING_TZ) - timedelta(hours=hours)).isoformat(
        timespec="milliseconds"
    )
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM error_logs WHERE ts < ?", (cutoff,)
        )
        conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# availability history
# ---------------------------------------------------------------------------


def _insert_avail_history_row(
    conn: sqlite3.Connection, entry: dict
) -> None:
    conn.execute(
        "INSERT INTO availability_history"
        "(ts, model, ok, light, multiplier, upstream, source, status_code, payload) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _ts_key(entry.get("ts")),
            str(entry.get("model") or ""),
            1 if entry.get("ok") is True else (0 if entry.get("ok") is False else None),
            _as_opt_str(entry.get("light")),
            _as_opt_float(entry.get("multiplier")),
            _as_opt_str(entry.get("upstream")),
            _as_opt_str(entry.get("source")),
            _as_opt_int(entry.get("status_code")),
            json.dumps(entry, ensure_ascii=False),
        ),
    )


def insert_availability_history(entry: dict) -> None:
    with _lock:
        conn = _get_conn()
        _insert_avail_history_row(conn, entry)
        conn.commit()


def load_availability_history() -> list[dict]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT payload FROM availability_history ORDER BY seq"
        ).fetchall()
    return [_decode(r["payload"]) for r in rows]


def prune_availability_history(days: int) -> int:
    cutoff = (datetime.now(BEIJING_TZ) - timedelta(days=days)).isoformat(
        timespec="milliseconds"
    )
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM availability_history WHERE ts < ?", (cutoff,)
        )
        conn.commit()
    return cur.rowcount


def clear_availability_history() -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM availability_history")
        conn.commit()


def save_model_availability(items: dict[str, dict]) -> None:
    """Persist the latest live availability snapshot per client model."""
    with _lock:
        conn = _get_conn()
        for model, payload in items.items():
            conn.execute(
                "INSERT INTO model_availability(model, payload) VALUES(?, ?) "
                "ON CONFLICT(model) DO UPDATE SET payload = excluded.payload",
                (str(model), json.dumps(payload, ensure_ascii=False)),
            )
        conn.commit()


def load_model_availability() -> dict[str, dict]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT model, payload FROM model_availability"
        ).fetchall()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            out[row["model"]] = json.loads(row["payload"])
        except Exception:
            continue
    return out


def clear_model_availability() -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM model_availability")
        conn.commit()


# ---------------------------------------------------------------------------
# one-time legacy migration
# ---------------------------------------------------------------------------


def _load_legacy_json(name: str, default: Any) -> Any:
    path = DATA / name
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.exception("failed to parse legacy JSON file %s", name)
        return default


def _import_legacy_jsonl(conn: sqlite3.Connection, name: str, table: str) -> int:
    path = DATA / name
    if not path.exists():
        return 0
    imported = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            if table == "request_logs":
                _insert_request_log_row(conn, entry)
            elif table == "error_logs":
                _insert_error_log_row(conn, entry)
            elif table == "availability_history":
                _insert_avail_history_row(conn, entry)
            imported += 1
    return imported


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in (
        "settings",
        "upstreams",
        "newapi_probes",
        "request_logs",
        "error_logs",
        "availability_history",
    ):
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        out[table] = int(row["n"] or 0)
    return out


def _archive_legacy_files() -> None:
    if not any((DATA / name).exists() for name in LEGACY_FILES):
        return
    stamp = datetime.now(BEIJING_TZ).strftime("%Y%m%d-%H%M%S")
    dest = LEGACY_BACKUP_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for name in LEGACY_FILES:
        src = DATA / name
        if not src.exists():
            continue
        try:
            shutil.move(str(src), str(dest / name))
        except Exception:
            _log.exception("archive failed for %s", name)
    _log.info("archived legacy JSON/JSONL files to %s", dest)


def migrate_legacy_data(force: bool = False) -> dict[str, Any]:
    """Import legacy JSON/JSONL files into SQLite once, then archive them."""
    with _lock:
        conn = _get_conn()

        if not force and get_setting(MIGRATION_KEY):
            _archive_legacy_files()
            return {"migrated": False, "reason": "already-migrated"}

        counts = _table_counts(conn)
        if any(counts.values()) and not force:
            set_setting(
                MIGRATION_KEY,
                {"migrated_at": _ts_key(datetime.now(BEIJING_TZ)), "note": "db already populated"},
            )
            _archive_legacy_files()
            return {"migrated": False, "reason": "db-already-populated"}

        report: dict[str, Any] = {"imported": {}, "archived": False}
        try:
            conn.execute("BEGIN")

            config = _load_legacy_json("config.json", None)
            if isinstance(config, dict):
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES('config', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (json.dumps(config, ensure_ascii=False),),
                )
                report["imported"]["config.json"] = True

            auth = _load_legacy_json("auth.json", None)
            if isinstance(auth, dict):
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES('auth', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (json.dumps(auth, ensure_ascii=False),),
                )
                report["imported"]["auth.json"] = True

            upstreams = _load_legacy_json("upstreams.json", [])
            for item in upstreams if isinstance(upstreams, list) else []:
                if not isinstance(item, dict):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO upstreams(id, payload) VALUES(?, ?)",
                    (
                        str(item.get("id") or uuid.uuid4()),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
            report["imported"]["upstreams"] = len(upstreams) if isinstance(upstreams, list) else 0

            probes = _load_legacy_json("newapi_probes.json", [])
            for item in probes if isinstance(probes, list) else []:
                if not isinstance(item, dict):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO newapi_probes(id, payload) VALUES(?, ?)",
                    (
                        str(item.get("id") or uuid.uuid4()),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
            report["imported"]["newapi_probes"] = len(probes) if isinstance(probes, list) else 0

            report["imported"]["request_logs"] = _import_legacy_jsonl(
                conn, "request_logs.jsonl", "request_logs"
            )
            report["imported"]["error_logs"] = _import_legacy_jsonl(
                conn, "error_logs.jsonl", "error_logs"
            )
            report["imported"]["availability_history"] = _import_legacy_jsonl(
                conn, "availability_history.jsonl", "availability_history"
            )

            conn.commit()
        except Exception:
            conn.rollback()
            _log.exception("legacy migration failed; no changes committed")
            raise

        _archive_legacy_files()
        report["archived"] = True
        set_setting(
            MIGRATION_KEY,
            {
                "migrated_at": _ts_key(datetime.now(BEIJING_TZ)),
                "report": report,
            },
        )
        _log.info("legacy migration complete: %s", report)
        return report
