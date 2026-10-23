import sqlite3
import os
import shutil
import time
import re
import math
import random
import io
import glob
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
_level_payout_lock = threading.RLock()
_guild_schema_migration_lock = threading.RLock()
_guild_schema_initialized = set()
global_conn = sqlite3.connect(GLOBAL_DB_PATH, check_same_thread=False, timeout=10)
global_cursor = global_conn.cursor()


def _ensure_guild_db_column(cur, table: str, column: str, definition: str, schema_key: str) -> bool:
    """Add a guild DB column once, tolerating another process winning the migration race."""
    if schema_key in _guild_schema_initialized:
        return False
    with _guild_schema_migration_lock:
        if schema_key in _guild_schema_initialized:
            return False
        columns = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in columns:
            return False
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            return True
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                return False
            raise

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
global_cursor.execute('''CREATE TABLE IF NOT EXISTS giveaway_prize_payouts (
    payout_key TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    giveaway_message_id INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    prize_slot INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK(amount > 0),
    created_at INTEGER NOT NULL
)''')
global_conn.commit()

# قواعد بيانات السيرفرات الفردية
def get_guild_db(guild_id: int):
    db_path = f"guilds_data/guild_{guild_id}.db"
    schema_key = os.path.abspath(db_path)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15)
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
        owner_role_id INTEGER,
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
    for column, definition in (
        ("status", "TEXT NOT NULL DEFAULT 'active'"),
        ("generation", "INTEGER NOT NULL DEFAULT 0"),
        ("managed_version", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _ensure_guild_db_column(cur, "active_giveaways", column, definition, schema_key)
    if schema_key not in _guild_schema_initialized:
        cur.execute(
            "UPDATE active_giveaways SET status = 'ended' "
            "WHERE is_finished = 1 AND status = 'active'"
        )
    cur.execute('''CREATE TABLE IF NOT EXISTS giveaway_winners (
        message_id INTEGER NOT NULL,
        generation INTEGER NOT NULL,
        prize_slot INTEGER NOT NULL,
        user_id INTEGER,
        state TEXT NOT NULL,
        PRIMARY KEY(message_id, generation, prize_slot)
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

    for column, definition in (
        ("provider_order_id", "TEXT"),
        ("provider_payment_id", "TEXT"),
        ("quote_usd", "TEXT"),
        ("pay_address", "TEXT"),
        ("pay_currency", "TEXT"),
        ("provider_status", "TEXT"),
    ):
        _ensure_guild_db_column(cur, "budget_payment_orders", column, definition, schema_key)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_payment_provider_order ON budget_payment_orders(provider_order_id)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_budget_payment_provider_payment ON budget_payment_orders(provider_payment_id)")

    cur.execute('''CREATE TABLE IF NOT EXISTS auto_replies (
        trigger_word TEXT PRIMARY KEY,
        reply_content TEXT
    )''')

    cur.execute('''CREATE TABLE IF NOT EXISTS leveling_config (
        guild_id INTEGER PRIMARY KEY,
        is_enabled INTEGER NOT NULL DEFAULT 0,
        milestone_reward INTEGER NOT NULL DEFAULT 0,
        announcement_channel_id INTEGER,
        last_daily_report_date TEXT,
        enabled_since REAL
    )''')
    _ensure_guild_db_column(cur, "leveling_config", "enabled_since", "REAL", schema_key)
    cur.execute(
        "UPDATE leveling_config SET enabled_since = ? WHERE is_enabled = 1 AND enabled_since IS NULL",
        (time.time(),),
    )
    cur.execute('''CREATE TABLE IF NOT EXISTS leveling_channels (
        channel_id INTEGER PRIMARY KEY
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS leveling_users (
        user_id INTEGER PRIMARY KEY,
        total_points INTEGER NOT NULL DEFAULT 0,
        last_point_at REAL NOT NULL DEFAULT 0,
        spam_until REAL NOT NULL DEFAULT 0,
        last_spam_warning_at REAL NOT NULL DEFAULT 0
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS leveling_pending_messages (
        message_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        created_at REAL NOT NULL
    )''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_level_pending_user_time ON leveling_pending_messages(user_id, created_at)')
    cur.execute('''CREATE TABLE IF NOT EXISTS leveling_message_events (
        message_id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        points_awarded INTEGER NOT NULL DEFAULT 0
    )''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_level_events_user_time ON leveling_message_events(user_id, created_at)')
    cur.execute('''CREATE TABLE IF NOT EXISTS leveling_salary_tiers (
        message_threshold INTEGER PRIMARY KEY,
        amount INTEGER NOT NULL CHECK(amount >= 0)
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS leveling_payouts (
        payout_key TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        payout_type TEXT NOT NULL,
        amount INTEGER NOT NULL CHECK(amount > 0),
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )''')
    for column, definition in (
        ("local_date", "TEXT"),
        ("message_count", "INTEGER"),
        ("tier_threshold", "INTEGER"),
    ):
        _ensure_guild_db_column(cur, "leveling_payouts", column, definition, schema_key)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_level_salary_history "
        "ON leveling_payouts(payout_type, local_date DESC, user_id)"
    )
    cur.execute('''CREATE TABLE IF NOT EXISTS leveling_daily_reports (
        local_date TEXT PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        message_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        last_send_attempt_at REAL,
        last_send_channel_id INTEGER
    )''')
    for column, definition in (
        ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ("next_attempt_at", "INTEGER NOT NULL DEFAULT 0"),
        ("last_error", "TEXT"),
        ("last_send_attempt_at", "REAL"),
        ("last_send_channel_id", "INTEGER"),
    ):
        _ensure_guild_db_column(cur, "leveling_daily_reports", column, definition, schema_key)

    # ترقية الجداول في حال وجود قواعد بيانات منشأة مسبقاً
    _ensure_guild_db_column(cur, "premium_info", "custom_banner_url", "TEXT", schema_key)
    _ensure_guild_db_column(cur, "settings", "ticket_log", "INTEGER", schema_key)
    with _guild_schema_migration_lock:
        if _ensure_guild_db_column(cur, "settings", "owner_role_id", "INTEGER", schema_key):
            cur.execute(
                "UPDATE settings SET owner_role_id = admin_role_id, admin_role_id = NULL "
                "WHERE admin_role_id IS NOT NULL"
            )
    _ensure_guild_db_column(cur, "premium_info", "custom_currency_name", "TEXT", schema_key)

    if schema_key not in _guild_schema_initialized:
        cur.execute(
            "UPDATE leveling_daily_reports SET last_send_attempt_at = created_at, last_send_channel_id = channel_id "
            "WHERE status = 'pending' AND last_send_attempt_at IS NULL"
        )

    conn.commit()
    with _guild_schema_migration_lock:
        _guild_schema_initialized.add(schema_key)
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
    # لا يمكن إلغاء فاتورة LTCPay بأمان من المفتاح المحلي؛ الطلبات الصادرة
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
    """Reserve an LTC-only LTCPay invoice before the external invoice is created."""
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
    """Attach the LTCPay invoice ID and checked checkout URL to its reserved order."""
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


def _find_crypto_order_by_invoice_id(invoice_id: str):
    """Find the local order linked to an invoice without trusting invoice metadata."""
    invoice_id = str(invoice_id).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invoice_id):
        return None
    database_dir = os.path.abspath("guilds_data")
    for path in glob.glob(os.path.join(database_dir, "guild_*.db")):
        name = os.path.basename(path)
        match = re.fullmatch(r"guild_(\d+)\.db", name)
        if not match:
            continue
        guild_id = int(match.group(1))
        try:
            with closing(sqlite3.connect(path, timeout=5)) as conn:
                row = conn.execute(
                    "SELECT provider_order_id FROM budget_payment_orders "
                    "WHERE method = 'crypto' AND provider_payment_id = ? "
                    "AND status NOT IN ('cancelled', 'failed') LIMIT 1",
                    (invoice_id,),
                ).fetchone()
            if row and row[0]:
                return guild_id, str(row[0]).strip()
        except sqlite3.DatabaseError:
            continue
    return None


def process_ltcpay_webhook(event: dict, invoice: dict):
    """Settle one signed LTCPay event after fetching its canonical invoice state."""
    event_type = str(event.get("type", "")).strip()
    invoice_id = str(event.get("invoiceId", "")).strip()
    event_store_id = str(event.get("storeId", "")).strip()
    canonical_invoice_id = str(invoice.get("id") or invoice_id).strip()
    canonical_store_id = str(invoice.get("storeId") or event_store_id).strip()
    if not invoice_id or canonical_invoice_id != invoice_id or not event_store_id or canonical_store_id != event_store_id:
        return None

    # Prefer the provider order reference, but fall back to our stored invoice
    # ID when a compatible provider omits custom metadata from GET responses.
    # The saved invoice ID is tied to one local order before checkout is shown.
    metadata = invoice.get("metadata") if isinstance(invoice.get("metadata"), dict) else {}
    provider_order_id = str(metadata.get("orderId", "")).strip()
    linked_by_invoice_id = False
    match = re.fullmatch(r"cosmo:(\d{1,20}):([0-9a-f]{32})", provider_order_id)
    if not match:
        linked_order = _find_crypto_order_by_invoice_id(invoice_id)
        if not linked_order:
            return None
        provider_order_id = linked_order[1]
        linked_by_invoice_id = True
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
            "quote_usd, status, provider_order_id, provider_status, pay_currency FROM budget_payment_orders "
            "WHERE guild_id = ? AND method = 'crypto' AND provider_order_id = ? LIMIT 1",
            (guild_id, provider_order_id),
        ).fetchone()
        if not order:
            g_conn.rollback()
            # The invoice is structurally ours, but the local order may still
            # be committing while LTCPay delivers the webhook. Do not credit
            # anything; ask the provider to retry instead.
            return {
                "status": "retry",
                "guild_id": guild_id,
                "provider_order_id": provider_order_id,
            }
        order_id, user_id, channel_id, budget_amount, expected_usd_units, saved_invoice_id, quote_usd, order_status, saved_order_id, previous_provider_status, saved_pay_currency = order
        if str(saved_order_id) != provider_order_id:
            g_conn.rollback()
            return None
        if saved_invoice_id and str(saved_invoice_id) != invoice_id:
            g_conn.rollback()
            return None
        if order_status == "paid":
            g_conn.rollback()
            return {"status": "duplicate", "order_id": int(order_id)}
        can_settle_order = order_status in {"pending", "creating", "processing", "expired"} or (
            order_status == "needs_review"
            and previous_provider_status in {"Settled_review", "Expired", "Invalid"}
        )

        checkout = invoice.get("checkout") if isinstance(invoice.get("checkout"), dict) else {}
        methods = checkout.get("paymentMethods") if "paymentMethods" in checkout else invoice.get("paymentMethods")
        if methods is None or methods == []:
            # Our checkout creation pins every crypto order to LTC. Reuse that
            # saved fact when the provider omits its method list in the GET.
            method_ok = str(saved_pay_currency or "").casefold() == "ltc"
        else:
            method_ok = (
                isinstance(methods, list) and len(methods) == 1
                and str(methods[0]).upper() in {"LTC", "LTC-CHAIN"}
            )
        try:
            expected_quote = Decimal(str(quote_usd))
            raw_invoice_amount = invoice.get("amount")
            raw_paid_amount = invoice.get("paidAmount")
            # Amount and payment amount are immutable once the provider has
            # reached Settled. When omitted, use the USD quote stored before
            # checkout; a present but malformed value is still rejected.
            invoice_amount = expected_quote if raw_invoice_amount in (None, "") else Decimal(str(raw_invoice_amount))
            paid_amount = invoice_amount if raw_paid_amount in (None, "") else Decimal(str(raw_paid_amount))
            expected_units = int((expected_quote * Decimal(100_000_000)).to_integral_value())
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            invoice_amount = paid_amount = expected_quote = Decimal("0")
            expected_units = 0
        raw_currency = str(invoice.get("currency") or "").strip().upper()
        currency_matches = raw_currency in {"", "USD"}
        local_invoice_matches = bool(saved_invoice_id and str(saved_invoice_id) == invoice_id)
        metadata_user_id = str(metadata.get("discord_user_id", "")).strip()
        metadata_user_matches = not metadata_user_id or metadata_user_id == str(user_id)
        identity_matches = (
            currency_matches
            and invoice_amount.is_finite() and invoice_amount == expected_quote
            and expected_units == int(expected_usd_units)
            and method_ok
            and metadata_user_matches
            and (
                linked_by_invoice_id or local_invoice_matches
                or (
                    isinstance(invoice.get("metadata"), dict)
                    and str(invoice["metadata"].get("orderId", "")) == provider_order_id
                    and str(invoice["metadata"].get("discord_user_id", "")) == str(user_id)
                )
            )
        )

        if event_type == "InvoiceProcessing" and str(invoice.get("status", "")).lower() == "processing":
            # Processing is an intermediate on-chain state. Providers may
            # omit paidAmount, payment methods, or metadata at this stage.
            # Never turn that incomplete snapshot into a terminal review state;
            # the complete security decision is made on InvoiceSettled.
            if order_status in {"pending", "creating", "processing"}:
                g_conn.execute(
                    "UPDATE budget_payment_orders SET status = 'processing', provider_status = 'Processing', "
                    "provider_payment_id = COALESCE(provider_payment_id, ?) "
                    "WHERE id = ? AND status IN ('pending', 'creating', 'processing')",
                    (invoice_id, order_id),
                )
                g_conn.commit()
                return {"status": "processing", "order_id": int(order_id)}
            g_conn.rollback()
            return None
            g_conn.execute(
                "UPDATE budget_payment_orders SET status = 'processing', provider_status = 'Processing', "
                "provider_payment_id = COALESCE(provider_payment_id, ?) WHERE id = ? AND status IN ('pending', 'creating', 'processing')",
                (invoice_id, order_id),
            )
            g_conn.commit()
            return {"status": "processing", "order_id": int(order_id)}

        late_full_payment = False
        if event_type == "InvoiceExpired" and str(invoice.get("status", "")).lower() == "expired":
            additional = str(invoice.get("additionalStatus", "None")).casefold()
            if additional == "paidlate":
                # Reduced-safety option: a full amount observed by LTCPay on an
                # expired invoice can be credited before the provider changes
                # its base status to Settled. Require an explicit paidAmount
                # and the exact locally stored USD invoice amount.
                try:
                    expected_late_quote = Decimal(str(quote_usd))
                    late_invoice_amount = Decimal(str(invoice.get("amount")))
                    late_paid_amount = Decimal(str(invoice.get("paidAmount")))
                    late_full_payment = (
                        str(invoice.get("currency", "")).upper() in {"", "USD"}
                        and expected_late_quote.is_finite()
                        and late_invoice_amount.is_finite()
                        and late_paid_amount.is_finite()
                        and late_invoice_amount == expected_late_quote
                        and late_paid_amount >= late_invoice_amount
                    )
                except (InvalidOperation, TypeError, ValueError, OverflowError):
                    late_full_payment = False
            if not late_full_payment:
                status = "needs_review" if additional in {"paidpartial", "paidover", "paidlate", "marked"} else "expired"
                g_conn.execute(
                    "UPDATE budget_payment_orders SET status = ?, provider_status = 'Expired', "
                    "provider_payment_id = COALESCE(provider_payment_id, ?) "
                    "WHERE id = ? AND status IN ('pending', 'creating', 'processing')",
                    (status, invoice_id, order_id),
                )
                g_conn.commit()
                if status == "needs_review":
                    return {"status": status, "notify": order_status != "needs_review", "order_id": int(order_id), "user_id": int(user_id), "channel_id": int(channel_id)}
                return {"status": status, "order_id": int(order_id)}

        if event_type == "InvoiceInvalid" and str(invoice.get("status", "")).lower() == "invalid":
            notify = order_status != "needs_review"
            g_conn.execute(
                "UPDATE budget_payment_orders SET status = 'needs_review', provider_status = 'Invalid', "
                "provider_payment_id = COALESCE(provider_payment_id, ?) WHERE id = ? AND status != 'paid'",
                (invoice_id, order_id),
            )
            g_conn.commit()
            return {"status": "needs_review", "notify": notify, "order_id": int(order_id), "user_id": int(user_id), "channel_id": int(channel_id)}

        provider_confirms_settled = (
            event_type == "InvoiceSettled" and str(invoice.get("status", "")).lower() == "settled"
        )
        if not provider_confirms_settled and not late_full_payment:
            g_conn.rollback()
            return None

        additional = invoice.get("additionalStatus")
        additional_status = str(additional).casefold() if additional is not None else "none"
        payment_matches = (
            identity_matches and method_ok
            # A provider-confirmed Settled invoice is accepted even if it is
            # late or manually marked. A full late payment may also be credited
            # from an Expired/PaidLate event with an explicit sufficient amount.
            # When paidAmount is absent on a Settled invoice, use the pinned
            # local quote as the provider's full-payment confirmation.
            and additional_status != "paidpartial"
            and paid_amount.is_finite()
            and paid_amount >= invoice_amount
            and can_settle_order
        )
        if not payment_matches:
            notify = order_status != "needs_review"
            g_conn.execute(
                "UPDATE budget_payment_orders SET status = 'needs_review', provider_status = 'Settled_review', "
                "provider_payment_id = COALESCE(provider_payment_id, ?) WHERE id = ? AND status != 'paid'",
                (invoice_id, order_id),
            )
            g_conn.commit()
            return {"status": "needs_review", "notify": notify, "order_id": int(order_id), "user_id": int(user_id), "channel_id": int(channel_id)}

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
            "provider_payment_id = ?, paid_at = ? WHERE id = ? AND ("
            "status IN ('pending', 'creating', 'processing') OR "
            "status = 'expired' OR "
            "(status = 'needs_review' AND provider_status IN ('Settled_review', 'Expired', 'Invalid'))) ",
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

def get_guild_management_role_ids(guild_id: int) -> tuple[int | None, int | None]:
    """Return (owner_role_id, limited_admin_role_id) for this guild."""
    g_conn, g_cur = get_guild_db(guild_id)
    g_cur.execute("SELECT owner_role_id, admin_role_id FROM settings WHERE guild_id = ?", (guild_id,))
    row = g_cur.fetchone()
    g_conn.close()
    return (row[0], row[1]) if row else (None, None)


def has_guild_role(member, role_id: int | None, guild=None) -> bool:
    if not member or not role_id:
        return False
    guild = guild or getattr(member, "guild", None)
    role = guild.get_role(role_id) if guild else None
    return bool(role and role in getattr(member, "roles", ()))


def is_bot_admin(ctx):
    if not ctx or not ctx.guild or not ctx.author:
        return False
    if ctx.author.id == PRIMARY_OWNER_ID:
        return True

    owner_role_id, _limited_admin_role_id = get_guild_management_role_ids(ctx.guild.id)
    if has_guild_role(ctx.author, owner_role_id, ctx.guild):
        return True

    if not is_guild_premium(ctx.guild.id):
        return False
    if ctx.author.id == ctx.guild.owner_id:
        return True

    g_conn, g_cur = get_guild_db(ctx.guild.id)
    g_cur.execute("SELECT 1 FROM bot_whitelist WHERE bot_id = ? AND channel_id = ?", (ctx.author.id, ctx.channel.id))
    allowed = bool(g_cur.fetchone())
    g_conn.close()
    return allowed

def get_server_spending_balance(guild_id: int) -> int:
    g_conn, g_cur = get_guild_db(guild_id)
    g_cur.execute("SELECT spending_balance FROM settings WHERE guild_id = ?", (guild_id,))
    row = g_cur.fetchone()
    g_conn.close()
    return row[0] if row and row[0] else 0


def payout_from_server_budget(
    guild_id: int,
    user_id: int,
    amount: int,
    payout_key: str,
    payout_type: str,
    timestamp: int | None = None,
) -> dict:
    """Atomically pay a guild-funded level reward/salary and prevent duplicate payouts."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("يجب أن يكون مبلغ المكافأة أكبر من صفر.")
    timestamp = int(timestamp or time.time())
    db_path = os.path.abspath(f"guilds_data/guild_{int(guild_id)}.db")
    get_guild_db(guild_id)[0].close()  # ensure schema exists for both new and existing guild files

    with _balance_lock, _level_payout_lock, closing(sqlite3.connect(db_path, timeout=15, isolation_level=None)) as conn:
        conn.execute("ATTACH DATABASE ? AS global_wallet", (GLOBAL_DB_PATH,))
        guild_journal = str(conn.execute("PRAGMA main.journal_mode").fetchone()[0]).lower()
        global_journal = str(conn.execute("PRAGMA global_wallet.journal_mode").fetchone()[0]).lower()
        if guild_journal == "wal" or global_journal == "wal":
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT user_id, amount, status FROM leveling_payouts WHERE payout_key = ?", (payout_key,)
            ).fetchone()
            if existing and (int(existing[0]) != int(user_id) or int(existing[1]) != amount):
                conn.rollback()
                raise ValueError("مفتاح صرف المكافأة مستخدم ببيانات مختلفة.")
            if existing and existing[2] == "paid":
                conn.commit()
                return {"status": "already_paid", "amount": existing[1], "user_id": existing[0]}
            conn.execute(
                "INSERT INTO leveling_payouts (payout_key, user_id, payout_type, amount, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'blocked_wal', ?, ?) "
                "ON CONFLICT(payout_key) DO UPDATE SET status='blocked_wal', updated_at=excluded.updated_at",
                (payout_key, user_id, payout_type, amount, timestamp, timestamp),
            )
            conn.commit()
            return {"status": "blocked_wal", "amount": amount, "user_id": int(user_id)}
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT user_id, amount, status FROM leveling_payouts WHERE payout_key = ?",
            (payout_key,),
        ).fetchone()
        if existing and (int(existing[0]) != int(user_id) or int(existing[1]) != amount):
            conn.rollback()
            raise ValueError("مفتاح صرف المكافأة مستخدم ببيانات مختلفة.")
        if existing and existing[2] == "paid":
            conn.commit()
            return {"status": "already_paid", "amount": existing[1], "user_id": existing[0]}

        conn.execute(
            "INSERT OR IGNORE INTO settings (guild_id, spending_balance) VALUES (?, 0)",
            (guild_id,),
        )
        balance_row = conn.execute(
            "SELECT spending_balance FROM settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        old_budget = int(balance_row[0] or 0) if balance_row else 0
        if old_budget < amount:
            conn.execute(
                "INSERT INTO leveling_payouts (payout_key, user_id, payout_type, amount, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'insufficient', ?, ?) "
                "ON CONFLICT(payout_key) DO UPDATE SET status='insufficient', updated_at=excluded.updated_at",
                (payout_key, user_id, payout_type, amount, timestamp, timestamp),
            )
            conn.commit()
            return {"status": "insufficient", "amount": amount, "budget": old_budget, "user_id": int(user_id)}

        new_budget = old_budget - amount
        conn.execute(
            "UPDATE settings SET spending_balance = ? WHERE guild_id = ?",
            (new_budget, guild_id),
        )
        conn.execute(
            "INSERT INTO global_wallet.balances (user_id, balance) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance",
            (user_id, amount),
        )
        conn.execute(
            "INSERT INTO budget_logs (action, amount, old_balance, new_balance, admin_id, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("مكافأة لفل" if payout_type == "milestone" else "راتب نظام اللفلات", amount,
             old_budget, new_budget, PRIMARY_OWNER_ID, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))),
        )
        conn.execute(
            "INSERT INTO leveling_payouts (payout_key, user_id, payout_type, amount, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'paid', ?, ?) "
            "ON CONFLICT(payout_key) DO UPDATE SET status='paid', updated_at=excluded.updated_at",
            (payout_key, user_id, payout_type, amount, timestamp, timestamp),
        )
        conn.commit()
        return {"status": "paid", "amount": amount, "budget": new_budget, "user_id": int(user_id)}

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


def credit_giveaway_prize(
    guild_id: int,
    giveaway_message_id: int,
    generation: int,
    prize_slot: int,
    user_id: int,
    amount: int,
) -> dict:
    """Credit one reserved giveaway prize exactly once, including after retries."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("يجب أن يكون مبلغ جائزة الجيف أكبر من صفر.")
    payout_key = f"giveaway:{int(guild_id)}:{int(giveaway_message_id)}:{int(generation)}:{int(prize_slot)}"
    created_at = int(time.time())
    with _balance_lock, closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=15)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS giveaway_prize_payouts (
            payout_key TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            giveaway_message_id INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            prize_slot INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL CHECK(amount > 0),
            created_at INTEGER NOT NULL
        )""")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT user_id, amount FROM giveaway_prize_payouts WHERE payout_key = ?",
            (payout_key,),
        ).fetchone()
        if existing:
            if int(existing[0]) != int(user_id) or int(existing[1]) != amount:
                conn.rollback()
                raise ValueError("مفتاح صرف الجيف مستخدم ببيانات مختلفة.")
            conn.commit()
            return {"status": "already_paid", "user_id": int(user_id), "amount": amount}

        conn.execute("INSERT OR IGNORE INTO balances (user_id, balance) VALUES (?, 0)", (user_id,))
        conn.execute("UPDATE balances SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.execute(
            "INSERT INTO giveaway_prize_payouts "
            "(payout_key, guild_id, giveaway_message_id, generation, prize_slot, user_id, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (payout_key, guild_id, giveaway_message_id, generation, prize_slot, user_id, amount, created_at),
        )
        conn.commit()
        return {"status": "paid", "user_id": int(user_id), "amount": amount}


def giveaway_has_any_payout(guild_id: int, giveaway_message_id: int) -> bool:
    with closing(sqlite3.connect(GLOBAL_DB_PATH, timeout=15)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS giveaway_prize_payouts (
            payout_key TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            giveaway_message_id INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            prize_slot INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL CHECK(amount > 0),
            created_at INTEGER NOT NULL
        )""")
        row = conn.execute(
            "SELECT 1 FROM giveaway_prize_payouts WHERE guild_id = ? AND giveaway_message_id = ? LIMIT 1",
            (guild_id, giveaway_message_id),
        ).fetchone()
        return bool(row)

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
