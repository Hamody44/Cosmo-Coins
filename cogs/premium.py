import asyncio
import time
from decimal import Decimal
import discord
from discord.ext import commands
import database
from database import PRIMARY_OWNER_ID, check_is_owner, get_balance, parse_duration
from cogs.admin import parse_budget_payment_confirmation
from ltcpay import create_ltc_invoice, format_usd, LTCPayError
import premium_payments as pp

METHOD_LABELS = {"credit":"ProBot", "adamc":"AdamC", "cosmo":"رصيد Cosmo", "crypto":"LTC"}

def _manager(ctx):
    p = getattr(ctx.author, "guild_permissions", None)
    return ctx.author.id == PRIMARY_OWNER_ID or ctx.author.id == getattr(ctx.guild, "owner_id", 0) or bool(p and (p.administrator or p.manage_guild))

def _bool(value):
    value = str(value).strip().lower()
    if value in {"true", "1", "on", "yes", "فتح", "تفعيل"}: return True
    if value in {"false", "0", "off", "no", "قفل", "تعطيل"}: return False
    raise ValueError("boolean")

class PremiumSelect(discord.ui.Select):
    def __init__(self, cog, ctx, duration, price):
        self.cog, self.ctx, self.duration, self.price = cog, ctx, duration, price
        methods = [m for m in ("credit", "adamc", "cosmo", "crypto") if pp.method_enabled(m)]
        options = [discord.SelectOption(label=f"{METHOD_LABELS[m]} — {price[m] if m != 'crypto' else '$'+format(price[m], 'f')}", value=m) for m in methods]
        super().__init__(placeholder="اختر طريقة الدفع", options=options, min_values=1, max_values=1)
    async def callback(self, interaction):
        if interaction.user.id != self.ctx.author.id:
            return await interaction.response.send_message("هذه القائمة تخص صاحب الطلب فقط.", ephemeral=True)
        method = self.values[0]
        if not pp.purchase_enabled() or not pp.method_enabled(method):
            return await interaction.response.send_message("⛔ الشراء أو هذه الطريقة مغلقة حاليًا.", ephemeral=True)
        now = int(time.time()); seconds = self.price["duration_seconds"]
        expected = None if method == "crypto" else int(self.price[method])
        quote = self.price["crypto_usd"] if method == "crypto" else None
        result = await asyncio.to_thread(
            pp.create_order, interaction.guild_id, interaction.user.id, interaction.channel_id,
            method, self.duration, seconds, expected, quote, now, now + 600,
            getattr(interaction.user, "name", ""),
        )
        if not result: return await interaction.response.send_message("❌ لديك طلب Premium آخر معلّق أو تعذر إنشاء الطلب.", ephemeral=True)
        if method == "cosmo":
            result = await asyncio.to_thread(pp.settle_cosmo, interaction.guild_id, interaction.user.id, interaction.channel_id, result["order_id"])
            if not result: return await interaction.response.send_message("❌ رصيدك الشخصي غير كافٍ؛ لم يُخصم شيء.", ephemeral=True)
            return await interaction.response.edit_message(content=f"✅ تم تفعيل Premium حتى <t:{result['premium_end']}:F>.", view=None)
        if method == "crypto":
            try:
                invoice = await asyncio.to_thread(create_ltc_invoice, price_usd=quote, provider_order_id=result["provider_order_id"], discord_user_id=interaction.user.id, item_description=f"Cosmo Premium {self.duration}")
                if not await asyncio.to_thread(pp.attach_invoice, interaction.guild_id, result["order_id"], invoice): raise LTCPayError("تعذر ربط الفاتورة")
                return await interaction.response.edit_message(content=f"💎 ادفع فاتورة LTC لإتمام Premium لمدة `{self.duration}`:\n{invoice['pay_address']}\nتنتهي <t:{invoice['expires_at']}:R>", view=None)
            except Exception:
                await asyncio.to_thread(pp.fail_order, interaction.guild_id, result["order_id"])
                return await interaction.response.send_message("⚠️ تعذر إنشاء فاتورة LTC؛ لم يتم التفعيل.", ephemeral=True)
        command = f"!transfer {PRIMARY_OWNER_ID} {expected}"
        return await interaction.response.edit_message(content=f"💳 حوّل `{expected:,}` إلى مالك البوت عبر {METHOD_LABELS[method]} خلال 10 دقائق. يجب أن يطابق اسم الحساب والمبلغ والمستلم والوقت.", view=PremiumCommandView(interaction.user.id, command))

class PremiumCommandView(discord.ui.View):
    def __init__(self, user_id, command):
        super().__init__(timeout=600); self.user_id = user_id; self.command = command
    @discord.ui.button(label="نسخ أمر التحويل", style=discord.ButtonStyle.primary)
    async def copy(self, interaction, button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("هذا الزر لصاحب الطلب فقط.", ephemeral=True)
        await interaction.response.send_message(self.command, ephemeral=True)

class PremiumView(discord.ui.View):
    def __init__(self, cog, ctx, duration, price):
        super().__init__(timeout=120); self.add_item(PremiumSelect(cog, ctx, duration, price))

class Premium(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.hybrid_command(name="buy_premium", aliases=["premium_buy"], description="شراء Premium للسيرفر الحالي")
    async def buy_premium(self, ctx, duration: str):
        if not ctx.guild: return await ctx.send("⛔ استخدم الأمر داخل السيرفر.")
        if not pp.purchase_enabled(): return await ctx.send("⛔ شراء Premium مغلق حاليًا.")
        price = pp.get_price(duration)
        if not price: return await ctx.send("❌ هذه المدة غير مسعرة. استخدم مدة مثل `30d`.")
        methods = [m for m in ("credit", "adamc", "cosmo", "crypto") if pp.method_enabled(m)]
        if not methods: return await ctx.send("⛔ لا توجد طريقة دفع مفعلة.")
        await ctx.send(f"💎 اختر طريقة دفع Premium لمدة `{duration}` للسيرفر الحالي:", view=PremiumView(self, ctx, duration, price))

    @commands.command(name="set_premium_price")
    async def set_premium_price(self, ctx, duration: str, credit: int, adamc: int, cosmo: int, crypto_usd: str):
        if not await check_is_owner(self.bot, ctx): return await ctx.send("⛔ هذا الأمر متاح فقط لمالك البوت الأساسي.")
        try: seconds = parse_duration(duration); pp.set_price(duration, seconds, {"credit":credit,"adamc":adamc,"cosmo":cosmo,"crypto_usd":crypto_usd})
        except (ValueError, TypeError, Exception): return await ctx.send("❌ صيغة أو أسعار غير صالحة.")
        await ctx.send(f"✅ تم حفظ سعر Premium لمدة `{duration}`.")

    @commands.command(name="toggle_premium_purchase")
    async def toggle_premium_purchase(self, ctx, value: str):
        if not await check_is_owner(self.bot, ctx): return await ctx.send("⛔ هذا الأمر متاح فقط لمالك البوت الأساسي.")
        try: enabled = _bool(value)
        except ValueError: return await ctx.send("❌ استخدم true أو false.")
        pp.set_purchase_enabled(enabled); await ctx.send(f"✅ شراء Premium: {'مفتوح' if enabled else 'مغلق'}.")

    @commands.command(name="toggle_premium_method")
    async def toggle_premium_method(self, ctx, method: str, value: str):
        if not await check_is_owner(self.bot, ctx): return await ctx.send("⛔ هذا الأمر متاح فقط لمالك البوت الأساسي.")
        method = method.lower()
        try: enabled = _bool(value); pp.set_method_enabled(method, enabled)
        except (ValueError, TypeError): return await ctx.send("❌ الطريقة أو القيمة غير صالحة.")
        await ctx.send(f"✅ طريقة `{method}`: {'مفتوحة' if enabled else 'مغلقة'}.")

    @commands.Cog.listener()
    async def on_message(self, message):
        if not message.guild or not message.author.bot: return
        method = "credit" if message.author.id == database.PROBOT_PAYMENT_BOT_ID else "adamc" if message.author.id == database.ADAMC_PAYMENT_BOT_ID else None
        if not method: return
        parts = [message.content or ""] + [e.description or "" for e in message.embeds]
        receipt = parse_budget_payment_confirmation(method, message.author.id, "\n".join(parts))
        if not receipt or receipt["recipient_id"] != PRIMARY_OWNER_ID: return
        result = await asyncio.to_thread(pp.settle_receipt, message.guild.id, method, message.channel.id, receipt["sender"], receipt["amount"], message.id, message.author.id, int(message.created_at.timestamp()), receipt["recipient_id"])
        if result: await message.channel.send(f"✅ تم تفعيل Premium حتى <t:{result['premium_end']}:F>.", allowed_mentions=discord.AllowedMentions.none())

async def setup(bot): await bot.add_cog(Premium(bot))
