import discord
from discord.ext import commands
import io
import datetime
from database import get_guild_db, is_bot_admin, PRIMARY_OWNER_ID

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

        # التحقق من الصلاحيات: صاحب التكت أو الإدارة
        g_cur.execute("SELECT admin_role_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
        admin_row = g_cur.fetchone()
        has_admin = interaction.user.id in [interaction.guild.owner_id, PRIMARY_OWNER_ID]
        if admin_row and admin_row[0]:
            role = interaction.guild.get_role(admin_row[0])
            if role and role in interaction.user.roles:
                has_admin = True

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
        # المسؤولون فقط يمكنهم حذف التذكرة نهائياً
        g_conn, g_cur = get_guild_db(interaction.guild.id)
        g_cur.execute("SELECT admin_role_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
        admin_row = g_cur.fetchone()
        has_admin = interaction.user.id in [interaction.guild.owner_id, PRIMARY_OWNER_ID]
        if admin_row and admin_row[0]:
            role = interaction.guild.get_role(admin_row[0])
            if role and role in interaction.user.roles:
                has_admin = True

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

        g_cur.execute("DELETE FROM tickets WHERE channel_id = ?", (interaction.channel.id,))
        g_conn.commit()
        g_conn.close()

        await interaction.channel.delete(reason=f"Deleted by {interaction.user.name}")


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

        await interaction.response.defer(ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        }

        g_cur.execute("SELECT admin_role_id FROM settings WHERE guild_id = ?", (guild.id,))
        admin_row = g_cur.fetchone()
        if admin_row and admin_row[0]:
            admin_role = guild.get_role(admin_row[0])
            if admin_role:
                overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        channel_name = f"ticket-{user.name[:10]}"
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="Ticket created"
        )

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        g_cur.execute(
            "INSERT INTO tickets (channel_id, user_id, opened_at, is_closed) VALUES (?, ?, ?, 0)",
            (ticket_channel.id, user.id, now_str)
        )
        g_conn.commit()
        g_conn.close()

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
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لاستخدام هذا الأمر.")

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
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية لاستخدام هذا الأمر.")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO settings (guild_id, ticket_log) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET ticket_log = excluded.ticket_log",
            (ctx.guild.id, channel.id)
        )
        g_conn.commit()
        g_conn.close()
        await ctx.send(f"✅ تم ضبط روم لوج التذاكر على: {channel.mention}")

    @commands.hybrid_command(name="rename", aliases=["تغيير_اسم_التكت"], description="تغيير اسم التذكرة مع الحفاظ عليها داخل النظام")
    async def rename_ticket(self, ctx: commands.Context, new_name: str):
        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute("SELECT user_id FROM tickets WHERE channel_id = ?", (ctx.channel.id,))
        row = g_cur.fetchone()
        g_conn.close()

        if not row:
            return await ctx.send("❌ هذا الروم ليس تذكرة مسجلة في النظام.")

        if not is_bot_admin(ctx) and ctx.author.id != row[0]:
            return await ctx.send("⛔ ليس لديك صلاحية لتغيير اسم هذه التذكرة.")

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

        if not is_bot_admin(ctx) and ctx.author.id != owner_id:
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
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ المسؤولون فقط يمكنهم حذف التذكرة نهائياً.")

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

        g_cur.execute("DELETE FROM tickets WHERE channel_id = ?", (ctx.channel.id,))
        g_conn.commit()
        g_conn.close()

        await ctx.channel.delete(reason=f"Deleted by {ctx.author.name}")

async def setup(bot):
    await bot.add_cog(Tickets(bot))
