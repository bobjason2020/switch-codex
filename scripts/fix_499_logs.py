#!/usr/bin/env python3
"""Repair historical logs written by the removed 499 client-disconnect feature.

Rewrites request logs recorded as 499 back to the upstream status (usually
200), removes their error-log links, and deletes the error logs that only
existed for client-disconnected streams. Backs up the database first.
Idempotent: safe to re-run.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "switchyard.db"

CLIENT_DISCONNECT_ERRORS = (
    "client disconnected before stream completed",
    "stream interrupted before response completed "
    "(backfilled: originally recorded as 200 with no usage/tokens)",
)


def main() -> int:
    if not DB.exists():
        print(f"database not found: {DB}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB.with_name(f"{DB.name}.bak-499-{stamp}")
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup.parent.chmod(0o700)
    backup.touch(mode=0o600, exist_ok=False)
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    backup.chmod(0o600)
    print(f"backup: {backup}")

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT seq, payload FROM request_logs "
        "WHERE json_extract(payload, '$.status') = 499"
    ).fetchall()
    updated = 0
    for seq, payload in rows:
        obj = json.loads(payload)
        obj["status"] = 200
        obj.pop("error_log_id", None)
        cur.execute(
            "UPDATE request_logs SET status = ?, payload = ? WHERE seq = ?",
            (200, json.dumps(obj, ensure_ascii=False), seq),
        )
        updated += 1

    marks = ",".join("?" * len(CLIENT_DISCONNECT_ERRORS))
    cur.execute(
        f"DELETE FROM error_logs "
        f"WHERE json_extract(payload, '$.error') IN ({marks})",
        CLIENT_DISCONNECT_ERRORS,
    )
    deleted = cur.rowcount
    conn.commit()

    remaining = cur.execute(
        "SELECT COUNT(*) FROM request_logs "
        "WHERE json_extract(payload, '$.status') = 499"
    ).fetchone()[0]
    conn.close()

    print(f"request logs rewritten to 200: {updated}")
    print(f"error logs deleted: {deleted}")
    print(f"remaining 499 request logs: {remaining}")
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
