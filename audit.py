"""Append-only audit trail shared by Discord, dashboard, webhooks and workers."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            guild_id INTEGER,
            actor_id INTEGER,
            actor_name TEXT,
            target_user_id INTEGER,
            amount INTEGER,
            reason TEXT NOT NULL DEFAULT '',
            before_json TEXT,
            after_json TEXT,
            source TEXT NOT NULL DEFAULT 'system',
            correlation_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            created_at_iso TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_guild_time ON audit_events(guild_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_type_time ON audit_events(event_type, created_at DESC)")
    return conn


def record_event(
    db_path: str,
    event_type: str,
    *,
    guild_id: int | None = None,
    actor_id: int | None = None,
    actor_name: str | None = None,
    target_user_id: int | None = None,
    amount: int | None = None,
    reason: str = "",
    before: Any = None,
    after: Any = None,
    source: str = "system",
    correlation_id: str | None = None,
    created_at: int | None = None,
) -> str:
    """Record one immutable event and return its correlation id."""
    timestamp = int(created_at or time.time())
    correlation_id = correlation_id or uuid.uuid4().hex
    iso = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        conn.execute(
            """INSERT INTO audit_events
            (event_type, guild_id, actor_id, actor_name, target_user_id, amount, reason,
             before_json, after_json, source, correlation_id, created_at, created_at_iso)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(event_type)[:120], int(guild_id) if guild_id else None,
                int(actor_id) if actor_id else None, str(actor_name)[:200] if actor_name else None,
                int(target_user_id) if target_user_id else None, int(amount) if amount is not None else None,
                str(reason or "")[:1000],
                json.dumps(before, ensure_ascii=False, sort_keys=True, default=str) if before is not None else None,
                json.dumps(after, ensure_ascii=False, sort_keys=True, default=str) if after is not None else None,
                str(source)[:80], correlation_id, timestamp, iso,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return correlation_id


def record_ctx(db_path: str, event_type: str, ctx, *, reason: str = "", **kwargs) -> str:
    actor = getattr(ctx, "author", None)
    return record_event(
        db_path, event_type, guild_id=getattr(getattr(ctx, "guild", None), "id", None),
        actor_id=getattr(actor, "id", None), actor_name=getattr(actor, "display_name", None) or getattr(actor, "name", None),
        source="discord", reason=reason, **kwargs,
    )


def list_events(db_path: str, *, guild_id: int | None = None, event_type: str | None = None,
                actor_id: int | None = None, limit: int = 25) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    conn = _connect(db_path)
    try:
        clauses, params = [], []
        if guild_id is not None:
            clauses.append("guild_id = ?"); params.append(int(guild_id))
        if event_type:
            clauses.append("event_type = ?"); params.append(str(event_type))
        if actor_id is not None:
            clauses.append("actor_id = ?"); params.append(int(actor_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = conn.execute(
            "SELECT id,event_type,guild_id,actor_id,actor_name,target_user_id,amount,reason,before_json,after_json,source,correlation_id,created_at,created_at_iso "
            f"FROM audit_events{where} ORDER BY created_at DESC, id DESC LIMIT ?", (*params, limit)
        ).fetchall()
    finally:
        conn.close()
    keys = ("id", "event_type", "guild_id", "actor_id", "actor_name", "target_user_id", "amount", "reason", "before", "after", "source", "correlation_id", "created_at", "created_at_iso")
    result = []
    for row in rows:
        item = dict(zip(keys, row))
        for key in ("before", "after"):
            try:
                item[key] = json.loads(item[key]) if item[key] else None
            except (TypeError, json.JSONDecodeError):
                pass
        result.append(item)
    return result


def ensure_report_store(db_path: str) -> None:
    conn = _connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS periodic_report_runs (period_key TEXT PRIMARY KEY, created_at INTEGER NOT NULL)")
    conn.commit(); conn.close()


def claim_report(db_path: str, period_key: str) -> bool:
    ensure_report_store(db_path)
    conn = _connect(db_path)
    try:
        cur = conn.execute("INSERT OR IGNORE INTO periodic_report_runs(period_key, created_at) VALUES (?, ?)", (period_key, int(time.time())))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
