import asyncio
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


def period_bounds(period: str | None, now: datetime | None = None):
    """Return UTC epoch bounds for a Riyadh-local calendar period."""
    if period is None or not period.strip():
        return None, None
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


def _set_guild_embed_author(embed: discord.Embed, server_name: str, server_icon: str | None) -> None:
    if server_icon:
        embed.set_author(name=server_name, icon_url=server_icon)
    else:
        embed.set_author(name=server_name)


def profile_embed(
    server_name: str,
    server_icon: str | None,
    display_name: str,
    avatar: str | None,
    profile: dict,
) -> discord.Embed:
    progress = max(0, min(XP_PER_LEVEL, int(profile["progress"])))
    filled = round(progress / XP_PER_LEVEL * 10)
    bar = "▰" * filled + "▱" * (10 - filled)
    progress_percent = round(progress / XP_PER_LEVEL * 100)
    safe_name = discord.utils.escape_markdown(display_name)[:200]
    embed = discord.Embed(
        title=f"🌟 بطاقة المستوى — {safe_name}",
        description="إحصاءات نشاطك في هذا السيرفر",
        color=discord.Color.from_rgb(123, 103, 238),
    )
    _set_guild_embed_author(embed, server_name, server_icon)
    if avatar:
        embed.set_thumbnail(url=avatar)
    embed.add_field(name="المستوى", value=f"**{profile['level']:,}**", inline=True)
    embed.add_field(name="النقاط", value=f"**{profile['points']:,}**", inline=True)
    embed.add_field(name="الترتيب", value=f"**#{profile['rank']:,}**", inline=True)
    embed.add_field(name="الرسائل المحتسبة", value=f"**{profile['messages']:,}**", inline=True)
    embed.add_field(
        name=f"التقدم إلى المستوى {profile['level'] + 1:,}",
        value=f"`{bar}` **{progress_percent}%**\nالمتبقي: **{profile['next_level_points']:,}** نقطة",
        inline=False,
    )
    embed.set_footer(text="بيانات هذا السيرفر فقط • توقيت الرياض")
    return embed


def leaderboard_embed(
    server_name: str,
    server_icon: str | None,
    period_title: str | None,
    entries: list[dict],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏆 المتصدرون — {period_title}" if period_title else "🏆 المتصدرون",
        description="أفضل خمسة أعضاء حسب النقاط المكتسبة",
        color=discord.Color.from_rgb(123, 103, 238),
    )
    _set_guild_embed_author(embed, server_name, server_icon)
    if server_icon:
        embed.set_thumbnail(url=server_icon)
    if entries:
        medals = ("🥇", "🥈", "🥉", "🏅", "🏅")
        lines = [
            f"{medals[index]} **المركز {index + 1} —** <@{int(entry['user_id'])}>"
            f"   •   ⭐ النقاط: **{entry['points']:,}**"
            f"   •   💬 الرسائل المحتسبة: **{entry['messages']:,}**"
            for index, entry in enumerate(entries[:5])
        ]
        embed.description = "\n".join(lines)
    else:
        embed.description = "لا توجد نقاط أو رسائل محتسبة في هذه الفترة بعد."
    embed.set_footer(text="الترتيب خاص بهذا السيرفر • توقيت الرياض")
    return embed


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
            description="ترتيب اليوم حسب النقاط المكتسبة. الرواتب تحتسب الرسائل المقبولة غير المزعجة.",
            color=discord.Color.from_rgb(123, 103, 238),
            timestamp=datetime.now(RIYADH),
        )
        if guild.icon:
            embed.set_author(name=guild.name, icon_url=guild.icon.url)
            embed.set_thumbnail(url=guild.icon.url)
        else:
            embed.set_author(name=guild.name)
        embed.add_field(name="🏆 التوب 5", value=ranking_text, inline=False)
        embed.add_field(name="💰 الرواتب اليومية", value=salary_text, inline=False)
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

    @commands.hybrid_command(name="level_points", aliases=["my_level"], description="عرض نقاطك ومستواك في هذا السيرفر")
    async def level_points(self, ctx: commands.Context, member: discord.Member | None = None):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم هذا الأمر داخل السيرفر.")
        target = member or ctx.author
        profile = await asyncio.to_thread(get_level_profile, ctx.guild.id, target.id)
        embed = profile_embed(
            ctx.guild.name,
            ctx.guild.icon.url if ctx.guild.icon else None,
            target.display_name,
            target.display_avatar.url,
            profile,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="top", description="عرض أفضل خمسة أعضاء يومياً أو أسبوعياً أو شهرياً أو أبدياً")
    @discord.app_commands.choices(period=[
        discord.app_commands.Choice(name="يومي", value="daily"),
        discord.app_commands.Choice(name="أسبوعي", value="weekly"),
        discord.app_commands.Choice(name="شهري", value="monthly"),
    ])
    async def top(self, ctx: commands.Context, period: str | None = None):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم هذا الأمر داخل السيرفر.")
        try:
            start_at, end_at = period_bounds(period)
        except ValueError as exc:
            return await ctx.send(f"❌ {exc}")
        entries = await asyncio.to_thread(get_level_leaderboard, ctx.guild.id, start_at, end_at, 5)
        period_name = None if not period or not period.strip() else {
            "daily": "اليومي", "day": "اليومي", "يومي": "اليومي", "اليومي": "اليومي",
            "weekly": "الأسبوعي", "week": "الأسبوعي", "اسبوعي": "الأسبوعي", "أسبوعي": "الأسبوعي", "الأسبوعي": "الأسبوعي",
            "monthly": "الشهري", "month": "الشهري", "شهري": "الشهري", "الشهري": "الشهري",
        }[period.strip().lower()]
        display_entries = []
        for entry in entries:
            member = ctx.guild.get_member(int(entry["user_id"]))
            display_entries.append({
                **entry,
                "display_name": member.display_name if member else f"عضو {entry['user_id']}",
            })
        embed = leaderboard_embed(
            ctx.guild.name,
            ctx.guild.icon.url if ctx.guild.icon else None,
            f"الترتيب {period_name}" if period_name else None,
            display_entries,
        )
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Leveling(bot))
