import discord
from discord.ext import commands, tasks
import shutil
import os
import time
import uuid
import string
import random
import asyncio
import base64
import math
import re
import unicodedata
from datetime import datetime
from database import (
    check_is_owner, is_bot_admin, is_guild_premium, parse_duration,
    parse_amount, get_guild_db, global_cursor, global_conn, PRIMARY_OWNER_ID,
    deduct_server_spending, update_balance, debit_balance, get_balance, is_blacklisted, get_server_spending_balance,
    send_global_action_log, COSMO_GUILD_ID, get_currency_label,
    set_exchange_currency_status, upsert_exchange_currency, delete_exchange_currency,
    create_budget_payment_order, settle_budget_payment, is_auto_budget_payment_enabled,
    create_crypto_budget_payment_order, update_crypto_budget_payment_order,
    fail_crypto_budget_payment_order, transfer_balance_to_server_budget,
    set_auto_budget_payment_enabled, set_global_exchange_enabled,
    PROBOT_PAYMENT_BOT_ID, ADAMC_PAYMENT_BOT_ID
)
from nowpayments import (
    FIXED_RATE_TTL_SECONDS, NowPaymentsError, UnderMinimumPayment, amount_to_litoshi,
    create_ltc_payment, format_ltc, format_usd, is_configured as nowpayments_is_configured,
    quote_ltc_amount, usd_for_budget,
)

PROBOT_ID = PROBOT_PAYMENT_BOT_ID
ADAMC_ID = ADAMC_PAYMENT_BOT_ID
AUTO_BUDGET_PAYMENT_TTL = 5 * 60
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807

def parse_budget_payment_confirmation(method: str, author_id: int, text: str):
    """Parse only the user-provided receipt shape; caller must also validate message guild/channel/time."""
    method = method.strip().lower()
    expected_author = {"credit": PROBOT_ID, "adamc": ADAMC_ID}.get(method)
    if not expected_author or int(author_id) != expected_author:
        return None
    if method == "credit":
        patterns = [
            re.compile(
                r"(?:\*\*\s*)?ـ\s*(?P<sender>[^,*\n]+?)\s*,\s*قام\s+بتحويل\s+`?\$?(?P<amount>[\d,]+)`?\s+لـ\s*<@!?(?P<recipient>\d+)>"
            ),
            re.compile(
                r"(?:\*\*\s*)?(?::moneybag:|💰)\s*\|\s*\*{0,2}(?P<sender>[^,*\n]+?)\*{0,2}\s*,\s*has transferred\s+\*{0,2}`?\$?(?P<amount>[\d,]+)`?\*{0,2}\s+to\s+<@!?(?P<recipient>\d+)>",
                re.IGNORECASE,
            ),
        ]
    else:
        patterns = [re.compile(
            r"💰\s*\|\s*\*\*(?P<sender>[^*\n]+?)\*\*,\s*has transferred\s+\*\*\$?(?P<amount>[\d,]+)\*\*\s+to\s+<@!?(?P<recipient>\d+)>",
            re.IGNORECASE,
        )]
    match = None
    for pattern in patterns:
        match = pattern.search(text or "")
        if match:
            break
    if not match:
        return None
    try:
        amount = int(match.group("amount").replace(",", ""))
        recipient_id = int(match.group("recipient"))
    except (TypeError, ValueError):
        return None
    sender = unicodedata.normalize("NFKC", match.group("sender")).strip().casefold()
    if not sender or amount <= 0:
        return None
    return {"sender": sender, "amount": amount, "recipient_id": recipient_id}

class AutoBudgetPaymentSelect(discord.ui.Select):
    def __init__(self, bot, user_id: int, budget_amount: int, methods: list[str]):
        self.bot = bot
        self.user_id = user_id
        self.budget_amount = budget_amount
        labels = {
            "credit": ("Credits عبر ProBot", "credit"),
            "adamc": ("AdamC", "adamc"),
            "crypto": ("كريبتو (LTC)", "crypto"),
            "cosmo": ("رصيد Cosmo الشخصي", "cosmo"),
        }
        options = [discord.SelectOption(label=labels[m][0], value=labels[m][1]) for m in methods]
        super().__init__(placeholder="اختر وسيلة الدفع", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("هذه القائمة تخص الشخص الذي بدأ الطلب فقط.", ephemeral=True)

        method = self.values[0]
        if method != "cosmo" and not is_auto_budget_payment_enabled(method):
            return await interaction.response.send_message("⛔ وسيلة الدفع هذه مغلقة حاليًا. شغّل أمر الشراء من جديد بعد تفعيلها.", ephemeral=True)

        if method == "cosmo":
            await interaction.response.defer(ephemeral=True)
            try:
                result = await asyncio.to_thread(
                    transfer_balance_to_server_budget,
                    interaction.user.id,
                    interaction.guild_id,
                    self.budget_amount,
                )
            except Exception as exc:
                print(f"Personal Cosmo budget transfer failed: {exc}")
                await interaction.followup.send(
                    "⚠️ تعذر تنفيذ التحويل الذري؛ لم يُخصم أي رصيد. أبلغ مالك البوت إذا تكرر الخطأ.",
                    ephemeral=True,
                )
                try:
                    await interaction.message.edit(content="تعذر تنفيذ طلب التحويل؛ لم تُخصم ميزانية ولم يُضف شيء.", view=None)
                except discord.HTTPException:
                    pass
                return
            if result is None:
                await interaction.followup.send(
                    "❌ رصيد Cosmo الشخصي غير كافٍ أو تعذر العثور عليه؛ لم تُخصم أي عملة.",
                    ephemeral=True,
                )
                try:
                    await interaction.message.edit(content="لم يكتمل التحويل لعدم كفاية الرصيد الشخصي.", view=None)
                except discord.HTTPException:
                    pass
                return
            label = get_currency_label(interaction.guild_id)
            await interaction.followup.send(
                f"✅ تم تحويل `{result['amount']:,}` Cosmo من رصيدك الشخصي إلى ميزانية **{interaction.guild.name}**.\n"
                f"• الميزانية: `{result['old_budget']:,}` ← `{result['new_budget']:,}` {label}\n"
                f"• رصيدك الشخصي المتبقي: `{result['new_personal_balance']:,}` Cosmo",
                ephemeral=True,
            )
            try:
                await interaction.message.edit(content="✅ اكتمل تحويل رصيد Cosmo الشخصي إلى ميزانية السيرفر.", view=None)
            except discord.HTTPException:
                pass
            return

        if method == "crypto":
            dashboard_url = os.getenv("DASHBOARD_URL", "").strip().rstrip("/")
            if not nowpayments_is_configured() or not dashboard_url.startswith("https://"):
                return await interaction.response.send_message(
                    "⛔ تكامل NOWPayments غير مكتمل الإعداد؛ لم يُنشأ طلب ولم تُضاف ميزانية.",
                    ephemeral=True,
                )
            await interaction.response.defer(ephemeral=True)
            price_usd = usd_for_budget(self.budget_amount)
            now = int(time.time())
            provider_order_id = f"cosmo:{interaction.guild_id}:{uuid.uuid4().hex}"
            try:
                estimated_ltc = await asyncio.to_thread(quote_ltc_amount, price_usd)
                expected_litoshi = amount_to_litoshi(estimated_ltc, round_up=True)
                order_id = await asyncio.to_thread(
                    create_crypto_budget_payment_order,
                    interaction.guild_id,
                    interaction.user.id,
                    unicodedata.normalize("NFKC", interaction.user.name).strip(),
                    interaction.channel_id,
                    self.budget_amount,
                    expected_litoshi,
                    format(price_usd, "f"),
                    provider_order_id,
                    now,
                    now + FIXED_RATE_TTL_SECONDS,
                )
                if order_id is None:
                    raise NowPaymentsError("لديك طلب شحن آخر قيد الإنشاء أو الانتظار في هذا السيرفر، أو أُغلقت وسيلة LTC.")
                payment = await asyncio.to_thread(
                    create_ltc_payment,
                    price_usd=price_usd,
                    estimated_ltc=estimated_ltc,
                    provider_order_id=provider_order_id,
                    callback_url=f"{dashboard_url}/nowpayments/ipn",
                )
                saved = await asyncio.to_thread(
                    update_crypto_budget_payment_order,
                    interaction.guild_id,
                    order_id,
                    provider_order_id,
                    payment,
                )
                if not saved:
                    raise NowPaymentsError("تعذر ربط فاتورة المزود بطلب الميزانية؛ لن نعرض عنوان الدفع لتجنب دفع غير قابل للتسوية.")
            except UnderMinimumPayment as exc:
                await interaction.followup.send(
                    f"❌ مبلغ الميزانية أصغر من الحد الأدنى الحالي لـ NOWPayments. {exc} ارفع مبلغ Cosmo ثم أعد المحاولة.",
                    ephemeral=True,
                )
                try:
                    await interaction.message.edit(content="لم يُنشأ طلب LTC لأن المبلغ دون الحد الأدنى للمزود.", view=None)
                except discord.HTTPException:
                    pass
                return
            except Exception as exc:
                if "order_id" in locals() and order_id is not None:
                    try:
                        await asyncio.to_thread(
                            fail_crypto_budget_payment_order,
                            interaction.guild_id,
                            order_id,
                            provider_order_id,
                        )
                    except Exception:
                        pass
                print(f"NOWPayments order creation failed: {exc}")
                message = str(exc) if isinstance(exc, NowPaymentsError) else "تعذر إنشاء طلب LTC الآن. لم تُعتمد أي ميزانية؛ حاول لاحقًا أو تواصل مع مالك البوت."
                await interaction.followup.send(f"⚠️ {message}", ephemeral=True)
                try:
                    await interaction.message.edit(content="تعذر إنشاء طلب LTC؛ لم تُضف ميزانية. يمكنك إعادة المحاولة لاحقًا.", view=None)
                except discord.HTTPException:
                    pass
                return

            expires_at = int(payment["expires_at"])
            pay_amount = format_ltc(payment["pay_amount_litoshi"])
            message = (
                f"💸 **طلب LTC #{order_id} — NOWPayments**\n"
                f"• الميزانية: `{self.budget_amount:,}` Cosmo\n"
                f"• السعر حسب 1 USD = 260,000,000 Cosmo: **${format_usd(price_usd)}**\n"
                f"• أرسل بالضبط: **{pay_amount} LTC**\n"
                f"• عنوان الإيداع: `{payment['pay_address']}`\n"
                "⚠️ أرسل **Litecoin (LTC) على شبكة Litecoin فقط** إلى عنوان هذا الطلب؛ لا ترسل أي عملة أو شبكة أخرى. "
                f"السعر الثابت صالح حتى <t:{expires_at}:R> (10 دقائق)، ولا تُضاف الميزانية إلا بعد تأكيد الحالة `finished` من NOWPayments."
            )
            await interaction.followup.send(message, ephemeral=True)
            try:
                await interaction.message.edit(content="✅ أُرسلت تفاصيل طلب LTC بشكل خاص إلى صاحب الطلب.", view=None)
            except discord.HTTPException:
                pass
            return

        if self.budget_amount % 5 != 0:
            return await interaction.response.send_message(
                "للحفاظ على سعر صرف صحيح بلا كسور، يجب أن تكون ميزانية الشحن من مضاعفات 5. ابدأ الأمر بمبلغ مناسب.",
                ephemeral=True,
            )

        if method == "credit":
            expected_payment = self.budget_amount * 6 // 5
            if expected_payment > MAX_SQLITE_INTEGER:
                return await interaction.response.send_message("المبلغ كبير جدًا ولا يمكن معالجته بأمان.", ephemeral=True)
            command = f"#credit {PRIMARY_OWNER_ID} {expected_payment} with tax"
            method_name = "ProBot Credits"
        else:
            expected_payment = self.budget_amount // 5
            command = f"!transfer {PRIMARY_OWNER_ID} {expected_payment}"
            method_name = "AdamC"

        now = int(time.time())
        expires_at = now + AUTO_BUDGET_PAYMENT_TTL
        payer_name = unicodedata.normalize("NFKC", interaction.user.name).strip().casefold()
        try:
            order_id = create_budget_payment_order(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                payer_name=payer_name,
                channel_id=interaction.channel_id,
                method=method,
                budget_amount=self.budget_amount,
                expected_payment=expected_payment,
                created_at=now,
                expires_at=expires_at,
            )
        except Exception as exc:
            print(f"Auto budget order creation failed: {exc}")
            return await interaction.response.send_message("تعذر إنشاء طلب الشحن الآن؛ لم يُنشأ طلب ولم تُضف ميزانية.", ephemeral=True)
        if order_id is None:
            if not is_auto_budget_payment_enabled(method):
                return await interaction.response.send_message("أُغلقت وسيلة الدفع قبل إنشاء الطلب؛ لم يبدأ مؤقت ولم تُضف ميزانية.", ephemeral=True)
            return await interaction.response.send_message(
                "لديك طلب شحن آخر ما زال معلقًا في هذا السيرفر. انتظر انتهاء مهلة الخمس دقائق أو إتمامه قبل إنشاء طلب جديد.",
                ephemeral=True,
            )

        message = (
            f"💳 **طلب شحن ميزانية #{order_id} عبر {method_name}**\n"
            f"• الميزانية المطلوبة: `{self.budget_amount:,}` Cosmo\n"
            f"• المبلغ المطلوب تحويله إلى مالك البوت: `{expected_payment:,}`\n"
            "• اضغط زر **نسخ أمر التحويل** لعرض الأمر جاهزًا للنسخ. أرسله من حساب Discord نفسه الذي بدأ الطلب.\n"
            f"يجب أن يطابق اسم الدافع في رسالة التأكيد اسم حسابك في Discord، وأن يصل التأكيد من بوت الدفع الصحيح وبالمبلغ والمستلم المطابقين خلال 5 دقائق (ينتهي <t:{expires_at}:R>). "
            "لا تعتمد على لقطة شاشة أو رسالة معاد توجيهها؛ لا تُضاف الميزانية إلا بعد مطابقة رسالة البوت الأصلية."
        )
        await interaction.response.edit_message(
            content=message,
            view=AutoBudgetCommandView(self.user_id, command),
        )


class AutoBudgetCommandButton(discord.ui.Button):
    def __init__(self, user_id: int, command: str):
        super().__init__(label="نسخ أمر التحويل", style=discord.ButtonStyle.primary, emoji="📋")
        self.user_id = user_id
        self.command = command

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "هذا الزر مخصص لصاحب طلب الشحن فقط.", ephemeral=True
            )
        await interaction.response.send_message(self.command, ephemeral=True)


class AutoBudgetCommandView(discord.ui.View):
    def __init__(self, user_id: int, command: str):
        super().__init__(timeout=AUTO_BUDGET_PAYMENT_TTL)
        self.add_item(AutoBudgetCommandButton(user_id, command))


class AutoBudgetPaymentView(discord.ui.View):
    def __init__(self, bot, user_id: int, budget_amount: int, methods: list[str]):
        super().__init__(timeout=120)
        self.add_item(AutoBudgetPaymentSelect(bot, user_id, budget_amount, methods))

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

def can_manage_global_exchange(ctx: commands.Context) -> bool:
    """كتالوج التبديل العام لا يُدار إلا من سيرفر Cosmo بواسطة إدارته."""
    if not ctx.guild or ctx.guild.id != COSMO_GUILD_ID:
        return False
    if ctx.author.id == PRIMARY_OWNER_ID or ctx.author.id == ctx.guild.owner_id:
        return True
    permissions = getattr(ctx.author, "guild_permissions", None)
    return bool(permissions and (permissions.administrator or permissions.manage_guild))


def budget_history_action_label(action: str) -> str:
    action = (action or "").strip()
    labels = {
        "إضافة": "🟢 إضافة يدوية",
        "خصم": "🔴 خصم",
        "تعيين مباشر": "⚙️ تعيين مباشر",
        "إضافة تلقائية (credit)": "💳 شحن تلقائي عبر ProBot Credits",
        "إضافة تلقائية (adamc)": "💳 شحن تلقائي عبر AdamC",
        "إضافة تلقائية (crypto)": "Ł شحن تلقائي عبر LTC / NOWPayments",
        "إضافة من رصيد Cosmo": "🪙 تحويل من رصيد Cosmo الشخصي",
    }
    if action in labels:
        return labels[action]
    if action.startswith("إضافة تلقائية"):
        return f"💳 {action}"
    return f"📄 {action or 'عملية غير معروفة'}"


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

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or not getattr(message.author, "bot", False):
            return
        if message.author.id == PROBOT_ID:
            method = "credit"
        elif message.author.id == ADAMC_ID:
            method = "adamc"
        else:
            return

        parts = [message.content or ""]
        for embed in message.embeds:
            parts.extend([embed.title or "", embed.description or ""])
            for field in embed.fields:
                parts.extend([field.name or "", field.value or ""])
        receipt = parse_budget_payment_confirmation(method, message.author.id, "\n".join(parts))
        if not receipt or receipt["recipient_id"] != PRIMARY_OWNER_ID:
            return

        try:
            result = settle_budget_payment(
                guild_id=message.guild.id,
                method=method,
                channel_id=message.channel.id,
                payer_name=receipt["sender"],
                expected_payment=receipt["amount"],
                payment_message_id=message.id,
                payment_author_id=message.author.id,
                payment_timestamp=int(message.created_at.timestamp()),
                recipient_id=receipt["recipient_id"],
            )
        except Exception as exc:
            print(f"Auto budget payment settlement failed: {exc}")
            return
        if not result:
            return

        currency_label = get_currency_label(message.guild.id)
        provider = "ProBot Credits" if method == "credit" else "AdamC"
        notice = (
            f"✅ تم التحقق من رسالة **{provider}** الأصلية وإضافة "
            f"`+{result['budget_amount']:,}` {currency_label} إلى ميزانية السيرفر. "
            f"الرصيد الجديد: `{result['new_balance']:,}` {currency_label}."
        )
        try:
            await message.channel.send(notice, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        member = message.guild.get_member(result["user_id"])
        if member:
            try:
                await member.send(
                    f"✅ اكتمل شحن ميزانية **{message.guild.name}** بطلب `#{result['order_id']}`: "
                    f"`+{result['budget_amount']:,}` {currency_label}."
                )
            except discord.HTTPException:
                pass

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
        currency_label = get_currency_label(ctx.guild.id)
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
        await ctx.send(f"✅ تمت إضافة `{parsed_amount:,}` {currency_label} إلى رصيد {member.mention}.\n{pool_msg}")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT balance_log FROM settings WHERE guild_id = ?", (ctx.guild.id,))
        row = g_cur.fetchone()
        g_conn.close()
        if row and row[0]:
            chan = ctx.guild.get_channel(row[0])
            if chan:
                await chan.send(f"📥 **شحن رصيد:** قام {ctx.author.mention} بإضافة `{parsed_amount:,}` {currency_label} إلى {member.mention}.")

        global_embed = discord.Embed(
            title="🌐 عملية شحن رصيد (Global Add Balance)",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        global_embed.add_field(name="🏛️ السيرفر", value=f"{ctx.guild.name} (`{ctx.guild.id}`)", inline=False)
        global_embed.add_field(name="🛡️ المسؤول", value=f"{ctx.author.mention} (`{ctx.author.id}`)", inline=True)
        global_embed.add_field(name="👤 العضو المشحون له", value=f"{member.mention} (`{member.id}`)", inline=True)
        global_embed.add_field(name="🪙 المبلغ المضاف", value=f"`{parsed_amount:,}` {currency_label}", inline=False)
        global_embed.set_footer(text="Cosmo Global Admin Log")
        await send_global_action_log(self.bot, global_embed)

    @commands.hybrid_command(name="remove_balance", aliases=["خصم"], description="خصم رصيد من عضو")
    async def remove_balance(self, ctx: commands.Context, member: discord.Member, amount: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لخصم الرصيد.")
        currency_label = get_currency_label(ctx.guild.id)
        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed_amount <= 0:
            return await ctx.send("❌ المبلغ يجب أن يكون رقماً موجباً.")
        
        if not debit_balance(member.id, parsed_amount):
            return await ctx.send("❌ رصيد العضو أقل من المبلغ المطلوب خصمه.")
        await ctx.send(f"✅ تم خصم `{parsed_amount:,}` {currency_label} من رصيد {member.mention}.")

    @commands.hybrid_command(name="server_budget", aliases=["ميزانية_السيرفر"], description="عرض ميزانية الصرف المتبقية للسيرفر")
    async def server_budget(self, ctx: commands.Context):
        budget = get_server_spending_balance(ctx.guild.id)
        await ctx.send(f"🪙 **رصيد ميزانية السيرفر المتاحة للصرف:** `{budget:,}` {get_currency_label(ctx.guild.id)}.")

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
        currency_label = get_currency_label(target_guild_id)

        await ctx.send(
            f"🎯 **تم تعيين ميزانية السيرفر بنجاح!**\n"
            f"• **السيرفر المستهدف:** {g_name}\n"
            f"• **الميزانية السابقة:** `{current_budget:,}` {currency_label}\n"
            f"• **الميزانية المحددة الجديدة:** `{parsed:,}` {currency_label}"
        )

    @commands.hybrid_command(
        name="auto_add_server_budget",
        aliases=["شحن_ميزانية_تلقائي"],
        description="بدء طلب شحن ميزانية السيرفر عبر وسيلة دفع مفعلة"
    )
    async def auto_add_server_budget(self, ctx: commands.Context, amount: str):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم الأمر داخل السيرفر المطلوب شحن ميزانيته.")
        try:
            budget_amount = parse_amount(amount)
        except Exception:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if budget_amount <= 0 or budget_amount > MAX_SQLITE_INTEGER:
            return await ctx.send("❌ أدخل ميزانية موجبة ضمن الحد الرقمي المدعوم.")

        is_manager = can_manage_bot(ctx)
        personal_balance = get_balance(ctx.author.id)
        if not is_manager:
            if personal_balance < budget_amount:
                return await ctx.send(
                    f"⛔ لست مالك السيرفر أو مسؤولًا. يمكن لأي عضو شحن الميزانية من رصيد Cosmo الشخصي فقط، "
                    f"لكن رصيدك `{personal_balance:,}` أقل من المطلوب `{budget_amount:,}`."
                )
            methods = ["cosmo"]
        else:
            methods = [method for method in ("credit", "adamc") if is_auto_budget_payment_enabled(method)]
            dashboard_url = os.getenv("DASHBOARD_URL", "").strip().rstrip("/")
            if is_auto_budget_payment_enabled("crypto") and nowpayments_is_configured() and dashboard_url.startswith("https://"):
                methods.append("crypto")
            if personal_balance >= budget_amount:
                methods.append("cosmo")
        if not methods:
            return await ctx.send("⛔ لا توجد وسيلة دفع مفعّلة أو مكتملة الإعداد حاليًا؛ تواصل مع مالك البوت.")
        view = AutoBudgetPaymentView(self.bot, ctx.author.id, budget_amount, methods)
        await ctx.send(
            f"اختر طريقة الدفع لإضافة `{budget_amount:,}` Cosmo إلى ميزانية **{ctx.guild.name}**. "
            "تُفعّل وسائل الدفع الخارجية عالميًا بواسطة مالك البوت؛ ويمكن تحويل رصيدك الشخصي بنسبة 1:1.",
            view=view,
        )

    @commands.hybrid_command(
        name="toggle_auto_add_server_budget",
        aliases=["تحكم_الشحن_التلقائي"],
        description="فتح أو إغلاق وسيلة دفع للشحن التلقائي (مالك البوت فقط)"
    )
    async def toggle_auto_add_server_budget(self, ctx: commands.Context, method: str, enabled: bool):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك البوت الأساسي.")
        aliases = {
            "credit": "credit", "credits": "credit", "كريديت": "credit",
            "adamc": "adamc", "ادم سي": "adamc", "آدم سي": "adamc",
            "crypto": "crypto", "ltc": "crypto", "كريبتو": "crypto",
        }
        method_key = aliases.get(method.strip().lower())
        if not method_key:
            return await ctx.send("❌ الطريقة غير معروفة. استخدم `credit` أو `adamc` أو `crypto`.")
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        guild_ids = [guild.id for guild in self.bot.guilds]
        try:
            cancelled = await asyncio.to_thread(set_auto_budget_payment_enabled, method_key, enabled, guild_ids)
        except Exception as exc:
            print(f"Auto budget payment toggle failed: {exc}")
            return await ctx.send("⚠️ تعذر تأكيد تغيير حالة الدفع أو إنهاء الطلبات المعلقة. أعد الفحص قبل إعادة فتح الوسيلة.")
        state = "مفعلة ✅" if enabled else "مغلقة ⛔"
        labels = {"credit": "ProBot Credits", "adamc": "AdamC", "crypto": "كريبتو (LTC)"}
        for order in cancelled:
            notice = (
                f"⛔ أُلغي طلب الشحن التلقائي `#{order['order_id']}` لأن وسيلة الدفع "
                f"**{labels[method_key]}** أُغلقت أو أُعيد فتحها. إذا كنت قد حولت بالفعل، "
                "لا تكرر التحويل وتواصل مع مالك البوت للمراجعة اليدوية."
            )
            channel = self.bot.get_channel(order["channel_id"])
            if channel:
                try:
                    await channel.send(notice, allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    pass
            member = self.bot.get_user(order["user_id"])
            if member:
                try:
                    await member.send(notice)
                except discord.HTTPException:
                    pass
        summary = f"⚙️ وسيلة الدفع **{labels[method_key]}** أصبحت {state} على مستوى البوت."
        if cancelled:
            summary += f" أُلغيت {len(cancelled)} طلبات شحن معلقة وأُبلغ أصحابها."
        if method_key == "crypto" and not enabled:
            summary += " لن تُقبل طلبات LTC جديدة؛ الفواتير الصادرة تبقى قابلة للتسوية حتى انتهاء صلاحية السعر الثابت أو اكتمال الدفع."
        await ctx.send(summary)

    @commands.hybrid_command(
        name="add_server_budget", 
        aliases=["اضافة_ميزانية_السيرفر", "شحن_ميزانية_السيرفر", "add_budget_server"], 
        description="إضافة وشحن مبلغ إلى ميزانية الصرف الحالية للسيرفر (مالك البوت فقط)"
    )
    async def add_server_budget(self, ctx: commands.Context, amount: str):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك ومطور البوت الأساسي.")
        currency_label = get_currency_label(ctx.guild.id if ctx.guild else None)

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
            f"• **المبلغ المضاف:** `+{parsed:,}` {currency_label}\n"
            f"• **الميزانية السابقة:** `{current_budget:,}` {currency_label}\n"
            f"• **إجمالي الميزانية المتاحة الآن:** `{new_total_budget:,}` {currency_label}"
        )

    @commands.hybrid_command(
        name="remove_server_budget", 
        aliases=["خصم_ميزانية_السيرفر", "remove_budget_server"], 
        description="خصم مبلغ من ميزانية الصرف المتاحة للسيرفر (مالك البوت فقط)"
    )
    async def remove_server_budget(self, ctx: commands.Context, amount: str):
        if not await check_is_owner(self.bot, ctx):
            return await ctx.send("⛔ هذا الأمر متاح فقط لمالك ومطور البوت الأساسي.")
        currency_label = get_currency_label(ctx.guild.id if ctx.guild else None)

        try:
            parsed = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed <= 0:
            return await ctx.send("❌ المبلغ المخصوم يجب أن يكون أكبر من 0.")

        current_budget = get_server_spending_balance(ctx.guild.id)
        if current_budget < parsed:
            return await ctx.send(
                f"❌ لا يمكن الخصم: المبلغ المطلوب خصمه (`{parsed:,}`) أكبر من ميزانية السيرفر الحالية (`{current_budget:,}` {currency_label})."
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
            f"• **المبلغ المخصوم:** `-{parsed:,}` {currency_label}\n"
            f"• **الميزانية السابقة:** `{current_budget:,}` {currency_label}\n"
            f"• **الميزانية المتبقية الآن:** `{new_total_budget:,}` {currency_label}"
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
        currency_label = get_currency_label(ctx.guild.id)
        g_conn.close()

        if not rows:
            return await ctx.send(f"ℹ️ لا توجد أي عمليات مسجلة في ميزانية السيرفر حتى الآن.\n🪙 **الميزانية الحالية:** `{current_pool:,}` {currency_label}.")

        embed = discord.Embed(
            title=f"📋 سجل ميزانية السيرفر | {ctx.guild.name}",
            description=f"🪙 **الميزانية المتاحة حالياً:** `{current_pool:,}` {currency_label}\n\nعرض آخر **5** عمليات تمت على الميزانية:",
            color=discord.Color.gold()
        )

        for log_id, action, amt, old_b, new_b, admin_id, t_stamp in rows:
            action_label = budget_history_action_label(action)
            details = (
                f"• **نوع العملية:** {action_label}\n"
                f"• **المبلغ:** `{amt:,}` {currency_label}\n"
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
        g_cur.execute("SELECT activated_at, premium_end, custom_name, custom_avatar_url, custom_banner_url, custom_currency_name FROM premium_info WHERE guild_id = ?", (ctx.guild.id,))
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
        embed.add_field(name="🪙 اسم الرصيد المعروض", value=f"`{row[5]}`" if len(row) > 5 and row[5] else "Cosmo (الافتراضي)", inline=True)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="set_currency_name", aliases=["اسم_العملة"], description="تخصيص اسم العرض لرصيد Cosmo في هذا السيرفر (Premium)")
    async def set_currency_name(self, ctx: commands.Context, *, new_name: str):
        if not can_manage_bot(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية إدارة إعدادات السيرفر.")
        if not is_guild_premium(ctx.guild.id):
            return await ctx.send("💎 **ميزة Premium:** تخصيص اسم الرصيد يحتاج اشتراكًا نشطًا في هذا السيرفر.")
        new_name = " ".join(new_name.split())
        if len(new_name) > 24 or any(not char.isprintable() for char in new_name) or any(char in new_name for char in "@`<>"):
            return await ctx.send("❌ الاسم يجب أن يكون واضحًا وبحد أقصى 24 حرفًا، ومن دون رموز الإشارات أو backticks.")
        stored_name = None if not new_name or new_name.casefold() in {"cosmo", "default", "افتراضي"} else new_name
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO premium_info (guild_id, custom_currency_name) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET custom_currency_name = excluded.custom_currency_name",
            (ctx.guild.id, stored_name),
        )
        g_conn.commit()
        g_conn.close()
        visible_name = stored_name or "Cosmo"
        await ctx.send(f"✅ اسم عرض رصيدك في هذا السيرفر أصبح **{visible_name}**. لم يتغير الرصيد نفسه، ولا يتأثر أي سيرفر آخر.")

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

    @commands.hybrid_command(name="toggle_exchange_system", aliases=["قفل_التبديل_العام"], description="قفل أو فتح نظام التبديل لجميع السيرفرات من سيرفر Cosmo")
    async def toggle_exchange_system(self, ctx: commands.Context, status: bool):
        if not can_manage_global_exchange(ctx):
            return await ctx.send(f"⛔ قفل أو فتح التبديل العام متاح لإدارة سيرفر Cosmo فقط (`{COSMO_GUILD_ID}`).")
        set_global_exchange_enabled(status)
        msg = "مفتوحاً في جميع السيرفرات ✅" if status else "مقفلًا في جميع السيرفرات ⛔"
        await ctx.send(f"⚙️ تم ضبط نظام التبديل العام ليكون: **{msg}**.")

    @commands.hybrid_command(name="toggle_currency_status", aliases=["قفل_عملة"], description="قفل أو فتح تبديل عملة عامة لجميع السيرفرات من Cosmo")
    async def toggle_currency_status(self, ctx: commands.Context, currency_name: str, status: bool):
        if not can_manage_global_exchange(ctx):
            return await ctx.send(f"⛔ إدارة العملات العامة متاحة لإدارة سيرفر Cosmo فقط (`{COSMO_GUILD_ID}`).")
        existing_name = set_exchange_currency_status(currency_name, status)
        if not existing_name:
            return await ctx.send(f"❌ العملة `{currency_name}` غير موجودة في الكتالوج العام.")
        msg = "متاحة للتبديل ✅" if status else "معطلة ومقفلة ⛔"
        await ctx.send(f"⚙️ العملة العامة **{existing_name}** أصبحت الآن: **{msg}** في جميع السيرفرات.")

    @commands.hybrid_command(name="add_exchange_currency", aliases=["اضافة_عملة_تبديل"], description="إضافة أو تعديل عملة في كتالوج التبديل العام من سيرفر Cosmo")
    async def add_exchange_currency(self, ctx: commands.Context, currency_name: str, rate: float, min_received: float = 0.0):
        if not can_manage_global_exchange(ctx):
            return await ctx.send(f"⛔ إدارة العملات العامة متاحة لإدارة سيرفر Cosmo فقط (`{COSMO_GUILD_ID}`).")
        currency_name = " ".join(currency_name.split())
        if not currency_name or len(currency_name) > 32 or any(not char.isprintable() for char in currency_name) or any(char in currency_name for char in "@`<>"):
            return await ctx.send("❌ اسم العملة يجب أن يكون واضحاً وبحد أقصى 32 حرفاً ومن دون رموز الإشارات أو backticks.")
        if not math.isfinite(rate) or rate <= 0 or not math.isfinite(min_received) or min_received < 0:
            return await ctx.send("❌ يجب أن يكون السعر أكبر من صفر والحد الأدنى صفراً أو أكثر، وبقيم رقمية صالحة.")
        currency_name = upsert_exchange_currency(currency_name, rate, min_received)
        await ctx.send(f"✅ تم تحديث العملة العامة **{currency_name}** لكل السيرفرات:\n• السعر: `1 Cosmo = {rate:g} {currency_name}`\n• الحد الأدنى: `{min_received:g} {currency_name}`.")

    @commands.hybrid_command(name="delete_exchange_currency", aliases=["حذف_عملة_تبديل"], description="حذف عملة من كتالوج التبديل العام من سيرفر Cosmo")
    async def delete_exchange_currency(self, ctx: commands.Context, currency_name: str):
        if not can_manage_global_exchange(ctx):
            return await ctx.send(f"⛔ إدارة العملات العامة متاحة لإدارة سيرفر Cosmo فقط (`{COSMO_GUILD_ID}`).")
        deleted_name = delete_exchange_currency(currency_name)
        if not deleted_name:
            return await ctx.send(f"❌ العملة `{currency_name}` غير موجودة في الكتالوج العام.")
        await ctx.send(f"🗑️ تم حذف العملة العامة **{deleted_name}** من قائمة جميع السيرفرات.")

    @commands.hybrid_command(name="set_admin_role", aliases=["رتبة_المسؤول"], description="تعيين رتبة مسؤولي البوت")
    async def set_admin_role(self, ctx: commands.Context, role: discord.Role):
        if ctx.author.id != ctx.guild.owner_id and ctx.author.id != PRIMARY_OWNER_ID:
            return await ctx.send("⛔ مالك السيرفر فقط يمكنه تعيين الرتبة.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO settings (guild_id, admin_role_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET admin_role_id = excluded.admin_role_id",
            (ctx.guild.id, role.id)
        )
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
