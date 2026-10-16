import sqlite3
import os
import shutil
import time
import re
import math
import random
import io
import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps

PRIMARY_OWNER_ID = 988840428591808584
COSMO_GUILD_ID = 1540014549179039867

if not os.path.exists("guilds_data"):
    os.makedirs("guilds_data")

# قاعدة البيانات المركزية العامة
global_conn = sqlite3.connect("global_cosmo.db", check_same_thread=False)
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
        custom_banner_url TEXT
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
    global_cursor.execute("SELECT balance FROM balances WHERE user_id = ?", (user_id,))
    row = global_cursor.fetchone()
    return row[0] if row else 0

def update_balance(user_id, amount):
    bal = get_balance(user_id) + amount
    global_cursor.execute("INSERT OR REPLACE INTO balances (user_id, balance) VALUES (?, ?)", (user_id, bal))
    global_conn.commit()

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
