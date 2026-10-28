import discord
from discord.ext import commands
import asyncio
import time
from database import parse_duration, check_is_owner


class HelpView(discord.ui.View):
    def __init__(self, author: discord.Member):
        super().__init__(timeout=180)
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("⛔ هذه الأزرار خاصة بصاحب الأمر فقط.", ephemeral=True)
            return False
        return True

    async def show_page(self, interaction, title, description, color, fields, footer):
        embed = discord.Embed(title=title, description=description, color=color)
        for name, value in fields:
            # Discord limits each embed field value to 1024 characters. Keep the
            # help button usable even when a command section grows over time.
            value = str(value)
            chunks = []
            remaining = value
            while len(remaining) > 1024:
                split_at = remaining.rfind("\n", 0, 1024)
                if split_at <= 0:
                    split_at = 1024
                chunks.append(remaining[:split_at])
                remaining = remaining[split_at:].lstrip("\n")
            chunks.append(remaining)
            for index, chunk in enumerate(chunks):
                field_name = name if index == 0 else f"{name} (تابع {index + 1})"
                embed.add_field(name=field_name[:256], value=chunk[:1024] or "—", inline=False)
        embed.set_footer(text=footer)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="العامة 🌐", style=discord.ButtonStyle.primary, row=0)
    async def general_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_page(
            interaction,
            "🌐 أوامر الأعضاء العامة",
            "اختر الأمر المناسب من القائمة:",
            discord.Color.blue(),
            [
                ("🪙 الرصيد والتحويل والتبديل", (
                    "• `/balance [@user]` — عرض الرصيد.\n"
                    "• `/exchange <currency> <amount> [note]` — تقديم طلب تبديل.\n"
                    "• `/exchange_history` — سجل طلبات التبديل.\n"
                    "• `/exchange_list` — العملات وأسعارها.\n"
                    "• `/exchange_stats` — إحصائيات التبديل.\n"
                    "• `/transfer @user <amount>` — تحويل رصيد مع تحقق.\n"
                    "• `/transfer_history` — سجل التحويلات."
                )),
                ("🤖 الذكاء الاصطناعي", (
                    "• `/ask <prompt>` — التحدث مع Gemini.\n"
                    "• `/reset_chat` — بدء محادثة جديدة.\n"
                    "• ويمكن مناداة البوت مباشرةً في الرسائل."
                )),
                ("ℹ️ معلومات وخدمات", (
                    "• `/help` — عرض هذه القائمة.\n"
                    "• `/my_boost_status` — حالة مكافآت البوست.\n"
                    "• `/ping` — فحص الاستجابة.\n"
                    "• `/server_budget` — عرض ميزانية الجوائز المتاحة.\n"
                    "• `/server_count` — إحصاء السيرفرات والأعضاء.\n"
                    "• `/timer <duration> [reason]` — مؤقت تنبيهي."
                )),
            ],
            "Cosmo System • الأوامر العامة",
        )

    @discord.ui.button(label="الألعاب والجوائز 🎁", style=discord.ButtonStyle.success, row=0)
    async def games_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_page(
            interaction,
            "🎁 الألعاب والجوائز",
            "ألعاب وفعاليات السيرفر:",
            discord.Color.green(),
            [
                ("🪙 شحن وخصم رصيد الأعضاء", (
                    "• `/add_balance @user <amount>` — شحن رصيد عضو من ميزانية السيرفر.\n"
                    "• `/remove_balance @user <amount>` — خصم رصيد عضو."
                )),
                ("🎉 الفعاليات", (
                    "• `/box <amount> [condition]` — بوكس سريع.\n"
                    "• `/giveaway <amount> <duration> <winners> [condition]` — سحب بالريأكشن.\n"
                    "• `/stop_giveaway <message_id>` — إيقاف جيف نشط وإرجاع كامل المبلغ المحجوز للسيرفر.\n"
                    "• `/end_giveaway <message_id>` — إنهاء الجيف فوراً واختيار الفائزين وتسليم الجوائز تلقائياً إذا لم يوجد شرط.\n"
                    "• `/reroll_giveaway <message_id>` — إعادة اختيار كل الفائزين بشرط عدم صرف أي جائزة؛ وقد يتطلب إعادة حجز المبالغ المستردة.\n"
                    "• `/roulette <amount> [condition]` — لعبة الروليت.\n"
                    "• `/stop_roulette` — إيقاف الروليت الجارية.\n"
                    "• `!set_double_event <duration>` — تفعيل مضاعفة جوائز العجلة."
                )),
            ],
            "Cosmo System • الألعاب والجوائز",
        )

    @discord.ui.button(label="التذاكر 🎫", style=discord.ButtonStyle.primary, row=0)
    async def tickets_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_page(
            interaction,
            "🎫 نظام التذاكر",
            "إنشاء التذاكر وإدارتها:",
            discord.Color.teal(),
            [
                ("📩 الإعداد والاستخدام", (
                    "• `/setup_ticket` — نشر رسالة فتح التذاكر في الروم الحالي.\n"
                    "• `/set_ticket_log #channel` — تعيين قناة أرشيف التذاكر لهذا السيرفر فقط.\n"
                    "• `/add_person <member_id_or_mention>` — إضافة عضو إلى التذكرة الحالية (الإدارة فقط).\n"
                    "• `/rename <name>` — إعادة تسمية التذكرة (للإدارة فقط).\n"
                    "• `/close` — إغلاق التذكرة وإخفاؤها عن صاحبها.\n"
                    "• `/delete` — حفظ المحادثة في ملف وإرسالها إلى قناة أرشيف هذا السيرفر ثم حذف التذكرة."
                )),
                ("🔐 الصلاحيات", (
                    "رتبة `owner` ومالك السيرفر يمكنهما إعداد نظام التذاكر وإعادة التسمية. رتبة `admin` المحدودة تستطيع مشاهدة التذاكر وإضافة أعضاء وإغلاقها وحذفها فقط. صاحب التذكرة يستطيع إغلاق تذكرته."
                )),
            ],
            "Cosmo System • التذاكر",
        )

    @discord.ui.button(label="إدارة السيرفر ⚙️", style=discord.ButtonStyle.danger, row=1)
    async def admin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_page(
            interaction,
            "⚙️ إدارة السيرفر",
            "أوامر إدارة السيرفر:",
            discord.Color.red(),
            [
                ("💬 الردود والاختصارات", (
                    "• `!add_alias <command> <alias>` و`!list_aliases` — إدارة الاختصارات.\n"
                    "• `!add_reply <trigger> <reply>` — إضافة رد تلقائي.\n"
                    "• `!delete_reply <trigger>` و`!list_replies` — حذف الردود وعرضها.\n"
                    "• `/ai_toggle` — تشغيل أو إيقاف ميزات الذكاء الاصطناعي."
                )),
                ("💱 التبديل والبوست", (
                    "• `/accept_exchange <id>` و`/deny_exchange <id>` — معالجة طلبات التبديل.\n"
                    "• `/add_exchange_currency` و`/delete_exchange_currency` و`/toggle_currency_status` — إدارة الكتالوج العام من سيرفر Cosmo فقط.\n"
                    "• `/toggle_exchange_system <true|false>` — فتح أو قفل التبديل عالميًا من سيرفر Cosmo فقط.\n"
                    "• الإغلاق يمنع الطلبات الجديدة؛ الطلبات المعلقة تبقى في قائمة الإدارة للقبول أو الرفض.\n"
                    "• `/setup_boost_rewards` و`/toggle_boost_system` — إعداد مكافآت البوست.\n"
                    "• `/fake_boost <1|2>` — اختبار مكافآت البوست."
                )),
                ("📋 الرتب والسجلات", (
                    "• `/setup` — فتح معالج إعداد قنوات السيرفر خطوة بخطوة.\n"
                    "• `!server_budget_history` — سجل حركة ميزانية الجوائز.\n"
                    "• `/set_owner_role` — رتبة بمعاملة مالك السيرفر داخل البوت (لا تتجاوز أوامر مالك البوت).\n"
                    "• `/set_staff_role` — تعيين رتبة محدودة للتذاكر والراتب اليومي فقط.\n"
                    "• `/set_blacklist_role` — تعيين رتبة البلاك ليست.\n"
                    "• `!set_balance_log` و`/set_boost_log` — قنوات سجلات الرصيد والبوست.\n"
                    "• `!set_delivery_log` و`!set_success_log` — قنوات التسليم والنجاح.\n"
                    "• `!set_transfer_log` — قناة سجل التحويلات.\n"
                    "• `!set_invite_log #channel` — تحديد روم سجل الدخول والخروج وتصنيف الدعوات.\n"
                    "• `!disable_invite_log` — إيقاف سجل الدعوات.\n"
                    "• `/invite_event_start <amount>` — بدء فعالية الدعوات في روم الأمر.\n"
                    "• `/invite_event_end` — إنهاء فعالية الدعوات وإيقاف المكافآت."
                )),
                ("📈 النقاط واللفلات والرواتب", (
                    "• `/add_level_channel` و`/remove_level_channel` و`/list_level_channels` — تحديد رومات احتساب الرسائل.\n"
                    "• `/toggle_level_system <true|false>` — فتح النظام أو إغلاقه لهذا السيرفر فقط.\n"
                    "• `/toggle_voice_level_system <true|false>` — فتح نظام الفويس أو إغلاقه بشكل مستقل؛ يرصد تلقائياً الرومات الصوتية المؤقتة والعادية عدا AFK.\n"
                    "• `/set_level_reward <amount>` — مكافأة المستوى.\n"
                    "• `/level_points` و`/top` — عرض النقاط والترتيب.\n"
                    "• `/set_level_announcement_channel` — روم إعلانات اللفلات.\n"
                    "• `/set_level_salary` و`/level_salary_history` — إعداد ومراجعة الرواتب."
                )),
                ("🏦 شحن ميزانية السيرفر", (
                    "• `/buy_budget <amount>` — لمالك السيرفر أو مسؤوليه؛ اختر وسيلة دفع مفعلة.\n"
                    "• تأكيد ProBot أو AdamC يُطابق آليًا خلال 5 دقائق؛ الخيار يتطلب مبلغًا من مضاعفات 5 Cosmo.\n"
                    "• خيار LTC ينشئ فاتورة دفع عبر LTCPay؛ تُضاف الميزانية بعد تحقق الفاتورة."
                )),
            ],
            "Cosmo System • إدارة السيرفر",
        )

    @discord.ui.button(label="Premium والمالك 👑", style=discord.ButtonStyle.secondary, row=1)
    async def owner_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.show_page(
            interaction,
            "👑 Premium وأدوات مالك البوت",
            "أوامر Premium وأدوات المالك:",
            discord.Color.gold(),
            [
                ("💎 Premium", (
                    "• `/buy_premium <duration>` — شراء Premium للسيرفر الحالي عبر ProBot أو AdamC أو Cosmo أو LTC، ويُفعّل تلقائيًا بعد التحقق.\n"
                    "• `/active_premium <code>` — تفعيل اشتراك السيرفر.\n"
                    "• `/premium_status` — عرض حالة الاشتراك.\n"
                    "• `!set_currency_name <name>` — تغيير اسم عرض الرصيد في هذا السيرفر فقط؛ اكتب `Cosmo` لإعادة الاسم الافتراضي.\n"
                    "• `!set_bot_name <name>` — تخصيص اسم البوت في السيرفر.\n"
                    "• `!set_bot_avatar <file>` — تخصيص صورة البوت.\n"
                    "• `!set_bot_banner <file>` — تخصيص بنر البوت."
                )),
                ("🔒 مالك البوت فقط", (
                    "• `!add_server_budget <amount>` و`!remove_server_budget <amount>` — إضافة أو خصم ميزانية سيرفر يدويًا (مالك البوت فقط).\n"
                    "• `/toggle_buy_budget <credit|adamc|crypto> <true|false>` — تشغيل أو إيقاف وسيلة دفع الشحن التلقائي عالميًا.\n"
                    "• إيقاف وسيلة دفع يلغي طلباتها المعلقة؛ من حوّل بالفعل عليه التواصل مع مالك البوت للمراجعة اليدوية.\n"
                    "• `!set_server_budget <guild_id> <amount>` — تعيين ميزانية سيرفر.\n"
                    "• `/premium_code <duration>` — إنشاء كود Premium.\n"
                    "• `!set_premium_price <duration> <credit> <adamc> <cosmo> <crypto_usd>` — تحديد أسعار Premium لكل مدة.\n"
                    "• `!toggle_premium_purchase <true|false>` و`!toggle_premium_method <credit|adamc|cosmo|crypto> <true|false>` — فتح/إغلاق الشراء أو وسيلة دفع.\n"
                    "• `!set_global_log` و`!disable_global_log` — إعداد سجل العمليات العام.\n"
                    "• `!set_global_request_log` — تحديد قناة الطلبات العامة.\n"
                    "• `!allow_bot` و`!disallow_bot` — إدارة قائمة البوتات المسموحة.\n"
                    "• `!auto_backup` — طلب نسخة احتياطية.\n"
                    "• `!leave_guild <id>` — إخراج البوت من سيرفر."
                )),
            ],
            "Cosmo System • Premium والمالك",
        )


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="help", aliases=["مساعدة"], description="عرض قائمة أوامر البوت مقسمة بالأزرار")
    async def help_command(self, ctx: commands.Context):
        embed = discord.Embed(
            title="✨ قائمة أوامر بوت Cosmo",
            description="اختر القسم المطلوب لعرض الأوامر المتاحة.",
            color=discord.Color.purple()
        )
        embed.set_footer(text="Cosmo • اختر قسمًا من الأزرار")
        view = HelpView(ctx.author)
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="ping", aliases=["بنق"], description="فحص سرعة استجابة البوت")
    async def ping(self, ctx: commands.Context):
        latency = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 بونق! سرعة الاستجابة: `{latency}ms`")

    @commands.hybrid_command(name="server_count", aliases=["عدد_السيرفرات"], description="عرض عدد وإحصائيات السيرفرات")
    async def server_count(self, ctx: commands.Context):
        if not await check_is_owner(self.bot, ctx):
            total_guilds = len(self.bot.guilds)
            total_members = sum(g.member_count for g in self.bot.guilds if g.member_count)
            return await ctx.send(f"📊 يتواجد البوت حالياً في **{total_guilds}** سيرفر، ويخدم أكثر من **{total_members:,}** عضو.")

        await ctx.defer()
        total_guilds = len(self.bot.guilds)
        total_members = sum(g.member_count for g in self.bot.guilds if g.member_count)
        server_entries = []

        for idx, guild in enumerate(self.bot.guilds, 1):
            invite_link = None
            try:
                invites = await guild.invites()
                if invites:
                    invite_link = invites[0].url
            except Exception:
                pass

            if not invite_link:
                for channel in guild.text_channels:
                    if channel.permissions_for(guild.me).create_instant_invite:
                        try:
                            new_inv = await channel.create_invite(max_age=0, max_uses=0, reason="Server List Inspection")
                            invite_link = new_inv.url
                            break
                        except Exception:
                            continue

            invite_display = f"[اضغط هنا للدخول]({invite_link})" if invite_link else "`لا تتوفر صلاحية إنشاء رابط`"
            entry = f"**#{idx} | {guild.name}**\n• **ID:** `{guild.id}`\n• **الأعضاء:** `{guild.member_count:,}`\n• **رابط الدعوة:** {invite_display}\n"
            server_entries.append(entry)

        chunks, current_chunk = [], ""
        for entry in server_entries:
            if len(current_chunk) + len(entry) > 1800:
                chunks.append(current_chunk)
                current_chunk = entry
            else:
                current_chunk += entry
        if current_chunk:
            chunks.append(current_chunk)

        for i, chunk in enumerate(chunks, 1):
            embed = discord.Embed(
                title=f"📊 قائمة سيرفرات البوت ({total_guilds} سيرفر) - صفحة {i}/{len(chunks)}",
                description=f"👥 **إجمالي الأعضاء:** `{total_members:,}` عضو\n\n" + chunk,
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"طلب بواسطة المالك: {ctx.author.name}")
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="timer", aliases=["مؤقت"], description="إنشاء مؤقت مع تنبيه عند الانتهاء")
    async def timer(self, ctx: commands.Context, duration: str, *, reason: str = "انتهى الوقت!"):
        sec = parse_duration(duration)
        if not sec:
            return await ctx.send("❌ صيغة الوقت غير صحيحة. مثال: `10s`, `5m`, `1h`.")
        end_t = int(time.time()) + sec
        await ctx.send(f"⏳ **المؤقت بدأ:** سينتهي <t:{end_t}:R>.\n📌 **السبب:** `{reason}`")
        await asyncio.sleep(sec)
        await ctx.send(f"⏰ {ctx.author.mention} **المؤقت انتهى!** ➔ `{reason}`")


async def setup(bot):
    await bot.add_cog(General(bot))
