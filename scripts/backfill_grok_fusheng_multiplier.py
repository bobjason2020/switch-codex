#!/usr/bin/env python3
"""Rewrite historical grok-fusheng request-log multipliers.

Those rows were written before the NewAPI grok group ratio (0.000004) was
applied, so dashboard cost used list price x 1.0. This sets payload.multiplier
to 0.000004. Idempotent. Krill / other grok upstreams are left alone.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "switchyard.db"

# 浮生 NewAPI grok 分组倍率
GROK_FUSHENG_MULTIPLIER = 0.000004
UPSTREAM_NAME = "grok-fusheng"


def _needs_rewrite(obj: dict) -> bool:
    if str(obj.get("upstream") or "") != UPSTREAM_NAME:
        return False
    try:
        current = float(obj.get("multiplier"))
    except (TypeError, ValueError):
        return True
    return abs(current - GROK_FUSHENG_MULTIPLIER) > 1e-15


def main() -> int:
    if not DB.exists():
        print(f"database not found: {DB}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DB.with_name(f"{DB.name}.pre-grok-mult-{stamp}.bak")
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(backup)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    print(f"backup: {backup}")

    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT seq, payload FROM request_logs WHERE upstream = ?",
        (UPSTREAM_NAME,),
    ).fetchall()

    updated = 0
    skipped = 0
    for seq, payload in rows:
        try:
            obj = json.loads(payload)
        except Exception:
            skipped += 1
            continue
        if not _needs_rewrite(obj):
            skipped += 1
            continue
        obj["multiplier"] = GROK_FUSHENG_MULTIPLIER
        cur.execute(
            "UPDATE request_logs SET payload = ? WHERE seq = ?",
            (json.dumps(obj, ensure_ascii=False), seq),
        )
        updated += 1
    conn.commit()

    remaining = 0
    for (_seq, payload) in cur.execute(
        "SELECT seq, payload FROM request_logs WHERE upstream = ?",
        (UPSTREAM_NAME,),
    ):
        try:
            obj = json.loads(payload)
        except Exception:
            remaining += 1
            continue
        if _needs_rewrite(obj):
            remaining += 1
    conn.close()

    print(f"candidates: {len(rows)}")
    print(f"rewritten to {GROK_FUSHENG_MULTIPLIER}: {updated}")
    print(f"skipped: {skipped}")
    print(f"remaining wrong multiplier: {remaining}")
    return 0 if remaining == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
