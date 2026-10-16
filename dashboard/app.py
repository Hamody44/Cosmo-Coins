import os
import sqlite3
import sys
from datetime import timedelta, datetime
import time
import requests
import base64
from flask import Flask, redirect, request, session, render_template, url_for, flash
from dotenv import load_dotenv

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# استيراد الدوال والأدوات المتاحة من database.py
import database
from database import get_guild_db, PRIMARY_OWNER_ID, update_balance, get_balance, parse_amount

load_dotenv(os.path.join(project_root, ".env"))

template_dir = os.path.join(current_dir, "templates")
static_dir = os.path.join(current_dir, "static")
app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

# جلسة دائمة لمدة 30 يوماً
app.secret_key = os.getenv("FLASK_SECRET_KEY", "cosmo_fixed_dashboard_secret_key_2026")
app.permanent_session_lifetime = timedelta(days=30)

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
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
    if configured_url:
        return f"{configured_url.rstrip('/')}/callback"
    return "https://domain-glaring-unpledged.ngrok-free.dev/callback"

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
    discord_auth_url = (
        f"https://discord.com/oauth2/authorize?client_id={CLIENT_ID}"
        f"&response_type=code&redirect_uri={redirect_uri}&scope=identify+guilds"
        f"&prompt=consent"
    )
    return redirect(discord_auth_url)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/callback")
def callback():
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
    r = requests.post(f"{DISCORD_API_BASE}/oauth2/token", data=data, headers=headers)
    token_data = r.json()
    access_token = token_data.get("access_token")

    if not access_token:
        return f"فشل تسجيل الدخول: {token_data.get('error_description', 'تحقق من Redirect URI')}", 400

    session["access_token"] = access_token
    auth_headers = {"Authorization": f"Bearer {access_token}"}
    user_res = requests.get(f"{DISCORD_API_BASE}/users/@me", headers=auth_headers).json()
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

    return render_template("servers.html", user=user, guilds=manageable_guilds)

@app.route("/manage/<int:guild_id>", methods=["GET", "POST"])
def manage(guild_id):
    user = session.get("user")
    token = session.get("access_token")
    if not user or not token:
        return redirect(url_for("login"))

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
            cur.execute("SELECT premium_end, custom_banner_url, custom_avatar_url FROM premium_info WHERE guild_id = ?", (guild_id,))
            prem_row = cur.fetchone()
            if prem_row and prem_row[0] and int(prem_row[0]) > now_ts:
                is_premium = True
                rem = int(prem_row[0]) - now_ts
                days = rem // 86400
                hours = (rem % 86400) // 3600
                premium_expiry_str = f"متبقي {days} يوم و {hours} ساعة ({datetime.fromtimestamp(int(prem_row[0])).strftime('%Y-%m-%d %H:%M')})"
                banner_url = prem_row[1] or ""
                avatar_url = prem_row[2] or ""
        except Exception as e:
            print(f"[DEBUG] Premium read error: {e}")

        # 2. معالجة إرسال البيانات (POST)
        if request.method == "POST":
            action = request.form.get("action")
            
            if action == "save_settings":
                exchange_enabled = 1 if request.form.get("exchange_enabled") == "on" else 0
                transfer_log = request.form.get("transfer_log") or None
                delivery_log = request.form.get("delivery_log") or None
                balance_log = request.form.get("balance_log") or None
                exchange_success_log = request.form.get("exchange_success_log") or None

                cur.execute("""
                    UPDATE settings 
                    SET exchange_enabled = ?, 
                        transfer_log = ?, 
                        delivery_log = ?, 
                        balance_log = ?, 
                        exchange_success_log = ? 
                    WHERE guild_id = ?
                """, (exchange_enabled, transfer_log, delivery_log, balance_log, exchange_success_log, guild_id))
                conn.commit()
                flash("تم حفظ إعدادات السيرفر وجميع اللوجات بنجاح!", "success")
                conn.close()
                return redirect(url_for("manage", guild_id=guild_id))

            elif action == "toggle_currency":
                c_name = request.form.get("currency_name", "").strip()
                new_status = int(request.form.get("status", 1))

                cur.execute(
                    "UPDATE exchange_currencies SET is_active = ? WHERE LOWER(currency_name) = LOWER(?)",
                    (new_status, c_name)
                )
                conn.commit()
                conn.close()
                flash(f"تم تحديث حالة عملة ({c_name}) بنجاح!", "success")
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

                patch_payload = {}
                if new_nick:
                    patch_payload["nick"] = new_nick[:32]
                    cur.execute("UPDATE premium_info SET custom_name = ? WHERE guild_id = ?", (new_nick[:32], guild_id))

                if banner_link:
                    try:
                        b_resp = requests.get(banner_link, timeout=5)
                        if b_resp.status_code == 200:
                            content_type = b_resp.headers.get("Content-Type", "image/png").split(";")[0]
                            b64_str = base64.b64encode(b_resp.content).decode("utf-8")
                            patch_payload["banner"] = f"data:{content_type};base64,{b64_str}"
                    except Exception as ex:
                        print(f"Error encoding banner: {ex}")
                    cur.execute("UPDATE premium_info SET custom_banner_url = ? WHERE guild_id = ?", (banner_link, guild_id))
                    banner_url = banner_link

                if avatar_link:
                    try:
                        a_resp = requests.get(avatar_link, timeout=5)
                        if a_resp.status_code == 200:
                            content_type = a_resp.headers.get("Content-Type", "image/png").split(";")[0]
                            b64_str = base64.b64encode(a_resp.content).decode("utf-8")
                            patch_payload["avatar"] = f"data:{content_type};base64,{b64_str}"
                    except Exception as ex:
                        print(f"Error encoding avatar: {ex}")
                    cur.execute("UPDATE premium_info SET custom_avatar_url = ? WHERE guild_id = ?", (avatar_link, guild_id))
                    avatar_url = avatar_link

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
            SELECT spending_balance, exchange_enabled, transfer_log, delivery_log, balance_log, exchange_success_log 
            FROM settings WHERE guild_id = ?
        """, (guild_id,))
        s_row = cur.fetchone()

        spending_balance = s_row[0] if s_row and s_row[0] is not None else 0
        exchange_enabled = s_row[1] if s_row and s_row[1] is not None else 1
        transfer_log = str(s_row[2] or "") if s_row else ""
        delivery_log = str(s_row[3] or "") if s_row else ""
        balance_log = str(s_row[4] or "") if s_row else ""
        exchange_success_log = str(s_row[5] or "") if s_row else ""

        settings_data = {
            "spending_balance": spending_balance,
            "exchange_enabled": exchange_enabled,
            "transfer_log": transfer_log,
            "delivery_log": delivery_log,
            "balance_log": balance_log,
            "exchange_success_log": exchange_success_log
        }

        # 4. قراءة العملات
        cur.execute("SELECT currency_name, exchange_rate, is_active FROM exchange_currencies")
        currencies = cur.fetchall()

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

    # 2. جلب سيرفرات المستخدم والتحقق من (وجود البوت + اشتراك البريميوم + نظام التبديل مفعل)
    auth_headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{DISCORD_API_BASE}/users/@me/guilds", headers=auth_headers)
    user_guilds = r.json() if r.status_code == 200 else []
    bot_guild_ids = get_bot_guild_ids()

    eligible_guilds = []
    guilds_dict = {}
    if isinstance(user_guilds, list):
        for g in user_guilds:
            gid = int(g.get("id", 0))
            guilds_dict[gid] = g.get("name", f"Server {gid}")
            if gid in bot_guild_ids:
                try:
                    s_conn, s_cur = get_guild_db(gid)
                    # فحص البريميوم
                    s_cur.execute("SELECT premium_end FROM premium_info WHERE guild_id = ?", (gid,))
                    prem_row = s_cur.fetchone()
                    is_prem = prem_row and prem_row[0] and int(prem_row[0]) > now_ts

                    # فحص قفل التبديل العام
                    s_cur.execute("SELECT exchange_enabled, delivery_log FROM settings WHERE guild_id = ?", (gid,))
                    set_row = s_cur.fetchone()
                    ex_enabled = set_row[0] if set_row and set_row[0] is not None else 1
                    deliv_log_id = set_row[1] if set_row and len(set_row) > 1 else None

                    if is_prem and ex_enabled == 1:
                        # جلب العملات المفعلة في هذا السيرفر
                        s_cur.execute("SELECT currency_name, exchange_rate, min_received FROM exchange_currencies WHERE is_active = 1")
                        curr_rows = s_cur.fetchall()
                        currencies_data = [
                            {"name": c[0], "rate": float(c[1]), "min": float(c[2])}
                            for c in curr_rows
                        ]
                        if currencies_data:
                            g_data = dict(g)
                            g_data["currencies"] = currencies_data
                            g_data["delivery_log"] = deliv_log_id
                            eligible_guilds.append(g_data)
                    s_conn.close()
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
        target_guild_id = request.form.get("guild_id")
        currency_name = request.form.get("currency_name", "").strip()
        amount_input = request.form.get("cosmo_amount", "").strip()
        note = request.form.get("notes", "").strip()
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

        current_user_bal = get_balance(user_id)
        if current_user_bal < parsed_amount:
            flash("❌ رصيدك من Cosmo لا يكفي.", "danger")
            return redirect(url_for("profile"))

        selected_guild = next((g for g in eligible_guilds if str(g["id"]) == str(target_guild_id)), None)
        if not selected_guild:
            flash("❌ السيرفر المحدد غير متاح أو لا يمتلك اشتراك بريميوم نشط.", "danger")
            return redirect(url_for("profile"))

        gid = int(selected_guild["id"])
        s_conn, s_cur = get_guild_db(gid)
        s_cur.execute("SELECT exchange_rate, is_active, min_received FROM exchange_currencies WHERE LOWER(currency_name) = LOWER(?)", (currency_name,))
        curr_row = s_cur.fetchone()
        
        s_cur.execute("SELECT delivery_log FROM settings WHERE guild_id = ?", (gid,))
        set_row = s_cur.fetchone()
        delivery_log_id = set_row[0] if set_row and set_row[0] else None
        s_conn.close()

        if not curr_row:
            flash(f"❌ العملة `{currency_name}` غير متوفرة في هذا السيرفر.", "danger")
            return redirect(url_for("profile"))

        rate, is_active, min_recv = curr_row
        if is_active == 0:
            flash(f"⛔ تبديل عملة {currency_name} متوقف ومقفل حالياً.", "danger")
            return redirect(url_for("profile"))

        total_received = round(parsed_amount * rate, 4)
        if total_received < min_recv:
            flash(f"❌ تم رفض طلبك تلقائياً: المبلغ المستحق (`{total_received} {currency_name}`) أقل من الحد الأدنى وهو `{min_recv} {currency_name}`.", "danger")
            return redirect(url_for("profile"))

        g_conn, g_cur = get_global_db()
        g_cur.execute("SELECT value FROM global_settings WHERE key = 'global_request_log'")
        global_req_row = g_cur.fetchone()

        if not global_req_row or not global_req_row[0]:
            g_conn.close()
            flash("⚠️ لم يقم مالك البوت بتعيين الروم العام لاستقبال الطلبات بعد (/set_global_request_log).", "warning")
            return redirect(url_for("profile"))

        req_channel_id = global_req_row[0]

        update_balance(user_id, -parsed_amount)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        g_cur.execute(
            "INSERT INTO exchange_logs (guild_id, user_id, cosmo_amount, target_currency, received_amount, note, status, timestamp) VALUES (?, ?, ?, ?, ?, ?, 'قيد الانتظار', ?)",
            (gid, user_id, parsed_amount, currency_name, total_received, note, now_str)
        )
        req_id = g_cur.lastrowid
        g_conn.commit()
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
                {"name": "🪙 مبلغ Cosmo", "value": f"`{parsed_amount:,}`", "inline": True},
                {"name": f"💵 المستحق من ({currency_name})", "value": f"`{total_received}`", "inline": True},
                {"name": "📝 الوصف والملاحظات", "value": f"```{note}```", "inline": False}
            ],
            "footer": {"text": f"للقبول: /accept_exchange {req_id} | للرفض: /deny_exchange {req_id}"}
        }

        try:
            requests.post(
                f"{DISCORD_API_BASE}/channels/{req_channel_id}/messages",
                headers=bot_headers,
                json={"embeds": [embed]},
                timeout=5
            )
        except Exception as ex:
            print(f"[DEBUG] Send exchange request error: {ex}")

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
