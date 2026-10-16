import discord
from discord.ext import commands, tasks
import shutil
import time
import string
import random
import asyncio
import base64
from datetime import datetime
from database import (
    check_is_owner, is_bot_admin, is_guild_premium, parse_duration,
    parse_amount, get_guild_db, global_cursor, global_conn, PRIMARY_OWNER_ID,
    deduct_server_spending, update_balance, is_blacklisted, get_server_spending_balance,
    send_global_action_log
)

def can_manage_bot(ctx: commands.Context) -> bool:
    """
    التحقق من صلاحية إدارة البوت:
    - متاح لمالك البوت الأساسي
    - متاح لمالك السيرفر (Owner) حتى بدون بريميوم
    - متاح لمن لديه رتبة مسؤول البوت المسجلة أو صلاحية إدارة السيرفر/الأدمن
    """
    if not ctx.guild:
        return False
    if ctx.author.id == PRIMARY_OWNER_ID or ctx.author.id == ctx.guild.owner_id:
        return True
    if ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_guild:
        return True
    return is_bot_admin(ctx)

async def schedule_double_announcement(channel: discord.TextChannel, guild_id: int, duration_sec: int):
    await asyncio.sleep(duration_sec)
    g_conn, g_cur = get_guild_db(guild_id)
    g_cur.execute("UPDATE settings SET double_event_end = 0 WHERE guild_id = ?", (guild_id,))
    g_conn.commit()
    g_conn.close()
    try:
        await channel.send("📢 **انتهى حدث الدبل (المضاعفة ×2) لعجلة الحظ الآن!**\nعادت جوائز العجلة إلى قيمتها الأصلية.")
    except:
        pass

class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if not self.auto_backup_task.is_running():
            self.auto_backup_task.start()

    def cog_unload(self):
        self.auto_backup_task.cancel()

    @tasks.loop(hours=24)
    async def auto_backup_task(self):
        try:
            shutil.copyfile("global_cosmo.db", "global_cosmo_backup.db")
            owner = self.bot.get_user(PRIMARY_OWNER_ID) or await self.bot.fetch_user(PRIMARY_OWNER_ID)
            if owner:
                file = discord.File("global_cosmo_backup.db", filename=f"global_cosmo_backup_{int(time.time())}.db")
                await owner.send("📦 **النسخة الاحتياطية اليومية التلقائية لقاعدة البيانات العامة:**", file=file)
        except Exception as e:
            print(f"Auto backup error: {e}")

    @commands.hybrid_command(
        name="set_global_log",
        aliases=["لوج_عام", "تحديد_اللوج_العام"],
        description="تحديد روم عام لإرسال لوجات جميع عمليات الشحن والتحويل في كل السيرفرات (مالك البوت فقط)"
    )
    async def set_global_log(self, ctx: commands.Context, channel: discord.TextChannel):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك البوت الأساسي.")

        global_cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES ('global_action_log', ?)", (channel.id,))
        global_conn.commit()
        await ctx.send(f"🌐 تم تعيين روم اللوج العام الموحد لعمليات الشحن والتحويل على: {channel.mention} (`ID: {channel.id}`).")

    @commands.hybrid_command(
        name="disable_global_log",
        aliases=["تعطيل_اللوج_العام"],
        description="تعطيل روم اللوج العام الموحد (مالك البوت فقط)"
    )
    async def disable_global_log(self, ctx: commands.Context):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك البوت الأساسي.")

        global_cursor.execute("DELETE FROM global_settings WHERE key = 'global_action_log'")
        global_conn.commit()
        await ctx.send("🛑 تم تعطيل روم اللوج العام بنجاح.")

    @commands.hybrid_command(
        name="add_reply",
        aliases=["اضافة_رد", "رد_تلقائي"],
        description="إضافة رد تلقائي (يمسح رسالة العضو ويرسل الرد بدلاً منها)"
    )
    async def add_reply(self, ctx: commands.Context, trigger: str, *, reply: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لإضافة ردود تلقائية.")

        clean_trigger = trigger.strip().lower()
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT OR REPLACE INTO auto_replies (trigger_word, reply_content) VALUES (?, ?)",
            (clean_trigger, reply)
        )
        g_conn.commit()
        g_conn.close()

        await ctx.send(f"✅ تم حفظ الرد التلقائي بنجاح!\n• **الكلمة المستهدفة:** `{clean_trigger}`\n• **الرد:** {reply}")

    @commands.hybrid_command(
        name="delete_reply",
        aliases=["حذف_رد"],
        description="حذف رد تلقائي مسجل بالسيرفر"
    )
    async def delete_reply(self, ctx: commands.Context, trigger: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لحذف الردود التلقائية.")

        clean_trigger = trigger.strip().lower()
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("DELETE FROM auto_replies WHERE trigger_word = ?", (clean_trigger,))
        changes = g_conn.total_changes
        g_conn.commit()
        g_conn.close()

        if changes > 0:
            await ctx.send(f"🗑️ تم حذف الرد التلقائي المرتبط بكلمة: `{clean_trigger}` بنجاح.")
        else:
            await ctx.send(f"❌ لم يتم العثور على رد تلقائي مسجل بالكلمة: `{clean_trigger}`.")

    @commands.hybrid_command(
        name="list_replies",
        aliases=["قائمة_الردود", "الردود_التلقائية"],
        description="عرض جميع الردود التلقائية المفعلة في هذا السيرفر"
    )
    async def list_replies(self, ctx: commands.Context):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لعرض قائمة الردود التلقائية.")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT trigger_word, reply_content FROM auto_replies")
        rows = g_cur.fetchall()
        g_conn.close()

        if not rows:
            return await ctx.send("ℹ️ لا توجد ردود تلقائية مسجلة في هذا السيرفر حتى الآن.")

        embed = discord.Embed(
            title=f"📋 قائمة الردود التلقائية | {ctx.guild.name}",
            description="الكلمات المراقبة التي يتم مسحها وإرسال الرد بدلاً منها:",
            color=discord.Color.blue()
        )

        for trigger_word, reply_content in rows:
            preview_reply = reply_content if len(reply_content) <= 50 else f"{reply_content[:47]}..."
            embed.add_field(
                name=f"الكلمة: `{trigger_word}`",
                value=f"➔ **الرد:** {preview_reply}",
                inline=False
            )

        embed.set_footer(text=f"إجمالي الردود: {len(rows)}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="list_aliases", aliases=["الاختصارات", "قائمة_الاختصارات"], description="عرض جميع الأوامر المختصرة المفعلة في هذا السيرفر")
    async def list_aliases(self, ctx: commands.Context):
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT original_command, alias FROM command_aliases")
        rows = g_cur.fetchall()
        g_conn.close()

        if not rows:
            return await ctx.send("ℹ️ لا توجد اختصارات مخصصة مسجلة في هذا السيرفر حتى الآن.")

        embed = discord.Embed(
            title=f"📑 قائمة الاختصارات المخصصة | {ctx.guild.name}",
            description="جميع الاختصارات المعينة والأوامر الأصلية التي تنفذها:",
            color=discord.Color.teal()
        )
        for orig, alias in rows:
            embed.add_field(name=f"اختصار: `!{alias}`", value=f"➔ الأمر الأصلي: `{orig}`", inline=False)

        embed.set_footer(text=f"إجمالي الاختصارات: {len(rows)}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="add_alias", aliases=["اضافة_اختصار"], description="إضافة اختصار جديد لأي أمر خاص بهذا السيرفر")
    async def add_alias(self, ctx: commands.Context, original_command: str, new_alias: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لإضافة اختصارات الأوامر.")
        cmd = self.bot.get_command(original_command)
        if not cmd:
            return await ctx.send("❌ الأمر الأساسي غير موجود.")
        
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("INSERT OR REPLACE INTO command_aliases (original_command, alias) VALUES (?, ?)", (original_command, new_alias.strip()))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم تعيين الاختصار `{new_alias}` للأمر `{original_command}` في هذا السيرفر بنجاح.")

    @commands.hybrid_command(name="add_balance", aliases=["اضافة"], description="إضافة رصيد لعضو (يخصم من ميزانية السيرفر)")
    async def add_balance(self, ctx: commands.Context, member: discord.Member, amount: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لإضافة الرصيد.")
        if is_blacklisted(member):
            return await ctx.send("⛔ العضو مدرج في البلاك ليست.")

        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed_amount <= 0:
            return await ctx.send("❌ المبلغ يجب أن يكون رقماً موجباً.")

        allowed, pool_msg = deduct_server_spending(ctx.guild.id, parsed_amount)
        if not allowed:
            return await ctx.send(pool_msg)
        
        update_balance(member.id, parsed_amount)
        await ctx.send(f"✅ تمت إضافة `{parsed_amount:,}` Cosmo إلى رصيد {member.mention}.\n{pool_msg}")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT balance_log FROM settings WHERE guild_id = ?", (ctx.guild.id,))
        row = g_cur.fetchone()
        g_conn.close()
        if row and row[0]:
            chan = ctx.guild.get_channel(row[0])
            if chan:
                await chan.send(f"📥 **شحن رصيد:** قام {ctx.author.mention} بإضافة `{parsed_amount:,}` Cosmo إلى {member.mention}.")

        global_embed = discord.Embed(
            title="🌐 عملية شحن رصيد (Global Add Balance)",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        global_embed.add_field(name="🏛️ السيرفر", value=f"{ctx.guild.name} (`{ctx.guild.id}`)", inline=False)
        global_embed.add_field(name="🛡️ المسؤول", value=f"{ctx.author.mention} (`{ctx.author.id}`)", inline=True)
        global_embed.add_field(name="👤 العضو المشحون له", value=f"{member.mention} (`{member.id}`)", inline=True)
        global_embed.add_field(name="🪙 المبلغ المضاف", value=f"`{parsed_amount:,}` Cosmo", inline=False)
        global_embed.set_footer(text="Cosmo Global Admin Log")
        await send_global_action_log(self.bot, global_embed)

    @commands.hybrid_command(name="remove_balance", aliases=["خصم"], description="خصم رصيد من عضو")
    async def remove_balance(self, ctx: commands.Context, member: discord.Member, amount: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لخصم الرصيد.")
        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed_amount <= 0:
            return await ctx.send("❌ المبلغ يجب أن يكون رقماً موجباً.")
        
        update_balance(member.id, -parsed_amount)
        await ctx.send(f"✅ تم خصم `{parsed_amount:,}` Cosmo من رصيد {member.mention}.")

    @commands.hybrid_command(name="server_budget", aliases=["ميزانية_السيرفر"], description="عرض ميزانية الصرف المتبقية للسيرفر")
    async def server_budget(self, ctx: commands.Context):
        budget = get_server_spending_balance(ctx.guild.id)
        await ctx.send(f"🪙 **رصيد ميزانية السيرفر المتاحة للصرف:** `{budget:,}` Cosmo.")

    @commands.hybrid_command(
        name="set_server_budget",
        aliases=["تحديد_ميزانية_السيرفر", "ضبط_ميزانية_السيرفر"],
        description="تحديد وضبط ميزانية الصرف الإجمالية لسيرفر معين مباشرة (مالك البوت حصرياً)"
    )
    async def set_server_budget(self, ctx: commands.Context, guild_id: str, amount: str):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر مخصص حصرياً لمالك ومطور البوت الأساسي فقط.")

        try:
            target_guild_id = int(guild_id)
        except ValueError:
            return await ctx.send("❌ يرجى إدخال ID سيرفر صالح (أرقام فقط).")

        try:
            parsed = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed < 0:
            return await ctx.send("❌ لا يمكن تعيين الميزانية برقم سالب.")

        current_budget = get_server_spending_balance(target_guild_id)
        g_conn, g_cur = get_guild_db(target_guild_id)
        g_cur.execute(
            "INSERT INTO settings (guild_id, spending_balance) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET spending_balance = excluded.spending_balance",
            (target_guild_id, parsed)
        )
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        g_cur.execute(
            "INSERT INTO budget_logs (action, amount, old_balance, new_balance, admin_id, timestamp) VALUES ('تعيين مباشر', ?, ?, ?, ?, ?)",
            (parsed, current_budget, parsed, ctx.author.id, now_str)
        )
        g_conn.commit()
        g_conn.close()

        target_guild = self.bot.get_guild(target_guild_id)
        g_name = target_guild.name if target_guild else f"السيرفر (`{target_guild_id}`)"

        await ctx.send(
            f"🎯 **تم تعيين ميزانية السيرفر بنجاح!**\n"
            f"• **السيرفر المستهدف:** {g_name}\n"
            f"• **الميزانية السابقة:** `{current_budget:,}` Cosmo\n"
            f"• **الميزانية المحددة الجديدة:** `{parsed:,}` Cosmo"
        )

    @commands.hybrid_command(
        name="add_server_budget", 
        aliases=["اضافة_ميزانية_السيرفر", "شحن_ميزانية_السيرفر", "add_budget_server"], 
        description="إضافة وشحن مبلغ إلى ميزانية الصرف الحالية للسيرفر (مالك البوت فقط)"
    )
    async def add_server_budget(self, ctx: commands.Context, amount: str):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك ومطور البوت الأساسي.")

        try:
            parsed = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed <= 0:
            return await ctx.send("❌ المبلغ المضاف يجب أن يكون أكبر من 0.")

        current_budget = get_server_spending_balance(ctx.guild.id)
        new_total_budget = current_budget + parsed

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO settings (guild_id, spending_balance) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET spending_balance = spending_balance + excluded.spending_balance",
            (ctx.guild.id, parsed)
        )
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        g_cur.execute(
            "INSERT INTO budget_logs (action, amount, old_balance, new_balance, admin_id, timestamp) VALUES ('إضافة', ?, ?, ?, ?, ?)",
            (parsed, current_budget, new_total_budget, ctx.author.id, now_str)
        )
        g_conn.commit()
        g_conn.close()

        await ctx.send(
            f"🏦 **تمت إضافة ميزانية جديدة للسيرفر بنجاح!**\n"
            f"• **المبلغ المضاف:** `+{parsed:,}` Cosmo\n"
            f"• **الميزانية السابقة:** `{current_budget:,}` Cosmo\n"
            f"• **إجمالي الميزانية المتاحة الآن:** `{new_total_budget:,}` Cosmo"
        )

    @commands.hybrid_command(
        name="remove_server_budget", 
        aliases=["خصم_ميزانية_السيرفر", "remove_budget_server"], 
        description="خصم مبلغ من ميزانية الصرف المتاحة للسيرفر (مالك البوت فقط)"
    )
    async def remove_server_budget(self, ctx: commands.Context, amount: str):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك ومطور البوت الأساسي.")

        try:
            parsed = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed <= 0:
            return await ctx.send("❌ المبلغ المخصوم يجب أن يكون أكبر من 0.")

        current_budget = get_server_spending_balance(ctx.guild.id)
        if current_budget < parsed:
            return await ctx.send(
                f"❌ لا يمكن الخصم: المبلغ المطلوب خصمه (`{parsed:,}`) أكبر من ميزانية السيرفر الحالية (`{current_budget:,}` Cosmo)."
            )

        new_total_budget = current_budget - parsed

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("UPDATE settings SET spending_balance = ? WHERE guild_id = ?", (new_total_budget, ctx.guild.id))
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        g_cur.execute(
            "INSERT INTO budget_logs (action, amount, old_balance, new_balance, admin_id, timestamp) VALUES ('خصم', ?, ?, ?, ?, ?)",
            (parsed, current_budget, new_total_budget, ctx.author.id, now_str)
        )
        g_conn.commit()
        g_conn.close()

        await ctx.send(
            f"📉 **تم خصم مبلغ من ميزانية السيرفر بنجاح!**\n"
            f"• **المبلغ المخصوم:** `-{parsed:,}` Cosmo\n"
            f"• **الميزانية السابقة:** `{current_budget:,}` Cosmo\n"
            f"• **الميزانية المتبقية الآن:** `{new_total_budget:,}` Cosmo"
        )

    @commands.hybrid_command(
        name="server_budget_history", 
        aliases=["سجل_ميزانية_السيرفر", "budget_history"], 
        description="عرض آخر عمليات الإضافة والخصم على ميزانية السيرفر"
    )
    async def server_budget_history(self, ctx: commands.Context):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لعرض سجل ميزانية السيرفر.")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "SELECT id, action, amount, old_balance, new_balance, admin_id, timestamp "
            "FROM budget_logs ORDER BY id DESC LIMIT 5"
        )
        rows = g_cur.fetchall()
        current_pool = get_server_spending_balance(ctx.guild.id)
        g_conn.close()

        if not rows:
            return await ctx.send(f"ℹ️ لا توجد أي عمليات مسجلة في ميزانية السيرفر حتى الآن.\n🪙 **الميزانية الحالية:** `{current_pool:,}` Cosmo.")

        embed = discord.Embed(
            title=f"📋 سجل ميزانية السيرفر | {ctx.guild.name}",
            description=f"🪙 **الميزانية المتاحة حالياً:** `{current_pool:,}` Cosmo\n\nعرض آخر **5** عمليات تمت على الميزانية:",
            color=discord.Color.gold()
        )

        for log_id, action, amt, old_b, new_b, admin_id, t_stamp in rows:
            action_icon = "🟢 إضافة" if action == "إضافة" else "🔴 خصم"
            details = (
                f"• **نوع العملية:** {action_icon}\n"
                f"• **المبلغ:** `{amt:,}` Cosmo\n"
                f"• **الرصيد:** من `{old_b:,}` ➔ إلى `{new_b:,}`\n"
                f"• **المسؤول:** <@{admin_id}> (`{admin_id}`)\n"
                f"• **التاريخ:** `{t_stamp}`"
            )
            embed.add_field(name=f"🔖 عملية رقم #{log_id}", value=details, inline=False)

        embed.set_footer(text="Cosmo Server Budget History")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="premium_code", aliases=["انشاء_كود_بريميوم"], description="إنشاء كود بريميوم مكون من 14 حرف لمدة محددة")
    async def premium_code(self, ctx: commands.Context, duration: str):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك ومطور البوت الأساسي.")

        duration_sec = parse_duration(duration)
        if not duration_sec:
            return await ctx.send("❌ صيغة الوقت غير صحيحة. مثال: `30d`, `7d`, `12h`.")

        chars = string.ascii_uppercase + string.digits
        code = ''.join(random.choice(chars) for _ in range(14))
        now = int(time.time())

        global_cursor.execute("INSERT INTO premium_codes (code, duration_seconds, created_at, is_used) VALUES (?, ?, ?, 0)", (code, duration_sec, now))
        global_conn.commit()
        await ctx.send(f"💎 **تم إنشاء كود بريميوم جديد:**\n• **الكود:** `{code}`\n• **المدة:** `{duration}`\n• **طريقة التفعيل:** `/active_premium code:{code}`")

    @commands.hybrid_command(name="active_premium", aliases=["تفعيل_البريميوم"], description="تفعيل كود البريميوم للسيرفر الحالي")
    async def active_premium(self, ctx: commands.Context, code: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لتفعيل الكود في هذا السيرفر.")

        clean_code = code.strip().upper()
        global_cursor.execute("SELECT duration_seconds, is_used FROM premium_codes WHERE code = ?", (clean_code,))
        row = global_cursor.fetchone()

        if not row:
            return await ctx.send("❌ كود البريميوم غير صالح أو غير موجود.")
        if row[1] == 1:
            return await ctx.send("❌ تم استخدام هذا الكود مسبقاً.")

        duration_sec = row[0]
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT premium_end FROM premium_info WHERE guild_id = ?", (ctx.guild.id,))
        s_row = g_cur.fetchone()
        current_end = s_row[0] if s_row and s_row[0] else 0

        now = int(time.time())
        new_end = (current_end + duration_sec) if current_end > now else (now + duration_sec)

        g_cur.execute(
            "INSERT INTO premium_info (guild_id, activated_at, premium_end) VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET premium_end = excluded.premium_end",
            (ctx.guild.id, now, new_end)
        )
        g_conn.commit()
        g_conn.close()

        global_cursor.execute("UPDATE premium_codes SET is_used = 1, used_guild_id = ? WHERE code = ?", (ctx.guild.id, clean_code))
        global_conn.commit()
        await ctx.send(f"🎉 **تم تفعيل اشتراك البريميوم لهذا السيرفر بنجاح!**\n• ينتهي في: <t:{new_end}:F> (<t:{new_end}:R>)")

    @commands.hybrid_command(name="premium_status", aliases=["حالة_البريميوم"], description="عرض تفاصيل اشتراك البريميوم في هذا السيرفر والوقت المتبقي")
    async def premium_status(self, ctx: commands.Context):
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT activated_at, premium_end, custom_name, custom_avatar_url, custom_banner_url FROM premium_info WHERE guild_id = ?", (ctx.guild.id,))
        row = g_cur.fetchone()
        g_conn.close()

        now = int(time.time())
        if not row or not row[1] or now >= row[1]:
            return await ctx.send("❌ هذا السيرفر لا يمتلك اشتراك بريميوم نشط حالياً (أو قد انتهت صلاحيته).")

        embed = discord.Embed(title=f"💎 تفاصيل اشتراك البريميوم | {ctx.guild.name}", color=discord.Color.purple())
        embed.add_field(name="📅 تاريخ التفعيل", value=f"<t:{row[0]}:F>", inline=False)
        embed.add_field(name="⏳ تاريخ الانتهاء", value=f"<t:{row[1]}:F>", inline=False)
        embed.add_field(name="⏱️ الوقت المتبقي", value=f"<t:{row[1]}:R>", inline=False)
        embed.add_field(name="🎨 الاسم المخصص", value=f"`{row[2]}`" if row[2] else "الافتراضي", inline=True)
        embed.add_field(name="🖼️ الصورة المخصصة", value="مفعلة ✅" if row[3] else "الافتراضية", inline=True)
        embed.add_field(name="🚩 البنر المخصص", value="مفعل ✅" if len(row) > 4 and row[4] else "الافتراضي", inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="set_bot_name", aliases=["تغيير_اسم_البوت"], description="تغيير اسم ولقب البوت داخل هذا السيرفر (بريميوم)")
    async def set_bot_name(self, ctx: commands.Context, new_name: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لإدارة البوت.")
        if not is_guild_premium(ctx.guild.id):
            return await ctx.send("💎 **ميزة بريميوم:** تغيير اسم ولقب البوت يتطلب اشتراك بريميوم نشط للسيرفر!")
        try:
            await ctx.guild.me.edit(nick=new_name[:32])
            g_conn, g_cur = get_guild_db(ctx.guild.id)
            g_cur.execute("UPDATE premium_info SET custom_name = ? WHERE guild_id = ?", (new_name[:32], ctx.guild.id))
            g_conn.commit()
            g_conn.close()
            await ctx.send(f"✅ تم تغيير اسم البوت بنجاح إلى: **{new_name[:32]}**.")
        except Exception as e:
            await ctx.send(f"❌ تعذر تغيير الاسم: {e}")

    @commands.hybrid_command(name="set_bot_avatar", aliases=["تغيير_صورة_البوت"], description="تغيير صورة البوت الخاصة بهذا السيرفر (بريميوم)")
    async def set_bot_avatar(self, ctx: commands.Context, image: discord.Attachment):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لإدارة البوت.")
        if not is_guild_premium(ctx.guild.id):
            return await ctx.send("💎 **ميزة بريميوم:** تغيير صورة البوت تتطلب اشتراك بريميوم نشط للسيرفر!")
        if not image.content_type or not image.content_type.startswith("image/"):
            return await ctx.send("❌ يرجى إرفاق ملف صورة صالح.")
        try:
            img_bytes = await image.read()
            await ctx.guild.me.edit(avatar=img_bytes)
            g_conn, g_cur = get_guild_db(ctx.guild.id)
            g_cur.execute("UPDATE premium_info SET custom_avatar_url = ? WHERE guild_id = ?", (image.url, ctx.guild.id))
            g_conn.commit()
            g_conn.close()
            await ctx.send("✅ تم تغيير صورة البوت بنجاح!")
        except Exception as e:
            await ctx.send(f"❌ حدث خطأ: {e}")

    @commands.hybrid_command(
        name="set_bot_banner",
        aliases=["تغيير_بنر_البوت", "بنر_البوت"],
        description="تغيير بنر البوت الخاص بهذا السيرفر فقط (ميزة بريميوم)"
    )
    async def set_bot_banner(self, ctx: commands.Context, image: discord.Attachment):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لإدارة البوت.")
        if not is_guild_premium(ctx.guild.id):
            return await ctx.send("💎 **ميزة بريميوم:** تغيير بنر البوت تتطلب اشتراك بريميوم نشط للسيرفر!")

        if not image.content_type or not image.content_type.startswith("image/"):
            return await ctx.send("❌ يرجى إرفاق ملف صورة صالح (PNG/JPG/GIF).")

        await ctx.defer()

        try:
            img_bytes = await image.read()
            mime_type = image.content_type
            b64_data = base64.b64encode(img_bytes).decode("utf-8")
            data_uri = f"data:{mime_type};base64,{b64_data}"

            route = discord.http.Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=ctx.guild.id)
            await self.bot.http.request(route, json={"banner": data_uri})

            g_conn, g_cur = get_guild_db(ctx.guild.id)
            g_cur.execute("UPDATE premium_info SET custom_banner_url = ? WHERE guild_id = ?", (image.url, ctx.guild.id))
            g_conn.commit()
            g_conn.close()

            embed = discord.Embed(
                title="✅ تم تغيير بنر البوت في هذا السيرفر بنجاح!",
                description="تم تطبيق البنر الجديد في بروفايل البوت الخاص بهذا السيرفر حصراً.",
                color=discord.Color.green()
            )
            embed.set_image(url=image.url)
            await ctx.send(embed=embed)

        except discord.HTTPException as e:
            await ctx.send(f"❌ تعذر تغيير البنر: {e}")
        except Exception as e:
            await ctx.send(f"❌ حدث خطأ غير متوقع: {e}")

    @commands.hybrid_command(name="set_global_request_log", aliases=["لوج_الطلبات_العام"], description="تحديد الروم الموحد لاستقبال طلبات التبديل من كل السيرفرات (مالك البوت فقط)")
    async def set_global_request_log(self, ctx: commands.Context, channel: discord.TextChannel):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك ومطور البوت الأساسي.")
        global_cursor.execute("INSERT OR REPLACE INTO global_settings (key, value) VALUES ('global_request_log', ?)", (channel.id,))
        global_conn.commit()
        await ctx.send(f"✅ تم ضبط روم استقبال طلبات التبديل العام على: {channel.mention}")

    @commands.hybrid_command(name="set_delivery_log", aliases=["لوج_التسليمات"], description="تحديد روم تسليمات وإثباتات التبديل لهذا السيرفر")
    async def set_delivery_log(self, ctx: commands.Context, channel: discord.TextChannel):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO settings (guild_id, delivery_log) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET delivery_log = excluded.delivery_log",
            (ctx.guild.id, channel.id)
        )
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم ضبط روم تسليمات التبديل لهذا السيرفر على: {channel.mention}")

    @commands.hybrid_command(name="set_transfer_log", aliases=["لوج_التحويل"], description="تحديد روم إشعارات التحويل")
    async def set_transfer_log(self, ctx: commands.Context, channel: discord.TextChannel):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("UPDATE settings SET transfer_log = ? WHERE guild_id = ?", (channel.id, ctx.guild.id))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم ضبط روم لوج التحويلات على: {channel.mention}")

    @commands.hybrid_command(name="set_success_log", aliases=["لوج_نجاح_التبديل"], description="تحديد روم إعلانات التبديل الناجحة الخاص بهذا السيرفر")
    async def set_success_log(self, ctx: commands.Context, channel: discord.TextChannel):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("UPDATE settings SET exchange_success_log = ? WHERE guild_id = ?", (channel.id, ctx.guild.id))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم ضبط روم نجاح التبديل على: {channel.mention}")

    @commands.hybrid_command(name="set_balance_log", aliases=["لوج_الرصيد"], description="تحديد روم إشعارات إضافة وخصم الرصيد")
    async def set_balance_log(self, ctx: commands.Context, channel: discord.TextChannel):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("UPDATE settings SET balance_log = ? WHERE guild_id = ?", (channel.id, ctx.guild.id))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم ضبط روم لوج إضافة/خصم الرصيد على: {channel.mention}")

    @commands.hybrid_command(name="toggle_exchange_system", aliases=["قفل_التبديل_العام"], description="قفل أو فتح نظام التبديل كاملاً في هذا السيرفر")
    async def toggle_exchange_system(self, ctx: commands.Context, status: bool):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        val = 1 if status else 0
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("UPDATE settings SET exchange_enabled = ? WHERE guild_id = ?", (val, ctx.guild.id))
        g_conn.commit()
        g_conn.close()
        msg = "مفتوحاً للجميع ✅" if status else "مقفلاً حالياً ⛔"
        await ctx.send(f"⚙️ تم ضبط نظام التبديل ليكون: **{msg}**.")

    @commands.hybrid_command(name="toggle_currency_status", aliases=["قفل_عملة"], description="قفل أو فتح تبديل عملة معينة في السيرفر")
    async def toggle_currency_status(self, ctx: commands.Context, currency_name: str, status: bool):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        val = 1 if status else 0
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("UPDATE exchange_currencies SET is_active = ? WHERE LOWER(currency_name) = LOWER(?)", (val, currency_name.strip()))
        g_conn.commit()
        g_conn.close()
        msg = "متاحة للتبديل ✅" if status else "معطلة ومقفلة ⛔"
        await ctx.send(f"⚙️ عملة **{currency_name}** أصبحت الآن: **{msg}**.")

    @commands.hybrid_command(name="add_exchange_currency", aliases=["اضافة_عملة_تبديل"], description="إضافة/تعديل عملة تبديل بالسيرفر")
    async def add_exchange_currency(self, ctx: commands.Context, currency_name: str, rate: float, min_received: float = 0.0):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT OR REPLACE INTO exchange_currencies (currency_name, exchange_rate, is_active, min_received) VALUES (?, ?, 1, ?)",
            (currency_name.strip(), rate, min_received)
        )
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم ضبط عملة **{currency_name}**:\n• السعر: `1 Cosmo = {rate} {currency_name}`\n• الحد الأدنى: `{min_received} {currency_name}`.")

    @commands.hybrid_command(name="delete_exchange_currency", aliases=["حذف_عملة_تبديل"], description="حذف عملة من قائمة التبديل في هذا السيرفر")
    async def delete_exchange_currency(self, ctx: commands.Context, currency_name: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("DELETE FROM exchange_currencies WHERE LOWER(currency_name) = LOWER(?)", (currency_name.strip(),))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"🗑️ تم حذف العملة **{currency_name}** بنجاح.")

    @commands.hybrid_command(name="set_admin_role", aliases=["رتبة_المسؤول"], description="تعيين رتبة مسؤولي البوت")
    async def set_admin_role(self, ctx: commands.Context, role: discord.Role):
        if ctx.author.id != ctx.guild.owner_id and ctx.author.id != PRIMARY_OWNER_ID:
            return await ctx.send("⛔ مالك السيرفر فقط يمكنه تعيين الرتبة.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("UPDATE settings SET admin_role_id = ? WHERE guild_id = ?", (role.id, ctx.guild.id))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم اعتماد رتبة {role.mention} كمسؤول للبوت.")

    @commands.hybrid_command(name="set_blacklist_role", aliases=["رتبة_البلاك_ليست"], description="تحديد رتبة البلاك ليست")
    async def set_blacklist_role(self, ctx: commands.Context, role: discord.Role):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("UPDATE settings SET blacklist_role_id = ? WHERE guild_id = ?", (role.id, ctx.guild.id))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم اعتماد رتبة البلاك ليست: {role.mention}.")

    @commands.hybrid_command(name="allow_bot", aliases=["سماح_لبوت"], description="السماح لبوت معين باستخدام أوامر الإدارة في روم محدد")
    async def allow_bot(self, ctx: commands.Context, bot_id: str, channel: discord.TextChannel):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        try:
            b_id = int(bot_id)
        except ValueError:
            return await ctx.send("❌ يرجى إدخال معرف بوت صالح.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("INSERT OR REPLACE INTO bot_whitelist (bot_id, channel_id) VALUES (?, ?)", (b_id, channel.id))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم السماح للبوت (`ID: {b_id}`) في {channel.mention}.")

    @commands.hybrid_command(name="disallow_bot", aliases=["الغاء_سماح_لبوت"], description="إلغاء تصريح البوت من تنفيذ الأوامر")
    async def disallow_bot(self, ctx: commands.Context, bot_id: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        try:
            b_id = int(bot_id)
        except ValueError:
            return await ctx.send("❌ يرجى إدخال معرف بوت صالح.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("DELETE FROM bot_whitelist WHERE bot_id = ?", (b_id,))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"🗑️ تم إلغاء التصريح للبوت (`ID: {b_id}`).")

    @commands.hybrid_command(name="leave_guild", aliases=["خروج_البوت"], description="إخراج البوت من سيرفر معين (مالك البوت)")
    async def leave_guild(self, ctx: commands.Context, guild_id: str):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك البوت الأساسي.")
        try:
            target_guild = self.bot.get_guild(int(guild_id))
            if not target_guild:
                return await ctx.send("❌ البوت غير متواجد في هذا السيرفر.")
            await target_guild.leave()
            await ctx.send(f"✅ تم خروج البوت من السيرفر: **{target_guild.name}** (`{guild_id}`).")
        except Exception as e:
            await ctx.send(f"❌ حدث خطأ: {e}")

    @commands.hybrid_command(name="auto_backup", aliases=["باك_اب_تلقائي"], description="إرسال نسخة احتياطية فورية لقاعدة البيانات لخاص المالك")
    async def auto_backup(self, ctx: commands.Context):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك البوت الأساسي.")
        try:
            shutil.copyfile("global_cosmo.db", "global_cosmo_backup.db")
            file = discord.File("global_cosmo_backup.db", filename=f"global_cosmo_backup_{int(time.time())}.db")
            await ctx.author.send("📦 **نسخة احتياطية لقاعدة البيانات العامة:**", file=file)
            await ctx.send("✅ تم إرسال ملف النسخة الاحتياطية بنجاح إلى رسائلك الخاصة!")
        except Exception as e:
            await ctx.send(f"❌ حدث خطأ: {e}")

    @commands.hybrid_command(name="set_double_event", aliases=["تفعيل_الدبل"], description="تفعيل حدث مضاعفة جوائز عجلة الحظ")
    async def set_double_event(self, ctx: commands.Context, duration: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية.")
        sec = parse_duration(duration)
        if not sec:
            return await ctx.send("❌ صيغة الوقت غير صحيحة.")
        end_time = int(time.time()) + sec
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("UPDATE settings SET double_event_end = ? WHERE guild_id = ?", (end_time, ctx.guild.id))
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"🔥 تم تفعيل حدث المضاعفة (الدبل ×2)!\n⏳ ينتهي: <t:{end_time}:R>.")
        asyncio.create_task(schedule_double_announcement(ctx.channel, ctx.guild.id, sec))

async def setup(bot):
    await bot.add_cog(Admin(bot))
