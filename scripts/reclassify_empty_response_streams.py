#!/usr/bin/env python3
"""Reclassify historical empty Responses streams as errors.

Only successful ``/v1/responses`` stream records with absent or zero total
usage are changed.  The script is idempotent: once a request log is changed
to 502, it no longer matches.  A consistent SQLite backup is created before
any write.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "switchyard.db"
ERROR = "upstream Responses stream completed without token usage"


def _is_target(entry: dict[str, Any]) -> bool:
    if entry.get("path") != "/v1/responses" or not entry.get("stream"):
        return False
    try:
        return int(entry.get("total_tokens") or 0) <= 0
    except (TypeError, ValueError):
        return True


def _attempts(entry: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = entry.get("attempts")
    if isinstance(attempts, list):
        return [item for item in attempts if isinstance(item, dict)]
    upstream = str(entry.get("upstream") or "").strip()
    if not upstream:
        return []
    return [{
        "upstream": upstream,
        "pool": entry.get("pool"),
        "status": 200,
        "error": ERROR,
        "failover": False,
    }]


def _error_entry(entry: dict[str, Any], error_id: str) -> dict[str, Any]:
    return {
        "id": error_id,
        "ts": entry.get("ts"),
        "client_ip": entry.get("client_ip", ""),
        "method": entry.get("method", "POST"),
        "path": entry.get("path", "/v1/responses"),
        "pool": entry.get("pool", ""),
        "client_model": entry.get("client_model"),
        "stream": True,
        "is_probe": bool(entry.get("is_probe", False)),
        "status": 502,
        "error": ERROR,
        "duration_ms": entry.get("duration_ms", 0),
        "request_body": None,
        "request_body_len": None,
        "request_body_truncated": False,
        "attempts": _attempts(entry),
    }


def _backup_database() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB.with_name(f"{DB.name}.pre-empty-response-streams-{stamp}.bak")
    source = sqlite3.connect(DB)
    destination = sqlite3.connect(backup)
    try:
        with destination:
            source.backup(destination)
    finally:
        destination.close()
        source.close()
    backup.chmod(0o600)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not DB.exists():
        print(f"database not found: {DB}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    rows = conn.execute(
        "SELECT seq, payload FROM request_logs WHERE status BETWEEN 200 AND 399"
    ).fetchall()
    targets: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        try:
            entry = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict) and _is_target(entry):
            targets.append((int(row["seq"]), entry))

    existing_errors = sum(bool(entry.get("error_log_id")) for _, entry in targets)
    print(f"candidates: {len(targets)}")
    print(f"existing error links retained: {existing_errors}")
    if args.dry_run:
        conn.close()
        return 0

    backup = _backup_database()
    print(f"backup: {backup}")
    created_errors = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for seq, entry in targets:
            error_id = entry.get("error_log_id")
            if not error_id:
                error_id = uuid.uuid4().hex
                error = _error_entry(entry, error_id)
                attempted = [
                    str(item.get("upstream") or "").strip()
                    for item in error["attempts"]
                    if str(item.get("upstream") or "").strip()
                ]
                conn.execute(
                    "INSERT INTO error_logs "
                    "(id, ts, pool, client_model, status, is_probe, upstream, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        error_id,
                        error["ts"],
                        error["pool"] or None,
                        error["client_model"],
                        502,
                        1 if error["is_probe"] else 0,
                        json.dumps(attempted, ensure_ascii=False) if attempted else None,
                        json.dumps(error, ensure_ascii=False),
                    ),
                )
                created_errors += 1
            entry["status"] = 502
            entry["error_log_id"] = error_id
            entry["stream_error"] = entry.get("stream_error") or ERROR
            if entry.get("stream_completed") is None:
                entry["stream_completed"] = True
            conn.execute(
                "UPDATE request_logs SET status = ?, payload = ? WHERE seq = ?",
                (502, json.dumps(entry, ensure_ascii=False), seq),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"request logs reclassified: {len(targets)}")
    print(f"error logs created: {created_errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
