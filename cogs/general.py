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

    @discord.ui.button(label="الأوامر العامة 🌐", style=discord.ButtonStyle.primary, row=0)
    async def general_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            embed = discord.Embed(
                title="🌐 أوامر الأعضاء العامة",
                description="أوامر الرصيد، التحويل، الذكاء الاصطناعي، والخدمات المتاحة للجميع:",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="🪙 الاقتصاد والتبديل",
                value=(
                    "• `/balance [@user]`: عرض الرصيد.\n"
                    "• `/transfer @user <amount>`: تحويل رصيد مع كابتشا.\n"
                    "• `/exchange_list`: قائمة العملات المتاحة للتبديل.\n"
                    "• `/exchange <curr> <amount> [note]`: طلب تبديل كوزمو.\n"
                    "• `/transfer_history`: سجل آخر التحويلات.\n"
                    "• `/exchange_history`: سجل طلبات التبديل ورقم كل طلب.\n"
                    "• `/exchange_stats`: إحصائيات التبديل بالسيرفر."
                ),
                inline=False
            )
            embed.add_field(
                name="🤖 الذكاء الاصطناعي (Gemini)",
                value=(
                    "• `/ask <prompt>`: التحدث مع جيميناي بشكل طبيعي.\n"
                    "• `/reset_chat`: مسح سياق المحادثة وبدء حوار جديد.\n"
                    "• يمكنك أيضاً التحدث مباشرة بعمل **منشن للبوت** `@البوت`."
                ),
                inline=False
            )
            embed.add_field(
                name="⚙️ الخدمات والمعلومات",
                value=(
                    "• `/my_boost_status`: تفاصيل اشتراك ودفعات البوست.\n"
                    "• `/list_aliases`: قائمة الاختصارات المخصصة للسيرفر.\n"
                    "• `/timer <duration> [reason]`: ضبط مؤقت تنبيهي.\n"
                    "• `/ping`: فحص سرعة الاستجابة.\n"
                    "• `/server_count`: عرض عدد وإحصائيات السيرفرات."
                ),
                inline=False
            )
            embed.set_footer(text="Cosmo System • عام")
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            print(f"[Help General Button Error]: {e}")
            await interaction.followup.send(f"❌ حدث خطأ: {e}", ephemeral=True)

    @discord.ui.button(label="الأوامر الإدارية 🛡️", style=discord.ButtonStyle.danger, row=0)
    async def admin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            embed = discord.Embed(
                title="🛡️ أوامر الإدارة والمسؤولين",
                description="أوامر الفعاليات، التذاكر، الردود، وإعدادات السيرفر:",
                color=discord.Color.red()
            )
            embed.add_field(
                name="🎮 الفعاليات والشحن",
                value=(
                    "• `/add_balance @user <amount>`: شحن رصيد لعضو.\n"
                    "• `/remove_balance @user <amount>`: خصم رصيد من عضو.\n"
                    "• `/giveaway <amount> <duration> <winners> [cond]`: سحب جيف أواي بالريأكشن.\n"
                    "• `/box <amount> [cond]`: إطلاق بوكس سريع.\n"
                    "• `/roulette <amount> [cond]`: بدء لعبة الروليت بالعجلة.\n"
                    "• `/stop_roulette`: إيقاف الروليت الجارية.\n"
                    "• `/set_double_event <duration>`: تفعيل حدث الدبل ×2."
                ),
                inline=False
            )
            embed.add_field(
                name="🎫 التذاكر والرد التلقائي",
                value=(
                    "• `/setup_ticket`: إنشاء رسالة وزر التذاكر.\n"
                    "• `/rename <name>`: تغيير اسم التذكرة مع حفظها بالنظام.\n"
                    "• `/close`: إغلاق التذكرة وإخفاؤها عن العضو.\n"
                    "• `/delete`: حذف التذكرة وتفريغ الشات بملف txt.\n"
                    "• `/add_reply <trigger> <reply>`: إضافة رد تلقائي وحذف الرسالة.\n"
                    "• `/delete_reply <trigger>`: حذف رد تلقائي مسجل.\n"
                    "• `/list_replies`: استعراض الردود التلقائية المفعلة."
                ),
                inline=False
            )
            embed.add_field(
                name="🚀 إدارة التبديل والبوست واللوجات",
                value=(
                    "• `/accept_exchange <ID>` / `/deny_exchange <ID>`: قبول/رفض التبديل.\n"
                    "• `/setup_boost_rewards` / `/toggle_boost_system`: نظام البوست.\n"
                    "• `/fake_boost <1/2>`: تجربة واختبار نظام البوست.\n"
                    "• `/toggle_exchange_system` / `/toggle_currency_status`: قفل العملات.\n"
                    "• `/add_exchange_currency` / `/delete_exchange_currency`: إدارة العملات.\n"
                    "• `/add_alias <orig> <alias>`: تعيين اختصار لأي أمر.\n"
                    "• `/set_delivery_log` / `/set_ticket_log`: رومات التسليم والتذاكر.\n"
                    "• `/set_transfer_log` / `/set_balance_log`: رومات اللوج.\n"
                    "• `/set_success_log` / `/set_boost_log`: رومات الإعلانات.\n"
                    "• `/set_admin_role` / `/set_blacklist_role`: تعيين الرتب."
                ),
                inline=False
            )
            embed.set_footer(text="Cosmo System • الإدارة")
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            print(f"[Help Admin Button Error]: {e}")
            await interaction.followup.send(f"❌ حدث خطأ: {e}", ephemeral=True)

    @discord.ui.button(label="البريميوم والمالك 👑", style=discord.ButtonStyle.secondary, row=1)
    async def owner_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            embed = discord.Embed(
                title="👑 أوامر البريميوم والمالك الأساسي",
                description="ميزات السيرفرات المفعلة وأدوات مالك ومطور البوت:",
                color=discord.Color.gold()
            )
            embed.add_field(
                name="💎 ميزات سيرفرات البريميوم",
                value=(
                    "• `/active_premium <code>`: تفعيل كود البريميوم للسيرفر.\n"
                    "• `/premium_status`: تفاصيل الاشتراك والوقت المتبقي.\n"
                    "• `/server_budget`: معرفة ميزانية الصرف المتبقية.\n"
                    "• `/server_budget_history`: سجل آخر عمليات شحن وخصم الميزانية.\n"
                    "• `/set_bot_name <name>`: تخصيص اسم البوت بالسيرفر.\n"
                    "• `/set_bot_avatar <file>`: تخصيص صورة البوت بالسيرفر.\n"
                    "• `/set_bot_banner <file>`: تخصيص بنر البوت بالسيرفر."
                ),
                inline=False
            )
            embed.add_field(
                name="🔒 أدوات مالك البوت فقط",
                value=(
                    "• `/add_server_budget <amount>`: شحن ميزانية السيرفر تراكمياً.\n"
                    "• `/remove_server_budget <amount>`: خصم مبلغ من ميزانية السيرفر.\n"
                    "• `/set_global_log #channel`: تحديد روم اللوج العام الموحد للشحن والتحويل.\n"
                    "• `/disable_global_log`: تعطيل اللوج العام الموحد.\n"
                    "• `/premium_code <duration>`: توليد كود بريميوم 14 حرف.\n"
                    "• `/set_global_request_log #channel`: روم طلبات التبديل العام.\n"
                    "• `/allow_bot <id> #channel` / `/disallow_bot <id>`: تصريح البوتات.\n"
                    "• `/auto_backup`: إرسال نسخة احتياطية فورية لخاص المالك.\n"
                    "• `/leave_guild <id>`: إخراج البوت من سيرفر معين."
                ),
                inline=False
            )
            embed.set_footer(text="Cosmo System • المالك والبريميوم")
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            print(f"[Help Owner Button Error]: {e}")
            await interaction.followup.send(f"❌ حدث خطأ: {e}", ephemeral=True)

class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="help", aliases=["مساعدة"], description="عرض قائمة أوامر البوت مقسمة بالأزرار")
    async def help_command(self, ctx: commands.Context):
        embed = discord.Embed(
            title="✨ قائمة أوامر بوت Cosmo",
            description=(
                "مرحباً بك! اختر القسم المطلوب عبر الأزرار أدناه:\n\n"
                "🔹 **الأوامر العامة 🌐:** أوامر الأعضاء، الرصيد، التبديل، وجيميناي.\n"
                "🔸 **الأوامر الإدارية 🛡️:** الفعاليات، التذاكر، الردود، واللوجات.\n"
                "👑 **البريميوم والمالك 👑:** ميزات البريميوم، تخصيص البنر والاسم، الميزانية، واللوج العام."
            ),
            color=discord.Color.purple()
        )
        embed.set_footer(text="Cosmo Currency System • يدعم السلاش / والبادئة !")
        view = HelpView(ctx.author)
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="ping", aliases=["بنق"], description="فحص سرعة استجابة البوت")
    async def ping(self, ctx: commands.Context):
        latency = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 بونق! سرعة الاستجابة: `{latency}ms`")

    @commands.hybrid_command(name="server_count", aliases=["عدد_السيرفرات"], description="عرض قائمة السيرفرات المتواجد بها البوت وروابطها (مالك البوت)")
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
            except:
                pass

            if not invite_link:
                for channel in guild.text_channels:
                    if channel.permissions_for(guild.me).create_instant_invite:
                        try:
                            new_inv = await channel.create_invite(max_age=0, max_uses=0, reason="Server List Inspection")
                            invite_link = new_inv.url
                            break
                        except:
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
