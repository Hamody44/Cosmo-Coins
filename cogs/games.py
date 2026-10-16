import discord
from discord.ext import commands
import random
import io
import json
import asyncio
import time
import math
from database import (
    parse_amount, parse_duration, is_bot_admin, deduct_server_spending,
    refund_server_spending, update_balance, is_blacklisted, get_guild_db,
    clean_display_name, get_unicode_font, PRIMARY_OWNER_ID
)
from PIL import Image, ImageDraw, ImageOps

active_roulettes = {}

def can_manage_giveaway(ctx: commands.Context) -> bool:
    """مالك السيرفر يستطيع بدء الجيف والبوكس حتى دون بريميوم؛ بقية الصلاحيات كما هي."""
    if not ctx.guild:
        return False
    return ctx.author.id == ctx.guild.owner_id or is_bot_admin(ctx)

class RouletteSession:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.is_stopped = False
        self.current_view = None

    def stop(self):
        self.is_stopped = True
        if self.current_view:
            self.current_view.stop()

class ClaimVerificationView(discord.ui.View):
    def __init__(self, winner: discord.Member, amount: int, reward_type: str = "الجائزة"):
        super().__init__(timeout=None)
        self.winner = winner
        self.amount = amount
        self.reward_type = reward_type

    @discord.ui.button(label="تسليم", style=discord.ButtonStyle.success, emoji="✅")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_conn, g_cur = get_guild_db(interaction.guild.id)
        g_cur.execute("SELECT admin_role_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
        row = g_cur.fetchone()
        g_conn.close()

        is_admin = interaction.user.id == interaction.guild.owner_id or interaction.user.id == PRIMARY_OWNER_ID
        if row and row[0]:
            role = interaction.guild.get_role(row[0])
            if role and role in interaction.user.roles:
                is_admin = True

        if not is_admin:
            return await interaction.response.send_message("⛔ المسؤولون فقط يمكنهم اتخاذ هذا الإجراء.", ephemeral=True)

        if is_blacklisted(self.winner):
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
            return await interaction.response.send_message(f"⛔ تعذر التسليم: العضو {self.winner.mention} مدرج في البلاك ليست.")

        update_balance(self.winner.id, self.amount)
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(f"✅ تم تسليم `{self.amount:,}` Cosmo إلى {self.winner.mention} بواسطة {interaction.user.mention}.")
        try:
            await self.winner.send(f"🎉 تم تسليمك {self.reward_type} بقيمة `{self.amount:,}` Cosmo.")
        except:
            pass

    @discord.ui.button(label="عدم التسليم", style=discord.ButtonStyle.danger, emoji="❌")
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        g_conn, g_cur = get_guild_db(interaction.guild.id)
        g_cur.execute("SELECT admin_role_id FROM settings WHERE guild_id = ?", (interaction.guild.id,))
        row = g_cur.fetchone()
        g_conn.close()

        is_admin = interaction.user.id == interaction.guild.owner_id or interaction.user.id == PRIMARY_OWNER_ID
        if row and row[0]:
            role = interaction.guild.get_role(row[0])
            if role and role in interaction.user.roles:
                is_admin = True

        if not is_admin:
            return await interaction.response.send_message("⛔ المسؤولون فقط يمكنهم اتخاذ هذا الإجراء.", ephemeral=True)

        refund_server_spending(interaction.guild.id, self.amount)
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(f"🛑 تم إلغاء تسليم الجائزة لـ {self.winner.mention} بواسطة {interaction.user.mention} (أُعيد المبلغ لميزانية السيرفر).")

class BoxClaimView(discord.ui.View):
    def __init__(self, amount: int, condition: str = None):
        super().__init__(timeout=None)
        self.amount = amount
        self.condition = condition
        self.claimed = False

    @discord.ui.button(label="فتح البوكس 🎁", style=discord.ButtonStyle.success)
    async def open_box(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed:
            return await interaction.response.send_message("❌ تم فتح هذا البوكس بالفعل من قبل شخص آخر!", ephemeral=True)
        if is_blacklisted(interaction.user):
            return await interaction.response.send_message("⛔ لا يمكنك فتح البوكس؛ أنت مدرج في البلاك ليست.", ephemeral=True)

        self.claimed = True
        button.disabled = True
        button.label = "تم فتح البوكس"
        button.style = discord.ButtonStyle.secondary
        await interaction.message.edit(view=self)

        winner = interaction.user
        if not self.condition:
            update_balance(winner.id, self.amount)
            await interaction.response.send_message(f"🎉 مبروك {winner.mention}! فتحت البوكس وربحت `{self.amount:,}` Cosmo.")
            try:
                await winner.send(f"🎁 مبروك! حصلت على `{self.amount:,}` Cosmo من فتح البوكس السريع.")
            except:
                pass
        else:
            await interaction.response.send_message(f"🎉 {winner.mention} ضغط على البوكس أولاً!\n⚠️ **الشرط:** `{self.condition}`\nفي انتظار مراجعة الإدارة.")
            claim_view = ClaimVerificationView(winner, self.amount, "جائزة البوكس")
            await interaction.channel.send(f"مراجعة شرط البوكس لـ {winner.mention}:", view=claim_view)

class RouletteLobbyView(discord.ui.View):
    def __init__(self, amount: int, condition: str):
        super().__init__(timeout=None)
        self.amount = amount
        self.condition = condition
        self.participants = set()

    def make_embed(self, guild: discord.Guild):
        members_text = ""
        if self.participants:
            members_list = [guild.get_member(uid) for uid in self.participants if guild.get_member(uid)]
            members_text = "\n".join([f"• {m.mention}" for m in members_list])
        else:
            members_text = "لا يوجد مشاركون حتى الآن."

        embed = discord.Embed(
            title="🎯 لعبة الروليت | Roulette 🎯",
            description=f"**الجائزة للفائز الأخير:** `{self.amount:,}` Cosmo\n"
                        f"**الشرط:** {self.condition if self.condition else 'لا يوجد شرط'}\n"
                        f"**الحد الأدنى للبدء:** 4 لاعبين\n"
                        f"⏳ **وقت التسجيل:** 30 ثانية!\n\n"
                        f"👥 **المشاركون ({len(self.participants)}):**\n{members_text}",
            color=discord.Color.red()
        )
        return embed

    @discord.ui.button(label="دخول / خروج الروليت 🎯", style=discord.ButtonStyle.danger)
    async def join_roulette(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_blacklisted(interaction.user):
            return await interaction.response.send_message("⛔ لا يمكنك المشاركة؛ أنت في قائمة البلاك ليست.", ephemeral=True)
            
        if interaction.user.id in self.participants:
            self.participants.remove(interaction.user.id)
            await interaction.response.send_message("❌ تم خروجك من الروليت.", ephemeral=True)
        else:
            self.participants.add(interaction.user.id)
            await interaction.response.send_message("✅ تم تسجيل دخولك في الروليت!", ephemeral=True)
        try:
            await interaction.message.edit(embed=self.make_embed(interaction.guild), view=self)
        except:
            pass

class RouletteTargetSelect(discord.ui.Select):
    def __init__(self, active_members, current_turn_member):
        options = [
            discord.SelectOption(label=clean_display_name(m.display_name)[:25], value=str(m.id))
            for m in active_members if m.id != current_turn_member.id
        ]
        super().__init__(placeholder="اختر لاعباً لطرده...", min_values=1, max_values=1, options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        self.view.chosen_target_id = int(self.values[0])
        self.view.action_taken = "kick_specific"
        self.view.stop()
        await interaction.response.defer()

class RouletteTurnView(discord.ui.View):
    def __init__(self, current_player: discord.Member, active_players: list):
        super().__init__(timeout=25)
        self.current_player = current_player
        self.action_taken = None
        self.chosen_target_id = None
        self.add_item(RouletteTargetSelect(active_players, current_player))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.current_player.id:
            await interaction.response.send_message("⛔ ليس دورك الآن!", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="طرد عشوائي 🎲", style=discord.ButtonStyle.danger, row=1)
    async def kick_random(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action_taken = "kick_random"
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="انسحاب 🏳️", style=discord.ButtonStyle.secondary, row=1)
    async def surrender(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action_taken = "surrender"
        self.stop()
        await interaction.response.defer()

async def generate_roulette_wheel_image(guild: discord.Guild, participants: list, selected_index: int):
    size = 600
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = size // 2
    radius = 260
    num_parts = len(participants)
    angle_per_part = 360 / num_parts
    palette = [(107, 142, 100), (155, 110, 155), (150, 150, 140), (145, 115, 85), (90, 130, 140), (170, 130, 100)]
    font = get_unicode_font(18)

    for i in range(num_parts):
        start_angle = i * angle_per_part
        end_angle = (i + 1) * angle_per_part
        color = palette[i % len(palette)]
        draw.pieslice([center - radius, center - radius, center + radius, center + radius], start=start_angle, end=end_angle, fill=color, outline=(255, 255, 255), width=2)
        mid_angle = math.radians((start_angle + end_angle) / 2)
        tx = center + (radius * 0.65) * math.cos(mid_angle)
        ty = center + (radius * 0.65) * math.sin(mid_angle)
        name_str = clean_display_name(participants[i].display_name)
        draw.text((tx - 24, ty - 10), name_str, fill=(255, 255, 255), font=font)

    inner_radius = 80
    if guild.icon:
        try:
            icon_bytes = await guild.icon.read()
            icon_img = Image.open(io.BytesIO(icon_bytes)).convert("RGBA").resize((inner_radius * 2, inner_radius * 2))
            mask = Image.new('L', (inner_radius * 2, inner_radius * 2), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, inner_radius * 2, inner_radius * 2), fill=255)
            output_icon = ImageOps.fit(icon_img, mask.size, centering=(0.5, 0.5))
            output_icon.putalpha(mask)
            image.paste(output_icon, (center - inner_radius, center - inner_radius), output_icon)
        except:
            draw.ellipse([center - inner_radius, center - inner_radius, center + inner_radius, center + inner_radius], fill=(35, 30, 50), outline=(255, 255, 255), width=4)
    else:
        draw.ellipse([center - inner_radius, center - inner_radius, center + inner_radius, center + inner_radius], fill=(35, 30, 50), outline=(255, 255, 255), width=4)

    target_angle = math.radians((selected_index * angle_per_part) + (angle_per_part / 2))
    ptr_x = center + (radius + 20) * math.cos(target_angle)
    ptr_y = center + (radius + 20) * math.sin(target_angle)
    draw.polygon([(ptr_x, ptr_y), (ptr_x + 25 * math.cos(target_angle + 0.4), ptr_y + 25 * math.sin(target_angle + 0.4)), (ptr_x + 25 * math.cos(target_angle - 0.4), ptr_y + 25 * math.sin(target_angle - 0.4))], fill=(255, 255, 255), outline=(0, 0, 0))

    buf = io.BytesIO()
    image.save(buf, format='PNG')
    buf.seek(0)
    return buf

class Games(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        asyncio.create_task(self.resume_all_giveaways())

    async def finish_giveaway(self, guild: discord.Guild, channel: discord.TextChannel, msg_id: int, amount: int, winners_count: int, condition: str, participants: list):
        g_conn, g_cur = get_guild_db(guild.id)
        g_cur.execute("UPDATE active_giveaways SET is_finished = 1 WHERE message_id = ?", (msg_id,))
        g_conn.commit()
        g_conn.close()

        valid_participants = [uid for uid in participants if guild.get_member(uid) and not is_blacklisted(guild.get_member(uid))]
        if not valid_participants:
            refund_server_spending(guild.id, amount * winners_count)
            return await channel.send(f"⚠️ انتهى السحب على `{amount:,}` Cosmo، ولكن لم يشترك أحد (أُعيدت الميزانية للسيرفر).")

        winners_ids = random.sample(valid_participants, min(len(valid_participants), winners_count))
        winners = [guild.get_member(uid) for uid in winners_ids]
        winners_mentions = ", ".join([w.mention for w in winners])

        if len(winners) < winners_count:
            refund_amount = (winners_count - len(winners)) * amount
            refund_server_spending(guild.id, refund_amount)

        if not condition:
            for winner in winners:
                update_balance(winner.id, amount)
                try:
                    await winner.send(f"🎉 مبروك! فزت بـ `{amount:,}` Cosmo في السحب وتم تسليمها تلقائياً.")
                except:
                    pass
            await channel.send(f"🎊 مبارك للفائزين: {winners_mentions}!\nتم تسليم `{amount:,}` Cosmo تلقائياً لكل فائز.")
        else:
            await channel.send(f"🎊 الفائزون بالقرعة: {winners_mentions}!\n⚠️ **الشرط:** `{condition}`\nفي انتظار مراجعة الإدارة للتسليم.")
            for winner in winners:
                claim_view = ClaimVerificationView(winner, amount, "جائزة الجيف أواي")
                await channel.send(f"تحقق من شرط العضو {winner.mention}:", view=claim_view)

    async def monitor_giveaway(self, guild_id: int, channel_id: int, msg_id: int, amount: int, duration_sec: int, winners_count: int, condition: str):
        await asyncio.sleep(duration_sec)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(channel_id)
        if not channel:
            return

        g_conn, g_cur = get_guild_db(guild_id)
        g_cur.execute("SELECT participants_json, is_finished FROM active_giveaways WHERE message_id = ?", (msg_id,))
        row = g_cur.fetchone()
        g_conn.close()

        if row and row[1] == 0:
            participants = json.loads(row[0]) if row[0] else []
            await self.finish_giveaway(guild, channel, msg_id, amount, winners_count, condition, participants)

    async def resume_all_giveaways(self):
        await self.bot.wait_until_ready()
        now = int(time.time())
        for guild in self.bot.guilds:
            g_conn, g_cur = get_guild_db(guild.id)
            g_cur.execute("SELECT message_id, channel_id, amount, end_timestamp, winners_count, condition, participants_json FROM active_giveaways WHERE is_finished = 0")
            rows = g_cur.fetchall()
            g_conn.close()

            for r in rows:
                msg_id, chan_id, amount, end_time, winners_cnt, condition, part_json = r
                channel = guild.get_channel(chan_id)
                if not channel:
                    continue
                participants = json.loads(part_json) if part_json else []

                if now >= end_time:
                    asyncio.create_task(self.finish_giveaway(guild, channel, msg_id, amount, winners_cnt, condition, participants))
                else:
                    remaining = end_time - now
                    asyncio.create_task(self.monitor_giveaway(guild.id, chan_id, msg_id, amount, remaining, winners_cnt, condition))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or str(payload.emoji) != "🎉":
            return
        if not payload.guild_id:
            return

        g_conn, g_cur = get_guild_db(payload.guild_id)
        g_cur.execute("SELECT participants_json, is_finished, amount, winners_count, condition, end_timestamp FROM active_giveaways WHERE message_id = ?", (payload.message_id,))
        row = g_cur.fetchone()

        if not row or row[1] == 1:
            g_conn.close()
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = payload.member or (guild.get_member(payload.user_id) if guild else None)
        if member and is_blacklisted(member):
            g_conn.close()
            return

        participants = json.loads(row[0]) if row[0] else []
        if payload.user_id not in participants:
            participants.append(payload.user_id)
            g_cur.execute("UPDATE active_giveaways SET participants_json = ? WHERE message_id = ?", (json.dumps(participants), payload.message_id))
            g_conn.commit()

            channel = self.bot.get_channel(payload.channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    if msg.embeds:
                        embed = msg.embeds[0]
                        embed.description = f"**الجائزة لكل فائز:** `{row[2]:,}` Cosmo\n" \
                                            f"**عدد الفائزين:** `{row[3]}`\n" \
                                            f"**المشاركين:** `{len(participants)}`\n" \
                                            f"**الشرط:** {row[4] if row[4] else 'لا يوجد شرط'}\n" \
                                            f"**ينتهي:** <t:{row[5]}:R>"
                        await msg.edit(embed=embed)
                except:
                    pass
        g_conn.close()

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.bot.user.id or str(payload.emoji) != "🎉":
            return
        if not payload.guild_id:
            return

        g_conn, g_cur = get_guild_db(payload.guild_id)
        g_cur.execute("SELECT participants_json, is_finished, amount, winners_count, condition, end_timestamp FROM active_giveaways WHERE message_id = ?", (payload.message_id,))
        row = g_cur.fetchone()

        if not row or row[1] == 1:
            g_conn.close()
            return

        participants = json.loads(row[0]) if row[0] else []
        if payload.user_id in participants:
            participants.remove(payload.user_id)
            g_cur.execute("UPDATE active_giveaways SET participants_json = ? WHERE message_id = ?", (json.dumps(participants), payload.message_id))
            g_conn.commit()

            channel = self.bot.get_channel(payload.channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    if msg.embeds:
                        embed = msg.embeds[0]
                        embed.description = f"**الجائزة لكل فائز:** `{row[2]:,}` Cosmo\n" \
                                            f"**عدد الفائزين:** `{row[3]}`\n" \
                                            f"**المشاركين:** `{len(participants)}`\n" \
                                            f"**الشرط:** {row[4] if row[4] else 'لا يوجد شرط'}\n" \
                                            f"**ينتهي:** <t:{row[5]}:R>"
                        await msg.edit(embed=embed)
                except:
                    pass
        g_conn.close()

    @commands.hybrid_command(name="giveaway", aliases=["سحب", "جيف_اواي"], description="بدء سحب جيف أواي على عملات Cosmo بالريأكشن 🎉")
    async def giveaway(self, ctx: commands.Context, amount: str, duration: str, winners_count: int, *, condition: str = None):
        if not can_manage_giveaway(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")
        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed_amount <= 0 or winners_count <= 0:
            return await ctx.send("❌ القيم غير صحيحة.")

        total_cost = parsed_amount * winners_count
        allowed, pool_msg = deduct_server_spending(ctx.guild.id, total_cost)
        if not allowed:
            return await ctx.send(pool_msg)

        duration_seconds = parse_duration(duration)
        if not duration_seconds:
            refund_server_spending(ctx.guild.id, total_cost)
            return await ctx.send("❌ صيغة الوقت غير صحيحة.")

        end_timestamp = int(time.time()) + duration_seconds
        embed = discord.Embed(
            title="🎉 سحب على عملات Cosmo! 🎉",
            description=f"**الجائزة لكل فائز:** `{parsed_amount:,}` Cosmo\n**عدد الفائزين:** `{winners_count}`\n**المشاركين:** `0`\n**الشرط:** {condition if condition else 'لا يوجد شرط'}\n**ينتهي:** <t:{end_timestamp}:R>",
            color=discord.Color.purple()
        )
        embed.set_footer(text="تفاعل بالريأكشن 🎉 للدخول في السحب!")
        msg = await ctx.send(embed=embed)
        await msg.add_reaction("🎉")

        g_conn, g_cur = get_guild_db(ctx.guild.id)
        g_cur.execute(
            "INSERT INTO active_giveaways (message_id, channel_id, amount, end_timestamp, winners_count, condition, participants_json, is_finished) VALUES (?, ?, ?, ?, ?, ?, '[]', 0)",
            (msg.id, ctx.channel.id, parsed_amount, end_timestamp, winners_count, condition)
        )
        g_conn.commit()
        g_conn.close()

        asyncio.create_task(self.monitor_giveaway(ctx.guild.id, ctx.channel.id, msg.id, parsed_amount, duration_seconds, winners_count, condition))

    @commands.hybrid_command(name="box", aliases=["بوكس"], description="إنشاء بوكس سريع لأول شخص يضغط على الزر")
    async def box(self, ctx: commands.Context, amount: str, *, condition: str = None):
        if not can_manage_giveaway(ctx):
            return await ctx.send("⛔ هذا الأمر متاح لمالك السيرفر أو لمسؤول البوت المخوّل.")
        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed_amount <= 0:
            return await ctx.send("❌ المبلغ يجب أن يكون أكبر من 0.")

        allowed, pool_msg = deduct_server_spending(ctx.guild.id, parsed_amount)
        if not allowed:
            return await ctx.send(pool_msg)

        embed = discord.Embed(
            title="🎁 بوكس Cosmo السريع! 🎁",
            description=f"**الجائزة:** `{parsed_amount:,}` Cosmo\n**الشرط:** {condition if condition else 'لا يوجد شرط (تسليم فوري)'}\n**القاعدة:** أول من يضغط على الزر يفوز بالجائزة فوراً!",
            color=discord.Color.gold()
        )
        embed.set_footer(text="كن الأسرع واضغط على الزر أدناه!")
        view = BoxClaimView(parsed_amount, condition)
        await ctx.send(embed=embed, view=view)

    @commands.hybrid_command(name="roulette", aliases=["روليت"], description="بدء لعبة الروليت التنافسية مع العجلة الرسومية")
    async def roulette(self, ctx: commands.Context, amount: str, *, condition: str = None):
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية (أو انتهى اشتراك البريميوم بالسيرفر).")
        if ctx.guild.id in active_roulettes:
            return await ctx.send("⚠️ هناك لعبة روليت جارية بالفعل!")

        try:
            parsed_amount = parse_amount(amount)
        except:
            return await ctx.send("❌ صيغة المبلغ غير صحيحة.")
        if parsed_amount <= 0:
            return await ctx.send("❌ المبلغ يجب أن يكون أكبر من 0.")

        allowed, pool_msg = deduct_server_spending(ctx.guild.id, parsed_amount)
        if not allowed:
            return await ctx.send(pool_msg)

        session = RouletteSession(ctx.guild.id)
        active_roulettes[ctx.guild.id] = session

        lobby_view = RouletteLobbyView(parsed_amount, condition)
        session.current_view = lobby_view
        await ctx.send(embed=lobby_view.make_embed(ctx.guild), view=lobby_view)

        for _ in range(30):
            if session.is_stopped:
                active_roulettes.pop(ctx.guild.id, None)
                refund_server_spending(ctx.guild.id, parsed_amount)
                return
            await asyncio.sleep(1)

        participants = [ctx.guild.get_member(uid) for uid in lobby_view.participants if ctx.guild.get_member(uid) and not is_blacklisted(ctx.guild.get_member(uid))]
        if len(participants) < 4:
            active_roulettes.pop(ctx.guild.id, None)
            refund_server_spending(ctx.guild.id, parsed_amount)
            return await ctx.channel.send("⚠️ تم إلغاء الروليت لعدم اكتمال 4 لاعبين على الأقل (أُعيدت الجائزة لميزانية السيرفر).")

        round_num = 1
        while len(participants) > 2:
            if session.is_stopped:
                active_roulettes.pop(ctx.guild.id, None)
                refund_server_spending(ctx.guild.id, parsed_amount)
                return

            chosen_index = random.randint(0, len(participants) - 1)
            turn_player = participants[chosen_index]

            wheel_buf = await generate_roulette_wheel_image(ctx.guild, participants, chosen_index)
            wheel_file = discord.File(wheel_buf, filename="wheel.png")
            await ctx.channel.send(f"🎡 **الجولة {round_num}: تم تدوير العجلة ووقفت على {turn_player.mention}!**", file=wheel_file)

            turn_view = RouletteTurnView(turn_player, participants)
            session.current_view = turn_view
            await ctx.channel.send(f"🎲 {turn_player.mention} معك **25 ثانية** لاختيار قرارك:", view=turn_view)
            await turn_view.wait()

            if session.is_stopped:
                active_roulettes.pop(ctx.guild.id, None)
                refund_server_spending(ctx.guild.id, parsed_amount)
                return

            eliminated = None
            if turn_view.action_taken == "kick_random":
                others = [p for p in participants if p.id != turn_player.id]
                eliminated = random.choice(others)
                await ctx.channel.send(f"🎲 طرد عشوائي: تم طرد {eliminated.mention}!")
            elif turn_view.action_taken == "kick_specific" and turn_view.chosen_target_id:
                eliminated = ctx.guild.get_member(turn_view.chosen_target_id)
                await ctx.channel.send(f"🎯 طرد محدد: قرر {turn_player.mention} طرد {eliminated.mention}!")
            else:
                eliminated = turn_player
                await ctx.channel.send(f"⏱️ تم استبعاد {turn_player.mention} لانتهاء الوقت أو الانسحاب.")

            if eliminated and eliminated in participants:
                participants.remove(eliminated)

            round_num += 1
            await asyncio.sleep(2)

        if session.is_stopped:
            active_roulettes.pop(ctx.guild.id, None)
            refund_server_spending(ctx.guild.id, parsed_amount)
            return

        await ctx.channel.send(f"🚨 **المواجهة النهائية الحاسمة بين:** {participants[0].mention} و {participants[1].mention}!\nالعجلة تدور الآن لتحديد الفائز...")
        await asyncio.sleep(2)

        winner_index = random.randint(0, 1)
        final_winner = participants[winner_index]

        final_wheel_buf = await generate_roulette_wheel_image(ctx.guild, participants, winner_index)
        final_wheel_file = discord.File(final_wheel_buf, filename="final_wheel.png")
        await ctx.channel.send(f"🎡 **العجلة وقفت على الفائز النهائي:** {final_winner.mention}!", file=final_wheel_file)

        active_roulettes.pop(ctx.guild.id, None)

        if not condition:
            update_balance(final_winner.id, parsed_amount)
            await ctx.channel.send(f"👑 **مبروك للفائز بالروليت:** {final_winner.mention}!\nتم تسليم `{parsed_amount:,}` Cosmo إلى رصيدك تلقائياً.")
            try:
                await final_winner.send(f"🎉 مبروك! فزت في لعبة الروليت وحصلت على `{parsed_amount:,}` Cosmo.")
            except:
                pass
        else:
            await ctx.channel.send(
                f"👑 **الفائز النهائي:** {final_winner.mention}!\n"
                f"⚠️ **الشرط المطلوب:** `{condition}`\nفي انتظار مراجعة الإدارة للتسليم."
            )
            claim_view = ClaimVerificationView(final_winner, parsed_amount, "جائزة الروليت")
            await ctx.channel.send(f"تحقق من شرط فائز الروليت {final_winner.mention}:", view=claim_view)

    @commands.hybrid_command(name="stop_roulette", aliases=["ايقاف_الروليت"], description="إيقاف لعبة الروليت الجارية فوراً")
    async def stop_roulette(self, ctx: commands.Context):
        if not is_bot_admin(ctx):
            return await ctx.send("⛔ ليس لديك صلاحية (أو انتهى اشتراك البريميوم بالسيرفر).")
        if ctx.guild.id not in active_roulettes:
            return await ctx.send("❌ لا توجد لعبة نشطة.")
        session = active_roulettes.pop(ctx.guild.id)
        session.stop()
        await ctx.send("🛑 تم إيقاف لعبة الروليت بدون فائز (أُعيدت الميزانية للسيرفر).")

async def setup(bot):
    await bot.add_cog(Games(bot))
