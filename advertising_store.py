"""Persistence primitives for Premium advertising tickets.

Advertising records intentionally live beside the guild's operational data but are
not mixed with support tickets, budget payments, or the existing giveaway tables.
"""
import sqlite3
import time
import json
from database import get_guild_db, MAX_SQLITE_INTEGER

AD_DURATIONS = {"1d": 86400, "2d": 2 * 86400, "3d": 3 * 86400, "5d": 5 * 86400, "7d": 7 * 86400}
AD_STATUSES = {"draft", "awaiting_payment", "paid", "queued", "publishing", "live", "giveaway_ended", "awaiting_owner_decision", "completed", "needs_review", "cancelled"}
MIN_GIVEAWAY = 5_000_000
MULTI_WINNER_MINIMUM = 100_000_000


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
            everyone_fee INTEGER NOT NULL DEFAULT 0 CHECK(everyone_fee >= 0),
            total_payment INTEGER NOT NULL DEFAULT 1 CHECK(total_payment > 0),
            giveaway_total INTEGER NOT NULL CHECK(giveaway_total >= 5000000),
            winners_count INTEGER NOT NULL DEFAULT 1 CHECK(winners_count > 0),
            amount_per_winner INTEGER NOT NULL DEFAULT 5000000 CHECK(amount_per_winner >= 5000000),
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
            ("everyone_fee", "INTEGER NOT NULL DEFAULT 0"),
            ("total_payment", "INTEGER NOT NULL DEFAULT 1"),
            ("amount_per_winner", "INTEGER NOT NULL DEFAULT 5000000"),
        ):
            columns = {row[1] for row in cur.execute("PRAGMA table_info(ad_orders)").fetchall()}
            if column not in columns:
                cur.execute(f"ALTER TABLE ad_orders ADD COLUMN {column} {definition}")
        cur.execute("UPDATE ad_orders SET total_payment=ad_price+everyone_fee+giveaway_total WHERE total_payment=1")
        cur.execute("UPDATE ad_orders SET amount_per_winner=giveaway_total/winners_count WHERE winners_count>0 AND giveaway_total/winners_count>=5000000 AND amount_per_winner=5000000")
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
        payment_columns = {row[1] for row in cur.execute("PRAGMA table_info(ad_payments)").fetchall()}
        for column, definition in (("recipient_id", "INTEGER NOT NULL DEFAULT 0"), ("channel_id", "INTEGER NOT NULL DEFAULT 0")):
            if column not in payment_columns:
                cur.execute(f"ALTER TABLE ad_payments ADD COLUMN {column} {definition}")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ad_open_advertiser ON ad_orders(guild_id, advertiser_id) WHERE status NOT IN ('completed','cancelled','needs_review')")
        cur.execute("""CREATE TABLE IF NOT EXISTS ad_giveaways (
            ad_order_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
            message_id INTEGER UNIQUE, channel_id INTEGER, generation INTEGER NOT NULL DEFAULT 0,
            original_channel_name TEXT,
            end_timestamp INTEGER,
            status TEXT NOT NULL DEFAULT 'reserved', funding_source TEXT NOT NULL,
            reserved_amount INTEGER NOT NULL CHECK(reserved_amount > 0),
            reservation_key TEXT NOT NULL UNIQUE, payout_key TEXT UNIQUE, refund_key TEXT UNIQUE,
            winner_ids TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            FOREIGN KEY(ad_order_id) REFERENCES ad_orders(id)
        )""")
        giveaway_columns = {row[1] for row in cur.execute("PRAGMA table_info(ad_giveaways)").fetchall()}
        if "original_channel_name" not in giveaway_columns:
            cur.execute("ALTER TABLE ad_giveaways ADD COLUMN original_channel_name TEXT")
        if "end_timestamp" not in giveaway_columns:
            cur.execute("ALTER TABLE ad_giveaways ADD COLUMN end_timestamp INTEGER")
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


def giveaway_amount_per_winner(giveaway_total: int, winners_count: int = 1) -> int:
    total, winners = int(giveaway_total), int(winners_count)
    if total < MIN_GIVEAWAY or winners <= 0 or (winners > 1 and total < MULTI_WINNER_MINIMUM):
        raise ValueError("invalid giveaway winners")
    if total % winners != 0 or total // winners < MIN_GIVEAWAY:
        raise ValueError("each winner must receive at least 5000000")
    return total // winners


def create_ad_order(guild_id: int, advertiser_id: int, ticket_channel_id: int, duration_key: str, ad_price: int, giveaway_total: int, winners_count: int = 1, mention_type: str = "here", queued: bool = False, everyone_fee: int = 0, premium_snapshot: bool = True, server_owner_id: int | None = None):
    if duration_key not in AD_DURATIONS or int(ad_price) <= 0 or mention_type not in {"here", "everyone"}:
        raise ValueError("invalid advertisement order")
    amount_per_winner = giveaway_amount_per_winner(giveaway_total, winners_count)
    everyone_fee = int(everyone_fee)
    total_payment = int(ad_price) + (everyone_fee if mention_type == "everyone" else 0) + int(giveaway_total)
    if everyone_fee < 0 or total_payment <= 0 or total_payment > MAX_SQLITE_INTEGER:
        raise ValueError("invalid advertisement payment")
    ensure_ad_schema(guild_id)
    now = int(time.time())
    status = "queued" if queued else "draft"
    conn, cur = get_guild_db(guild_id)
    try:
        open_order = cur.execute("SELECT id FROM ad_orders WHERE guild_id=? AND advertiser_id=? AND status NOT IN ('completed','cancelled','needs_review')", (guild_id, advertiser_id)).fetchone()
        if open_order:
            raise ValueError("advertiser already has an open advertisement")
        cur.execute("INSERT INTO ad_orders(guild_id,advertiser_id,ticket_channel_id,duration_key,ad_price,everyone_fee,total_payment,giveaway_total,winners_count,amount_per_winner,mention_type,status,premium_snapshot,created_at,updated_at,server_owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (guild_id, advertiser_id, ticket_channel_id, duration_key, int(ad_price), everyone_fee, total_payment, int(giveaway_total), int(winners_count), amount_per_winner, mention_type, status, int(bool(premium_snapshot)), now, now, server_owner_id))
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
        row = cur.execute("SELECT id,guild_id,advertiser_id,ticket_channel_id,target_channel_id,duration_key,ad_price,everyone_fee,total_payment,giveaway_total,winners_count,amount_per_winner,mention_type,status,queue_rank,created_at,updated_at,published_at,completed_at,cancel_reason,title,content,media_url,server_owner_id,premium_snapshot FROM ad_orders WHERE guild_id=? AND id=?", (guild_id, order_id)).fetchone()
        if not row:
            return None
        keys = "id guild_id advertiser_id ticket_channel_id target_channel_id duration_key ad_price everyone_fee total_payment giveaway_total winners_count amount_per_winner mention_type status queue_rank created_at updated_at published_at completed_at cancel_reason title content media_url server_owner_id premium_snapshot".split()
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


def create_ad_payment(guild_id: int, order_id: int, method: str, expected_amount: int, recipient_id: int = 0, channel_id: int = 0):
    if method not in {"credit", "adamc"} or int(expected_amount) <= 0:
        raise ValueError("invalid advertisement payment")
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        cur.execute("INSERT INTO ad_payments(ad_order_id,method,expected_amount,recipient_id,channel_id,status,created_at) VALUES(?,?,?,?,?,'pending',?) ON CONFLICT(ad_order_id) DO UPDATE SET method=excluded.method,expected_amount=excluded.expected_amount,recipient_id=excluded.recipient_id,channel_id=excluded.channel_id,status='pending'", (order_id, method, int(expected_amount), int(recipient_id), int(channel_id), int(time.time())))
        conn.commit()
    finally:
        conn.close()


def get_ad_payment(guild_id: int, order_id: int):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        row = cur.execute("SELECT id,ad_order_id,method,expected_amount,recipient_id,channel_id,status,payment_message_id,payment_author_id,paid_at FROM ad_payments WHERE ad_order_id=?", (order_id,)).fetchone()
        if not row:
            return None
        keys = "id ad_order_id method expected_amount recipient_id channel_id status payment_message_id payment_author_id paid_at".split()
        return dict(zip(keys, row))
    finally:
        conn.close()


def settle_ad_payment(guild_id: int, order_id: int, payment_message_id: int, payment_author_id: int, paid_amount: int, *, recipient_id: int | None = None, channel_id: int | None = None):
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = cur.execute("SELECT expected_amount,status,recipient_id,channel_id FROM ad_payments WHERE ad_order_id=?", (order_id,)).fetchone()
        if not row or int(paid_amount) < int(row[0]) or (row[2] and recipient_id is not None and int(row[2]) != int(recipient_id)) or (row[3] and channel_id is not None and int(row[3]) != int(channel_id)):
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


def claim_next_queued_order(guild_id: int, start_timestamp: int, daily_limit: int):
    """Claim one queued order atomically; ordering is created_at then id."""
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        published = cur.execute("SELECT COUNT(*) FROM ad_orders WHERE guild_id=? AND status IN ('publishing','live','giveaway_ended','awaiting_owner_decision','completed') AND published_at>=?", (guild_id, int(start_timestamp))).fetchone()[0]
        if int(published) >= int(daily_limit):
            conn.rollback(); return None
        row = cur.execute("SELECT o.id FROM ad_orders o LEFT JOIN ad_payments p ON p.ad_order_id=o.id WHERE o.guild_id=? AND o.status='queued' AND o.title IS NOT NULL AND o.content IS NOT NULL AND (o.advertiser_id=o.server_owner_id OR p.status='paid') ORDER BY o.created_at,o.id LIMIT 1", (guild_id,)).fetchone()
        if not row:
            conn.rollback(); return None
        cur.execute("UPDATE ad_orders SET status='publishing',published_at=?,updated_at=? WHERE guild_id=? AND id=? AND status='queued'", (int(time.time()), int(time.time()), guild_id, row[0]))
        if cur.rowcount != 1:
            conn.rollback(); return None
        conn.commit(); return int(row[0])
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def mark_ad_needs_review(guild_id: int, order_id: int, error: str):
    return update_ad_order(guild_id, order_id, status="needs_review", cancel_reason=str(error)[:1000])


def reserve_ad_giveaway(guild_id: int, order_id: int, funding_source: str = "server_budget"):
    """Reserve an ad prize once. Server-funded ads debit the guild budget atomically."""
    if funding_source not in {"server_budget", "customer_payment"}:
        raise ValueError("invalid advertisement funding source")
    ensure_ad_schema(guild_id)
    conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = cur.execute("SELECT status,reserved_amount FROM ad_giveaways WHERE ad_order_id=?", (order_id,)).fetchone()
        if existing:
            conn.commit()
            return {"status": "already_reserved", "amount": int(existing[1])}
        order = cur.execute("SELECT giveaway_total,status FROM ad_orders WHERE guild_id=? AND id=?", (guild_id, order_id)).fetchone()
        if not order or order[1] not in {"paid", "publishing"}:
            conn.rollback(); return {"status": "needs_review"}
        amount = int(order[0])
        if funding_source == "server_budget":
            cur.execute("INSERT OR IGNORE INTO settings(guild_id, spending_balance) VALUES(?,0)", (guild_id,))
            changed = cur.execute("UPDATE settings SET spending_balance=spending_balance-? WHERE guild_id=? AND spending_balance>=?", (amount,guild_id,amount))
            if changed.rowcount != 1:
                conn.rollback(); return {"status": "insufficient_budget", "amount": amount}
        now = int(time.time()); key = f"ad-reservation:{guild_id}:{order_id}"
        cur.execute("INSERT INTO ad_giveaways(ad_order_id,guild_id,status,funding_source,reserved_amount,reservation_key,created_at,updated_at) VALUES(?,?, 'reserved',?,?,?,?,?)", (order_id,guild_id,funding_source,amount,key,now,now))
        conn.commit(); return {"status": "reserved", "amount": amount, "reservation_key": key}
    except Exception:
        conn.rollback(); raise
    finally: conn.close()


def record_ad_published(guild_id: int, order_id: int, channel_id: int, message_id: int, generation: int = 0, original_channel_name: str | None = None):
    ensure_ad_schema(guild_id); conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = cur.execute("SELECT status,message_id FROM ad_giveaways WHERE ad_order_id=?", (order_id,)).fetchone()
        if not row:
            conn.rollback(); return {"status": "needs_review"}
        if row[1] is not None:
            if int(row[1]) == int(message_id): conn.commit(); return {"status": "already_live"}
            conn.rollback(); return {"status": "needs_review"}
        duration = cur.execute("SELECT duration_key FROM ad_orders WHERE guild_id=? AND id=?", (guild_id, order_id)).fetchone()
        if not duration or duration[0] not in AD_DURATIONS:
            conn.rollback(); return {"status": "needs_review"}
        now = int(time.time())
        cur.execute("UPDATE ad_giveaways SET status='live',channel_id=?,message_id=?,generation=?,original_channel_name=?,end_timestamp=?,updated_at=? WHERE ad_order_id=? AND status='reserved'", (channel_id,message_id,generation,original_channel_name,now + AD_DURATIONS[duration[0]],now,order_id))
        cur.execute("UPDATE ad_orders SET status='live',target_channel_id=?,published_at=COALESCE(published_at,?),updated_at=? WHERE guild_id=? AND id=? AND status IN ('paid','publishing')", (channel_id,int(time.time()),int(time.time()),guild_id,order_id))
        if cur.rowcount != 1: conn.rollback(); return {"status":"needs_review"}
        conn.commit(); return {"status":"live", "message_id": int(message_id)}
    except Exception: conn.rollback(); raise
    finally: conn.close()


def finish_ad_giveaway(guild_id: int, order_id: int, winner_ids):
    winner_ids = [int(x) for x in winner_ids]
    ensure_ad_schema(guild_id); conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = cur.execute("SELECT status FROM ad_giveaways WHERE ad_order_id=?", (order_id,)).fetchone()
        if not row or row[0] not in {"live", "ending", "giveaway_ended", "awaiting_owner_decision"}:
            conn.rollback(); return {"status":"needs_review"}
        cur.execute("UPDATE ad_giveaways SET status='awaiting_owner_decision',winner_ids=?,updated_at=? WHERE ad_order_id=? AND status IN ('live','ending','giveaway_ended')", (json.dumps(winner_ids),int(time.time()),order_id))
        cur.execute("UPDATE ad_orders SET status='awaiting_owner_decision',updated_at=? WHERE guild_id=? AND id=? AND status IN ('live','giveaway_ended')", (int(time.time()),guild_id,order_id))
        conn.commit(); return {"status":"awaiting_owner_decision", "winner_ids": winner_ids}
    except Exception: conn.rollback(); raise
    finally: conn.close()


def approve_ad_delivery(guild_id: int, order_id: int):
    """Pay every winner through the existing idempotent giveaway ledger."""
    ensure_ad_schema(guild_id); conn, cur = get_guild_db(guild_id)
    try:
        row = cur.execute("SELECT message_id,generation,reserved_amount,winner_ids,status FROM ad_giveaways WHERE ad_order_id=?", (order_id,)).fetchone()
        order = cur.execute("SELECT amount_per_winner FROM ad_orders WHERE guild_id=? AND id=?", (guild_id,order_id)).fetchone()
        if not row or not order or row[4] not in {"awaiting_owner_decision", "paying"} or not row[0]: return {"status":"needs_review"}
        winners = json.loads(row[3] or "[]")
        cur.execute("UPDATE ad_giveaways SET status='paying',payout_key=?,updated_at=? WHERE ad_order_id=? AND status='awaiting_owner_decision'", (f"ad-payout:{guild_id}:{order_id}",int(time.time()),order_id)); conn.commit()
    finally: conn.close()
    from database import credit_giveaway_prize
    try:
        for slot, user_id in enumerate(winners):
            credit_giveaway_prize(guild_id, int(row[0]), int(row[1]), slot, int(user_id), int(order[0]))
    except Exception as exc:
        mark_ad_needs_review(guild_id, order_id, f"payout failed: {exc}")
        return {"status":"needs_review"}
    conn, cur = get_guild_db(guild_id)
    try:
        cur.execute("UPDATE ad_giveaways SET status='paid',updated_at=? WHERE ad_order_id=? AND status='paying'", (int(time.time()),order_id))
        cur.execute("UPDATE ad_orders SET status='completed',completed_at=?,updated_at=? WHERE guild_id=? AND id=?", (int(time.time()),int(time.time()),guild_id,order_id)); conn.commit()
        return {"status":"paid", "count": len(winners)}
    finally: conn.close()


def reject_ad_delivery(guild_id: int, order_id: int):
    """Refund a server-funded reservation once; external-payment refunds fail closed."""
    ensure_ad_schema(guild_id); conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = cur.execute("SELECT funding_source,reserved_amount,status,refund_key FROM ad_giveaways WHERE ad_order_id=?", (order_id,)).fetchone()
        if not row: conn.rollback(); return {"status":"needs_review"}
        if row[2] == "refunded": conn.commit(); return {"status":"already_refunded"}
        if row[0] != "server_budget":
            cur.execute("UPDATE ad_giveaways SET status='needs_review',updated_at=? WHERE ad_order_id=?", (int(time.time()),order_id)); conn.commit(); return {"status":"needs_review"}
        from database import guarded_server_budget_credit
        guarded_server_budget_credit(cur, guild_id, int(row[1]))
        key = row[3] or f"ad-refund:{guild_id}:{order_id}"
        cur.execute("UPDATE ad_giveaways SET status='refunded',refund_key=?,updated_at=? WHERE ad_order_id=? AND status NOT IN ('refunded','paid')", (key,int(time.time()),order_id))
        cur.execute("UPDATE ad_orders SET status='completed',completed_at=?,updated_at=? WHERE guild_id=? AND id=?", (int(time.time()),int(time.time()),guild_id,order_id)); conn.commit(); return {"status":"refunded", "refund_key":key}
    except Exception: conn.rollback(); raise
    finally: conn.close()


def cleanup_ad_order(guild_id: int, order_id: int):
    ensure_ad_schema(guild_id); conn, cur = get_guild_db(guild_id)
    try:
        row = cur.execute("SELECT status FROM ad_giveaways WHERE ad_order_id=?", (order_id,)).fetchone()
        if not row or row[0] not in {"paid", "refunded"}:
            return False
        cur.execute("UPDATE ad_orders SET status='completed',completed_at=COALESCE(completed_at,?),updated_at=? WHERE guild_id=? AND id=?", (int(time.time()),int(time.time()),guild_id,order_id)); conn.commit(); return cur.rowcount == 1
    finally: conn.close()


def get_ad_giveaway(guild_id: int, order_id: int):
    ensure_ad_schema(guild_id); conn, cur = get_guild_db(guild_id)
    try:
        row = cur.execute("SELECT ad_order_id,guild_id,message_id,channel_id,generation,original_channel_name,end_timestamp,status,funding_source,reserved_amount,winner_ids FROM ad_giveaways WHERE guild_id=? AND ad_order_id=?", (guild_id,order_id)).fetchone()
        if not row: return None
        return dict(zip("ad_order_id guild_id message_id channel_id generation original_channel_name end_timestamp status funding_source reserved_amount winner_ids".split(), row))
    finally: conn.close()


def claim_expired_ad_giveaways(guild_id: int, now: int | None = None):
    """Claim expired live ad giveaways once so only one worker selects winners."""
    ensure_ad_schema(guild_id); now = int(now or time.time())
    conn, cur = get_guild_db(guild_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = cur.execute("SELECT ad_order_id,message_id,channel_id,generation FROM ad_giveaways WHERE guild_id=? AND status='live' AND end_timestamp IS NOT NULL AND end_timestamp<=? ORDER BY end_timestamp,ad_order_id", (guild_id,now)).fetchall()
        claimed = []
        for order_id, message_id, channel_id, generation in rows:
            cur.execute("UPDATE ad_giveaways SET status='ending',updated_at=? WHERE ad_order_id=? AND status='live'", (now,order_id))
            if cur.rowcount == 1:
                claimed.append({"order_id":int(order_id),"message_id":int(message_id),"channel_id":int(channel_id),"generation":int(generation)})
        conn.commit(); return claimed
    except Exception:
        conn.rollback(); raise
    finally: conn.close()


def list_ad_settlement_messages(guild_id: int):
    ensure_ad_schema(guild_id); conn, cur = get_guild_db(guild_id)
    try:
        return cur.execute("SELECT ad_order_id,message_id,channel_id FROM ad_giveaways WHERE guild_id=? AND status='awaiting_owner_decision' AND message_id IS NOT NULL", (guild_id,)).fetchall()
    finally: conn.close()


def list_ad_cleanup_orders(guild_id: int):
    ensure_ad_schema(guild_id); conn, cur = get_guild_db(guild_id)
    try:
        return cur.execute("SELECT ad_order_id,channel_id,original_channel_name FROM ad_giveaways WHERE guild_id=? AND status IN ('paid','refunded')", (guild_id,)).fetchall()
    finally: conn.close()
