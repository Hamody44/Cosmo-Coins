"""Premium-only advertising tickets: configuration and order intake foundation."""
import datetime
import re
import time

import discord
from discord.ext import commands

from advertising_store import (
    AD_DURATIONS,
    add_ad_channel,
    count_published_today,
    create_ad_order,
    create_ad_payment,
    ensure_ad_schema,
    get_ad_payment,
    get_ad_config,
    get_ad_order,
    remove_ad_channel,
    set_ad_config,
    set_ad_price,
    settle_ad_payment,
    submit_ad_content,
    update_ad_order,
)
from database import ADAMC_PAYMENT_BOT_ID, PROBOT_PAYMENT_BOT_ID, get_guild_db, get_guild_management_role_ids, is_guild_premium, parse_amount
from cogs.admin import parse_budget_payment_confirmation
from cogs.tickets import can_manage_ticket_member


def can_manage_ads(ctx: commands.Context) -> bool:
    if not ctx.guild or not ctx.author:
        return False
    owner_role, admin_role = get_guild_management_role_ids(ctx.guild.id)
    return can_manage_ticket_member(ctx.guild, ctx.author, admin_role, owner_role)


def premium_required(ctx):
    return bool(ctx.guild and is_guild_premium(ctx.guild.id))


def local_day_start() -> int:
    now = datetime.datetime.now()
    return int(datetime.datetime(now.year, now.month, now.day).timestamp())


def validate_ad_content(content: str):
    content = " ".join((content or "").split())
    if len(content) < 10 or len(content) > 2000:
        return False, "محتوى الإعلان يجب أن يكون بين 10 و2000 حرف."
    if re.search(r"@(everyone|here)|<@!?\d+>|<@&\d+>", content, re.IGNORECASE):
        return False, "يمنع وضع منشن يدوي داخل الإعلان؛ سيضيف البوت المنشن المسموح تلقائيًا."
    if not re.search(r"https?://(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", content, re.IGNORECASE):
        return False, "يجب أن يحتوي الإعلان على رابط دعوة Discord صالح."
    return True, content


class AdTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="فتح تذكرة إعلان", style=discord.ButtonStyle.primary, emoji="📣", custom_id="premium_ad_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not is_guild_premium(interaction.guild.id):
            return await interaction.response.send_message("⛔ نظام تذاكر الإعلانات متاح لسيرفرات Premium فقط.", ephemeral=True)
        await interaction.response.send_message("استخدم أمر `ad_ticket` مع المدة وقيمة الجيف لفتح تذكرة إعلان.", ephemeral=True)


class Advertisements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(AdTicketView())

    @commands.hybrid_command(name="setup_ad_panel", aliases=["لوحة_الإعلانات"], description="إرسال لوحة فتح تذاكر الإعلانات Premium")
    async def setup_ad_panel(self, ctx: commands.Context):
        if not premium_required(ctx):
            return await ctx.send("⛔ نظام تذاكر الإعلانات متاح لسيرفرات Premium فقط.")
        if not can_manage_ads(ctx):
            return await ctx.send("⛔ مالك السيرفر أو الإدارة المخولة فقط يمكنهم إعداد لوحة الإعلانات.")
        embed = discord.Embed(
            title="📣 إعلانات السيرفر",
            description=("هذا النظام متاح لسيرفرات Premium فقط.\n\n"
                         "افتح تذكرة عبر الأمر:\n"
                         "`ad_ticket <1d|2d|3d|5d|7d> <قيمة_الجيف> [عدد_الفائزين] [here|everyone]`\n\n"
                         "مثال: `ad_ticket 3d 5000000 1 here`"),
            color=discord.Color.gold(),
        )
        await ctx.send(embed=embed, view=AdTicketView())

    @commands.hybrid_command(name="set_ad_price", aliases=["سعر_الإعلان"], description="تحديد سعر الإعلان حسب المدة")
    async def set_ad_price_command(self, ctx: commands.Context, duration: str, price: str):
        if not premium_required(ctx):
            return await ctx.send("⛔ إعدادات الإعلانات متاحة لسيرفرات Premium فقط.")
        if not can_manage_ads(ctx):
            return await ctx.send("⛔ مالك السيرفر أو الإدارة المخولة فقط.")
        duration = duration.strip().lower()
        if duration not in AD_DURATIONS:
            return await ctx.send("❌ المدة يجب أن تكون واحدة من: `1d`, `2d`, `3d`, `5d`, `7d`.")
        try:
            value = parse_amount(price)
        except Exception:
            return await ctx.send("❌ سعر الإعلان غير صحيح.")
        if value <= 0:
            return await ctx.send("❌ سعر الإعلان يجب أن يكون أكبر من صفر.")
        set_ad_price(ctx.guild.id, duration, value)
        await ctx.send(f"✅ تم تحديد سعر إعلان مدة `{duration}` على `{value:,}` Cosmo. قيمة الجيف منفصلة.")

    @commands.hybrid_command(name="set_ad_daily_limit", aliases=["حد_إعلانات_اليوم"], description="تحديد الحد اليومي للإعلانات")
    async def set_ad_daily_limit(self, ctx: commands.Context, limit: int):
        if not premium_required(ctx):
            return await ctx.send("⛔ إعدادات الإعلانات متاحة لسيرفرات Premium فقط.")
        if not can_manage_ads(ctx):
            return await ctx.send("⛔ مالك السيرفر أو الإدارة المخولة فقط.")
        if limit <= 0 or limit > 100:
            return await ctx.send("❌ الحد اليومي يجب أن يكون بين 1 و100.")
        set_ad_config(ctx.guild.id, daily_limit=limit)
        await ctx.send(f"✅ تم ضبط الحد اليومي العام للإعلانات على `{limit}` إعلانًا.")

    @commands.hybrid_command(name="set_everyone_fee", aliases=["سعر_إيفري_وان"], description="تحديد الرسوم الإضافية لمنشن everyone")
    async def set_everyone_fee(self, ctx: commands.Context, fee: str):
        if not premium_required(ctx):
            return await ctx.send("⛔ إعدادات الإعلانات متاحة لسيرفرات Premium فقط.")
        if not can_manage_ads(ctx):
            return await ctx.send("⛔ مالك السيرفر أو الإدارة المخولة فقط.")
        try:
            value = parse_amount(fee)
        except Exception:
            return await ctx.send("❌ الرسوم غير صحيحة.")
        if value < 0:
            return await ctx.send("❌ الرسوم لا يمكن أن تكون سالبة.")
        set_ad_config(ctx.guild.id, everyone_fee=value)
        await ctx.send(f"✅ رسوم `everyone` أصبحت `{value:,}` Cosmo فوق سعر الإعلان.")

    @commands.hybrid_command(name="set_ad_channel", aliases=["روم_إعلان"], description="إضافة روم مسموح للإعلانات")
    async def set_ad_channel(self, ctx: commands.Context, channel: discord.TextChannel):
        if not premium_required(ctx):
            return await ctx.send("⛔ إعدادات الإعلانات متاحة لسيرفرات Premium فقط.")
        if not can_manage_ads(ctx):
            return await ctx.send("⛔ مالك السيرفر أو الإدارة المخولة فقط.")
        if channel.guild.id != ctx.guild.id:
            return await ctx.send("❌ يجب اختيار روم من نفس السيرفر.")
        add_ad_channel(ctx.guild.id, channel.id)
        await ctx.send(f"✅ تمت إضافة {channel.mention} إلى رومات الإعلانات المسموحة.")

    @commands.hybrid_command(name="remove_ad_channel", aliases=["حذف_روم_إعلان"], description="حذف روم من رومات الإعلانات")
    async def remove_ad_channel_command(self, ctx: commands.Context, channel: discord.TextChannel):
        if not premium_required(ctx):
            return await ctx.send("⛔ إعدادات الإعلانات متاحة لسيرفرات Premium فقط.")
        if not can_manage_ads(ctx):
            return await ctx.send("⛔ مالك السيرفر أو الإدارة المخولة فقط.")
        remove_ad_channel(ctx.guild.id, channel.id)
        await ctx.send(f"✅ تمت إزالة {channel.mention} من رومات الإعلانات المسموحة.")

    @commands.hybrid_command(name="ad_channels", aliases=["رومات_الإعلانات"], description="عرض رومات الإعلانات المسموحة والإعدادات")
    async def ad_channels(self, ctx: commands.Context):
        if not premium_required(ctx):
            return await ctx.send("⛔ نظام الإعلانات متاح لسيرفرات Premium فقط.")
        config = get_ad_config(ctx.guild.id)
        channels = [f"<#{channel_id}>" for channel_id in config["channels"]]
        prices = "\n".join(f"• `{duration}`: `{price:,}`" for duration, price in config["prices"].items()) or "لم يتم تحديد أسعار بعد"
        await ctx.send("**إعدادات الإعلانات**\n"
                       f"• الحد اليومي: `{config['daily_limit']}`\n"
                       f"• رسوم everyone: `{config['everyone_fee']:,}`\n"
                       f"• الرومات: {', '.join(channels) if channels else 'لم يتم تحديدها'}\n"
                       f"• الأسعار:\n{prices}")

    @commands.hybrid_command(name="ad_ticket", aliases=["تذكرة_إعلان"], description="فتح تذكرة إعلان Premium")
    async def ad_ticket(self, ctx: commands.Context, duration: str, giveaway: str, winners: int = 1, mention: str = "here"):
        if not premium_required(ctx):
            return await ctx.send("⛔ نظام تذاكر الإعلانات متاح لسيرفرات Premium فقط.")
        if not ctx.guild:
            return await ctx.send("❌ استخدم الأمر داخل السيرفر.")
        duration = duration.strip().lower()
        mention = mention.strip().lower()
        if duration not in AD_DURATIONS or mention not in {"here", "everyone"}:
            return await ctx.send("❌ استخدم مدة صحيحة (`1d`, `2d`, `3d`, `5d`, `7d`) ومنشن `here` أو `everyone`.")
        try:
            giveaway_total = parse_amount(giveaway)
        except Exception:
            return await ctx.send("❌ قيمة الجيف غير صحيحة.")
        if giveaway_total < 5_000_000:
            return await ctx.send("❌ الحد الأدنى لقيمة الجيف هو `5m`.")
        if giveaway_total >= 100_000_000 and winners > 1 and giveaway_total // winners < 5_000_000:
            return await ctx.send("❌ نصيب كل فائز يجب ألا يقل عن `5m`.")
        if winners <= 0 or (winners > 1 and giveaway_total < 100_000_000):
            return await ctx.send("❌ يسمح بأكثر من فائز فقط عندما تكون قيمة الجيف `100m` أو أكثر.")
        config = get_ad_config(ctx.guild.id)
        ad_price = int(config["prices"].get(duration, 0))
        if ad_price <= 0:
            return await ctx.send(f"❌ لم يحدد صاحب السيرفر سعر إعلان مدة `{duration}` بعد.")
        if mention == "everyone":
            ad_price += int(config["everyone_fee"])
        existing = get_guild_db(ctx.guild.id)
        conn, cur = existing
        try:
            row = cur.execute("SELECT channel_id FROM tickets WHERE user_id=? AND is_closed=0", (ctx.author.id,)).fetchone()
            if row and ctx.guild.get_channel(row[0]):
                return await ctx.send(f"⚠️ لديك تذكرة مفتوحة بالفعل: <#{row[0]}>")
        finally:
            conn.close()
        category = ctx.channel.category
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
            ctx.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        owner_role, admin_role = get_guild_management_role_ids(ctx.guild.id)
        for role_id in (owner_role, admin_role):
            role = ctx.guild.get_role(role_id) if role_id else None
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        await ctx.defer()
        channel = await ctx.guild.create_text_channel(f"ad-{ctx.author.name[:12]}", category=category, overwrites=overwrites, reason="Premium advertising ticket")
        now_text = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            ensure_ad_schema(ctx.guild.id)
            conn, cur = get_guild_db(ctx.guild.id)
            cur.execute("INSERT INTO tickets(channel_id,user_id,opened_at,is_closed) VALUES(?,?,?,0)", (channel.id, ctx.author.id, now_text))
            conn.commit()
            conn.close()
            day_limit = config["daily_limit"]
            queued = count_published_today(ctx.guild.id, local_day_start()) >= day_limit
            order_id = create_ad_order(ctx.guild.id, ctx.author.id, channel.id, duration, ad_price, giveaway_total, winners, mention, queued=queued)
        except Exception:
            try:
                await channel.delete(reason="Advertising order registration failed")
            except Exception:
                pass
            raise
        state = "قائمة الانتظار" if queued else "مسودة بانتظار إدخال محتوى الإعلان والدفع"
        await channel.send(f"📣 **تذكرة إعلان #{order_id}**\n"
                           f"• المدة: `{duration}`\n• سعر الإعلان: `{ad_price:,}` Cosmo\n"
                           f"• إجمالي الجيف: `{giveaway_total:,}` Cosmo\n• عدد الفائزين: `{winners}`\n"
                           f"• المنشن: `{mention}`\n• الحالة: **{state}**\n\n"
                           "أرسل محتوى الإعلان ورابط سيرفر Discord داخل التذكرة. يمنع إدخال أي منشن يدويًا؛ سيضيفه البوت بعد المراجعة.")
        await ctx.followup.send(f"✅ تم إنشاء تذكرة الإعلان: {channel.mention}" + ("\n📋 تم وضع الطلب في قائمة الانتظار." if queued else ""))

    @commands.hybrid_command(name="ad_submit", aliases=["إرسال_الإعلان"], description="إرسال محتوى إعلانك للمراجعة")
    async def ad_submit(self, ctx: commands.Context, order_id: int, title: str, *, content: str):
        if not premium_required(ctx):
            return await ctx.send("⛔ نظام الإعلانات متاح لسيرفرات Premium فقط.")
        order = get_ad_order(ctx.guild.id, order_id)
        if not order or order["ticket_channel_id"] != ctx.channel.id or order["advertiser_id"] != ctx.author.id:
            return await ctx.send("❌ هذا الطلب غير موجود أو لا ينتمي إلى هذه التذكرة.")
        valid, result = validate_ad_content(content)
        if not valid:
            return await ctx.send(f"❌ {result}")
        if not title.strip() or len(title.strip()) > 120:
            return await ctx.send("❌ عنوان الإعلان يجب أن يكون بين 1 و120 حرفًا.")
        owner_id = int(ctx.guild.owner_id)
        saved = submit_ad_content(ctx.guild.id, order_id, ctx.author.id, title.strip(), result, None, owner_id)
        if not saved:
            return await ctx.send("❌ لا يمكن تعديل هذا الطلب في حالته الحالية.")
        # صاحب السيرفر يستطيع الإعلان دون دفع رسم الإعلان أو قيمة الجيف؛ سيتم
        # حجز الجيف من ميزانية السيرفر عند النشر فقط.
        if ctx.author.id == owner_id:
            update_ad_order(ctx.guild.id, order_id, status="paid")
            return await ctx.send("✅ تم حفظ الإعلان. أنت صاحب السيرفر، لذلك لا يوجد دفع؛ ستُحجز قيمة الجيف من ميزانية السيرفر عند النشر.")
        expected = int(order["ad_price"]) + int(order["giveaway_total"])
        await ctx.send(f"✅ تم حفظ الإعلان. إجمالي الدفع المطلوب هو `{expected:,}` Cosmo.\n"
                       "استخدم `ad_pay credit` أو `ad_pay adamc` لاختيار طريقة الدفع.")

    @commands.hybrid_command(name="ad_pay", aliases=["دفع_الإعلان"], description="اختيار طريقة دفع الإعلان")
    async def ad_pay(self, ctx: commands.Context, method: str, order_id: int = None):
        if not premium_required(ctx):
            return await ctx.send("⛔ نظام الإعلانات متاح لسيرفرات Premium فقط.")
        if order_id is None:
            conn, cur = get_guild_db(ctx.guild.id)
            row = cur.execute("SELECT id FROM ad_orders WHERE ticket_channel_id=? AND advertiser_id=? ORDER BY id DESC LIMIT 1", (ctx.channel.id, ctx.author.id)).fetchone()
            conn.close()
            order_id = row[0] if row else None
        order = get_ad_order(ctx.guild.id, order_id) if order_id else None
        method = method.strip().lower()
        if not order or order["advertiser_id"] != ctx.author.id or order["ticket_channel_id"] != ctx.channel.id:
            return await ctx.send("❌ لم أجد طلب إعلان صالحًا داخل هذه التذكرة.")
        if order["advertiser_id"] == ctx.guild.owner_id:
            return await ctx.send("✅ صاحب السيرفر لا يحتاج إلى دفع؛ سيتم الحجز من ميزانية السيرفر عند النشر.")
        if method not in {"credit", "adamc"}:
            return await ctx.send("❌ طريقة الدفع يجب أن تكون `credit` أو `adamc`.")
        if order["status"] not in {"awaiting_payment", "queued"}:
            return await ctx.send("❌ يجب إرسال محتوى الإعلان أولًا، أو أن حالة الطلب لا تسمح بالدفع الآن.")
        expected = int(order["ad_price"]) + int(order["giveaway_total"])
        create_ad_payment(ctx.guild.id, order_id, method, expected)
        owner_id = int(ctx.guild.owner_id)
        command = f"#credit {owner_id} {expected} with tax" if method == "credit" else f"!transfer {owner_id} {expected}"
        await ctx.send(f"💳 حوّل `{expected:,}` إلى صاحب السيرفر <@{owner_id}> باستخدام:\n```{command}```\nثم انتظر إيصال البوت داخل هذه التذكرة.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        ensure_ad_schema(message.guild.id)
        conn, cur = get_guild_db(message.guild.id)
        row = cur.execute("SELECT id,advertiser_id,server_owner_id,status FROM ad_orders WHERE ticket_channel_id=? ORDER BY id DESC LIMIT 1", (message.channel.id,)).fetchone()
        conn.close()
        if not row or row[3] not in {"awaiting_payment", "queued"}:
            return
        order_id, advertiser_id, owner_id, status = row
        payment = get_ad_payment(message.guild.id, order_id)
        if not payment or payment["status"] != "pending" or message.author.id not in {PROBOT_PAYMENT_BOT_ID, ADAMC_PAYMENT_BOT_ID}:
            return
        method = payment["method"]
        if (method == "credit" and message.author.id != PROBOT_PAYMENT_BOT_ID) or (method == "adamc" and message.author.id != ADAMC_PAYMENT_BOT_ID):
            return
        parts = [message.content]
        for embed in message.embeds:
            parts.append(embed.description or "")
            parts.extend(field.value for field in embed.fields)
        receipt = parse_budget_payment_confirmation(method, message.author.id, "\n".join(parts))
        if not receipt or receipt["recipient_id"] != int(owner_id) or receipt["amount"] < int(payment["expected_amount"]):
            return
        result = settle_ad_payment(message.guild.id, order_id, message.id, message.author.id, receipt["amount"])
        if result["status"] == "paid":
            await message.channel.send(f"✅ تم تأكيد دفع الإعلان #{order_id}. سيتم نقله للمراجعة والنشر تلقائيًا.")

    @commands.hybrid_command(name="ad_status", aliases=["حالة_الإعلان"], description="عرض حالة طلب إعلان")
    async def ad_status(self, ctx: commands.Context, order_id: int):
        if not premium_required(ctx):
            return await ctx.send("⛔ نظام الإعلانات متاح لسيرفرات Premium فقط.")
        order = get_ad_order(ctx.guild.id, order_id)
        if not order or (order["advertiser_id"] != ctx.author.id and not can_manage_ads(ctx)):
            return await ctx.send("❌ لم أجد طلب الإعلان أو لا تملك صلاحية رؤيته.")
        await ctx.send(f"📣 الإعلان `#{order_id}`\n• الحالة: `{order['status']}`\n• المدة: `{order['duration_key']}`\n• ترتيب القائمة: `{order['queue_rank'] or 'غير موجود'}`\n• سعر الإعلان: `{order['ad_price']:,}`\n• الجيف: `{order['giveaway_total']:,}`")


async def setup(bot):
    await bot.add_cog(Advertisements(bot))
