import discord
import asyncio
from functools import wraps
from discord.ext import commands
import io
import datetime
import re
from database import (
    get_guild_db, is_bot_admin, PRIMARY_OWNER_ID,
    get_guild_management_role_ids, has_guild_role,
)

_ticket_creation_locks: dict[tuple[int, int], asyncio.Lock] = {}


def serialize_ticket_creation(callback):
    @wraps(callback)
    async def wrapped(view, interaction, button):
        guild = interaction.guild
        user = interaction.user
        key = (int(guild.id), int(user.id))
        lock = _ticket_creation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await callback(view, interaction, button)
    return wrapped

def can_manage_ticket_member(guild, member, admin_role_id=None, owner_role_id=None) -> bool:
    """صلاحيات التذاكر التشغيلية: owner/admin المعرّفان ومالكو السيرفر."""
    if not guild or not member:
        return False
    if member.id in (getattr(guild, "owner_id", None), PRIMARY_OWNER_ID):
        return True
    permissions = getattr(member, "guild_permissions", None)
    if permissions and (permissions.administrator or permissions.manage_guild):
        return True
    return has_guild_role(member, owner_role_id, guild) or has_guild_role(member, admin_role_id, guild)


def can_manage_ticket_settings(ctx: commands.Context) -> bool:
    """إعداد التذاكر وإعادة تسميتها متاحان للمالك/owner-role فقط، لا لرتبة admin المحدودة."""
    if not ctx.guild or not ctx.author:
        return False
    owner_role_id, _limited_admin_role_id = get_guild_management_role_ids(ctx.guild.id)
    return (
        can_manage_ticket_member(ctx.guild, ctx.author, owner_role_id=owner_role_id)
        or is_bot_admin(ctx)
    )

def can_manage_tickets(ctx: commands.Context) -> bool:
    if not ctx.guild or not ctx.author:
        return False
    g_conn, g_cur = get_guild_db(ctx.guild.id)
    g_cur.execute("SELECT admin_role_id, owner_role_id FROM settings WHERE guild_id = ?", (ctx.guild.id,))
    row = g_cur.fetchone()
    g_conn.close()
    admin_role_id, owner_role_id = row if row else (None, None)
    return can_manage_ticket_member(ctx.guild, ctx.author, admin_role_id, owner_role_id) or is_bot_admin(ctx)


async def sync_ticket_staff_roles(guild, previous_role_ids=(), active_role_ids=()):
    """Ensure configured staff roles can see existing tickets after role setup/change."""
    previous_role_ids = {int(role_id) for role_id in previous_role_ids if role_id}
    active_role_ids = {int(role_id) for role_id in active_role_ids if role_id}
    affected_ids = previous_role_ids | active_role_ids
    if not affected_ids:
        return
    conn, cur = get_guild_db(guild.id)
    cur.execute("SELECT channel_id FROM tickets")
    channel_ids = [int(row[0]) for row in cur.fetchall()]
    conn.close()
    for channel_id in channel_ids:
        channel = guild.get_channel(channel_id)
        if not channel:
            continue
        for role_id in affected_ids:
            role = guild.get_role(role_id)
            if not role:
                continue
            try:
                if role_id in active_role_ids:
                    overwrite = channel.overwrites_for(role)
                    overwrite.view_channel = True
                    overwrite.send_messages = True
                    overwrite.read_message_history = True
                    await channel.set_permissions(role, overwrite=overwrite, reason="Sync Cosmo ticket staff roles")
                else:
                    await channel.set_permissions(role, overwrite=None, reason="Remove old Cosmo ticket staff role")
            except discord.HTTPException:
                continue

# واجهة أزرار التحكم داخل التذكرة (إغلاق / حذف)
class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="إغلاق التذكرة | Close",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="ticket_btn_close_action"
    )
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_conn, g_cur = get_guild_db(interaction.guild.id)
        g_cur.execute("SELECT user_id, is_closed FROM tickets WHERE channel_id = ?", (interaction.channel.id,))
        row = g_cur.fetchone()

        if not row:
            g_conn.close()
            return await interaction.response.send_message("❌ هذا الروم ليس تذكرة مسجلة في النظام.", ephemeral=True)

        owner_id, is_closed = row
        if is_closed == 1:
            g_conn.close()
            return await interaction.response.send_message("⚠️ هذه التذكرة مغلقة بالفعل.", ephemeral=True)

        # التحقق من الصلاحيات: صاحب التكت أو الرتبتان المخولتان
        g_cur.execute("SELECT admin_role_id, owner_role_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
        admin_row = g_cur.fetchone()
        has_admin = can_manage_ticket_member(
            interaction.guild,
            interaction.user,
            admin_row[0] if admin_row else None,
            admin_row[1] if admin_row else None,
        )

        if not has_admin and interaction.user.id != owner_id:
            g_conn.close()
            return await interaction.response.send_message("⛔ فقط صاحب التذكرة أو الإدارة يمكنهم إغلاقها.", ephemeral=True)

        # سلب صلاحية الرؤية من صاحب التذكرة
        owner_member = interaction.guild.get_member(owner_id)
        if owner_member:
            await interaction.channel.set_permissions(owner_member, view_channel=False)

        # تحديث قاعدة البيانات
        g_cur.execute(
            "UPDATE tickets SET is_closed = 1, closed_by = ? WHERE channel_id = ?",
            (interaction.user.id, interaction.channel.id)
        )
        g_conn.commit()
        g_conn.close()

        button.disabled = True
        await interaction.message.edit(view=self)

        embed = discord.Embed(
            title="🔒 تم إغلاق التذكرة",
            description=f"تم إغلاق التذكرة بواسطة {interaction.user.mention}.\nتم إخفاء الروم عن صاحب التذكرة.\n\nلحذف التذكرة وسحب سجل المحادثات اضغط على زر **الحذف** أو استخدم أمر `/delete`.",
            color=discord.Color.orange()
        )
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(
        label="حذف التذكرة | Delete",
        style=discord.ButtonStyle.danger,
        emoji="🗑️",
        custom_id="ticket_btn_delete_action"
    )
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        # مالك السيرفر وowner-role وadmin-role المحدود يمكنهم حذف التذكرة
        g_conn, g_cur = get_guild_db(interaction.guild.id)
        g_cur.execute("SELECT admin_role_id, owner_role_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
        admin_row = g_cur.fetchone()
        has_admin = can_manage_ticket_member(
            interaction.guild,
            interaction.user,
            admin_row[0] if admin_row else None,
            admin_row[1] if admin_row else None,
        )

        if not has_admin:
            g_conn.close()
            return await interaction.response.send_message("⛔ المسؤولون فقط يمكنهم حذف التذكرة نهائياً.", ephemeral=True)

        g_cur.execute("SELECT user_id, opened_at, closed_by FROM tickets WHERE channel_id = ?", (interaction.channel.id,))
        row = g_cur.fetchone()

        if not row:
            g_conn.close()
            return await interaction.response.send_message("❌ هذا الروم ليس تذكرة مسجلة في النظام.", ephemeral=True)

        creator_id, opened_at, closed_by_id = row

        g_cur.execute("SELECT ticket_log FROM settings WHERE guild_id = ?", (interaction.guild.id,))
        log_row = g_cur.fetchone()
        log_channel_id = log_row[0] if log_row and log_row[0] else None

        await interaction.response.send_message("⏳ جاري تفريغ المحادثات وإرسال السجل تمهيداً لحذف التذكرة...")

        transcript_text = f"=== سجل تذكرة: #{interaction.channel.name} ===\n"
        transcript_text += f"السيرفر: {interaction.guild.name} ({interaction.guild.id})\n"
        transcript_text += f"من فتح التذكرة: {creator_id}\n"
        transcript_text += f"تاريخ الفتح: {opened_at}\n"
        transcript_text += f"من أغلق التذكرة: {closed_by_id if closed_by_id else 'لم يتم إغلاقها مسبقاً'}\n"
        transcript_text += f"من حذف التذكرة: {interaction.user.name} ({interaction.user.id})\n"
        transcript_text += "=" * 50 + "\n\n"

        async for msg in interaction.channel.history(limit=None, oldest_first=True):
            time_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author_str = f"{msg.author.name}#{msg.author.discriminator if hasattr(msg.author, 'discriminator') else ''} ({msg.author.id})"
            content_str = msg.clean_content or "[لا يوجد نص]"

            transcript_text += f"[{time_str}] {author_str}:\n{content_str}\n"

            if msg.attachments:
                for att in msg.attachments:
                    transcript_text += f"📎 مرفق: {att.url}\n"

            transcript_text += "\n"

        file_buffer = io.BytesIO(transcript_text.encode("utf-8"))
        transcript_file = discord.File(file_buffer, filename=f"transcript-{interaction.channel.name}.txt")

        if log_channel_id:
            log_chan = interaction.guild.get_channel(log_channel_id)
            if log_chan:
                embed = discord.Embed(
                    title="🗑️ تقرير حذف تذكرة",
                    description=f"تم حذف تذكرة **#{interaction.channel.name}** نهائياً.\nتم إرفاق ملف المحادثات النصي بالكامل أدناه.",
                    color=discord.Color.red()
                )
                embed.add_field(name="👤 من فتح التذكرة:", value=f"<@{creator_id}> (`{creator_id}`)", inline=True)
                closed_text = f"<@{closed_by_id}>" if closed_by_id else "`مباشرة عند الحذف`"
                embed.add_field(name="🔒 من أغلقها:", value=closed_text, inline=True)
                embed.add_field(name="🗑️ من حذفها:", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
                embed.add_field(name="📅 تاريخ الفتح:", value=f"`{opened_at}`", inline=False)
                await log_chan.send(embed=embed, file=transcript_file)

        try:
            await interaction.channel.delete(reason=f"Deleted by {interaction.user.name}")
            g_cur.execute("DELETE FROM tickets WHERE channel_id = ?", (interaction.channel.id,))
            g_conn.commit()
        finally:
            g_conn.close()


# واجهة زر فتح التذكرة الأساسية
class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="فتح تذكرة | Open Ticket",
        style=discord.ButtonStyle.primary,
        emoji="📩",
        custom_id="persistent_open_ticket_button"
    )
    @serialize_ticket_creation
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user
        category = interaction.channel.category

        g_conn, g_cur = get_guild_db(guild.id)
        g_cur.execute("SELECT channel_id FROM tickets WHERE user_id = ? AND is_closed = 0", (user.id,))
        existing_ticket = g_cur.fetchone()

        if existing_ticket:
            existing_channel = guild.get_channel(existing_ticket[0])
            if existing_channel:
                g_conn.close()
                return await interaction.response.send_message(
                    f"⚠️ لديك تذكرة مفتوحة بالفعل: {existing_channel.mention}",
                    ephemeral=True
                )
            g_cur.execute("UPDATE tickets SET is_closed = 1 WHERE channel_id = ? AND is_closed = 0", (existing_ticket[0],))
            g_conn.commit()

        await interaction.response.defer(ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }

        g_cur.execute("SELECT admin_role_id, owner_role_id FROM settings WHERE guild_id = ?", (guild.id,))
        admin_row = g_cur.fetchone()
        if admin_row:
            for role_id in admin_row:
                staff_role = guild.get_role(role_id) if role_id else None
                if staff_role:
                    overwrites[staff_role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True
                    )
        g_conn.close()

        channel_name = f"ticket-{user.name[:10]}"
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="Ticket created"
        )

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ticket_conn = None
        try:
            ticket_conn, ticket_cur = get_guild_db(guild.id)
            ticket_cur.execute(
                "INSERT INTO tickets (channel_id, user_id, opened_at, is_closed) VALUES (?, ?, ?, 0)",
                (ticket_channel.id, user.id, now_str)
            )
            ticket_conn.commit()
        except Exception:
            if ticket_conn:
                ticket_conn.rollback()
            try:
                await ticket_channel.delete(reason="Ticket database registration failed")
            except Exception:
                pass
            raise
        finally:
            if ticket_conn:
                ticket_conn.close()

        # رسالة الترحيب مرفقة بالأزرار التفاعلية
        embed = discord.Embed(
            title=f"تذكرة الدعم | Ticket #{user.name}",
            description=f"مرحباً بك {user.mention}!\nيرجى طرح استفسارك أو مشكلتك وسيقوم فريق الدعم بالرد عليك في أقرب وقت.\n\n"
                        f"🔒 **للإغلاق:** اضغط على زر الإغلاق أدناه أو اكتب `/close`\n"
                        f"🗑️ **للحذف النهائي:** اضغط على زر الحذف أو اكتب `/delete`",
            color=discord.Color.green()
        )
        embed.set_footer(text=f"ID: {user.id} • تم الفتح في: {now_str}")
        
        # إرسال الرسالة مع أزرار التحكم
        await ticket_channel.send(f"{user.mention}", embed=embed, view=TicketControlView())

        await interaction.followup.send(f"✅ تم إنشاء تذكرتك بنجاح: {ticket_channel.mention}", ephemeral=True)


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # تسجيل واجهات الأزرار لتعمل دائماً حتى بعد إعادة تشغيل البوت
        self.bot.add_view(TicketCreateView())
        self.bot.add_view(TicketControlView())

    @commands.hybrid_command(name="setup_ticket", aliases=["انشاء_تكت"], description="إنشاء رسالة وزر فتح التذاكر في هذا الروم")
    async def setup_ticket(self, ctx: commands.Context):
        if not can_manage_ticket_settings(ctx):
            return await ctx.send("⛔ مالك السيرفر أو owner-role فقط يمكنه إعداد النظام.")

        embed = discord.Embed(
            title="🎫 نظام الدعم الفني والمساعدة",
            description="إذا كنت بحاجة إلى مساعدة، استفسار، أو إتمام عملية تبديل، اضغط على الزر أدناه لفتح تذكرة خاصة بك.\n\n"
                        "📌 **ملاحظة:** يرجى عدم فتح تذاكر دون سبب تجنباً للمخالفة.",
            color=discord.Color.blue()
        )
        embed.set_footer(text="Cosmo Support System • اضغط لفتح تذكرة")
        if ctx.guild.icon:
            embed.set_thumbnail(url=ctx.guild.icon.url)

        await ctx.send(embed=embed, view=TicketCreateView())

    @commands.hybrid_command(name="set_ticket_log", aliases=["لوج_التكت"], description="تحديد روم إرسال سجلات وتفريغ التذاكر بعد حذفها")
    async def set_ticket_log(self, ctx: commands.Context, channel: discord.TextChannel):
        if not can_manage_ticket_settings(ctx):
            return await ctx.send("⛔ مالك السيرفر أو owner-role فقط يمكنه ضبط قناة اللوج.")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO settings (guild_id, ticket_log) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET ticket_log = excluded.ticket_log",
            (ctx.guild.id, channel.id)
        )
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم ضبط روم لوج التذاكر على: {channel.mention}")

    @commands.hybrid_command(
        name="add_person",
        aliases=["اضافة_شخص_للتكت"],
        description="إضافة عضو إلى التذكرة الحالية (الإدارة فقط)",
    )
    async def add_person(self, ctx: commands.Context, member_id_or_mention: str):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم هذا الأمر داخل تذكرة في السيرفر.")
        if not can_manage_tickets(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو مسؤولي التذاكر فقط.")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT is_closed FROM tickets WHERE channel_id = ?", (ctx.channel.id,))
        ticket = g_cur.fetchone()
        g_conn.close()
        if not ticket:
            return await ctx.send("❌ هذا الروم ليس تذكرة مسجلة في النظام.")
        if ticket[0]:
            return await ctx.send("⚠️ لا يمكن إضافة عضو إلى تذكرة مغلقة.")

        match = re.fullmatch(r"(?:<@!?(\d{1,20})>|(\d{1,20}))", member_id_or_mention.strip())
        if not match:
            return await ctx.send("❌ أدخل ID عضو صالحاً أو منشنه داخل السيرفر.")
        member_id = int(match.group(1) or match.group(2))
        try:
            member = ctx.guild.get_member(member_id)
            if member is None:
                member = await ctx.guild.fetch_member(member_id)
        except discord.NotFound:
            return await ctx.send("❌ لم أعثر على هذا العضو في السيرفر.")
        except (discord.Forbidden, discord.HTTPException):
            return await ctx.send("⚠️ تعذر جلب العضو من Discord؛ تحقق من صلاحيات البوت ثم أعد المحاولة.")

        try:
            overwrite = ctx.channel.overwrites_for(member)
            overwrite.view_channel = True
            overwrite.send_messages = True
            await ctx.channel.set_permissions(
                member,
                overwrite=overwrite,
                reason=f"Ticket access granted by {ctx.author} ({ctx.author.id})",
            )
        except discord.Forbidden:
            return await ctx.send("⛔ لا يملك البوت صلاحية إدارة أذونات هذه القناة.")
        except discord.HTTPException:
            return await ctx.send("⚠️ تعذر تحديث أذونات القناة؛ حاول مرة أخرى لاحقاً.")

        await ctx.send(f"✅ تمت إضافة {member.mention} إلى التذكرة.")

    @commands.hybrid_command(name="rename", aliases=["تغيير_اسم_التكت"], description="إعادة تسمية التذكرة (مالك السيرفر أو مسؤول التذاكر فقط)")
    async def rename_ticket(self, ctx: commands.Context, new_name: str):
        if not ctx.guild:
            return await ctx.send("⛔ استخدم هذا الأمر داخل التذكرة في السيرفر.")
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT 1 FROM tickets WHERE channel_id = ?", (ctx.channel.id,))
        row = g_cur.fetchone()
        g_conn.close()

        if not row:
            return await ctx.send("❌ هذا الروم ليس تذكرة مسجلة في النظام.")

        if not can_manage_ticket_settings(ctx):
            return await ctx.send("⛔ إعادة تسمية التذكرة متاحة لمالك السيرفر أو owner-role فقط.")

        cleaned_name = new_name.strip().replace(" ", "-")[:32]
        await ctx.channel.edit(name=cleaned_name)
        await ctx.send(f"✅ تم تغيير اسم التذكرة إلى: `{cleaned_name}` مع بقائها مسجلة في النظام بشكل كامل.")

    @commands.hybrid_command(name="close", aliases=["اغلاق_التكت"], description="إغلاق التذكرة وإخفائها عن العضو دون حذفها")
    async def close_ticket(self, ctx: commands.Context):
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT user_id, is_closed FROM tickets WHERE channel_id = ?", (ctx.channel.id,))
        row = g_cur.fetchone()

        if not row:
            g_conn.close()
            return await ctx.send("❌ هذا الروم ليس تذكرة مسجلة في النظام.")

        owner_id, is_closed = row
        if is_closed == 1:
            g_conn.close()
            return await ctx.send("⚠️ هذه التذكرة مغلقة بالفعل.")

        if not can_manage_tickets(ctx) and ctx.author.id != owner_id:
            g_conn.close()
            return await ctx.send("⛔ فقط صاحب التذكرة أو الإدارة يمكنهم إغلاقها.")

        owner_member = ctx.guild.get_member(owner_id)
        if owner_member:
            await ctx.channel.set_permissions(owner_member, view_channel=False)

        g_cur.execute("UPDATE tickets SET is_closed = 1, closed_by = ? WHERE channel_id = ?", (ctx.author.id, ctx.channel.id))
        g_conn.commit()
        g_conn.close()

        embed = discord.Embed(
            title="🔒 تم إغلاق التذكرة",
            description=f"تم إغلاق التذكرة بواسطة {ctx.author.mention}.\nتم إخفاء الروم عن صاحب التذكرة.\n\nلحذف التذكرة وسحب سجل المحادثات نهائياً اضغط على زر الحذف أو استخدم أمر `/delete`.",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="delete", aliases=["حذف_التكت"], description="حذف التذكرة وحفظ سجل محادثاتها في روم اللوج")
    async def delete_ticket(self, ctx: commands.Context):
        if not can_manage_tickets(ctx):
            return await ctx.send("⛔ مالك السيرفر أو مسؤول التذاكر فقط يمكنه حذف التذكرة نهائياً.")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT user_id, opened_at, closed_by FROM tickets WHERE channel_id = ?", (ctx.channel.id,))
        row = g_cur.fetchone()

        if not row:
            g_conn.close()
            return await ctx.send("❌ هذا الروم ليس تذكرة مسجلة في النظام.")

        creator_id, opened_at, closed_by_id = row

        g_cur.execute("SELECT ticket_log FROM settings WHERE guild_id = ?", (ctx.guild.id,))
        log_row = g_cur.fetchone()
        log_channel_id = log_row[0] if log_row and log_row[0] else None

        await ctx.send("⏳ جاري استخراج محتوى التذكرة وتجهيز السجل تمهيداً للحذف...")

        transcript_text = f"=== سجل تذكرة: #{ctx.channel.name} ===\n"
        transcript_text += f"السيرفر: {ctx.guild.name} ({ctx.guild.id})\n"
        transcript_text += f"من فتح التذكرة: {creator_id}\n"
        transcript_text += f"تاريخ الفتح: {opened_at}\n"
        transcript_text += f"من أغلق التذكرة: {closed_by_id if closed_by_id else 'لم يتم إغلاقها مسبقاً'}\n"
        transcript_text += f"من حذف التذكرة: {ctx.author.name} ({ctx.author.id})\n"
        transcript_text += "=" * 50 + "\n\n"

        async for msg in ctx.channel.history(limit=None, oldest_first=True):
            time_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            author_str = f"{msg.author.name}#{msg.author.discriminator if hasattr(msg.author, 'discriminator') else ''} ({msg.author.id})"
            content_str = msg.clean_content or "[لا يوجد نص]"

            transcript_text += f"[{time_str}] {author_str}:\n{content_str}\n"

            if msg.attachments:
                for att in msg.attachments:
                    transcript_text += f"📎 مرفق: {att.url}\n"

            transcript_text += "\n"

        file_buffer = io.BytesIO(transcript_text.encode("utf-8"))
        transcript_file = discord.File(file_buffer, filename=f"transcript-{ctx.channel.name}.txt")

        if log_channel_id:
            log_chan = ctx.guild.get_channel(log_channel_id)
            if log_chan:
                embed = discord.Embed(
                    title="🗑️ تقرير حذف تذكرة",
                    description=f"تم حذف تذكرة **#{ctx.channel.name}** نهائياً.\nتم إرفاق ملف المحادثات النصي بالكامل أدناه.",
                    color=discord.Color.red()
                )
                embed.add_field(name="👤 من فتح التذكرة:", value=f"<@{creator_id}> (`{creator_id}`)", inline=True)
                closed_text = f"<@{closed_by_id}>" if closed_by_id else "`مباشرة عند الحذف`"
                embed.add_field(name="🔒 من أغلقها:", value=closed_text, inline=True)
                embed.add_field(name="🗑️ من حذفها:", value=f"{ctx.author.mention} (`{ctx.author.id}`)", inline=True)
                embed.add_field(name="📅 تاريخ الفتح:", value=f"`{opened_at}`", inline=False)
                await log_chan.send(embed=embed, file=transcript_file)

        try:
            await ctx.channel.delete(reason=f"Deleted by {ctx.author.name}")
            g_cur.execute("DELETE FROM tickets WHERE channel_id = ?", (ctx.channel.id,))
            g_conn.commit()
        finally:
            g_conn.close()

async def setup(bot):
    await bot.add_cog(Tickets(bot))
