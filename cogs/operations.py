from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import audit
import backup as backup_service
import database
from database import PRIMARY_OWNER_ID, check_is_owner, get_guild_db, get_payment_order, is_bot_admin, list_payment_orders
from leveling_store import (
    adjust_level_points, get_level_admin_config, get_level_profile, get_voice_member_status,
    reset_level_member,
)
from ltcpay import active_store_id, get_invoice

RIYADH = ZoneInfo("Asia/Riyadh")


def _fmt_ts(value):
    if not value:
        return "—"
    return f"<t:{int(value)}:f>"


def _has_guild_admin(ctx):
    if not ctx.guild or not ctx.author:
        return False
    perms = getattr(ctx.author, "guild_permissions", None)
    return ctx.author.id in (PRIMARY_OWNER_ID, ctx.guild.owner_id) or bool(perms and (perms.administrator or perms.manage_guild)) or is_bot_admin(ctx)


class Operations(commands.Cog):
    """Operational controls, audit access, reports and verified backups."""
    def __init__(self, bot):
        self.bot = bot
        self.periodic_report_worker.start()
        self.exchange_expiry_worker.start()

    def cog_unload(self):
        self.periodic_report_worker.cancel()
        self.exchange_expiry_worker.cancel()

    @commands.hybrid_command(name="audit_log", aliases=["سجل_التدقيق"], description="عرض سجل التدقيق الموحّد للعمليات")
    async def audit_log(self, ctx: commands.Context, limit: int = 20):
        if not _has_guild_admin(ctx):
            return await ctx.send("⛔ لا تملك صلاحية عرض سجل التدقيق.")
        rows = await asyncio.to_thread(audit.list_events, database.GLOBAL_DB_PATH, guild_id=ctx.guild.id, limit=limit)
        if not rows:
            return await ctx.send("ℹ️ لا توجد عمليات مسجلة بعد.")
        lines = []
        for item in rows[:25]:
            actor = item["actor_name"] or (f"<@{item['actor_id']}>" if item["actor_id"] else "النظام")
            target = f" → <@{item['target_user_id']}>" if item["target_user_id"] else ""
            amount = f" | `{item['amount']:,}`" if item["amount"] is not None else ""
            reason = f" | {item['reason']}" if item["reason"] else ""
            lines.append(f"• {_fmt_ts(item['created_at'])} — **{item['event_type']}** — {actor}{target}{amount}{reason}")
        embed = discord.Embed(title=f"سجل التدقيق | {ctx.guild.name}", description="\n".join(lines), color=discord.Color.blurple())
        embed.set_footer(text=f"المعروض: {len(rows[:25])} • المصدر: {database.GLOBAL_DB_PATH}")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="payment_status", aliases=["حالة_الدفع"], description="عرض حالة طلب دفع تملكه")
    async def payment_status(self, ctx: commands.Context, order_id: int):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم الأمر داخل السيرفر الذي أنشأ الطلب.")
        owner = await check_is_owner(self.bot, ctx)
        order = await asyncio.to_thread(get_payment_order, ctx.guild.id, order_id, None if owner else ctx.author.id)
        if not order:
            return await ctx.send("❌ لم أعثر على طلب بهذا الرقم ضمن الطلبات المسموح لك بعرضها.")
        embed = discord.Embed(title=f"حالة طلب الدفع #{order['id']}", color=discord.Color.green() if order['status'] == 'paid' else discord.Color.orange())
        embed.add_field(name="الحالة", value=f"`{order['status']}`", inline=True)
        embed.add_field(name="الطريقة", value=f"`{order['method']}`", inline=True)
        embed.add_field(name="ميزانية السيرفر", value=f"`{order['budget_amount']:,}` Cosmo", inline=True)
        embed.add_field(name="الإنشاء", value=_fmt_ts(order['created_at']), inline=True)
        embed.add_field(name="الانتهاء", value=_fmt_ts(order['expires_at']), inline=True)
        embed.add_field(name="الفاتورة", value=f"`{order['provider_payment_id'] or '—'}`", inline=True)
        if order["pay_address"]:
            embed.add_field(name="رابط الدفع", value=order["pay_address"], inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="pending_payments", aliases=["المدفوعات_المعلقة"], description="عرض طلبات الدفع المعلقة والمتأخرة لمالك البوت")
    async def pending_payments(self, ctx: commands.Context, overdue: bool = False, limit: int = 20):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك البوت فقط.")
        rows = await asyncio.to_thread(list_payment_orders, None, pending_only=True, overdue_only=overdue, limit=limit)
        if not rows:
            return await ctx.send("✅ لا توجد طلبات مطابقة.")
        lines = [f"• `#{r['id']}` guild `{r['guild_id']}` user <@{r['user_id']}> — `{r['method']}` — `{r['status']}` — `{r['budget_amount']:,}` — ينتهي {_fmt_ts(r['expires_at'])}" for r in rows]
        await ctx.send(embed=discord.Embed(title="طلبات الدفع المعلقة", description="\n".join(lines), color=discord.Color.orange()), allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="payment_reconcile", aliases=["مطابقة_الدفع"], description="إعادة مطابقة فاتورة LTC بأمان وبشكل idempotent")
    async def payment_reconcile(self, ctx: commands.Context, order_id: int):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك البوت فقط.")
        if not ctx.guild:
            return await ctx.send("⛔ نفذ الأمر داخل السيرفر صاحب الطلب.")
        order = await asyncio.to_thread(get_payment_order, ctx.guild.id, order_id)
        if not order or order["method"] != "crypto" or not order["provider_payment_id"]:
            return await ctx.send("❌ الطلب غير موجود أو ليس طلب LTC مكتمل الربط.")
        try:
            invoice = await asyncio.to_thread(get_invoice, order["provider_payment_id"])
            status = str(invoice.get("status", "")).lower()
            event_type = "InvoiceSettled" if status == "settled" else "InvoiceExpired" if status == "expired" else "InvoiceProcessing"
            event = {"type": event_type, "invoiceId": order["provider_payment_id"], "storeId": active_store_id()}
            result = await asyncio.to_thread(database.process_ltcpay_webhook, event, invoice)
            await asyncio.to_thread(database.record_audit, "payment_reconcile", guild_id=ctx.guild.id, actor_id=ctx.author.id, actor_name=ctx.author.display_name, target_user_id=order["user_id"], amount=order["budget_amount"], reason=f"manual reconcile order #{order_id}", before={"status": order["status"]}, after=result, source="discord", correlation_id=f"payment:{order_id}")
            await ctx.send(f"✅ تمت المطابقة الآمنة للطلب `#{order_id}`. النتيجة: `{(result or {}).get('status', 'غير مطابق')}`.")
        except Exception as exc:
            await ctx.send(f"⚠️ تعذر جلب أو مطابقة الفاتورة: `{str(exc)[:300]}`")

    @commands.hybrid_command(name="level_config", aliases=["إعدادات_اللفلات"], description="عرض إعدادات اللفلات والفويس والرواتب")
    async def level_config(self, ctx: commands.Context):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم الأمر داخل السيرفر.")
        config = await asyncio.to_thread(get_level_admin_config, ctx.guild.id)
        await ctx.send(embed=discord.Embed(title=f"إعدادات اللفلات والفويس | {ctx.guild.name}", description=f"اللفلات: **{'مفعلة' if config['enabled'] else 'مغلقة'}**\nالفويس: **{'مفعل' if config['voice_enabled'] else 'مغلق'}**\nالرومات: {', '.join(f'<#{x}>' for x in config['channels']) or 'لا يوجد'}\nمكافأة المستوى: `{config['milestone_reward']:,}`\nشرائح الرواتب: `{config['salary_tiers']}`", color=discord.Color.blurple()))

    @commands.hybrid_command(name="level_adjust", aliases=["تصحيح_نقاط"], description="تصحيح نقاط عضو مع سبب إلزامي")
    async def level_adjust(self, ctx: commands.Context, member: discord.Member, points: int, *, reason: str):
        if not _has_guild_admin(ctx):
            return await ctx.send("⛔ لا تملك صلاحية تصحيح النقاط.")
        if not reason.strip() or points < 0:
            return await ctx.send("❌ أدخل نقاطًا غير سالبة وسببًا واضحًا للتعديل.")
        before = await asyncio.to_thread(get_level_profile, ctx.guild.id, member.id)
        result = await asyncio.to_thread(adjust_level_points, ctx.guild.id, member.id, points)
        await asyncio.to_thread(audit.record_ctx, database.GLOBAL_DB_PATH, "level_points_adjusted", ctx, target_user_id=member.id, reason=reason, before=before, after=result)
        await ctx.send(f"✅ تم تصحيح نقاط {member.mention} من `{result['before']}` إلى `{result['after']}`. السبب: {reason}")

    @commands.hybrid_command(name="level_reset", aliases=["مسح_نقاط"], description="مسح نقاط ووقت فويس عضو مع الاحتفاظ بسجل التدقيق")
    async def level_reset(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        if not _has_guild_admin(ctx):
            return await ctx.send("⛔ لا تملك صلاحية مسح النقاط.")
        if not reason.strip():
            return await ctx.send("❌ السبب إلزامي.")
        result = await asyncio.to_thread(reset_level_member, ctx.guild.id, member.id)
        await asyncio.to_thread(audit.record_ctx, database.GLOBAL_DB_PATH, "level_member_reset", ctx, target_user_id=member.id, reason=reason, after=result)
        await ctx.send(f"✅ تم تصفير نقاط ووقت فويس {member.mention}. السبب: {reason}")

    @commands.hybrid_command(name="voice_status", aliases=["حالة_الفويس"], description="عرض وقت فويس عضو")
    async def voice_status(self, ctx: commands.Context, member: discord.Member | None = None):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم الأمر داخل السيرفر.")
        member = member or ctx.author
        result = await asyncio.to_thread(get_voice_member_status, ctx.guild.id, member.id)
        await ctx.send(f"🎙️ {member.mention}: إجمالي الفويس المؤهل `{result['seconds']:,}` ثانية، واليوم `{result['today_seconds']:,}` ثانية.")

    @commands.hybrid_command(name="backup_create", aliases=["إنشاء_نسخة"], description="إنشاء نسخة SQLite وفحص سلامتها")
    async def backup_create(self, ctx: commands.Context):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك البوت فقط.")
        result = await asyncio.to_thread(backup_service.create_backup, os.getcwd())
        await asyncio.to_thread(database.record_audit, "backup_created", actor_id=ctx.author.id, actor_name=ctx.author.display_name, reason="manual backup", after=result, source="discord")
        await ctx.send(f"✅ أُنشئت النسخة `{result['id']}` بعد فحص SQLite. الملفات: `{len(result['files'])}`، النتيجة: `{result['ok']}`. المسار محفوظ على الخادم.")

    @commands.hybrid_command(name="backup_list", aliases=["قائمة_النسخ"], description="عرض النسخ الاحتياطية المتاحة")
    async def backup_list(self, ctx: commands.Context):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك البوت فقط.")
        rows = await asyncio.to_thread(backup_service.list_backups, os.getcwd())
        await ctx.send("\n".join(f"• `{r['id']}` — ملفات `{r['files']}` — صالح: `{r['ok']}`" for r in rows[:20]) or "ℹ️ لا توجد نسخ.")

    @commands.hybrid_command(name="backup_verify", aliases=["فحص_نسخة"], description="فحص نسخة واختبار استعادتها في مجلد مؤقت")
    async def backup_verify(self, ctx: commands.Context, backup_id: str):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك البوت فقط.")
        if "/" in backup_id or "\\" in backup_id or not backup_id.startswith("backup_"):
            return await ctx.send("❌ معرّف النسخة غير صالح.")
        result = await asyncio.to_thread(backup_service.verify_backup, os.path.join(os.getcwd(), "backups", backup_id))
        await asyncio.to_thread(database.record_audit, "backup_verified", actor_id=ctx.author.id, actor_name=ctx.author.display_name, reason=f"verify {backup_id}", after=result, source="discord")
        await ctx.send(f"فحص `{backup_id}`: **{'ناجح' if result.get('ok') else 'فاشل'}**. تم اختبار الاستعادة في مساحة مؤقتة.")

    async def _summary_embed(self, guild: discord.Guild, period_seconds: int = 86400):
        now = int(time.time()); start = now - period_seconds
        events = await asyncio.to_thread(audit.list_events, database.GLOBAL_DB_PATH, guild_id=guild.id, limit=200)
        events = [e for e in events if int(e.get("created_at") or 0) >= start]
        conn, _ = await asyncio.to_thread(get_guild_db, guild.id)
        try:
            budget = conn.execute("SELECT COALESCE(spending_balance,0) FROM settings WHERE guild_id=?", (guild.id,)).fetchone()
            payments = conn.execute("SELECT status, COUNT(*) FROM budget_payment_orders WHERE created_at >= ? GROUP BY status", (start,)).fetchall()
            tickets = conn.execute("SELECT is_closed, COUNT(*) FROM tickets GROUP BY is_closed").fetchall()
            levels = conn.execute("SELECT COUNT(*) FROM leveling_message_events WHERE created_at >= ?", (start,)).fetchone()[0]
        finally:
            conn.close()
        payment_text = ", ".join(f"{status}: {count}" for status, count in payments) or "لا يوجد"
        ticket_text = ", ".join(("مغلقة" if closed else "مفتوحة") + f": {count}" for closed, count in tickets) or "لا يوجد"
        return discord.Embed(title=f"ملخص الإدارة | {guild.name}", description=f"آخر {period_seconds // 3600} ساعة", color=discord.Color.teal()).add_field(name="الميزانية الحالية", value=f"`{int(budget[0] if budget else 0):,}` Cosmo").add_field(name="المدفوعات", value=payment_text, inline=False).add_field(name="التذاكر", value=ticket_text).add_field(name="رسائل اللفلات", value=f"`{levels:,}`").add_field(name="عمليات التدقيق", value=f"`{len(events)}`")

    @commands.hybrid_command(name="admin_summary", aliases=["ملخص_الإدارة"], description="إرسال ملخص إداري يومي أو أسبوعي الآن")
    async def admin_summary(self, ctx: commands.Context, period: str = "daily"):
        if not _has_guild_admin(ctx):
            return await ctx.send("⛔ لا تملك صلاحية عرض الملخص.")
        seconds = 7 * 86400 if period.lower() in ("weekly", "week", "أسبوعي") else 86400
        await ctx.send(embed=await self._summary_embed(ctx.guild, seconds))

    @tasks.loop(minutes=10)
    async def periodic_report_worker(self):
        now = datetime.now(RIYADH)
        if now.hour != 23 or now.minute < 45:
            return
        for guild in list(self.bot.guilds):
            config = await asyncio.to_thread(get_level_admin_config, guild.id)
            channel_id = config.get("announcement_channel_id")
            channel = guild.get_channel(channel_id) if channel_id else None
            if not channel:
                continue
            daily_key = f"daily:{guild.id}:{now.date().isoformat()}"
            if await asyncio.to_thread(audit.claim_report, database.GLOBAL_DB_PATH, daily_key):
                await channel.send(embed=await self._summary_embed(guild, 86400), allowed_mentions=discord.AllowedMentions.none())
            if now.weekday() == 6:
                weekly_key = f"weekly:{guild.id}:{now.date().isoformat()}"
                if await asyncio.to_thread(audit.claim_report, database.GLOBAL_DB_PATH, weekly_key):
                    await channel.send(embed=await self._summary_embed(guild, 7 * 86400), allowed_mentions=discord.AllowedMentions.none())

    @periodic_report_worker.before_loop
    async def before_periodic_report_worker(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=10)
    async def exchange_expiry_worker(self):
        expired = await asyncio.to_thread(database.expire_old_exchange_requests, 24 * 60 * 60)
        for item in expired:
            try:
                user = self.bot.get_user(item["user_id"]) or await self.bot.fetch_user(item["user_id"])
                if user:
                    await user.send(
                        f"❌ تم رفض طلب التبديل رقم `#{item['request_id']}` تلقائيًا لأنه بقي قيد الانتظار أكثر من 24 ساعة. "
                        f"تمت إعادة `{item['amount']:,}` Cosmo إلى رصيدك."
                    )
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                # The refund and status update already succeeded; a closed DM must
                # not make the worker retry or reverse the transaction.
                pass

    @exchange_expiry_worker.before_loop
    async def before_exchange_expiry_worker(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Operations(bot))
