"""Persistence primitives for Premium advertising tickets.

Advertising records intentionally live beside the guild's operational data but are
not mixed with support tickets, budget payments, or the existing giveaway tables.
"""
import sqlite3
import time
from database import get_guild_db, MAX_SQLITE_INTEGER

AD_DURATIONS = {"1d": 86400, "2d": 2 * 86400, "3d": 3 * 86400, "5d": 5 * 86400, "7d": 7 * 86400}
AD_STATUSES = {"draft", "awaiting_payment", "paid", "queued", "publishing", "live", "giveaway_ended", "completed", "needs_review", "cancelled"}


def ensure_ad_schema(guild_id: int):
    conn, cur = get_guild_db(guild_id)
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS ad_config (
            guild_id INTEGER PRIMARY KEY,
            daily_limit INTEGER NOT NULL DEFAULT 1 CHECK(daily_limit > 0),
            everyone_fee INTEGER NOT NULL DEFAULT 0 CHECK(everyone_fee >= 0),
            updated_at INTEGER NOT NULL DEFAULT 0
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS ad_prices (
            guild_id INTEGER NOT NULL,
            duration_key TEXT NOT NULL,
            price INTEGER NOT NULL CHECK(price > 0),
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(guild_id, duration_key)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS ad_channels (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            added_at INTEGER NOT NULL,
            PRIMARY KEY(guild_id, channel_id)
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS ad_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            advertiser_id INTEGER NOT NULL,
            ticket_channel_id INTEGER UNIQUE,
            target_channel_id INTEGER,
            duration_key TEXT NOT NULL,
            ad_price INTEGER NOT NULL CHECK(ad_price > 0),
            giveaway_total INTEGER NOT NULL CHECK(giveaway_total >= 5000000),
            winners_count INTEGER NOT NULL DEFAULT 1 CHECK(winners_count > 0),
            mention_type TEXT NOT NULL DEFAULT 'here',
            status TEXT NOT NULL DEFAULT 'draft',
            premium_snapshot INTEGER NOT NULL DEFAULT 1,
            queue_rank INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            published_at INTEGER,
            completed_at INTEGER,
            cancel_reason TEXT,
            title TEXT,
            content TEXT,
            media_url TEXT,
            server_owner_id INTEGER
        )""")
        for column, definition in (
            ("title", "TEXT"), ("content", "TEXT"), ("media_url", "TEXT"),
            ("server_owner_id", "INTEGER"),
        ):
            columns = {row[1] for row in cur.execute("PRAGMA table_info(ad_orders)").fetchall()}
            if column not in columns:
                cur.execute(f"ALTER TABLE ad_orders ADD COLUMN {column} {definition}")
        cur.execute("""CREATE TABLE IF NOT EXISTS ad_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_order_id INTEGER NOT NULL UNIQUE,
            method TEXT NOT NULL,
            expected_amount INTEGER NOT NULL CHECK(expected_amount > 0),
            status TEXT NOT NULL DEFAULT 'pending',
            payment_message_id INTEGER UNIQUE,
            payment_author_id INTEGER,
            paid_at INTEGER,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(ad_order_id) REFERENCES ad_orders(id)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_orders_queue ON ad_orders(status, premium_snapshot DESC, created_at, id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_orders_guild_day ON ad_orders(guild_id, status, published_at)")
        conn.commit()
    finally:
        conn.close()


def get_ad_config(guild_id: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        config = cur.execute("SELECT daily_limit, everyone_fee FROM ad_config WHERE guild_id=?", (guild_id,)).fetchone()
        prices = dict(cur.execute("SELECT duration_key, price FROM ad_prices WHERE guild_id=?", (guild_id,)).fetchall())
        channels = [int(row[0]) for row in cur.execute("SELECT channel_id FROM ad_channels WHERE guild_id=? ORDER BY channel_id", (guild_id,))]
        return {"daily_limit": int(config[0]) if config else 1, "everyone_fee": int(config[1]) if config else 0, "prices": prices, "channels": channels}
    finally:
        conn.close()


def set_ad_price(guild_id: int, duration_key: str, price: int):
    if duration_key not in AD_DURATIONS or int(price) <= 0 or int(price) > MAX_SQLITE_INTEGER:
        raise ValueError("invalid advertisement price")
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        cur.execute("INSERT INTO ad_prices(guild_id,duration_key,price,updated_at) VALUES(?,?,?,?) ON CONFLICT(guild_id,duration_key) DO UPDATE SET price=excluded.price,updated_at=excluded.updated_at", (guild_id, duration_key, int(price), int(time.time())))
        conn.commit()
    finally:
        conn.close()


def set_ad_config(guild_id: int, *, daily_limit=None, everyone_fee=None):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        current = cur.execute("SELECT daily_limit,everyone_fee FROM ad_config WHERE guild_id=?", (guild_id,)).fetchone()
        limit = int(daily_limit if daily_limit is not None else (current[0] if current else 1))
        fee = int(everyone_fee if everyone_fee is not None else (current[1] if current else 0))
        if limit <= 0 or fee < 0:
            raise ValueError("invalid advertisement config")
        cur.execute("INSERT INTO ad_config(guild_id,daily_limit,everyone_fee,updated_at) VALUES(?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET daily_limit=excluded.daily_limit,everyone_fee=excluded.everyone_fee,updated_at=excluded.updated_at", (guild_id, limit, fee, int(time.time())))
        conn.commit()
    finally:
        conn.close()


def add_ad_channel(guild_id: int, channel_id: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        cur.execute("INSERT OR IGNORE INTO ad_channels(guild_id,channel_id,added_at) VALUES(?,?,?)", (guild_id, channel_id, int(time.time())))
        conn.commit()
    finally:
        conn.close()


def remove_ad_channel(guild_id: int, channel_id: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        cur.execute("DELETE FROM ad_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
        conn.commit()
    finally:
        conn.close()


def count_published_today(guild_id: int, start_timestamp: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        row = cur.execute("SELECT COUNT(*) FROM ad_orders WHERE guild_id=? AND status IN ('publishing','live','giveaway_ended','completed') AND published_at>=?", (guild_id, int(start_timestamp))).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def create_ad_order(guild_id: int, advertiser_id: int, ticket_channel_id: int, duration_key: str, ad_price: int, giveaway_total: int, winners_count: int = 1, mention_type: str = "here", queued: bool = False):
    if duration_key not in AD_DURATIONS or int(ad_price) <= 0 or int(giveaway_total) < 5_000_000 or int(winners_count) <= 0 or mention_type not in {"here", "everyone"}:
        raise ValueError("invalid advertisement order")
    ensure_ad_schema(guild_id)
    now = int(time.time())
    status = "queued" if queued else "draft"
    conn, cur = get_guild_db(guild_id)
    try:
        cur.execute("INSERT INTO ad_orders(guild_id,advertiser_id,ticket_channel_id,duration_key,ad_price,giveaway_total,winners_count,mention_type,status,premium_snapshot,created_at,updated_at,server_owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (guild_id, advertiser_id, ticket_channel_id, duration_key, int(ad_price), int(giveaway_total), int(winners_count), mention_type, status, 1, now, now, None))
        order_id = cur.lastrowid
        if queued:
            rank = cur.execute("SELECT COUNT(*) FROM ad_orders WHERE guild_id=? AND status='queued'", (guild_id,)).fetchone()[0]
            cur.execute("UPDATE ad_orders SET queue_rank=? WHERE id=?", (int(rank), order_id))
        conn.commit()
        return int(order_id)
    finally:
        conn.close()


def get_ad_order(guild_id: int, order_id: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        row = cur.execute("SELECT id,guild_id,advertiser_id,ticket_channel_id,target_channel_id,duration_key,ad_price,giveaway_total,winners_count,mention_type,status,queue_rank,created_at,updated_at,published_at,completed_at,cancel_reason,title,content,media_url,server_owner_id FROM ad_orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            return None
        keys = "id guild_id advertiser_id ticket_channel_id target_channel_id duration_key ad_price giveaway_total winners_count mention_type status queue_rank created_at updated_at published_at completed_at cancel_reason title content media_url server_owner_id".split()
        return dict(zip(keys, row))
    finally:
        conn.close()


def update_ad_order(guild_id: int, order_id: int, **fields):
    allowed = {"target_channel_id", "status", "queue_rank", "updated_at", "published_at", "completed_at", "cancel_reason", "duration_key", "ad_price", "giveaway_total", "winners_count", "mention_type", "title", "content", "media_url", "server_owner_id"}
    fields = {key: value for key, value in fields.items() if key in allowed}
    if not fields:
        return False
    fields["updated_at"] = int(time.time())
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        assignments = ", ".join(f"{key}=?" for key in fields)
        cur.execute(f"UPDATE ad_orders SET {assignments} WHERE guild_id=? AND id=?", (*fields.values(), guild_id, order_id))
        changed = cur.rowcount == 1
        conn.commit()
        return changed
    finally:
        conn.close()


def submit_ad_content(guild_id: int, order_id: int, advertiser_id: int, title: str, content: str, media_url: str | None, server_owner_id: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        row = cur.execute("SELECT advertiser_id,status FROM ad_orders WHERE id=? AND guild_id=?", (order_id, guild_id)).fetchone()
        if not row or int(row[0]) != int(advertiser_id) or row[1] not in {"draft", "queued"}:
            return False
        next_status = "queued" if row[1] == "queued" else "awaiting_payment"
        cur.execute("UPDATE ad_orders SET title=?,content=?,media_url=?,server_owner_id=?,status=?,updated_at=? WHERE id=? AND guild_id=?", (title, content, media_url, server_owner_id, next_status, int(time.time()), order_id, guild_id))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def create_ad_payment(guild_id: int, order_id: int, method: str, expected_amount: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        cur.execute("INSERT INTO ad_payments(ad_order_id,method,expected_amount,status,created_at) VALUES(?,?,?,?,?) ON CONFLICT(ad_order_id) DO UPDATE SET method=excluded.method,expected_amount=excluded.expected_amount,status='pending'", (order_id, method, int(expected_amount), "pending", int(time.time())))
        conn.commit()
    finally:
        conn.close()


def get_ad_payment(guild_id: int, order_id: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        row = cur.execute("SELECT id,ad_order_id,method,expected_amount,status,payment_message_id,payment_author_id,paid_at FROM ad_payments WHERE ad_order_id=?", (order_id,)).fetchone()
        if not row:
            return None
        keys = "id ad_order_id method expected_amount status payment_message_id payment_author_id paid_at".split()
        return dict(zip(keys, row))
    finally:
        conn.close()


def settle_ad_payment(guild_id: int, order_id: int, payment_message_id: int, payment_author_id: int, paid_amount: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = cur.execute("SELECT expected_amount,status FROM ad_payments WHERE ad_order_id=?", (order_id,)).fetchone()
        if not row or int(paid_amount) < int(row[0]):
            conn.rollback(); return {"status": "needs_review"}
        if row[1] == "paid":
            conn.commit(); return {"status": "already_paid"}
        cur.execute("UPDATE ad_payments SET status='paid',payment_message_id=?,payment_author_id=?,paid_at=? WHERE ad_order_id=? AND status='pending'", (payment_message_id, payment_author_id, int(time.time()), order_id))
        if cur.rowcount != 1:
            conn.rollback(); return {"status": "already_paid"}
        cur.execute("UPDATE ad_orders SET status='paid',updated_at=? WHERE id=? AND status IN ('awaiting_payment','queued')", (int(time.time()), order_id))
        conn.commit()
        return {"status": "paid"}
    finally:
        conn.close()
