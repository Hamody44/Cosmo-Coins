import asyncio
import html
import io
import os
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

from database import (
    PRIMARY_OWNER_ID,
    get_guild_db,
    get_guild_management_role_ids,
    has_guild_role,
    is_bot_admin,
    parse_amount,
    payout_from_server_budget,
)
from leveling_store import (
    XP_PER_LEVEL,
    add_level_channel,
    ensure_level_config,
    finish_daily_report,
    get_level_channels,
    get_level_leaderboard,
    get_level_profile,
    get_report_status,
    get_retryable_payouts,
    get_salary_history,
    get_salary_tiers,
    mature_pending_messages,
    mark_daily_report_send_attempt,
    pending_reports,
    queue_payout,
    record_report_failure,
    record_pending_message,
    remove_level_channel,
    save_daily_report_message,
    set_announcement_channel,
    set_leveling_enabled,
    set_milestone_reward,
    set_salary_tier,
    should_track_channel,
    start_daily_report,
)

RIYADH = ZoneInfo("Asia/Riyadh")
REPORT_HOUR = 23
REPORT_MINUTE = 55
SALARY_THRESHOLDS = (500, 1000, 2000, 3000)
MAX_DAILY_REPORT_CATCHUP_DAYS = 31


def can_manage_level_settings(ctx: commands.Context) -> bool:
    if not ctx.guild or not ctx.author:
        return False
    if ctx.author.id in (PRIMARY_OWNER_ID, ctx.guild.owner_id):
        return True
    permissions = getattr(ctx.author, "guild_permissions", None)
    if permissions and (permissions.administrator or permissions.manage_guild):
        return True
    return is_bot_admin(ctx)


def period_bounds(period: str, now: datetime | None = None):
    """Return UTC epoch bounds for a Riyadh-local calendar period."""
    period = period.strip().lower()
    aliases = {
        "daily": "daily", "day": "daily", "يومي": "daily", "اليومي": "daily",
        "weekly": "weekly", "week": "weekly", "اسبوعي": "weekly", "أسبوعي": "weekly", "الأسبوعي": "weekly",
        "monthly": "monthly", "month": "monthly", "شهري": "monthly", "الشهري": "monthly",
        "all": "all", "alltime": "all", "all-time": "all", "ابدي": "all", "أبدي": "all", "الكل": "all",
    }
    normalized = aliases.get(period)
    if not normalized:
        raise ValueError("اختر daily أو weekly أو monthly أو all.")
    if normalized == "all":
        return None, None
    local_now = (now or datetime.now(RIYADH)).astimezone(RIYADH)
    local_day = local_now.date()
    if normalized == "daily":
        first_day = local_day
    elif normalized == "weekly":
        first_day = local_day - timedelta(days=local_day.weekday())
    else:
        first_day = local_day.replace(day=1)
    start = datetime.combine(first_day, datetime_time.min, tzinfo=RIYADH)
    if normalized == "daily":
        end = start + timedelta(days=1)
    elif normalized == "weekly":
        end = start + timedelta(days=7)
    else:
        next_month = (first_day.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = datetime.combine(next_month, datetime_time.min, tzinfo=RIYADH)
    return int(start.timestamp()), int(end.timestamp())


def daily_report_dates_due(enabled_since: float | None, last_report_date: str | None, now: datetime) -> list[str]:
    """Return missing active local dates through today's 23:55 report window."""
    if enabled_since is None:
        return []
    local_now = now.astimezone(RIYADH)
    last_due_date = local_now.date()
    if local_now.hour < REPORT_HOUR or (local_now.hour == REPORT_HOUR and local_now.minute < REPORT_MINUTE):
        last_due_date -= timedelta(days=1)

    first_date = datetime.fromtimestamp(float(enabled_since), RIYADH).date()
    if last_report_date:
        first_date = max(first_date, date.fromisoformat(last_report_date) + timedelta(days=1))
    if first_date > last_due_date:
        return []

    first_allowed = last_due_date - timedelta(days=MAX_DAILY_REPORT_CATCHUP_DAYS - 1)
    first_date = max(first_date, first_allowed)
    return [
        (first_date + timedelta(days=offset)).isoformat()
        for offset in range((last_due_date - first_date).days + 1)
    ]


def _safe_url(url: str | None) -> str:
    return html.escape(url or "", quote=True)


def _render_png(document: str) -> bytes:
    """Render the exact-value card from HTML using a headless browser."""
    from playwright.sync_api import sync_playwright

    chromium_path = os.getenv("LEVEL_CARD_CHROMIUM", "/usr/bin/chromium")
    with sync_playwright() as playwright:
        launch_options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        if os.path.exists(chromium_path):
            launch_options["executable_path"] = chromium_path
        browser = playwright.chromium.launch(**launch_options)
        page = browser.new_page(viewport={"width": 1024, "height": 1000}, device_scale_factor=1.5)
        page.set_content(document, wait_until="domcontentloaded", timeout=15000)
        try:
            page.evaluate("Promise.all(Array.from(document.images).map(image => image.decode().catch(() => null)))")
        except Exception:
            pass
        card = page.locator("main.card")
        png = card.screenshot(type="png", animations="disabled")
        browser.close()
        return png


def _card_shell(content: str, server_name: str, server_icon: str | None) -> str:
    server_icon = _safe_url(server_icon)
    server_name = html.escape(server_name)
    icon_html = (
        f'<img class="server-icon" src="{server_icon}" alt="" onerror="this.style.display=\'none\'">'
        if server_icon else '<div class="server-icon fallback">✦</div>'
    )
    return f'''<!doctype html>
<html lang="ar" dir="rtl">
<head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 20px; background: #0b1024; color: #f4f2ff; font-family: "Noto Sans Arabic", "Segoe UI", Arial, sans-serif; }}
main.card {{ width: 960px; padding: 34px; border-radius: 30px; overflow: hidden; background: radial-gradient(circle at 12% 6%, #3c2c79 0, transparent 27%), radial-gradient(circle at 93% 90%, #13546b 0, transparent 28%), linear-gradient(145deg, #11162f, #191d3d 56%, #101b34); border: 1px solid rgba(202,194,255,.25); box-shadow: 0 22px 70px rgba(0,0,0,.38); }}
.server {{ display:flex; align-items:center; gap:14px; padding-bottom:22px; border-bottom:1px solid rgba(255,255,255,.12); }}
.server-icon {{ width:52px; height:52px; border-radius:17px; object-fit:cover; background:#4d3c8b; display:grid; place-items:center; }}
.server-name {{ font-size:22px; font-weight:800; }}
.brand {{ color:#aaa3d1; font-size:13px; margin-top:4px; letter-spacing:.3px; }}
h1 {{ font-size:30px; margin:24px 0 7px; }}
.subtitle {{ color:#c1bddb; font-size:15px; margin-bottom:20px; }}
.profile {{ display:flex; align-items:center; gap:22px; padding:22px; background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.12); border-radius:22px; }}
.avatar {{ width:112px; height:112px; border-radius:50%; object-fit:cover; border:4px solid #b8a4ff; background:#252b4e; }}
.name {{ font-size:25px; font-weight:800; }}
.meta {{ margin-top:7px; color:#bcb8d3; font-size:14px; }}
.stats {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:17px; }}
.stat {{ padding:18px; border-radius:18px; background:rgba(255,255,255,.065); border:1px solid rgba(255,255,255,.1); }}
.label {{ color:#bcb8d3; font-size:13px; }}
.value {{ font-size:25px; font-weight:800; margin-top:7px; color:#fff; }}
.progress-wrap {{ margin-top:19px; padding:18px 20px; border-radius:18px; background:rgba(255,255,255,.06); }}
.progress-head {{ display:flex; justify-content:space-between; color:#d0cce8; font-size:13px; margin-bottom:11px; }}
.track {{ height:13px; background:#303455; border-radius:20px; overflow:hidden; }}
.bar {{ height:100%; border-radius:20px; background:linear-gradient(90deg,#65e6d2,#bba2ff); }}
.rows {{ display:grid; gap:11px; margin-top:14px; }}
.row {{ display:grid; grid-template-columns:55px 1fr auto auto; align-items:center; gap:14px; padding:13px 16px; background:rgba(255,255,255,.065); border:1px solid rgba(255,255,255,.09); border-radius:17px; }}
.rank {{ color:#c5b8ff; font-weight:900; font-size:18px; text-align:center; }}
.row-user {{ display:flex; align-items:center; gap:12px; min-width:0; }}
.row-avatar {{ width:42px; height:42px; border-radius:50%; object-fit:cover; background:#303455; }}
.row-name {{ font-weight:750; font-size:16px; overflow-wrap:anywhere; }}
.metric {{ min-width:120px; text-align:left; }}
.metric-label {{ color:#bcb8d3; font-size:12px; }}
.metric-value {{ font-weight:800; font-size:16px; margin-top:3px; }}
.empty {{ padding:28px; text-align:center; color:#cac5e3; background:rgba(255,255,255,.06); border-radius:18px; }}
.footer {{ color:#9b97b9; margin-top:19px; font-size:12px; }}
@media (max-width:900px) {{ main.card {{ width:920px; }} }}
</style></head><body><main class="card">
<div class="server">{icon_html}<div><div class="server-name">{server_name}</div><div class="brand">COSMO • نظام المستويات</div></div></div>
{content}
<div class="footer">معلومات هذا النظام محفوظة لهذا السيرفر فقط • توقيت الرياض</div>
</main></body></html>'''


def profile_card_html(server_name: str, server_icon: str | None, display_name: str, avatar: str | None, profile: dict) -> str:
    avatar = _safe_url(avatar)
    name = html.escape(display_name)
    pct = max(0, min(100, int(profile["progress"])))
    content = f'''<h1>بطاقة المستوى</h1><div class="subtitle">ملفك ونشاطك في هذا السيرفر</div>
<div class="profile"><img class="avatar" src="{avatar}" alt="" onerror="this.style.visibility='hidden'"><div><div class="name">{name}</div><div class="meta">الترتيب العام: <b>#{profile['rank']}</b> • الرسائل غير المزعجة: <b>{profile['messages']:,}</b></div></div></div>
<div class="stats"><div class="stat"><div class="label">المستوى الحالي</div><div class="value">{profile['level']}</div></div>
<div class="stat"><div class="label">نقاطك</div><div class="value">{profile['points']:,}</div></div>
<div class="stat"><div class="label">المتبقي للمستوى التالي</div><div class="value">{profile['next_level_points']:,}</div></div></div>
<div class="progress-wrap"><div class="progress-head"><span>التقدم للمستوى {profile['level'] + 1}</span><span>{pct}%</span></div><div class="track"><div class="bar" style="width:{pct}%"></div></div></div>'''
    return _card_shell(content, server_name, server_icon)


def leaderboard_card_html(server_name: str, server_icon: str | None, title: str, entries: list[dict]) -> str:
    title = html.escape(title)
    if entries:
        rows = []
        medals = ("01", "02", "03", "04", "05")
        for index, entry in enumerate(entries):
            avatar = _safe_url(entry.get("avatar"))
            name = html.escape(entry["name"])
            avatar_html = f'<img class="row-avatar" src="{avatar}" alt="" onerror="this.style.visibility=\'hidden\'">' if avatar else '<div class="row-avatar"></div>'
            rows.append(
                f'<div class="row"><div class="rank">#{medals[index]}</div>'
                f'<div class="row-user">{avatar_html}<div class="row-name">{name}</div></div>'
                f'<div class="metric"><div class="metric-label">النقاط المكتسبة</div><div class="metric-value">{entry["points"]:,}</div></div>'
                f'<div class="metric"><div class="metric-label">الرسائل المقبولة</div><div class="metric-value">{entry["messages"]:,}</div></div></div>'
            )
        rows_html = '<div class="rows">' + "".join(rows) + "</div>"
    else:
        rows_html = '<div class="empty">لا توجد رسائل محتسبة في هذه الفترة بعد.</div>'
    content = f'<h1>لوحة المتصدرين</h1><div class="subtitle">{title} • أفضل خمسة أعضاء</div>{rows_html}'
    return _card_shell(content, server_name, server_icon)


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not self.leveling_worker.is_running():
            self.leveling_worker.start()

    def cog_unload(self):
        self.leveling_worker.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (
            not message.guild
            or not message.author
            or message.author.bot
            or message.webhook_id
            or message.type is not discord.MessageType.default
            or message.content.startswith(("!", "/"))
        ):
            return
        try:
            track = await asyncio.to_thread(should_track_channel, message.guild.id, message.channel.id)
            if not track:
                return
            result = await asyncio.to_thread(
                record_pending_message,
                message.guild.id,
                message.author.id,
                message.id,
                message.channel.id,
                message.created_at.timestamp(),
            )
            if result.get("status") == "spam" and result.get("warn"):
                try:
                    await message.author.send(
                        f"⛔ لم تُحتسب رسائلك في سيرفر **{message.guild.name}**: أُرسل 5 رسائل أو أكثر خلال 10 ثوانٍ. "
                        "توقّف قليلاً ثم تابع المحادثة بهدوء."
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
        except Exception as exc:
            print(f"Leveling message listener error in guild {message.guild.id}: {exc}")

    @tasks.loop(seconds=15)
    async def leveling_worker(self):
        for guild in list(self.bot.guilds):
            try:
                await asyncio.to_thread(mature_pending_messages, guild.id)
                await self.process_retryable_payouts(guild)
                config = await asyncio.to_thread(ensure_level_config, guild.id)
                if config["enabled"] and config["announcement_channel_id"]:
                    local_now = datetime.now(RIYADH)
                    due_days = daily_report_dates_due(
                        config["enabled_since"], config["last_daily_report_date"], local_now
                    )
                    for day in due_days:
                        status = await asyncio.to_thread(get_report_status, guild.id, day)
                        if not status or (status[2] in ("pending", "failed") and status[0] != config["announcement_channel_id"]):
                            await asyncio.to_thread(start_daily_report, guild.id, day, config["announcement_channel_id"])
                reports = await asyncio.to_thread(pending_reports, guild.id)
                if reports:
                    local_day, channel_id = reports[0]
                    try:
                        await self.publish_daily_report(guild, local_day, channel_id)
                    except Exception as exc:
                        print(f"Daily leveling report worker error in guild {guild.id} for {local_day}: {exc}")
                        await asyncio.to_thread(record_report_failure, guild.id, local_day, str(exc))
            except Exception as exc:
                print(f"Leveling worker error in guild {guild.id}: {exc}")

    @leveling_worker.before_loop
    async def before_leveling_worker(self):
        await self.bot.wait_until_ready()

    async def process_retryable_payouts(self, guild: discord.Guild):
        payouts = await asyncio.to_thread(get_retryable_payouts, guild.id)
        for payout in payouts:
            try:
                result = await asyncio.to_thread(
                    payout_from_server_budget,
                    guild.id,
                    payout["user_id"],
                    payout["amount"],
                    payout["payout_key"],
                    payout["payout_type"],
                )
                if result["status"] not in ("paid", "already_paid"):
                    continue
                if payout["payout_type"] == "milestone":
                    member = guild.get_member(payout["user_id"])
                    if member:
                        milestone = payout["payout_key"].rsplit(":", 1)[-1]
                        await member.send(
                            f"🎉 وصلت إلى المستوى `{milestone}` في سيرفر **{guild.name}**، "
                            f"واستلمت مكافأة `{payout['amount']:,}` Cosmo من ميزانية السيرفر."
                        )
                elif payout["payout_type"] == "salary" and payout["status"] in ("insufficient", "blocked_wal"):
                    config = await asyncio.to_thread(ensure_level_config, guild.id)
                    channel_id = config["announcement_channel_id"]
                    channel = guild.get_channel(channel_id) if channel_id else None
                    if channel:
                        await channel.send(
                            f"✅ تم استكمال راتب <@{payout['user_id']}> (`{payout['amount']:,}` Cosmo) "
                            "بعد إعادة توفر ميزانية السيرفر.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
            except (discord.Forbidden, discord.HTTPException) as exc:
                print(f"Leveling payout notification failed in guild {guild.id} for {payout['payout_key']}: {exc}")
            except Exception as exc:
                print(f"Leveling payout processing failed in guild {guild.id} for {payout['payout_key']}: {exc}")

    async def publish_daily_report(self, guild: discord.Guild, local_day: str, channel_id: int):
        report_status = await asyncio.to_thread(get_report_status, guild.id, local_day)
        if report_status and report_status[2] == "sent":
            return
        if not report_status:
            await asyncio.to_thread(start_daily_report, guild.id, local_day, channel_id)
            report_status = await asyncio.to_thread(get_report_status, guild.id, local_day)
        prior_send_at = float(report_status[7]) if report_status and report_status[7] is not None else None
        prior_send_channel_id = int(report_status[8]) if report_status and report_status[8] is not None else None
        prior_message_id = int(report_status[1]) if report_status and report_status[1] is not None else None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                print(f"Daily leveling report channel unavailable in guild {guild.id}: {exc}")
                await asyncio.to_thread(record_report_failure, guild.id, local_day, f"channel unavailable: {exc}")
                return
        start_local = datetime.combine(date.fromisoformat(local_day), datetime_time.min, tzinfo=RIYADH)
        end_local = start_local + timedelta(days=1)
        start_at, end_at = int(start_local.timestamp()), int(end_local.timestamp())
        entries = await asyncio.to_thread(get_level_leaderboard, guild.id, start_at, end_at, 5)

        salaries = []
        salary_processing_error = False
        try:
            _owner_role_id, admin_role_id = await asyncio.to_thread(get_guild_management_role_ids, guild.id)
            salary_tiers = await asyncio.to_thread(get_salary_tiers, guild.id)
            salary_entries = entries
        except Exception as exc:
            print(f"Daily salary configuration lookup failed in guild {guild.id}: {exc}")
            salary_processing_error = True
            admin_role_id = None
            salary_tiers = []
            salary_entries = []
            salaries.extend({
                "user_id": entry["user_id"],
                "messages": entry["messages"],
                "threshold": 0,
                "amount": 0,
                "status": "error",
            } for entry in entries)

        for entry in salary_entries:
            try:
                member = guild.get_member(entry["user_id"])
                if not member and admin_role_id:
                    try:
                        member = await guild.fetch_member(entry["user_id"])
                    except discord.NotFound:
                        member = None
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        raise RuntimeError(f"could not verify salary role for {entry['user_id']}: {exc}") from exc
                if not member or not has_guild_role(member, admin_role_id):
                    continue
                eligible_tiers = [tier for tier in salary_tiers if tier[0] <= entry["messages"]]
                if not eligible_tiers:
                    salaries.append({"user_id": entry["user_id"], "messages": entry["messages"], "threshold": 0, "amount": 0, "status": "below"})
                    continue
                threshold, amount = max(eligible_tiers, key=lambda tier: tier[0])
                if amount <= 0:
                    salaries.append({"user_id": entry["user_id"], "messages": entry["messages"], "threshold": threshold, "amount": 0, "status": "disabled"})
                    continue
                payout_key = f"salary:{guild.id}:{local_day}:{entry['user_id']}"
                queued = await asyncio.to_thread(
                    queue_payout,
                    guild.id,
                    payout_key,
                    entry["user_id"],
                    "salary",
                    amount,
                    local_date=local_day,
                    message_count=entry["messages"],
                    tier_threshold=threshold,
                )
                amount = queued["amount"]
                message_count = queued["message_count"] if queued["message_count"] is not None else entry["messages"]
                threshold = queued["tier_threshold"] if queued["tier_threshold"] is not None else threshold
                result = await asyncio.to_thread(
                    payout_from_server_budget,
                    guild.id,
                    entry["user_id"],
                    amount,
                    payout_key,
                    "salary",
                )
                salaries.append({
                    "user_id": entry["user_id"],
                    "messages": message_count,
                    "threshold": threshold,
                    "amount": amount,
                    "status": (
                        "paid" if result["status"] in ("paid", "already_paid")
                        else "blocked_wal" if result["status"] == "blocked_wal"
                        else "insufficient"
                    ),
                })
            except Exception as exc:
                salary_processing_error = True
                print(f"Daily salary processing failed in guild {guild.id} for {entry['user_id']}: {exc}")
                salaries.append({
                    "user_id": entry["user_id"],
                    "messages": entry["messages"],
                    "threshold": 0,
                    "amount": 0,
                    "status": "error",
                })

        marker = f"level-daily:{guild.id}:{local_day}"

        card_entries = await self.prepare_card_entries(guild, entries)
        document = leaderboard_card_html(guild.name, guild.icon.url if guild.icon else None, f"الترتيب اليومي • {local_day}", card_entries)
        try:
            image_bytes = await asyncio.to_thread(_render_png, document)
        except Exception as exc:
            image_bytes = None
            print(f"Level card render failed for guild {guild.id}: {exc}")

        if salaries:
            salary_lines = []
            for salary in salaries:
                if salary["status"] in ("below", "disabled"):
                    continue
                mention = f"<@{salary['user_id']}>"
                if salary["status"] == "paid":
                    salary_lines.append(
                        f"• {mention}: {salary['messages']:,} رسالة — شريحة {salary['threshold']:,} — `+{salary['amount']:,}` Cosmo."
                    )
                elif salary["status"] == "blocked_wal":
                    salary_lines.append(
                        f"• {mention}: تعذر الصرف تلقائياً بسبب إعداد journal_mode في قاعدة البيانات؛ يلزم تدخل مالك البوت."
                    )
                elif salary["status"] == "error":
                    salary_lines.append(
                        f"• {mention}: تعذر التحقق أو تجهيز الراتب مؤقتاً؛ سيعيد البوت المحاولة."
                    )
                else:
                    salary_lines.append(
                        f"• {mention}: {salary['messages']:,} رسالة — شريحة {salary['threshold']:,} — تعذّر الصرف لعدم كفاية ميزانية السيرفر."
                    )
            salary_text = "\n".join(salary_lines) if salary_lines else "لا يوجد إداري ضمن التوب 5 بلغ الحد الأدنى لشريحة الراتب."
        else:
            salary_text = "لا يوجد إداري مستحق للراتب ضمن التوب 5 اليوم."

        ranking_lines = [
            f"{index}. <@{entry['user_id']}> — `{entry['points']:,}` نقطة / `{entry['messages']:,}` رسالة"
            for index, entry in enumerate(entries, 1)
        ]
        ranking_text = "\n".join(ranking_lines) if ranking_lines else "لا توجد رسائل محتسبة اليوم."

        embed = discord.Embed(
            title=f"🏆 أفضل 5 لليوم | {guild.name}",
            description=(
                f"ترتيب اليوم حسب النقاط المكتسبة. الرواتب تحتسب الرسائل المقبولة غير المزعجة.\n\n"
                f"**التوب 5:**\n{ranking_text}\n\n**الرواتب:**\n{salary_text}"
            ),
            color=discord.Color.from_rgb(123, 103, 238),
            timestamp=datetime.now(RIYADH),
        )
        embed.set_footer(text=marker)
        if prior_send_at is not None:
            reconcile_channel_id = prior_send_channel_id or channel_id
            reconcile_channel = channel if reconcile_channel_id == channel_id else guild.get_channel(reconcile_channel_id)
            if reconcile_channel is None:
                try:
                    reconcile_channel = await self.bot.fetch_channel(reconcile_channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                    print(f"Cannot inspect previous report channel in guild {guild.id}: {exc}")
                    await asyncio.to_thread(record_report_failure, guild.id, local_day, f"prior report channel unavailable: {exc}")
                    return

            report_message = None
            if prior_message_id:
                try:
                    candidate = await reconcile_channel.fetch_message(prior_message_id)
                    if any(item.footer and item.footer.text == marker for item in candidate.embeds):
                        report_message = candidate
                except discord.NotFound:
                    pass
                except (discord.Forbidden, discord.HTTPException) as exc:
                    print(f"Cannot fetch prior report in guild {guild.id}: {exc}")
                    await asyncio.to_thread(record_report_failure, guild.id, local_day, f"prior report fetch failed: {exc}")
                    return

            if report_message is None:
                try:
                    report_message = await self.find_report_message(reconcile_channel, marker, prior_send_at)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    print(f"Cannot inspect report history in guild {guild.id}; refusing duplicate send: {exc}")
                    await asyncio.to_thread(record_report_failure, guild.id, local_day, f"report history unavailable: {exc}")
                    return

            if report_message:
                if report_message.embeds:
                    old_image_url = report_message.embeds[0].image.url
                    if old_image_url:
                        embed.set_image(url=old_image_url)
                try:
                    await report_message.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                except (discord.Forbidden, discord.HTTPException) as exc:
                    print(f"Could not update existing report in guild {guild.id}: {exc}")
                    await asyncio.to_thread(record_report_failure, guild.id, local_day, f"report edit failed: {exc}")
                    return
                if salary_processing_error:
                    await asyncio.to_thread(
                        record_report_failure,
                        guild.id,
                        local_day,
                        "one or more salary entries could not be processed; retry after backoff",
                    )
                else:
                    await asyncio.to_thread(finish_daily_report, guild.id, local_day, report_message.id)
                return

        await asyncio.to_thread(mark_daily_report_send_attempt, guild.id, local_day, channel.id)
        try:
            if image_bytes:
                file = discord.File(io.BytesIO(image_bytes), filename="daily-level-top5.png")
                embed.set_image(url="attachment://daily-level-top5.png")
                message = await channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
            else:
                message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            if salary_processing_error:
                await asyncio.to_thread(save_daily_report_message, guild.id, local_day, message.id)
                await asyncio.to_thread(
                    record_report_failure,
                    guild.id,
                    local_day,
                    "one or more salary entries could not be processed; retry after backoff",
                )
            else:
                await asyncio.to_thread(finish_daily_report, guild.id, local_day, message.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"Could not send daily leveling report in guild {guild.id}: {exc}")
            await asyncio.to_thread(record_report_failure, guild.id, local_day, f"report send failed: {exc}")

    async def find_report_message(self, channel, marker: str, send_attempt_at: float):
        after = datetime.fromtimestamp(send_attempt_at, tz=timezone.utc)
        async for message in channel.history(limit=None, after=after):
            if any(embed.footer and embed.footer.text == marker for embed in message.embeds):
                return message
        return None

    async def prepare_card_entries(self, guild: discord.Guild, entries: list[dict]) -> list[dict]:
        prepared = []
        for entry in entries:
            member = guild.get_member(entry["user_id"])
            if member:
                name = member.display_name
                avatar = member.display_avatar.url
            else:
                name = f"عضو {entry['user_id']}"
                avatar = None
            prepared.append({**entry, "name": name, "avatar": avatar})
        return prepared

    @commands.hybrid_command(name="toggle_level_system", description="فتح أو إغلاق نظام النقاط واللفلات لهذا السيرفر")
    async def toggle_level_system(self, ctx: commands.Context, enabled: bool):
        if not ctx.guild or not can_manage_level_settings(ctx):
            return await ctx.send("⛔ مالك السيرفر أو owner-role أو إدارة Discord فقط يمكنهم إعداد النظام.")
        await asyncio.to_thread(set_leveling_enabled, ctx.guild.id, enabled)
        state = "مفتوح ويحتسب الرسائل في الرومات المحددة ✅" if enabled else "مغلق ولن يحتسب رسائل جديدة ⛔"
        await ctx.send(f"⚙️ نظام اللفلات في **{ctx.guild.name}** أصبح {state}.")

    @commands.hybrid_command(name="add_level_channel", description="السماح باحتساب الرسائل في روم لهذا السيرفر")
    async def add_level_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not ctx.guild or not can_manage_level_settings(ctx):
            return await ctx.send("⛔ لا تملك صلاحية إعداد نظام اللفلات.")
        if channel.guild.id != ctx.guild.id:
            return await ctx.send("❌ يجب اختيار روم من هذا السيرفر.")
        added = await asyncio.to_thread(add_level_channel, ctx.guild.id, channel.id)
        await ctx.send(f"✅ تمت إضافة {channel.mention} إلى رومات احتساب النقاط." if added else f"ℹ️ {channel.mention} موجود بالفعل في القائمة.")

    @commands.hybrid_command(name="remove_level_channel", description="إزالة روم من رومات احتساب الرسائل")
    async def remove_level_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not ctx.guild or not can_manage_level_settings(ctx):
            return await ctx.send("⛔ لا تملك صلاحية إعداد نظام اللفلات.")
        removed = await asyncio.to_thread(remove_level_channel, ctx.guild.id, channel.id)
        await ctx.send(f"✅ أُزيل {channel.mention} من رومات احتساب النقاط." if removed else f"ℹ️ {channel.mention} غير موجود في القائمة.")

    @commands.hybrid_command(name="list_level_channels", description="عرض الرومات المحددة لنظام اللفلات")
    async def list_level_channels(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم هذا الأمر داخل السيرفر.")
        channel_ids = await asyncio.to_thread(get_level_channels, ctx.guild.id)
        if not channel_ids:
            return await ctx.send("ℹ️ لم يتم تحديد رومات لاحتساب النقاط بعد.")
        await ctx.send("📚 رومات احتساب النقاط في هذا السيرفر:\n" + "\n".join(f"• <#{channel_id}>" for channel_id in channel_ids))

    @commands.hybrid_command(name="set_level_reward", description="تحديد مكافأة Cosmo لكل مستوى من مضاعفات 10")
    async def set_level_reward(self, ctx: commands.Context, amount: str):
        if not ctx.guild or not can_manage_level_settings(ctx):
            return await ctx.send("⛔ لا تملك صلاحية إعداد مكافأة اللفلات.")
        try:
            parsed = parse_amount(amount)
        except Exception:
            return await ctx.send("❌ أدخل مبلغاً صحيحاً.")
        if parsed < 0:
            return await ctx.send("❌ لا يمكن أن يكون المبلغ سالباً.")
        await asyncio.to_thread(set_milestone_reward, ctx.guild.id, parsed)
        await ctx.send(
            f"✅ تم ضبط مكافأة المستوى إلى `{parsed:,}` Cosmo لكل مستوى من مضاعفات 10. "
            "تُدفع من ميزانية هذا السيرفر عند بلوغ المستوى."
        )

    @commands.hybrid_command(name="set_level_announcement_channel", description="تحديد روم التوب 5 اليومي والرواتب")
    async def set_level_announcement_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not ctx.guild or not can_manage_level_settings(ctx):
            return await ctx.send("⛔ لا تملك صلاحية إعداد روم الإعلانات.")
        if channel.guild.id != ctx.guild.id:
            return await ctx.send("❌ يجب اختيار روم من هذا السيرفر.")
        await asyncio.to_thread(set_announcement_channel, ctx.guild.id, channel.id)
        await ctx.send(
            f"✅ سيتم إرسال التوب 5 اليومي والرواتب إلى {channel.mention} قبل منتصف الليل بتوقيت الرياض. "
            "كما أُعيدت التقارير المتعثرة إلى قائمة المحاولة."
        )

    @commands.hybrid_command(name="set_level_salary", description="تحديد مبلغ راتب شريحة رسائل إدارية")
    @discord.app_commands.choices(threshold=[
        discord.app_commands.Choice(name="500 رسالة", value=500),
        discord.app_commands.Choice(name="1000 رسالة", value=1000),
        discord.app_commands.Choice(name="2000 رسالة", value=2000),
        discord.app_commands.Choice(name="3000 رسالة", value=3000),
    ])
    async def set_level_salary(self, ctx: commands.Context, threshold: int, amount: str):
        if not ctx.guild or not can_manage_level_settings(ctx):
            return await ctx.send("⛔ لا تملك صلاحية ضبط الرواتب.")
        if threshold not in SALARY_THRESHOLDS:
            return await ctx.send("❌ اختر شريحة واحدة من: `500`, `1000`, `2000`, `3000` رسالة.")
        try:
            parsed = parse_amount(amount)
        except Exception:
            return await ctx.send("❌ أدخل مبلغاً صحيحاً.")
        if parsed < 0:
            return await ctx.send("❌ لا يمكن أن يكون الراتب سالباً؛ استخدم `0` لتعطيل الشريحة.")
        await asyncio.to_thread(set_salary_tier, ctx.guild.id, threshold, parsed)
        await ctx.send(
            f"✅ راتب الشريحة `{threshold:,}` رسالة أصبح `{parsed:,}` Cosmo. "
            "عند التصفير يأخذ الإداري مبلغ أعلى شريحة بلغها فقط، من ميزانية السيرفر."
        )

    @commands.hybrid_command(
        name="level_salary_history",
        aliases=["salary_history"],
        description="عرض سجل الرواتب اليومية السابقة للإداريين",
    )
    @discord.app_commands.describe(member="تصفية السجل على إداري واحد", limit="عدد السجلات، من 1 إلى 25")
    async def level_salary_history(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
        limit: int = 20,
    ):
        if not ctx.guild or not can_manage_level_settings(ctx):
            return await ctx.send("⛔ هذا السجل متاح لمالك البوت أو مالك السيرفر أو owner-role أو إدارة Discord فقط.")
        if not 1 <= limit <= 25:
            return await ctx.send("❌ اختر عدداً بين 1 و25.")

        history = await asyncio.to_thread(
            get_salary_history,
            ctx.guild.id,
            member.id if member else None,
            limit,
        )
        if not history:
            return await ctx.send("ℹ️ لا توجد سجلات رواتب يومية محفوظة لهذا السيرفر بعد.")

        statuses = {
            "paid": "مصروف",
            "insufficient": "تعذر الصرف: ميزانية غير كافية",
            "blocked_wal": "معلّق: إعداد قاعدة البيانات",
            "pending": "قيد المعالجة",
        }
        lines = []
        paid_total = 0
        for item in history:
            status = statuses.get(item["status"], item["status"])
            message_count = f"{item['message_count']:,}" if item["message_count"] is not None else "غير مسجل"
            threshold = f"{item['tier_threshold']:,}" if item["tier_threshold"] is not None else "غير مسجل"
            amount = f"+{item['amount']:,}" if item["status"] == "paid" else f"{item['amount']:,}"
            lines.append(
                f"• `{item['local_date']}` — <@{item['user_id']}> — {message_count} رسالة "
                f"(شريحة {threshold}) — `{amount}` Cosmo — **{status}**"
            )
            if item["status"] == "paid":
                paid_total += item["amount"]

        embed = discord.Embed(
            title=f"سجل الرواتب اليومية | {ctx.guild.name}",
            description="\n".join(lines),
            color=discord.Color.from_rgb(123, 103, 238),
        )
        embed.set_footer(text=f"عدد السجلات المعروضة: {len(history)} • مجموع المصروف بينها: {paid_total:,} Cosmo")
        send_kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
        if ctx.interaction is not None:
            send_kwargs["ephemeral"] = True
        await ctx.send(**send_kwargs)

    @commands.hybrid_command(name="level_points", aliases=["my_level"], description="عرض نقاطك ومستواك في هذا السيرفر على بطاقة")
    async def level_points(self, ctx: commands.Context, member: discord.Member | None = None):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم هذا الأمر داخل السيرفر.")
        target = member or ctx.author
        profile = await asyncio.to_thread(get_level_profile, ctx.guild.id, target.id)
        document = profile_card_html(
            ctx.guild.name,
            ctx.guild.icon.url if ctx.guild.icon else None,
            target.display_name,
            target.display_avatar.url,
            profile,
        )
        try:
            image_bytes = await asyncio.to_thread(_render_png, document)
            file = discord.File(io.BytesIO(image_bytes), filename="level-profile.png")
            embed = discord.Embed(
                title=f"مستوى {target.display_name} في {ctx.guild.name}",
                color=discord.Color.from_rgb(123, 103, 238),
            )
            embed.set_image(url="attachment://level-profile.png")
            await ctx.send(embed=embed, file=file)
        except Exception as exc:
            print(f"Level profile card failed: {exc}")
            await ctx.send(
                f"🌟 **{target.display_name}** في **{ctx.guild.name}**\n"
                f"• المستوى: `{profile['level']}`\n• النقاط: `{profile['points']:,}`\n"
                f"• المتبقي للمستوى التالي: `{profile['next_level_points']}`\n• الترتيب: `#{profile['rank']}`"
            )

    @commands.hybrid_command(name="level_top5", description="عرض أفضل خمسة أعضاء يومياً أو أسبوعياً أو شهرياً أو أبدياً")
    @discord.app_commands.choices(period=[
        discord.app_commands.Choice(name="يومي", value="daily"),
        discord.app_commands.Choice(name="أسبوعي", value="weekly"),
        discord.app_commands.Choice(name="شهري", value="monthly"),
        discord.app_commands.Choice(name="أبدي", value="all"),
    ])
    async def level_top5(self, ctx: commands.Context, period: str):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم هذا الأمر داخل السيرفر.")
        try:
            start_at, end_at = period_bounds(period)
        except ValueError as exc:
            return await ctx.send(f"❌ {exc}")
        entries = await asyncio.to_thread(get_level_leaderboard, ctx.guild.id, start_at, end_at, 5)
        card_entries = await self.prepare_card_entries(ctx.guild, entries)
        period_name = {
            "daily": "اليومي", "day": "اليومي", "يومي": "اليومي", "اليومي": "اليومي",
            "weekly": "الأسبوعي", "week": "الأسبوعي", "اسبوعي": "الأسبوعي", "أسبوعي": "الأسبوعي", "الأسبوعي": "الأسبوعي",
            "monthly": "الشهري", "month": "الشهري", "شهري": "الشهري", "الشهري": "الشهري",
            "all": "الأبدي", "alltime": "الأبدي", "all-time": "الأبدي", "ابدي": "الأبدي", "أبدي": "الأبدي", "الكل": "الأبدي",
        }[period.strip().lower()]
        document = leaderboard_card_html(
            ctx.guild.name,
            ctx.guild.icon.url if ctx.guild.icon else None,
            f"الترتيب {period_name}",
            card_entries,
        )
        try:
            image_bytes = await asyncio.to_thread(_render_png, document)
            file = discord.File(io.BytesIO(image_bytes), filename="level-top5.png")
            embed = discord.Embed(color=discord.Color.from_rgb(123, 103, 238))
            embed.set_image(url="attachment://level-top5.png")
            await ctx.send(embed=embed, file=file)
        except Exception as exc:
            print(f"Level leaderboard card failed: {exc}")
            if entries:
                lines = [
                    f"{index}. <@{entry['user_id']}> — `{entry['points']:,}` نقطة / `{entry['messages']:,}` رسالة"
                    for index, entry in enumerate(entries, 1)
                ]
                await ctx.send(f"🏆 أفضل خمسة ({period_name}) في **{ctx.guild.name}**:\n" + "\n".join(lines))
            else:
                await ctx.send(f"ℹ️ لا توجد نقاط في ترتيب {period_name} بعد.")


async def setup(bot):
    await bot.add_cog(Leveling(bot))
