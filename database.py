import sqlite3
import os
import shutil
import time
import re
import math
import random
import io
import threading
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from contextlib import closing
import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps

PRIMARY_OWNER_ID = 988840428591808584
COSMO_GUILD_ID = 1540014549179039867
PROBOT_PAYMENT_BOT_ID = 282859044593598464
ADAMC_PAYMENT_BOT_ID = 1505622388975468684

if not os.path.exists("guilds_data"):
    os.makedirs("guilds_data")

# قاعدة البيانات المركزية العامة
GLOBAL_DB_PATH = os.path.abspath("global_cosmo.db")
_balance_lock = threading.RLock()
_auto_budget_payment_lock = threading.RLock()
_exchange_state_lock = threading.RLock()
global_conn = sqlite3.connect(GLOBAL_DB_PATH, check_same_thread=False, timeout=10)
global_cursor = global_conn.cursor()

global_cursor.execute('''CREATE TABLE IF NOT EXISTS balances (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 0
)''')

global_cursor.execute('''CREATE TABLE IF NOT EXISTS transfer_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id INTEGER,
    receiver_id INTEGER,
    amount INTEGER,
    timestamp TEXT
)''')

global_cursor.execute('''CREATE TABLE IF NOT EXISTS exchange_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    user_id INTEGER,
    cosmo_amount INTEGER,
    target_currency TEXT,
    received_amount REAL,
    note TEXT,
    status TEXT,
    timestamp TEXT
)''')

global_cursor.execute('''CREATE TABLE IF NOT EXISTS premium_codes (
    code TEXT PRIMARY KEY,
    duration_seconds INTEGER,
    created_at INTEGER,
    is_used INTEGER DEFAULT 0,
    used_guild_id INTEGER
)''')

global_cursor.execute('''CREATE TABLE IF NOT EXISTS global_settings (
    key TEXT PRIMARY KEY,
    value INTEGER
)''')
global_conn.commit()

# قواعد بيانات السيرفرات الفردية
def get_guild_db(guild_id: int):
    db_path = f"guilds_data/guild_{guild_id}.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    cur = conn.cursor()

    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        guild_id INTEGER PRIMARY KEY,
        transfer_log INTEGER,
        exchange_request_log INTEGER,
        exchange_success_log INTEGER,
        delivery_log INTEGER,
        balance_log INTEGER,
        ticket_log INTEGER,
        admin_role_id INTEGER,
        blacklist_role_id INTEGER,
        exchange_enabled INTEGER DEFAULT 1,
        double_event_end INTEGER DEFAULT 0,
        spending_balance INTEGER DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS premium_info (
        guild_id INTEGER PRIMARY KEY,
        activated_at INTEGER DEFAULT 0,
        premium_end INTEGER DEFAULT 0,
        custom_name TEXT,
        custom_avatar_url TEXT,
        custom_banner_url TEXT,
        custom_currency_name TEXT
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS exchange_currencies (
        currency_name TEXT PRIMARY KEY,
        exchange_rate REAL,
        is_active INTEGER DEFAULT 1,
        min_received REAL DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS command_aliases (
        original_command TEXT,
        alias TEXT PRIMARY KEY
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS bot_whitelist (
        bot_id INTEGER,
        channel_id INTEGER,
        PRIMARY KEY (bot_id, channel_id)
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS active_giveaways (
        message_id INTEGER PRIMARY KEY,
        channel_id INTEGER,
        amount INTEGER,
        end_timestamp INTEGER,
        winners_count INTEGER,
        condition TEXT,
        participants_json TEXT,
        is_finished INTEGER DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS boost_system_config (
        guild_id INTEGER PRIMARY KEY,
        is_enabled INTEGER DEFAULT 0,
        single_boost_reward INTEGER DEFAULT 0,
        double_boost_reward INTEGER DEFAULT 0,
        boost_log_channel INTEGER DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS active_boost_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        boost_type INTEGER,
        weekly_reward INTEGER,
        payments_remaining INTEGER,
        next_payment_timestamp INTEGER,
        is_fake INTEGER DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS tickets (
        channel_id INTEGER PRIMARY KEY,
        user_id INTEGER,
        opened_at TEXT,
        closed_by INTEGER DEFAULT NULL,
        is_closed INTEGER DEFAULT 0
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS budget_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT,
        amount INTEGER,
        old_balance INTEGER,
        new_balance INTEGER,
        admin_id INTEGER,
        timestamp TEXT
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS budget_payment_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        payer_name TEXT NOT NULL,
        channel_id INTEGER NOT NULL,
        method TEXT NOT NULL,
        budget_amount INTEGER NOT NULL CHECK (budget_amount > 0),
        expected_payment INTEGER NOT NULL CHECK (expected_payment > 0),
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        payment_message_id INTEGER UNIQUE,
        payment_author_id INTEGER,
        paid_at INTEGER,
        provider_order_id TEXT,
        provider_payment_id TEXT,
        quote_usd TEXT,
        pay_address TEXT,
        pay_currency TEXT,
        provider_status TEXT
    )''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_budget_payment_orders_pending
                   ON budget_payment_orders(status, expires_at, method, channel_id)''')

    payment_columns = {row[1] for row in cur.execute("PRAGMA table_info(budget_payment_orders)").fetchall()}
    for column, definition in (
        ("provider_order_id", "TEXT"),
        ("provider_payment_id", "TEXT"),
        ("quote_usd", "TEXT"),
        ("pay_address", "TEXT"),
        ("pay_currency", "TEXT"),
        ("provider_status", "TEXT"),
    ):
        if column not in payment_columns:
            cur.execute(f"ALTER TABLE budget_payment_orders ADD COLUMN {column} {definition}")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_payment_provider_order ON budget_payment_orders(provider_order_id)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_payment_provider_payment ON budget_payment_orders(provider_payment_id)")

    cur.execute('''CREATE TABLE IF NOT EXISTS auto_replies (
        trigger_word TEXT PRIMARY KEY,
        reply_content TEXT
    )''')

    # ترقية الجداول في حال وجود قواعد بيانات منشأة مسبقاً
    try:
        cur.execute("ALTER TABLE premium_info ADD COLUMN custom_banner_url TEXT")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE settings ADD COLUMN ticket_log INTEGER")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE premium_info ADD COLUMN custom_currency_name TEXT")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    return conn, cur

# دوال الفحص والتحقق
async def check_is_owner(bot, ctx_or_user):
    user = ctx_or_user.author if hasattr(ctx_or_user, "author") else ctx_or_user
    if user.id == PRIMARY_OWNER_ID:
        return True
    try:
        return await bot.is_owner(user)
    except:
        return False

def is_guild_premium(guild_id: int) -> bool:
    g_conn, g_cur = get_guild_db(guild_id)
    g_cur.execute("SELECT premium_end FROM premium_info WHERE guild_id = ?", (guild_id,))
    row = g_cur.fetchone()
    g_conn.close()
    if row and row[0]:
        return int(time.time()) < row[0]
    return False

def get_currency_label(guild_id: int | None = None) -> str:
    """اسم العرض لرصيد Cosmo في سيرفر بعينه؛ لا ينشئ عملة أو رصيداً منفصلاً."""
    if guild_id is None:
        return "Cosmo"
    g_conn, g_cur = get_guild_db(guild_id)
    try:
        g_cur.execute(
            "SELECT custom_currency_name, premium_end FROM premium_info WHERE guild_id = ?",
            (guild_id,),
        )
        row = g_cur.fetchone()
        label = row[0].strip() if row and row[0] and row[1] and int(row[1]) > int(time.time()) else ""
        if not label:
            return "Cosmo"
        return label.replace("@", "@\u200b").replace("`", "ˋ").replace("<", "‹").replace(">", "›")
    finally:
        g_conn.close()

def get_exchange_currencies(active_only: bool = False):
    """الكتالوج المشترك؛ قاعدة سيرفر Cosmo هي المصدر الوحيد لكل السيرفرات."""
    g_conn, g_cur = get_guild_db(COSMO_GUILD_ID)
    try:
        query = "SELECT currency_name, exchange_rate, is_active, min_received FROM exchange_currencies"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY LOWER(currency_name), currency_name"
        g_cur.execute(query)
        return g_cur.fetchall()
    finally:
        g_conn.close()

def get_exchange_currency(currency_name: str):
    g_conn, g_cur = get_guild_db(COSMO_GUILD_ID)
    try:
        g_cur.execute(
            "SELECT currency_name, exchange_rate, is_active, min_received "
            "FROM exchange_currencies WHERE LOWER(currency_name) = LOWER(?)",
            (currency_name.strip(),),
        )
        return g_cur.fetchone()
    finally:
        g_conn.close()

def set_exchange_currency_status(currency_name: str, active: bool):
    with _exchange_state_lock:
        existing = get_exchange_currency(currency_name)
        if not existing:
            return None
        g_conn, g_cur = get_guild_db(COSMO_GUILD_ID)
        try:
            g_cur.execute(
                "UPDATE exchange_currencies SET is_active = ? WHERE currency_name = ?",
                (1 if active else 0, existing[0]),
            )
            g_conn.commit()
            return existing[0]
        finally:
            g_conn.close()

def upsert_exchange_currency(currency_name: str, rate: float, min_received: float):
    with _exchange_state_lock:
        existing = get_exchange_currency(currency_name)
        g_conn, g_cur = get_guild_db(COSMO_GUILD_ID)
        try:
            if existing:
                g_cur.execute(
                    "UPDATE exchange_currencies SET exchange_rate = ?, min_received = ? WHERE currency_name = ?",
                    (rate, min_received, existing[0]),
                )
                result_name = existing[0]
            else:
                g_cur.execute(
                    "INSERT INTO exchange_currencies (currency_name, exchange_rate, is_active, min_received) "
                    "VALUES (?, ?, 1, ?)",
                    (currency_name, rate, min_received),
                )
                result_name = currency_name
            g_conn.commit()
            return result_name
        finally:
            g_conn.close()

def delete_exchange_currency(currency_name: str):
    with _exchange_state_lock:
        existing = get_exchange_currency(currency_name)
        if not existing:
            return None
        g_conn, g_cur = get_guild_db(COSMO_GUILD_ID)
        try:
            g_cur.execute("DELETE FROM exchange_currencies WHERE currency_name = ?", (existing[0],))
            g_conn.commit()
            return existing[0]
        finally:
            g_conn.close()

def is_global_exchange_enabled() -> bool:
    """حالة التبديل العامة محفوظة في إعدادات سيرفر Cosmo؛ الحالة الافتراضية مفتوحة."""
    with _exchange_state_lock:
        g_conn, g_cur = get_guild_db(COSMO_GUILD_ID)
        try:
            g_cur.execute("SELECT exchange_enabled FROM settings WHERE guild_id = ?", (COSMO_GUILD_ID,))
            row = g_cur.fetchone()
            return row is None or row[0] is None or int(row[0]) != 0
        finally:
            g_conn.close()

def set_global_exchange_enabled(enabled: bool):
    """يحفظ حالة السوق العامة مركزيًا؛ يجب أن يمر أمر البوت بتفويض سيرفر Cosmo."""
    with _exchange_state_lock:
        g_conn, g_cur = get_guild_db(COSMO_GUILD_ID)
        try:
            g_cur.execute(
                "INSERT INTO settings (guild_id, exchange_enabled) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET exchange_enabled = excluded.exchange_enabled",
                (COSMO_GUILD_ID, 1 if enabled else 0),
            )
            g_conn.commit()
        finally:
            g_conn.close()

AUTO_BUDGET_PAYMENT_METHODS = {"credit", "adamc", "crypto"}

def is_auto_budget_payment_enabled(method: str) -> bool:
    method = method.strip().lower()
    if method not in AUTO_BUDGET_PAYMENT_METHODS:
        return False
    key = f"auto_budget_{method}_enabled"
    with _auto_budget_payment_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        row = conn.execute("SELECT value FROM global_settings WHERE key = ?", (key,)).fetchone()
        return bool(row and row[0] == 1)

def _cancel_pending_budget_orders(guild_ids, method: str):
    # لا يمكن إلغاء فاتورة BTCPay بأمان من المفتاح المحلي؛ الطلبات الصادرة
    # تبقى قابلة للتسوية حتى انتهاء مهلة المزود، بينما يمنع الإغلاق إنشاء طلبات جديدة.
    if method == "crypto":
        return []
    cancelled = []
    for guild_id in sorted({int(gid) for gid in guild_ids if int(gid) > 0}):
        g_conn, _ = get_guild_db(guild_id)
        try:
            g_conn.execute("BEGIN IMMEDIATE")
            rows = g_conn.execute(
                "SELECT id, user_id, channel_id FROM budget_payment_orders "
                "WHERE guild_id = ? AND method = ? AND status = 'pending'",
                (guild_id, method),
            ).fetchall()
            if rows:
                g_conn.execute(
                    "UPDATE budget_payment_orders SET status = 'cancelled' "
                    "WHERE guild_id = ? AND method = ? AND status = 'pending'",
                    (guild_id, method),
                )
                cancelled.extend(
                    {"guild_id": guild_id, "order_id": int(row[0]), "user_id": int(row[1]), "channel_id": int(row[2])}
                    for row in rows
                )
            g_conn.commit()
        finally:
            g_conn.close()
    return cancelled

def set_auto_budget_payment_enabled(method: str, enabled: bool, guild_ids=()):
    method = method.strip().lower()
    if method not in AUTO_BUDGET_PAYMENT_METHODS:
        raise ValueError("طريقة الدفع غير معروفة.")
    key = f"auto_budget_{method}_enabled"
    with _auto_budget_payment_lock:
        was_enabled = is_auto_budget_payment_enabled(method)
        if enabled and was_enabled:
            return []
        if enabled:
            # Expire any stale orders before re-enabling the method.
            cancelled = _cancel_pending_budget_orders(guild_ids, method)
            value = 1
        else:
            with closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
                conn.execute(
                    "INSERT INTO global_settings (key, value) VALUES (?, 0) "
                    "ON CONFLICT(key) DO UPDATE SET value = 0",
                    (key,),
                )
                conn.commit()
            cancelled = _cancel_pending_budget_orders(guild_ids, method)
            return cancelled

        with closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
            conn.execute(
                "INSERT INTO global_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()
        return cancelled

def _create_budget_payment_order_locked(guild_id: int, user_id: int, payer_name: str, channel_id: int,
                                         method: str, budget_amount: int, expected_payment: int,
                                         created_at: int, expires_at: int):
    method = method.strip().lower()
    budget_amount, expected_payment = int(budget_amount), int(expected_payment)
    if (method not in {"credit", "adamc"} or budget_amount <= 0
            or budget_amount > 9_223_372_036_854_775_807 or budget_amount % 5 != 0):
        return None
    if not is_auto_budget_payment_enabled(method):
        return None
    expected_for_rate = budget_amount * 6 // 5 if method == "credit" else budget_amount // 5
    if expected_payment != expected_for_rate or expected_payment <= 0 or expected_payment > 9_223_372_036_854_775_807:
        return None
    if not payer_name.strip() or guild_id <= 0 or user_id <= 0 or channel_id <= 0 or expires_at <= created_at:
        return None
    g_conn, _ = get_guild_db(guild_id)
    try:
        g_conn.execute("BEGIN IMMEDIATE")
        g_conn.execute(
            "UPDATE budget_payment_orders SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at < ?",
            (created_at,),
        )
        pending = g_conn.execute(
            "SELECT 1 FROM budget_payment_orders WHERE user_id = ? "
            "AND status IN ('pending', 'creating', 'processing') LIMIT 1",
            (user_id,),
        ).fetchone()
        if pending:
            g_conn.rollback()
            return None
        cur = g_conn.execute(
            "INSERT INTO budget_payment_orders "
            "(guild_id, user_id, payer_name, channel_id, method, budget_amount, expected_payment, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, payer_name.strip().casefold(), channel_id, method, budget_amount, expected_payment, created_at, expires_at),
        )
        order_id = cur.lastrowid
        g_conn.commit()
        return order_id
    finally:
        g_conn.close()

def create_budget_payment_order(guild_id: int, user_id: int, payer_name: str, channel_id: int,
                                method: str, budget_amount: int, expected_payment: int,
                                created_at: int, expires_at: int):
    with _auto_budget_payment_lock:
        if not is_auto_budget_payment_enabled(method):
            return None
        return _create_budget_payment_order_locked(
            guild_id, user_id, payer_name, channel_id, method,
            budget_amount, expected_payment, created_at, expires_at,
        )

def create_crypto_budget_payment_order(
    guild_id: int, user_id: int, payer_name: str, channel_id: int, budget_amount: int,
    quote_usd: str, provider_order_id: str, created_at: int, expires_at: int,
):
    """Reserve an LTC-only BTCPay invoice before the external invoice is created."""
    guild_id, user_id, channel_id = int(guild_id), int(user_id), int(channel_id)
    budget_amount = int(budget_amount)
    created_at, expires_at = int(created_at), int(expires_at)
    quote_usd, provider_order_id = str(quote_usd).strip(), str(provider_order_id).strip()
    if (
        guild_id <= 0 or user_id <= 0 or channel_id <= 0 or budget_amount < 26_000_000
        or budget_amount > 9_223_372_036_854_775_807 or not quote_usd
        or not re.fullmatch(r"cosmo:\d{1,20}:[0-9a-f]{32}", provider_order_id)
        or expires_at <= created_at
    ):
        return None
    try:
        quote_value = Decimal(quote_usd)
        expected_quote = (Decimal(budget_amount) / Decimal(260_000_000)).quantize(
            Decimal("0.00000001"), rounding=ROUND_UP
        )
        expected_usd_units = int((quote_value * Decimal(100_000_000)).to_integral_value())
        if (
            not quote_value.is_finite() or quote_value != expected_quote
            or quote_value < Decimal("0.10") or expected_usd_units <= 0
            or expected_usd_units > 9_223_372_036_854_775_807
        ):
            return None
    except (InvalidOperation, ValueError, OverflowError):
        return None

    with _auto_budget_payment_lock:
        if not is_auto_budget_payment_enabled("crypto"):
            return None
        g_conn, _ = get_guild_db(guild_id)
        try:
            g_conn.execute("BEGIN IMMEDIATE")
            g_conn.execute(
                "UPDATE budget_payment_orders SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at <= ?",
                (created_at,),
            )
            pending = g_conn.execute(
                "SELECT 1 FROM budget_payment_orders WHERE user_id = ? "
                "AND status IN ('pending', 'creating', 'processing') LIMIT 1",
                (user_id,),
            ).fetchone()
            if pending:
                g_conn.rollback()
                return None
            cur = g_conn.execute(
                "INSERT INTO budget_payment_orders "
                "(guild_id, user_id, payer_name, channel_id, method, budget_amount, expected_payment, "
                "created_at, expires_at, status, provider_order_id, quote_usd, pay_currency) "
                "VALUES (?, ?, ?, ?, 'crypto', ?, ?, ?, ?, 'creating', ?, ?, 'ltc')",
                (
                    guild_id, user_id, str(payer_name).strip().casefold(), channel_id,
                    budget_amount, expected_usd_units, created_at, expires_at,
                    provider_order_id, quote_usd,
                ),
            )
            order_id = int(cur.lastrowid)
            g_conn.commit()
            return order_id
        finally:
            g_conn.close()


def update_crypto_budget_payment_order(guild_id: int, order_id: int, provider_order_id: str, invoice: dict):
    """Attach the BTCPay invoice ID and checked checkout URL to its reserved order."""
    try:
        invoice_id = str(invoice["payment_id"]).strip()
        checkout_url = str(invoice["pay_address"]).strip()
        currency = str(invoice["pay_currency"]).strip().lower()
        provider_status = str(invoice["provider_status"]).strip()
        expires_at = int(invoice["expires_at"])
        store_id = str(invoice["store_id"]).strip()
        amount = Decimal(str(invoice["amount_usd"]))
        metadata_order_id = str(invoice["metadata_order_id"]).strip()
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False
    if (
        not invoice_id or not checkout_url.startswith("https://") or currency != "ltc"
        or not store_id or amount < Decimal("0.10") or metadata_order_id != str(provider_order_id)
    ):
        return False
    g_conn, _ = get_guild_db(int(guild_id))
    try:
        g_conn.execute("BEGIN IMMEDIATE")
        order = g_conn.execute(
            "SELECT quote_usd FROM budget_payment_orders "
            "WHERE id = ? AND guild_id = ? AND method = 'crypto' AND provider_order_id = ? "
            "AND status = 'creating'",
            (int(order_id), int(guild_id), str(provider_order_id)),
        ).fetchone()
        if not order or Decimal(str(order[0])) != amount:
            g_conn.rollback()
            return False
        cur = g_conn.execute(
            "UPDATE budget_payment_orders SET provider_payment_id = ?, pay_address = ?, "
            "pay_currency = 'ltc', provider_status = ?, expires_at = ?, status = 'pending' "
            "WHERE id = ? AND guild_id = ? AND method = 'crypto' AND provider_order_id = ? "
            "AND status = 'creating' AND provider_payment_id IS NULL",
            (invoice_id, checkout_url, provider_status, expires_at, int(order_id), int(guild_id), str(provider_order_id)),
        )
        if cur.rowcount != 1:
            g_conn.rollback()
            return False
        g_conn.commit()
        return True
    except sqlite3.IntegrityError:
        g_conn.rollback()
        return False
    finally:
        g_conn.close()


def fail_crypto_budget_payment_order(guild_id: int, order_id: int, provider_order_id: str):
    g_conn, _ = get_guild_db(int(guild_id))
    try:
        cur = g_conn.execute(
            "UPDATE budget_payment_orders SET status = 'failed', provider_status = 'creation_failed' "
            "WHERE id = ? AND method = 'crypto' AND provider_order_id = ? AND status = 'creating'",
            (int(order_id), str(provider_order_id)),
        )
        g_conn.commit()
        return cur.rowcount == 1
    finally:
        g_conn.close()


def process_btcpay_webhook(event: dict, invoice: dict):
    """Settle one verified BTCPay invoice event after fetching its canonical invoice state."""
    event_type = str(event.get("type", "")).strip()
    invoice_id = str(event.get("invoiceId", "")).strip()
    event_store_id = str(event.get("storeId", "")).strip()
    canonical_invoice_id = str(invoice.get("id", "")).strip()
    canonical_store_id = str(invoice.get("storeId", "")).strip()
    if not invoice_id or canonical_invoice_id != invoice_id or not event_store_id or canonical_store_id != event_store_id:
        return None

    # The local order reference is encoded in the signed and fetched BTCPay invoice metadata.
    metadata = invoice.get("metadata") if isinstance(invoice.get("metadata"), dict) else {}
    provider_order_id = str(metadata.get("orderId", "")).strip()
    match = re.fullmatch(r"cosmo:(\d{1,20}):([0-9a-f]{32})", provider_order_id)
    if not match:
        return None
    guild_id = int(match.group(1))
    if guild_id <= 0:
        return None

    g_conn, _ = get_guild_db(guild_id)
    try:
        g_conn.execute("BEGIN IMMEDIATE")
        order = g_conn.execute(
            "SELECT id, user_id, channel_id, budget_amount, expected_payment, provider_payment_id, "
            "quote_usd, status, provider_order_id FROM budget_payment_orders "
            "WHERE guild_id = ? AND method = 'crypto' AND provider_order_id = ? LIMIT 1",
            (guild_id, provider_order_id),
        ).fetchone()
        if not order:
            g_conn.rollback()
            return None
        order_id, user_id, channel_id, budget_amount, expected_usd_units, saved_invoice_id, quote_usd, order_status, saved_order_id = order
        if str(saved_order_id) != provider_order_id:
            g_conn.rollback()
            return None
        if saved_invoice_id and str(saved_invoice_id) != invoice_id:
            g_conn.rollback()
            return None
        if order_status == "paid":
            g_conn.rollback()
            return {"status": "duplicate", "order_id": int(order_id)}

        checkout = invoice.get("checkout") if isinstance(invoice.get("checkout"), dict) else {}
        methods = checkout.get("paymentMethods")
        method_ok = isinstance(methods, list) and [str(item).upper() for item in methods] == ["LTC-CHAIN"]
        try:
            invoice_amount = Decimal(str(invoice.get("amount")))
            paid_amount = Decimal(str(invoice.get("paidAmount")))
            expected_quote = Decimal(str(quote_usd))
            expected_units = int((expected_quote * Decimal(100_000_000)).to_integral_value())
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            invoice_amount = paid_amount = expected_quote = Decimal("0")
            expected_units = 0
        identity_matches = (
            str(invoice.get("currency", "")).upper() == "USD"
            and invoice_amount.is_finite() and invoice_amount == expected_quote
            and expected_units == int(expected_usd_units)
            and method_ok
            and isinstance(invoice.get("metadata"), dict)
            and str(invoice["metadata"].get("orderId", "")) == provider_order_id
        )

        if event_type == "InvoiceProcessing" and str(invoice.get("status", "")).lower() == "processing":
            if not identity_matches or order_status not in {"pending", "creating", "processing"}:
                g_conn.rollback()
                return {"status": "needs_review", "order_id": int(order_id), "user_id": int(user_id), "channel_id": int(channel_id)}
            g_conn.execute(
                "UPDATE budget_payment_orders SET status = 'processing', provider_status = 'Processing', "
                "provider_payment_id = COALESCE(provider_payment_id, ?) WHERE id = ? AND status IN ('pending', 'creating', 'processing')",
                (invoice_id, order_id),
            )
            g_conn.commit()
            return {"status": "processing", "order_id": int(order_id)}

        if event_type == "InvoiceExpired" and str(invoice.get("status", "")).lower() == "expired":
            additional = str(invoice.get("additionalStatus", "None")).casefold()
            status = "needs_review" if additional in {"paidpartial", "paidover", "paidlate", "marked"} else "expired"
            g_conn.execute(
                "UPDATE budget_payment_orders SET status = ?, provider_status = 'Expired', "
                "provider_payment_id = COALESCE(provider_payment_id, ?) "
                "WHERE id = ? AND status IN ('pending', 'creating', 'processing')",
                (status, invoice_id, order_id),
            )
            g_conn.commit()
            if status == "needs_review":
                return {"status": status, "order_id": int(order_id), "user_id": int(user_id), "channel_id": int(channel_id)}
            return {"status": status, "order_id": int(order_id)}

        if event_type == "InvoiceInvalid" and str(invoice.get("status", "")).lower() == "invalid":
            g_conn.execute(
                "UPDATE budget_payment_orders SET status = 'needs_review', provider_status = 'Invalid', "
                "provider_payment_id = COALESCE(provider_payment_id, ?) WHERE id = ? AND status != 'paid'",
                (invoice_id, order_id),
            )
            g_conn.commit()
            return {"status": "needs_review", "order_id": int(order_id), "user_id": int(user_id), "channel_id": int(channel_id)}

        if event_type != "InvoiceSettled" or str(invoice.get("status", "")).lower() != "settled":
            g_conn.rollback()
            return None

        additional = invoice.get("additionalStatus")
        payment_matches = (
            identity_matches and method_ok
            and (additional is None or str(additional).casefold() == "none")
            and paid_amount.is_finite() and paid_amount == invoice_amount
            and order_status in {"pending", "creating", "processing"}
        )
        if not payment_matches:
            g_conn.execute(
                "UPDATE budget_payment_orders SET status = 'needs_review', provider_status = 'Settled_review', "
                "provider_payment_id = COALESCE(provider_payment_id, ?) WHERE id = ? AND status != 'paid'",
                (invoice_id, order_id),
            )
            g_conn.commit()
            return {"status": "needs_review", "order_id": int(order_id), "user_id": int(user_id), "channel_id": int(channel_id)}

        old_row = g_conn.execute(
            "SELECT spending_balance FROM settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        old_balance = int(old_row[0] or 0) if old_row else 0
        max_sqlite_int = 9_223_372_036_854_775_807
        if old_balance > max_sqlite_int - int(budget_amount):
            g_conn.rollback()
            return {"status": "needs_review", "order_id": int(order_id), "user_id": int(user_id), "channel_id": int(channel_id)}
        cur = g_conn.execute(
            "UPDATE budget_payment_orders SET status = 'paid', provider_status = 'Settled', "
            "provider_payment_id = ?, paid_at = ? WHERE id = ? AND status IN ('pending', 'creating', 'processing')",
            (invoice_id, int(time.time()), order_id),
        )
        if cur.rowcount != 1:
            g_conn.rollback()
            return None
        new_balance = old_balance + int(budget_amount)
        g_conn.execute(
            "INSERT INTO settings (guild_id, spending_balance) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET spending_balance = excluded.spending_balance",
            (guild_id, new_balance),
        )
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        g_conn.execute(
            "INSERT INTO budget_logs (action, amount, old_balance, new_balance, admin_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("إضافة تلقائية (crypto)", int(budget_amount), old_balance, new_balance, int(user_id), timestamp),
        )
        g_conn.commit()
        return {
            "status": "paid", "order_id": int(order_id), "guild_id": guild_id,
            "user_id": int(user_id), "channel_id": int(channel_id),
            "budget_amount": int(budget_amount), "old_balance": old_balance,
            "new_balance": new_balance,
        }
    finally:
        g_conn.close()


def _settle_budget_payment_locked(guild_id: int, method: str, channel_id: int, payer_name: str,
                                  expected_payment: int, payment_message_id: int, payment_author_id: int,
                                  payment_timestamp: int, recipient_id: int):
    """تطابق إيصالًا واحدًا مع طلب حي وتضيف الميزانية وتؤشر الإيصال في معاملة واحدة."""
    method = method.strip().lower()
    payer_name = payer_name.strip().casefold()
    provider_ids = {"credit": PROBOT_PAYMENT_BOT_ID, "adamc": ADAMC_PAYMENT_BOT_ID}
    if (method not in provider_ids or not payer_name or guild_id <= 0 or channel_id <= 0
            or int(payment_author_id) != provider_ids.get(method)
            or int(recipient_id) != PRIMARY_OWNER_ID or int(payment_message_id) <= 0):
        return None
    g_conn, _ = get_guild_db(guild_id)
    try:
        g_conn.execute("BEGIN IMMEDIATE")
        order = g_conn.execute(
            "SELECT id, user_id, budget_amount, expected_payment FROM budget_payment_orders "
            "WHERE guild_id = ? AND method = ? AND payer_name = ? "
            "AND expected_payment = ? AND status = 'pending' AND created_at <= ? AND expires_at >= ? "
            "ORDER BY created_at ASC LIMIT 1",
            (guild_id, method, payer_name, int(expected_payment), int(payment_timestamp), int(payment_timestamp)),
        ).fetchone()
        if not order:
            g_conn.rollback()
            return None
        order_id, user_id, budget_amount, _ = order
        old_row = g_conn.execute(
            "SELECT spending_balance FROM settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        old_balance = int(old_row[0]) if old_row and old_row[0] else 0
        max_sqlite_int = 9_223_372_036_854_775_807
        if old_balance > max_sqlite_int - int(budget_amount):
            g_conn.rollback()
            return None
        try:
            cur = g_conn.execute(
                "UPDATE budget_payment_orders SET status = 'paid', payment_message_id = ?, "
                "payment_author_id = ?, paid_at = ? WHERE id = ? AND status = 'pending'",
                (int(payment_message_id), int(payment_author_id), int(payment_timestamp), order_id),
            )
        except sqlite3.IntegrityError:
            g_conn.rollback()
            return None
        if cur.rowcount != 1:
            g_conn.rollback()
            return None
        new_balance = old_balance + int(budget_amount)
        g_conn.execute(
            "INSERT INTO settings (guild_id, spending_balance) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET spending_balance = excluded.spending_balance",
            (guild_id, new_balance),
        )
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(payment_timestamp))
        g_conn.execute(
            "INSERT INTO budget_logs (action, amount, old_balance, new_balance, admin_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (f"إضافة تلقائية ({method})", int(budget_amount), old_balance, new_balance, int(user_id), timestamp),
        )
        g_conn.commit()
        return {
            "order_id": int(order_id), "user_id": int(user_id), "budget_amount": int(budget_amount),
            "old_balance": old_balance, "new_balance": new_balance, "method": method,
        }
    finally:
        g_conn.close()

def settle_budget_payment(guild_id: int, method: str, channel_id: int, payer_name: str,
                          expected_payment: int, payment_message_id: int, payment_author_id: int,
                          payment_timestamp: int, recipient_id: int):
    method = method.strip().lower()
    with _auto_budget_payment_lock:
        if not is_auto_budget_payment_enabled(method):
            return None
        return _settle_budget_payment_locked(
            guild_id, method, channel_id, payer_name, expected_payment,
            payment_message_id, payment_author_id, payment_timestamp, recipient_id,
        )

def is_bot_admin(ctx):
    if ctx.author.id == PRIMARY_OWNER_ID:
        return True
    if not is_guild_premium(ctx.guild.id):
        return False
    if ctx.author.id == ctx.guild.owner_id:
        return True

    g_conn, g_cur = get_guild_db(ctx.guild.id)
    g_cur.execute("SELECT 1 FROM bot_whitelist WHERE bot_id = ? AND channel_id = ?", (ctx.author.id, ctx.channel.id))
    if g_cur.fetchone():
        g_conn.close()
        return True

    g_cur.execute("SELECT admin_role_id FROM settings WHERE guild_id = ?", (ctx.guild.id,))
    row = g_cur.fetchone()
    g_conn.close()
    if row and row[0]:
        role = ctx.guild.get_role(row[0])
        if role and role in ctx.author.roles:
            return True
    return False

def get_server_spending_balance(guild_id: int) -> int:
    g_conn, g_cur = get_guild_db(guild_id)
    g_cur.execute("SELECT spending_balance FROM settings WHERE guild_id = ?", (guild_id,))
    row = g_cur.fetchone()
    g_conn.close()
    return row[0] if row and row[0] else 0

def deduct_server_spending(guild_id: int, amount: int) -> tuple[bool, str]:
    g_conn, g_cur = get_guild_db(guild_id)
    g_cur.execute("SELECT spending_balance FROM settings WHERE guild_id = ?", (guild_id,))
    row = g_cur.fetchone()
    current_pool = row[0] if row and row[0] else 0

    if current_pool < amount:
        g_conn.close()
        return False, f"⛔ ميزانية السيرفر المتاحة للصرف غير كافية! المطلوب: `{amount:,}` Cosmo، والمتبقي: `{current_pool:,}` Cosmo."

    new_pool = current_pool - amount
    g_cur.execute("UPDATE settings SET spending_balance = ? WHERE guild_id = ?", (new_pool, guild_id))
    g_conn.commit()
    g_conn.close()
    return True, f"🪙 المتبقي من ميزانية السيرفر: `{new_pool:,}` Cosmo."

def refund_server_spending(guild_id: int, amount: int):
    g_conn, g_cur = get_guild_db(guild_id)
    g_cur.execute("UPDATE settings SET spending_balance = spending_balance + ? WHERE guild_id = ?", (amount, guild_id))
    g_conn.commit()
    g_conn.close()

def is_blacklisted(member: discord.Member) -> bool:
    if not member or not hasattr(member, "guild") or not member.guild:
        return False
    g_conn, g_cur = get_guild_db(member.guild.id)
    g_cur.execute("SELECT blacklist_role_id FROM settings WHERE guild_id = ?", (member.guild.id,))
    row = g_cur.fetchone()
    g_conn.close()
    if row and row[0]:
        role = member.guild.get_role(row[0])
        if role and role in member.roles:
            return True
    return False

def get_balance(user_id):
    with _balance_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        row = conn.execute("SELECT balance FROM balances WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else 0

def update_balance(user_id, amount):
    amount = int(amount)
    with _balance_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO balances (user_id, balance) VALUES (?, 0)", (user_id,))
        cur = conn.execute(
            "UPDATE balances SET balance = balance + ? WHERE user_id = ? AND balance + ? >= 0",
            (amount, user_id, amount),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise ValueError("رصيد غير كافٍ لإتمام الخصم.")
        conn.commit()
        return conn.execute("SELECT balance FROM balances WHERE user_id = ?", (user_id,)).fetchone()[0]

def debit_balance(user_id, amount: int) -> bool:
    amount = int(amount)
    if amount <= 0:
        raise ValueError("يجب أن يكون مبلغ الخصم موجباً.")
    with _balance_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE balances SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        conn.commit()
        return cur.rowcount == 1

def transfer_balance(sender_id: int, receiver_id: int, amount: int, timestamp: str) -> bool:
    amount = int(amount)
    if amount <= 0 or sender_id == receiver_id:
        return False
    with _balance_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO balances (user_id, balance) VALUES (?, 0)", (sender_id,))
        cur = conn.execute(
            "UPDATE balances SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, sender_id, amount),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        conn.execute("INSERT OR IGNORE INTO balances (user_id, balance) VALUES (?, 0)", (receiver_id,))
        conn.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (amount, receiver_id))
        conn.execute(
            "INSERT INTO transfer_logs (sender_id, receiver_id, amount, timestamp) VALUES (?, ?, ?, ?)",
            (sender_id, receiver_id, amount, timestamp),
        )
        conn.commit()
        return True

def transfer_balance_to_server_budget(user_id: int, guild_id: int, amount: int, timestamp: str | None = None):
    """Atomically debit global personal Cosmo and credit one guild budget."""
    user_id, guild_id, amount = int(user_id), int(guild_id), int(amount)
    max_sqlite_int = 9_223_372_036_854_775_807
    if user_id <= 0 or guild_id <= 0 or amount <= 0 or amount > max_sqlite_int:
        raise ValueError("بيانات تحويل رصيد Cosmo غير صالحة.")

    guild_conn, _ = get_guild_db(guild_id)
    try:
        guild_conn.commit()
        guild_path = guild_conn.execute("PRAGMA database_list").fetchone()[2]
    finally:
        guild_conn.close()
    if not guild_path or guild_path == ":memory:":
        raise RuntimeError("لا يمكن تنفيذ تحويل ذري على قاعدة بيانات مؤقتة.")

    with _balance_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        conn.execute("ATTACH DATABASE ? AS guild", (os.path.abspath(guild_path),))
        main_path = conn.execute("PRAGMA main.database_list").fetchone()[2]
        main_journal = str(conn.execute("PRAGMA main.journal_mode").fetchone()[0]).lower()
        guild_journal = str(conn.execute("PRAGMA guild.journal_mode").fetchone()[0]).lower()
        if not main_path or main_path == ":memory:" or "wal" in {main_journal, guild_journal}:
            raise RuntimeError("قاعدة البيانات لا تسمح بمعاملة آمنة متعددة الملفات؛ لم يُخصم أي رصيد.")

        conn.execute("BEGIN IMMEDIATE")
        balance_row = conn.execute("SELECT balance FROM main.balances WHERE user_id = ?", (user_id,)).fetchone()
        if not balance_row or int(balance_row[0] or 0) < amount:
            conn.rollback()
            return None
        old_balance = int(balance_row[0] or 0)
        budget_row = conn.execute(
            "SELECT spending_balance FROM guild.settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        old_budget = int(budget_row[0] or 0) if budget_row else 0
        if old_budget > max_sqlite_int - amount:
            conn.rollback()
            raise OverflowError("ميزانية السيرفر ستتجاوز الحد الرقمي المدعوم.")

        debit = conn.execute(
            "UPDATE main.balances SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        if debit.rowcount != 1:
            conn.rollback()
            return None
        conn.execute(
            "INSERT INTO guild.settings (guild_id, spending_balance) VALUES (?, 0) "
            "ON CONFLICT(guild_id) DO NOTHING",
            (guild_id,),
        )
        new_budget = old_budget + amount
        conn.execute(
            "UPDATE guild.settings SET spending_balance = ? WHERE guild_id = ?",
            (new_budget, guild_id),
        )
        timestamp = timestamp or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        conn.execute(
            "INSERT INTO guild.budget_logs (action, amount, old_balance, new_balance, admin_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("إضافة من رصيد Cosmo", amount, old_budget, new_budget, user_id, timestamp),
        )
        conn.commit()
        return {
            "amount": amount,
            "old_personal_balance": old_balance,
            "new_personal_balance": old_balance - amount,
            "old_budget": old_budget,
            "new_budget": new_budget,
        }

def has_pending_exchange_request(user_id: int) -> bool:
    with closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        row = conn.execute(
            "SELECT 1 FROM exchange_logs WHERE user_id = ? AND status = 'قيد الانتظار' LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None

class ExchangeSystemClosed(RuntimeError):
    """Raised when the global exchange switch closes before a request is committed."""

class ExchangeCurrencyUnavailable(RuntimeError):
    """Raised when the selected global currency or quote changed before commit."""

def _create_exchange_request_locked(guild_id: int, user_id: int, amount: int, currency: str,
                                    received_amount: float, note: str, timestamp: str):
    amount = int(amount)
    if amount <= 0:
        return None
    with _balance_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        pending = conn.execute(
            "SELECT 1 FROM exchange_logs WHERE user_id = ? AND status = 'قيد الانتظار' LIMIT 1",
            (user_id,),
        ).fetchone()
        if pending:
            conn.rollback()
            return None
        cur = conn.execute(
            "UPDATE balances SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        cur = conn.execute(
            "INSERT INTO exchange_logs (guild_id, user_id, cosmo_amount, target_currency, received_amount, note, status, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, 'قيد الانتظار', ?)",
            (guild_id, user_id, amount, currency, received_amount, note, timestamp),
        )
        request_id = cur.lastrowid
        conn.commit()
        return request_id

def create_exchange_request(guild_id: int, user_id: int, amount: int, currency: str,
                            received_amount: float, note: str, timestamp: str):
    with _exchange_state_lock:
        if not is_global_exchange_enabled():
            raise ExchangeSystemClosed("التبديل مغلق عالميًا.")
        try:
            amount = int(amount)
            received_amount = float(received_amount)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ExchangeCurrencyUnavailable("قيمة عرض العملة غير صالحة.") from exc
        if amount <= 0 or amount > 9_223_372_036_854_775_807 or not math.isfinite(received_amount):
            raise ExchangeCurrencyUnavailable("قيمة عرض العملة غير صالحة.")
        currency_row = get_exchange_currency(currency)
        if not currency_row or not currency_row[2]:
            raise ExchangeCurrencyUnavailable("العملة لم تعد متاحة للتبديل.")
        try:
            rate = float(currency_row[1])
            minimum = float(currency_row[3])
            expected_received = round(amount * rate, 4)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ExchangeCurrencyUnavailable("تعذر التحقق من سعر العملة.") from exc
        if (not math.isfinite(rate) or rate <= 0 or not math.isfinite(minimum) or minimum < 0
                or not math.isclose(received_amount, expected_received, rel_tol=0, abs_tol=1e-9)
                or expected_received < minimum):
            raise ExchangeCurrencyUnavailable("تغير سعر العملة أو حدها الأدنى؛ أعد تحميل القائمة وحاول مجددًا.")
        return _create_exchange_request_locked(
            guild_id, user_id, amount, currency, received_amount, note, timestamp,
        )

def accept_exchange_request(request_id: int) -> bool:
    with _balance_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE exchange_logs SET status = 'تم القبول' WHERE id = ? AND status = 'قيد الانتظار'",
            (request_id,),
        )
        conn.commit()
        return cur.rowcount == 1

def deny_exchange_request(request_id: int):
    with _balance_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=10)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, cosmo_amount FROM exchange_logs WHERE id = ? AND status = 'قيد الانتظار'",
            (request_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        cur = conn.execute(
            "UPDATE exchange_logs SET status = 'مرفوض' WHERE id = ? AND status = 'قيد الانتظار'",
            (request_id,),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        user_id, amount = row
        conn.execute("INSERT OR IGNORE INTO balances (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
        return row

def parse_amount(amount_str: str) -> int:
    amount_str = str(amount_str).strip().lower()
    multipliers = {'k': 1_000, 'm': 1_000_000, 'b': 1_000_000_000}
    if amount_str[-1] in multipliers:
        unit = amount_str[-1]
        number = float(amount_str[:-1])
        return int(number * multipliers[unit])
    return int(float(amount_str))

def parse_duration(time_str: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    match = re.match(r"^(\d+)([smhd])$", time_str.lower())
    if not match:
        return None
    val, unit = match.groups()
    return int(val) * units[unit]

def clean_display_name(name: str) -> str:
    cleaned = "".join([c for c in name if c.isprintable()]).strip()
    return cleaned[:10] if cleaned else "Player"

def get_unicode_font(size: int):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "DejaVuSans.ttf",
        "arial.ttf"
    ]
    for p in font_paths:
        try:
            return ImageFont.truetype(p, size)
        except:
            continue
    return ImageFont.load_default()

def generate_captcha_image(text):
    base_img = Image.new('RGB', (120, 40), color=(25, 25, 30))
    draw = ImageDraw.Draw(base_img)
    spaced_text = " ".join(text)
    draw.text((20, 12), spaced_text, fill=(255, 255, 255))
    big_img = base_img.resize((300, 100), resample=Image.NEAREST)
    draw_big = ImageDraw.Draw(big_img)
    for _ in range(5):
        draw_big.line([random.randint(0, 300), random.randint(0, 100), random.randint(0, 300), random.randint(0, 100)], fill=(90, 90, 110), width=2)
    buf = io.BytesIO()
    big_img.save(buf, format='PNG')
    buf.seek(0)
    return buf

# إرسال إشعار اللوج العام الموحد
async def send_global_action_log(bot: discord.Client, embed: discord.Embed):
    try:
        global_cursor.execute("SELECT value FROM global_settings WHERE key = 'global_action_log'")
        row = global_cursor.fetchone()
        if row and row[0]:
            log_chan = bot.get_channel(row[0]) or await bot.fetch_channel(row[0])
            if log_chan:
                await log_chan.send(embed=embed)
    except Exception as e:
        print(f"[Global Log Error]: {e}")
