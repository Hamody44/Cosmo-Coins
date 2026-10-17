import os
import sqlite3
import sys
import secrets
import hmac
import ipaddress
import socket
import math
from urllib.parse import urlencode, urlsplit
from datetime import timedelta, datetime
import time
import requests
import base64
from flask import Flask, redirect, request, session, render_template, url_for, flash, abort
from dotenv import load_dotenv

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# استيراد الدوال والأدوات المتاحة من database.py
import database
from database import (
    get_guild_db, PRIMARY_OWNER_ID, get_balance, parse_amount,
    create_exchange_request, deny_exchange_request,
)

load_dotenv(os.path.join(project_root, ".env"))

template_dir = os.path.join(current_dir, "templates")
static_dir = os.path.join(current_dir, "static")
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

# جلسة دائمة لمدة 30 يوماً. لا تستخدم مفتاحاً ثابتاً/افتراضياً معروفاً.
app.secret_key = os.getenv("FLASK_SECRET_KEY")
if not app.secret_key or len(app.secret_key) < 32:
    app.secret_key = secrets.token_urlsafe(48)
    print("[SECURITY] FLASK_SECRET_KEY غير مضبوط أو ضعيف؛ استُخدم مفتاح مؤقت عشوائي. اضبط مفتاحاً ثابتاً بطول 32 محرفاً على الأقل.")
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true",
)

# معرّف تطبيق/بوت Cosmo Coins (معرّف عام)؛ هذا هو مصدر الحقيقة لرابط OAuth والدعوة.
CLIENT_ID = "1550533434362044607"
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_API_BASE = "https://discord.com/api/v10"

# ذاكرة كاش لأسماء مستخدمي ديسكورد لتسريع التصفح
USER_CACHE = {}

def get_discord_username(user_id):
    if not user_id:
        return "غير معروف"
    try:
        user_id = int(user_id)
    except:
        return str(user_id)

    if user_id in USER_CACHE:
        return USER_CACHE[user_id]

    # 1. محاولة قراءة الاسم من قاعدة البيانات العامة إذا كان مخزناً
    try:
        g_conn, g_cur = get_global_db()
        g_cur.execute("PRAGMA table_info(users)")
        cols = [c[1].lower() for c in g_cur.fetchall()]
        name_col = next((c for c in cols if c in ["username", "name", "display_name", "user_name"]), None)
        if name_col:
            g_cur.execute(f"SELECT {name_col} FROM users WHERE user_id = ?", (user_id,))
            row = g_cur.fetchone()
            if row and row[0]:
                g_conn.close()
                USER_CACHE[user_id] = str(row[0])
                return str(row[0])
        g_conn.close()
    except Exception as e:
        print(f"[DEBUG] DB user name lookup error: {e}")

    # 2. الجلب المباشر عبر Discord Bot API
    if BOT_TOKEN:
        try:
            clean_token = BOT_TOKEN.replace("Bot ", "").strip()
            headers = {
                "Authorization": f"Bot {clean_token}",
                "User-Agent": "DiscordBot (https://discord.com, v10)"
            }
            r = requests.get(f"{DISCORD_API_BASE}/users/{user_id}", headers=headers, timeout=4)
            if r.status_code == 200:
                data = r.json()
                name = data.get("global_name") or data.get("username")
                if name:
                    USER_CACHE[user_id] = name
                    return name
            else:
                print(f"[DEBUG] Discord API returned status {r.status_code} for user {user_id}: {r.text}")
        except Exception as e:
            print(f"[DEBUG] Discord API user fetch exception for {user_id}: {e}")

    # 3. اسم احتياطي واضح
    fallback_name = f"مستخدم #{str(user_id)[-4:]}"
    USER_CACHE[user_id] = fallback_name
    return fallback_name

def get_redirect_uri():
    configured_url = os.getenv("DASHBOARD_URL")
    if not configured_url:
        raise RuntimeError("يجب ضبط DASHBOARD_URL إلى عنوان لوحة التحكم العام قبل استخدام تسجيل الدخول.")
    parsed = urlsplit(configured_url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise RuntimeError("قيمة DASHBOARD_URL غير صالحة.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("يجب أن يستخدم DASHBOARD_URL بروتوكول HTTPS خارج التطوير المحلي.")
    return f"{configured_url.rstrip('/')}/callback"

def get_global_db():
    db_path = os.path.join(project_root, "global_cosmo.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return conn, conn.cursor()

def get_bot_guild_ids():
    if not BOT_TOKEN:
        return set()
    try:
        r = requests.get(
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            timeout=5
        )
        if r.status_code == 200:
            return {int(g["id"]) for g in r.json()}
    except Exception as e:
        print(f"Error fetching bot guilds: {e}")
    return set()

def build_bot_invite_url():
    """رابط إضافة تطبيق Cosmo Coins؛ client_id هو معرّف التطبيق وليس معرّف السيرفر."""
    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": CLIENT_ID,
        "scope": "bot applications.commands",
        "permissions": "8",
    })

def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token

@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token}

@app.before_request
def enforce_csrf_for_posts():
    if request.method == "POST":
        expected = session.get("csrf_token", "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            abort(400, description="رمز حماية النموذج غير صالح أو منتهي.")

def user_can_manage_guild(access_token: str, guild_id: int) -> bool:
    """تحقق من صلاحيات OAuth الفعلية، لا من رقم السيرفر المرسل في URL فقط."""
    try:
        response = requests.get(
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=5,
        )
        guilds = response.json() if response.status_code == 200 else None
        if not isinstance(guilds, list):
            return False
        for guild in guilds:
            if int(guild.get("id", 0)) != int(guild_id):
                continue
            permissions = int(guild.get("permissions", 0))
            return bool(guild.get("owner") or (permissions & 0x20) or (permissions & 0x8))
    except (requests.RequestException, ValueError, TypeError):
        return False
    return False

def guild_can_manage_exchange(guild_data: dict) -> bool:
    """لا يكفي كون المستخدم عضوًا؛ يلزم أن يكون مالكًا أو مديرًا للسيرفر."""
    try:
        permissions = int(guild_data.get("permissions", 0))
    except (TypeError, ValueError):
        permissions = 0
    return bool(guild_data.get("owner") or (permissions & 0x8) or (permissions & 0x20))

def fetch_public_image(url: str, max_bytes: int = 8 * 1024 * 1024):
    """جلب صورة HTTPS عامة فقط مع منع عناوين الشبكات الخاصة والتحويلات."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return None
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            return None
        response = requests.get(url, timeout=(3, 5), allow_redirects=False, stream=True)
        if response.status_code != 200:
            response.close()
            return None
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            response.close()
            return None
        data = bytearray()
        for chunk in response.iter_content(64 * 1024):
            data.extend(chunk)
            if len(data) > max_bytes:
                response.close()
                return None
        response.close()
        return content_type, bytes(data)
    except (ValueError, OSError, requests.RequestException):
        return None

@app.before_request
def make_session_permanent():
    session.permanent = True

# ==========================================================
# المسارات (Routes)
# ==========================================================

@app.route("/")
def index():
    user = session.get("user")
    return render_template("index.html", user=user)

@app.route("/login")
def login():
    redirect_uri = get_redirect_uri()
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    discord_auth_url = "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": CLIENT_ID or "",
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "identify guilds",
        "prompt": "consent",
        "state": state,
    })
    return redirect(discord_auth_url)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/callback")
def callback():
    expected_state = session.pop("oauth_state", None)
    supplied_state = request.args.get("state", "")
    if not expected_state or not supplied_state or not hmac.compare_digest(expected_state, supplied_state):
        abort(400, description="فشل التحقق من جلسة تسجيل الدخول؛ أعد المحاولة.")
    code = request.args.get("code")
    if not code:
        return redirect(url_for("index"))

    redirect_uri = get_redirect_uri()
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(f"{DISCORD_API_BASE}/oauth2/token", data=data, headers=headers, timeout=8)
    token_data = r.json()
    access_token = token_data.get("access_token")

    if not access_token:
        return f"فشل تسجيل الدخول: {token_data.get('error_description', 'تحقق من Redirect URI')}", 400

    session["access_token"] = access_token
    auth_headers = {"Authorization": f"Bearer {access_token}"}
    user_response = requests.get(f"{DISCORD_API_BASE}/users/@me", headers=auth_headers, timeout=8)
    if user_response.status_code != 200:
        session.clear()
        abort(400, description="تعذر التحقق من حساب Discord.")
    user_res = user_response.json()
    session["user"] = user_res

    return redirect(url_for("servers"))

@app.route("/servers")
def servers():
    user = session.get("user")
    token = session.get("access_token")
    if not user or not token:
        return redirect(url_for("login"))

    auth_headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{DISCORD_API_BASE}/users/@me/guilds", headers=auth_headers)
    user_guilds = r.json() if r.status_code == 200 else []

    bot_guild_ids = get_bot_guild_ids()
    manageable_guilds = []

    if isinstance(user_guilds, list):
        for g in user_guilds:
            try:
                perms = int(g.get("permissions", 0))
            except (ValueError, TypeError):
                perms = 0
            
            is_owner = g.get("owner", False)
            has_manage_guild = (perms & 0x20) == 0x20
            has_admin = (perms & 0x8) == 0x8

            if is_owner or has_manage_guild or has_admin:
                g["bot_joined"] = int(g["id"]) in bot_guild_ids
                manageable_guilds.append(g)

    return render_template(
        "servers.html",
        user=user,
        guilds=manageable_guilds,
        bot_invite_url=build_bot_invite_url(),
    )

@app.route("/manage/<int:guild_id>", methods=["GET", "POST"])
def manage(guild_id):
    user = session.get("user")
    token = session.get("access_token")
    if not user or not token:
        return redirect(url_for("login"))
    if not user_can_manage_guild(token, guild_id):
        abort(403, description="لا تملك صلاحية إدارة هذا السيرفر.")
    if guild_id not in get_bot_guild_ids():
        abort(404, description="البوت غير موجود في هذا السيرفر.")

    try:
        conn, cur = get_guild_db(guild_id)
        bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

        cur.execute("""
            CREATE TABLE IF NOT EXISTS exchange_currencies (
                currency_name TEXT PRIMARY KEY,
                exchange_rate REAL,
                is_active INTEGER DEFAULT 1,
                min_received REAL DEFAULT 0.0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_replies (
                trigger_word TEXT PRIMARY KEY,
                reply_content TEXT
            )
        """)
        conn.commit()

        # 1. فحص حالة البريميوم
        now_ts = int(time.time())
        is_premium = False
        premium_expiry_str = "غير مفعل"
        banner_url = ""
        avatar_url = ""
        try:
            cur.execute("SELECT premium_end, custom_banner_url, custom_avatar_url, custom_currency_name FROM premium_info WHERE guild_id = ?", (guild_id,))
            prem_row = cur.fetchone()
            if prem_row and prem_row[0] and int(prem_row[0]) > now_ts:
                is_premium = True
                rem = int(prem_row[0]) - now_ts
                days = rem // 86400
                hours = (rem % 86400) // 3600
                premium_expiry_str = f"متبقي {days} يوم و {hours} ساعة ({datetime.fromtimestamp(int(prem_row[0])).strftime('%Y-%m-%d %H:%M')})"
                banner_url = prem_row[1] or ""
                avatar_url = prem_row[2] or ""
                currency_label = prem_row[3] or "Cosmo"
            else:
                currency_label = "Cosmo"
        except Exception as e:
            print(f"[DEBUG] Premium read error: {e}")
            currency_label = "Cosmo"

        # 2. معالجة إرسال البيانات (POST)
        if request.method == "POST":
            action = request.form.get("action")
            
            if action == "save_settings":
                exchange_enabled = 1 if request.form.get("exchange_enabled") == "on" else 0
                transfer_log = request.form.get("transfer_log") or None
                delivery_log = request.form.get("delivery_log") or None
                balance_log = request.form.get("balance_log") or None
                exchange_success_log = request.form.get("exchange_success_log") or None
                ticket_log = request.form.get("ticket_log") or None

                cur.execute("""
                    INSERT INTO settings (
                        guild_id, exchange_enabled, transfer_log, delivery_log,
                        balance_log, exchange_success_log, ticket_log
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        exchange_enabled = excluded.exchange_enabled,
                        transfer_log = excluded.transfer_log,
                        delivery_log = excluded.delivery_log,
                        balance_log = excluded.balance_log,
                        exchange_success_log = excluded.exchange_success_log,
                        ticket_log = excluded.ticket_log
                """, (guild_id, exchange_enabled, transfer_log, delivery_log, balance_log, exchange_success_log, ticket_log))
                conn.commit()
                flash("تم حفظ إعدادات السيرفر وجميع اللوجات بنجاح!", "success")
                conn.close()
                return redirect(url_for("manage", guild_id=guild_id))

            elif action == "add_reply":
                trigger = request.form.get("trigger", "").strip().lower()
                reply = request.form.get("reply", "").strip()

                if trigger and reply:
                    cur.execute(
                        "INSERT OR REPLACE INTO auto_replies (trigger_word, reply_content) VALUES (?, ?)",
                        (trigger, reply)
                    )
                    conn.commit()
                    flash(f"تمت إضافة الرد التلقائي للكلمة ({trigger}) بنجاح!", "success")
                else:
                    flash("يرجى كتابة الكلمة ومحتوى الرد المطلوب.", "warning")

                conn.close()
                return redirect(url_for("manage", guild_id=guild_id))

            elif action == "delete_reply":
                trigger = request.form.get("trigger", "").strip().lower()
                if trigger:
                    cur.execute("DELETE FROM auto_replies WHERE trigger_word = ?", (trigger,))
                    conn.commit()
                    flash(f"تم حذف الرد التلقائي للكلمة ({trigger}) بنجاح!", "success")
                
                conn.close()
                return redirect(url_for("manage", guild_id=guild_id))

            elif action == "update_bot_identity" and is_premium:
                new_nick = request.form.get("bot_nick", "").strip()
                banner_link = request.form.get("banner_url", "").strip()
                avatar_link = request.form.get("avatar_url", "").strip()
                requested_currency_label = " ".join(request.form.get("currency_name", "").split())

                patch_payload = {}
                if new_nick:
                    patch_payload["nick"] = new_nick[:32]
                    cur.execute("UPDATE premium_info SET custom_name = ? WHERE guild_id = ?", (new_nick[:32], guild_id))

                if banner_link:
                    fetched = fetch_public_image(banner_link)
                    if fetched:
                        content_type, image_bytes = fetched
                        b64_str = base64.b64encode(image_bytes).decode("utf-8")
                        patch_payload["banner"] = f"data:{content_type};base64,{b64_str}"
                        cur.execute("UPDATE premium_info SET custom_banner_url = ? WHERE guild_id = ?", (banner_link, guild_id))
                        banner_url = banner_link
                    else:
                        flash("تعذر استخدام رابط البنر: يجب أن يكون HTTPS لصورة عامة صالحة وألا يتجاوز 8MB.", "warning")

                if avatar_link:
                    fetched = fetch_public_image(avatar_link)
                    if fetched:
                        content_type, image_bytes = fetched
                        b64_str = base64.b64encode(image_bytes).decode("utf-8")
                        patch_payload["avatar"] = f"data:{content_type};base64,{b64_str}"
                        cur.execute("UPDATE premium_info SET custom_avatar_url = ? WHERE guild_id = ?", (avatar_link, guild_id))
                        avatar_url = avatar_link
                    else:
                        flash("تعذر استخدام رابط الصورة: يجب أن يكون HTTPS لصورة عامة صالحة وألا يتجاوز 8MB.", "warning")

                if len(requested_currency_label) > 24 or any(not char.isprintable() for char in requested_currency_label) or any(char in requested_currency_label for char in "@`<>"):
                    flash("اسم العملة يجب أن يكون واضحًا وبحد أقصى 24 حرفًا، ومن دون رموز الإشارات أو backticks.", "warning")
                else:
                    stored_currency_label = (
                        None if not requested_currency_label or requested_currency_label.casefold() == "cosmo"
                        else requested_currency_label
                    )
                    cur.execute(
                        "INSERT INTO premium_info (guild_id, custom_currency_name) VALUES (?, ?) "
                        "ON CONFLICT(guild_id) DO UPDATE SET custom_currency_name = excluded.custom_currency_name",
                        (guild_id, stored_currency_label),
                    )
                    currency_label = stored_currency_label or "Cosmo"

                if patch_payload:
                    try:
                        requests.patch(
                            f"{DISCORD_API_BASE}/guilds/{guild_id}/members/@me",
                            headers=bot_headers,
                            json=patch_payload,
                            timeout=8
                        )
                    except Exception as e:
                        print(f"Error updating bot member in Discord: {e}")

                conn.commit()
                flash("تم تحديث مظهر وهوية البوت في السيرفر بنجاح!", "success")
                conn.close()
                return redirect(url_for("manage", guild_id=guild_id))

        # 3. قراءة الإعدادات الحالية
        cur.execute("""
            SELECT spending_balance, exchange_enabled, transfer_log, delivery_log, balance_log, exchange_success_log, ticket_log
            FROM settings WHERE guild_id = ?
        """, (guild_id,))
        s_row = cur.fetchone()

        spending_balance = s_row[0] if s_row and s_row[0] is not None else 0
        exchange_enabled = s_row[1] if s_row and s_row[1] is not None else 1
        transfer_log = str(s_row[2] or "") if s_row else ""
        delivery_log = str(s_row[3] or "") if s_row else ""
        balance_log = str(s_row[4] or "") if s_row else ""
        exchange_success_log = str(s_row[5] or "") if s_row else ""
        ticket_log = str(s_row[6] or "") if s_row else ""

        settings_data = {
            "spending_balance": spending_balance,
            "exchange_enabled": exchange_enabled,
            "transfer_log": transfer_log,
            "delivery_log": delivery_log,
            "balance_log": balance_log,
            "exchange_success_log": exchange_success_log,
            "ticket_log": ticket_log,
        }

        # العملات العامة موحدة من قاعدة سيرفر Cosmo ولا يمكن تعديلها من صفحة سيرفر أخرى.
        currencies = database.get_exchange_currencies(active_only=False)

        # 5. قراءة الردود
        replies = []
        try:
            cur.execute("SELECT trigger_word, reply_content FROM auto_replies ORDER BY trigger_word ASC")
            replies = cur.fetchall()
        except:
            pass

        conn.close()

        guild_info = {"name": f"Server {guild_id}"}
        try:
            g_res = requests.get(f"{DISCORD_API_BASE}/guilds/{guild_id}", headers=bot_headers, timeout=5)
            if g_res.status_code == 200:
                guild_info = g_res.json()
        except:
            pass

        text_channels = []
        try:
            c_res = requests.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/channels", headers=bot_headers, timeout=5)
            if c_res.status_code == 200:
                c_data = c_res.json()
                if isinstance(c_data, list):
                    text_channels = [c for c in c_data if isinstance(c, dict) and c.get("type") in [0, 5]]
        except:
            pass

        bot_current_nick = ""
        try:
            m_res = requests.get(f"{DISCORD_API_BASE}/guilds/{guild_id}/members/@me", headers=bot_headers, timeout=5)
            if m_res.status_code == 200:
                m_data = m_res.json()
                bot_current_nick = m_data.get("nick") or m_data.get("user", {}).get("username", "")
        except:
            pass

        return render_template(
            "manage.html",
            user=user,
            guild=guild_info,
            settings=settings_data,
            currencies=currencies,
            replies=replies,
            channels=text_channels,
            is_premium=is_premium,
            premium_expiry=premium_expiry_str,
            currency_label=currency_label,
            bot_nick=bot_current_nick,
            banner_url=banner_url,
            avatar_url=avatar_url
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"<div dir='rtl' style='padding:20px;font-family:sans-serif;'><h3>حدث خطأ أثناء فتح صفحة السيرفر:</h3><pre style='background:#f1f1f1;padding:10px;'>{e}</pre></div>", 500

@app.route("/profile", methods=["GET", "POST"])
def profile():
    user = session.get("user")
    token = session.get("access_token")
    if not user or not token:
        return redirect(url_for("login"))

    user_id = int(user["id"])
    now_ts = int(time.time())

    # 1. رصيد المستخدم
    balance = 0
    try:
        balance = get_balance(user_id)
    except:
        try:
            g_conn, g_cur = get_global_db()
            g_cur.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            r = g_cur.fetchone()
            if r:
                balance = r[0]
            g_conn.close()
        except:
            pass

    # 2. لا يشترط Premium: يجب أن يكون المستخدم مديرًا للسيرفر، والبوت موجودًا، والتبديل مفتوحًا.
    auth_headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{DISCORD_API_BASE}/users/@me/guilds", headers=auth_headers, timeout=8)
    user_guilds = r.json() if r.status_code == 200 else []
    bot_guild_ids = get_bot_guild_ids()
    global_currencies = database.get_exchange_currencies(active_only=True)
    currencies_data = [
        {"name": name, "rate": float(rate), "min": float(minimum)}
        for name, rate, _active, minimum in global_currencies
        if math.isfinite(float(rate)) and float(rate) > 0
        and math.isfinite(float(minimum)) and float(minimum) >= 0
    ]

    eligible_guilds = []
    guilds_dict = {}
    if isinstance(user_guilds, list):
        for g in user_guilds:
            try:
                gid = int(g.get("id", 0))
            except (TypeError, ValueError):
                continue
            guilds_dict[gid] = g.get("name", f"Server {gid}")
            manageable = guild_can_manage_exchange(g)
            if manageable and gid in bot_guild_ids and currencies_data:
                try:
                    s_conn, s_cur = get_guild_db(gid)
                    s_cur.execute("SELECT exchange_enabled, delivery_log FROM settings WHERE guild_id = ?", (gid,))
                    set_row = s_cur.fetchone()
                    ex_enabled = set_row[0] if set_row and set_row[0] is not None else 1
                    deliv_log_id = set_row[1] if set_row and len(set_row) > 1 else None
                    s_conn.close()
                    if ex_enabled == 1:
                        g_data = dict(g)
                        g_data["currencies"] = currencies_data
                        g_data["delivery_log"] = deliv_log_id
                        g_data["currency_label"] = database.get_currency_label(gid)
                        eligible_guilds.append(g_data)
                except Exception as ex:
                    print(f"[DEBUG] Check eligible guild {gid} error: {ex}")

    # 3. سجل التحويلات مرتباً من الأحدث للأقدم مع جلب الاسم والآيدي لكل طرف
    transfers = []
    try:
        g_conn, g_cur = get_global_db()
        g_cur.execute(
            "SELECT sender_id, receiver_id, amount, timestamp FROM transfer_logs WHERE sender_id = ? OR receiver_id = ? ORDER BY id DESC LIMIT 20",
            (user_id, user_id)
        )
        rows = g_cur.fetchall()
        for s_id, r_id, amt, t_stamp in rows:
            is_receiver = (r_id == user_id)
            other_id = s_id if is_receiver else r_id
            other_name = get_discord_username(other_id)
            transfers.append({
                "type": "استلام" if is_receiver else "إرسال",
                "is_receiver": is_receiver,
                "other_id": other_id,
                "other_name": other_name,
                "amount": amt,
                "timestamp": t_stamp
            })
        g_conn.close()
    except Exception as ex:
        print(f"[DEBUG] Transfers read error: {ex}")

    # 4. سجل طلبات التبديل (exchange_history) من الأحدث للأقدم
    exchanges = []
    try:
        g_conn, g_cur = get_global_db()
        g_cur.execute(
            "SELECT id, guild_id, cosmo_amount, target_currency, received_amount, status, timestamp, note "
            "FROM exchange_logs WHERE user_id = ? ORDER BY id DESC LIMIT 15",
            (user_id,)
        )
        ex_rows = g_cur.fetchall()
        for req_id, g_id, cosmo_amt, curr_name, recv_amt, status, t_stamp, note in ex_rows:
            server_name = guilds_dict.get(g_id, f"سيرفر ({g_id})")
            exchanges.append({
                "id": req_id,
                "server_name": server_name,
                "currency_label": database.get_currency_label(g_id),
                "cosmo_amount": cosmo_amt,
                "target_currency": curr_name,
                "received_amount": recv_amt,
                "status": status,
                "timestamp": t_stamp,
                "note": note or "لا يوجد"
            })
        g_conn.close()
    except Exception as ex:
        print(f"[DEBUG] Exchange logs read error: {ex}")

    # 5. معالجة طلب التبديل (مثل أمر exchange)
    if request.method == "POST":
        target_guild_id = request.form.get("guild_id", "")
        currency_name = request.form.get("currency_name", "").strip()
        amount_input = request.form.get("cosmo_amount", "").strip()
        note = request.form.get("notes", "").strip()[:500]
        if not note:
            note = "لا يوجد"

        try:
            parsed_amount = parse_amount(amount_input)
        except:
            flash("❌ صيغة المبلغ غير صحيحة.", "danger")
            return redirect(url_for("profile"))

        if parsed_amount <= 0:
            flash("❌ المبلغ يجب أن يكون أكبر من 0.", "danger")
            return redirect(url_for("profile"))

        selected_guild = next((g for g in eligible_guilds if str(g["id"]) == str(target_guild_id)), None)
        if not selected_guild:
            flash("❌ السيرفر المحدد غير متاح: يجب أن تديره، وأن يكون البوت موجودًا والتبديل مفتوحًا.", "danger")
            return redirect(url_for("profile"))

        gid = int(selected_guild["id"])
        curr_row = database.get_exchange_currency(currency_name)

        if not curr_row:
            flash(f"❌ العملة `{currency_name}` غير موجودة في الكتالوج العام.", "danger")
            return redirect(url_for("profile"))

        currency_name, rate, is_active, min_recv = curr_row
        if is_active == 0:
            flash(f"⛔ تبديل العملة العامة {currency_name} متوقف حاليًا.", "danger")
            return redirect(url_for("profile"))

        if not math.isfinite(float(rate)) or float(rate) <= 0 or not math.isfinite(float(min_recv)) or float(min_recv) < 0:
            flash("❌ إعداد سعر العملة غير صالح؛ تواصل مع إدارة البوت.", "danger")
            return redirect(url_for("profile"))

        total_received = round(parsed_amount * rate, 4)
        if total_received < min_recv:
            flash(f"❌ تم رفض طلبك تلقائياً: المبلغ المستحق (`{total_received} {currency_name}`) أقل من الحد الأدنى وهو `{min_recv} {currency_name}`.", "danger")
            return redirect(url_for("profile"))

        delivery_log_id = selected_guild.get("delivery_log")
        currency_label = selected_guild.get("currency_label", "Cosmo")
        if get_balance(user_id) < parsed_amount:
            flash(f"❌ رصيدك من {currency_label} لا يكفي.", "danger")
            return redirect(url_for("profile"))

        g_conn, g_cur = get_global_db()
        g_cur.execute("SELECT value FROM global_settings WHERE key = 'global_request_log'")
        global_req_row = g_cur.fetchone()

        if not global_req_row or not global_req_row[0]:
            g_conn.close()
            flash("⚠️ لم يقم مالك البوت بتعيين الروم العام لاستقبال الطلبات بعد (/set_global_request_log).", "warning")
            return redirect(url_for("profile"))

        req_channel_id = global_req_row[0]

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        req_id = create_exchange_request(gid, user_id, parsed_amount, currency_name, total_received, note, now_str)
        if req_id is None:
            g_conn.close()
            if database.has_pending_exchange_request(user_id):
                flash("⏳ لديك طلب تبديل سابق قيد الانتظار؛ لم يُخصم أي مبلغ جديد. انتظر معالجته قبل إنشاء طلب آخر.", "warning")
                return redirect(url_for("profile"))
            flash("❌ لم يعد رصيدك كافياً لإتمام الطلب؛ لم يُخصم أي مبلغ.", "danger")
            return redirect(url_for("profile"))
        g_conn.close()

        bot_headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
        
        invite_link = None
        try:
            inv_res = requests.get(f"{DISCORD_API_BASE}/guilds/{gid}/invites", headers=bot_headers, timeout=4)
            if inv_res.status_code == 200 and inv_res.json():
                invite_link = inv_res.json()[0].get("code")
                if invite_link:
                    invite_link = f"https://discord.gg/{invite_link}"
        except:
            pass

        server_title = selected_guild.get("name", f"Server {gid}")
        server_display = f"[{server_title}]({invite_link})" if invite_link else f"**{server_title}**"

        if delivery_log_id:
            channel_link = f"https://discord.com/channels/{gid}/{delivery_log_id}"
            delivery_display = f"[#روم-التسليم]({channel_link}) (<#{delivery_log_id}>)"
        else:
            delivery_display = "`لم يتم التحديد بعد`"

        embed = {
            "title": f"📥 طلب تبديل جديد #{req_id}",
            "color": 0xFFD700,
            "fields": [
                {"name": "🏛️ السيرفر", "value": server_display, "inline": True},
                {"name": "📦 روم التسليمات", "value": delivery_display, "inline": True},
                {"name": "👤 العضو", "value": f"<@{user_id}> (`{user_id}`)", "inline": False},
                {"name": f"🪙 مبلغ {currency_label}", "value": f"`{parsed_amount:,}`", "inline": True},
                {"name": f"💵 المستحق من ({currency_name})", "value": f"`{total_received}`", "inline": True},
                {"name": "📝 الوصف والملاحظات", "value": f"```{note}```", "inline": False}
            ],
            "footer": {"text": f"للقبول: /accept_exchange {req_id} | للرفض: /deny_exchange {req_id}"}
        }

        try:
            send_result = requests.post(
                f"{DISCORD_API_BASE}/channels/{req_channel_id}/messages",
                headers=bot_headers,
                json={"embeds": [embed]},
                timeout=5
            )
            if 400 <= send_result.status_code < 500:
                deny_exchange_request(req_id)
                flash("❌ تعذر إرسال الطلب للإدارة؛ أُعيد رصيدك تلقائياً.", "danger")
                return redirect(url_for("profile"))
            if not 200 <= send_result.status_code < 300:
                flash(
                    f"⚠️ تعذر تأكيد نتيجة إرسال الطلب #{req_id} (استجابة Discord غير نهائية). أبقينا الطلب والخصم قيد الانتظار لتجنب تكرار العملية؛ لا تعِد الإرسال وتواصل مع الإدارة للتحقق.",
                    "warning",
                )
                return redirect(url_for("profile"))
        except Exception as ex:
            print(f"[DEBUG] Send exchange request error: {ex}")
            flash(
                f"⚠️ لم نتمكن من تأكيد وصول الطلب #{req_id} بسبب انقطاع الاتصال. أبقينا الطلب والخصم قيد الانتظار لتجنب ازدواج العملية؛ لا تعِد الإرسال وتواصل مع الإدارة للتحقق.",
                "warning",
            )
            return redirect(url_for("profile"))

        flash(f"✅ تم إرسال طلب التبديل برقم `{req_id}` إلى الإدارة بنجاح.", "success")
        return redirect(url_for("profile"))

    return render_template(
        "profile.html",
        user=user,
        balance=balance,
        transfers=transfers,
        exchanges=exchanges,
        guilds=eligible_guilds
    )

def run_dashboard():
    app.run(host="0.0.0.0", port=2009, debug=False, use_reloader=False)

if __name__ == "__main__":
    run_dashboard()
