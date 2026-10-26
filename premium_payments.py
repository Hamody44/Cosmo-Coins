"""Premium purchase persistence and idempotent settlement."""
import hashlib
import re
import sqlite3
import time
import uuid
from contextlib import closing
from decimal import Decimal, InvalidOperation, ROUND_UP
import database

PREMIUM_METHODS = {"credit", "adamc", "cosmo", "crypto"}
PREMIUM_PROVIDER_ORDER_RE = re.compile(r"premium:(\d{1,20}):([0-9a-f]{32})")

def _global_schema():
    with closing(sqlite3.connect(database.GLOBAL_DB_PATH, timeout=10)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS premium_prices (duration TEXT PRIMARY KEY, duration_seconds INTEGER NOT NULL, credit INTEGER NOT NULL, adamc INTEGER NOT NULL, cosmo INTEGER NOT NULL, crypto_usd TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS global_settings (key TEXT PRIMARY KEY, value INTEGER)")
        conn.commit()

def _key(kind, method=None):
    return f"premium_{kind}_{method}" if method else f"premium_{kind}"

def purchase_enabled():
    _global_schema()
    with closing(sqlite3.connect(database.GLOBAL_DB_PATH, timeout=10)) as c:
        row = c.execute("SELECT value FROM global_settings WHERE key=?", (_key("purchase_enabled"),)).fetchone()
    return bool(row and int(row[0]) == 1)

def method_enabled(method):
    return method in PREMIUM_METHODS and _setting(_key("method_enabled", method), False)

def _setting(key, default=False):
    _global_schema()
    with closing(sqlite3.connect(database.GLOBAL_DB_PATH, timeout=10)) as c:
        row = c.execute("SELECT value FROM global_settings WHERE key=?", (key,)).fetchone()
    return bool(row and int(row[0]) == 1) if row else default

def set_purchase_enabled(enabled):
    _global_schema(); _set(_key("purchase_enabled"), enabled)

def set_method_enabled(method, enabled):
    if method not in PREMIUM_METHODS: raise ValueError("unknown premium method")
    _global_schema(); _set(_key("method_enabled", method), enabled)

def _set(key, value):
    with closing(sqlite3.connect(database.GLOBAL_DB_PATH, timeout=10)) as c:
        c.execute("INSERT INTO global_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, int(bool(value))))
        c.commit()

def set_price(duration, duration_seconds, prices):
    if not re.fullmatch(r"\d+[smhdw]", duration.lower()) or duration_seconds <= 0: raise ValueError("invalid duration")
    vals = [int(prices[m]) for m in ("credit", "adamc", "cosmo")]
    usd = Decimal(str(prices["crypto_usd"])).quantize(Decimal("0.01"))
    if any(v <= 0 for v in vals) or usd < Decimal("0.10"): raise ValueError("prices must be positive and crypto must be at least $0.10")
    _global_schema()
    with closing(sqlite3.connect(database.GLOBAL_DB_PATH, timeout=10)) as c:
        c.execute("INSERT INTO premium_prices VALUES(?,?,?,?,?,?) ON CONFLICT(duration) DO UPDATE SET duration_seconds=excluded.duration_seconds,credit=excluded.credit,adamc=excluded.adamc,cosmo=excluded.cosmo,crypto_usd=excluded.crypto_usd", (duration.lower(), int(duration_seconds), *vals, format(usd, 'f'))); c.commit()

def get_price(duration):
    _global_schema()
    with closing(sqlite3.connect(database.GLOBAL_DB_PATH, timeout=10)) as c:
        row = c.execute("SELECT duration,duration_seconds,credit,adamc,cosmo,crypto_usd FROM premium_prices WHERE duration=?", (duration.lower(),)).fetchone()
    if not row: return None
    return {"duration":row[0], "duration_seconds":int(row[1]), "credit":int(row[2]), "adamc":int(row[3]), "cosmo":int(row[4]), "crypto_usd":Decimal(row[5])}

def _ensure_orders(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS premium_payment_orders (id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id INTEGER NOT NULL,user_id INTEGER NOT NULL,payer_name TEXT NOT NULL DEFAULT '',channel_id INTEGER NOT NULL,method TEXT NOT NULL,duration TEXT NOT NULL,duration_seconds INTEGER NOT NULL,expected_payment INTEGER,quote_usd TEXT,provider_order_id TEXT UNIQUE,provider_payment_id TEXT UNIQUE,status TEXT NOT NULL DEFAULT 'pending',created_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,payment_message_id INTEGER UNIQUE,payment_author_id INTEGER,paid_at INTEGER)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(premium_payment_orders)").fetchall()}
    if "payer_name" not in columns:
        conn.execute("ALTER TABLE premium_payment_orders ADD COLUMN payer_name TEXT NOT NULL DEFAULT ''")

def create_order(guild_id, user_id, channel_id, method, duration, duration_seconds, expected_payment=None, quote_usd=None, created_at=None, expires_at=None, payer_name=""):
    method = str(method).lower(); now = int(time.time()) if created_at is None else int(created_at); expires = now + 600 if expires_at is None else int(expires_at)
    if (not purchase_enabled() or not method_enabled(method) or method not in PREMIUM_METHODS
            or min(int(guild_id), int(user_id), int(channel_id), int(duration_seconds)) <= 0
            or expires <= now): return None
    provider = f"premium:{int(guild_id)}:{uuid.uuid4().hex}" if method == "crypto" else None
    conn, _ = database.get_guild_db(int(guild_id))
    try:
        _ensure_orders(conn); conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM premium_payment_orders WHERE guild_id=? AND user_id=? AND status IN ('pending','creating','processing')", (guild_id,user_id)).fetchone(): conn.rollback(); return None
        status = 'creating' if method == 'crypto' else 'pending'
        cur = conn.execute("INSERT INTO premium_payment_orders(guild_id,user_id,payer_name,channel_id,method,duration,duration_seconds,expected_payment,quote_usd,provider_order_id,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (guild_id,user_id,str(payer_name).strip().casefold(),channel_id,method,duration.lower(),duration_seconds,expected_payment, str(quote_usd) if quote_usd is not None else None,provider,status,now,expires))
        conn.commit(); return {"order_id":cur.lastrowid,"provider_order_id":provider}
    finally: conn.close()

def attach_invoice(guild_id, order_id, invoice):
    conn,_=database.get_guild_db(guild_id)
    try:
        _ensure_orders(conn); cur=conn.execute("UPDATE premium_payment_orders SET provider_payment_id=?,provider_order_id=provider_order_id,status='pending',expires_at=? WHERE id=? AND method='crypto' AND status='creating'", (str(invoice['payment_id']),int(invoice['expires_at']),int(order_id))); conn.commit(); return cur.rowcount==1
    finally: conn.close()

def fail_order(guild_id, order_id):
    conn,_=database.get_guild_db(guild_id)
    try: _ensure_orders(conn); cur=conn.execute("UPDATE premium_payment_orders SET status='failed' WHERE id=? AND status='creating'",(int(order_id),)); conn.commit(); return cur.rowcount==1
    finally: conn.close()

def settle_receipt(guild_id, method, channel_id, payer_name, amount, message_id, author_id, timestamp, recipient_id):
    expected_bot={"credit":database.PROBOT_PAYMENT_BOT_ID,"adamc":database.ADAMC_PAYMENT_BOT_ID}.get(method)
    if not expected_bot or int(author_id)!=expected_bot or int(recipient_id)!=database.PRIMARY_OWNER_ID: return None
    payer_name = str(payer_name).strip().casefold()
    if not payer_name: return None
    conn,_=database.get_guild_db(guild_id)
    try:
        _ensure_orders(conn); conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT id,user_id,duration_seconds,status FROM premium_payment_orders WHERE guild_id=? AND method=? AND channel_id=? AND payer_name=? AND expected_payment=? AND status='pending' AND created_at<=? AND expires_at>=? ORDER BY id LIMIT 1",(guild_id,method,channel_id,payer_name,int(amount),int(timestamp),int(timestamp))).fetchone()
        if not row: conn.rollback(); return None
        order_id,user_id,duration,status=row
        cur=conn.execute("UPDATE premium_payment_orders SET status='paid',payment_message_id=?,payment_author_id=?,paid_at=? WHERE id=? AND status='pending'",(message_id,author_id,timestamp,order_id))
        if cur.rowcount!=1: conn.rollback(); return None
        result=_activate_locked(conn,guild_id,duration,user_id,order_id,method); conn.commit(); return result
    finally: conn.close()

def settle_cosmo(guild_id,user_id,channel_id,order_id):
    conn,_=database.get_guild_db(guild_id)
    try:
        _ensure_orders(conn); conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT duration_seconds,expected_payment,status,expires_at FROM premium_payment_orders WHERE id=? AND guild_id=? AND user_id=? AND channel_id=? AND method='cosmo'",(order_id,guild_id,user_id,channel_id)).fetchone()
        if not row or row[2] != 'pending' or int(time.time()) > int(row[3]): conn.rollback(); return None
        # debit the global balance under the same process lock used by balance operations
        with database._balance_lock, closing(sqlite3.connect(database.GLOBAL_DB_PATH,timeout=10)) as gc:
            gc.execute("BEGIN IMMEDIATE"); bal=gc.execute("SELECT balance FROM balances WHERE user_id=?",(user_id,)).fetchone(); current=int(bal[0] or 0) if bal else 0
            if current < int(row[1]): gc.rollback(); conn.rollback(); return None
            gc.execute("UPDATE balances SET balance=balance-? WHERE user_id=?",(int(row[1]),user_id)); gc.commit()
        conn.execute("UPDATE premium_payment_orders SET status='paid',paid_at=? WHERE id=?",(int(time.time()),order_id)); result=_activate_locked(conn,guild_id,row[0],user_id,order_id,'cosmo'); conn.commit(); return result
    finally: conn.close()

def _activate_locked(conn,guild_id,duration,user_id,order_id,method):
    now=int(time.time()); old=conn.execute("SELECT premium_end FROM premium_info WHERE guild_id=?",(guild_id,)).fetchone(); old_end=int(old[0] or 0) if old else 0; new_end=max(now,old_end)+int(duration)
    conn.execute("INSERT INTO premium_info(guild_id,activated_at,premium_end) VALUES(?,?,?) ON CONFLICT(guild_id) DO UPDATE SET premium_end=excluded.premium_end",(guild_id,now,new_end))
    database.record_audit("premium_payment_settled",guild_id=guild_id,actor_id=user_id,target_user_id=user_id,reason=f"premium {method} order #{order_id}",after={"status":"paid","premium_end":new_end},source="discord" if method != "crypto" else "webhook",correlation_id=f"premium:{order_id}")
    return {"status":"paid","order_id":int(order_id),"guild_id":int(guild_id),"user_id":int(user_id),"duration_seconds":int(duration),"premium_end":new_end}

def process_webhook(event, invoice):
    metadata=invoice.get('metadata') if isinstance(invoice.get('metadata'),dict) else {}; provider=str(metadata.get('orderId',''))
    match=PREMIUM_PROVIDER_ORDER_RE.fullmatch(provider)
    if not match: return None
    guild_id=int(match.group(1)); invoice_id=str(invoice.get('id') or event.get('invoiceId','')); store=str(invoice.get('storeId') or event.get('storeId',''))
    if not invoice_id or not store or invoice_id != str(event.get('invoiceId','')): return None
    conn,_=database.get_guild_db(guild_id)
    try:
        _ensure_orders(conn); conn.execute('BEGIN IMMEDIATE'); row=conn.execute("SELECT id,user_id,duration_seconds,status,provider_payment_id,quote_usd FROM premium_payment_orders WHERE guild_id=? AND method='crypto' AND provider_order_id=?",(guild_id,provider)).fetchone()
        if not row: conn.rollback(); return {"status":"retry","provider_order_id":provider}
        oid,user,duration,status,saved,quote=row
        if status=='paid': conn.rollback(); return {"status":"duplicate","order_id":oid}
        if saved and str(saved)!=invoice_id: conn.rollback(); return None
        checkout = invoice.get("checkout") if isinstance(invoice.get("checkout"), dict) else {}
        methods = checkout.get("paymentMethods") if "paymentMethods" in checkout else invoice.get("paymentMethods")
        method_ok = methods is None or (isinstance(methods, list) and len(methods) == 1 and str(methods[0]).upper() in {"LTC", "LTC-CHAIN"})
        metadata_user = str(metadata.get("discord_user_id", "")).strip()
        currency_ok = str(invoice.get("currency", "USD")).upper() == "USD"
        if str(invoice.get('status','')).lower()!='settled': conn.rollback(); return {"status":"processing","order_id":oid}
        if not method_ok or not currency_ok or (metadata_user and metadata_user != str(user)):
            conn.rollback(); return {"status":"needs_review","order_id":oid}
        try: paid=Decimal(str(invoice.get('paidAmount',invoice.get('amount',quote)))); expected=Decimal(str(quote));
        except (InvalidOperation,TypeError): conn.rollback(); return {"status":"needs_review","order_id":oid}
        if paid < expected or Decimal(str(invoice.get('amount',quote))) != expected: conn.rollback(); return {"status":"needs_review","order_id":oid}
        conn.execute("UPDATE premium_payment_orders SET status='paid',provider_payment_id=?,paid_at=? WHERE id=? AND status IN ('pending','processing','expired','creating')",(invoice_id,int(time.time()),oid)); result=_activate_locked(conn,guild_id,duration,user,oid,'crypto'); conn.commit(); return result
    finally: conn.close()
