import discord
from discord.ext import commands
import random
import asyncio
from datetime import datetime
from database import (
    get_balance, parse_amount, generate_captcha_image,
    is_blacklisted, is_bot_admin, global_cursor, get_guild_db, PRIMARY_OWNER_ID,
    send_global_action_log, transfer_balance, create_exchange_request,
    accept_exchange_request, deny_exchange_request, get_currency_label,
    get_exchange_currencies, get_exchange_currency, has_pending_exchange_request,
    is_global_exchange_enabled, ExchangeSystemClosed, ExchangeCurrencyUnavailable,
    EXCHANGE_BASE_COSMO, format_exchange_amount
)

def can_manage_exchange_request(ctx: commands.Context, origin_guild_id: int) -> bool:
    """مالك البوت أو مدير السيرفر صاحب الطلب يستطيع معالجة الطلب؛ لا يلزم Premium."""
    if ctx.author.id == PRIMARY_OWNER_ID:
        return True
    if not ctx.guild or ctx.guild.id != origin_guild_id:
        return False
    if ctx.author.id == ctx.guild.owner_id:
        return True
    permissions = getattr(ctx.author, "guild_permissions", None)
    if permissions and (permissions.administrator or permissions.manage_guild):
        return True
    return is_bot_admin(ctx)

class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="balance", aliases=["رصيد", "bal"], description="معرفة رصيد العملات")
    async def balance(self, ctx: commands.Context, member: discord.Member = None):
        target = member or ctx.author
        bal = get_balance(target.id)
        label = get_currency_label(ctx.guild.id if ctx.guild else None)
        await ctx.send(f"🪙 رصيد **{target.display_name}** هو: `{bal:,}` {label}")

    @commands.hybrid_command(name="transfer", aliases=["تحويل"], description="تحويل عملات Cosmo لشخص آخر")
    async def transfer(self, ctx: commands.Context, member: discord.Member, amount: str):
        currency_label = get_currency_label(ctx.guild.id if ctx.guild else None)
        if member.bot or member.id == ctx.author.id:
            return await ctx.send("❌ لا يمكنك التحويل لنفسك أو إلى بوت.")
        if is_blacklisted(ctx.author) or is_blacklisted(member):
            return await ctx.send("⛔ لا يمكن إتمام التحويل بسبب إدراج أحد الطرفين في البلاك ليست.")

        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")

        if parsed_amount <= 0:
            return await ctx.send("❌ المبلغ يجب أن يكون أكبر من 0.")
        if get_balance(ctx.author.id) < parsed_amount:
            return await ctx.send(f"❌ رصيدك من {currency_label} لا يكفي.")

        captcha_code = str(random.randint(1000, 9999))
        captcha_img = generate_captcha_image(captcha_code)
        file = discord.File(captcha_img, filename="captcha.png")

        msg = await ctx.send(f"⚠️ {ctx.author.mention} لتأكيد تحويل `{parsed_amount:,}` {currency_label}، اكتب الكود خلال **20 ثانية**:", file=file)

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            reply = await self.bot.wait_for("message", timeout=20.0, check=check)
            if reply.content.strip() == captcha_code:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if not transfer_balance(ctx.author.id, member.id, parsed_amount, now):
                    await ctx.send("❌ لم يعد رصيدك كافياً لإتمام التحويل؛ لم يُخصم أي مبلغ.")
                    try:
                        await msg.delete()
                        await reply.delete()
                    except Exception:
                        pass
                    return

                await ctx.send(f"✅ تم تحويل `{parsed_amount:,}` {currency_label} إلى {member.mention}.")
                try:
                    await msg.delete()
                    await reply.delete()
                    await member.send(f"📥 استلمت `{parsed_amount:,}` {currency_label} من **{ctx.author.name}** (`ID: {ctx.author.id}`).")
                except:
                    pass

                g_conn, g_cur = get_guild_db(ctx.guild.id)
                g_cur.execute("SELECT transfer_log FROM settings WHERE guild_id = ?", (ctx.guild.id,))
                row = g_cur.fetchone()
                g_conn.close()
                if row and row[0]:
                    log_chan = ctx.guild.get_channel(row[0])
                    if log_chan:
                        await log_chan.send(f"🔄 **تحويل جديد:** قام {ctx.author.mention} بتحويل `{parsed_amount:,}` {currency_label} إلى {member.mention}.")

                # إرسال إلى اللوج العام العالمي
                global_embed = discord.Embed(
                    title="🌐 عملية تحويل عملات (Global Transfer)",
                    color=discord.Color.blue(),
                    timestamp=datetime.now()
                )
                global_embed.add_field(name="🏛️ السيرفر", value=f"{ctx.guild.name} (`{ctx.guild.id}`)", inline=False)
                global_embed.add_field(name="📤 المرسل", value=f"{ctx.author.mention} (`{ctx.author.id}`)", inline=True)
                global_embed.add_field(name="📥 المستلم", value=f"{member.mention} (`{member.id}`)", inline=True)
                global_embed.add_field(name="🪙 المبلغ", value=f"`{parsed_amount:,}` {currency_label}", inline=False)
                global_embed.set_footer(text="Cosmo Global Economy Log")
                await send_global_action_log(self.bot, global_embed)
            else:
                await ctx.send("❌ الكود غير صحيح. تم إلغاء العملية.")
                try:
                    await msg.delete()
                except:
                    pass
        except asyncio.TimeoutError:
            try:
                await msg.delete()
            except:
                pass
            await ctx.send(f"⏱️ {ctx.author.mention} انتهى الوقت المحدد.")

    @commands.hybrid_command(name="exchange_history", aliases=["سجل_التبديل"], description="عرض سجل عمليات التبديل ورقم كل طلب")
    async def exchange_history(self, ctx: commands.Context, member: discord.Member = None):
        target = member or ctx.author
        global_cursor.execute(
            "SELECT id, guild_id, cosmo_amount, target_currency, received_amount, status, timestamp, note "
            "FROM exchange_logs WHERE user_id = ? ORDER BY id DESC LIMIT 5",
            (target.id,)
        )
        rows = global_cursor.fetchall()
        
        if not rows:
            return await ctx.send(f"ℹ️ لا توجد عمليات تبديل مسجلة لـ **{target.display_name}** حتى الآن.")

        embed = discord.Embed(
            title=f"📋 سجل طلبات التبديل | {target.display_name}",
            description="عرض آخر **5** طلبات تبديل تم تقديمها:",
            color=discord.Color.gold()
        )
        if target.display_avatar:
            embed.set_thumbnail(url=target.display_avatar.url)

        status_emojis = {
            "تم القبول": "✅ تم القبول",
            "قيد الانتظار": "⏳ قيد الانتظار",
            "مرفوض": "❌ مرفوض"
        }

        for req_id, g_id, cosmo_amt, curr_name, recv_amt, status, t_stamp, note in rows:
            guild_obj = self.bot.get_guild(g_id)
            guild_title = guild_obj.name if guild_obj else "سيرفر غير معروف"
            currency_label = get_currency_label(g_id)
            status_display = status_emojis.get(status, f"🔹 {status}")
            
            details = (
                f"• **المبلغ:** `{cosmo_amt:,}` {currency_label} ➔ `{recv_amt}` {curr_name}\n"
                f"• **الحالة:** {status_display}\n"
                f"• **السيرفر:** `{guild_title}`\n"
                f"• **التاريخ:** `{t_stamp}`\n"
                f"• **الملاحظة:** ```{note}```"
            )
            embed.add_field(name=f"🔖 طلب رقم #{req_id}", value=details, inline=False)

        embed.set_footer(text="Cosmo Currency System • Exchange History")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="exchange_list", aliases=["قائمة_العملات"], description="عرض العملات العامة المتاحة للتبديل")
    async def exchange_list(self, ctx: commands.Context):
        if not is_global_exchange_enabled():
            return await ctx.send("⛔ نظام التبديل مغلق عالميًا من سيرفر Cosmo حاليًا.")
        rows = get_exchange_currencies(active_only=True)
        if not rows:
            return await ctx.send("⚠️ لا توجد عملات مفعّلة للتبديل حاليًا في الكتالوج العام.")

        label = get_currency_label(ctx.guild.id if ctx.guild else None)
        pages = [rows[index:index + 20] for index in range(0, len(rows), 20)]
        for page_number, page in enumerate(pages, 1):
            embed = discord.Embed(
                title="💱 العملات العامة المتاحة للتبديل",
                description=(
                    f"الأسعار موحّدة لكل السيرفرات، وتُدار من سيرفر Cosmo.\n"
                    f"رصيدك الأساسي هنا يظهر باسم **{label}**."
                ),
                color=discord.Color.teal(),
            )
            for name, rate, _active, minimum in page:
                embed.add_field(
                    name=f"🪙 {name}",
                    value=(
                        f"`{format_exchange_amount(EXCHANGE_BASE_COSMO)} {label} = "
                        f"{format_exchange_amount(float(rate) * EXCHANGE_BASE_COSMO)} {name}`\n"
                        f"الحد الأدنى للاستلام: `{format_exchange_amount(float(minimum))} {name}`"
                    ),
                    inline=True,
                )
            embed.set_footer(text=f"كتالوج Cosmo العام • صفحة {page_number}/{len(pages)}")
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="exchange_stats", aliases=["احصائيات_التبديل"], description="عرض إحصائيات عمليات التبديل الناجحة في هذا السيرفر")
    async def exchange_stats(self, ctx: commands.Context):
        global_cursor.execute("SELECT COUNT(*), SUM(cosmo_amount) FROM exchange_logs WHERE guild_id = ? AND status = 'تم القبول'", (ctx.guild.id,))
        row = global_cursor.fetchone()
        total_count = row[0] if row and row[0] else 0
        total_cosmo = row[1] if row and row[1] else 0

        embed = discord.Embed(title=f"📊 إحصائيات التبديل | {ctx.guild.name}", color=discord.Color.green())
        embed.add_field(name="إجمالي الطلبات المقبولة", value=f"`{total_count:,}` عملية", inline=False)
        embed.add_field(name=f"إجمالي {get_currency_label(ctx.guild.id if ctx.guild else None)} المستبدلة", value=f"`{total_cosmo:,}` {get_currency_label(ctx.guild.id if ctx.guild else None)}", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="exchange", aliases=["تبديل"], description="طلب تبديل عملات Cosmo إلى عملة أخرى")
    async def exchange(self, ctx: commands.Context, currency_name: str, amount: str, *, note: str = "لا يوجد"):
        currency_label = get_currency_label(ctx.guild.id if ctx.guild else None)
        if not is_global_exchange_enabled():
            return await ctx.send("⛔ نظام التبديل مغلق عالميًا من سيرفر Cosmo حاليًا.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT delivery_log FROM settings WHERE guild_id = ?", (ctx.guild.id,))
        s_row = g_cur.fetchone()

        try:
            parsed_amount = parse_amount(amount)
        except:
            g_conn.close()
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")

        if parsed_amount <= 0:
            g_conn.close()
            return await ctx.send("❌ المبلغ يجب أن يكون أكبر من 0.")
        if get_balance(ctx.author.id) < parsed_amount:
            g_conn.close()
            return await ctx.send(f"❌ رصيدك من {currency_label} لا يكفي.")

        curr_row = get_exchange_currency(currency_name)
        if not curr_row:
            g_conn.close()
            return await ctx.send(f"❌ العملة `{currency_name}` غير موجودة في الكتالوج العام.")

        currency_name, rate, is_active, min_recv = curr_row
        if is_active == 0:
            g_conn.close()
            return await ctx.send(f"⛔ تبديل عملة **{currency_name}** متوقف ومقفل حالياً.")

        total_received = round(parsed_amount * rate, 4)
        if total_received < min_recv:
            g_conn.close()
            return await ctx.send(f"❌ تم رفض طلبك تلقائياً: المبلغ المستحق (`{total_received} {currency_name}`) أقل من الحد الأدنى وهو `{min_recv} {currency_name}`.")

        delivery_log_id = s_row[0] if s_row and s_row[0] else None
        g_conn.close()

        global_cursor.execute("SELECT value FROM global_settings WHERE key = 'global_request_log'")
        global_req_row = global_cursor.fetchone()

        if not global_req_row or not global_req_row[0]:
            return await ctx.send("⚠️ لم يقم مالك البوت بتعيين الروم العام لاستقبال الطلبات بعد (`/set_global_request_log`).")

        req_channel = self.bot.get_channel(global_req_row[0])
        if not req_channel:
            return await ctx.send("❌ روم استقبال طلبات التبديل العام غير متاح حالياً.")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            req_id = create_exchange_request(ctx.guild.id, ctx.author.id, parsed_amount, currency_name, total_received, note, now)
        except ExchangeSystemClosed:
            return await ctx.send("⛔ أُغلق نظام التبديل عالميًا قبل حفظ الطلب؛ لم يُخصم أي رصيد.")
        except ExchangeCurrencyUnavailable:
            return await ctx.send("⚠️ تغيرت حالة العملة أو سعرها قبل حفظ الطلب؛ لم يُخصم أي رصيد. أعد تحميل `/exchange_list` ثم حاول مجددًا.")
        if req_id is None:
            if has_pending_exchange_request(ctx.author.id):
                return await ctx.send("⏳ لديك طلب تبديل سابق قيد الانتظار؛ لم يُخصم أي مبلغ جديد. انتظر معالجته قبل إنشاء طلب آخر.")
            return await ctx.send("❌ لم يعد رصيدك كافياً لإتمام الطلب؛ لم يُخصم أي مبلغ.")

        invite_link = None
        try:
            invites = await ctx.guild.invites()
            if invites:
                invite_link = invites[0].url
            else:
                new_invite = await ctx.channel.create_invite(max_age=0, max_uses=0)
                invite_link = new_invite.url
        except:
            invite_link = None

        server_display = f"[{ctx.guild.name}]({invite_link})" if invite_link else f"**{ctx.guild.name}**"

        if delivery_log_id:
            delivery_channel = ctx.guild.get_channel(delivery_log_id)
            if delivery_channel:
                channel_link = f"https://discord.com/channels/{ctx.guild.id}/{delivery_log_id}"
                delivery_display = f"[#{delivery_channel.name}]({channel_link}) (<#{delivery_log_id}>)"
            else:
                delivery_display = f"<#{delivery_log_id}>"
        else:
            delivery_display = "`لم يتم التحديد بعد`"

        embed = discord.Embed(title=f"📥 طلب تبديل جديد #{req_id}", color=discord.Color.gold())
        embed.add_field(name="🏛️ السيرفر", value=server_display, inline=True)
        embed.add_field(name="📦 روم التسليمات", value=delivery_display, inline=True)
        embed.add_field(name="👤 العضو", value=f"{ctx.author.mention} (`{ctx.author.id}`)", inline=False)
        embed.add_field(name=f"🪙 مبلغ {currency_label}", value=f"`{parsed_amount:,}`", inline=True)
        embed.add_field(name=f"💵 المستحق من ({currency_name})", value=f"`{total_received}`", inline=True)
        embed.add_field(name="📝 الوصف والملاحظات", value=f"```{note}```", inline=False)
        embed.set_footer(text=f"للقبول: /accept_exchange {req_id} | للرفض: /deny_exchange {req_id}")

        try:
            await req_channel.send(embed=embed)
        except discord.HTTPException:
            deny_exchange_request(req_id)
            return await ctx.send("❌ تعذر إرسال الطلب للإدارة؛ أُعيد رصيدك تلقائياً.")
        await ctx.send(f"✅ تم إرسال طلب التبديل برقم `{req_id}` إلى الإدارة بنجاح.")

    @commands.hybrid_command(name="accept_exchange", aliases=["قبول_التبديل"], description="قبول طلب التبديل")
    async def accept_exchange(self, ctx: commands.Context, req_id: int):
        global_cursor.execute("SELECT user_id, cosmo_amount, target_currency, received_amount, status, guild_id FROM exchange_logs WHERE id = ?", (req_id,))
        row = global_cursor.fetchone()
        if not row or row[4] != "قيد الانتظار":
            return await ctx.send("❌ الطلب غير موجود أو تمت معالجته مسبقاً.")

        if not can_manage_exchange_request(ctx, row[5]):
            return await ctx.send("⛔ يجب أن تكون مسؤولاً في السيرفر الذي يخصه طلب التبديل.")
        if not accept_exchange_request(req_id):
            return await ctx.send("❌ تمت معالجة هذا الطلب بالفعل؛ لم يُنفذ القبول مرة أخرى.")

        await ctx.send(f"✅ تم قبول طلب التبديل #{req_id}.")

        origin_guild_id = row[5]
        origin_currency_label = get_currency_label(origin_guild_id)
        origin_guild = self.bot.get_guild(origin_guild_id)
        if origin_guild:
            g_conn, g_cur = get_guild_db(origin_guild_id)
            g_cur.execute("SELECT exchange_success_log FROM settings WHERE guild_id = ?", (origin_guild_id,))
            s_log = g_cur.fetchone()
            g_conn.close()
            if s_log and s_log[0]:
                succ_chan = origin_guild.get_channel(s_log[0])
                if succ_chan:
                    await succ_chan.send(f"🎉 تم بنجاح تبديل `{row[1]:,}` {origin_currency_label} للعضو <@{row[0]}> مقابل `{row[3]}` {row[2]}.")

    @commands.hybrid_command(name="deny_exchange", aliases=["رفض_التبديل"], description="رفض طلب التبديل مع ذكر السبب")
    async def deny_exchange(self, ctx: commands.Context, req_id: int, *, reason: str = "غير محدد"):
        global_cursor.execute("SELECT user_id, cosmo_amount, target_currency, status, guild_id FROM exchange_logs WHERE id = ?", (req_id,))
        row = global_cursor.fetchone()
        if not row or row[3] != "قيد الانتظار":
            return await ctx.send("❌ الطلب غير صالح أو تمت معالجته.")

        if not can_manage_exchange_request(ctx, row[4]):
            return await ctx.send("⛔ يجب أن تكون مسؤولاً في السيرفر الذي يخصه طلب التبديل.")
        refunded = deny_exchange_request(req_id)
        if not refunded:
            return await ctx.send("❌ تمت معالجة هذا الطلب بالفعل؛ لم يُرد الرصيد مرة أخرى.")

        await ctx.send(f"🛑 تم رفض الطلب #{req_id} وإعادة الرصيد.")
        try:
            user = await self.bot.fetch_user(refunded[0])
            if user:
                await user.send(f"❌ تم رفض طلب التبديل #{req_id} وإعادة `{refunded[1]:,}` {get_currency_label(row[4])} لحسابك.\n**السبب:** `{reason}`")
        except:
            pass

    @commands.hybrid_command(name="transfer_history", aliases=["سجل_التحويل"], description="عرض آخر عمليات التحويل العامة")
    async def transfer_history(self, ctx: commands.Context, member: discord.Member = None):
        target = member or ctx.author
        currency_label = get_currency_label(ctx.guild.id if ctx.guild else None)
        global_cursor.execute("SELECT sender_id, receiver_id, amount, timestamp FROM transfer_logs WHERE sender_id = ? OR receiver_id = ? ORDER BY id DESC LIMIT 5", (target.id, target.id))
        rows = global_cursor.fetchall()
        if not rows:
            return await ctx.send("لا توجد تحويلات سابقة مسجلة.")
        
        msg = f"📋 **آخر تحويلات {target.display_name}:**\n"
        for r in rows:
            action = "إرسال إلى" if r[0] == target.id else "استلام من"
            other_id = r[1] if r[0] == target.id else r[0]
            msg += f"• {action} <@{other_id}> بمبلغ `{r[2]:,}` {currency_label} بتاريخ `{r[3]}`\n"
        await ctx.send(msg)

async def setup(bot):
    await bot.add_cog(Economy(bot))
